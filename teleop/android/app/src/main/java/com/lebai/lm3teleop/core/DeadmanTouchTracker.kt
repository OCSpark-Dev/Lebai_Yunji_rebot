package com.lebai.lm3teleop.core

enum class DeadmanTouchDecision {
    NONE,
    START,
    STOP,
}

class DeadmanTouchTracker {
    var activePointerId: Int = NO_POINTER
        private set

    fun onDown(pointerId: Int): DeadmanTouchDecision {
        activePointerId = pointerId
        return DeadmanTouchDecision.START
    }

    fun onMove(activePointerPresent: Boolean, insideBounds: Boolean): DeadmanTouchDecision {
        if (activePointerId == NO_POINTER) return DeadmanTouchDecision.NONE
        if (!activePointerPresent || !insideBounds) {
            activePointerId = NO_POINTER
            return DeadmanTouchDecision.STOP
        }
        return DeadmanTouchDecision.NONE
    }

    fun onPointerUp(pointerId: Int): DeadmanTouchDecision {
        if (pointerId != activePointerId) return DeadmanTouchDecision.NONE
        activePointerId = NO_POINTER
        return DeadmanTouchDecision.STOP
    }

    fun onTerminal(): DeadmanTouchDecision {
        if (activePointerId == NO_POINTER) return DeadmanTouchDecision.NONE
        activePointerId = NO_POINTER
        return DeadmanTouchDecision.STOP
    }

    fun reset() {
        activePointerId = NO_POINTER
    }

    companion object {
        const val NO_POINTER = -1
    }
}
