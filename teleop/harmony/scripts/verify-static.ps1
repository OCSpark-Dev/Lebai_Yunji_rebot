$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$clientPath = Join-Path $projectRoot 'entry\src\main\ets\service\TeleopClient.ets'
$protocolPath = Join-Path $projectRoot 'entry\src\main\ets\model\TeleopProtocol.ets'
$networkPolicyPath = Join-Path $projectRoot 'entry\src\main\ets\service\NetworkPolicy.ets'
$indexPath = Join-Path $projectRoot 'entry\src\main\ets\pages\Index.ets'
$abilityPath = Join-Path $projectRoot 'entry\src\main\ets\entryability\EntryAbility.ets'

$client = Get-Content -Raw -Encoding utf8 -LiteralPath $clientPath
$protocol = Get-Content -Raw -Encoding utf8 -LiteralPath $protocolPath
$networkPolicy = Get-Content -Raw -Encoding utf8 -LiteralPath $networkPolicyPath
$index = Get-Content -Raw -Encoding utf8 -LiteralPath $indexPath
$ability = Get-Content -Raw -Encoding utf8 -LiteralPath $abilityPath

# Keep this script ASCII-only so it parses consistently in Windows PowerShell 5.1.
$checks = [ordered]@{}

$checks['hello uses canonical type'] = (
  $client.Contains("sendEnvelope('session.hello'")
)
$checks['first outbound seq uses zero before increment'] = (
  $client.Contains('private nextSeq: number = 0;') -and
  $client.Contains('const sequence = this.nextSeq;') -and
  $client.Contains('this.nextSeq += 1;') -and
  $client.Contains('encodeEnvelope(type, sequence, body)')
)
$checks['envelope requires integer seq and timestamp'] = (
  $protocol.Contains('!isInteger(envelope.seq)') -and
  $protocol.Contains('!isInteger(envelope.sent_at_ms)')
)
$checks['envelope requires object body'] = (
  $protocol.Contains('!isBodyObject(envelope.body)')
)
$checks['first server frame is canonical welcome seq zero'] = (
  $client.Contains("envelope.seq !== 0 || envelope.type !== 'session.welcome'")
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
  $client.Contains('!this.deadmanHeld || this.activeAxis !== AxisName.NONE || !this.hasSafeLease(false)')
)
$checks['canonical capabilities present'] = (
  $protocol.Contains("'cartesian_velocity'") -and
  $protocol.Contains("'gripper'") -and
  $protocol.Contains("'recording'")
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
