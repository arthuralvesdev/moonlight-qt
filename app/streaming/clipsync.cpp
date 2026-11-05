#include "clipsync.h"

#include <SDL.h>

#include <QJsonDocument>
#include <QJsonObject>
#include <QMetaObject>
#include <QTcpSocket>
#include <QTimer>

// Must match MAX_TEXT_BYTES in the host sidecar.
static const int MAX_TEXT_BYTES = 256 * 1024;
// A JSON-escaped 256 KiB payload can be several times larger than the text.
static const int MAX_LINE_BYTES = 2 * 1024 * 1024;
static const int RECONNECT_INTERVAL_MS = 3000;

ClipSyncWorker::ClipSyncWorker(QString host, quint16 port, QString token)
    : m_Host(std::move(host)),
      m_Port(port),
      m_Token(std::move(token))
{
}

void ClipSyncWorker::start()
{
    m_Socket = new QTcpSocket(this);
    m_ReconnectTimer = new QTimer(this);
    m_ReconnectTimer->setSingleShot(true);
    m_ReconnectTimer->setInterval(RECONNECT_INTERVAL_MS);

    connect(m_Socket, &QTcpSocket::connected, this, &ClipSyncWorker::onConnected);
    connect(m_Socket, &QTcpSocket::readyRead, this, &ClipSyncWorker::onReadyRead);
    connect(m_Socket, &QTcpSocket::disconnected, this, &ClipSyncWorker::scheduleReconnect);
    connect(m_Socket, &QAbstractSocket::errorOccurred, this, &ClipSyncWorker::scheduleReconnect);
    connect(m_ReconnectTimer, &QTimer::timeout, this, &ClipSyncWorker::connectNow);

    connectNow();
}

void ClipSyncWorker::stop()
{
    m_Stopped = true;
    m_Ready = false;
    if (m_ReconnectTimer) {
        m_ReconnectTimer->stop();
    }
    if (m_Socket) {
        m_Socket->abort();
    }
}

void ClipSyncWorker::connectNow()
{
    if (m_Stopped) {
        return;
    }

    m_Ready = false;
    m_Buffer.clear();
    m_Socket->abort();
    m_Socket->connectToHost(m_Host, m_Port);
}

void ClipSyncWorker::scheduleReconnect()
{
    m_Ready = false;
    if (!m_Stopped && !m_ReconnectTimer->isActive()) {
        m_ReconnectTimer->start();
    }
}

void ClipSyncWorker::onConnected()
{
    QJsonObject hello;
    hello["t"] = "hello";
    hello["token"] = m_Token;
    m_Socket->write(QJsonDocument(hello).toJson(QJsonDocument::Compact) + '\n');
}

void ClipSyncWorker::onReadyRead()
{
    m_Buffer.append(m_Socket->readAll());

    for (;;) {
        int newline = m_Buffer.indexOf('\n');
        if (newline < 0) {
            if (m_Buffer.size() > MAX_LINE_BYTES) {
                SDL_LogWarn(SDL_LOG_CATEGORY_APPLICATION,
                            "ClipSync: oversized message from host, dropping connection");
                m_Socket->abort();
            }
            return;
        }

        QByteArray line = m_Buffer.left(newline);
        m_Buffer.remove(0, newline + 1);
        handleLine(line);
    }
}

void ClipSyncWorker::handleLine(const QByteArray& line)
{
    QJsonObject msg = QJsonDocument::fromJson(line).object();
    QString type = msg["t"].toString();

    if (type == "hello") {
        if (msg["ok"].toBool()) {
            m_Ready = true;
            SDL_LogInfo(SDL_LOG_CATEGORY_APPLICATION, "ClipSync: connected to host");
        }
        else {
            // A wrong token never becomes right by retrying.
            SDL_LogError(SDL_LOG_CATEGORY_APPLICATION,
                         "ClipSync: host rejected the token, clipboard sync disabled for this session");
            m_Stopped = true;
            m_Socket->abort();
        }
    }
    else if (type == "clip" && m_Ready) {
        QByteArray utf8 = msg["text"].toString().toUtf8();
        if (utf8.isEmpty() || utf8.size() > MAX_TEXT_BYTES) {
            return;
        }

        SDL_Event event = {};
        event.type = SDL_USEREVENT;
        event.user.code = SDL_CODE_CLIPBOARD_SET;
        event.user.data1 = SDL_strdup(utf8.constData());
        if (SDL_PushEvent(&event) <= 0) {
            SDL_free(event.user.data1);
        }
    }
}

void ClipSyncWorker::sendText(QString text)
{
    if (!m_Ready || m_Stopped) {
        return;
    }

    QJsonObject msg;
    msg["t"] = "clip";
    msg["text"] = text;
    m_Socket->write(QJsonDocument(msg).toJson(QJsonDocument::Compact) + '\n');
}

ClipSync::ClipSync(const QString& host, quint16 port, const QString& token)
    : m_Worker(new ClipSyncWorker(host, port, token))
{
    m_Worker->moveToThread(&m_Thread);
    QObject::connect(&m_Thread, &QThread::finished, m_Worker, &QObject::deleteLater);
    m_Thread.start();
    QMetaObject::invokeMethod(m_Worker, "start", Qt::QueuedConnection);
}

ClipSync::~ClipSync()
{
    QMetaObject::invokeMethod(m_Worker, "stop", Qt::BlockingQueuedConnection);
    m_Thread.quit();
    m_Thread.wait();
}

void ClipSync::onLocalClipboardChanged()
{
    if (!SDL_HasClipboardText()) {
        return;
    }

    char* text = SDL_GetClipboardText();
    if (text == nullptr) {
        return;
    }

    std::string current(text);
    SDL_free(text);

    if (current.empty() || current.size() > MAX_TEXT_BYTES || current == m_LastText) {
        return;
    }

    m_LastText = current;
    QMetaObject::invokeMethod(m_Worker, "sendText", Qt::QueuedConnection,
                              Q_ARG(QString, QString::fromUtf8(current.c_str())));
}

void ClipSync::applyRemoteText(const char* utf8Text)
{
    if (utf8Text == nullptr || m_LastText == utf8Text) {
        return;
    }

    // Record first: SDL_SetClipboardText() raises SDL_CLIPBOARDUPDATE, which
    // onLocalClipboardChanged() must recognise as our own write.
    m_LastText = utf8Text;
    SDL_SetClipboardText(utf8Text);
}
