package com.lebai.lm3teleop

import android.animation.Animator
import android.animation.AnimatorListenerAdapter
import android.animation.ValueAnimator
import android.annotation.SuppressLint
import android.app.Activity
import android.os.Build
import android.os.Bundle
import android.view.MotionEvent
import android.view.View
import android.view.WindowManager
import android.widget.Button
import android.widget.SeekBar
import android.widget.Toast
import com.lebai.lm3teleop.control.ControllerSnapshot
import com.lebai.lm3teleop.control.TeleopController
import com.lebai.lm3teleop.core.AxisDirection
import com.lebai.lm3teleop.core.DeadmanTouchDecision
import com.lebai.lm3teleop.core.DeadmanTouchTracker
import com.lebai.lm3teleop.core.LifecycleSafetyPolicy
import com.lebai.lm3teleop.core.NetworkPolicy
import com.lebai.lm3teleop.core.SafetyChecklist
import com.lebai.lm3teleop.core.SpeedGear
import com.lebai.lm3teleop.databinding.ActivityMainBinding
import com.lebai.lm3teleop.network.TransportState
import com.lebai.lm3teleop.protocol.CAMERA_TOP
import com.lebai.lm3teleop.protocol.CAMERA_WRIST
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
        binding.clientNameInput.setText(
            preferences.getString(KEY_CLIENT_NAME, null)
                ?: "${Build.MANUFACTURER} ${Build.MODEL}".trim(),
        )

        controller = TeleopController(
            clientId = clientId,
            appVersion = BuildConfig.VERSION_NAME,
            listener = { snapshot -> runOnUiThread { render(snapshot) } },
        )

        setupConnection(preferences)
        setupChecklist()
        setupUnlockHold()
        setupMotionControls()
        setupGripper()
        setupRecording()
        render(lastSnapshot)
    }

    override fun onResume() {
        super.onResume()
        if (::controller.isInitialized) controller.onAppForeground()
    }

    override fun onPause() {
        cancelUnlockHold()
        deadmanTouchTracker.reset()
        if (::binding.isInitialized) binding.tokenInput.text?.clear()
        if (::controller.isInitialized) controller.onAppBackground()
        super.onPause()
    }

    override fun onDestroy() {
        cancelUnlockHold()
        deadmanTouchTracker.reset()
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
            val clientName = binding.clientNameInput.text.toString().trim()
            val token = binding.tokenInput.text.toString()
            if (clientName.isBlank()) {
                showMessage("请输入操作者或终端名称")
                return@setOnClickListener
            }
            if (token.isBlank()) {
                showMessage("请输入共享 token")
                return@setOnClickListener
            }
            preferences.edit()
                .putString(KEY_ENDPOINT, validation.normalizedUrl)
                .putString(KEY_CLIENT_NAME, clientName)
                .apply()
            binding.transportWarning.text = validation.warning
                ?: "WSS 已启用；仍应在隔离控制网中部署并校验证书。"
            controller.connect(validation.normalizedUrl!!, clientName, token)
            binding.tokenInput.text?.clear()
        }

        binding.disconnectButton.setOnClickListener {
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
            controller.releaseControl("operator_release")
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
            controller.emergencyStop("operator_stop_button")
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
        binding.clientNameInput.isEnabled = !connectedOrConnecting
        binding.tokenInput.isEnabled = !connectedOrConnecting

        binding.unlockHoldButton.isEnabled = snapshot.canRequestControl
        binding.unlockHoldButton.text = when {
            snapshot.pendingAcquire -> "等待服务端授权…"
            snapshot.leaseId != null -> "控制权已解锁"
            else -> "按住 1.5 秒申请控制权"
        }
        binding.releaseButton.isEnabled = snapshot.leaseId != null
        binding.deadmanButton.isEnabled = snapshot.canAct
        binding.deadmanButton.alpha = if (snapshot.canAct) 1.0f else 0.45f
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
    }

    private fun buildStatusText(snapshot: ControllerSnapshot): String = buildString {
        append("连接: ${snapshot.transportState} (${snapshot.transportDetail})\n")
        append("认证: ${if (snapshot.welcomeReceived) "已收到 welcome" else "未完成"}")
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

    companion object {
        private const val PREFS_NAME = "lm3_up_teleop_settings"
        private const val KEY_CLIENT_ID = "client_id"
        private const val KEY_ENDPOINT = "endpoint"
        private const val KEY_CLIENT_NAME = "client_name"
        private const val UNLOCK_HOLD_MS = 1_500L
    }
}
