# ClipSync

Bidirectional plain-text clipboard sync for Moonlight streams, without changing Sunshine.

```
Moonlight client (this fork)  <-- TCP, newline-delimited JSON, shared token -->  moonlight-clipsync-host  <-- D-Bus -->  Klipper (KDE)
```

Unlike the stock client, text copied on the host shows up in the client clipboard
and vice versa. Text only, max 256 KiB, no files or images.

## Host (KDE Plasma, e.g. CachyOS)

Requires Python 3 with `dbus-python` and `PyGObject` (`python-dbus`, `python-gobject`) and Klipper running.

1. Install the script and a config file:

   ```
   install -Dm755 clipsync/host/moonlight-clipsync-host.py ~/.local/bin/moonlight-clipsync-host
   mkdir -p ~/.config/moonlight-clipsync
   umask 077
   cat > ~/.config/moonlight-clipsync/config <<EOF
   TOKEN=$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')
   BIND=<ip to listen on, e.g. your Tailscale IP>
   PORT=48100
   EOF
   ```

2. Run it as a user service (`~/.config/systemd/user/moonlight-clipsync.service`):

   ```
   [Unit]
   Description=Moonlight ClipSync host sidecar
   After=graphical-session.target

   [Service]
   ExecStart=%h/.local/bin/moonlight-clipsync-host
   Restart=always
   RestartSec=3

   [Install]
   WantedBy=graphical-session.target
   ```

   `systemctl --user daemon-reload && systemctl --user enable --now moonlight-clipsync`

3. Make sure the firewall allows the port on the interface you bound to.

`BIND` defaults to `127.0.0.1`. Bind to a private interface (Tailscale/LAN), never to a
public one: the token is the only authentication and traffic is not encrypted by ClipSync itself.

## Client (this fork)

Settings → *Sync clipboard with host (ClipSync)*, then set:

- **Host**: address of the sidecar; empty uses the address you stream from.
- **Port**: `48100` unless changed.
- **Token**: the `TOKEN` from the host config.

Sync runs only while a stream is active. Anything copied on the client during a stream
is sent to the host, including from other windows, so leave it off if that matters.
