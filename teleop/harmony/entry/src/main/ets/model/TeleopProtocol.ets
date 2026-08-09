export const TELEOP_PROTOCOL: string = 'lm3-teleop.v1';
export const COMMAND_RATE_HZ: number = 20;
export const COMMAND_PERIOD_MS: number = 50;
export const VELOCITY_DURATION_MS: number = 100;
export const REQUESTED_LEASE_MS: number = 2000;
export const OPERATOR_HOLD_MS: number = 1500;
export const MAX_WATCHDOG_MS: number = 300;
export const ROBOT_STATE_STALE_MS: number = 1000;
export const POSE_SAMPLE_PERIOD_MS: number = COMMAND_PERIOD_MS;
export const POSE_SENSOR_STALE_MS: number = 150;
export const POSE_MAX_SAMPLE_GAP_MS: number = 250;
export const POSE_MIN_SENT_INTERVAL_MS: number = 20;
export const POSE_MAX_SENT_INTERVAL_MS: number = 150;
export const POSE_MAX_INPUT_ANGULAR_RPS: number = 6;
export const POSE_MAX_SENSOR_JUMP_RAD: number = 0.6108652381980153;
export const POSE_MAX_RELATIVE_ANGLE_RAD: number = 0.5235987755982988;
export const POSE_MAX_COMMAND_DELTA_RAD: number = 0.20943951023931953;
export const POSE_MIN_ACCURACY: number = 2;
export const POSE_TCP_RX_FROM_PHONE_Y_SIGN: number = 1;
export const POSE_TCP_RY_FROM_PHONE_X_SIGN: number = -1;
export const POSE_TCP_RZ_FROM_PHONE_Z_SIGN: number = 1;

export class AxisName {
  static readonly NONE: string = 'none';
  static readonly X: string = 'x';
  static readonly Y: string = 'y';
  static readonly Z: string = 'z';
  static readonly RX: string = 'rx';
  static readonly RY: string = 'ry';
  static readonly RZ: string = 'rz';
}

export class Vector3 {
  x: number = 0;
  y: number = 0;
  z: number = 0;
}

export class Angular3 {
  rx: number = 0;
  ry: number = 0;
  rz: number = 0;
}

export class Quaternion4 {
  x: number = 0;
  y: number = 0;
  z: number = 0;
  w: number = 1;
}

export class Pose6 {
  x: number = 0;
  y: number = 0;
  z: number = 0;
  rx: number = 0;
  ry: number = 0;
  rz: number = 0;
}

export class SpeedProfile {
  name: string;
  linearMps: number;
  angularRps: number;

  constructor(name: string, linearMps: number, angularRps: number) {
    this.name = name;
    this.linearMps = linearMps;
    this.angularRps = angularRps;
  }
}

export class SafetyAck {
  base_stationary: boolean = false;
  workspace_clear: boolean = false;
  estop_accessible: boolean = false;
  tool_secure: boolean = false;

  allChecked(): boolean {
    return this.base_stationary && this.workspace_clear && this.estop_accessible && this.tool_secure;
  }
}

export class HelloBody {
  client_id: string;
  client_name: string;
  platform: string = 'HarmonyOS';
  app_version: string = '1.0.0';
  capabilities: Array<string> = [
    'cartesian_velocity',
    'gripper',
    'recording',
    'pose_sample'
  ];

  constructor(clientId: string, clientName: string) {
    this.client_id = clientId;
    this.client_name = clientName;
  }
}

export class AcquireBody {
  requested_lease_ms: number = REQUESTED_LEASE_MS;
  operator_hold_ms: number = OPERATOR_HOLD_MS;
  safety_ack: SafetyAck;

  constructor(safetyAck: SafetyAck) {
    this.safety_ack = safetyAck;
  }
}

export class HeartbeatBody {
  lease_id?: string;
  deadman: boolean = false;

  constructor(leaseId: string) {
    if (leaseId.length > 0) {
      this.lease_id = leaseId;
    }
  }
}

export class ControlReleaseBody {
  lease_id: string;

  constructor(leaseId: string) {
    this.lease_id = leaseId;
  }
}

export class VelocityBody {
  lease_id: string;
  deadman: boolean = true;
  frame: string = 'base';
  linear_mps: Vector3;
  angular_rps: Angular3;
  duration_ms: number = VELOCITY_DURATION_MS;

  constructor(leaseId: string, linear: Vector3, angular: Angular3) {
    this.lease_id = leaseId;
    this.linear_mps = linear;
    this.angular_rps = angular;
  }
}

export class StopBody {
  lease_id?: string;
  reason: string;

  constructor(leaseId: string, reason: string) {
    if (leaseId.length > 0) {
      this.lease_id = leaseId;
    }
    this.reason = reason;
  }
}

export class GripperBody {
  lease_id: string;
  deadman: boolean = true;
  position_pct: number;

  constructor(leaseId: string, positionPct: number) {
    this.lease_id = leaseId;
    this.position_pct = positionPct;
  }
}

export class PoseSampleBody {
  lease_id: string;
  deadman: boolean = true;
  frame: string = 'phone_calibrated';
  mapping: string = 'tcp_orientation';
  calibration_id: string;
  sensor_timestamp_ms: number;
  tracking_state: string = 'tracking';
  confidence: number;
  angular_delta_rad: Angular3;

  constructor(
    leaseId: string,
    calibrationId: string,
    sensorTimestampMs: number,
    confidence: number,
    angularDeltaRad: Angular3
  ) {
    this.lease_id = leaseId;
    this.calibration_id = calibrationId;
    this.sensor_timestamp_ms = sensorTimestampMs;
    this.confidence = confidence;
    this.angular_delta_rad = angularDeltaRad;
  }
}

export class RecordingStartBody {
  lease_id: string;
  task: string;
  episode_id?: string;
  cameras: Array<string>;

  constructor(leaseId: string, task: string, episodeId: string, cameras: Array<string>) {
    this.lease_id = leaseId;
    this.task = task;
    if (episodeId.length > 0) {
      this.episode_id = episodeId;
    }
    this.cameras = cameras;
  }
}

export class RecordingStopBody {
  lease_id: string;
  reason: string;

  constructor(leaseId: string, reason: string) {
    this.lease_id = leaseId;
    this.reason = reason;
  }
}

export class ProtocolEnvelope {
  protocol: string = TELEOP_PROTOCOL;
  type: string;
  seq: number;
  sent_at_ms: number;
  body: Object;

  constructor(type: string, seq: number, sentAtMs: number, body: Object) {
    this.type = type;
    this.seq = seq;
    this.sent_at_ms = sentAtMs;
    this.body = body;
  }
}

export interface WelcomeBody {
  session_id: string;
  server_time_ms: number;
  mode: string;
  watchdog_ms: number;
  command_rate_hz: number;
  limits: Object;
  base_locked: boolean;
}

export interface ControlStatusBody {
  granted: boolean;
  lease_id?: string;
  owner_client_id?: string;
  expires_at_ms?: number;
  reason?: string;
}

export interface RobotStateBody {
  robot_state: string;
  estop_reason: string;
  joint_position_rad: Array<number>;
  joint_velocity_rad_s: Array<number>;
  tcp_pose: Pose6;
  gripper_pct: number;
  base_locked: boolean;
  watchdog_ok: boolean;
  recording: boolean;
}

export interface RecordingStatusBody {
  recording: boolean;
  episode_id: string;
  frame_count: number;
  started_at_ms: number;
  path: string;
  reason?: string;
}

export interface AckBody {
  ack_seq: number;
  ack_type: string;
  accepted: boolean;
  clamped?: boolean;
  detail?: string;
}

export interface ErrorBody {
  ack_seq?: number;
  code: string;
  message: string;
  recoverable: boolean;
}

export interface SafetyEventBody {
  severity: string;
  code: string;
  message: string;
  action: string;
}

function isBodyObject(value: Object | null): boolean {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function isFiniteNumber(value: number): boolean {
  return typeof value === 'number' && Number.isFinite(value);
}

function isInteger(value: number): boolean {
  return isFiniteNumber(value) && Number.isInteger(value);
}

function isNonEmptyString(value: string): boolean {
  return typeof value === 'string' && value.length > 0;
}

function isFiniteNumberArray(value: Array<number>, expectedLength: number): boolean {
  if (!Array.isArray(value) || value.length !== expectedLength) {
    return false;
  }
  for (let index = 0; index < value.length; index += 1) {
    if (!isFiniteNumber(value[index])) {
      return false;
    }
  }
  return true;
}

function isFinitePose(value: Pose6): boolean {
  if (!isBodyObject(value)) {
    return false;
  }
  return isFiniteNumber(value.x) && isFiniteNumber(value.y) && isFiniteNumber(value.z) &&
    isFiniteNumber(value.rx) && isFiniteNumber(value.ry) && isFiniteNumber(value.rz);
}

export function validateWelcomeBody(body: Object): WelcomeBody | undefined {
  if (!isBodyObject(body)) {
    return undefined;
  }
  const candidate = body as WelcomeBody;
  if (!isNonEmptyString(candidate.session_id) || !isInteger(candidate.server_time_ms) ||
    !isNonEmptyString(candidate.mode) || !isInteger(candidate.watchdog_ms) ||
    !isInteger(candidate.command_rate_hz) || typeof candidate.base_locked !== 'boolean' ||
    !isBodyObject(candidate.limits)) {
    return undefined;
  }
  return candidate;
}

export function validateControlStatusBody(body: Object): ControlStatusBody | undefined {
  if (!isBodyObject(body)) {
    return undefined;
  }
  const candidate = body as ControlStatusBody;
  if (typeof candidate.granted !== 'boolean') {
    return undefined;
  }
  if (candidate.reason !== undefined && typeof candidate.reason !== 'string') {
    return undefined;
  }
  if (candidate.lease_id !== undefined && typeof candidate.lease_id !== 'string') {
    return undefined;
  }
  if (candidate.owner_client_id !== undefined && typeof candidate.owner_client_id !== 'string') {
    return undefined;
  }
  if (candidate.expires_at_ms !== undefined && !isInteger(candidate.expires_at_ms)) {
    return undefined;
  }
  if (candidate.granted && (!candidate.lease_id || candidate.lease_id.length === 0 ||
    !candidate.owner_client_id || candidate.owner_client_id.length === 0 ||
    candidate.expires_at_ms === undefined || candidate.expires_at_ms <= 0)) {
    return undefined;
  }
  return candidate;
}

export function validateRobotStateBody(body: Object): RobotStateBody | undefined {
  if (!isBodyObject(body)) {
    return undefined;
  }
  const candidate = body as RobotStateBody;
  if (!isNonEmptyString(candidate.robot_state) || typeof candidate.estop_reason !== 'string' ||
    !isFiniteNumberArray(candidate.joint_position_rad, 6) ||
    !isFiniteNumberArray(candidate.joint_velocity_rad_s, 6) || !isFinitePose(candidate.tcp_pose) ||
    !isFiniteNumber(candidate.gripper_pct) || candidate.gripper_pct < 0 || candidate.gripper_pct > 100 ||
    typeof candidate.base_locked !== 'boolean' || typeof candidate.watchdog_ok !== 'boolean' ||
    typeof candidate.recording !== 'boolean') {
    return undefined;
  }
  return candidate;
}

export function validateRecordingStatusBody(body: Object): RecordingStatusBody | undefined {
  if (!isBodyObject(body)) {
    return undefined;
  }
  const candidate = body as RecordingStatusBody;
  if (typeof candidate.recording !== 'boolean' || typeof candidate.episode_id !== 'string' ||
    !isInteger(candidate.frame_count) || candidate.frame_count < 0 ||
    !isInteger(candidate.started_at_ms) || candidate.started_at_ms < 0 ||
    typeof candidate.path !== 'string' ||
    (candidate.reason !== undefined && typeof candidate.reason !== 'string')) {
    return undefined;
  }
  return candidate;
}

export function validateAckBody(body: Object): AckBody | undefined {
  if (!isBodyObject(body)) {
    return undefined;
  }
  const candidate = body as AckBody;
  if (!isInteger(candidate.ack_seq) || candidate.ack_seq < 0 || !isNonEmptyString(candidate.ack_type) ||
    typeof candidate.accepted !== 'boolean' ||
    (candidate.clamped !== undefined && typeof candidate.clamped !== 'boolean') ||
    (candidate.detail !== undefined && typeof candidate.detail !== 'string')) {
    return undefined;
  }
  return candidate;
}

export function validateErrorBody(body: Object): ErrorBody | undefined {
  if (!isBodyObject(body)) {
    return undefined;
  }
  const candidate = body as ErrorBody;
  if ((candidate.ack_seq !== undefined && (!isInteger(candidate.ack_seq) || candidate.ack_seq < 0)) ||
    !isNonEmptyString(candidate.code) || typeof candidate.message !== 'string' ||
    typeof candidate.recoverable !== 'boolean') {
    return undefined;
  }
  return candidate;
}

export function validateSafetyEventBody(body: Object): SafetyEventBody | undefined {
  if (!isBodyObject(body)) {
    return undefined;
  }
  const candidate = body as SafetyEventBody;
  if (!isNonEmptyString(candidate.severity) || !isNonEmptyString(candidate.code) ||
    typeof candidate.message !== 'string' || !isNonEmptyString(candidate.action)) {
    return undefined;
  }
  return candidate;
}

export function encodeEnvelope(type: string, seq: number, sentAtMs: number, body: Object): string {
  return JSON.stringify(new ProtocolEnvelope(type, seq, sentAtMs, body));
}

export function decodeEnvelope(text: string): ProtocolEnvelope | undefined {
  try {
    const parsed = JSON.parse(text) as Object;
    if (!isBodyObject(parsed)) {
      return undefined;
    }
    const envelope = parsed as ProtocolEnvelope;
    if (typeof envelope.protocol !== 'string' || envelope.protocol !== TELEOP_PROTOCOL ||
      !isNonEmptyString(envelope.type) || !isInteger(envelope.seq) || envelope.seq < 0 ||
      !isInteger(envelope.sent_at_ms) || envelope.sent_at_ms <= 0 || !isBodyObject(envelope.body)) {
      return undefined;
    }
    return envelope;
  } catch (error) {
    return undefined;
  }
}
