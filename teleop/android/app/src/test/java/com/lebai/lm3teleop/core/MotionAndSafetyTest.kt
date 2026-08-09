package com.lebai.lm3teleop.core

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class MotionAndSafetyTest {
    private val checked = SafetyChecklist(
        baseStationary = true,
        workspaceClear = true,
        estopAccessible = true,
        toolSecure = true,
    )

    @Test
    fun welcomeRequiresTwentyHertzAndAtMostThreeHundredMillisecondWatchdog() {
        assertTrue(SafetyGate.welcomeIsCompatible(20, 300))
        assertTrue(SafetyGate.welcomeIsCompatible(20, 100))
        assertFalse(SafetyGate.welcomeIsCompatible(20, 301))
        assertFalse(SafetyGate.welcomeIsCompatible(10, 300))
    }

    @Test
    fun eachAxisCommandOnlySetsOneComponent() {
        val command = AxisDirection.RY_NEG.command(SpeedGear.CREEP)
        assertEquals(0.0, command.linear.x, 0.0)
        assertEquals(0.0, command.linear.y, 0.0)
        assertEquals(0.0, command.linear.z, 0.0)
        assertEquals(0.0, command.angular.x, 0.0)
        assertEquals(-0.02, command.angular.y, 0.0)
        assertEquals(0.0, command.angular.z, 0.0)
    }

    @Test
    fun acquireOnlyAllowsIdleRobotState() {
        assertTrue(SafetyGate.canAcquire(safeContext(robotState = "IDLE")).allowed)
        assertFalse(SafetyGate.canAcquire(safeContext(robotState = "MOVING")).allowed)
        assertFalse(SafetyGate.canAcquire(safeContext(robotState = "RUNNING")).allowed)
        assertFalse(SafetyGate.canAcquire(safeContext(robotState = "PROTECTIVE_STOP")).allowed)
        assertFalse(SafetyGate.canAcquire(safeContext(robotState = "unexpected")).allowed)
        assertFalse(SafetyGate.canAcquire(safeContext(robotState = null)).allowed)
    }

    @Test
    fun actionAllowsKnownOperationalStatesAndRejectsUnsafeOrUnknownStates() {
        listOf("IDLE", "MOVING", "RUNNING").forEach { state ->
            assertTrue(SafetyGate.canAct(safeContext(robotState = state, leaseId = "lease-1")).allowed)
        }
        assertFalse(
            SafetyGate.canAct(
                safeContext(robotState = "PROTECTIVE_STOP", leaseId = "lease-1"),
            ).allowed,
        )
        assertFalse(SafetyGate.canAct(safeContext(robotState = "unexpected", leaseId = "lease-1")).allowed)
        assertFalse(
            SafetyGate.canAct(
                safeContext(robotState = "IDLE", leaseId = "lease-1", estopReason = "hardware estop"),
            ).allowed,
        )
    }

    @Test
    fun actionRequiresLeaseAndAllSafetySignals() {
        val base = safeContext()
        assertTrue(SafetyGate.canAcquire(base).allowed)
        assertFalse(SafetyGate.canAct(base).allowed)
        assertTrue(SafetyGate.canAct(base.copy(leaseId = "lease-1")).allowed)
        assertFalse(SafetyGate.canAct(base.copy(leaseId = "lease-1", appForeground = false)).allowed)
        assertFalse(SafetyGate.canAcquire(base.copy(checklist = checked.copy(workspaceClear = false))).allowed)
    }

    @Test
    fun robotStateFreshnessFailsClosedAfterOneSecond() {
        assertFalse(SafetyGate.robotStateIsFresh(null, nowMs = 5_000L))
        assertTrue(SafetyGate.robotStateIsFresh(receivedAtMs = 4_000L, nowMs = 5_000L))
        assertFalse(SafetyGate.robotStateIsFresh(receivedAtMs = 3_999L, nowMs = 5_000L))
        assertFalse(SafetyGate.robotStateIsFresh(receivedAtMs = 5_001L, nowMs = 5_000L))
        assertFalse(SafetyGate.canAcquire(safeContext(robotStateFresh = false)).allowed)
        assertFalse(SafetyGate.canAct(safeContext(leaseId = "lease-1", robotStateFresh = false)).allowed)
    }

    @Test
    fun leaseDeadlineUsesServerBaselineAndMonotonicElapsedTime() {
        val deadline = SafetyGate.leaseDeadlineMonotonic(
            serverTimeAtWelcomeMs = 1_000_000L,
            welcomeReceivedAtMonotonicMs = 10_000L,
            leaseExpiresAtServerMs = 1_002_000L,
            nowMonotonicMs = 10_500L,
        )

        assertEquals(12_000L, deadline)
        assertTrue(SafetyGate.leaseIsCurrent("lease-1", deadline, 11_999L))
        assertFalse(SafetyGate.leaseIsCurrent("lease-1", deadline, 12_000L))
        assertEquals(
            0L,
            SafetyGate.leaseDeadlineMonotonic(
                serverTimeAtWelcomeMs = 1_000_000L,
                welcomeReceivedAtMonotonicMs = 10_000L,
                leaseExpiresAtServerMs = 1_000_400L,
                nowMonotonicMs = 10_500L,
            ),
        )
    }

    @Test
    fun staleStateFailsClosedWheneverSafetyRelevantWorkExists() {
        assertFalse(
            SafetyGate.shouldFailClosedForStaleState(
                robotStateFresh = false,
                leaseId = null,
                pendingAcquire = false,
                deadmanActive = false,
                recordingActive = false,
                recordingPending = false,
            ),
        )
        assertTrue(staleFailure(leaseId = "lease-1"))
        assertTrue(staleFailure(pendingAcquire = true))
        assertTrue(staleFailure(deadmanActive = true))
        assertTrue(staleFailure(recordingActive = true))
        assertTrue(staleFailure(recordingPending = true))
        assertFalse(staleFailure(robotStateFresh = true, leaseId = "lease-1"))
    }

    private fun safeContext(
        robotState: String? = "IDLE",
        leaseId: String? = null,
        leaseDeadlineMonotonicMs: Long = 20_000L,
        nowMonotonicMs: Long = 10_000L,
        robotStateFresh: Boolean = true,
        appForeground: Boolean = true,
        estopReason: String? = null,
    ): GateContext = GateContext(
        socketOpen = true,
        sessionReady = true,
        appForeground = appForeground,
        leaseId = leaseId,
        leaseDeadlineMonotonicMs = leaseDeadlineMonotonicMs,
        nowMonotonicMs = nowMonotonicMs,
        robotStateFresh = robotStateFresh,
        baseLocked = true,
        watchdogOk = true,
        estopReason = estopReason,
        robotState = robotState,
        checklist = checked,
    )

    private fun staleFailure(
        robotStateFresh: Boolean = false,
        leaseId: String? = null,
        pendingAcquire: Boolean = false,
        deadmanActive: Boolean = false,
        recordingActive: Boolean = false,
        recordingPending: Boolean = false,
    ): Boolean = SafetyGate.shouldFailClosedForStaleState(
        robotStateFresh = robotStateFresh,
        leaseId = leaseId,
        pendingAcquire = pendingAcquire,
        deadmanActive = deadmanActive,
        recordingActive = recordingActive,
        recordingPending = recordingPending,
    )
}
