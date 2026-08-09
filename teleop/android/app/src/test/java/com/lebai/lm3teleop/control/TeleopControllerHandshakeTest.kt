package com.lebai.lm3teleop.control

import com.lebai.lm3teleop.network.ConnectionConfig
import com.lebai.lm3teleop.network.TeleopTransport
import com.lebai.lm3teleop.network.TeleopTransportListener
import com.lebai.lm3teleop.network.TransportState
import com.lebai.lm3teleop.protocol.ServerMessage
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.Executors

class TeleopControllerHandshakeTest {
    @Test
    fun connectRequiresNoCredential() {
        val scheduler = Executors.newSingleThreadScheduledExecutor()
        lateinit var transport: CapturingTransport
        val controller = TeleopController(
            clientId = "client-1",
            appVersion = "test",
            listener = {},
            scheduler = scheduler,
            transportFactory = { listener ->
                CapturingTransport(listener).also { transport = it }
            },
        )

        try {
            controller.connect("wss://robot.example.com/teleop", "phone")

            assertEquals("phone", transport.connectedConfig?.clientName)
        } finally {
            controller.destroy()
            scheduler.shutdownNow()
        }
    }

    @Test
    fun preWelcomeHandshakeErrorIsShownAndConnectionIsCancelled() {
        val scheduler = Executors.newSingleThreadScheduledExecutor()
        val snapshots = CopyOnWriteArrayList<ControllerSnapshot>()
        lateinit var transport: CapturingTransport
        val controller = TeleopController(
            clientId = "client-1",
            appVersion = "test",
            listener = snapshots::add,
            scheduler = scheduler,
            transportFactory = { listener ->
                CapturingTransport(listener).also { transport = it }
            },
        )

        try {
            controller.connect("wss://robot.example.com/teleop", "phone")
            transport.listener.onTransportState(TransportState.OPEN, "open")
            transport.listener.onServerMessage(
                ServerMessage.Error(
                    seq = 0L,
                    sentAtMs = 1_000L,
                    ackSeq = 0L,
                    code = "HELLO_REQUIRED",
                    message = "session.hello required",
                    recoverable = false,
                ),
            )

            val snapshot = snapshots.last()
            assertEquals(TransportState.FAILED, snapshot.transportState)
            assertFalse(snapshot.welcomeReceived)
            assertTrue(snapshot.lastEvent.contains("HELLO_REQUIRED: session.hello required"))
            assertEquals(1, transport.cancelCount)
        } finally {
            scheduler.shutdownNow()
        }
    }

    private class CapturingTransport(
        val listener: TeleopTransportListener,
    ) : TeleopTransport {
        var cancelCount = 0
        var connectedConfig: ConnectionConfig? = null

        override fun connect(config: ConnectionConfig) {
            connectedConfig = config
        }

        override fun send(type: String, body: JSONObject): Long? = null

        override fun closeGracefully(reason: String) = Unit

        override fun cancelNow() {
            cancelCount += 1
        }

        override fun shutdown() = Unit
    }
}
