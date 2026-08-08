package com.lebai.lm3teleop.network

import com.lebai.lm3teleop.protocol.ProtocolBodies
import com.lebai.lm3teleop.protocol.ProtocolCodec
import com.lebai.lm3teleop.protocol.ProtocolException
import com.lebai.lm3teleop.protocol.ServerMessage
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONObject
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.TimeUnit

class ConnectionConfig(
    val url: String,
    val clientId: String,
    val clientName: String,
    val appVersion: String,
    internal val authToken: String,
) {
    override fun toString(): String =
        "ConnectionConfig(url=$url, clientId=$clientId, clientName=$clientName, appVersion=$appVersion, authToken=<redacted>)"
}

enum class TransportState {
    DISCONNECTED,
    CONNECTING,
    OPEN,
    CLOSING,
    FAILED,
}

interface TeleopTransportListener {
    fun onTransportState(state: TransportState, detail: String)
    fun onServerMessage(message: ServerMessage)
    fun onProtocolFailure(detail: String)
}

interface TeleopTransport {
    fun connect(config: ConnectionConfig)
    fun send(type: String, body: JSONObject): Long?
    fun closeGracefully(reason: String)
    fun cancelNow()
    fun shutdown()
}

internal class ProtocolFailureGate {
    private val failed = AtomicBoolean(false)

    fun reset() {
        failed.set(false)
    }

    fun enterOnce(): Boolean = failed.compareAndSet(false, true)
}

class TeleopWebSocket(
    private val listener: TeleopTransportListener,
    private val codec: ProtocolCodec = ProtocolCodec(),
) : TeleopTransport {

    private val httpClient = OkHttpClient.Builder()
        .connectTimeout(5, TimeUnit.SECONDS)
        .readTimeout(0, TimeUnit.MILLISECONDS)
        .writeTimeout(5, TimeUnit.SECONDS)
        .pingInterval(5, TimeUnit.SECONDS)
        .retryOnConnectionFailure(false)
        .build()

    private val lock = Any()
    private val failureGate = ProtocolFailureGate()
    private var webSocket: WebSocket? = null
    private var pendingConfig: ConnectionConfig? = null

    override fun connect(config: ConnectionConfig) {
        synchronized(lock) {
            webSocket?.cancel()
            webSocket = null
            pendingConfig = config
            codec.reset()
            failureGate.reset()
        }
        listener.onTransportState(TransportState.CONNECTING, "正在连接")
        val request = Request.Builder()
            .url(config.url)
            .header("User-Agent", "LM3UPTeleop/${config.appVersion} Android")
            .build()
        val socket = httpClient.newWebSocket(request, SocketListener())
        synchronized(lock) {
            webSocket = socket
        }
    }

    override fun send(type: String, body: JSONObject): Long? {
        val result = synchronized(lock) {
            val socket = webSocket ?: return@synchronized null
            val frame = codec.encode(type, body)
            frame.seq to socket.send(frame.json)
        }
        if (result == null) {
            notifyProtocolFailureOnce("WebSocket 不可用，无法发送：$type")
            return null
        }
        return if (result.second) {
            result.first
        } else {
            notifyProtocolFailureOnce("WebSocket 发送队列已关闭：$type")
            null
        }
    }

    override fun closeGracefully(reason: String) {
        val socket = synchronized(lock) { webSocket }
        if (socket == null) {
            listener.onTransportState(TransportState.DISCONNECTED, reason)
            return
        }
        listener.onTransportState(TransportState.CLOSING, reason)
        if (!socket.close(1000, reason.take(120))) {
            notifyProtocolFailureOnce("WebSocket 关闭队列已关闭")
        }
    }

    override fun cancelNow() {
        val socket = synchronized(lock) {
            val current = webSocket
            webSocket = null
            pendingConfig = null
            current
        }
        socket?.cancel()
    }

    override fun shutdown() {
        cancelNow()
        httpClient.dispatcher.executorService.shutdown()
        httpClient.connectionPool.evictAll()
        httpClient.cache?.close()
    }

    private inner class SocketListener : WebSocketListener() {
        override fun onOpen(webSocket: WebSocket, response: Response) {
            val config = synchronized(lock) {
                if (this@TeleopWebSocket.webSocket !== webSocket) {
                    null
                } else {
                    pendingConfig.also { pendingConfig = null }
                }
            }
            if (config == null) {
                webSocket.close(1000, "stale connection")
                return
            }
            listener.onTransportState(TransportState.OPEN, "WebSocket 已连接，等待认证")
            val hello = codec.encode(
                "session.hello",
                ProtocolBodies.sessionHello(
                    clientId = config.clientId,
                    clientName = config.clientName,
                    appVersion = config.appVersion,
                    authToken = config.authToken,
                ),
            )
            if (!webSocket.send(hello.json)) {
                notifyProtocolFailureOnce("无法发送 session.hello")
                webSocket.cancel()
            }
        }

        override fun onMessage(webSocket: WebSocket, text: String) {
            if (synchronized(lock) { this@TeleopWebSocket.webSocket !== webSocket }) return
            try {
                listener.onServerMessage(codec.parse(text))
            } catch (error: ProtocolException) {
                notifyProtocolFailureOnce(error.message ?: "协议解析失败")
            } catch (error: Exception) {
                notifyProtocolFailureOnce("协议解析异常：${error.message}")
            }
        }

        override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
            if (synchronized(lock) { this@TeleopWebSocket.webSocket !== webSocket }) return
            listener.onTransportState(TransportState.CLOSING, "服务端关闭：$code $reason")
            webSocket.close(code, reason)
        }

        override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
            val current = synchronized(lock) {
                if (this@TeleopWebSocket.webSocket === webSocket) {
                    this@TeleopWebSocket.webSocket = null
                    true
                } else {
                    false
                }
            }
            if (current) listener.onTransportState(TransportState.DISCONNECTED, "连接已关闭：$code $reason")
        }

        override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
            val current = synchronized(lock) {
                if (this@TeleopWebSocket.webSocket === webSocket) {
                    this@TeleopWebSocket.webSocket = null
                    pendingConfig = null
                    true
                } else {
                    false
                }
            }
            if (!current) return
            val status = response?.code?.let { "HTTP $it" } ?: t.javaClass.simpleName
            listener.onTransportState(TransportState.FAILED, "$status：${t.message.orEmpty()}")
        }
    }

    private fun notifyProtocolFailureOnce(detail: String) {
        if (failureGate.enterOnce()) listener.onProtocolFailure(detail)
    }
}
