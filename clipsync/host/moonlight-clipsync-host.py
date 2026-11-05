#!/usr/bin/env python3
"""Moonlight ClipSync host sidecar.

Bidirectional plain-text clipboard sync between a host (KDE Plasma via Klipper on
D-Bus, or Windows via the Win32 clipboard API) and a Moonlight client fork that implements the ClipSync protocol.

Protocol: newline-delimited JSON over TCP.
  client -> host : {"t":"hello","token":"<shared secret>"}
  host   -> client: {"t":"hello","ok":true}   (or ok:false, then close)
  either direction: {"t":"clip","text":"<utf-8 text>"}

Config file (KEY=VALUE), default ~/.config/moonlight-clipsync/config:
  TOKEN=<shared secret, required>
  BIND=<ip to listen on, default 127.0.0.1; use the Tailscale IP>
  PORT=<tcp port, default 48100>

Clipboard contents are never logged.
"""

import asyncio
import hmac
import json
import os
import sys
import threading
from pathlib import Path
import time

if sys.platform != "win32":
    import dbus
    import dbus.mainloop.glib
    from gi.repository import GLib

MAX_TEXT_BYTES = 256 * 1024
MAX_LINE_BYTES = 2 * 1024 * 1024  # JSON escaping can inflate the payload
AUTH_TIMEOUT_S = 5


def log(msg: str) -> None:
    print(msg, flush=True)


def load_config() -> dict:
    path = Path(os.environ.get("CLIPSYNC_CONFIG", "~/.config/moonlight-clipsync/config")).expanduser()
    cfg = {"BIND": "127.0.0.1", "PORT": "48100"}
    for raw in path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        cfg[key.strip()] = value.strip()
    if not cfg.get("TOKEN"):
        sys.exit(f"TOKEN missing in {path}")
    return cfg


class Klipper:
    """Owns the D-Bus connection; all calls run on the GLib thread."""

    def __init__(self, on_change):
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        self._bus = dbus.SessionBus()
        self._on_change = on_change
        self._iface = None
        self._bus.add_signal_receiver(
            self._signal,
            signal_name="clipboardHistoryUpdated",
            dbus_interface="org.kde.klipper.klipper",
            path="/klipper",
        )
        self._loop = GLib.MainLoop()
        self._thread = threading.Thread(target=self._loop.run, name="glib", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _klipper(self):
        # Re-resolved on every call so a Klipper restart is survived.
        proxy = self._bus.get_object("org.kde.klipper", "/klipper")
        return dbus.Interface(proxy, "org.kde.klipper.klipper")

    def _signal(self) -> None:
        try:
            text = str(self._klipper().getClipboardContents())
        except dbus.DBusException as exc:
            log(f"klipper read failed: {exc.get_dbus_name()}")
            return
        self._on_change(text)

    def set_text(self, text: str) -> None:
        def run():
            try:
                self._klipper().setClipboardContents(text)
            except dbus.DBusException as exc:
                log(f"klipper write failed: {exc.get_dbus_name()}")
            return False

        GLib.idle_add(run)


class WinClipboard:
    """Win32 clipboard (CF_UNICODETEXT), change detection by sequence number."""

    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002
    POLL_INTERVAL_S = 0.15

    def __init__(self, on_change):
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._on_change = on_change
        self._lock = threading.Lock()

        u32 = ctypes.WinDLL("user32", use_last_error=True)
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        u32.OpenClipboard.argtypes = [wintypes.HWND]
        u32.OpenClipboard.restype = wintypes.BOOL
        u32.CloseClipboard.restype = wintypes.BOOL
        u32.EmptyClipboard.restype = wintypes.BOOL
        u32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
        u32.IsClipboardFormatAvailable.restype = wintypes.BOOL
        u32.GetClipboardData.argtypes = [wintypes.UINT]
        u32.GetClipboardData.restype = wintypes.HANDLE
        u32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
        u32.SetClipboardData.restype = wintypes.HANDLE
        u32.GetClipboardSequenceNumber.restype = wintypes.DWORD
        k32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        k32.GlobalAlloc.restype = wintypes.HGLOBAL
        k32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        k32.GlobalLock.restype = wintypes.LPVOID
        k32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        k32.GlobalFree.argtypes = [wintypes.HGLOBAL]
        self._u32, self._k32 = u32, k32
        self._thread = threading.Thread(target=self._poll, name="clipboard-poll", daemon=True)

    def start(self) -> None:
        self._seq = self._u32.GetClipboardSequenceNumber()  # do not send what is already there
        self._thread.start()

    def _open(self) -> bool:
        for _ in range(20):  # another process may hold the clipboard briefly
            if self._u32.OpenClipboard(None):
                return True
            time.sleep(0.01)
        return False

    def _get_text(self):
        if not self._open():
            return None
        try:
            if not self._u32.IsClipboardFormatAvailable(self.CF_UNICODETEXT):
                return None
            handle = self._u32.GetClipboardData(self.CF_UNICODETEXT)
            if not handle:
                return None
            ptr = self._k32.GlobalLock(handle)
            if not ptr:
                return None
            try:
                return self._ctypes.wstring_at(ptr).replace("\r\n", "\n")
            finally:
                self._k32.GlobalUnlock(handle)
        finally:
            self._u32.CloseClipboard()

    def _poll(self) -> None:
        while True:
            time.sleep(self.POLL_INTERVAL_S)
            with self._lock:
                seq = self._u32.GetClipboardSequenceNumber()
                if seq == self._seq:
                    continue
                self._seq = seq
                text = self._get_text()
            if text:
                self._on_change(text)

    def set_text(self, text: str) -> None:
        data = (text.replace("\r\n", "\n").replace("\n", "\r\n") + "\0").encode("utf-16-le")
        with self._lock:
            if not self._open():
                log("clipboard write failed: clipboard is locked by another process")
                return
            try:
                handle = self._k32.GlobalAlloc(self.GMEM_MOVEABLE, len(data))
                ptr = self._k32.GlobalLock(handle)
                self._ctypes.memmove(ptr, data, len(data))
                self._k32.GlobalUnlock(handle)
                self._u32.EmptyClipboard()
                if not self._u32.SetClipboardData(self.CF_UNICODETEXT, handle):
                    self._k32.GlobalFree(handle)
                    log("clipboard write failed: SetClipboardData")
            finally:
                self._u32.CloseClipboard()
            self._seq = self._u32.GetClipboardSequenceNumber()


class Server:
    def __init__(self, cfg: dict):
        self.token = cfg["TOKEN"].encode()
        self.bind = cfg["BIND"]
        self.port = int(cfg["PORT"])
        self.writers = set()
        self.last_text = None
        self.loop = None
        self.clipboard = None

    # ---- host clipboard -> clients (called from the GLib thread) ----
    def _host_changed(self, text: str) -> None:
        self.loop.call_soon_threadsafe(self._broadcast_host_text, text)

    def _broadcast_host_text(self, text: str) -> None:
        if not text or text == self.last_text or len(text.encode()) > MAX_TEXT_BYTES:
            return
        self.last_text = text
        line = (json.dumps({"t": "clip", "text": text}) + "\n").encode()
        for w in list(self.writers):
            w.write(line)

    # ---- client -> host clipboard ----
    def _client_text(self, text: str, origin) -> None:
        if not text or text == self.last_text or len(text.encode()) > MAX_TEXT_BYTES:
            return
        self.last_text = text
        self.clipboard.set_text(text)
        line = (json.dumps({"t": "clip", "text": text}) + "\n").encode()
        for w in list(self.writers):
            if w is not origin:
                w.write(line)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        try:
            hello = json.loads(await asyncio.wait_for(reader.readline(), AUTH_TIMEOUT_S))
            ok = (
                hello.get("t") == "hello"
                and isinstance(hello.get("token"), str)
                and hmac.compare_digest(hello["token"].encode(), self.token)
            )
            writer.write((json.dumps({"t": "hello", "ok": ok}) + "\n").encode())
            await writer.drain()
            if not ok:
                log(f"auth rejected from {peer}")
                return
            log(f"client connected: {peer}")
            self.writers.add(writer)
            while True:
                line = await reader.readline()
                if not line:
                    break
                msg = json.loads(line)
                if msg.get("t") == "clip" and isinstance(msg.get("text"), str):
                    self._client_text(msg["text"], writer)
        except (asyncio.TimeoutError, asyncio.LimitOverrunError, ValueError, ConnectionError, OSError):
            pass
        finally:
            if writer in self.writers:
                self.writers.discard(writer)
                log(f"client disconnected: {peer}")
            writer.close()

    async def run(self) -> None:
        self.loop = asyncio.get_running_loop()
        backend = WinClipboard if sys.platform == "win32" else Klipper
        self.clipboard = backend(self._host_changed)
        self.clipboard.start()
        while True:  # the Tailscale interface may not be up yet at boot
            try:
                server = await asyncio.start_server(
                    self.handle, self.bind, self.port, limit=MAX_LINE_BYTES
                )
                break
            except OSError as exc:
                log(f"bind {self.bind}:{self.port} failed ({exc}); retrying in 5s")
                await asyncio.sleep(5)
        log(f"listening on {self.bind}:{self.port}")
        async with server:
            await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(Server(load_config()).run())
