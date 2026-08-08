package com.lebai.lm3teleop.protocol

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test

class ProtocolCodecTest {
    @Test
    fun outboundEnvelopeUsesStrictlyIncreasingSequence() {
        val codec = ProtocolCodec { 1234L }
        val first = codec.encode(
            "session.hello",
            ProtocolBodies.sessionHello("client", "phone", "1.0", "secret"),
        )
        val second = codec.encode("motion.stop", ProtocolBodies.motionStop(null, "test"))

        assertEquals(0L, first.seq)
        assertEquals(1L, second.seq)
        val json = JSONObject(first.json)
        assertEquals(TELEOP_PROTOCOL, json.getString("protocol"))
        assertEquals(1234L, json.getLong("sent_at_ms"))
        assertEquals("android", json.getJSONObject("body").getString("platform"))
        assertEquals(3, json.getJSONObject("body").getJSONArray("capabilities").length())
    }

    @Test
    fun motionStopOmitsUnknownLease() {
        val body = ProtocolBodies.motionStop(null, "app_background")
        assertFalse(body.has("lease_id"))
        assertEquals("app_background", body.getString("reason"))
    }

    @Test
    fun recordingStartUsesCanonicalCameraNames() {
        val body = ProtocolBodies.recordingStart(
            "lease",
            "task",
            null,
            listOf(CAMERA_WRIST, CAMERA_TOP),
        )
        val cameras = body.getJSONArray("cameras")
        assertEquals("camera_wrist", cameras.getString(0))
        assertEquals("camera_top", cameras.getString(1))
    }

    @Test
    fun parsesWelcomeAndRejectsDuplicateServerSequence() {
        val codec = ProtocolCodec()
        val welcome = welcomeEnvelope()
        val parsed = codec.parse(welcome) as ServerMessage.Welcome
        assertEquals("s-1", parsed.sessionId)
        assertTrue(parsed.baseLocked)

        try {
            codec.parse(welcome)
            fail("duplicate sequence must fail")
        } catch (_: ProtocolException) {
            // Expected.
        }
    }

    @Test
    fun firstServerFrameMustBeCanonicalWelcomeWithSequenceZero() {
        assertProtocolFailure {
            ProtocolCodec().parse(
                envelope(
                    seq = 0,
                    type = "ack",
                    body = JSONObject()
                        .put("ack_seq", 0)
                        .put("ack_type", "session.hello")
                        .put("accepted", true),
                ),
            )
        }
        assertProtocolFailure { ProtocolCodec().parse(welcomeEnvelope(seq = 1)) }
    }

    @Test
    fun repeatedWelcomeFailsEvenWithIncreasingSequence() {
        val codec = ProtocolCodec()
        codec.parse(welcomeEnvelope())

        assertProtocolFailure { codec.parse(welcomeEnvelope(seq = 1)) }
    }

    @Test
    fun unknownServerMessageFailsClosed() {
        val codec = ProtocolCodec()
        codec.parse(welcomeEnvelope())

        assertProtocolFailure { codec.parse(envelope(1, "future.message", JSONObject())) }
    }

    @Test
    fun robotStateRequiresSixJointValues() {
        val body = validRobotStateBody()
            .put("joint_position_rad", JSONArray(listOf(0, 0, 0)))

        assertRobotStateFailure(body)
    }

    @Test
    fun sequenceAndTimestampRejectStringsAndFractions() {
        assertProtocolFailure {
            ProtocolCodec().parse(envelopeWithRawNumbers(seq = "0", sentAtMs = 1_000L))
        }
        assertProtocolFailure {
            ProtocolCodec().parse(envelopeWithRawNumbers(seq = 0.5, sentAtMs = 1_000L))
        }
        assertProtocolFailure {
            ProtocolCodec().parse(envelopeWithRawNumbers(seq = 0, sentAtMs = "1000"))
        }
    }

    @Test
    fun welcomeIntegerFieldsAndLimitsAreStrictlyTyped() {
        val body = JSONObject()
            .put("session_id", "s-1")
            .put("server_time_ms", 100L)
            .put("mode", "teleop")
            .put("watchdog_ms", "300")
            .put("command_rate_hz", 20)
            .put("limits", JSONObject())
            .put("base_locked", true)
        assertProtocolFailure { ProtocolCodec().parse(envelope(0, "session.welcome", body)) }

        body.put("watchdog_ms", 300).put("limits", "{}")
        assertProtocolFailure { ProtocolCodec().parse(envelope(0, "session.welcome", body)) }
    }

    @Test
    fun robotStateRejectsStringJointAndGripperValues() {
        val stringJoint = validRobotStateBody().put(
            "joint_position_rad",
            JSONArray(listOf("0", 0, 0, 0, 0, 0)),
        )
        assertRobotStateFailure(stringJoint)

        val stringGripper = validRobotStateBody().put("gripper_pct", "50")
        assertRobotStateFailure(stringGripper)
    }

    @Test
    fun robotStateRejectsOutOfRangeGripperValues() {
        assertRobotStateFailure(validRobotStateBody().put("gripper_pct", -0.1))
        assertRobotStateFailure(validRobotStateBody().put("gripper_pct", 100.1))
    }

    @Test
    fun robotStatePoseRequiresSixFiniteNumericFields() {
        val missingField = validRobotStateBody().put(
            "tcp_pose",
            JSONObject().put("x", 0).put("y", 0).put("z", 0).put("rx", 0).put("ry", 0),
        )
        assertRobotStateFailure(missingField)

        val stringField = validRobotStateBody().put(
            "tcp_pose",
            validPose().put("rz", "0"),
        )
        assertRobotStateFailure(stringField)
    }

    @Test
    fun recordingStatusRequiresRealBoolean() {
        val codec = ProtocolCodec()
        codec.parse(welcomeEnvelope())
        assertProtocolFailure {
            codec.parse(
                envelope(1, "recording.status", JSONObject().put("recording", "false")),
            )
        }
    }

    private fun welcomeEnvelope(seq: Long = 0): String = envelope(
        seq = seq,
        type = "session.welcome",
        body = validWelcomeBody(),
    )

    private fun validWelcomeBody(): JSONObject = JSONObject()
        .put("session_id", "s-1")
        .put("server_time_ms", 100L)
        .put("mode", "teleop")
        .put("watchdog_ms", 300)
        .put("command_rate_hz", 20)
        .put("limits", JSONObject().put("linear_mps", 0.02))
        .put("base_locked", true)

    private fun envelope(seq: Long, type: String, body: JSONObject): String = JSONObject()
        .put("protocol", TELEOP_PROTOCOL)
        .put("type", type)
        .put("seq", seq)
        .put("sent_at_ms", 1_000L + seq)
        .put("body", body)
        .toString()

    private fun envelopeWithRawNumbers(seq: Any, sentAtMs: Any): String = JSONObject()
        .put("protocol", TELEOP_PROTOCOL)
        .put("type", "session.welcome")
        .put("seq", seq)
        .put("sent_at_ms", sentAtMs)
        .put("body", validWelcomeBody())
        .toString()

    private fun validRobotStateBody(): JSONObject = JSONObject()
        .put("robot_state", "IDLE")
        .put("estop_reason", JSONObject.NULL)
        .put("joint_position_rad", JSONArray(listOf(0, 0, 0, 0, 0, 0)))
        .put("joint_velocity_rad_s", JSONArray(listOf(0, 0, 0, 0, 0, 0)))
        .put("tcp_pose", validPose())
        .put("gripper_pct", 50)
        .put("base_locked", true)
        .put("watchdog_ok", true)
        .put("recording", false)

    private fun validPose(): JSONObject = JSONObject()
        .put("x", 0)
        .put("y", 0)
        .put("z", 0)
        .put("rx", 0)
        .put("ry", 0)
        .put("rz", 0)

    private fun assertProtocolFailure(block: () -> Unit) {
        try {
            block()
            fail("invalid protocol frame must fail")
        } catch (_: ProtocolException) {
            // Expected.
        }
    }

    private fun assertRobotStateFailure(body: JSONObject) {
        val codec = ProtocolCodec()
        codec.parse(welcomeEnvelope())
        assertProtocolFailure { codec.parse(envelope(1, "robot.state", body)) }
    }
}
