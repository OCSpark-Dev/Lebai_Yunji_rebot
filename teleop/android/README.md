# LM3-UP Android 安全遥操

原生 Kotlin Android 客户端，面向 LM3-UP 的低速笛卡尔遥操网关。客户端只实现 `lm3-teleop.v1` WebSocket 协议，不直接连接 `pylebai` 或 WATER，也不能替代服务端安全网关、现场监护和物理急停。

## 安全行为

- 连接后必须收到 `session.welcome`、20 Hz 命令率、有效服务端看门狗和 `robot.state`，才允许申请控制权。
- 客户端按本地单调时钟记录最后一帧 `robot.state`；超过 1000 ms 未更新就禁止申请控制和执行动作，运动中会立即 `motion.stop` 并释放租约。
- 操作者必须逐项确认底盘停止、工作区清空、急停可触达、工具固定，然后持续按住 1.5 秒申请 2 秒租约。
- 手机姿态旋转遥操运行时必须先检测到硬件 `TYPE_GYROSCOPE`（或 `TYPE_GYROSCOPE_UNCALIBRATED`），再优先读取 Android `TYPE_GAME_ROTATION_VECTOR`（陀螺仪+加速度计融合），缺失时才回退 `TYPE_ROTATION_VECTOR`；没有陀螺仪的设备会禁用姿态模式。客户端不直接积分原始角速度，避免把积分漂移直接送给机械臂。`GAME_ROTATION_VECTOR` 不使用磁北，短时相对姿态平滑但 yaw 仍会随时间漂移，因此每次作业前必须显式归零。
- IMU 只控制 TCP 的 `Rx/Ry/Rz`，绝不把手机姿态当成 XYZ 平移。XYZ 仍由触屏单轴低速控制；这不是 ARCore 6DoF 位置跟踪。
- 归零约定为手机屏幕朝上、顶部指向机器人基坐标 `+X`。按 Android 设备轴右手正方向：绕顶部 `+Y` 映射 TCP `+Rx`，绕屏幕右侧 `+X` 映射 TCP `-Ry`，绕屏幕朝外 `+Z` 映射 TCP `+Rz`。该约定仍需在固定底盘、最低速度和空载状态下逐轴验向。
- 姿态 DEADMAN 按住期间最多以 20 Hz 发送 `pose.sample`。每次按住的首帧只做 priming；后续发送相邻成功发送姿态的最短旋转增量。低于 `0.8` 的置信度、非有限值、时间戳倒退、超过 250 ms 的采样间隔、超过 150 ms 的样本年龄、相对零位超过 30°、单次已发送增量超过 12°或传感器跳变都会失败关闭并要求重新归零。
- 已发送 `pose.sample` 的待确认序号会保留到对应 ACK/error 或会话关闭；即使操作者已经松开姿态 DEADMAN，延迟到达的拒绝仍会撤销租约并清除归零。待确认姿态命令超过 32 条时主动 `motion.stop`，不会静默丢弃旧序号。
- 触屏笛卡尔运动采用“先选择单一轴向，再按住绿色 DEADMAN”的方式。只有按住期间才以 20 Hz 发送 `motion.cartesian_velocity`，每帧 `duration_ms=100`。
- DEADMAN 松手、触摸取消、租约到期、安全项撤销、底盘未锁定、看门狗异常、急停/故障、服务端拒绝、App 进入后台和主动断开都会立即发送 `motion.stop`。
- App 进入后台还会停止录制并释放控制租约；回到前台必须重新完成长按解锁。
- `control.acquire` 会跟踪准确的请求序号；申请失败会立即清除 pending。收到 `LEASE_REQUIRED` 或 `LEASE_EXPIRED` 时，无论服务端是否标记 recoverable，都清除幽灵租约、待申请、运动 ACK、DEADMAN 和姿态发送状态，允许重新申请。
- 租约结束、断连或 App 进入后台时，界面会主动清除姿态归零；重新取得租约后必须再次点击“姿态归零”。USB 连接用途等系统窗口也可能触发 `onPause()`，返回 App 后应重新勾选四项、申请控制权并归零，不能沿用后台前的控制状态。
- 网络已经断开时无法再发送停机帧，客户端会清空本地控制状态，服务端必须依靠约 300 ms 的独立看门狗失败关闭。
- `motion.stop` 不要求租约；任何已完成 `session.hello` 的会话都可发送。

默认速度档均为开发期保守值，服务端限制始终具有更高优先级：

| 档位 | 线速度 | 角速度 |
| --- | ---: | ---: |
| 爬行 | 0.005 m/s | 0.02 rad/s |
| 低速 | 0.010 m/s | 0.05 rad/s |
| 谨慎上限 | 0.020 m/s | 0.10 rad/s |

首次真机仍须固定 UP 底盘、空载、小工作空间、最低速度并由现场人员直接掌握物理急停。

## 连接

界面唯一需要填写的是 WebSocket 地址。`client_name` 自动使用经过裁剪的 Android 设备型号（为空时回退为 `Android phone`），不会读取序列号、账号或其他敏感设备标识。客户端没有 token 输入框，`session.hello` 也不发送 `auth_token`。

- 其他非 `session.welcome seq=0` 首帧仍视为协议错误。
- 首个 `session.hello` 建立会话后，客户端会用 `session.welcome.server_time_ms` 自动校正后续出站时间。若之后仍出现 `STALE_MESSAGE`，表示网络延迟、会话中的时钟跳变或校正结果超过服务端配置；不要为真机随意放宽消息时效限制。

- release 构建只允许 `wss://`。
- debug 构建为了局域网开发允许明文，但应用层只接受环回、RFC1918、链路本地、无点局域网主机名或 `.local` 主机的 `ws://`。
- 当前桥接器没有应用层身份认证。明文规则不是安全边界；跨不可信网络时应在桥前部署带设备认证的 WSS/TLS 网关，并继续由服务端执行单写入者租约、动作限幅、工作空间/碰撞检查和看门狗。

## 协议摘要

所有帧都使用以下 envelope，`seq` 在单连接内从 `0` 开始严格递增：

```json
{
  "protocol": "lm3-teleop.v1",
  "type": "motion.stop",
  "seq": 0,
  "sent_at_ms": 1786200000000,
  "body": {
    "lease_id": "optional",
    "reason": "deadman_released"
  }
}
```

客户端发送：

- `session.hello`
- `control.acquire` / `control.release`
- `heartbeat`
- `motion.cartesian_velocity` / `motion.stop`
- `pose.sample`（`phone_calibrated` / `tcp_orientation`，仅旋转 3DoF）
- `gripper.set`
- `recording.start` / `recording.stop`

`gripper.set` 的按住操作只用于授权发送一次目标；松手或断线后的 `motion.stop` 不能保证中途停止夹爪。真机前必须单独验证 LMG-90 的停止/保持与解困流程。

服务端消息：

- `session.welcome`
- `control.status`
- `robot.state`
- `recording.status`
- `ack` / `error` / `safety.event`

`pose.sample.angular_delta_rad` 使用归零手机坐标中的相邻已发送姿态左差：`q_rel = inverse(q_zero) * q_current`，`q_delta = q_rel_current * inverse(q_rel_previous)`，再规范为最短旋转矢量并按 `[phone_y, -phone_x, phone_z] -> [tcp_rx, tcp_ry, tcp_rz]` 映射。`sensor_timestamp_ms` 来自设备单调时钟，而非 Unix 时间。

## 构建

需要完整 JDK 17+（必须包含 `bin/jlink.exe`）、Android SDK Platform 35 和 Build Tools 35.0.0。部分 IDE 附带的裁剪 JBR 没有 `jlink.exe`，不能用于该构建。仓库不提交 Android SDK、Gradle 缓存或签名秘密。

```powershell
$env:JAVA_HOME = 'D:\path\to\full-jdk-21'
$env:ANDROID_SDK_ROOT = 'D:\path\to\android-sdk'
Set-Location D:\Coding\lebai_yunji\teleop\android
.\gradlew.bat testDebugUnitTest assembleDebug
```

debug APK 默认输出：

```text
app/build/outputs/apk/debug/app-debug.apk
```

## 代码结构

- `protocol/Protocol.kt`：严格 envelope、消息解析和客户端 body 构造。
- `network/TeleopWebSocket.kt`：OkHttp WebSocket 连接与协议解码。
- `control/TeleopController.kt`：租约、安全门、20 Hz DEADMAN、心跳和失败关闭。
- `core/`：纯 Kotlin 的轴向、速度、安全门和网络策略，支持 JVM 单元测试。
- `core/OrientationTeleop.kt`：四元数归零、轴映射、姿态/时间戳/置信度门和相邻已发送增量。
- `sensor/PhoneOrientationSensor.kt`：Android 融合旋转矢量传感器选择与读取。
- `MainActivity.kt`：传统 Android View UI，不依赖 Flutter 或 Compose。

## 验证边界

单元测试和 APK 构建只验证客户端结构、协议编码、网络策略与安全门；它们不证明服务端兼容、无线网络时延或 LM3-UP 真机运动安全。连接真实机器人前还必须完成网关集成测试、断网/后台/拒绝/超时负向测试，以及现场低速空载验收。
