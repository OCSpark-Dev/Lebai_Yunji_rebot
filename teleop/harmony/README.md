# LM3-UP HarmonyOS 安全遥操客户端

这是 LM3-UP 复合机器人手机遥操与 VLA 示范录制链路的 HarmonyOS 原生 Stage 工程。首版只控制乐白机械臂和夹爪，**不控制云迹 UP 底盘**。开始遥操前，必须由独立、确定性的现场流程确认底盘已经停止并锁定。

## 确认：通过手机硬件陀螺仪遥操

是。姿态模式启动时会先检查手机是否存在 `SensorId.GYROSCOPE` 或 `SensorId.GYROSCOPE_UNCALIBRATED`；两者都不存在时直接禁用姿态遥操。硬件陀螺仪门禁通过后，客户端订阅 HarmonyOS `SensorId.ROTATION_VECTOR` 四元数，将手机姿态增量映射为 LM3 TCP 的 `rx/ry/rz`。

这是“硬件陀螺仪门禁 + 系统融合 Rotation Vector”，不是直接积分原始 `GYROSCOPE` 角速度。使用系统融合姿态可以减少原始积分漂移，但它仍只提供可用于旋转的 3DoF；手机 IMU 不能稳定提供 `X/Y/Z` 平移，平移仍由触屏轴键控制。

标准握持姿态是：屏幕朝上，手机顶部指向机器人基坐标 `+X`。固定映射为：

```text
phone [x, y, z] -> TCP [rx, ry, rz] = [y, -x, z]
```

即手机 `+X`（屏幕向右）对应 TCP `-ry`，手机 `+Y`（手机顶部）对应 TCP `+rx`，手机 `+Z`（垂直屏幕向外）对应 TCP `+rz`。机器人控制器的现场轴约定仍必须先在模拟后端逐轴验向。

## 安全边界

- 客户端只在 `session.welcome` 参数兼容、底盘锁定、服务端 watchdog 正常、最近 1 秒内收到无急停/故障的 `robot.state`、四项现场检查全部确认且控制租约有效时允许运动。
- `robot.state` 新鲜度与本地租约截止时间使用系统单调启动时钟计算，避免设备校时或墙上时钟跳变绕过超时；租约期限按服务端 `expires_at_ms - sent_at_ms` 推导，并在本地上限 2000 ms 内截断。
- 首帧 `session.hello` 使用手机本地墙钟。收到通过校验的 `session.welcome.server_time_ms` 后，客户端保存“服务端时间 - 本地 `Date.now()`”的偏移，后续所有出站信封的 `sent_at_ms` 都使用 `Date.now()+offset`；断连、重置或新连接会清零该偏移。这个墙钟同步不参与租约、watchdog、状态新鲜度或 IMU 样本年龄判定。
- 只有机器人处于 `IDLE` 时才能申请控制权。已有租约并实际点动时可接受 `IDLE`、`MOVING` 或兼容桥接状态 `RUNNING`；未点动时仍只接受 `IDLE`，其他状态立即失败关闭。
- 控制权必须通过 1500 ms 长按申请。触屏点动需要按住通用 DEADMAN 和单个轴键；陀螺仪姿态遥操使用另一个独立按住式“姿态 DEADMAN”，两种运动授权不能并发。
- DEADMAN 与轴键分别绑定首次按下的触点 ID；其他触点不能接管或覆盖当前输入，同一时间只允许一个笛卡尔轴，夹爪命令也不能与轴向点动并发。
- 姿态模式必须先显式归零。内部计算 `q_rel = inverse(q_zero) * q_current`，发送相邻已接受姿态的左差 `q_delta = q_rel_current * inverse(q_rel_previous)`，再取最短旋转向量。
- 每次归零或重新按下姿态 DEADMAN 后，首个 `pose.sample` 发送零增量，只做 priming，不执行运动。松手会发送 `motion.stop` 并丢弃差分基线；再次按下必须重新 priming，不会补发松手期间的手机转动。
- 松开任一 DEADMAN、松开轴键、切换速度、安全状态变差、状态或租约过期、App 进入后台、WebSocket 出错/断开都会在本地立即清零，并尽力发送 `stop`。
- 触屏 `motion.cartesian_velocity` 以 20 Hz 发送，单条命令持续 100 ms；姿态 `pose.sample` 最多 20 Hz，由服务端按 `20-150 ms` 传感器间隔换算角速度。服务端宣告的 watchdog 必须大于 0 且不超过 300 ms。
- 软件停止不能代替实体急停。真机调试必须由受训人员在实体急停可触达、空载、低速、底盘锁定的条件下进行。
- 夹爪 DEADMAN 只授权发送一次 `gripper.set` 目标；松手/后台/断线后的 `motion.stop` 不能保证中途停止 LMG-90，真机前必须单独验证停止/保持和人工解困。

服务端是最终安全边界，必须再次验证会话握手、严格递增序号、时间有效性、有限数值、租约、DEADMAN、机器人状态、底盘锁定、工作空间和速度限制。服务端必须在 WebSocket 关闭时立即停止，并在 300 ms 内没有新的有效 DEADMAN 运动命令时停止；不能依赖手机成功发送最后一条 `stop`。

### 陀螺仪姿态安全阈值

| 项目 | 客户端阈值/行为 |
| --- | ---: |
| 最大样本年龄 | `150 ms` |
| 最大原始采样间隔 | `250 ms` |
| 相邻已发送传感器时间间隔 | `20-150 ms` |
| 原始姿态单次跳变 | `35°` |
| 相对归零姿态范围 | `30°` |
| 单帧发送旋转增量 | `12°` (`0.20944 rad`) |
| 由增量/时间推导的输入角速度 | `<= 6 rad/s` |
| 最低传感器精度 | `ACCURACY_MEDIUM=2`，协议 `confidence=0.8` |
| 发送上限 | `20 Hz`，priming 帧也占用同一运动频率配额 |
| 待 ACK 姿态命令上限 | `32` 条，超出即失败关闭 |

HarmonyOS `Response.timestamp` 是从设备启动到上报时的纳秒数。客户端先除以 `1_000_000` 转为毫秒，检查 JavaScript 安全整数和严格递增，再与 `systemDateTime.getUptime(TimeType.STARTUP)` 比较样本年龄。低精度、陈旧、时间倒退、间隔异常、非有限数、跳变、超出归零范围、发送失败、命令拒绝和与 `pose.sample` 序号匹配的延迟服务端错误都会失败关闭。

## 工程与构建

- DevEco Studio / Hvigor 模型版本：26.0.0
- HarmonyOS Stage 模型，目标与兼容 SDK：`6.1.1(24)`
- 模块：`entry`
- 页面：`entry/src/main/ets/pages/Index.ets`
- 生命周期失败关闭：`entry/src/main/ets/entryability/EntryAbility.ets`
- 协议模型：`entry/src/main/ets/model/TeleopProtocol.ets`
- WebSocket 与安全状态机：`entry/src/main/ets/service/TeleopClient.ets`
- 硬件陀螺仪门禁与 Rotation Vector 订阅：`entry/src/main/ets/service/PhonePoseSensor.ets`

工程不包含签名配置、证书、私钥或应用层 token。命令行默认生成 unsigned HAP；需要安装到真机时，应在 DevEco Studio 中使用本机受控的调试/发布签名，不要把签名材料提交到 Git。

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

界面唯一需要填写的是 WebSocket 地址，由操作者输入 `ws://` 或 `wss://` URL。客户端不再显示、校验、保存或发送 token；`session.hello` 也不包含 `auth_token`。终端名自动读取 `deviceInfo.productModel`，裁剪到 48 个字符；设备型号为空时回退为 `HarmonyOS phone`，无需操作者输入。

连接策略拒绝 URL 用户名/密码、fragment 和任何 query 参数。明文 `ws://` 只允许 `localhost`、IPv4 `127.0.0.0/8`、IPv6 `::1`、RFC1918（`10/8`、`172.16/12`、`192.168/16`）、`169.254/16` 与 `.local` 名称；不再信任不含点的单标签主机名。`wss://` 不受本地地址范围限制，但同样禁止在 URL 中携带凭据或 query。当前客户端面向受控局域网验证，连接时只填写 WebSocket 地址。

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

建立会话时，服务端首帧必须严格为 `session.welcome` 且 `seq=0`。握手被拒绝时，唯一允许的 welcome 前例外是 `error seq=0`：客户端会严格校验必填的 `code`、`message`、`recoverable`，并在可选 `ack_seq` 存在时校验其整数类型，再显示服务端的真实 `code/message` 并关闭连接；该错误帧不会建立会话。其他 welcome 前消息仍会被拒绝。welcome 之后同一方向、同一连接内的 `seq` 必须严格递增，重复 welcome 也会失败关闭。客户端只接受文本帧，并在分派前验证信封以及每一种服务端消息的必填字段、布尔类型、整数时间戳、有限数值、六维关节/TCP 数据和夹爪范围；非法消息、重复/倒退序号、未知消息类型和二进制消息都会失败关闭。

`session.hello.sent_at_ms` 使用手机当前本地时间，便于服务端在尚未同步时完成首帧处理。welcome 验证成功后，客户端以 `server_time_ms - Date.now()` 计算当前连接的墙钟偏移，后续 `control.acquire`、heartbeat、运动、夹爪和录制等所有出站信封均带服务端对齐的 `sent_at_ms`。该偏移只属于当前 WebSocket 连接，不会被持久化或用于单调安全计时。

客户端发送：

| `type` | `body` 关键字段 |
| --- | --- |
| `session.hello` | `client_id`、自动生成的 `client_name`、`platform`、`app_version`、`capabilities=["cartesian_velocity","gripper","recording","pose_sample"]`；不发送 `auth_token` |
| `control.acquire` | `requested_lease_ms=2000`、`operator_hold_ms=1500`、四项 `safety_ack` |
| `heartbeat` | 可选 `lease_id`、`deadman=false`；不能刷新运动 watchdog |
| `motion.cartesian_velocity` | `lease_id`、`deadman=true`、`frame="base"`、线/角速度、`duration_ms=100` |
| `pose.sample` | `lease_id`、`deadman=true`、`frame="phone_calibrated"`、`mapping="tcp_orientation"`、`calibration_id`、`sensor_timestamp_ms`、`tracking_state="tracking"`、`confidence`、`angular_delta_rad={rx,ry,rz}` |
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

HarmonyOS 客户端现已发送仅旋转的 `pose.sample`。它不接受手机平移、不控制 UP 底盘，也不应被描述为完整 6DoF 位置跟踪。

## 真机验证顺序

1. 只连接模拟网关，验证握手拒绝、协议错误、序号倒退、欢迎参数不兼容和断线都失败关闭。
2. 验证四项检查未完成、状态陈旧、底盘未锁定、急停或错误码非零时不能取得租约。
3. 在模拟器回放按下/松开触屏 DEADMAN、姿态 DEADMAN、每个轴键、显式归零、首帧 priming、重复传感器时间戳、低精度、样本陈旧、姿态跳变、App 后台和网络中断，确认客户端立即清零，服务端在 300 ms 内停止。
4. 用模拟后端按标准握持姿态逐轴验证 `[y,-x,z]`，并检查松开后再按不会跳到松手期间的新姿态。
5. 真机只做底盘锁定、空载、极慢档、小工作空间点动；记录实际 TCP、关节、夹爪反馈与停止延迟。
6. 录制数据必须保留命令、实际反馈、陀螺仪 `calibration_id`/传感器时间/原始旋转增量、相机/服务端时间、网络间隔、watchdog 与异常标记。不能把被拒绝或被钳制的手机命令当成 VLA 训练真值。

当前仅启用经安全限幅的手机 3DoF 旋转增量。在完成模拟逐轴验向、延迟/跳变/失联停止验证和真机低速小工作空间验收前，不得扩展为手机平移或完整 6DoF 控制。
