import { BusinessError, systemDateTime } from '@kit.BasicServicesKit';
import { webSocket } from '@kit.NetworkKit';
import { NetworkPolicy } from './NetworkPolicy';
import {
  AckBody,
  AcquireBody,
  Angular3,
  AxisName,
  COMMAND_PERIOD_MS,
  COMMAND_RATE_HZ,
  ControlReleaseBody,
  ControlStatusBody,
  decodeEnvelope,
  encodeEnvelope,
  ErrorBody,
  GripperBody,
  HeartbeatBody,
  HelloBody,
  MAX_WATCHDOG_MS,
  OPERATOR_HOLD_MS,
  Pose6,
  ProtocolEnvelope,
  REQUESTED_LEASE_MS,
  RecordingStartBody,
  RecordingStatusBody,
  RecordingStopBody,
  ROBOT_STATE_STALE_MS,
  RobotStateBody,
  SafetyAck,
  SafetyEventBody,
  SpeedProfile,
  StopBody,
  Vector3,
  VelocityBody,
  WelcomeBody,
  validateAckBody,
  validateControlStatusBody,
  validateErrorBody,
  validateRecordingStatusBody,
  validateRobotStateBody,
  validateSafetyEventBody,
  validateWelcomeBody
} from '../model/TeleopProtocol';

export class TeleopSnapshot {
  connected: boolean = false;
  welcomed: boolean = false;
  foreground: boolean = true;
  connectionState: string = '未连接';
  sessionId: string = '';
  serverMode: string = '-';
  watchdogMs: number = 0;
  commandRateHz: number = 0;
  baseLocked: boolean = false;
  limitsText: string = '-';
  controlGranted: boolean = false;
  leaseIdDisplay: string = '-';
  leaseRemainingMs: number = 0;
  robotMode: string = '未知';
  estopReason: string = '-';
  motionReady: boolean = false;
  estop: boolean = true;
  errorCode: number = -1;
  watchdogOk: boolean = false;
  robotStateFresh: boolean = false;
  lastRobotStateAtMs: number = 0;
  tcpText: string = '-';
  jointText: string = '-';
  jointVelocityText: string = '-';
  gripperPositionPct: number = 0;
  recording: boolean = false;
  episodeId: string = '-';
  deadmanHeld: boolean = false;
  baseStationary: boolean = false;
  workspaceClear: boolean = false;
  estopAccessible: boolean = false;
  toolSecure: boolean = false;
  activeAxis: string = AxisName.NONE;
  speedName: string = '极慢';
  linearMps: number = 0.005;
  angularRps: number = 0.02;
  canAcquire: boolean = false;
  canCommand: boolean = false;
  lastEvent: string = '等待连接安全网关';
}

export type TeleopSnapshotListener = (snapshot: TeleopSnapshot) => void;

export class TeleopClient {
  private static instance: TeleopClient = new TeleopClient();
  private socket: webSocket.WebSocket | undefined = undefined;
  private listener: TeleopSnapshotListener | undefined = undefined;
  private connected: boolean = false;
  private welcomed: boolean = false;
  private foreground: boolean = true;
  private connectionState: string = '未连接';
  private clientId: string = `harmony-${Date.now()}`;
  private nextSeq: number = 0;
  private lastServerSeq: number = -1;
  private commandTimerId: number = -1;
  private connectTimerId: number = -1;
  private lastUiEmitAtUptimeMs: number = 0;
  private lastHeartbeatAtUptimeMs: number = 0;
  private lastMotionSentAtUptimeMs: number = 0;
  private sessionId: string = '';
  private serverMode: string = '-';
  private watchdogMs: number = 0;
  private commandRateHz: number = 0;
  private baseLocked: boolean = false;
  private limitsText: string = '-';
  private safetyAck: SafetyAck = new SafetyAck();
  private leaseId: string = '';
  private leaseExpiresAtMs: number = 0;
  private leaseDeadlineUptimeMs: number = 0;
  private robotMode: string = '未知';
  private estopReason: string = '-';
  private motionReady: boolean = false;
  private estop: boolean = true;
  private errorCode: number = -1;
  private watchdogOk: boolean = false;
  private lastRobotStateAtMs: number = 0;
  private lastRobotStateUptimeMs: number = 0;
  private tcp: Pose6 | undefined = undefined;
  private joints: Array<number> = [];
  private jointVelocities: Array<number> = [];
  private gripperPositionPct: number = 0;
  private recording: boolean = false;
  private episodeId: string = '-';
  private deadmanHeld: boolean = false;
  private deadmanTouchId: number = -1;
  private activeAxis: string = AxisName.NONE;
  private axisDirection: number = 0;
  private axisTouchId: number = -1;
  private speedProfiles: Array<SpeedProfile> = [
    new SpeedProfile('极慢', 0.005, 0.02),
    new SpeedProfile('低速', 0.015, 0.05),
    new SpeedProfile('调试', 0.03, 0.10)
  ];
  private speedIndex: number = 0;
  private lastEvent: string = '等待连接安全网关';

  static getInstance(): TeleopClient {
    return TeleopClient.instance;
  }

  setListener(listener: TeleopSnapshotListener | undefined): void {
    this.listener = listener;
    this.emitSnapshot();
  }

  getSnapshot(): TeleopSnapshot {
    return this.buildSnapshot();
  }

  connect(url: string, authToken: string): void {
    const validation = NetworkPolicy.validate(url);
    if (!validation.valid) {
      this.lastEvent = validation.error;
      this.emitSnapshot();
      return;
    }
    if (authToken.length === 0) {
      this.lastEvent = '共享 token 不能为空';
      this.emitSnapshot();
      return;
    }

    this.closeSocket('reconnect', true);
    this.resetConnectionState();
    this.connectionState = '正在连接';
    this.lastEvent = validation.warning.length > 0 ? validation.warning : '正在建立加密 WebSocket';
    this.emitSnapshot();

    const ws = webSocket.createWebSocket();
    this.socket = ws;
    this.attachSocketEvents(ws, authToken);
    this.connectTimerId = setTimeout(() => {
      if (this.socket === ws && !this.welcomed) {
        this.lastEvent = '连接或 welcome 超时，已失败关闭';
        this.closeSocket('welcome_timeout', true);
      }
    }, 5000);

    try {
      ws.connect(validation.normalizedUrl, (error: BusinessError, accepted: boolean) => {
        if (this.socket !== ws) {
          return;
        }
        if (error || !accepted) {
          this.lastEvent = error ? `连接请求失败：${error.code}` : '连接请求未被接受';
          this.closeSocket('connect_rejected', false);
        }
      });
    } catch (error) {
      this.lastEvent = `连接异常：${JSON.stringify(error)}`;
      this.closeSocket('connect_exception', false);
    }
  }

  disconnect(reason: string = 'operator_disconnect'): void {
    this.closeSocket(reason, true);
  }

  appForeground(): void {
    this.foreground = true;
    this.lastEvent = '应用回到前台；必须重新连接并长按解锁';
    this.emitSnapshot();
  }

  appBackground(): void {
    this.foreground = false;
    this.closeSocket('app_background', true);
  }

  updateSafety(safetyAck: SafetyAck): void {
    this.safetyAck = safetyAck;
    if (!this.safetyAck.allChecked() && this.leaseId.length > 0) {
      this.failClosed('safety_check_revoked', true);
      return;
    }
    this.lastEvent = this.safetyAck.allChecked() ? '安全检查已全部确认' : '安全检查未完成';
    this.emitSnapshot();
  }

  requestControl(): void {
    if (!this.canAcquireControl()) {
      this.lastEvent = '当前状态不满足控制权申请条件';
      this.emitSnapshot();
      return;
    }
    const copy = new SafetyAck();
    copy.base_stationary = this.safetyAck.base_stationary;
    copy.workspace_clear = this.safetyAck.workspace_clear;
    copy.estop_accessible = this.safetyAck.estop_accessible;
    copy.tool_secure = this.safetyAck.tool_secure;
    this.sendEnvelope('control.acquire', new AcquireBody(copy));
    this.lastEvent = `已完成 ${OPERATOR_HOLD_MS} ms 长按，等待 control.status`;
    this.emitSnapshot();
  }

  setSpeedProfile(index: number): void {
    if (index < 0 || index >= this.speedProfiles.length) {
      return;
    }
    if (this.deadmanHeld || this.activeAxis !== AxisName.NONE) {
      this.failClosed('speed_changed_during_motion', false);
    }
    this.speedIndex = index;
    this.lastEvent = `速度档切换为 ${this.speedProfiles[this.speedIndex].name}`;
    this.emitSnapshot();
  }

  setDeadman(held: boolean, touchId: number): void {
    if (!held) {
      if (touchId >= 0 && this.deadmanTouchId >= 0 && touchId !== this.deadmanTouchId) {
        return;
      }
      if (this.deadmanHeld || this.activeAxis !== AxisName.NONE) {
        this.sendStop('deadman_released');
      }
      this.deadmanHeld = false;
      this.deadmanTouchId = -1;
      this.activeAxis = AxisName.NONE;
      this.axisDirection = 0;
      this.axisTouchId = -1;
      this.lastEvent = 'deadman 已松开，已发送 stop';
      this.emitSnapshot();
      return;
    }
    if (touchId < 0 || (this.deadmanTouchId >= 0 && touchId !== this.deadmanTouchId)) {
      this.lastEvent = 'deadman 已由另一个触点持有';
      this.emitSnapshot();
      return;
    }
    if (!this.hasSafeLease(false)) {
      this.deadmanHeld = false;
      this.deadmanTouchId = -1;
      this.lastEvent = '无法按下 deadman：控制租约或安全状态无效';
      this.emitSnapshot();
      return;
    }
    this.deadmanHeld = true;
    this.deadmanTouchId = touchId;
    this.lastEvent = 'deadman 按住；继续按住并选择单一轴向';
    this.emitSnapshot();
  }

  pressAxis(axis: string, direction: number, touchId: number): void {
    if (!this.deadmanHeld || !this.hasSafeLease(false)) {
      this.lastEvent = '轴向命令被拒绝：必须先按住 deadman';
      this.emitSnapshot();
      return;
    }
    if (touchId < 0 || axis === AxisName.NONE || (direction !== -1 && direction !== 1)) {
      return;
    }
    if (this.axisTouchId >= 0 && touchId !== this.axisTouchId) {
      this.lastEvent = '已有轴键触点，拒绝同时按下第二个轴键';
      this.emitSnapshot();
      return;
    }
    if (this.activeAxis !== AxisName.NONE &&
      (this.activeAxis !== axis || this.axisDirection !== direction)) {
      this.lastEvent = '已有活动轴，必须先松开后再切换';
      this.emitSnapshot();
      return;
    }
    this.activeAxis = axis;
    this.axisDirection = direction;
    this.axisTouchId = touchId;
    this.lastEvent = `按住 ${axis}${direction > 0 ? '+' : '-'}，20 Hz 发送`;
    this.emitSnapshot();
  }

  releaseAxis(axis: string, touchId: number): void {
    if (touchId >= 0 && this.axisTouchId >= 0 && touchId !== this.axisTouchId) {
      return;
    }
    if (this.activeAxis === axis || touchId < 0) {
      this.sendStop('axis_released');
      this.activeAxis = AxisName.NONE;
      this.axisDirection = 0;
      this.axisTouchId = -1;
      this.lastEvent = `已松开 ${axis}，已发送 stop`;
      this.emitSnapshot();
    }
  }

  setGripper(positionPct: number): void {
    if (!this.deadmanHeld || this.activeAxis !== AxisName.NONE || !this.hasSafeLease(false)) {
      this.lastEvent = '夹爪命令被拒绝：必须按住 deadman 且租约有效';
      this.emitSnapshot();
      return;
    }
    const bounded = Math.max(0, Math.min(100, Math.round(positionPct)));
    this.sendEnvelope('gripper.set', new GripperBody(this.leaseId, bounded));
    this.lastEvent = `夹爪目标已发送：${bounded}%`;
    this.emitSnapshot();
  }

  startRecording(task: string, episodeId: string, cameras: Array<string>): void {
    const allowMoving = this.deadmanHeld && this.activeAxis !== AxisName.NONE;
    if (!this.hasSafeLease(allowMoving)) {
      this.lastEvent = '录制开始被拒绝：需要有效控制租约和安全状态';
      this.emitSnapshot();
      return;
    }
    const cleanTask = task.trim();
    if (cleanTask.length === 0) {
      this.lastEvent = '录制任务描述不能为空';
      this.emitSnapshot();
      return;
    }
    if (cameras.length === 0) {
      this.lastEvent = '至少配置一路相机，例如 camera_wrist';
      this.emitSnapshot();
      return;
    }
    this.sendEnvelope('recording.start', new RecordingStartBody(
      this.leaseId,
      cleanTask,
      episodeId.trim(),
      cameras
    ));
    this.lastEvent = '已请求开始录制，等待 recording.status';
    this.emitSnapshot();
  }

  stopRecording(reason: string = 'operator_stop'): void {
    if (this.leaseId.length === 0 || !this.connected || !this.welcomed) {
      this.recording = false;
      this.lastEvent = '本地录制状态已清除；当前无可用连接';
      this.emitSnapshot();
      return;
    }
    this.sendEnvelope('recording.stop', new RecordingStopBody(this.leaseId, reason));
    this.lastEvent = '已请求停止录制';
    this.emitSnapshot();
  }

  emergencyStopFromUi(): void {
    this.failClosed('operator_stop_button', true);
  }

  private attachSocketEvents(ws: webSocket.WebSocket, authToken: string): void {
    let oneShotToken = authToken;
    ws.on('open', (error: BusinessError, value: Object) => {
      if (this.socket !== ws) {
        return;
      }
      if (error) {
        this.lastEvent = `WebSocket open 失败：${error.code}`;
        this.closeSocket('open_error', false);
        return;
      }
      this.connected = true;
      this.connectionState = '已连接，等待 welcome';
      this.nextSeq = 0;
      this.lastServerSeq = -1;
      this.sendEnvelope('session.hello', new HelloBody(this.clientId, oneShotToken));
      oneShotToken = '';
      this.lastEvent = 'hello 已发送；token 不写入本地存储';
      this.emitSnapshot();
    });

    ws.on('message', (error: BusinessError, value: string | ArrayBuffer) => {
      if (this.socket !== ws) {
        return;
      }
      if (error) {
        this.failReceiveTransport(`消息接收失败：${error.code}`);
        return;
      }
      if (typeof value !== 'string') {
        this.failClosed('binary_message_rejected', true);
        return;
      }
      this.handleMessage(value);
    });

    ws.on('close', (error: BusinessError, value: webSocket.CloseResult) => {
      if (this.socket !== ws) {
        return;
      }
      const reason = error ? `close_error_${error.code}` : `socket_closed_${value.code}`;
      this.markDisconnected(reason);
    });

    ws.on('error', (error: BusinessError) => {
      if (this.socket !== ws) {
        return;
      }
      this.failReceiveTransport(`WebSocket 错误：${error.code}`);
    });
  }

  private handleMessage(text: string): void {
    try {
      this.handleMessageStrict(text);
    } catch (error) {
      this.failClosed('protocol_handler_exception', true);
    }
  }

  private handleMessageStrict(text: string): void {
    const envelope = decodeEnvelope(text);
    if (envelope === undefined) {
      this.failClosed('invalid_protocol_envelope', true);
      return;
    }
    if (this.lastServerSeq < 0) {
      if (envelope.seq !== 0 || envelope.type !== 'session.welcome') {
        this.failClosed('first_server_message_must_be_session_welcome_seq_0', true);
        return;
      }
    } else if (envelope.seq <= this.lastServerSeq) {
      this.failClosed('server_seq_not_strictly_increasing', true);
      return;
    } else if (!this.welcomed || envelope.type === 'session.welcome') {
      this.failClosed('invalid_server_session_state', true);
      return;
    }
    this.lastServerSeq = envelope.seq;

    if (envelope.type === 'session.welcome') {
      const body = validateWelcomeBody(envelope.body);
      if (body === undefined) {
        this.failClosed('invalid_session_welcome_body', true);
        return;
      }
      this.handleWelcome(body);
    } else if (envelope.type === 'control.status') {
      const body = validateControlStatusBody(envelope.body);
      if (body === undefined) {
        this.failClosed('invalid_control_status_body', true);
        return;
      }
      this.handleControlStatus(envelope, body);
    } else if (envelope.type === 'robot.state') {
      const body = validateRobotStateBody(envelope.body);
      if (body === undefined) {
        this.failClosed('invalid_robot_state_body', true);
        return;
      }
      this.handleRobotState(body);
    } else if (envelope.type === 'recording.status') {
      const body = validateRecordingStatusBody(envelope.body);
      if (body === undefined) {
        this.failClosed('invalid_recording_status_body', true);
        return;
      }
      this.handleRecordingStatus(body);
    } else if (envelope.type === 'ack') {
      const body = validateAckBody(envelope.body);
      if (body === undefined) {
        this.failClosed('invalid_ack_body', true);
        return;
      }
      const ackType = body.ack_type;
      const detail = body.detail ? `：${body.detail}` : '';
      this.lastEvent = `${body.accepted ? 'ACK' : '拒绝'} ${ackType}${body.clamped ? '（服务端已限幅）' : ''}${detail}`;
      if (!body.accepted && (ackType.startsWith('motion.') || ackType === 'gripper.set')) {
        this.failClosed('command_rejected', true);
      } else {
        this.emitSnapshot();
      }
    } else if (envelope.type === 'error') {
      const body = validateErrorBody(envelope.body);
      if (body === undefined) {
        this.failClosed('invalid_error_body', true);
        return;
      }
      const errorText = `server_error:${body.code}:${body.message}`;
      if (body.recoverable) {
        this.lastEvent = errorText;
        this.emitSnapshot();
      } else {
        this.failClosed(errorText, true);
      }
    } else if (envelope.type === 'safety.event') {
      const body = validateSafetyEventBody(envelope.body);
      if (body === undefined) {
        this.failClosed('invalid_safety_event_body', true);
        return;
      }
      const safetyText = `safety_event:${body.code}:${body.message}`;
      if (body.action === 'stop') {
        this.failClosed(safetyText, true);
      } else {
        this.lastEvent = safetyText;
        this.emitSnapshot();
      }
    } else {
      this.failClosed(`unsupported_server_message_type:${envelope.type}`, true);
    }
  }

  private handleWelcome(body: WelcomeBody): void {
    if (body.command_rate_hz !== COMMAND_RATE_HZ || body.watchdog_ms <= 0 || body.watchdog_ms > MAX_WATCHDOG_MS) {
      this.lastEvent = `服务端安全参数不兼容：${body.command_rate_hz} Hz / ${body.watchdog_ms} ms`;
      this.closeSocket('unsafe_welcome_parameters', true);
      return;
    }
    this.clearConnectTimer();
    this.welcomed = true;
    this.connectionState = '协议已就绪';
    this.sessionId = body.session_id;
    this.serverMode = body.mode;
    this.watchdogMs = body.watchdog_ms;
    this.commandRateHz = body.command_rate_hz;
    this.baseLocked = body.base_locked;
    const rawLimits = JSON.stringify(body.limits);
    this.limitsText = rawLimits.length > 180 ? `${rawLimits.substring(0, 180)}…` : rawLimits;
    this.lastEvent = this.baseLocked ? 'welcome 已验证；等待 robot.state' : 'welcome 表示底盘未锁定，禁止申请控制权';
    this.startCommandLoop();
    this.emitSnapshot();
  }

  private handleControlStatus(envelope: ProtocolEnvelope, body: ControlStatusBody): void {
    const nowUptimeMs = this.monotonicNowMs();
    const serverRemainingMs = body.expires_at_ms === undefined ? 0 : body.expires_at_ms - envelope.sent_at_ms;
    if (body.granted && body.lease_id && body.owner_client_id === this.clientId &&
      body.expires_at_ms !== undefined && serverRemainingMs > 0 && nowUptimeMs >= 0) {
      this.leaseId = body.lease_id;
      this.leaseExpiresAtMs = body.expires_at_ms;
      this.leaseDeadlineUptimeMs = nowUptimeMs + Math.min(REQUESTED_LEASE_MS, serverRemainingMs);
      const allowMoving = this.deadmanHeld && this.activeAxis !== AxisName.NONE;
      if (!this.hasSafeLease(allowMoving)) {
        this.failClosed('control_granted_while_gate_closed', true);
        return;
      }
      this.lastEvent = '控制权已授予，所有动作仍受 deadman 和安全状态约束';
    } else {
      this.sendStop('control_not_granted');
      this.clearLocalControl();
      this.lastEvent = `控制权未授予：${body.reason ? body.reason : '未说明原因'}`;
    }
    this.emitSnapshot();
  }

  private handleRobotState(body: RobotStateBody): void {
    const nowUptimeMs = this.monotonicNowMs();
    if (nowUptimeMs < 0) {
      this.closeSocket('monotonic_clock_unavailable', true);
      return;
    }
    this.robotMode = body.robot_state.trim().toUpperCase();
    this.estopReason = body.estop_reason.length > 0 ? body.estop_reason : '-';
    this.watchdogOk = body.watchdog_ok;
    this.estop = this.hasEstop(this.estopReason, this.robotMode);
    this.motionReady = this.watchdogOk && !this.estop;
    this.errorCode = this.estop ? 1 : 0;
    this.baseLocked = body.base_locked;
    this.tcp = body.tcp_pose;
    this.joints = body.joint_position_rad;
    this.jointVelocities = body.joint_velocity_rad_s;
    this.gripperPositionPct = body.gripper_pct;
    this.recording = body.recording;
    this.lastRobotStateAtMs = Date.now();
    this.lastRobotStateUptimeMs = nowUptimeMs;

    const allowMoving = this.deadmanHeld && this.activeAxis !== AxisName.NONE;
    if (this.leaseId.length > 0 &&
      (!this.baseSafetyReady() || !this.isLeaseRobotModeSafe(allowMoving))) {
      this.failClosed('robot_state_became_unsafe', true);
      return;
    }
    this.emitSnapshot();
  }

  private handleRecordingStatus(body: RecordingStatusBody): void {
    this.recording = body.recording;
    this.episodeId = body.episode_id.length > 0 ? body.episode_id : '-';
    this.lastEvent = this.recording ? `录制中：${this.episodeId}` : `录制已停止：${body.reason ? body.reason : 'server'}`;
    this.emitSnapshot();
  }

  private startCommandLoop(): void {
    this.stopCommandLoop();
    this.commandTimerId = setInterval(() => {
      this.commandTick();
    }, COMMAND_PERIOD_MS);
  }

  private stopCommandLoop(): void {
    if (this.commandTimerId >= 0) {
      clearInterval(this.commandTimerId);
      this.commandTimerId = -1;
    }
  }

  private commandTick(): void {
    if (!this.connected || !this.welcomed || !this.foreground) {
      return;
    }
    const nowUptimeMs = this.monotonicNowMs();
    if (nowUptimeMs < 0) {
      this.closeSocket('monotonic_clock_unavailable', true);
      return;
    }
    const moving = this.deadmanHeld && this.activeAxis !== AxisName.NONE;
    if (this.leaseId.length > 0 && !this.hasSafeLease(moving)) {
      this.failClosed('lease_or_state_expired', true);
      return;
    }

    if (moving && this.hasSafeLease(true) &&
      nowUptimeMs - this.lastMotionSentAtUptimeMs >= COMMAND_PERIOD_MS) {
      const linear = new Vector3();
      const angular = new Angular3();
      const profile = this.speedProfiles[this.speedIndex];
      if (this.activeAxis === AxisName.X) {
        linear.x = profile.linearMps * this.axisDirection;
      } else if (this.activeAxis === AxisName.Y) {
        linear.y = profile.linearMps * this.axisDirection;
      } else if (this.activeAxis === AxisName.Z) {
        linear.z = profile.linearMps * this.axisDirection;
      } else if (this.activeAxis === AxisName.RX) {
        angular.rx = profile.angularRps * this.axisDirection;
      } else if (this.activeAxis === AxisName.RY) {
        angular.ry = profile.angularRps * this.axisDirection;
      } else if (this.activeAxis === AxisName.RZ) {
        angular.rz = profile.angularRps * this.axisDirection;
      }
      this.sendEnvelope('motion.cartesian_velocity', new VelocityBody(this.leaseId, linear, angular));
      this.lastMotionSentAtUptimeMs = nowUptimeMs;
    } else if (!moving && nowUptimeMs - this.lastHeartbeatAtUptimeMs >= 250) {
      this.sendEnvelope('heartbeat', new HeartbeatBody(this.leaseId));
      this.lastHeartbeatAtUptimeMs = nowUptimeMs;
    }

    if (nowUptimeMs - this.lastUiEmitAtUptimeMs >= 250) {
      this.emitSnapshot();
    }
  }

  private sendStop(reason: string): void {
    if (this.connected && this.welcomed) {
      this.sendEnvelope('motion.stop', new StopBody(this.leaseId, reason));
    }
  }

  private sendEnvelope(type: string, body: Object): boolean {
    if (!this.connected || this.socket === undefined) {
      return false;
    }
    const sequence = this.nextSeq;
    this.nextSeq += 1;
    const payload = encodeEnvelope(type, sequence, body);
    const ws = this.socket;
    try {
      ws.send(payload, (error: BusinessError, sent: boolean) => {
        if (this.socket !== ws) {
          return;
        }
        if (error || !sent) {
          this.failSendTransport(error ? `发送失败：${error.code}` : '发送失败：未确认');
        }
      });
      return true;
    } catch (error) {
      this.failSendTransport(`发送异常：${JSON.stringify(error)}`);
      return false;
    }
  }

  private baseSafetyReady(): boolean {
    return this.connected && this.welcomed && this.foreground && this.baseLocked &&
      this.watchdogOk && this.motionReady && !this.estop && this.errorCode === 0 && this.isRobotStateFresh() &&
      this.safetyAck.allChecked() && this.watchdogMs > 0 && this.watchdogMs <= MAX_WATCHDOG_MS &&
      this.commandRateHz === COMMAND_RATE_HZ;
  }

  private canAcquireControl(): boolean {
    return this.baseSafetyReady() && this.robotMode === 'IDLE' && this.leaseId.length === 0;
  }

  private hasSafeLease(allowMoving: boolean): boolean {
    const nowUptimeMs = this.monotonicNowMs();
    return nowUptimeMs >= 0 && this.baseSafetyReady() && this.isLeaseRobotModeSafe(allowMoving) &&
      this.leaseId.length > 0 && this.leaseDeadlineUptimeMs > nowUptimeMs;
  }

  private isRobotStateFresh(): boolean {
    const nowUptimeMs = this.monotonicNowMs();
    const ageMs = nowUptimeMs - this.lastRobotStateUptimeMs;
    return nowUptimeMs >= 0 && this.lastRobotStateUptimeMs > 0 && ageMs >= 0 && ageMs <= ROBOT_STATE_STALE_MS;
  }

  private isLeaseRobotModeSafe(allowMoving: boolean): boolean {
    return this.robotMode === 'IDLE' ||
      (allowMoving && (this.robotMode === 'MOVING' || this.robotMode === 'RUNNING'));
  }

  private monotonicNowMs(): number {
    try {
      return systemDateTime.getUptime(systemDateTime.TimeType.STARTUP);
    } catch (error) {
      return -1;
    }
  }

  private failClosed(reason: string, clearLease: boolean): void {
    const leaseToRelease = this.leaseId;
    if (this.recording && this.connected && this.welcomed && this.leaseId.length > 0) {
      this.sendEnvelope('recording.stop', new RecordingStopBody(this.leaseId, reason));
    }
    this.sendStop(reason);
    if (clearLease && leaseToRelease.length > 0 && this.connected && this.welcomed) {
      this.sendEnvelope('control.release', new ControlReleaseBody(leaseToRelease));
    }
    this.deadmanHeld = false;
    this.deadmanTouchId = -1;
    this.activeAxis = AxisName.NONE;
    this.axisDirection = 0;
    this.axisTouchId = -1;
    this.recording = false;
    if (clearLease) {
      this.leaseId = '';
      this.leaseExpiresAtMs = 0;
      this.leaseDeadlineUptimeMs = 0;
    }
    this.lastEvent = `失败关闭：${reason}`;
    this.emitSnapshot();
  }

  private failReceiveTransport(reason: string): void {
    this.lastEvent = `接收失败关闭：${reason}`;
    this.closeSocket('receive_transport_failure', true);
  }

  private failSendTransport(reason: string): void {
    this.lastEvent = `通信失败关闭：${reason}`;
    this.closeSocket('send_transport_failure', false);
  }

  private closeSocket(reason: string, sendStopFirst: boolean): void {
    const ws = this.socket;
    const leaseToRelease = this.leaseId;
    if (sendStopFirst) {
      if (this.recording && this.connected && this.welcomed && this.leaseId.length > 0) {
        this.sendEnvelope('recording.stop', new RecordingStopBody(this.leaseId, reason));
      }
      this.sendStop(reason);
      if (leaseToRelease.length > 0 && this.connected && this.welcomed) {
        this.sendEnvelope('control.release', new ControlReleaseBody(leaseToRelease));
      }
    }
    this.stopCommandLoop();
    this.clearConnectTimer();
    this.socket = undefined;
    this.connected = false;
    this.welcomed = false;
    this.connectionState = '未连接';
    this.clearLocalControl();
    this.safetyAck = new SafetyAck();
    this.recording = false;
    this.lastEvent = `连接已关闭：${reason}`;
    this.emitSnapshot();

    if (ws !== undefined) {
      try {
        ws.close({ code: 1000, reason: reason }, (error: BusinessError, closed: boolean) => {
          if (error || !closed) {
            return;
          }
        });
      } catch (error) {
        return;
      }
    }
  }

  private markDisconnected(reason: string): void {
    this.stopCommandLoop();
    this.clearConnectTimer();
    this.socket = undefined;
    this.connected = false;
    this.welcomed = false;
    this.connectionState = '连接已断开';
    this.clearLocalControl();
    this.safetyAck = new SafetyAck();
    this.recording = false;
    this.lastEvent = `断连失败关闭：${reason}；服务端必须按 socket 关闭/300 ms 看门狗停止`;
    this.emitSnapshot();
  }

  private clearLocalControl(): void {
    this.leaseId = '';
    this.leaseExpiresAtMs = 0;
    this.leaseDeadlineUptimeMs = 0;
    this.deadmanHeld = false;
    this.deadmanTouchId = -1;
    this.activeAxis = AxisName.NONE;
    this.axisDirection = 0;
    this.axisTouchId = -1;
  }

  private resetConnectionState(): void {
    this.connected = false;
    this.welcomed = false;
    this.sessionId = '';
    this.serverMode = '-';
    this.watchdogMs = 0;
    this.commandRateHz = 0;
    this.baseLocked = false;
    this.limitsText = '-';
    this.robotMode = '未知';
    this.estopReason = '-';
    this.motionReady = false;
    this.estop = true;
    this.errorCode = -1;
    this.watchdogOk = false;
    this.lastRobotStateAtMs = 0;
    this.lastRobotStateUptimeMs = 0;
    this.tcp = undefined;
    this.joints = [];
    this.jointVelocities = [];
    this.gripperPositionPct = 0;
    this.lastHeartbeatAtUptimeMs = 0;
    this.lastMotionSentAtUptimeMs = 0;
    this.recording = false;
    this.episodeId = '-';
    this.safetyAck = new SafetyAck();
    this.clearLocalControl();
  }

  private clearConnectTimer(): void {
    if (this.connectTimerId >= 0) {
      clearTimeout(this.connectTimerId);
      this.connectTimerId = -1;
    }
  }

  private emitSnapshot(): void {
    const nowUptimeMs = this.monotonicNowMs();
    this.lastUiEmitAtUptimeMs = nowUptimeMs >= 0 ? nowUptimeMs : 0;
    if (this.listener !== undefined) {
      this.listener(this.buildSnapshot());
    }
  }

  private buildSnapshot(): TeleopSnapshot {
    const snapshot = new TeleopSnapshot();
    const profile = this.speedProfiles[this.speedIndex];
    const nowUptimeMs = this.monotonicNowMs();
    const allowMoving = this.deadmanHeld && this.activeAxis !== AxisName.NONE;
    snapshot.connected = this.connected;
    snapshot.welcomed = this.welcomed;
    snapshot.foreground = this.foreground;
    snapshot.connectionState = this.connectionState;
    snapshot.sessionId = this.sessionId.length > 0 ? this.sessionId : '-';
    snapshot.serverMode = this.serverMode;
    snapshot.watchdogMs = this.watchdogMs;
    snapshot.commandRateHz = this.commandRateHz;
    snapshot.baseLocked = this.baseLocked;
    snapshot.limitsText = this.limitsText;
    snapshot.controlGranted = this.hasSafeLease(allowMoving);
    snapshot.leaseIdDisplay = this.maskIdentifier(this.leaseId);
    snapshot.leaseRemainingMs = nowUptimeMs < 0 ? 0 : Math.max(0, this.leaseDeadlineUptimeMs - nowUptimeMs);
    snapshot.robotMode = this.robotMode;
    snapshot.estopReason = this.estopReason;
    snapshot.motionReady = this.motionReady;
    snapshot.estop = this.estop;
    snapshot.errorCode = this.errorCode;
    snapshot.watchdogOk = this.watchdogOk;
    snapshot.robotStateFresh = this.isRobotStateFresh();
    snapshot.lastRobotStateAtMs = this.lastRobotStateAtMs;
    snapshot.tcpText = this.formatTcp();
    snapshot.jointText = this.formatJoints();
    snapshot.jointVelocityText = this.formatJointVelocities();
    snapshot.gripperPositionPct = this.gripperPositionPct;
    snapshot.recording = this.recording;
    snapshot.episodeId = this.episodeId;
    snapshot.deadmanHeld = this.deadmanHeld;
    snapshot.baseStationary = this.safetyAck.base_stationary;
    snapshot.workspaceClear = this.safetyAck.workspace_clear;
    snapshot.estopAccessible = this.safetyAck.estop_accessible;
    snapshot.toolSecure = this.safetyAck.tool_secure;
    snapshot.activeAxis = this.activeAxis;
    snapshot.speedName = profile.name;
    snapshot.linearMps = profile.linearMps;
    snapshot.angularRps = profile.angularRps;
    snapshot.canAcquire = this.canAcquireControl();
    snapshot.canCommand = this.hasSafeLease(allowMoving);
    snapshot.lastEvent = this.lastEvent;
    return snapshot;
  }

  private formatTcp(): string {
    if (this.tcp === undefined) {
      return '-';
    }
    return `x ${this.tcp.x.toFixed(4)}  y ${this.tcp.y.toFixed(4)}  z ${this.tcp.z.toFixed(4)}\n` +
      `rx ${this.tcp.rx.toFixed(3)}  ry ${this.tcp.ry.toFixed(3)}  rz ${this.tcp.rz.toFixed(3)}`;
  }

  private formatJoints(): string {
    if (this.joints.length === 0) {
      return '-';
    }
    const values: Array<string> = [];
    for (let index = 0; index < this.joints.length; index += 1) {
      values.push(`J${index + 1} ${this.joints[index].toFixed(3)}`);
    }
    return values.join('  ');
  }

  private formatJointVelocities(): string {
    if (this.jointVelocities.length === 0) {
      return '-';
    }
    const values: Array<string> = [];
    for (let index = 0; index < this.jointVelocities.length; index += 1) {
      values.push(`J${index + 1} ${this.jointVelocities[index].toFixed(3)}`);
    }
    return values.join('  ');
  }

  private hasEstop(estopReason: string, robotState: string): boolean {
    const reason = estopReason.trim().toLowerCase();
    const explicitReason = reason.length > 0 && reason !== '-' && reason !== 'none' && reason !== 'ok' &&
      reason !== '0' && reason !== 'false';
    const state = robotState.toLowerCase();
    return explicitReason || state.indexOf('estop') >= 0 || state.indexOf('fault') >= 0 || state.indexOf('error') >= 0;
  }

  private maskIdentifier(value: string): string {
    if (value.length === 0) {
      return '-';
    }
    if (value.length <= 8) {
      return value;
    }
    return `${value.substring(0, 4)}…${value.substring(value.length - 4)}`;
  }
}
