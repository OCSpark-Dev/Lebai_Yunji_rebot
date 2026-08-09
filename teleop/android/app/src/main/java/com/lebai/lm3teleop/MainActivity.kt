package com.lebai.lm3teleop

import android.animation.Animator
import android.animation.AnimatorListenerAdapter
import android.animation.ValueAnimator
import android.annotation.SuppressLint
import android.app.Activity
import android.os.Build
import android.os.Bundle
import android.os.SystemClock
import android.view.MotionEvent
import android.view.View
import android.view.WindowManager
import android.widget.Button
import android.widget.SeekBar
import android.widget.Toast
import com.lebai.lm3teleop.control.ControllerSnapshot
import com.lebai.lm3teleop.control.MotionInput
import com.lebai.lm3teleop.control.TeleopController
import com.lebai.lm3teleop.core.AxisDirection
import com.lebai.lm3teleop.core.CalibratedOrientation
import com.lebai.lm3teleop.core.CalibrationResult
import com.lebai.lm3teleop.core.DeadmanTouchDecision
import com.lebai.lm3teleop.core.DeadmanTouchTracker
import com.lebai.lm3teleop.core.LifecycleSafetyPolicy
import com.lebai.lm3teleop.core.NetworkPolicy
import com.lebai.lm3teleop.core.OrientationResult
import com.lebai.lm3teleop.core.OrientationSafetyMapper
import com.lebai.lm3teleop.core.OrientationSensorSample
import com.lebai.lm3teleop.core.SafetyChecklist
import com.lebai.lm3teleop.core.SpeedGear
import com.lebai.lm3teleop.databinding.ActivityMainBinding
import com.lebai.lm3teleop.network.TransportState
import com.lebai.lm3teleop.protocol.CAMERA_TOP
import com.lebai.lm3teleop.protocol.CAMERA_WRIST
import com.lebai.lm3teleop.sensor.PhoneOrientationSensor
import java.util.Locale
import java.util.UUID

class MainActivity : Activity() {
    private lateinit var binding: ActivityMainBinding
    private lateinit var controller: TeleopController
    private lateinit var clientId: String
    private var selectedAxis = AxisDirection.X_POS
    private var selectedGear = SpeedGear.CREEP
    private var lastSnapshot = ControllerSnapshot()
    private var unlockAnimator: ValueAnimator? = null
    private var unlockHolding = false
    private var syncingChecklistUi = false
    private lateinit var axisButtons: Map<Button, AxisDirection>
    private val deadmanTouchTracker = DeadmanTouchTracker()
    private val gyroDeadmanTouchTracker = DeadmanTouchTracker()
    private val orientationMapper = OrientationSafetyMapper()
    private lateinit var orientationSensor: PhoneOrientationSensor
    private var latestOrientation: CalibratedOrientation? = null
    private var orientationCalibrationId: String? = null
    private var gyroHolding = false
    private var orientationStatus = "等待姿态传感器"
    private var lastOrientationUiUpdateNs = 0L
    private var previousSessionReady = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)

        val preferences = getSharedPreferences(PREFS_NAME, MODE_PRIVATE)
        clientId = preferences.getString(KEY_CLIENT_ID, null) ?: UUID.randomUUID().toString().also {
            preferences.edit().putString(KEY_CLIENT_ID, it).apply()
        }
        binding.endpointInput.setText(preferences.getString(KEY_ENDPOINT, ""))

        controller = TeleopController(
            clientId = clientId,
            appVersion = BuildConfig.VERSION_NAME,
            listener = { snapshot -> runOnUiThread { render(snapshot) } },
        )
        orientationSensor = PhoneOrientationSensor(
            context = this,
            onSample = ::onOrientationSample,
            onFault = ::onOrientationSensorFault,
        )

        setupConnection(preferences)
        setupChecklist()
        setupUnlockHold()
        setupOrientationControls()
        setupMotionControls()
        setupGripper()
        setupRecording()
        render(lastSnapshot)
    }

    override fun onResume() {
        super.onResume()
        if (::orientationSensor.isInitialized) {
            orientationStatus = if (orientationSensor.start()) {
                "传感器已启动：${orientationSensor.description}"
            } else {
                orientationSensor.description
            }
            renderOrientationState(force = true)
        }
        if (::controller.isInitialized) controller.onAppForeground()
    }

    override fun onPause() {
        cancelUnlockHold()
        deadmanTouchTracker.reset()
        gyroDeadmanTouchTracker.reset()
        gyroHolding = false
        resetOrientationInput("App 已进入后台，姿态归零已清除")
        if (::orientationSensor.isInitialized) orientationSensor.stop()
        if (::controller.isInitialized) controller.onAppBackground()
        super.onPause()
    }

    override fun onDestroy() {
        cancelUnlockHold()
        deadmanTouchTracker.reset()
        gyroDeadmanTouchTracker.reset()
        if (::orientationSensor.isInitialized) orientationSensor.stop()
        if (
            ::controller.isInitialized &&
            LifecycleSafetyPolicy.shouldDestroyController(isChangingConfigurations)
        ) {
            controller.destroy()
        }
        super.onDestroy()
    }

    private fun setupConnection(preferences: android.content.SharedPreferences) {
        binding.connectButton.setOnClickListener {
            val validation = NetworkPolicy.validate(
                binding.endpointInput.text.toString(),
                allowCleartextLan = BuildConfig.DEBUG,
            )
            if (!validation.valid) {
                showMessage(validation.error ?: "连接地址无效")
                return@setOnClickListener
            }
            preferences.edit()
                .putString(KEY_ENDPOINT, validation.normalizedUrl)
                .apply()
            binding.transportWarning.text = validation.warning
                ?: "WSS 已启用；仍应在隔离控制网中部署并校验证书。"
            resetOrientationInput("正在建立新会话；连接后请重新归零")
            controller.connect(validation.normalizedUrl!!, defaultClientName())
        }

        binding.disconnectButton.setOnClickListener {
            resetOrientationInput("连接已断开，姿态归零已清除")
            controller.disconnect("operator_disconnect")
        }
    }

    private fun setupChecklist() {
        val listener = android.widget.CompoundButton.OnCheckedChangeListener { _, _ ->
            if (!syncingChecklistUi) {
                controller.updateChecklist(currentChecklist())
            }
        }
        binding.baseStationaryCheck.setOnCheckedChangeListener(listener)
        binding.workspaceClearCheck.setOnCheckedChangeListener(listener)
        binding.estopAccessibleCheck.setOnCheckedChangeListener(listener)
        binding.toolSecureCheck.setOnCheckedChangeListener(listener)
    }

    @SuppressLint("ClickableViewAccessibility")
    private fun setupUnlockHold() {
        binding.unlockHoldButton.setOnClickListener { /* Touch listener owns the hold gesture. */ }
        binding.unlockHoldButton.setOnTouchListener { view, event ->
            when (event.actionMasked) {
                MotionEvent.ACTION_DOWN -> {
                    if (!view.isEnabled) return@setOnTouchListener false
                    unlockHolding = true
                    binding.unlockProgress.progress = 0
                    unlockAnimator?.cancel()
                    unlockAnimator = ValueAnimator.ofInt(0, 100).apply {
                        duration = UNLOCK_HOLD_MS
                        addUpdateListener { binding.unlockProgress.progress = it.animatedValue as Int }
                        addListener(object : AnimatorListenerAdapter() {
                            private var cancelled = false

                            override fun onAnimationCancel(animation: Animator) {
                                cancelled = true
                            }

                            override fun onAnimationEnd(animation: Animator) {
                                if (!cancelled && unlockHolding) {
                                    unlockHolding = false
                                    binding.unlockProgress.progress = 100
                                    controller.requestControl()
                                }
                            }
                        })
                        start()
                    }
                    true
                }

                MotionEvent.ACTION_UP, MotionEvent.ACTION_CANCEL, MotionEvent.ACTION_OUTSIDE -> {
                    if (unlockHolding) cancelUnlockHold()
                    view.performClick()
                    true
                }

                else -> true
            }
        }
        binding.releaseButton.setOnClickListener {
            gyroHolding = false
            gyroDeadmanTouchTracker.reset()
            resetOrientationInput("控制权已释放，请重新归零")
            controller.releaseControl("operator_release")
        }
    }

    @SuppressLint("ClickableViewAccessibility")
    private fun setupOrientationControls() {
        binding.gyroZeroButton.setOnClickListener {
            if (lastSnapshot.deadmanActive) {
                showMessage("运动期间不能重新归零")
                return@setOnClickListener
            }
            when (val result = orientationMapper.calibrate(SystemClock.elapsedRealtimeNanos())) {
                is CalibrationResult.Success -> {
                    latestOrientation = result.value
                    orientationCalibrationId = UUID.randomUUID().toString()
                    orientationStatus = "已归零；按住姿态 DEADMAN 后首帧仅用于 priming"
                }

                is CalibrationResult.Failure -> {
                    resetOrientationInput("归零失败：${result.code.reason}")
                    showMessage(orientationStatus)
                }
            }
            renderOrientationState(force = true)
        }

        binding.gyroDeadmanButton.setOnClickListener {
            // Accessibility hook; motion requires a continuous touch hold.
        }
        binding.gyroDeadmanButton.setOnTouchListener { view, event ->
            when (event.actionMasked) {
                MotionEvent.ACTION_DOWN -> {
                    if (!view.isEnabled) return@setOnTouchListener false
                    val tracking = orientationMapper.current(SystemClock.elapsedRealtimeNanos())
                    val calibrationId = orientationCalibrationId
                    if (tracking !is OrientationResult.Tracking || calibrationId.isNullOrBlank()) {
                        resetOrientationInput("姿态样本未归零或已过期")
                        showMessage(orientationStatus)
                        return@setOnTouchListener true
                    }
                    gyroDeadmanTouchTracker.onDown(event.getPointerId(event.actionIndex))
                    gyroHolding = true
                    val started = controller.startPoseMotion(calibrationId, tracking.value)
                    if (!started) {
                        gyroHolding = false
                        gyroDeadmanTouchTracker.reset()
                        showMessage("姿态动作未发送：${lastSnapshot.gateReason}")
                    }
                    renderOrientationState(force = true)
                    true
                }

                MotionEvent.ACTION_MOVE -> {
                    val pointerIndex = event.findPointerIndex(gyroDeadmanTouchTracker.activePointerId)
                    val activePointerPresent = pointerIndex >= 0
                    val insideBounds = activePointerPresent &&
                        event.getX(pointerIndex) >= 0f &&
                        event.getX(pointerIndex) < view.width.toFloat() &&
                        event.getY(pointerIndex) >= 0f &&
                        event.getY(pointerIndex) < view.height.toFloat()
                    if (
                        gyroDeadmanTouchTracker.onMove(activePointerPresent, insideBounds) ==
                        DeadmanTouchDecision.STOP
                    ) {
                        stopOrientationMotion(
                            if (activePointerPresent) {
                                "orientation_deadman_outside_bounds"
                            } else {
                                "orientation_deadman_pointer_lost"
                            },
                            clearCalibration = true,
                        )
                    }
                    true
                }

                MotionEvent.ACTION_POINTER_UP -> {
                    if (
                        gyroDeadmanTouchTracker.onPointerUp(event.getPointerId(event.actionIndex)) ==
                        DeadmanTouchDecision.STOP
                    ) {
                        stopOrientationMotion("orientation_deadman_active_pointer_released")
                    }
                    true
                }

                MotionEvent.ACTION_UP -> {
                    if (gyroDeadmanTouchTracker.onTerminal() == DeadmanTouchDecision.STOP) {
                        stopOrientationMotion("orientation_deadman_released")
                    }
                    view.performClick()
                    true
                }

                MotionEvent.ACTION_CANCEL, MotionEvent.ACTION_OUTSIDE -> {
                    if (gyroDeadmanTouchTracker.onTerminal() == DeadmanTouchDecision.STOP) {
                        stopOrientationMotion("orientation_deadman_cancelled", clearCalibration = true)
                    }
                    true
                }

                else -> true
            }
        }
        renderOrientationState(force = true)
    }

    private fun onOrientationSample(sample: OrientationSensorSample) {
        when (val result = orientationMapper.ingest(sample)) {
            is OrientationResult.AwaitingCalibration -> {
                latestOrientation = null
                orientationStatus = "传感器跟踪正常；请保持手机稳定并点击归零"
            }

            is OrientationResult.Tracking -> {
                latestOrientation = result.value
                orientationStatus = if (gyroHolding) {
                    "姿态 DEADMAN 生效；仅发送 TCP 旋转增量"
                } else {
                    "姿态已归零；等待按住 DEADMAN"
                }
                val calibrationId = orientationCalibrationId
                if (
                    gyroHolding &&
                    (calibrationId.isNullOrBlank() || !controller.updatePoseMotion(calibrationId, result.value))
                ) {
                    gyroHolding = false
                    gyroDeadmanTouchTracker.reset()
                    resetOrientationInput("姿态控制器已失败关闭，请重新归零和解锁")
                }
            }

            is OrientationResult.Fault -> {
                failClosedOrientation(result.code.reason, "orientation_${result.code.name.lowercase()}")
            }
        }
        renderOrientationState()
    }

    private fun onOrientationSensorFault(reason: String) {
        failClosedOrientation(reason, "orientation_sensor_fault")
    }

    private fun failClosedOrientation(message: String, stopReason: String) {
        val wasHolding = gyroHolding
        gyroHolding = false
        gyroDeadmanTouchTracker.reset()
        resetOrientationInput("姿态输入失败关闭：$message")
        if (wasHolding) {
            controller.releaseControl(stopReason)
        }
        renderOrientationState(force = true)
    }

    private fun stopOrientationMotion(reason: String, clearCalibration: Boolean = false) {
        val wasHolding = gyroHolding
        gyroHolding = false
        gyroDeadmanTouchTracker.reset()
        if (wasHolding) controller.stopMotion(reason)
        if (clearCalibration) resetOrientationInput("姿态触摸被取消，请重新归零")
        renderOrientationState(force = true)
    }

    private fun resetOrientationInput(status: String) {
        orientationMapper.reset()
        latestOrientation = null
        orientationCalibrationId = null
        orientationStatus = status
    }

    private fun renderOrientationState(force: Boolean = false) {
        if (!::binding.isInitialized || !::orientationSensor.isInitialized) return
        val nowNs = SystemClock.elapsedRealtimeNanos()
        if (!force && nowNs - lastOrientationUiUpdateNs < ORIENTATION_UI_PERIOD_NS) return
        lastOrientationUiUpdateNs = nowNs
        val tracking = latestOrientation
        binding.gyroSensorText.text = "${orientationSensor.description}\n$orientationStatus"
        binding.gyroRelativeText.text = if (tracking == null) {
            "相对姿态: Rx —  Ry —  Rz —"
        } else {
            String.format(
                Locale.US,
                "相对姿态: Rx %+.1f°  Ry %+.1f°  Rz %+.1f°  置信度 %.2f",
                Math.toDegrees(tracking.relativeRotationRad.x),
                Math.toDegrees(tracking.relativeRotationRad.y),
                Math.toDegrees(tracking.relativeRotationRad.z),
                tracking.confidence,
            )
        }
        val freshSample = orientationMapper.canCalibrate(nowNs)
        val sessionReady = lastSnapshot.transportState == TransportState.OPEN && lastSnapshot.welcomeReceived
        binding.gyroZeroButton.isEnabled = orientationSensor.available &&
            sessionReady &&
            freshSample &&
            !lastSnapshot.deadmanActive
        val canStart = lastSnapshot.canAct && orientationMapper.calibrated && freshSample
        binding.gyroDeadmanButton.isEnabled = gyroHolding || (canStart && !lastSnapshot.deadmanActive)
        binding.gyroDeadmanButton.alpha = if (binding.gyroDeadmanButton.isEnabled) 1.0f else 0.45f
        binding.gyroDeadmanButton.text = if (gyroHolding) {
            "保持按住：手机姿态 → TCP Rx/Ry/Rz"
        } else {
            "归零后按住姿态 DEADMAN"
        }
    }

    @SuppressLint("ClickableViewAccessibility")
    private fun setupMotionControls() {
        axisButtons = linkedMapOf(
            binding.xPlusButton to AxisDirection.X_POS,
            binding.xMinusButton to AxisDirection.X_NEG,
            binding.yPlusButton to AxisDirection.Y_POS,
            binding.yMinusButton to AxisDirection.Y_NEG,
            binding.zPlusButton to AxisDirection.Z_POS,
            binding.zMinusButton to AxisDirection.Z_NEG,
            binding.rxPlusButton to AxisDirection.RX_POS,
            binding.rxMinusButton to AxisDirection.RX_NEG,
            binding.ryPlusButton to AxisDirection.RY_POS,
            binding.ryMinusButton to AxisDirection.RY_NEG,
            binding.rzPlusButton to AxisDirection.RZ_POS,
            binding.rzMinusButton to AxisDirection.RZ_NEG,
        )
        axisButtons.forEach { (button, direction) ->
            button.setOnClickListener {
                if (lastSnapshot.deadmanActive) {
                    controller.stopMotion("axis_selection_changed")
                    return@setOnClickListener
                }
                selectedAxis = direction
                renderMotionSelection()
            }
        }

        binding.speedGroup.setOnCheckedChangeListener { _, checkedId ->
            selectedGear = when (checkedId) {
                binding.speedLow.id -> SpeedGear.LOW
                binding.speedCareful.id -> SpeedGear.CAREFUL
                else -> SpeedGear.CREEP
            }
            renderMotionSelection()
        }

        binding.deadmanButton.setOnTouchListener { view, event ->
            when (event.actionMasked) {
                MotionEvent.ACTION_DOWN -> {
                    if (!view.isEnabled) return@setOnTouchListener false
                    deadmanTouchTracker.onDown(event.getPointerId(event.actionIndex))
                    if (!controller.startMotion(selectedAxis, selectedGear)) {
                        deadmanTouchTracker.reset()
                        showMessage("动作未发送：${lastSnapshot.gateReason}")
                    }
                    true
                }

                MotionEvent.ACTION_MOVE -> {
                    val pointerIndex = event.findPointerIndex(deadmanTouchTracker.activePointerId)
                    val activePointerPresent = pointerIndex >= 0
                    val insideBounds = activePointerPresent &&
                        event.getX(pointerIndex) >= 0f &&
                        event.getX(pointerIndex) < view.width.toFloat() &&
                        event.getY(pointerIndex) >= 0f &&
                        event.getY(pointerIndex) < view.height.toFloat()
                    if (
                        deadmanTouchTracker.onMove(activePointerPresent, insideBounds) ==
                        DeadmanTouchDecision.STOP
                    ) {
                        controller.stopMotion(
                            if (activePointerPresent) "deadman_outside_bounds" else "deadman_pointer_lost",
                        )
                    }
                    true
                }

                MotionEvent.ACTION_POINTER_UP -> {
                    if (
                        deadmanTouchTracker.onPointerUp(event.getPointerId(event.actionIndex)) ==
                        DeadmanTouchDecision.STOP
                    ) {
                        controller.stopMotion("deadman_active_pointer_released")
                    }
                    true
                }

                MotionEvent.ACTION_UP -> {
                    if (deadmanTouchTracker.onTerminal() == DeadmanTouchDecision.STOP) {
                        controller.stopMotion("deadman_released")
                    }
                    view.performClick()
                    true
                }

                MotionEvent.ACTION_CANCEL, MotionEvent.ACTION_OUTSIDE -> {
                    if (deadmanTouchTracker.onTerminal() == DeadmanTouchDecision.STOP) {
                        controller.stopMotion("deadman_cancelled")
                    }
                    true
                }

                else -> true
            }
        }
        binding.deadmanButton.setOnClickListener { /* Accessibility hook; movement requires touch hold. */ }
        binding.stopButton.setOnClickListener {
            gyroHolding = false
            gyroDeadmanTouchTracker.reset()
            resetOrientationInput("已立即停止，姿态归零已清除")
            controller.emergencyStop("operator_stop_button")
            renderOrientationState(force = true)
        }
        renderMotionSelection()
    }

    @SuppressLint("ClickableViewAccessibility")
    private fun setupGripper() {
        binding.gripperSeekBar.setOnSeekBarChangeListener(object : SeekBar.OnSeekBarChangeListener {
            override fun onProgressChanged(seekBar: SeekBar?, progress: Int, fromUser: Boolean) {
                binding.gripperValueText.text = "目标开度: $progress%"
            }

            override fun onStartTrackingTouch(seekBar: SeekBar?) = Unit
            override fun onStopTrackingTouch(seekBar: SeekBar?) = Unit
        })
        binding.gripperHoldButton.setOnClickListener { /* Hold gesture only. */ }
        binding.gripperHoldButton.setOnTouchListener { view, event ->
            when (event.actionMasked) {
                MotionEvent.ACTION_DOWN -> {
                    if (!view.isEnabled) return@setOnTouchListener false
                    controller.sendGripper(binding.gripperSeekBar.progress)
                    true
                }

                MotionEvent.ACTION_UP, MotionEvent.ACTION_CANCEL, MotionEvent.ACTION_OUTSIDE -> {
                    controller.emergencyStop("gripper_deadman_released")
                    view.performClick()
                    true
                }

                else -> true
            }
        }
    }

    private fun setupRecording() {
        binding.recordStartButton.setOnClickListener {
            val cameras = buildList {
                if (binding.wristCameraCheck.isChecked) add(CAMERA_WRIST)
                if (binding.sceneCameraCheck.isChecked) add(CAMERA_TOP)
            }
            controller.startRecording(
                task = binding.taskInput.text.toString(),
                episodeId = binding.episodeInput.text.toString().takeIf { it.isNotBlank() },
                cameras = cameras,
            )
        }
        binding.recordStopButton.setOnClickListener {
            controller.stopRecording("operator_stop")
        }
    }

    private fun currentChecklist(): SafetyChecklist = SafetyChecklist(
        baseStationary = binding.baseStationaryCheck.isChecked,
        workspaceClear = binding.workspaceClearCheck.isChecked,
        estopAccessible = binding.estopAccessibleCheck.isChecked,
        toolSecure = binding.toolSecureCheck.isChecked,
    )

    private fun cancelUnlockHold() {
        unlockHolding = false
        unlockAnimator?.cancel()
        unlockAnimator = null
        if (::binding.isInitialized) binding.unlockProgress.progress = 0
    }

    private fun renderMotionSelection() {
        if (!::binding.isInitialized) return
        binding.selectedMotionText.text = "已选择: ${selectedAxis.displayName}（基坐标系）"
        binding.speedDescription.text = String.format(
            Locale.US,
            "线速度 %.3f m/s · 角速度 %.2f rad/s",
            selectedGear.linearMps,
            selectedGear.angularRps,
        )
        if (::axisButtons.isInitialized) {
            axisButtons.forEach { (button, direction) ->
                button.alpha = if (direction == selectedAxis) 1.0f else 0.62f
            }
        }
    }

    private fun render(snapshot: ControllerSnapshot) {
        val sessionReady = snapshot.transportState == TransportState.OPEN && snapshot.welcomeReceived
        val poseLeaseWasLost = orientationCalibrationId != null &&
            lastSnapshot.leaseId != null &&
            snapshot.leaseId == null
        if (previousSessionReady && !sessionReady) {
            gyroHolding = false
            gyroDeadmanTouchTracker.reset()
            resetOrientationInput("会话已关闭，姿态归零已清除")
        } else if (poseLeaseWasLost) {
            gyroHolding = false
            gyroDeadmanTouchTracker.reset()
            resetOrientationInput("控制租约已结束，姿态归零已清除")
        }
        previousSessionReady = sessionReady
        if (
            gyroHolding &&
            (!snapshot.deadmanActive || snapshot.motionInput != MotionInput.PHONE_ORIENTATION)
        ) {
            gyroHolding = false
            gyroDeadmanTouchTracker.reset()
            resetOrientationInput("姿态运动被安全停止，请重新归零")
        }
        lastSnapshot = snapshot
        syncingChecklistUi = true
        try {
            binding.baseStationaryCheck.isChecked = snapshot.checklist.baseStationary
            binding.workspaceClearCheck.isChecked = snapshot.checklist.workspaceClear
            binding.estopAccessibleCheck.isChecked = snapshot.checklist.estopAccessible
            binding.toolSecureCheck.isChecked = snapshot.checklist.toolSecure
        } finally {
            syncingChecklistUi = false
        }
        val connectedOrConnecting = snapshot.transportState in setOf(
            TransportState.CONNECTING,
            TransportState.OPEN,
            TransportState.CLOSING,
        )
        binding.connectButton.isEnabled = !connectedOrConnecting
        binding.disconnectButton.isEnabled = connectedOrConnecting
        binding.endpointInput.isEnabled = !connectedOrConnecting

        binding.unlockHoldButton.isEnabled = snapshot.canRequestControl
        binding.unlockHoldButton.text = when {
            snapshot.pendingAcquire -> "等待服务端授权…"
            snapshot.leaseId != null -> "控制权已解锁"
            else -> "按住 1.5 秒申请控制权"
        }
        binding.releaseButton.isEnabled = snapshot.leaseId != null
        val touchMotionEnabled = snapshot.canAct && !gyroHolding &&
            snapshot.motionInput != MotionInput.PHONE_ORIENTATION
        binding.deadmanButton.isEnabled = touchMotionEnabled
        binding.deadmanButton.alpha = if (touchMotionEnabled) 1.0f else 0.45f
        binding.deadmanButton.text = if (snapshot.deadmanActive) {
            "保持按住：${selectedAxis.displayName}"
        } else {
            "按住以运动（DEADMAN）"
        }
        if (::axisButtons.isInitialized) {
            axisButtons.keys.forEach { it.isEnabled = !snapshot.deadmanActive }
        }
        for (index in 0 until binding.speedGroup.childCount) {
            binding.speedGroup.getChildAt(index).isEnabled = !snapshot.deadmanActive
        }
        binding.gripperHoldButton.isEnabled = snapshot.canAct && !snapshot.deadmanActive
        binding.recordStartButton.isEnabled = snapshot.canAct && !snapshot.recordingActive && !snapshot.recordingPending
        binding.recordStopButton.isEnabled = snapshot.leaseId != null && (snapshot.recordingActive || snapshot.recordingPending)

        binding.statusText.text = buildStatusText(snapshot)
        binding.statusText.setTextColor(
            getColor(
                when {
                    snapshot.estopReason?.isNotBlank() == true || snapshot.watchdogOk == false -> R.color.red
                    snapshot.canAct -> R.color.green
                    else -> R.color.text_primary
                },
            ),
        )
        binding.lastEventText.text = "${snapshot.lastEvent} · ${snapshot.gateReason}"
        binding.lastEventText.setTextColor(
            getColor(
                when (snapshot.lastEventSeverity.lowercase()) {
                    "error", "critical", "fatal" -> R.color.red
                    "warning", "warn" -> R.color.amber
                    else -> R.color.text_secondary
                },
            ),
        )
        renderOrientationState(force = true)
    }

    private fun buildStatusText(snapshot: ControllerSnapshot): String = buildString {
        append("连接: ${snapshot.transportState} (${snapshot.transportDetail})\n")
        append("握手: ${if (snapshot.welcomeReceived) "已收到 welcome" else "未完成"}")
        if (snapshot.welcomeReceived && !snapshot.protocolCompatible) append(" [参数不兼容]")
        append('\n')
        append("会话: ${snapshot.sessionId ?: "-"}  模式: ${snapshot.mode ?: "-"}\n")
        append("命令率: ${snapshot.commandRateHz ?: "-"} Hz  看门狗: ${snapshot.watchdogMs ?: "-"} ms\n")
        append("机器人: ${snapshot.robotState ?: "未知"}  急停: ${snapshot.estopReason ?: "无/未知"}\n")
        append("底盘锁定: ${formatBoolean(snapshot.baseLocked)}  看门狗正常: ${formatBoolean(snapshot.watchdogOk)}\n")
        append("控制权: ${snapshot.leaseId?.take(12) ?: "无"}")
        if (snapshot.leaseId != null) {
            append(
                String.format(
                    Locale.US,
                    "  单调剩余 %.1f s",
                    snapshot.leaseRemainingMs / 1_000.0,
                ),
            )
        }
        append('\n')
        append(
            "输入: " + when (snapshot.motionInput) {
                MotionInput.TOUCH_AXIS -> "触屏轴向"
                MotionInput.PHONE_ORIENTATION -> "手机姿态（仅 TCP 旋转）"
                null -> "无"
            },
        )
        append('\n')
        append("夹爪反馈: ${snapshot.gripperPct?.let { String.format(Locale.US, "%.1f%%", it) } ?: "未知"}\n")
        append("录制: ${if (snapshot.recordingActive) "进行中" else if (snapshot.recordingPending) "等待确认" else "未录制"}")
        if (snapshot.jointPositionRad.size == 6) {
            append("\n关节(rad): ")
            append(snapshot.jointPositionRad.joinToString(", ") { String.format(Locale.US, "%.3f", it) })
        }
        if (!snapshot.limitsJson.isNullOrBlank() && snapshot.limitsJson != "{}") {
            append("\n服务端 limits: ${snapshot.limitsJson}")
        }
    }

    private fun formatBoolean(value: Boolean?): String = when (value) {
        true -> "是"
        false -> "否"
        null -> "未知"
    }

    private fun showMessage(message: String) {
        binding.lastEventText.text = message
        binding.lastEventText.setTextColor(getColor(R.color.red))
        Toast.makeText(this, message, Toast.LENGTH_LONG).show()
    }

    private fun defaultClientName(): String =
        Build.MODEL.trim().replace(Regex("\\s+"), " ").take(48).ifBlank { "Android phone" }

    companion object {
        private const val PREFS_NAME = "lm3_up_teleop_settings"
        private const val KEY_CLIENT_ID = "client_id"
        private const val KEY_ENDPOINT = "endpoint"
        private const val UNLOCK_HOLD_MS = 1_500L
        private const val ORIENTATION_UI_PERIOD_NS = 50_000_000L
    }
}
