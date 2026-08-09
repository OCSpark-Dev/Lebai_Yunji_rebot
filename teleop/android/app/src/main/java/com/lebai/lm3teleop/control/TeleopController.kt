package com.lebai.lm3teleop.control

import com.lebai.lm3teleop.core.AxisDirection
import com.lebai.lm3teleop.core.CalibratedOrientation
import com.lebai.lm3teleop.core.CartesianCommand
import com.lebai.lm3teleop.core.GateContext
import com.lebai.lm3teleop.core.PoseDeltaResult
import com.lebai.lm3teleop.core.PoseDeltaTracker
import com.lebai.lm3teleop.core.SafetyChecklist
import com.lebai.lm3teleop.core.SafetyGate
import com.lebai.lm3teleop.core.SpeedGear
import com.lebai.lm3teleop.network.ConnectionConfig
import com.lebai.lm3teleop.network.TeleopTransport
import com.lebai.lm3teleop.network.TeleopTransportListener
import com.lebai.lm3teleop.network.TeleopWebSocket
import com.lebai.lm3teleop.network.TransportState
import com.lebai.lm3teleop.protocol.ProtocolBodies
import com.lebai.lm3teleop.protocol.ServerMessage
import java.util.concurrent.Executors
import java.util.concurrent.ScheduledExecutorService
import java.util.concurrent.ScheduledFuture
import java.util.concurrent.TimeUnit

enum class MotionInput {
    TOUCH_AXIS,
    PHONE_ORIENTATION,
}

data class ControllerSnapshot(
    val transportState: TransportState = TransportState.DISCONNECTED,
    val transportDetail: String = "未连接",
    val welcomeReceived: Boolean = false,
    val protocolCompatible: Boolean = false,
    val sessionId: String? = null,
    val mode: String? = null,
    val watchdogMs: Int? = null,
    val commandRateHz: Int? = null,
    val limitsJson: String? = null,
    val robotState: String? = null,
    val estopReason: String? = null,
    val jointPositionRad: List<Double> = emptyList(),
    val jointVelocityRadS: List<Double> = emptyList(),
    val tcpPoseJson: String? = null,
    val gripperPct: Double? = null,
    val baseLocked: Boolean? = null,
    val watchdogOk: Boolean? = null,
    val leaseId: String? = null,
    val leaseOwnerClientId: String? = null,
    val leaseExpiresAtMs: Long = 0L,
    val leaseRemainingMs: Long = 0L,
    val pendingAcquire: Boolean = false,
    val deadmanActive: Boolean = false,
    val motionInput: MotionInput? = null,
    val recordingActive: Boolean = false,
    val recordingPending: Boolean = false,
    val recordingDetail: String? = null,
    val checklist: SafetyChecklist = SafetyChecklist(),
    val canRequestControl: Boolean = false,
    val canAct: Boolean = false,
    val gateReason: String = "未连接",
    val lastEvent: String = "等待连接",
    val lastEventSeverity: String = "info",
)

private sealed interface PendingMotion {
    data class Cartesian(
        val leaseId: String,
        val command: CartesianCommand,
        val generation: Long,
    ) : PendingMotion

    data class Pose(
        val leaseId: String,
        val calibrationId: String,
        val sample: CalibratedOrientation,
        val angularDeltaRad: com.lebai.lm3teleop.protocol.Vector3,
        val generation: Long,
    ) : PendingMotion
}

private data class PendingMotionAck(
    val seq: Long,
    val type: String,
    val generation: Long,
    val sentAtMonotonicMs: Long,
    val poseCalibrationId: String? = null,
    val poseSample: CalibratedOrientation? = null,
)

class TeleopController(
    private val clientId: String,
    private val appVersion: String,
    private val listener: (ControllerSnapshot) -> Unit,
    private val clock: () -> Long = System::currentTimeMillis,
    private val monotonicClock: () -> Long = {
        TimeUnit.NANOSECONDS.toMillis(System.nanoTime())
    },
    private val scheduler: ScheduledExecutorService = Executors.newSingleThreadScheduledExecutor { runnable ->
        Thread(runnable, "lm3-teleop-safety-loop").apply { isDaemon = true }
    },
    transportFactory: (TeleopTransportListener) -> TeleopTransport = { TeleopWebSocket(it) },
) : TeleopTransportListener {
    private val lock = Any()
    private val motionSendLock = Any()
    private val socket = transportFactory(this)

    private var transportState = TransportState.DISCONNECTED
    private var transportDetail = "未连接"
    private var welcome: ServerMessage.Welcome? = null
    private var protocolCompatible = false
    private var robotState: ServerMessage.RobotState? = null
    private var lastRobotStateReceivedAtMs: Long? = null
    private var lastPublishedRobotStateFresh = false
    private var leaseId: String? = null
    private var leaseOwnerClientId: String? = null
    private var leaseExpiresAtMs = 0L
    private var leaseDeadlineMonotonicMs = 0L
    private var serverTimeAtWelcomeMs: Long? = null
    private var welcomeReceivedAtMonotonicMs: Long? = null
    private var pendingAcquire = false
    private var checklist = SafetyChecklist()
    private var appForeground = true
    private var deadmanActive = false
    private var motionInput: MotionInput? = null
    private var activeCommand: CartesianCommand? = null
    private var activePose: CalibratedOrientation? = null
    private var activePoseCalibrationId: String? = null
    private var activePoseUpdatedAtMonotonicMs = 0L
    private var lastPoseSentTimestampNs = -1L
    private val poseDeltaTracker = PoseDeltaTracker()
    private var motionGeneration = 0L
    private var pendingMotionAck: PendingMotionAck? = null
    private var motionFuture: ScheduledFuture<*>? = null
    private var heartbeatFuture: ScheduledFuture<*>? = null
    private var stateWatchdogFuture: ScheduledFuture<*>? = null
    private var recordingActive = false
    private var recordingPending = false
    private var recordingDetail: String? = null
    private var lastEvent = "等待连接"
    private var lastEventSeverity = "info"
    private var protocolFailureHandled = false

    fun connect(url: String, clientName: String) {
        synchronized(lock) {
            resetSessionLocked()
            protocolFailureHandled = false
            transportState = TransportState.CONNECTING
            transportDetail = "正在连接"
            lastEvent = "正在建立安全会话"
            lastEventSeverity = "info"
        }
        publish()
        socket.connect(
            ConnectionConfig(
                url = url,
                clientId = clientId,
                clientName = clientName,
                appVersion = appVersion,
            ),
        )
    }

    fun updateChecklist(value: SafetyChecklist) {
        val mustRelease = synchronized(lock) {
            checklist = value
            !value.allChecked && leaseId != null
        }
        if (mustRelease) {
            forceSafetyStop("safety_check_revoked", releaseLease = true, stopRecording = true)
        } else {
            publish()
        }
    }

    fun requestControl(): Boolean {
        val decision = synchronized(lock) { SafetyGate.canAcquire(gateContextLocked()) }
        if (!decision.allowed) {
            setEvent(decision.reason, "warning")
            return false
        }
        val body = synchronized(lock) {
            pendingAcquire = true
            ProtocolBodies.controlAcquire(checklist.toProtocol())
        }
        if (socket.send("control.acquire", body) == null) {
            synchronized(lock) { pendingAcquire = false }
            setEvent("control.acquire 发送失败", "error")
            return false
        }
        setEvent("已申请 2 秒控制租约，等待服务端授权", "info")
        return true
    }

    fun releaseControl(reason: String = "operator_release") {
        forceSafetyStop(reason, releaseLease = true, stopRecording = true)
    }

    fun startMotion(axis: AxisDirection, gear: SpeedGear): Boolean {
        var rejection: String? = null
        synchronized(motionSendLock) {
            synchronized(lock) {
                val decision = SafetyGate.canAct(gateContextLocked())
                if (!decision.allowed) {
                    rejection = decision.reason
                } else {
                    cancelMotionLoopLocked()
                    clearPoseMotionLocked()
                    activeCommand = axis.command(gear)
                    motionInput = MotionInput.TOUCH_AXIS
                    deadmanActive = true
                    lastEvent = "DEADMAN 按下：${axis.displayName} / ${gear.displayName}"
                    lastEventSeverity = "warning"
                    motionFuture = scheduler.scheduleWithFixedDelay(
                        ::sendMotionTick,
                        0,
                        MOTION_PERIOD_MS,
                        TimeUnit.MILLISECONDS,
                    )
                }
            }
        }
        if (rejection != null) {
            setEvent(rejection!!, "warning")
            return false
        }
        publish()
        return true
    }

    fun startPoseMotion(calibrationId: String, sample: CalibratedOrientation): Boolean {
        if (calibrationId.isBlank() || !poseSampleIsValid(sample)) {
            setEvent("手机姿态样本无效，拒绝启动", "error")
            return false
        }
        var rejection: String? = null
        synchronized(motionSendLock) {
            synchronized(lock) {
                val decision = SafetyGate.canAct(gateContextLocked())
                if (!decision.allowed) {
                    rejection = decision.reason
                } else {
                    cancelMotionLoopLocked()
                    activeCommand = null
                    poseDeltaTracker.reset()
                    activePose = sample
                    activePoseCalibrationId = calibrationId
                    activePoseUpdatedAtMonotonicMs = monotonicClock()
                    lastPoseSentTimestampNs = -1L
                    motionInput = MotionInput.PHONE_ORIENTATION
                    deadmanActive = true
                    lastEvent = "手机姿态 DEADMAN 按下；首帧仅用于增量归零"
                    lastEventSeverity = "warning"
                    motionFuture = scheduler.scheduleWithFixedDelay(
                        ::sendMotionTick,
                        0,
                        MOTION_PERIOD_MS,
                        TimeUnit.MILLISECONDS,
                    )
                }
            }
        }
        if (rejection != null) {
            setEvent(rejection!!, "warning")
            return false
        }
        publish()
        return true
    }

    fun updatePoseMotion(calibrationId: String, sample: CalibratedOrientation): Boolean {
        if (calibrationId.isBlank() || !poseSampleIsValid(sample)) {
            forceSafetyStop("orientation_invalid_sample", releaseLease = true, stopRecording = true)
            return false
        }
        var failureReason: String? = null
        val updated = synchronized(lock) {
            when {
                !deadmanActive || motionInput != MotionInput.PHONE_ORIENTATION -> false
                calibrationId != activePoseCalibrationId -> {
                    failureReason = "orientation_calibration_changed"
                    false
                }
                sample.sensorTimestampNs <= (activePose?.sensorTimestampNs ?: -1L) -> {
                    failureReason = "orientation_timestamp_not_increasing"
                    false
                }
                else -> {
                    activePose = sample
                    activePoseUpdatedAtMonotonicMs = monotonicClock()
                    true
                }
            }
        }
        if (failureReason != null) {
            forceSafetyStop(failureReason!!, releaseLease = true, stopRecording = true)
        }
        return updated
    }

    fun stopMotion(reason: String = "deadman_released") {
        val lease: String?
        val sessionReady: Boolean
        synchronized(motionSendLock) {
            synchronized(lock) {
                cancelMotionLoopLocked()
                invalidateMotionCreditLocked()
                deadmanActive = false
                activeCommand = null
                motionInput = null
                clearPoseMotionLocked()
                lease = leaseId
                sessionReady = welcome != null && transportState == TransportState.OPEN
                lastEvent = "已停止：$reason"
                lastEventSeverity = "info"
            }
            if (sessionReady) {
                socket.send("motion.stop", ProtocolBodies.motionStop(lease, reason))
            }
        }
        publish()
    }

    fun emergencyStop(reason: String = "operator_stop") {
        stopMotion(reason)
    }

    fun sendGripper(positionPct: Int): Boolean {
        val lease: String
        val decision: String?
        synchronized(lock) {
            val gate = SafetyGate.canAct(gateContextLocked())
            decision = if (!gate.allowed) gate.reason else if (deadmanActive) "机械臂运动中不能同时控制夹爪" else null
            lease = leaseId.orEmpty()
        }
        if (decision != null || lease.isBlank()) {
            setEvent(decision ?: "没有有效控制租约", "warning")
            return false
        }
        val sent = socket.send("gripper.set", ProtocolBodies.gripperSet(lease, positionPct)) != null
        setEvent(
            if (sent) "已发送夹爪目标：${positionPct.coerceIn(0, 100)}%" else "夹爪命令发送失败",
            if (sent) "warning" else "error",
        )
        return sent
    }

    fun startRecording(task: String, episodeId: String?, cameras: List<String>): Boolean {
        val lease: String
        val gate = synchronized(lock) {
            lease = leaseId.orEmpty()
            SafetyGate.canAct(gateContextLocked())
        }
        if (!gate.allowed) {
            setEvent(gate.reason, "warning")
            return false
        }
        if (task.isBlank()) {
            setEvent("录制任务文本不能为空", "warning")
            return false
        }
        if (cameras.isEmpty()) {
            setEvent("至少选择一路相机", "warning")
            return false
        }
        val sent = socket.send(
            "recording.start",
            ProtocolBodies.recordingStart(lease, task.trim(), episodeId?.trim(), cameras),
        ) != null
        synchronized(lock) { recordingPending = sent }
        setEvent(if (sent) "正在请求开始录制" else "录制开始请求发送失败", if (sent) "info" else "error")
        return sent
    }

    fun stopRecording(reason: String = "operator_stop"): Boolean {
        val lease = synchronized(lock) { leaseId }
        if (lease.isNullOrBlank()) {
            setEvent("没有控制租约，无法发送 recording.stop", "warning")
            return false
        }
        val sent = socket.send("recording.stop", ProtocolBodies.recordingStop(lease, reason)) != null
        synchronized(lock) { recordingPending = sent }
        setEvent(if (sent) "正在请求停止录制" else "录制停止请求发送失败", if (sent) "info" else "error")
        return sent
    }

    fun onAppForeground() {
        synchronized(lock) {
            appForeground = true
            lastEvent = "App 已回到前台；请重新完成安全检查并长按解锁"
            lastEventSeverity = "info"
        }
        publish()
    }

    fun onAppBackground() {
        synchronized(lock) {
            appForeground = false
            checklist = SafetyChecklist()
        }
        forceSafetyStop("app_background", releaseLease = true, stopRecording = true)
    }

    fun disconnect(reason: String = "operator_disconnect") {
        forceSafetyStop(reason, releaseLease = true, stopRecording = true)
        socket.closeGracefully(reason)
    }

    fun destroy() {
        disconnect("activity_destroyed")
        scheduler.schedule(
            {
                socket.shutdown()
                scheduler.shutdown()
            },
            300,
            TimeUnit.MILLISECONDS,
        )
    }

    override fun onTransportState(state: TransportState, detail: String) {
        synchronized(lock) {
            transportState = state
            transportDetail = detail
            lastEvent = detail
            lastEventSeverity = if (state == TransportState.FAILED) "error" else "info"
            if (state == TransportState.DISCONNECTED || state == TransportState.FAILED || state == TransportState.CLOSING) {
                resetSessionLocked()
                if (state == TransportState.FAILED) {
                    lastEvent = "$detail；无法再发停机帧，依赖服务端看门狗失败关闭"
                }
            }
        }
        publish()
    }

    override fun onServerMessage(message: ServerMessage) {
        var forceStopReason: String? = null
        var releaseLease = false
        var preserveServerEvent = false
        var preWelcomeError = false
        var retryMotionAfterAck = false
        synchronized(lock) {
            when (message) {
                is ServerMessage.Welcome -> {
                    welcome = message
                    serverTimeAtWelcomeMs = message.serverTimeMs
                    welcomeReceivedAtMonotonicMs = monotonicClock()
                    protocolCompatible = SafetyGate.welcomeIsCompatible(
                        commandRateHz = message.commandRateHz,
                        watchdogMs = message.watchdogMs,
                    )
                    transportDetail = if (protocolCompatible) "协议已就绪" else "服务端安全参数不兼容"
                    lastEvent = if (protocolCompatible) {
                        "握手成功，会话 ${message.sessionId}"
                    } else {
                        "服务端参数不兼容：rate=${message.commandRateHz}Hz watchdog=${message.watchdogMs}ms"
                    }
                    lastEventSeverity = if (protocolCompatible) "info" else "error"
                    startHeartbeatLocked(message.watchdogMs)
                    startStateWatchdogLocked()
                }

                is ServerMessage.ControlStatus -> {
                    pendingAcquire = false
                    val nowMonotonicMs = monotonicClock()
                    val deadlineMonotonicMs = leaseDeadlineForServerExpiryLocked(
                        expiresAtServerMs = message.expiresAtMs,
                        nowMonotonicMs = nowMonotonicMs,
                    )
                    val ownedByThisClient = message.granted &&
                        !message.leaseId.isNullOrBlank() &&
                        message.ownerClientId == clientId &&
                        deadlineMonotonicMs > nowMonotonicMs
                    if (ownedByThisClient) {
                        leaseId = message.leaseId
                        leaseOwnerClientId = message.ownerClientId
                        leaseExpiresAtMs = message.expiresAtMs
                        leaseDeadlineMonotonicMs = deadlineMonotonicMs
                        lastEvent = "控制权已授予，租约到期 ${message.expiresAtMs}"
                        lastEventSeverity = "warning"
                    } else {
                        leaseId = null
                        leaseOwnerClientId = message.ownerClientId
                        leaseExpiresAtMs = 0L
                        leaseDeadlineMonotonicMs = 0L
                        forceStopReason = "control_not_granted"
                        preserveServerEvent = true
                        lastEvent = "控制权未授予：${message.reason.orEmpty()}"
                        lastEventSeverity = "warning"
                    }
                }

                is ServerMessage.RobotState -> {
                    robotState = message
                    lastRobotStateReceivedAtMs = monotonicClock()
                    lastPublishedRobotStateFresh = true
                    recordingActive = message.recording
                    recordingPending = false
                    val unsafe = !message.baseLocked ||
                        !message.watchdogOk ||
                        SafetyGate.hasEstop(message.estopReason, message.robotState)
                    if (
                        unsafe && SafetyGate.shouldFailClosedForStaleState(
                            robotStateFresh = false,
                            leaseId = leaseId,
                            pendingAcquire = pendingAcquire,
                            deadmanActive = deadmanActive,
                            recordingActive = recordingActive,
                            recordingPending = recordingPending,
                        )
                    ) {
                        forceStopReason = "unsafe_robot_state"
                        releaseLease = true
                    }
                }

                is ServerMessage.RecordingStatus -> {
                    recordingActive = message.active
                    recordingPending = false
                    recordingDetail = message.bodyJson
                    lastEvent = if (message.active) "录制已开始" else "录制已停止"
                    lastEventSeverity = "info"
                }

                is ServerMessage.Ack -> {
                    val pending = pendingMotionAck
                    val matchedMotionAck = pending?.takeIf {
                        it.seq == message.ackSeq && it.type == message.ackType
                    }
                    if (matchedMotionAck != null) {
                        pendingMotionAck = null
                        if (
                            message.accepted &&
                            matchedMotionAck.generation == motionGeneration
                        ) {
                            val acknowledgedPose = matchedMotionAck.poseSample
                            if (
                                acknowledgedPose != null &&
                                motionInput == MotionInput.PHONE_ORIENTATION &&
                                activePoseCalibrationId == matchedMotionAck.poseCalibrationId
                            ) {
                                // This advances the network-acknowledged pose baseline. A clamped
                                // v1 ACK doesn't expose the exact physical rotation that executed,
                                // so callers must not interpret it as measured robot pose closure.
                                poseDeltaTracker.commit(acknowledgedPose.relativeOrientation)
                                lastPoseSentTimestampNs = acknowledgedPose.sensorTimestampNs
                            }
                            retryMotionAfterAck = deadmanActive && motionInput != null
                        }
                    }
                    recordingPending = if (message.ackType.startsWith("recording.")) false else recordingPending
                    val routineHeartbeatAck = message.ackType == "heartbeat" && message.accepted
                    val postReleaseCleanupAck = message.accepted &&
                        message.ackType in CLEANUP_ACK_TYPES &&
                        leaseId.isNullOrBlank() &&
                        lastEventSeverity.lowercase() in STICKY_EVENT_SEVERITIES
                    // A routine heartbeat ACK is transport noise. In particular, it must not
                    // erase the control-status or safety-event reason that explains why a lease
                    // was just rejected or revoked. The same applies to ACKs for automatic
                    // cleanup frames sent after the local lease has already been cleared.
                    if (!routineHeartbeatAck && !postReleaseCleanupAck) {
                        lastEvent = buildString {
                            append(if (message.accepted) "ACK" else "拒绝")
                            append(" ${message.ackType}")
                            if (message.clamped == true) append("（服务端已限幅）")
                            if (!message.detail.isNullOrBlank()) append(": ${message.detail}")
                        }
                        lastEventSeverity = when {
                            !message.accepted -> "error"
                            message.clamped == true -> "warning"
                            else -> "info"
                        }
                    }
                    if (
                        !message.accepted &&
                        (
                            matchedMotionAck?.generation == motionGeneration ||
                                message.ackType == "gripper.set"
                            )
                    ) {
                        forceStopReason = "command_rejected"
                        releaseLease = true
                        preserveServerEvent = true
                    }
                }

                is ServerMessage.Error -> {
                    val failedMotion = message.ackSeq?.let { ackSeq ->
                        pendingMotionAck?.takeIf { it.seq == ackSeq }?.also {
                            pendingMotionAck = null
                        }
                    }
                    lastEvent = "${message.code}: ${message.message}"
                    lastEventSeverity = "error"
                    if (welcome == null && message.seq == 0L) {
                        preWelcomeError = true
                        protocolFailureHandled = true
                        transportState = TransportState.FAILED
                        transportDetail = lastEvent
                        lastEvent = "$lastEvent；连接已强制关闭"
                        resetSessionLocked()
                    } else if (
                        !message.recoverable ||
                        failedMotion?.generation == motionGeneration
                    ) {
                        forceStopReason = "nonrecoverable_error_${message.code}"
                        releaseLease = true
                        preserveServerEvent = true
                    }
                }

                is ServerMessage.SafetyEvent -> {
                    lastEvent = "安全事件 ${message.code}: ${message.message}"
                    lastEventSeverity = message.severity
                    if (message.action == "stop") {
                        forceStopReason = "safety_event_${message.code}"
                        releaseLease = true
                        preserveServerEvent = true
                    }
                }

            }
        }

        if (preWelcomeError) {
            socket.cancelNow()
            publish()
        } else if (forceStopReason != null) {
            forceSafetyStop(
                forceStopReason!!,
                releaseLease,
                stopRecording = true,
                updateEvent = !preserveServerEvent,
            )
        } else {
            publish()
            if (retryMotionAfterAck) {
                scheduleMotionTickAfterAck()
            }
        }
    }

    override fun onProtocolFailure(detail: String) {
        val firstFailure = synchronized(lock) {
            if (protocolFailureHandled) return@synchronized false
            protocolFailureHandled = true
            transportState = TransportState.FAILED
            transportDetail = detail
            lastEvent = "$detail；连接已强制关闭"
            lastEventSeverity = "error"
            resetSessionLocked()
            true
        }
        if (!firstFailure) return
        socket.cancelNow()
        publish()
    }

    private fun sendMotionTick() {
        synchronized(motionSendLock) {
            var failureReason: String? = null
            val action: PendingMotion? = synchronized(lock) state@{
                if (!deadmanActive || motionInput == null) {
                    return@state null
                }
                val nowMonotonicMs = monotonicClock()
                pendingMotionAck?.let { pending ->
                    val ageMs = nowMonotonicMs - pending.sentAtMonotonicMs
                    if (ageMs < 0L || ageMs >= motionAckTimeoutMsLocked()) {
                        failureReason = "motion_ack_timeout"
                    }
                    return@state null
                }
                val context = gateContextLocked()
                val gate = SafetyGate.canAct(context)
                if (!gate.allowed) {
                    failureReason = when {
                        !context.robotStateFresh -> "robot_state_stale"
                        !SafetyGate.leaseIsCurrent(
                            context.leaseId,
                            context.leaseDeadlineMonotonicMs,
                            context.nowMonotonicMs,
                        ) -> "lease_expired"
                        else -> "motion_gate_closed"
                    }
                    null
                } else {
                    val lease = leaseId
                    if (lease.isNullOrBlank()) {
                        failureReason = "motion_gate_closed"
                        null
                    } else {
                        when (motionInput) {
                            MotionInput.TOUCH_AXIS -> {
                                val command = activeCommand
                                if (command == null) {
                                    failureReason = "motion_gate_closed"
                                    null
                                } else {
                                    PendingMotion.Cartesian(
                                        leaseId = lease,
                                        command = command,
                                        generation = motionGeneration,
                                    )
                                }
                            }

                            MotionInput.PHONE_ORIENTATION -> {
                                val sample = activePose
                                val calibrationId = activePoseCalibrationId
                                when {
                                    sample == null || calibrationId.isNullOrBlank() -> {
                                        failureReason = "orientation_sample_missing"
                                        null
                                    }

                                    nowMonotonicMs < activePoseUpdatedAtMonotonicMs ||
                                        nowMonotonicMs - activePoseUpdatedAtMonotonicMs > POSE_SAMPLE_MAX_AGE_MS -> {
                                        failureReason = "orientation_sample_stale"
                                        null
                                    }

                                    sample.sensorTimestampNs == lastPoseSentTimestampNs -> null
                                    sample.sensorTimestampNs < lastPoseSentTimestampNs -> {
                                        failureReason = "orientation_timestamp_not_increasing"
                                        null
                                    }

                                    else -> when (val delta = poseDeltaTracker.preview(sample.relativeOrientation)) {
                                        is PoseDeltaResult.Fault -> {
                                            failureReason = "orientation_${delta.code.name.lowercase()}"
                                            null
                                        }

                                        is PoseDeltaResult.Ready -> PendingMotion.Pose(
                                            leaseId = lease,
                                            calibrationId = calibrationId,
                                            sample = sample,
                                            angularDeltaRad = delta.angularDeltaRad,
                                            generation = motionGeneration,
                                        )
                                    }
                                }
                            }

                            null -> null
                        }
                    }
                }
            }

            if (action == null) {
                failureReason?.let {
                    forceSafetyStop(it, releaseLease = true, stopRecording = true)
                }
                return
            }

            when (action) {
                is PendingMotion.Cartesian -> {
                    val sequence = socket.send(
                        "motion.cartesian_velocity",
                        ProtocolBodies.cartesianVelocity(
                            action.leaseId,
                            action.command.linear,
                            action.command.angular,
                        ),
                    )
                    if (sequence == null) {
                        if (synchronized(lock) { transportState != TransportState.FAILED }) {
                            forceSafetyStop("motion_send_failed", releaseLease = true, stopRecording = true)
                        }
                    } else {
                        synchronized(lock) {
                            if (
                                deadmanActive &&
                                motionInput == MotionInput.TOUCH_AXIS &&
                                motionGeneration == action.generation
                            ) {
                                pendingMotionAck = PendingMotionAck(
                                    seq = sequence,
                                    type = "motion.cartesian_velocity",
                                    generation = action.generation,
                                    sentAtMonotonicMs = monotonicClock(),
                                )
                            }
                        }
                    }
                }

                is PendingMotion.Pose -> {
                    val sequence = socket.send(
                        "pose.sample",
                        ProtocolBodies.poseSample(
                            leaseId = action.leaseId,
                            calibrationId = action.calibrationId,
                            sensorTimestampMs = action.sample.sensorTimestampMs,
                            confidence = action.sample.confidence,
                            angularDeltaRad = action.angularDeltaRad,
                        ),
                    )
                    if (sequence == null) {
                        if (synchronized(lock) { transportState != TransportState.FAILED }) {
                            forceSafetyStop("pose_send_failed", releaseLease = true, stopRecording = true)
                        }
                    } else {
                        synchronized(lock) {
                            if (
                                deadmanActive &&
                                motionInput == MotionInput.PHONE_ORIENTATION &&
                                activePoseCalibrationId == action.calibrationId &&
                                motionGeneration == action.generation
                            ) {
                                pendingMotionAck = PendingMotionAck(
                                    seq = sequence,
                                    type = "pose.sample",
                                    generation = action.generation,
                                    sentAtMonotonicMs = monotonicClock(),
                                    poseCalibrationId = action.calibrationId,
                                    poseSample = action.sample,
                                )
                            }
                        }
                    }
                }

                null -> Unit
            }
        }
    }

    private fun scheduleMotionTickAfterAck() {
        scheduler.execute(::sendMotionTick)
    }

    private fun motionAckTimeoutMsLocked(): Long {
        val watchdogMs = welcome?.watchdogMs?.toLong() ?: DEFAULT_WATCHDOG_MS
        return (watchdogMs - MOTION_ACK_TIMEOUT_MARGIN_MS).coerceIn(
            MIN_MOTION_ACK_TIMEOUT_MS,
            MAX_MOTION_ACK_TIMEOUT_MS,
        )
    }

    private fun heartbeatTick() {
        var expiredLease = false
        val lease = synchronized(lock) {
            if (
                transportState != TransportState.OPEN ||
                welcome == null ||
                !appForeground ||
                deadmanActive
            ) {
                return
            }
            if (!SafetyGate.leaseIsCurrent(leaseId, leaseDeadlineMonotonicMs, monotonicClock())) {
                expiredLease = leaseId != null
            }
            leaseId
        }
        if (expiredLease) {
            forceSafetyStop("lease_expired", releaseLease = true, stopRecording = true)
            return
        }
        socket.send("heartbeat", ProtocolBodies.heartbeat(lease))
    }

    private fun forceSafetyStop(
        reason: String,
        releaseLease: Boolean,
        stopRecording: Boolean,
        updateEvent: Boolean = true,
    ) {
        val lease: String?
        val sessionReady: Boolean
        val recording: Boolean
        synchronized(motionSendLock) {
            synchronized(lock) {
                cancelMotionLoopLocked()
                invalidateMotionCreditLocked()
                deadmanActive = false
                activeCommand = null
                motionInput = null
                clearPoseMotionLocked()
                lease = leaseId
                sessionReady = welcome != null && transportState == TransportState.OPEN
                recording = recordingActive || recordingPending
                if (releaseLease) {
                    leaseId = null
                    leaseOwnerClientId = null
                    leaseExpiresAtMs = 0L
                    leaseDeadlineMonotonicMs = 0L
                    pendingAcquire = false
                }
                if (stopRecording) {
                    recordingActive = false
                    recordingPending = false
                }
                if (updateEvent) {
                    lastEvent = "安全停止：$reason"
                    lastEventSeverity = "warning"
                }
            }
            if (sessionReady) {
                socket.send("motion.stop", ProtocolBodies.motionStop(lease, reason))
                if (stopRecording && recording && !lease.isNullOrBlank()) {
                    socket.send("recording.stop", ProtocolBodies.recordingStop(lease, reason))
                }
                if (releaseLease && !lease.isNullOrBlank()) {
                    socket.send("control.release", ProtocolBodies.controlRelease(lease))
                }
            }
        }
        publish()
    }

    private fun setEvent(message: String, severity: String) {
        synchronized(lock) {
            lastEvent = message
            lastEventSeverity = severity
        }
        publish()
    }

    private fun stateWatchdogTick() {
        val decision: Pair<Boolean, Boolean> = synchronized(lock) {
            if (transportState != TransportState.OPEN || welcome == null) return
            val fresh = robotStateFreshLocked()
            val freshnessChanged = fresh != lastPublishedRobotStateFresh
            lastPublishedRobotStateFresh = fresh
            SafetyGate.shouldFailClosedForStaleState(
                robotStateFresh = fresh,
                leaseId = leaseId,
                pendingAcquire = pendingAcquire,
                deadmanActive = deadmanActive,
                recordingActive = recordingActive,
                recordingPending = recordingPending,
            ) to freshnessChanged
        }
        if (decision.first) {
            forceSafetyStop("robot_state_stale", releaseLease = true, stopRecording = true)
        } else if (decision.second) {
            publish()
        }
    }

    private fun startHeartbeatLocked(watchdogMs: Int) {
        cancelHeartbeatLocked()
        val period = (watchdogMs / 2L).coerceIn(100L, 250L)
        heartbeatFuture = scheduler.scheduleWithFixedDelay(
            ::heartbeatTick,
            period,
            period,
            TimeUnit.MILLISECONDS,
        )
    }

    private fun startStateWatchdogLocked() {
        cancelStateWatchdogLocked()
        stateWatchdogFuture = scheduler.scheduleWithFixedDelay(
            ::stateWatchdogTick,
            STATE_WATCHDOG_PERIOD_MS,
            STATE_WATCHDOG_PERIOD_MS,
            TimeUnit.MILLISECONDS,
        )
    }

    private fun cancelMotionLoopLocked() {
        motionFuture?.cancel(false)
        motionFuture = null
    }

    private fun clearPoseMotionLocked() {
        activePose = null
        activePoseCalibrationId = null
        activePoseUpdatedAtMonotonicMs = 0L
        lastPoseSentTimestampNs = -1L
        poseDeltaTracker.reset()
    }

    private fun invalidateMotionCreditLocked() {
        motionGeneration += 1L
        pendingMotionAck = null
    }

    private fun cancelHeartbeatLocked() {
        heartbeatFuture?.cancel(false)
        heartbeatFuture = null
    }

    private fun cancelStateWatchdogLocked() {
        stateWatchdogFuture?.cancel(false)
        stateWatchdogFuture = null
    }

    private fun resetSessionLocked() {
        cancelMotionLoopLocked()
        cancelHeartbeatLocked()
        cancelStateWatchdogLocked()
        invalidateMotionCreditLocked()
        welcome = null
        protocolCompatible = false
        robotState = null
        lastRobotStateReceivedAtMs = null
        lastPublishedRobotStateFresh = false
        leaseId = null
        leaseOwnerClientId = null
        leaseExpiresAtMs = 0L
        leaseDeadlineMonotonicMs = 0L
        serverTimeAtWelcomeMs = null
        welcomeReceivedAtMonotonicMs = null
        pendingAcquire = false
        deadmanActive = false
        motionInput = null
        activeCommand = null
        clearPoseMotionLocked()
        recordingActive = false
        recordingPending = false
        recordingDetail = null
        checklist = SafetyChecklist()
    }

    private fun gateContextLocked(): GateContext = GateContext(
        socketOpen = transportState == TransportState.OPEN,
        sessionReady = welcome != null && protocolCompatible,
        appForeground = appForeground,
        leaseId = leaseId,
        leaseDeadlineMonotonicMs = leaseDeadlineMonotonicMs,
        nowMonotonicMs = monotonicClock(),
        robotStateFresh = robotStateFreshLocked(),
        baseLocked = robotState?.baseLocked ?: welcome?.baseLocked,
        watchdogOk = robotState?.watchdogOk,
        estopReason = robotState?.estopReason,
        robotState = robotState?.robotState,
        checklist = checklist,
    )

    private fun leaseDeadlineForServerExpiryLocked(
        expiresAtServerMs: Long,
        nowMonotonicMs: Long,
    ): Long {
        val serverTime = serverTimeAtWelcomeMs ?: return 0L
        val welcomeMonotonic = welcomeReceivedAtMonotonicMs ?: return 0L
        return SafetyGate.leaseDeadlineMonotonic(
            serverTimeAtWelcomeMs = serverTime,
            welcomeReceivedAtMonotonicMs = welcomeMonotonic,
            leaseExpiresAtServerMs = expiresAtServerMs,
            nowMonotonicMs = nowMonotonicMs,
        )
    }

    private fun robotStateFreshLocked(): Boolean {
        return SafetyGate.robotStateIsFresh(lastRobotStateReceivedAtMs, monotonicClock())
    }

    private fun snapshotLocked(): ControllerSnapshot {
        val context = gateContextLocked()
        val acquire = SafetyGate.canAcquire(context)
        val act = SafetyGate.canAct(context)
        val nowMonotonicMs = monotonicClock()
        val leaseRemainingMs = if (leaseDeadlineMonotonicMs > nowMonotonicMs) {
            leaseDeadlineMonotonicMs - nowMonotonicMs
        } else {
            0L
        }
        return ControllerSnapshot(
            transportState = transportState,
            transportDetail = transportDetail,
            welcomeReceived = welcome != null,
            protocolCompatible = protocolCompatible,
            sessionId = welcome?.sessionId,
            mode = welcome?.mode,
            watchdogMs = welcome?.watchdogMs,
            commandRateHz = welcome?.commandRateHz,
            limitsJson = welcome?.limitsJson,
            robotState = robotState?.robotState,
            estopReason = robotState?.estopReason,
            jointPositionRad = robotState?.jointPositionRad.orEmpty(),
            jointVelocityRadS = robotState?.jointVelocityRadS.orEmpty(),
            tcpPoseJson = robotState?.tcpPoseJson,
            gripperPct = robotState?.gripperPct?.takeIf { it.isFinite() },
            baseLocked = robotState?.baseLocked ?: welcome?.baseLocked,
            watchdogOk = robotState?.watchdogOk,
            leaseId = leaseId,
            leaseOwnerClientId = leaseOwnerClientId,
            leaseExpiresAtMs = leaseExpiresAtMs,
            leaseRemainingMs = leaseRemainingMs,
            pendingAcquire = pendingAcquire,
            deadmanActive = deadmanActive,
            motionInput = motionInput,
            recordingActive = recordingActive,
            recordingPending = recordingPending,
            recordingDetail = recordingDetail,
            checklist = checklist,
            canRequestControl = acquire.allowed && leaseId == null && !pendingAcquire,
            canAct = act.allowed,
            gateReason = when {
                act.allowed -> act.reason
                leaseId != null -> act.reason
                else -> acquire.reason
            },
            lastEvent = lastEvent,
            lastEventSeverity = lastEventSeverity,
        )
    }

    private fun publish() {
        val snapshot = synchronized(lock) { snapshotLocked() }
        listener(snapshot)
    }

    companion object {
        private const val MOTION_PERIOD_MS = 50L
        private const val POSE_SAMPLE_MAX_AGE_MS = 150L
        private const val MINIMUM_POSE_CONFIDENCE = 0.8
        private const val STATE_WATCHDOG_PERIOD_MS = 100L
        private const val DEFAULT_WATCHDOG_MS = 300L
        private const val MOTION_ACK_TIMEOUT_MARGIN_MS = 50L
        private const val MIN_MOTION_ACK_TIMEOUT_MS = 50L
        private const val MAX_MOTION_ACK_TIMEOUT_MS = 250L
        private val CLEANUP_ACK_TYPES = setOf("motion.stop", "control.release", "recording.stop")
        private val STICKY_EVENT_SEVERITIES = setOf("warning", "warn", "error", "critical", "fatal")
    }

    private fun poseSampleIsValid(sample: CalibratedOrientation): Boolean {
        return sample.sensorTimestampNs > 0L && sample.sensorTimestampMs > 0L &&
            sample.confidence.isFinite() &&
            sample.confidence in MINIMUM_POSE_CONFIDENCE..1.0 &&
            sample.relativeOrientation.normalizedOrNull() != null &&
            sample.relativeRotationRad.x.isFinite() &&
            sample.relativeRotationRad.y.isFinite() &&
            sample.relativeRotationRad.z.isFinite()
    }
}
