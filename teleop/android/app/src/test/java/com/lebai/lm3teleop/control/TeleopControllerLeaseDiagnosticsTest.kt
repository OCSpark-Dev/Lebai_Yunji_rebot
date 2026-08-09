package com.lebai.lm3teleop.control

import com.lebai.lm3teleop.core.SafetyChecklist
import com.lebai.lm3teleop.network.ConnectionConfig
import com.lebai.lm3teleop.network.TeleopTransport
import com.lebai.lm3teleop.network.TeleopTransportListener
import com.lebai.lm3teleop.network.TransportState
import com.lebai.lm3teleop.protocol.ServerMessage
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicLong

class TeleopControllerLeaseDiagnosticsTest {
    @Test
    fun heartbeatAckAfter150msDoesNotHideControlRevocationReason() {
        val scheduler = Executors.newSingleThreadScheduledExecutor()
        val monotonicNowMs = AtomicLong(1_000L)
        val snapshots = CopyOnWriteArrayList<ControllerSnapshot>()
        lateinit var transport: CapturingTransport
        val controller = TeleopController(
            clientId = "client-1",
            appVersion = "test",
            listener = snapshots::add,
            monotonicClock = monotonicNowMs::get,
            scheduler = scheduler,
            transportFactory = { listener ->
                CapturingTransport(listener).also { transport = it }
            },
        )

        try {
            establishLease(controller, transport.listener)
            transport.listener.onServerMessage(
                ServerMessage.ControlStatus(
                    seq = 3L,
                    sentAtMs = 5_010L,
                    granted = false,
                    leaseId = null,
                    ownerClientId = null,
                    expiresAtMs = 0L,
                    reason = "workspace_limit",
                ),
            )

            val revocationEvent = snapshots.last().lastEvent
            assertEquals("控制权未授予：workspace_limit", revocationEvent)
            assertEquals("warning", snapshots.last().lastEventSeverity)
            assertNull(snapshots.last().leaseId)

            transport.listener.onServerMessage(
                ServerMessage.Ack(
                    seq = 4L,
                    sentAtMs = 5_011L,
                    ackSeq = 3L,
                    ackType = "motion.stop",
                    accepted = true,
                    clamped = null,
                    detail = "motion stopped",
                ),
            )
            assertEquals(revocationEvent, snapshots.last().lastEvent)
            assertEquals("warning", snapshots.last().lastEventSeverity)

            monotonicNowMs.addAndGet(150L)
            transport.listener.onServerMessage(
                ServerMessage.Ack(
                    seq = 5L,
                    sentAtMs = 5_160L,
                    ackSeq = 4L,
                    ackType = "heartbeat",
                    accepted = true,
                    clamped = null,
                    detail = null,
                ),
            )

            assertEquals(revocationEvent, snapshots.last().lastEvent)
            assertEquals("warning", snapshots.last().lastEventSeverity)
        } finally {
            scheduler.shutdownNow()
        }
    }

    @Test
    fun heartbeatAckDoesNotHideSafetyEventAndRenewalStatusStillUpdates() {
        val scheduler = Executors.newSingleThreadScheduledExecutor()
        val monotonicNowMs = AtomicLong(1_000L)
        val snapshots = CopyOnWriteArrayList<ControllerSnapshot>()
        lateinit var transport: CapturingTransport
        val controller = TeleopController(
            clientId = "client-1",
            appVersion = "test",
            listener = snapshots::add,
            monotonicClock = monotonicNowMs::get,
            scheduler = scheduler,
            transportFactory = { listener ->
                CapturingTransport(listener).also { transport = it }
            },
        )

        try {
            establishLease(controller, transport.listener)
            transport.listener.onServerMessage(
                ServerMessage.SafetyEvent(
                    seq = 3L,
                    sentAtMs = 5_010L,
                    severity = "error",
                    code = "ORIENTATION_LIMIT",
                    message = "robot feedback left the configured TCP motion envelope",
                    action = "stop",
                ),
            )

            val safetyEvent = snapshots.last().lastEvent
            assertEquals(
                "安全事件 ORIENTATION_LIMIT: robot feedback left the configured TCP motion envelope",
                safetyEvent,
            )
            assertEquals("error", snapshots.last().lastEventSeverity)

            transport.listener.onServerMessage(
                ServerMessage.Ack(
                    seq = 4L,
                    sentAtMs = 5_011L,
                    ackSeq = 3L,
                    ackType = "motion.stop",
                    accepted = true,
                    clamped = null,
                    detail = "motion stopped",
                ),
            )
            transport.listener.onServerMessage(
                ServerMessage.Ack(
                    seq = 5L,
                    sentAtMs = 5_012L,
                    ackSeq = 4L,
                    ackType = "control.release",
                    accepted = true,
                    clamped = null,
                    detail = null,
                ),
            )
            assertEquals(safetyEvent, snapshots.last().lastEvent)
            assertEquals("error", snapshots.last().lastEventSeverity)

            monotonicNowMs.addAndGet(150L)
            transport.listener.onServerMessage(
                ServerMessage.Ack(
                    seq = 6L,
                    sentAtMs = 5_160L,
                    ackSeq = 5L,
                    ackType = "heartbeat",
                    accepted = true,
                    clamped = null,
                    detail = null,
                ),
            )
            assertEquals(safetyEvent, snapshots.last().lastEvent)
            assertEquals("error", snapshots.last().lastEventSeverity)

            transport.listener.onServerMessage(
                ServerMessage.ControlStatus(
                    seq = 7L,
                    sentAtMs = 5_170L,
                    granted = true,
                    leaseId = "lease-2",
                    ownerClientId = "client-1",
                    expiresAtMs = 8_000L,
                    reason = "renewed",
                ),
            )
            assertEquals("控制权已授予，租约到期 8000", snapshots.last().lastEvent)
            assertEquals("warning", snapshots.last().lastEventSeverity)
            assertEquals("lease-2", snapshots.last().leaseId)
        } finally {
            scheduler.shutdownNow()
        }
    }

    @Test
    fun nonHeartbeatAndRejectedHeartbeatAcksStillUpdateDiagnostics() {
        val scheduler = Executors.newSingleThreadScheduledExecutor()
        val snapshots = CopyOnWriteArrayList<ControllerSnapshot>()
        lateinit var transport: CapturingTransport
        val controller = TeleopController(
            clientId = "client-1",
            appVersion = "test",
            listener = snapshots::add,
            monotonicClock = { 1_000L },
            scheduler = scheduler,
            transportFactory = { listener ->
                CapturingTransport(listener).also { transport = it }
            },
        )

        try {
            establishLease(controller, transport.listener)
            transport.listener.onServerMessage(
                ServerMessage.Ack(
                    seq = 3L,
                    sentAtMs = 5_010L,
                    ackSeq = 3L,
                    ackType = "motion.stop",
                    accepted = true,
                    clamped = null,
                    detail = "operator stop accepted",
                ),
            )
            assertEquals("ACK motion.stop: operator stop accepted", snapshots.last().lastEvent)
            assertEquals("info", snapshots.last().lastEventSeverity)

            transport.listener.onServerMessage(
                ServerMessage.Ack(
                    seq = 4L,
                    sentAtMs = 5_020L,
                    ackSeq = 4L,
                    ackType = "gripper.set",
                    accepted = true,
                    clamped = null,
                    detail = "target accepted",
                ),
            )
            assertEquals("ACK gripper.set: target accepted", snapshots.last().lastEvent)
            assertEquals("info", snapshots.last().lastEventSeverity)

            transport.listener.onServerMessage(
                ServerMessage.Ack(
                    seq = 5L,
                    sentAtMs = 5_030L,
                    ackSeq = 5L,
                    ackType = "heartbeat",
                    accepted = false,
                    clamped = null,
                    detail = "lease expired",
                ),
            )
            assertEquals("拒绝 heartbeat: lease expired", snapshots.last().lastEvent)
            assertEquals("error", snapshots.last().lastEventSeverity)
        } finally {
            scheduler.shutdownNow()
        }
    }

    private fun establishLease(controller: TeleopController, listener: TeleopTransportListener) {
        controller.connect("wss://robot.example.com/teleop", "phone")
        listener.onTransportState(TransportState.OPEN, "open")
        listener.onServerMessage(
            ServerMessage.Welcome(
                seq = 0L,
                sentAtMs = 5_000L,
                sessionId = "session-1",
                serverTimeMs = 5_000L,
                mode = "hardware",
                watchdogMs = 300,
                commandRateHz = 20,
                limitsJson = "{}",
                baseLocked = true,
            ),
        )
        listener.onServerMessage(
            ServerMessage.RobotState(
                seq = 1L,
                sentAtMs = 5_001L,
                robotState = "IDLE",
                estopReason = null,
                jointPositionRad = List(6) { 0.0 },
                jointVelocityRadS = List(6) { 0.0 },
                tcpPoseJson = "{}",
                gripperPct = 0.0,
                baseLocked = true,
                watchdogOk = true,
                recording = false,
            ),
        )
        controller.updateChecklist(
            SafetyChecklist(
                baseStationary = true,
                workspaceClear = true,
                estopAccessible = true,
                toolSecure = true,
            ),
        )
        listener.onServerMessage(
            ServerMessage.ControlStatus(
                seq = 2L,
                sentAtMs = 5_002L,
                granted = true,
                leaseId = "lease-1",
                ownerClientId = "client-1",
                expiresAtMs = 7_000L,
                reason = "granted",
            ),
        )
    }

    private class CapturingTransport(
        val listener: TeleopTransportListener,
    ) : TeleopTransport {
        private val sequence = AtomicLong(0L)

        override fun connect(config: ConnectionConfig) = Unit

        override fun send(type: String, body: JSONObject): Long = sequence.getAndIncrement()

        override fun closeGracefully(reason: String) = Unit

        override fun cancelNow() = Unit

        override fun shutdown() = Unit
    }
}
