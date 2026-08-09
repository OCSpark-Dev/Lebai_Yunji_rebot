package com.lebai.lm3teleop.core

import com.lebai.lm3teleop.protocol.Vector3
import kotlin.math.abs
import kotlin.math.acos
import kotlin.math.atan2
import kotlin.math.sqrt

data class UnitQuaternion(
    val w: Double,
    val x: Double,
    val y: Double,
    val z: Double,
) {
    fun normalizedOrNull(): UnitQuaternion? {
        if (!listOf(w, x, y, z).all(Double::isFinite)) return null
        val normSquared = w * w + x * x + y * y + z * z
        if (!normSquared.isFinite() || normSquared !in MIN_NORM_SQUARED..MAX_NORM_SQUARED) return null
        val inverseNorm = 1.0 / sqrt(normSquared)
        return UnitQuaternion(
            w = w * inverseNorm,
            x = x * inverseNorm,
            y = y * inverseNorm,
            z = z * inverseNorm,
        ).canonicalized()
    }

    fun inverseUnit(): UnitQuaternion = UnitQuaternion(w, -x, -y, -z)

    operator fun times(other: UnitQuaternion): UnitQuaternion = UnitQuaternion(
        w = w * other.w - x * other.x - y * other.y - z * other.z,
        x = w * other.x + x * other.w + y * other.z - z * other.y,
        y = w * other.y - x * other.z + y * other.w + z * other.x,
        z = w * other.z + x * other.y - y * other.x + z * other.w,
    )

    fun angularDistanceRad(other: UnitQuaternion): Double {
        val dot = abs(w * other.w + x * other.x + y * other.y + z * other.z)
        return 2.0 * acos(dot.coerceIn(-1.0, 1.0))
    }

    fun toShortestRotationVector(): Vector3 {
        val canonical = canonicalized()
        val vectorNorm = sqrt(
            canonical.x * canonical.x +
                canonical.y * canonical.y +
                canonical.z * canonical.z,
        )
        if (vectorNorm < EPSILON) return Vector3()
        val angle = 2.0 * atan2(vectorNorm, canonical.w.coerceIn(0.0, 1.0))
        val scale = angle / vectorNorm
        return Vector3(
            x = canonical.x * scale,
            y = canonical.y * scale,
            z = canonical.z * scale,
        )
    }

    private fun canonicalized(): UnitQuaternion {
        return if (w < 0.0) UnitQuaternion(-w, -x, -y, -z) else this
    }

    companion object {
        val IDENTITY = UnitQuaternion(1.0, 0.0, 0.0, 0.0)

        private const val EPSILON = 1e-12
        private const val MIN_NORM_SQUARED = 0.25
        private const val MAX_NORM_SQUARED = 2.25
    }
}

data class OrientationSensorSample(
    val quaternion: UnitQuaternion,
    val timestampNs: Long,
    val confidence: Double,
)

enum class OrientationSensorSource {
    GAME_ROTATION_VECTOR,
    ROTATION_VECTOR,
    NONE,
}

fun selectOrientationSensorSource(
    hasGyroscope: Boolean,
    hasGameRotationVector: Boolean,
    hasRotationVector: Boolean,
): OrientationSensorSource = when {
    !hasGyroscope -> OrientationSensorSource.NONE
    hasGameRotationVector -> OrientationSensorSource.GAME_ROTATION_VECTOR
    hasRotationVector -> OrientationSensorSource.ROTATION_VECTOR
    else -> OrientationSensorSource.NONE
}

data class CalibratedOrientation(
    val relativeOrientation: UnitQuaternion,
    val relativeRotationRad: Vector3,
    val sensorTimestampNs: Long,
    val confidence: Double,
) {
    val sensorTimestampMs: Long
        get() = sensorTimestampNs / 1_000_000L
}

enum class OrientationFault(
    val reason: String,
) {
    NON_FINITE("姿态含非有限数值"),
    INVALID_QUATERNION("姿态四元数无效"),
    INVALID_CONFIDENCE("姿态置信度无效"),
    LOW_CONFIDENCE("姿态置信度低于 0.8"),
    INVALID_TIMESTAMP("传感器时间戳无效"),
    NON_MONOTONIC_TIMESTAMP("传感器时间戳未严格递增"),
    SAMPLE_GAP("姿态采样间隔过大"),
    SENSOR_JUMP("姿态传感器发生跳变"),
    RELATIVE_RANGE("手机偏离归零姿态过大"),
    SAMPLE_STALE("姿态样本已过期"),
    NOT_CALIBRATED("尚未归零"),
}

sealed interface OrientationResult {
    data class AwaitingCalibration(val sensorTimestampMs: Long) : OrientationResult
    data class Tracking(val value: CalibratedOrientation) : OrientationResult
    data class Fault(val code: OrientationFault) : OrientationResult
}

sealed interface CalibrationResult {
    data class Success(val value: CalibratedOrientation) : CalibrationResult
    data class Failure(val code: OrientationFault) : CalibrationResult
}

/**
 * Validates fused Android rotation-vector samples and expresses phone attitude relative to an
 * explicit operator-selected zero. This class never integrates raw gyroscope angular velocity.
 */
class OrientationSafetyMapper(
    private val maxSampleGapNs: Long = 250_000_000L,
    private val maxSampleAgeNs: Long = 150_000_000L,
    private val maxSensorJumpRad: Double = Math.toRadians(35.0),
    private val maxRelativeAngleRad: Double = Math.toRadians(30.0),
    private val minimumConfidence: Double = 0.8,
) {
    private var latestOrientation: UnitQuaternion? = null
    private var latestTimestampNs: Long? = null
    private var latestConfidence: Double = 0.0
    private var zeroOrientation: UnitQuaternion? = null

    val calibrated: Boolean
        get() = zeroOrientation != null

    fun ingest(sample: OrientationSensorSample): OrientationResult {
        if (sample.timestampNs <= 0L) return failAndReset(OrientationFault.INVALID_TIMESTAMP)
        if (!sample.confidence.isFinite() || sample.confidence !in 0.0..1.0) {
            return failAndReset(OrientationFault.INVALID_CONFIDENCE)
        }
        if (sample.confidence < minimumConfidence) {
            return failAndReset(OrientationFault.LOW_CONFIDENCE)
        }
        val normalized = sample.quaternion.normalizedOrNull()
            ?: return failAndReset(
                if (listOf(
                        sample.quaternion.w,
                        sample.quaternion.x,
                        sample.quaternion.y,
                        sample.quaternion.z,
                    ).all(Double::isFinite)
                ) {
                    OrientationFault.INVALID_QUATERNION
                } else {
                    OrientationFault.NON_FINITE
                },
            )

        val previousTimestamp = latestTimestampNs
        val previousOrientation = latestOrientation
        if (previousTimestamp != null) {
            if (sample.timestampNs <= previousTimestamp) {
                return failAndReset(OrientationFault.NON_MONOTONIC_TIMESTAMP)
            }
            if (sample.timestampNs - previousTimestamp > maxSampleGapNs) {
                return failAndReset(OrientationFault.SAMPLE_GAP)
            }
        }
        if (
            previousOrientation != null &&
            previousOrientation.angularDistanceRad(normalized) > maxSensorJumpRad
        ) {
            return failAndReset(OrientationFault.SENSOR_JUMP)
        }

        latestOrientation = normalized
        latestTimestampNs = sample.timestampNs
        latestConfidence = sample.confidence
        return currentTrackingOrAwaiting()
    }

    fun calibrate(nowNs: Long): CalibrationResult {
        val current = latestOrientation ?: return CalibrationResult.Failure(OrientationFault.SAMPLE_STALE)
        val timestamp = latestTimestampNs ?: return CalibrationResult.Failure(OrientationFault.SAMPLE_STALE)
        if (!isFresh(timestamp, nowNs)) {
            reset()
            return CalibrationResult.Failure(OrientationFault.SAMPLE_STALE)
        }
        zeroOrientation = current
        return CalibrationResult.Success(
            CalibratedOrientation(
                relativeOrientation = UnitQuaternion.IDENTITY,
                relativeRotationRad = Vector3(),
                sensorTimestampNs = timestamp,
                confidence = latestConfidence,
            ),
        )
    }

    fun current(nowNs: Long): OrientationResult {
        val timestamp = latestTimestampNs ?: return OrientationResult.Fault(OrientationFault.SAMPLE_STALE)
        if (!isFresh(timestamp, nowNs)) {
            reset()
            return OrientationResult.Fault(OrientationFault.SAMPLE_STALE)
        }
        return currentTrackingOrAwaiting()
    }

    fun canCalibrate(nowNs: Long): Boolean {
        val timestamp = latestTimestampNs ?: return false
        return latestOrientation != null && isFresh(timestamp, nowNs)
    }

    fun clearCalibration() {
        zeroOrientation = null
    }

    fun reset() {
        latestOrientation = null
        latestTimestampNs = null
        latestConfidence = 0.0
        zeroOrientation = null
    }

    private fun currentTrackingOrAwaiting(): OrientationResult {
        val current = latestOrientation ?: return OrientationResult.Fault(OrientationFault.SAMPLE_STALE)
        val timestamp = latestTimestampNs ?: return OrientationResult.Fault(OrientationFault.SAMPLE_STALE)
        val zero = zeroOrientation ?: return OrientationResult.AwaitingCalibration(
            timestamp / NANOS_PER_MILLISECOND,
        )
        val relative = (zero.inverseUnit() * current).normalizedOrNull()
            ?: return failAndReset(OrientationFault.INVALID_QUATERNION)
        val phoneRotation = relative.toShortestRotationVector()
        val angle = sqrt(
            phoneRotation.x * phoneRotation.x +
                phoneRotation.y * phoneRotation.y +
                phoneRotation.z * phoneRotation.z,
        )
        if (!angle.isFinite()) return failAndReset(OrientationFault.NON_FINITE)
        if (angle > maxRelativeAngleRad) {
            clearCalibration()
            return OrientationResult.Fault(OrientationFault.RELATIVE_RANGE)
        }
        return OrientationResult.Tracking(
            CalibratedOrientation(
                relativeOrientation = relative,
                relativeRotationRad = mapCalibratedPhoneRotationToTcp(phoneRotation),
                sensorTimestampNs = timestamp,
                confidence = latestConfidence,
            ),
        )
    }

    private fun isFresh(timestampNs: Long, nowNs: Long): Boolean {
        if (nowNs < timestampNs) return false
        return nowNs - timestampNs <= maxSampleAgeNs
    }

    private fun failAndReset(code: OrientationFault): OrientationResult.Fault {
        reset()
        return OrientationResult.Fault(code)
    }

    private companion object {
        const val NANOS_PER_MILLISECOND = 1_000_000L
    }
}

sealed interface PoseDeltaResult {
    data class Ready(
        val angularDeltaRad: Vector3,
        val priming: Boolean,
    ) : PoseDeltaResult

    data class Fault(val code: OrientationFault) : PoseDeltaResult
}

/** Computes the shortest left-difference in the calibrated phone frame between acknowledged samples. */
class PoseDeltaTracker(
    private val maxSentDeltaRad: Double = Math.toRadians(12.0),
) {
    private var lastSentOrientation: UnitQuaternion? = null

    fun preview(currentOrientation: UnitQuaternion): PoseDeltaResult {
        val current = currentOrientation.normalizedOrNull()
            ?: return PoseDeltaResult.Fault(OrientationFault.INVALID_QUATERNION)
        val previous = lastSentOrientation
            ?: return PoseDeltaResult.Ready(Vector3(), priming = true)
        val delta = (current * previous.inverseUnit()).normalizedOrNull()
            ?: return PoseDeltaResult.Fault(OrientationFault.INVALID_QUATERNION)
        val phoneVector = delta.toShortestRotationVector()
        val magnitude = sqrt(
            phoneVector.x * phoneVector.x +
                phoneVector.y * phoneVector.y +
                phoneVector.z * phoneVector.z,
        )
        if (!magnitude.isFinite()) return PoseDeltaResult.Fault(OrientationFault.NON_FINITE)
        if (magnitude > maxSentDeltaRad) return PoseDeltaResult.Fault(OrientationFault.SENSOR_JUMP)
        return PoseDeltaResult.Ready(
            mapCalibratedPhoneRotationToTcp(phoneVector),
            priming = false,
        )
    }

    fun commit(sentOrientation: UnitQuaternion) {
        lastSentOrientation = sentOrientation.normalizedOrNull()
    }

    fun reset() {
        lastSentOrientation = null
    }
}

/**
 * Neutral mounting convention: screen faces up and the phone top points toward robot base +X.
 * Android device +X (screen right) then points toward base -Y, +Y (phone top) toward base +X,
 * and +Z (out of screen) toward base +Z. Therefore phone [x,y,z] maps to TCP [rx,ry,rz]
 * as [y,-x,z]. Site commissioning must still verify robot/controller axis conventions.
 */
fun mapCalibratedPhoneRotationToTcp(phoneRotationRad: Vector3): Vector3 = Vector3(
    x = phoneRotationRad.y.withoutNegativeZero(),
    y = (-phoneRotationRad.x).withoutNegativeZero(),
    z = phoneRotationRad.z.withoutNegativeZero(),
)

private fun Double.withoutNegativeZero(): Double = if (abs(this) < 1e-15) 0.0 else this
