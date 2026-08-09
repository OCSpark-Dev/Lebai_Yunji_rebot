package com.lebai.lm3teleop.control

import com.lebai.lm3teleop.core.AxisDirection
import com.lebai.lm3teleop.core.SafetyChecklist
import com.lebai.lm3teleop.core.SpeedGear
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
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicLong
import kotlin.concurrent.thread

class TeleopControllerConcurrencyTest {
    private val checkedSafety = SafetyChecklist(
        baseStationary = true,
        workspaceClear = true,
        estopAccessible = true,
        toolSecure = true,
    )

    @Test
    fun stopIsOrderedAfterInflightVelocityAndNoVelocityCanFollowIt() {
        val scheduler = Executors.newSingleThreadScheduledExecutor()
        val monotonicNowMs = AtomicLong(1_000L)
        lateinit var transport: BlockingTransport
        val controller = TeleopController(
            clientId = "client-1",
            appVersion = "test",
            listener = {},
            clock = { Long.MAX_VALUE },
            monotonicClock = { monotonicNowMs.get() },
            scheduler = scheduler,
            transportFactory = { listener ->
                BlockingTransport(listener).also { transport = it }
            },
        )

        try {
            controller.connect("wss://robot.example.com/teleop", "phone")
            transport.listener.onTransportState(TransportState.OPEN, "open")
            transport.listener.onServerMessage(
                ServerMessage.Welcome(
                    seq = 0L,
                    sentAtMs = 5_000L,
                    sessionId = "session-1",
                    serverTimeMs = 5_000L,
                    mode = "teleop",
                    watchdogMs = 300,
                    commandRateHz = 20,
                    limitsJson = "{}",
                    baseLocked = true,
                ),
            )
            transport.listener.onServerMessage(
                ServerMessage.RobotState(
                    seq = 1L,
                    sentAtMs = 5_001L,
                    robotState = "IDLE",
                    estopReason = null,
                    jointPositionRad = List(6) { 0.0 },
                    jointVelocityRadS = List(6) { 0.0 },
                    tcpPoseJson = "{}",
                    gripperPct = 50.0,
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
            transport.listener.onServerMessage(
                ServerMessage.ControlStatus(
                    seq = 2L,
                    sentAtMs = 5_002L,
                    granted = true,
                    leaseId = "lease-1",
                    ownerClientId = "client-1",
                    expiresAtMs = 7_000L,
                    reason = null,
                ),
            )

            assertTrue(controller.startMotion(AxisDirection.X_POS, SpeedGear.CREEP))
            assertTrue(transport.velocityEntered.await(2, TimeUnit.SECONDS))

            val stopStarted = CountDownLatch(1)
            val stopFinished = CountDownLatch(1)
            val stopThread = thread(name = "controller-stop-test") {
                stopStarted.countDown()
                controller.stopMotion("test_stop")
                stopFinished.countDown()
            }
            assertTrue(stopStarted.await(1, TimeUnit.SECONDS))
            assertFalse(stopFinished.await(100, TimeUnit.MILLISECONDS))

            transport.releaseVelocity.countDown()
            assertTrue(stopFinished.await(2, TimeUnit.SECONDS))
            stopThread.join(2_000L)
            assertFalse(stopThread.isAlive)
            Thread.sleep(120L)

            val motionFrames = transport.sentTypes.filter {
                it == "motion.cartesian_velocity" || it == "motion.stop"
            }
            assertEquals(listOf("motion.cartesian_velocity", "motion.stop"), motionFrames)
        } finally {
            transport.releaseVelocity.countDown()
            scheduler.shutdownNow()
        }
    }

    @Test
    fun repeatedProtocolFailureIsHandledOnlyOnce() {
        val scheduler = Executors.newSingleThreadScheduledExecutor()
        lateinit var transport: BlockingTransport
        val snapshots = CopyOnWriteArrayList<ControllerSnapshot>()
        val controller = TeleopController(
            clientId = "client-1",
            appVersion = "test",
            listener = { snapshots += it },
            scheduler = scheduler,
            transportFactory = { listener ->
                BlockingTransport(listener).also { transport = it }
            },
        )

        try {
            controller.onProtocolFailure("first failure")
            controller.onProtocolFailure("second failure")

            assertEquals(1, transport.cancelCount.get())
            assertEquals(1, snapshots.size)
            assertEquals(TransportState.FAILED, snapshots.single().transportState)
            assertTrue(snapshots.single().transportDetail.contains("first failure"))
        } finally {
            scheduler.shutdownNow()
        }
    }

    @Test
    fun backgroundClearsSafetyChecklistBeforePublishing() {
        val scheduler = Executors.newSingleThreadScheduledExecutor()
        val snapshots = CopyOnWriteArrayList<ControllerSnapshot>()
        val controller = TeleopController(
            clientId = "client-1",
            appVersion = "test",
            listener = { snapshots += it },
            scheduler = scheduler,
            transportFactory = { listener -> BlockingTransport(listener) },
        )

        try {
            controller.updateChecklist(checkedSafety)
            assertTrue(snapshots.last().checklist.allChecked)

            controller.onAppBackground()

            assertEquals(SafetyChecklist(), snapshots.last().checklist)
            assertFalse(snapshots.last().canRequestControl)
        } finally {
            scheduler.shutdownNow()
        }
    }

    @Test
    fun disconnectClearsSafetyChecklistForTheNextSession() {
        val scheduler = Executors.newSingleThreadScheduledExecutor()
        val snapshots = CopyOnWriteArrayList<ControllerSnapshot>()
        lateinit var transport: BlockingTransport
        val controller = TeleopController(
            clientId = "client-1",
            appVersion = "test",
            listener = { snapshots += it },
            scheduler = scheduler,
            transportFactory = { listener ->
                BlockingTransport(listener).also { transport = it }
            },
        )

        try {
            controller.updateChecklist(checkedSafety)
            assertTrue(snapshots.last().checklist.allChecked)

            transport.listener.onTransportState(TransportState.DISCONNECTED, "disconnected")

            assertEquals(SafetyChecklist(), snapshots.last().checklist)
            assertEquals(TransportState.DISCONNECTED, snapshots.last().transportState)
        } finally {
            scheduler.shutdownNow()
        }
    }

    private class BlockingTransport(
        val listener: TeleopTransportListener,
    ) : TeleopTransport {
        val sentTypes = CopyOnWriteArrayList<String>()
        val velocityEntered = CountDownLatch(1)
        val releaseVelocity = CountDownLatch(1)
        val cancelCount = AtomicInteger(0)
        private val sequence = AtomicLong(0L)

        override fun connect(config: ConnectionConfig) = Unit

        override fun send(type: String, body: JSONObject): Long {
            sentTypes += type
            if (type == "motion.cartesian_velocity") {
                velocityEntered.countDown()
                releaseVelocity.await(2, TimeUnit.SECONDS)
            }
            return sequence.getAndIncrement()
        }

        override fun closeGracefully(reason: String) = Unit

        override fun cancelNow() {
            cancelCount.incrementAndGet()
        }

        override fun shutdown() = Unit
    }
}
