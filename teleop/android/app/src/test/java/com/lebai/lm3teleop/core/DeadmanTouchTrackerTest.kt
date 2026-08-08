package com.lebai.lm3teleop.core

import org.junit.Assert.assertEquals
import org.junit.Test

class DeadmanTouchTrackerTest {
    @Test
    fun leavingViewBoundsStopsAndClearsActivePointer() {
        val tracker = DeadmanTouchTracker()
        assertEquals(DeadmanTouchDecision.START, tracker.onDown(7))
        assertEquals(DeadmanTouchDecision.STOP, tracker.onMove(activePointerPresent = true, insideBounds = false))
        assertEquals(DeadmanTouchTracker.NO_POINTER, tracker.activePointerId)
    }

    @Test
    fun liftingActivePointerStops() {
        val tracker = DeadmanTouchTracker()
        tracker.onDown(7)
        assertEquals(DeadmanTouchDecision.STOP, tracker.onPointerUp(7))
    }

    @Test
    fun liftingDifferentPointerDoesNotStop() {
        val tracker = DeadmanTouchTracker()
        tracker.onDown(7)
        assertEquals(DeadmanTouchDecision.NONE, tracker.onPointerUp(8))
        assertEquals(7, tracker.activePointerId)
    }

    @Test
    fun losingActivePointerStops() {
        val tracker = DeadmanTouchTracker()
        tracker.onDown(7)
        assertEquals(DeadmanTouchDecision.STOP, tracker.onMove(activePointerPresent = false, insideBounds = false))
    }
}
