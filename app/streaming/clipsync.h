#pragma once

#include <QObject>
#include <QString>
#include <QThread>
#include <QByteArray>

#include <string>

class QTcpSocket;
class QTimer;

// SDL user event code used to hand text received from the host to the SDL thread.
// data1 holds an SDL_malloc'd UTF-8 string that the receiver must SDL_free().
#define SDL_CODE_CLIPBOARD_SET 106

// Network side of ClipSync. Lives on its own thread because the Qt event loop
// of the main thread is suspended for the whole duration of a stream.
class ClipSyncWorker : public QObject
{
    Q_OBJECT

public:
    ClipSyncWorker(QString host, quint16 port, QString token);

public slots:
    void start();
    void stop();
    void sendText(QString text);

private slots:
    void connectNow();
    void onConnected();
    void onReadyRead();
    void scheduleReconnect();

private:
    void handleLine(const QByteArray& line);

    QString m_Host;
    quint16 m_Port;
    QString m_Token;

    QTcpSocket* m_Socket = nullptr;
    QTimer* m_ReconnectTimer = nullptr;
    QByteArray m_Buffer;
    bool m_Ready = false;   // host accepted our token
    bool m_Stopped = false; // stop() called, or the host rejected our token
};

// Bidirectional plain-text clipboard sync with the moonlight-clipsync-host
// sidecar running on the streaming host. All public methods except the
// destructor must be called from the SDL (main) thread.
class ClipSync
{
public:
    ClipSync(const QString& host, quint16 port, const QString& token);
    ~ClipSync();

    // Call on SDL_CLIPBOARDUPDATE: forwards the local clipboard to the host.
    void onLocalClipboardChanged();

    // Call on SDL_CODE_CLIPBOARD_SET: places host text on the local clipboard.
    void applyRemoteText(const char* utf8Text);

private:
    QThread m_Thread;
    ClipSyncWorker* m_Worker;

    // Last text that crossed the wire in either direction; breaks echo loops.
    std::string m_LastText;
};
