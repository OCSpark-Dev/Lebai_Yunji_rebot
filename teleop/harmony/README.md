# LM3-UP HarmonyOS 安全遥操客户端

这是 LM3-UP 复合机器人手机遥操与 VLA 示范录制链路的 HarmonyOS 原生 Stage 工程。首版只控制乐白机械臂和夹爪，**不控制云迹 UP 底盘**。开始遥操前，必须由独立、确定性的现场流程确认底盘已经停止并锁定。

## 安全边界

- 客户端只在 `session.welcome` 参数兼容、底盘锁定、服务端 watchdog 正常、最近 1 秒内收到无急停/故障的 `robot.state`、四项现场检查全部确认且控制租约有效时允许运动。
- `robot.state` 新鲜度与本地租约截止时间使用系统单调启动时钟计算，避免设备校时或墙上时钟跳变绕过超时；租约期限按服务端 `expires_at_ms - sent_at_ms` 推导，并在本地上限 2000 ms 内截断。
- 只有机器人处于 `IDLE` 时才能申请控制权。已有租约并实际点动时可接受 `IDLE`、`MOVING` 或兼容桥接状态 `RUNNING`；未点动时仍只接受 `IDLE`，其他状态立即失败关闭。
- 控制权必须通过 1500 ms 长按申请；运动还要求操作者持续按住 DEADMAN，并同时按住单个轴键。
- DEADMAN 与轴键分别绑定首次按下的触点 ID；其他触点不能接管或覆盖当前输入，同一时间只允许一个笛卡尔轴，夹爪命令也不能与轴向点动并发。
- 松开 DEADMAN、松开轴键、切换速度、安全状态变差、状态或租约过期、App 进入后台、WebSocket 出错/断开都会在本地立即清零，并尽力发送 `stop`。
- 客户端以 20 Hz 发送速度命令，单条命令持续 100 ms；服务端宣告的 watchdog 必须大于 0 且不超过 300 ms。
- 软件停止不能代替实体急停。真机调试必须由受训人员在实体急停可触达、空载、低速、底盘锁定的条件下进行。
- 夹爪 DEADMAN 只授权发送一次 `gripper.set` 目标；松手/后台/断线后的 `motion.stop` 不能保证中途停止 LMG-90，真机前必须单独验证停止/保持和人工解困。

服务端是最终安全边界，必须再次验证认证、严格递增序号、时间有效性、有限数值、租约、DEADMAN、机器人状态、底盘锁定、工作空间和速度限制。服务端必须在 WebSocket 关闭时立即停止，并在 300 ms 内没有新的有效 DEADMAN 运动命令时停止；不能依赖手机成功发送最后一条 `stop`。

## 工程与构建

- DevEco Studio / Hvigor 模型版本：26.0.0
- HarmonyOS Stage 模型，目标与兼容 SDK：`6.1.1(24)`
- 模块：`entry`
- 页面：`entry/src/main/ets/pages/Index.ets`
- 生命周期失败关闭：`entry/src/main/ets/entryability/EntryAbility.ets`
- 协议模型：`entry/src/main/ets/model/TeleopProtocol.ets`
- WebSocket 与安全状态机：`entry/src/main/ets/service/TeleopClient.ets`

工程不包含签名配置、证书、私钥或 token。命令行默认生成 unsigned HAP；需要安装到真机时，应在 DevEco Studio 中使用本机受控的调试/发布签名，不要把签名材料提交到 Git。

```powershell
Set-Location 'D:\Coding\lebai_yunji\teleop\harmony'
$env:JAVA_HOME='C:\Program Files\Huawei\DevEco Studio\jbr'
$env:NODE_HOME='C:\Program Files\Huawei\DevEco Studio\tools\node'
$env:DEVECO_SDK_HOME='C:\Program Files\Huawei\DevEco Studio\sdk'
$env:Path="$env:JAVA_HOME\bin;$env:NODE_HOME;C:\Program Files\Huawei\DevEco Studio\tools\ohpm\bin;$env:Path"

& 'C:\Program Files\Huawei\DevEco Studio\tools\ohpm\bin\ohpm.bat' install
& .\scripts\verify-static.ps1
& 'C:\Program Files\Huawei\DevEco Studio\tools\hvigor\bin\hvigorw.bat' assembleHap --no-daemon --no-incremental --type-check
```

网关地址不写死在源码中，由操作者在 UI 输入 `ws://` 或 `wss://` URL。共享 token 仅保存在当前页面内存中，调用连接后立即清空，不写入首选项、源码、URL 或日志。

连接策略拒绝 URL 用户名/密码、fragment 和任何 query 参数。明文 `ws://` 只允许 `localhost`、IPv4 `127.0.0.0/8`、IPv6 `::1`、RFC1918（`10/8`、`172.16/12`、`192.168/16`）、`169.254/16` 与 `.local` 名称；不再信任不含点的单标签主机名。`wss://` 不受本地地址范围限制，但同样禁止在 URL 中携带凭据或 query。即使地址获准，明文 WS 也只能用于受控、隔离的开发局域网；生产部署必须使用 WSS/TLS 和设备级凭据。

## `lm3-teleop.v1` Harmony 首版协议

所有 WebSocket 文本消息都是一个 UTF-8 JSON 对象：

```json
{
  "protocol": "lm3-teleop.v1",
  "type": "session.hello",
  "seq": 0,
  "sent_at_ms": 1786200000000,
  "body": {}
}
```

服务端首帧必须严格为 `session.welcome` 且 `seq=0`；之后同一方向、同一连接内的 `seq` 必须严格递增，重复 welcome 或 welcome 前的其他消息都会失败关闭。客户端只接受文本帧，并在分派前验证信封以及每一种服务端消息的必填字段、布尔类型、整数时间戳、有限数值、六维关节/TCP 数据和夹爪范围；非法消息、重复/倒退序号、未知消息类型和二进制消息都会失败关闭。

客户端发送：

| `type` | `body` 关键字段 |
| --- | --- |
| `session.hello` | `client_id`、`client_name`、`platform`、`app_version`、`auth_token`、`capabilities=["cartesian_velocity","gripper","recording"]` |
| `control.acquire` | `requested_lease_ms=2000`、`operator_hold_ms=1500`、四项 `safety_ack` |
| `heartbeat` | 可选 `lease_id`、`deadman=false`；不能刷新运动 watchdog |
| `motion.cartesian_velocity` | `lease_id`、`deadman=true`、`frame="base"`、线/角速度、`duration_ms=100` |
| `motion.stop` | 可选 `lease_id`、`reason` |
| `control.release` | `lease_id`；失败关闭、断开或后台时释放控制租约 |
| `gripper.set` | `lease_id`、`deadman=true`、`position_pct`（0–100） |
| `recording.start` | `lease_id`、`task`、可选 `episode_id`、`cameras` |
| `recording.stop` | `lease_id`、`reason` |

服务端发送：

| `type` | 客户端要求 |
| --- | --- |
| `session.welcome` | `command_rate_hz` 必须为 20，`watchdog_ms` 必须在 1–300，包含 `base_locked`、`mode`、`limits` |
| `control.status` | 授权时包含未过期的 `lease_id` 和 `expires_at_ms` |
| `robot.state` | `robot_state`、`estop_reason`、六维 `joint_position_rad`/`joint_velocity_rad_s`、`tcp_pose`、`gripper_pct`、`base_locked`、`watchdog_ok`、`recording`；应持续推送以保持状态小于 1 秒 |
| `recording.status` | `recording`、可选 `episode_id`、`frame_count`、`started_at_ms`、`path` 与 `reason` |
| `ack` | `ack_seq`、`ack_type`、`accepted`、可选 `clamped` 与 `detail` |
| `error` | 可选 `ack_seq`、`code`、`message`、`recoverable` |
| `safety.event` | `severity`、`code`、`message`、`action`；`action="stop"` 时客户端停止并清除租约 |

三档客户端速度上限为：极慢 `0.005 m/s, 0.02 rad/s`，低速 `0.015 m/s, 0.05 rad/s`，调试 `0.03 m/s, 0.10 rad/s`。这些只是客户端保守值，服务端仍必须独立钳制。

`pose.sample` 是未来的客户端→服务端预留消息；HarmonyOS 首版既不发送也不接收该消息，服务端当前会返回 `UNSUPPORTED_MODE`。

## 真机验证顺序

1. 只连接模拟网关，验证认证失败、协议错误、序号倒退、欢迎参数不兼容和断线都失败关闭。
2. 验证四项检查未完成、状态陈旧、底盘未锁定、急停或错误码非零时不能取得租约。
3. 在模拟器回放按下/松开 DEADMAN、每个轴键、App 后台和网络中断，确认客户端立即清零，服务端在 300 ms 内停止。
4. 真机只做底盘锁定、空载、极慢档、小工作空间点动；记录实际 TCP、关节、夹爪反馈与停止延迟。
5. 录制数据必须保留命令、实际反馈、相机/服务端时间、网络间隔、watchdog 与异常标记。不能把被拒绝或被钳制的手机命令当成 VLA 训练真值。

在完成相机内参、手眼关系、坐标系、漂移、延迟与跳变检测前，不得启用预留的手机 6DoF `pose.sample` 控制。
