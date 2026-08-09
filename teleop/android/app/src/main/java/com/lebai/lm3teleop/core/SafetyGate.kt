package com.lebai.lm3teleop.core

import com.lebai.lm3teleop.protocol.SafetyAcknowledgement

data class SafetyChecklist(
    val baseStationary: Boolean = false,
    val workspaceClear: Boolean = false,
    val estopAccessible: Boolean = false,
    val toolSecure: Boolean = false,
) {
    val allChecked: Boolean
        get() = baseStationary && workspaceClear && estopAccessible && toolSecure

    fun toProtocol(): SafetyAcknowledgement = SafetyAcknowledgement(
        baseStationary = baseStationary,
        workspaceClear = workspaceClear,
        estopAccessible = estopAccessible,
        toolSecure = toolSecure,
    )
}

data class GateContext(
    val socketOpen: Boolean,
    val sessionReady: Boolean,
    val appForeground: Boolean,
    val leaseId: String?,
    val leaseDeadlineMonotonicMs: Long,
    val nowMonotonicMs: Long,
    val robotStateFresh: Boolean,
    val baseLocked: Boolean?,
    val watchdogOk: Boolean?,
    val estopReason: String?,
    val robotState: String?,
    val checklist: SafetyChecklist,
)

data class GateDecision(
    val allowed: Boolean,
    val reason: String,
)

object SafetyGate {
    const val ROBOT_STATE_MAX_AGE_MS = 1_000L
    private val ACQUIRE_STATES = setOf("idle")
    private val ACTION_STATES = setOf("idle", "moving", "running")

    fun welcomeIsCompatible(commandRateHz: Int, watchdogMs: Int): Boolean =
        commandRateHz == 20 && watchdogMs in 100..300

    fun canAcquire(context: GateContext): GateDecision {
        commonSafetyFailure(context)?.let { return it }
        if (!robotStateAllowsAcquire(context.robotState)) {
            return GateDecision(false, "仅 robot_state=IDLE 时允许申请控制权")
        }
        return GateDecision(true, "可以申请控制权")
    }

    fun canAct(context: GateContext): GateDecision {
        commonSafetyFailure(context)?.let { return it }
        if (!robotStateAllowsAction(context.robotState)) {
            return GateDecision(false, "robot_state 不允许运动")
        }
        if (context.leaseId.isNullOrBlank()) return GateDecision(false, "没有有效控制租约")
        if (!leaseIsCurrent(context.leaseId, context.leaseDeadlineMonotonicMs, context.nowMonotonicMs)) {
            return GateDecision(false, "控制租约已到期")
        }
        return GateDecision(true, "允许发送受限动作")
    }

    fun leaseIsCurrent(leaseId: String?, deadlineMonotonicMs: Long, nowMonotonicMs: Long): Boolean =
        !leaseId.isNullOrBlank() && deadlineMonotonicMs > 0L && nowMonotonicMs < deadlineMonotonicMs

    fun leaseDeadlineMonotonic(
        serverTimeAtWelcomeMs: Long,
        welcomeReceivedAtMonotonicMs: Long,
        leaseExpiresAtServerMs: Long,
        nowMonotonicMs: Long,
    ): Long {
        if (
            serverTimeAtWelcomeMs < 0L ||
            welcomeReceivedAtMonotonicMs < 0L ||
            leaseExpiresAtServerMs <= 0L ||
            nowMonotonicMs < welcomeReceivedAtMonotonicMs
        ) {
            return 0L
        }
        val elapsedMs = nowMonotonicMs - welcomeReceivedAtMonotonicMs
        val estimatedServerNowMs = if (Long.MAX_VALUE - serverTimeAtWelcomeMs < elapsedMs) {
            Long.MAX_VALUE
        } else {
            serverTimeAtWelcomeMs + elapsedMs
        }
        if (leaseExpiresAtServerMs <= estimatedServerNowMs) return 0L
        val remainingMs = leaseExpiresAtServerMs - estimatedServerNowMs
        return if (Long.MAX_VALUE - nowMonotonicMs < remainingMs) {
            Long.MAX_VALUE
        } else {
            nowMonotonicMs + remainingMs
        }
    }

    fun robotStateIsFresh(receivedAtMs: Long?, nowMs: Long): Boolean {
        val received = receivedAtMs ?: return false
        val ageMs = nowMs - received
        return ageMs in 0L..ROBOT_STATE_MAX_AGE_MS
    }

    fun robotStateAllowsAcquire(robotState: String?): Boolean =
        robotState?.trim()?.lowercase() in ACQUIRE_STATES

    fun robotStateAllowsAction(robotState: String?): Boolean =
        robotState?.trim()?.lowercase() in ACTION_STATES

    fun shouldFailClosedForStaleState(
        robotStateFresh: Boolean,
        leaseId: String?,
        pendingAcquire: Boolean,
        deadmanActive: Boolean,
        recordingActive: Boolean,
        recordingPending: Boolean,
    ): Boolean = !robotStateFresh && (
        !leaseId.isNullOrBlank() ||
            pendingAcquire ||
            deadmanActive ||
            recordingActive ||
            recordingPending
        )

    fun hasEstop(estopReason: String?, robotState: String?): Boolean {
        val reason = estopReason?.trim()?.lowercase()
        val explicitReason = !reason.isNullOrEmpty() && reason !in setOf("none", "ok", "0", "false")
        val state = robotState?.lowercase().orEmpty()
        return explicitReason || state.contains("estop") || state.contains("fault") || state.contains("error")
    }

    private fun commonSafetyFailure(context: GateContext): GateDecision? {
        if (!context.socketOpen) return GateDecision(false, "WebSocket 未连接")
        if (!context.sessionReady) return GateDecision(false, "尚未收到兼容的 session.welcome")
        if (!context.appForeground) return GateDecision(false, "App 不在前台")
        if (!context.checklist.allChecked) return GateDecision(false, "安全检查未全部确认")
        if (!context.robotStateFresh) return GateDecision(false, "robot.state 超过 1000ms 未更新")
        if (context.baseLocked != true) return GateDecision(false, "UP 底盘未确认锁定")
        if (context.watchdogOk != true) return GateDecision(false, "服务端看门狗未确认正常")
        if (hasEstop(context.estopReason, context.robotState)) {
            return GateDecision(false, "机器人处于急停或故障状态")
        }
        return null
    }
}
