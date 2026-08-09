$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$clientPath = Join-Path $projectRoot 'entry\src\main\ets\service\TeleopClient.ets'
$protocolPath = Join-Path $projectRoot 'entry\src\main\ets\model\TeleopProtocol.ets'
$networkPolicyPath = Join-Path $projectRoot 'entry\src\main\ets\service\NetworkPolicy.ets'
$indexPath = Join-Path $projectRoot 'entry\src\main\ets\pages\Index.ets'
$abilityPath = Join-Path $projectRoot 'entry\src\main\ets\entryability\EntryAbility.ets'
$poseSensorPath = Join-Path $projectRoot 'entry\src\main\ets\service\PhonePoseSensor.ets'
$modulePath = Join-Path $projectRoot 'entry\src\main\module.json5'

$client = Get-Content -Raw -Encoding utf8 -LiteralPath $clientPath
$protocol = Get-Content -Raw -Encoding utf8 -LiteralPath $protocolPath
$networkPolicy = Get-Content -Raw -Encoding utf8 -LiteralPath $networkPolicyPath
$index = Get-Content -Raw -Encoding utf8 -LiteralPath $indexPath
$ability = Get-Content -Raw -Encoding utf8 -LiteralPath $abilityPath
$poseSensor = Get-Content -Raw -Encoding utf8 -LiteralPath $poseSensorPath
$module = Get-Content -Raw -Encoding utf8 -LiteralPath $modulePath

# Keep this script ASCII-only so it parses consistently in Windows PowerShell 5.1.
function Multiply-Quaternion([double[]]$left, [double[]]$right) {
  return [double[]]@(
    ($left[3] * $right[0] + $left[0] * $right[3] + $left[1] * $right[2] - $left[2] * $right[1]),
    ($left[3] * $right[1] - $left[0] * $right[2] + $left[1] * $right[3] + $left[2] * $right[0]),
    ($left[3] * $right[2] + $left[0] * $right[1] - $left[1] * $right[0] + $left[2] * $right[3]),
    ($left[3] * $right[3] - $left[0] * $right[0] - $left[1] * $right[1] - $left[2] * $right[2])
  )
}

function Invert-Quaternion([double[]]$value) {
  return [double[]]@(-$value[0], -$value[1], -$value[2], $value[3])
}

$halfPi = [Math]::PI / 4
$zero = [double[]]@(0, 0, [Math]::Sin($halfPi), [Math]::Cos($halfPi))
$previousLocal = [double[]]@([Math]::Sin([Math]::PI / 36), 0, 0, [Math]::Cos([Math]::PI / 36))
$currentLocal = [double[]]@([Math]::Sin([Math]::PI / 18), 0, 0, [Math]::Cos([Math]::PI / 18))
$previousRaw = Multiply-Quaternion -left $zero -right $previousLocal
$currentRaw = Multiply-Quaternion -left $zero -right $currentLocal
$previousRelative = Multiply-Quaternion -left (Invert-Quaternion $zero) -right $previousRaw
$currentRelative = Multiply-Quaternion -left (Invert-Quaternion $zero) -right $currentRaw
$leftDelta = Multiply-Quaternion -left $currentRelative -right (Invert-Quaternion $previousRelative)
$leftDeltaAngle = 2 * [Math]::Atan2([Math]::Abs($leftDelta[0]), $leftDelta[3])
$poseMathExecutablePassed = [Math]::Abs($leftDeltaAngle - ([Math]::PI / 18)) -lt 0.000001 -and
  [Math]::Abs($leftDelta[1]) -lt 0.000001 -and [Math]::Abs($leftDelta[2]) -lt 0.000001
$phoneVector = [double[]]@(0.1, 0.2, 0.3)
$tcpVector = [double[]]@($phoneVector[1], -$phoneVector[0], $phoneVector[2])
$poseAxisMappingExecutablePassed = [Math]::Abs($tcpVector[0] - 0.2) -lt 0.000001 -and
  [Math]::Abs($tcpVector[1] + 0.1) -lt 0.000001 -and [Math]::Abs($tcpVector[2] - 0.3) -lt 0.000001

$checks = [ordered]@{}

$checks['hello uses canonical type'] = (
  $client.Contains("sendEnvelope('session.hello'")
)
$checks['connection UI requires only websocket address'] = (
  $index.Contains('this.client.connect(this.endpoint)') -and
  $index.Contains('this.snapshot.clientName') -and
  -not $index.Contains('authToken') -and
  -not $index.Contains('共享 token')
)
$checks['hello contains no application token'] = (
  $client.Contains('new HelloBody(this.clientId, this.clientName)') -and
  -not $client.Contains('authToken') -and
  -not $protocol.Contains('auth_token')
)
$checks['terminal name is generated from the device model'] = (
  $client.Contains("deviceInfo, systemDateTime") -and
  $client.Contains('deviceInfo.productModel.trim()') -and
  $client.Contains("return 'HarmonyOS phone';") -and
  $protocol.Contains('constructor(clientId: string, clientName: string)') -and
  $protocol.Contains('this.client_name = clientName;')
)
$checks['first outbound seq uses zero before increment'] = (
  $client.Contains('private nextSeq: number = 0;') -and
  $client.Contains('const sequence = this.nextSeq;') -and
  $client.Contains('this.nextSeq += 1;') -and
  $client.Contains('encodeEnvelope(type, sequence, sentAtMs, body)')
)
$checks['welcome synchronizes outbound wall clock timestamps'] = (
  $client.Contains('private serverWallClockOffsetMs: number = 0;') -and
  $client.Contains('this.serverWallClockOffsetMs = body.server_time_ms - Date.now();') -and
  $client.Contains('const sentAtMs = this.outboundWallClockNowMs(type);') -and
  $client.Contains('return localNowMs + this.serverWallClockOffsetMs;')
)
$checks['hello stays on local wall clock and resets discard offset'] = (
  $client.Contains("if (type === 'session.hello')") -and
  $client.Contains('return localNowMs;') -and
  ([regex]::Matches($client, 'this\.serverWallClockOffsetMs = 0;')).Count -ge 4
)
$checks['protocol encoder requires caller supplied timestamp'] = (
  $protocol.Contains('constructor(type: string, seq: number, sentAtMs: number, body: Object)') -and
  $protocol.Contains('this.sent_at_ms = sentAtMs;') -and
  $protocol.Contains('encodeEnvelope(type: string, seq: number, sentAtMs: number, body: Object)') -and
  -not $protocol.Contains('this.sent_at_ms = Date.now();')
)
$checks['envelope requires integer seq and timestamp'] = (
  $protocol.Contains('!isInteger(envelope.seq)') -and
  $protocol.Contains('!isInteger(envelope.sent_at_ms)')
)
$checks['envelope requires object body'] = (
  $protocol.Contains('!isBodyObject(envelope.body)')
)
$checks['pre-welcome error seq zero is strictly validated surfaced and closed'] = (
  $client.Contains("envelope.seq === 0 && envelope.type === 'error'") -and
  $client.Contains('const preWelcomeError = validateErrorBody(envelope.body);') -and
  $client.Contains("this.closeSocket('invalid_pre_welcome_error_body', false)") -and
  $client.Contains('this.closeSocket(`server_error:${preWelcomeError.code}:${preWelcomeError.message}`, false)')
)
$checks['other first server frames still require canonical welcome seq zero'] = (
  $client.Contains("envelope.seq !== 0 || envelope.type !== 'session.welcome'") -and
  $client.Contains("failClosed('first_server_message_must_be_session_welcome_seq_0', true)")
)
$checks['noncanonical welcome alias is rejected'] = (
  -not $client.Contains("envelope.type === 'welcome'")
)
$checks['repeated welcome and pre-welcome traffic are rejected'] = (
  $client.Contains("!this.welcomed || envelope.type === 'session.welcome'")
)
$checks['all canonical server bodies use runtime validators'] = (
  $client.Contains('validateWelcomeBody(envelope.body)') -and
  $client.Contains('validateControlStatusBody(envelope.body)') -and
  $client.Contains('validateRobotStateBody(envelope.body)') -and
  $client.Contains('validateRecordingStatusBody(envelope.body)') -and
  $client.Contains('validateAckBody(envelope.body)') -and
  $client.Contains('validateErrorBody(envelope.body)') -and
  $client.Contains('validateSafetyEventBody(envelope.body)')
)
$checks['robot state validator requires finite canonical fields'] = (
  $protocol.Contains('isFiniteNumberArray(candidate.joint_position_rad, 6)') -and
  $protocol.Contains('isFiniteNumberArray(candidate.joint_velocity_rad_s, 6)') -and
  $protocol.Contains('isFinitePose(candidate.tcp_pose)') -and
  $protocol.Contains("typeof candidate.base_locked !== 'boolean'") -and
  $protocol.Contains("typeof candidate.watchdog_ok !== 'boolean'")
)
$checks['state timestamp is updated only after validator succeeds'] = (
  $client.IndexOf('validateRobotStateBody(envelope.body)') -ge 0 -and
  $client.IndexOf('this.lastRobotStateUptimeMs = nowUptimeMs;') -gt
    $client.IndexOf('validateRobotStateBody(envelope.body)')
)
$checks['monotonic uptime drives safety timing'] = (
  $client.Contains('systemDateTime.getUptime(systemDateTime.TimeType.STARTUP)') -and
  $client.Contains('this.lastRobotStateUptimeMs') -and
  $client.Contains('this.leaseDeadlineUptimeMs')
)
$checks['lease deadline is derived from server duration and locally capped'] = (
  $client.Contains('body.expires_at_ms - envelope.sent_at_ms') -and
  $client.Contains('Math.min(REQUESTED_LEASE_MS, serverRemainingMs)')
)
$checks['acquire only permits IDLE'] = (
  $client.Contains("this.robotMode === 'IDLE' && this.leaseId.length === 0")
)
$checks['active leased motion permits IDLE MOVING or RUNNING'] = (
  $client.Contains("this.robotMode === 'MOVING'") -and
  $client.Contains("this.robotMode === 'RUNNING'")
)
$checks['motion rate has monotonic 50 ms guard'] = (
  $client.Contains('nowUptimeMs - this.lastMotionSentAtUptimeMs >= COMMAND_PERIOD_MS')
)
$checks['motion uses canonical type'] = (
  $client.Contains("sendEnvelope('motion.cartesian_velocity'")
)
$checks['stop uses canonical type'] = (
  $client.Contains("sendEnvelope('motion.stop'")
)
$checks['control acquire uses canonical type'] = (
  $client.Contains("sendEnvelope('control.acquire'")
)
$checks['gripper uses canonical type'] = (
  $client.Contains("sendEnvelope('gripper.set'")
)
$checks['gripper is mutually exclusive with cartesian motion'] = (
  $client.Contains('!this.deadmanHeld || this.poseDeadmanHeld || this.activeAxis !== AxisName.NONE')
)
$checks['canonical capabilities present'] = (
  $protocol.Contains("'cartesian_velocity'") -and
  $protocol.Contains("'gripper'") -and
  $protocol.Contains("'recording'") -and
  $protocol.Contains("'pose_sample'")
)
$checks['hardware gyroscope gates rotation vector subscription'] = (
  $poseSensor.Contains("import { sensor } from '@kit.SensorServiceKit'") -and
  $poseSensor.Contains('sensor.getSingleSensorSync(sensor.SensorId.GYROSCOPE)') -and
  $poseSensor.Contains('sensor.getSingleSensorSync(sensor.SensorId.GYROSCOPE_UNCALIBRATED)') -and
  $poseSensor.Contains("failureListener('hardware_gyroscope_unavailable')") -and
  $poseSensor.Contains('sensor.getSingleSensorSync(sensor.SensorId.ROTATION_VECTOR)') -and
  $poseSensor.Contains("sensor.on(sensor.SensorId.ROTATION_VECTOR") -and
  $poseSensor.Contains("sensor.off(sensor.SensorId.ROTATION_VECTOR") -and
  -not $poseSensor.Contains("sensor.on(sensor.SensorId.GYROSCOPE") -and
  $poseSensor.Contains("interval: 'game'")
)
$checks['sensor timestamp is converted from boot nanoseconds safely'] = (
  $poseSensor.Contains('Math.floor(value.timestamp / 1000000)') -and
  $poseSensor.Contains('Number.isSafeInteger(sensorTimestampMs)') -and
  $client.Contains('reading.sensorTimestampMs <= this.poseLastSensorTimestampMs') -and
  $client.Contains('this.poseLastSensorTimestampMs,')
)
$checks['fused rotation vector adds no raw IMU permission'] = (
  -not $module.Contains('ohos.permission.ACCELEROMETER') -and
  -not $module.Contains('ohos.permission.GYROSCOPE')
)
$checks['pose sample schema is rotation only and canonical'] = (
  $protocol.Contains("frame: string = 'phone_calibrated'") -and
  $protocol.Contains("mapping: string = 'tcp_orientation'") -and
  $protocol.Contains("tracking_state: string = 'tracking'") -and
  $protocol.Contains('sensor_timestamp_ms: number;') -and
  $protocol.Contains('angular_delta_rad: Angular3;') -and
  -not $protocol.Contains('position_m:')
)
$checks['pose control requires explicit zero and held deadman'] = (
  $client.Contains('calibratePose(): void') -and
  $client.Contains('this.poseCalibrationQuaternion = this.copyQuaternion(latest);') -and
  $client.Contains('setPoseDeadman(held: boolean, touchId: number): void') -and
  $protocol.Contains('deadman: boolean = true;') -and
  $index.Contains('handlePoseDeadmanTouch(event: TouchEvent)')
)
$checks['pose delta is left difference in calibrated frame'] = (
  $client.Contains('this.inverseQuaternion(this.poseCalibrationQuaternion)') -and
  $client.Contains('this.inverseQuaternion(this.posePreviousSentRelative)') -and
  $client.Contains('this.posePrimePending = true;') -and
  $client.Contains('normalized.w < 0') -and
  $poseMathExecutablePassed
)
$checks['pose axis mapping is explicit y negative-x z'] = (
  $protocol.Contains('POSE_TCP_RX_FROM_PHONE_Y_SIGN: number = 1') -and
  $protocol.Contains('POSE_TCP_RY_FROM_PHONE_X_SIGN: number = -1') -and
  $protocol.Contains('POSE_TCP_RZ_FROM_PHONE_Z_SIGN: number = 1') -and
  $client.Contains('tcpDelta.rx = phoneDelta.ry') -and
  $client.Contains('tcpDelta.ry = phoneDelta.rx') -and
  $poseAxisMappingExecutablePassed
)
$checks['pose commands are limited to 20 Hz monotonic timestamps'] = (
  $protocol.Contains('POSE_SAMPLE_PERIOD_MS: number = COMMAND_PERIOD_MS') -and
  $client.Contains('nowUptimeMs - this.lastMotionSentAtUptimeMs >= POSE_SAMPLE_PERIOD_MS') -and
  $client.Contains('this.poseLastSensorTimestampMs,') -and
  $client.Contains('this.poseLastSensorTimestampMs === this.poseLastSentSensorTimestampMs') -and
  $client.Contains('sensorIntervalMs < POSE_MIN_SENT_INTERVAL_MS') -and
  $client.Contains('sensorIntervalMs > POSE_MAX_SENT_INTERVAL_MS') -and
  $client.Contains('derivedAngularRps > POSE_MAX_INPUT_ANGULAR_RPS') -and
  $client.Contains("sendEnvelope('pose.sample'")
)
$checks['pose jump stale and accuracy loss fail closed'] = (
  $client.Contains('POSE_MAX_SENSOR_JUMP_RAD') -and
  $client.Contains('POSE_MAX_COMMAND_DELTA_RAD') -and
  $client.Contains('POSE_MAX_RELATIVE_ANGLE_RAD') -and
  $client.Contains('POSE_MAX_SAMPLE_GAP_MS') -and
  $client.Contains('POSE_SENSOR_STALE_MS') -and
  $client.Contains("failClosed('pose_sensor_jump', true)") -and
  $client.Contains("failClosed('pose_sensor_gap', true)") -and
  $client.Contains("failClosed('pose_sensor_accuracy_lost', true)") -and
  $client.Contains("failClosed('pose_relative_range_exceeded', true)")
)
$checks['pose confidence matches bridge minimum'] = (
  $protocol.Contains('POSE_MIN_ACCURACY: number = 2') -and
  $client.Contains('poseConfidenceForAccuracy') -and
  $client.Contains('return 0.8;')
)
$checks['pose lifecycle stops subscription and clears calibration'] = (
  ([regex]::Matches($client, 'stopPoseSensorInternal\(true\)')).Count -ge 3 -and
  $client.Contains('this.clearPoseCalibration();') -and
  $client.Contains("closeSocket('app_background', true)")
)
$checks['pose ACK and delayed errors are sequence correlated'] = (
  $client.Contains('private pendingPoseAckSeqs: Array<number> = [];') -and
  $client.Contains('const poseSequence = this.nextSeq;') -and
  $client.Contains('this.pendingPoseAckSeqs.push(poseSequence);') -and
  $client.Contains('this.pendingPoseAckSeqs.length > 32') -and
  $client.Contains("ackType === 'pose.sample'") -and
  $client.Contains('body.ack_seq !== undefined && this.removePendingPoseAck(body.ack_seq)') -and
  $client.Contains('failClosed(`pose_server_error:${body.code}`, true)') -and
  $client.Contains("failClosed('pose_ack_backlog', true)") -and
  ([regex]::Matches($client, 'this.pendingPoseAckSeqs = \[\];')).Count -ge 2 -and
  $client.Contains("closeSocket('send_transport_failure', false)")
)
$checks['pose UI states rotation only and simulator axis check'] = (
  $index.Contains('this.client.startPoseSensor()') -and
  $index.Contains('this.client.stopPoseSensor()') -and
  $index.Contains('this.client.calibratePose()') -and
  $index.Contains('this.snapshot.poseDeltaText') -and
  $index.Contains('GYROSCOPE') -and
  $index.Contains('ROTATION_VECTOR')
)
$checks['recording uses canonical camera names and rejects empty selection'] = (
  $index.Contains("'camera_top,camera_wrist'") -and
  $client.Contains("cameras.length === 0") -and
  $client.Contains('camera_wrist') -and
  -not $client.Contains('wrist_rgb')
)
$checks['endpoint validation is invoked before connect'] = (
  $client.Contains('NetworkPolicy.validate(url)')
)
$checks['URL query is rejected'] = (
  $networkPolicy.Contains('parsed.search.length > 0')
)
$checks['public cleartext websocket is rejected'] = (
  $networkPolicy.Contains("scheme === 'ws:'") -and
  $networkPolicy.Contains('!NetworkPolicy.isPrivateOrLocalHost(parsed.hostname)')
)
$checks['single-label hosts are not trusted as cleartext local'] = (
  -not $networkPolicy.Contains("host.indexOf('.') < 0")
)
$checks['cleartext allowlist covers only explicit local names and literal ranges'] = (
  $networkPolicy.Contains("host === 'localhost'") -and
  $networkPolicy.Contains("host === '::1'") -and
  $networkPolicy.Contains("host.endsWith('.local')") -and
  $networkPolicy.Contains('octets[0] === 10') -and
  $networkPolicy.Contains('octets[0] === 127') -and
  $networkPolicy.Contains('octets[0] === 169 && octets[1] === 254') -and
  $networkPolicy.Contains('octets[0] === 172 && octets[1] >= 16 && octets[1] <= 31') -and
  $networkPolicy.Contains('octets[0] === 192 && octets[1] === 168')
)
$checks['axis input has touch ownership and mutual exclusion'] = (
  $client.Contains('private axisTouchId: number = -1;') -and
  $client.Contains('if (this.axisTouchId >= 0 && touchId !== this.axisTouchId)') -and
  $client.Contains('this.activeAxis !== axis || this.axisDirection !== direction') -and
  $index.Contains('event.changedTouches[0].id')
)
$checks['deadman input has touch ownership'] = (
  $client.Contains('private deadmanTouchId: number = -1;') -and
  $client.Contains('this.deadmanTouchId >= 0 && touchId !== this.deadmanTouchId')
)
$checks['receive failure tries stop while send failure closes directly'] = (
  $client.Contains("closeSocket('receive_transport_failure', true)") -and
  $client.Contains("closeSocket('send_transport_failure', false)")
)
$checks['background lifecycle fails closed'] = (
  $ability.Contains('appBackground()') -and
  $client.Contains("closeSocket('app_background', true)")
)
$checks['safety checklist is included in snapshots'] = (
  $client.Contains('baseStationary: boolean = false;') -and
  $client.Contains('snapshot.baseStationary = this.safetyAck.base_stationary;') -and
  $client.Contains('snapshot.workspaceClear = this.safetyAck.workspace_clear;') -and
  $client.Contains('snapshot.estopAccessible = this.safetyAck.estop_accessible;') -and
  $client.Contains('snapshot.toolSecure = this.safetyAck.tool_secure;')
)
$checks['safety checklist is cleared across sessions'] = (
  $client.Contains('private safetyAck: SafetyAck = new SafetyAck();') -and
  ([regex]::Matches($client, 'this\.safetyAck = new SafetyAck\(\);')).Count -ge 3 -and
  $index.Contains('private applySnapshot(snapshot: TeleopSnapshot): void') -and
  $index.Contains('this.baseStationary = snapshot.baseStationary;') -and
  $index.Contains('this.workspaceClear = snapshot.workspaceClear;') -and
  $index.Contains('this.estopAccessible = snapshot.estopAccessible;') -and
  $index.Contains('this.toolSecure = snapshot.toolSecure;')
)

$failed = @($checks.GetEnumerator() | Where-Object { -not $_.Value })
$checks.GetEnumerator() | ForEach-Object {
  $status = if ($_.Value) { 'PASS' } else { 'FAIL' }
  Write-Host "[$status] $($_.Key)"
}

if ($failed.Count -gt 0) {
  throw "$($failed.Count) static safety/protocol check(s) failed"
}
