package com.lebai.lm3teleop.protocol

import org.json.JSONArray
import org.json.JSONObject
import java.math.BigInteger
import java.util.concurrent.atomic.AtomicLong

const val TELEOP_PROTOCOL = "lm3-teleop.v1"
const val CAMERA_TOP = "camera_top"
const val CAMERA_WRIST = "camera_wrist"

data class OutboundFrame(
    val seq: Long,
    val type: String,
    val json: String,
)

data class SafetyAcknowledgement(
    val baseStationary: Boolean,
    val workspaceClear: Boolean,
    val estopAccessible: Boolean,
    val toolSecure: Boolean,
)

data class Vector3(
    val x: Double = 0.0,
    val y: Double = 0.0,
    val z: Double = 0.0,
) {
    fun toJson(): JSONObject = JSONObject()
        .put("x", x)
        .put("y", y)
        .put("z", z)
}

sealed class ServerMessage(
    open val seq: Long,
    open val sentAtMs: Long,
) {
    data class Welcome(
        override val seq: Long,
        override val sentAtMs: Long,
        val sessionId: String,
        val serverTimeMs: Long,
        val mode: String,
        val watchdogMs: Int,
        val commandRateHz: Int,
        val limitsJson: String,
        val baseLocked: Boolean,
    ) : ServerMessage(seq, sentAtMs)

    data class ControlStatus(
        override val seq: Long,
        override val sentAtMs: Long,
        val granted: Boolean,
        val leaseId: String?,
        val ownerClientId: String?,
        val expiresAtMs: Long,
        val reason: String?,
    ) : ServerMessage(seq, sentAtMs)

    data class RobotState(
        override val seq: Long,
        override val sentAtMs: Long,
        val robotState: String,
        val estopReason: String?,
        val jointPositionRad: List<Double>,
        val jointVelocityRadS: List<Double>,
        val tcpPoseJson: String,
        val gripperPct: Double,
        val baseLocked: Boolean,
        val watchdogOk: Boolean,
        val recording: Boolean,
    ) : ServerMessage(seq, sentAtMs)

    data class RecordingStatus(
        override val seq: Long,
        override val sentAtMs: Long,
        val active: Boolean,
        val bodyJson: String,
    ) : ServerMessage(seq, sentAtMs)

    data class Ack(
        override val seq: Long,
        override val sentAtMs: Long,
        val ackSeq: Long,
        val ackType: String,
        val accepted: Boolean,
        val clamped: Boolean?,
        val detail: String?,
    ) : ServerMessage(seq, sentAtMs)

    data class Error(
        override val seq: Long,
        override val sentAtMs: Long,
        val ackSeq: Long?,
        val code: String,
        val message: String,
        val recoverable: Boolean,
    ) : ServerMessage(seq, sentAtMs)

    data class SafetyEvent(
        override val seq: Long,
        override val sentAtMs: Long,
        val severity: String,
        val code: String,
        val message: String,
        val action: String,
    ) : ServerMessage(seq, sentAtMs)

}

class ProtocolException(message: String) : IllegalArgumentException(message)

class ProtocolCodec(
    private val clock: () -> Long = System::currentTimeMillis,
) {
    private val outboundSeq = AtomicLong(0)
    private var lastInboundSeq = -1L
    private var welcomeReceived = false

    @Synchronized
    fun reset() {
        outboundSeq.set(0)
        lastInboundSeq = -1L
        welcomeReceived = false
    }

    fun encode(type: String, body: JSONObject): OutboundFrame {
        require(type.isNotBlank()) { "type must not be blank" }
        val seq = outboundSeq.getAndIncrement()
        val envelope = JSONObject()
            .put("protocol", TELEOP_PROTOCOL)
            .put("type", type)
            .put("seq", seq)
            .put("sent_at_ms", clock())
            .put("body", body)
        return OutboundFrame(seq, type, envelope.toString())
    }

    @Synchronized
    fun parse(text: String): ServerMessage {
        val envelope = try {
            JSONObject(text)
        } catch (error: Exception) {
            throw ProtocolException("invalid JSON: ${error.message}")
        }

        if (envelope.optString("protocol") != TELEOP_PROTOCOL) {
            throw ProtocolException("unexpected protocol")
        }
        val type = envelope.optString("type")
        if (type.isBlank()) throw ProtocolException("missing type")
        val seq = envelope.requiredLong("seq")
        if (seq < 0L || seq <= lastInboundSeq) {
            throw ProtocolException("server seq must increase: last=$lastInboundSeq received=$seq")
        }
        val sentAtMs = envelope.requiredLong("sent_at_ms")
        if (sentAtMs < 0L) throw ProtocolException("invalid sent_at_ms")
        val body = envelope.optJSONObject("body") ?: throw ProtocolException("body must be an object")
        if (!welcomeReceived && (type != "session.welcome" || seq != 0L)) {
            throw ProtocolException("first server frame must be session.welcome with seq=0")
        }
        if (welcomeReceived && type == "session.welcome") {
            throw ProtocolException("duplicate session.welcome")
        }

        val message = when (type) {
            "session.welcome" -> ServerMessage.Welcome(
                seq = seq,
                sentAtMs = sentAtMs,
                sessionId = body.requiredString("session_id"),
                serverTimeMs = body.requiredLong("server_time_ms"),
                mode = body.requiredString("mode"),
                watchdogMs = body.requiredInt("watchdog_ms"),
                commandRateHz = body.requiredInt("command_rate_hz"),
                limitsJson = body.requiredObject("limits").toString(),
                baseLocked = body.requiredBoolean("base_locked"),
            )

            "control.status" -> ServerMessage.ControlStatus(
                seq = seq,
                sentAtMs = sentAtMs,
                granted = body.requiredBoolean("granted"),
                leaseId = body.nullableString("lease_id"),
                ownerClientId = body.nullableString("owner_client_id"),
                expiresAtMs = body.optionalLong("expires_at_ms") ?: 0L,
                reason = body.nullableString("reason"),
            )

            "robot.state" -> ServerMessage.RobotState(
                seq = seq,
                sentAtMs = sentAtMs,
                robotState = body.requiredString("robot_state"),
                estopReason = body.nullableString("estop_reason"),
                jointPositionRad = body.requiredSixVector("joint_position_rad"),
                jointVelocityRadS = body.requiredSixVector("joint_velocity_rad_s"),
                tcpPoseJson = body.requiredPose("tcp_pose").toString(),
                gripperPct = body.requiredFiniteDouble("gripper_pct").also { value ->
                    if (value !in 0.0..100.0) throw ProtocolException("invalid gripper_pct")
                },
                baseLocked = body.requiredBoolean("base_locked"),
                watchdogOk = body.requiredBoolean("watchdog_ok"),
                recording = body.requiredBoolean("recording"),
            )

            "recording.status" -> ServerMessage.RecordingStatus(
                seq = seq,
                sentAtMs = sentAtMs,
                active = when {
                    body.has("recording") -> body.requiredBoolean("recording")
                    body.has("active") -> body.requiredBoolean("active")
                    else -> throw ProtocolException("missing recording")
                },
                bodyJson = body.toString(),
            )

            "ack" -> ServerMessage.Ack(
                seq = seq,
                sentAtMs = sentAtMs,
                ackSeq = body.requiredLong("ack_seq"),
                ackType = body.requiredString("ack_type"),
                accepted = body.requiredBoolean("accepted"),
                clamped = body.optionalBoolean("clamped"),
                detail = body.nullableString("detail"),
            )

            "error" -> ServerMessage.Error(
                seq = seq,
                sentAtMs = sentAtMs,
                ackSeq = body.optionalLong("ack_seq"),
                code = body.requiredString("code"),
                message = body.requiredString("message"),
                recoverable = body.requiredBoolean("recoverable"),
            )

            "safety.event" -> ServerMessage.SafetyEvent(
                seq = seq,
                sentAtMs = sentAtMs,
                severity = body.requiredString("severity"),
                code = body.requiredString("code"),
                message = body.requiredString("message"),
                action = body.requiredString("action"),
            )

            else -> throw ProtocolException("unknown server message type: $type")
        }
        lastInboundSeq = seq
        welcomeReceived = message is ServerMessage.Welcome || welcomeReceived
        return message
    }
}

object ProtocolBodies {
    fun sessionHello(
        clientId: String,
        clientName: String,
        appVersion: String,
        authToken: String,
    ): JSONObject = JSONObject()
        .put("client_id", clientId)
        .put("client_name", clientName)
        .put("platform", "android")
        .put("app_version", appVersion)
        .put("auth_token", authToken)
        .put(
            "capabilities",
            JSONArray(listOf("cartesian_velocity", "gripper", "recording")),
        )

    fun controlAcquire(safety: SafetyAcknowledgement): JSONObject = JSONObject()
        .put("requested_lease_ms", 2_000)
        .put("operator_hold_ms", 1_500)
        .put(
            "safety_ack",
            JSONObject()
                .put("base_stationary", safety.baseStationary)
                .put("workspace_clear", safety.workspaceClear)
                .put("estop_accessible", safety.estopAccessible)
                .put("tool_secure", safety.toolSecure),
        )

    fun controlRelease(leaseId: String): JSONObject = JSONObject().put("lease_id", leaseId)

    fun heartbeat(leaseId: String?): JSONObject = JSONObject().apply {
        if (leaseId != null) put("lease_id", leaseId)
        put("deadman", false)
    }

    fun cartesianVelocity(
        leaseId: String,
        linearMps: Vector3,
        angularRps: Vector3,
    ): JSONObject = JSONObject()
        .put("lease_id", leaseId)
        .put("deadman", true)
        .put("frame", "base")
        .put("linear_mps", linearMps.toJson())
        .put(
            "angular_rps",
            JSONObject()
                .put("rx", angularRps.x)
                .put("ry", angularRps.y)
                .put("rz", angularRps.z),
        )
        .put("duration_ms", 100)

    fun motionStop(leaseId: String?, reason: String): JSONObject = JSONObject().apply {
        if (leaseId != null) put("lease_id", leaseId)
        put("reason", reason)
    }

    fun gripperSet(leaseId: String, positionPct: Int): JSONObject = JSONObject()
        .put("lease_id", leaseId)
        .put("deadman", true)
        .put("position_pct", positionPct.coerceIn(0, 100))

    fun recordingStart(
        leaseId: String,
        task: String,
        episodeId: String?,
        cameras: List<String>,
    ): JSONObject = JSONObject()
        .put("lease_id", leaseId)
        .put("task", task)
        .apply {
            if (!episodeId.isNullOrBlank()) put("episode_id", episodeId)
        }
        .put("cameras", JSONArray(cameras))

    fun recordingStop(leaseId: String, reason: String): JSONObject = JSONObject()
        .put("lease_id", leaseId)
        .put("reason", reason)
}

private fun JSONObject.requiredString(name: String): String {
    val value = nullableString(name)
    if (value.isNullOrBlank()) throw ProtocolException("missing $name")
    return value
}

private fun JSONObject.requiredLong(name: String): Long {
    if (!has(name) || isNull(name)) throw ProtocolException("missing $name")
    return when (val raw = get(name)) {
        is Byte -> raw.toLong()
        is Short -> raw.toLong()
        is Int -> raw.toLong()
        is Long -> raw
        is BigInteger -> {
            if (raw < LONG_MIN_BIG_INTEGER || raw > LONG_MAX_BIG_INTEGER) {
                throw ProtocolException("invalid $name")
            }
            raw.toLong()
        }
        else -> throw ProtocolException("invalid $name")
    }
}

private fun JSONObject.requiredInt(name: String): Int {
    val value = requiredLong(name)
    if (value !in Int.MIN_VALUE.toLong()..Int.MAX_VALUE.toLong()) throw ProtocolException("invalid $name")
    return value.toInt()
}

private fun JSONObject.requiredBoolean(name: String): Boolean {
    if (!has(name) || isNull(name)) throw ProtocolException("missing $name")
    val raw = get(name)
    if (raw !is Boolean) throw ProtocolException("invalid $name")
    return raw
}

private fun JSONObject.nullableString(name: String): String? {
    if (!has(name) || isNull(name)) return null
    val raw = get(name)
    if (raw !is String) throw ProtocolException("invalid $name")
    return raw.takeUnless { it.equals("null", ignoreCase = true) }
}

private fun JSONObject.requiredSixVector(name: String): List<Double> {
    val array = optJSONArray(name) ?: throw ProtocolException("missing $name")
    if (array.length() != 6) throw ProtocolException("$name must contain 6 values")
    return List(6) { index ->
        val raw = array.get(index)
        if (raw !is Number) throw ProtocolException("$name contains non-number values")
        raw.toDouble().also {
            if (!it.isFinite()) throw ProtocolException("$name contains non-finite values")
        }
    }
}

private fun JSONObject.requiredFiniteDouble(name: String): Double {
    if (!has(name) || isNull(name)) throw ProtocolException("missing $name")
    val raw = get(name)
    if (raw !is Number) throw ProtocolException("invalid $name")
    return raw.toDouble().also {
        if (!it.isFinite()) throw ProtocolException("invalid $name")
    }
}

private fun JSONObject.requiredObject(name: String): JSONObject =
    optJSONObject(name) ?: throw ProtocolException("missing or invalid $name")

private fun JSONObject.requiredPose(name: String): JSONObject {
    val pose = requiredObject(name)
    listOf("x", "y", "z", "rx", "ry", "rz").forEach { pose.requiredFiniteDouble(it) }
    return pose
}

private fun JSONObject.optionalLong(name: String): Long? {
    if (!has(name) || isNull(name)) return null
    return requiredLong(name)
}

private fun JSONObject.optionalBoolean(name: String): Boolean? {
    if (!has(name) || isNull(name)) return null
    return requiredBoolean(name)
}

private val LONG_MIN_BIG_INTEGER: BigInteger = BigInteger.valueOf(Long.MIN_VALUE)
private val LONG_MAX_BIG_INTEGER: BigInteger = BigInteger.valueOf(Long.MAX_VALUE)
