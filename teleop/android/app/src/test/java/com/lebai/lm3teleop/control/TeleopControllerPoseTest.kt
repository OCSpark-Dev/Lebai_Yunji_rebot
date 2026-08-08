package com.lebai.lm3teleop.control

import com.lebai.lm3teleop.core.CalibratedOrientation
import com.lebai.lm3teleop.core.SafetyChecklist
import com.lebai.lm3teleop.core.UnitQuaternion
import com.lebai.lm3teleop.core.mapCalibratedPhoneRotationToTcp
import com.lebai.lm3teleop.network.ConnectionConfig
import com.lebai.lm3teleop.network.TeleopTransport
import com.lebai.lm3teleop.network.TeleopTransportListener
import com.lebai.lm3teleop.network.TransportState
import com.lebai.lm3teleop.protocol.ServerMessage
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicLong
import kotlin.math.cos
import kotlin.math.sin

class TeleopControllerPoseTest {
    @Test
    fun poseLoopPrimesThenSendsShortestMappedDeltaAtTwentyHertz() {
        val scheduler = Executors.newSingleThreadScheduledExecutor()
        val monotonicNowMs = AtomicLong(1_000L)
        lateinit var transport: CapturingTransport
        val controller = TeleopController(
            clientId = "client-1",
            appVersion = "test",
            listener = {},
            monotonicClock = monotonicNowMs::get,
            scheduler = scheduler,
            transportFactory = { listener ->
                CapturingTransport(listener, poseTarget = 2).also { transport = it }
            },
        )

        try {
            establishLease(controller, transport.listener)
            val initial = calibrated(UnitQuaternion.IDENTITY, 1_000_000_000L)
            assertTrue(controller.startPoseMotion("calibration-1", initial))
            assertTrue(transport.firstPose.await(1, TimeUnit.SECONDS))

            val phoneTopRotation = axisAngle(y = 1.0, degrees = 5.0)
            assertTrue(
                controller.updatePoseMotion(
                    "calibration-1",
                    calibrated(phoneTopRotation, 1_020_000_000L),
                ),
            )
            assertTrue(transport.poseTargetReached.await(2, TimeUnit.SECONDS))

            val poses = transport.frames.filter { it.first == "pose.sample" }.map { it.second }
            assertTrue(poses.size >= 2)
            val priming = poses[0].getJSONObject("angular_delta_rad")
            assertEquals(0.0, priming.getDouble("rx"), 0.0)
            assertEquals(0.0, priming.getDouble("ry"), 0.0)
            assertEquals(0.0, priming.getDouble("rz"), 0.0)
            assertEquals(1_000L, poses[0].getLong("sensor_timestamp_ms"))

            val delta = poses[1].getJSONObject("angular_delta_rad")
            assertEquals(Math.toRadians(5.0), delta.getDouble("rx"), 1e-9)
            assertEquals(0.0, delta.getDouble("ry"), 1e-9)
            assertEquals(0.0, delta.getDouble("rz"), 1e-9)
            assertEquals(1_020L, poses[1].getLong("sensor_timestamp_ms"))
        } finally {
            controller.stopMotion("test_complete")
            scheduler.shutdownNow()
        }
    }

    @Test
    fun stalePhoneSampleStopsAndReleasesLeaseLocally() {
        val scheduler = Executors.newSingleThreadScheduledExecutor()
        val monotonicNowMs = AtomicLong(1_000L)
        lateinit var transport: CapturingTransport
        val controller = TeleopController(
            clientId = "client-1",
            appVersion = "test",
            listener = {},
            monotonicClock = monotonicNowMs::get,
            scheduler = scheduler,
            transportFactory = { listener ->
                CapturingTransport(listener, poseTarget = 1).also { transport = it }
            },
        )

        try {
            establishLease(controller, transport.listener)
            assertTrue(
                controller.startPoseMotion(
                    "calibration-1",
                    calibrated(UnitQuaternion.IDENTITY, 1_000_000_000L),
                ),
            )
            assertTrue(transport.firstPose.await(1, TimeUnit.SECONDS))

            monotonicNowMs.set(1_151L)
            assertTrue(transport.stopSent.await(2, TimeUnit.SECONDS))
            assertTrue(transport.releaseSent.await(2, TimeUnit.SECONDS))
        } finally {
            scheduler.shutdownNow()
        }
    }

    @Test
    fun delayedRecoverablePoseErrorAfterDeadmanReleaseStillRevokesLease() {
        val scheduler = Executors.newSingleThreadScheduledExecutor()
        lateinit var transport: CapturingTransport
        val snapshots = CopyOnWriteArrayList<ControllerSnapshot>()
        val controller = TeleopController(
            clientId = "client-1",
            appVersion = "test",
            listener = snapshots::add,
            monotonicClock = { 1_000L },
            scheduler = scheduler,
            transportFactory = { listener ->
                CapturingTransport(listener, poseTarget = 1).also { transport = it }
            },
        )

        try {
            establishLease(controller, transport.listener)
            assertTrue(
                controller.startPoseMotion(
                    "calibration-1",
                    calibrated(UnitQuaternion.IDENTITY, 1_000_000_000L),
                ),
            )
            assertTrue(transport.firstPose.await(1, TimeUnit.SECONDS))
            val poseSequence = transport.sequencedFrames.first { it.second == "pose.sample" }.first

            controller.stopMotion("orientation_deadman_released")
            controller.onServerMessage(
                ServerMessage.Error(
                    seq = 3L,
                    sentAtMs = 5_003L,
                    ackSeq = poseSequence,
                    code = "POSE_REJECTED",
                    message = "delayed rejection",
                    recoverable = true,
                ),
            )

            assertTrue(transport.releaseSent.await(1, TimeUnit.SECONDS))
            assertEquals(null, snapshots.last().leaseId)
        } finally {
            scheduler.shutdownNow()
        }
    }

    @Test
    fun unacknowledgedPoseBacklogFailsClosedInsteadOfDroppingOldSequences() {
        val scheduler = Executors.newSingleThreadScheduledExecutor()
        lateinit var controller: TeleopController
        lateinit var transport: CapturingTransport
        val poseTimestampNs = AtomicLong(1_000_000_000L)
        controller = TeleopController(
            clientId = "client-1",
            appVersion = "test",
            listener = {},
            monotonicClock = { 1_000L },
            scheduler = scheduler,
            transportFactory = { listener ->
                CapturingTransport(
                    listener = listener,
                    poseTarget = 33,
                    onPoseSent = { poseCount ->
                        if (poseCount < 33) {
                            val nextTimestamp = poseTimestampNs.addAndGet(50_000_000L)
                            controller.updatePoseMotion(
                                "calibration-1",
                                calibrated(
                                    axisAngle(z = 1.0, degrees = poseCount * 0.1),
                                    nextTimestamp,
                                ),
                            )
                        }
                    },
                ).also { transport = it }
            },
        )

        try {
            establishLease(controller, transport.listener)
            assertTrue(
                controller.startPoseMotion(
                    "calibration-1",
                    calibrated(UnitQuaternion.IDENTITY, poseTimestampNs.get()),
                ),
            )

            assertTrue(transport.poseTargetReached.await(3, TimeUnit.SECONDS))
            assertTrue(transport.releaseSent.await(1, TimeUnit.SECONDS))
            assertTrue(
                transport.frames.any {
                    it.first == "motion.stop" && it.second.getString("reason") == "pose_ack_backlog"
                },
            )
        } finally {
            scheduler.shutdownNow()
        }
    }

    private fun establishLease(controller: TeleopController, listener: TeleopTransportListener) {
        controller.connect("wss://robot.example.com/teleop", "phone", "secret")
        listener.onTransportState(TransportState.OPEN, "open")
        listener.onServerMessage(
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
        listener.onServerMessage(
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
        listener.onServerMessage(
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
    }

    private fun calibrated(orientation: UnitQuaternion, timestampNs: Long): CalibratedOrientation =
        CalibratedOrientation(
            relativeOrientation = orientation,
            relativeRotationRad = mapCalibratedPhoneRotationToTcp(orientation.toShortestRotationVector()),
            sensorTimestampNs = timestampNs,
            confidence = 1.0,
        )

    private fun axisAngle(
        x: Double = 0.0,
        y: Double = 0.0,
        z: Double = 0.0,
        degrees: Double,
    ): UnitQuaternion {
        val halfAngle = Math.toRadians(degrees) / 2.0
        val scale = sin(halfAngle)
        return UnitQuaternion(cos(halfAngle), x * scale, y * scale, z * scale)
    }

    private class CapturingTransport(
        val listener: TeleopTransportListener,
        private val poseTarget: Int,
        private val onPoseSent: ((Int) -> Unit)? = null,
    ) : TeleopTransport {
        val frames = CopyOnWriteArrayList<Pair<String, JSONObject>>()
        val sequencedFrames = CopyOnWriteArrayList<Triple<Long, String, JSONObject>>()
        val firstPose = CountDownLatch(1)
        val poseTargetReached = CountDownLatch(1)
        val stopSent = CountDownLatch(1)
        val releaseSent = CountDownLatch(1)
        private val sequence = AtomicLong(0L)

        override fun connect(config: ConnectionConfig) = Unit

        override fun send(type: String, body: JSONObject): Long {
            val sentSequence = sequence.getAndIncrement()
            val copiedBody = JSONObject(body.toString())
            frames += type to copiedBody
            sequencedFrames += Triple(sentSequence, type, copiedBody)
            if (type == "pose.sample") {
                firstPose.countDown()
                val poseCount = frames.count { it.first == "pose.sample" }
                onPoseSent?.invoke(poseCount)
                if (poseCount >= poseTarget) poseTargetReached.countDown()
            }
            if (type == "motion.stop") stopSent.countDown()
            if (type == "control.release") releaseSent.countDown()
            return sentSequence
        }

        override fun closeGracefully(reason: String) = Unit
        override fun cancelNow() = Unit
        override fun shutdown() = Unit
    }
}
