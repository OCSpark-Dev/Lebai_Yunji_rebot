package com.lebai.lm3teleop.core

import com.lebai.lm3teleop.protocol.Vector3
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlin.math.cos
import kotlin.math.sin

class OrientationTeleopTest {
    @Test
    fun sensorSelectionRequiresGyroscopeAndPrefersGameRotationVector() {
        assertEquals(
            OrientationSensorSource.NONE,
            selectOrientationSensorSource(
                hasGyroscope = false,
                hasGameRotationVector = true,
                hasRotationVector = true,
            ),
        )
        assertEquals(
            OrientationSensorSource.GAME_ROTATION_VECTOR,
            selectOrientationSensorSource(
                hasGyroscope = true,
                hasGameRotationVector = true,
                hasRotationVector = true,
            ),
        )
        assertEquals(
            OrientationSensorSource.ROTATION_VECTOR,
            selectOrientationSensorSource(
                hasGyroscope = true,
                hasGameRotationVector = false,
                hasRotationVector = true,
            ),
        )
    }

    @Test
    fun explicitZeroMapsPhoneTopAxisToPositiveTcpRx() {
        val mapper = OrientationSafetyMapper()
        val firstTimestamp = 1_000_000_000L
        assertTrue(mapper.ingest(sample(UnitQuaternion.IDENTITY, firstTimestamp)) is OrientationResult.AwaitingCalibration)
        assertTrue(mapper.calibrate(firstTimestamp) is CalibrationResult.Success)

        val result = mapper.ingest(
            sample(axisAngle(0.0, 1.0, 0.0, degrees = 10.0), firstTimestamp + 20_000_000L),
        ) as OrientationResult.Tracking

        assertEquals(Math.toRadians(10.0), result.value.relativeRotationRad.x, 1e-9)
        assertEquals(0.0, result.value.relativeRotationRad.y, 1e-9)
        assertEquals(0.0, result.value.relativeRotationRad.z, 1e-9)
    }

    @Test
    fun neutralMountMapsPhoneRightAxisToNegativeTcpRy() {
        val mapped = mapCalibratedPhoneRotationToTcp(
            Vector3(x = Math.toRadians(7.0)),
        )

        assertEquals(0.0, mapped.x, 0.0)
        assertEquals(Math.toRadians(-7.0), mapped.y, 1e-12)
        assertEquals(0.0, mapped.z, 0.0)
    }

    @Test
    fun relativePoseUsesInverseZeroTimesCurrent() {
        val mapper = OrientationSafetyMapper()
        val timestamp = 1_000_000_000L
        val zero = axisAngle(1.0, 0.0, 0.0, degrees = 20.0)
        val desiredRelative = axisAngle(0.0, 1.0, 0.0, degrees = 5.0)
        mapper.ingest(sample(zero, timestamp))
        mapper.calibrate(timestamp)

        val result = mapper.ingest(
            sample(zero * desiredRelative, timestamp + 20_000_000L),
        ) as OrientationResult.Tracking

        assertEquals(Math.toRadians(5.0), result.value.relativeRotationRad.x, 1e-9)
        assertEquals(0.0, result.value.relativeRotationRad.y, 1e-9)
        assertEquals(0.0, result.value.relativeRotationRad.z, 1e-9)
    }

    @Test
    fun sentDeltaUsesLeftDifferenceAndFirstFrameOnlyPrimes() {
        val tracker = PoseDeltaTracker()
        val previous = axisAngle(1.0, 0.0, 0.0, degrees = 25.0)
        val desiredPhoneDelta = axisAngle(0.0, 0.0, 1.0, degrees = 5.0)
        val current = desiredPhoneDelta * previous

        val first = tracker.preview(previous) as PoseDeltaResult.Ready
        assertTrue(first.priming)
        assertEquals(Vector3(), first.angularDeltaRad)
        tracker.commit(previous)

        val second = tracker.preview(current) as PoseDeltaResult.Ready
        assertFalse(second.priming)
        assertEquals(0.0, second.angularDeltaRad.x, 1e-9)
        assertEquals(0.0, second.angularDeltaRad.y, 1e-9)
        assertEquals(Math.toRadians(5.0), second.angularDeltaRad.z, 1e-9)
    }

    @Test
    fun quaternionSignFlipRepresentsSameOrientationWithoutJump() {
        val mapper = OrientationSafetyMapper()
        val orientation = axisAngle(0.0, 0.0, 1.0, degrees = 8.0)
        val timestamp = 1_000_000_000L
        mapper.ingest(sample(orientation, timestamp))
        mapper.calibrate(timestamp)

        val sameOrientation = UnitQuaternion(
            -orientation.w,
            -orientation.x,
            -orientation.y,
            -orientation.z,
        )
        val result = mapper.ingest(sample(sameOrientation, timestamp + 20_000_000L))

        assertTrue(result is OrientationResult.Tracking)
        assertEquals(Vector3(), (result as OrientationResult.Tracking).value.relativeRotationRad)
    }

    @Test
    fun lowConfidenceFailsClosedAndClearsCalibration() {
        val mapper = OrientationSafetyMapper()
        val timestamp = 1_000_000_000L
        mapper.ingest(sample(UnitQuaternion.IDENTITY, timestamp))
        mapper.calibrate(timestamp)

        val result = mapper.ingest(
            sample(UnitQuaternion.IDENTITY, timestamp + 20_000_000L, confidence = 0.79),
        )

        assertEquals(OrientationFault.LOW_CONFIDENCE, (result as OrientationResult.Fault).code)
        assertFalse(mapper.calibrated)
        assertFalse(mapper.canCalibrate(timestamp + 20_000_000L))
    }

    @Test
    fun timestampRegressionAndLongGapFailClosed() {
        val timestamp = 1_000_000_000L
        val regressionMapper = OrientationSafetyMapper()
        regressionMapper.ingest(sample(UnitQuaternion.IDENTITY, timestamp))
        val regression = regressionMapper.ingest(sample(UnitQuaternion.IDENTITY, timestamp))
        assertEquals(
            OrientationFault.NON_MONOTONIC_TIMESTAMP,
            (regression as OrientationResult.Fault).code,
        )

        val gapMapper = OrientationSafetyMapper()
        gapMapper.ingest(sample(UnitQuaternion.IDENTITY, timestamp))
        val gap = gapMapper.ingest(sample(UnitQuaternion.IDENTITY, timestamp + 250_000_001L))
        assertEquals(OrientationFault.SAMPLE_GAP, (gap as OrientationResult.Fault).code)
    }

    @Test
    fun sensorJumpAndStaleSampleFailClosed() {
        val timestamp = 1_000_000_000L
        val jumpMapper = OrientationSafetyMapper()
        jumpMapper.ingest(sample(UnitQuaternion.IDENTITY, timestamp))
        jumpMapper.calibrate(timestamp)
        val jump = jumpMapper.ingest(
            sample(axisAngle(1.0, 0.0, 0.0, degrees = 36.0), timestamp + 20_000_000L),
        )
        assertEquals(OrientationFault.SENSOR_JUMP, (jump as OrientationResult.Fault).code)

        val staleMapper = OrientationSafetyMapper()
        staleMapper.ingest(sample(UnitQuaternion.IDENTITY, timestamp))
        staleMapper.calibrate(timestamp)
        val stale = staleMapper.current(timestamp + 150_000_001L)
        assertEquals(OrientationFault.SAMPLE_STALE, (stale as OrientationResult.Fault).code)
        assertFalse(staleMapper.calibrated)
    }

    @Test
    fun excessiveDeltaBetweenSentSamplesFailsClosed() {
        val tracker = PoseDeltaTracker()
        tracker.preview(UnitQuaternion.IDENTITY)
        tracker.commit(UnitQuaternion.IDENTITY)

        val result = tracker.preview(axisAngle(0.0, 0.0, 1.0, degrees = 12.1))

        assertEquals(OrientationFault.SENSOR_JUMP, (result as PoseDeltaResult.Fault).code)
    }

    private fun sample(
        quaternion: UnitQuaternion,
        timestampNs: Long,
        confidence: Double = 1.0,
    ): OrientationSensorSample = OrientationSensorSample(
        quaternion = quaternion,
        timestampNs = timestampNs,
        confidence = confidence,
    )

    private fun axisAngle(
        x: Double,
        y: Double,
        z: Double,
        degrees: Double,
    ): UnitQuaternion {
        val halfAngle = Math.toRadians(degrees) / 2.0
        val scale = sin(halfAngle)
        return UnitQuaternion(
            w = cos(halfAngle),
            x = x * scale,
            y = y * scale,
            z = z * scale,
        )
    }
}
