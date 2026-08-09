# LM3-UP 手机遥操协议 v1

协议标识：`lm3-teleop.v1`

本协议连接 Android/HarmonyOS 客户端与 LM3-UP Python 安全桥。它只定义机械臂、夹爪、状态和数据采集；**不定义 UP 底盘速度控制**。

## 1. 传输与部署

- 传输：UTF-8 JSON over WebSocket。
- 开发默认：`ws://127.0.0.1:8765/ws`，只能访问本机模拟器。
- 局域网：必须显式更改监听地址、设置高强度共享令牌，并把机器人、GPU 主机和手机放入受控网络。
- 生产：使用反向代理提供 WSS/TLS、设备级凭据、访问日志和网络隔离。令牌不能在 URL 查询参数中传递。
- 一个 WebSocket 文本帧只包含一个完整 JSON 消息；首版不接受二进制控制消息。

## 2. 通用信封

每个消息都包含以下字段：

```json
{
  "protocol": "lm3-teleop.v1",
  "type": "session.hello",
  "seq": 0,
  "sent_at_ms": 1786200000000,
  "body": {}
}
```

| 字段 | 规则 |
| --- | --- |
| `protocol` | 必须精确等于 `lm3-teleop.v1` |
| `type` | 下文定义的消息类型 |
| `seq` | 每条连接从 0 开始严格递增；重复或倒退的控制消息被拒绝并停止运动 |
| `sent_at_ms` | Unix epoch 毫秒；桥接器允许配置范围内的时钟偏差，但用服务端单调时钟管理租约和看门狗 |
| `body` | 对应类型的数据对象 |

所有速度、位置和角度必须是有限数值。`NaN`、`Infinity`、字符串数字和缺字段都属于协议错误。

## 3. 会话与认证

连接后的第一条消息必须是：

```json
{
  "protocol": "lm3-teleop.v1",
  "type": "session.hello",
  "seq": 0,
  "sent_at_ms": 1786200000000,
  "body": {
    "client_id": "android-7f8d...",
    "client_name": "LM3-UP Android Teleop",
    "platform": "android",
    "app_version": "0.1.0",
    "auth_token": "replace-with-a-random-secret",
    "capabilities": ["cartesian_velocity", "pose_sample", "gripper", "recording"]
  }
}
```

认证成功后服务端返回 `session.welcome`：

```json
{
  "protocol": "lm3-teleop.v1",
  "type": "session.welcome",
  "seq": 0,
  "sent_at_ms": 1786200000012,
  "body": {
    "session_id": "5ed2...",
    "server_time_ms": 1786200000012,
    "mode": "simulator",
    "watchdog_ms": 300,
    "command_rate_hz": 20,
    "base_locked": true,
    "limits": {
      "max_linear_mps": 0.03,
      "max_angular_rps": 0.15,
      "max_command_duration_ms": 150,
      "workspace_min_m": [0.10, -0.60, 0.02],
      "workspace_max_m": [0.80, 0.60, 0.80],
      "orientation_configured": true,
      "orientation_center_rad": [-1.54, 0.03, 1.56],
      "orientation_tolerance_rad": [0.05, 0.05, 0.05],
      "orientation_gimbal_lock_margin_rad": 0.10
    }
  }
}
```

认证失败时服务端返回 `error` 后关闭连接。客户端不得在持久日志中输出 `auth_token`。

## 4. 控制租约

机械臂只有一个运动写入者。客户端完成现场检查并持续长按解锁至少 1500 ms 后发送：

```json
{
  "protocol": "lm3-teleop.v1",
  "type": "control.acquire",
  "seq": 1,
  "sent_at_ms": 1786200001500,
  "body": {
    "requested_lease_ms": 2000,
    "operator_hold_ms": 1500,
    "safety_ack": {
      "base_stationary": true,
      "workspace_clear": true,
      "estop_accessible": true,
      "tool_secure": true
    }
  }
}
```

`requested_lease_ms` 最小为 500 ms；服务端配置和协议请求的最大有效值均为 2000 ms，超过上限不会得到更长租约。只有机器人反馈乐白官方状态码 `5 (IDLE)`、无急停、底盘已确认锁定、现场限位配置完整，并且当前 TCP XYZ 与 `Rx/Ry/Rz` 都位于已配置运动包络内时才会授予租约；状态码 `7 (MOVING)` 不能取得新租约。安全停止执行期间也拒绝新租约。当前 TCP 不在包络内时返回 `control.status granted=false`，`reason="robot_not_within_configured_motion_envelope"`。

服务端返回：

```json
{
  "protocol": "lm3-teleop.v1",
  "type": "control.status",
  "seq": 1,
  "sent_at_ms": 1786200001510,
  "body": {
    "granted": true,
    "lease_id": "a1c4...",
    "owner_client_id": "android-7f8d...",
    "expires_at_ms": 1786200003510,
    "reason": "granted"
  }
}
```

`control.release` 的 `body` 为 `{ "lease_id": "..." }`。连接关闭、App 进入后台、租约过期或安全事件都会释放租约并执行停止。

`heartbeat` 可保持空闲会话，但**不能保持运动**：

```json
{"lease_id":"a1c4...","deadman":false}
```

运动看门狗只由有效的、`deadman=true` 的运动命令刷新。

## 5. 机械臂运动

客户端仅在操作者持续按住 deadman 时，以最多 20 Hz 发送 `motion.cartesian_velocity`。服务端使用容量为 1 的 token bucket，两个命令不能在同一时刻突发通过；每接受一条后必须等待令牌按配置速率重新生成：

```json
{
  "protocol": "lm3-teleop.v1",
  "type": "motion.cartesian_velocity",
  "seq": 25,
  "sent_at_ms": 1786200002500,
  "body": {
    "lease_id": "a1c4...",
    "deadman": true,
    "frame": "base",
    "linear_mps": {"x": 0.01, "y": 0.0, "z": 0.0},
    "angular_rps": {"rx": 0.0, "ry": 0.0, "rz": 0.0},
    "duration_ms": 100
  }
}
```

规则：

- 首版只接受 `frame="base"`。
- 服务端再次钳制线速度、角速度和持续时间，客户端数值不构成安全依据。
- 当前或预测 TCP XYZ 将离开配置工作空间时拒绝命令并停止，错误码为 `WORKSPACE_LIMIT`。
- 当前或预测 TCP `Rx/Ry/Rz` 将离开配置姿态包络时拒绝命令、撤销租约并停止，错误码为 `ORIENTATION_LIMIT`。
- 持有租约或正在运动时，后台状态反馈一旦离开位置或姿态包络，也立即失败关闭并撤销租约。
- 300 ms 内未收到新的有效运动命令时调用后端停止。
- 连续有效的非零运动命令期间，若关节与 TCP 反馈持续无可观测变化超过 `feedback_stall_ms`，撤销租约并停止；该软件检测仍须用真机标定阈值和停机行为。
- 桥接器不得把连续命令无界堆入乐白异步运动缓冲。
- UI 松手时立即发送零速和 `motion.stop`，不能等待下一个定时周期。

姿态包络由实测 `orientation_center_rad=[rx,ry,rz]` 和逐轴 `orientation_tolerance_rad` 定义。当前角度与中心的比较使用 `atan2(sin(delta), cos(delta))` 得到跨 `+π/-π` 的最短角距离；预测使用“当前最短角偏差 + 已限幅角速度 × 最终有限命令时长”，预测路径不得在离开包络后靠绕一圈重新进入而被接受。乐白这里的 `Rx/Ry/Rz` 是 Euler ZYX，配置包络和运行时反馈都排除距离 `ry=±π/2` 小于等于 `0.10 rad` 的奇异区。`current Euler + rate * duration` 只是低速、有限小步的保守近似，不是完整 SO(3) 积分、碰撞模型或工具/线缆扫掠证明；真机仍须逐轴低速验证控制器角速度语义。

`motion.stop` 的 `body`：

```json
{"lease_id":"a1c4...","reason":"deadman_released"}
```

任何已认证会话都可以请求停止；没有控制租约也不能阻止紧急软件停止。若停止来自当前租约之外的已认证会话，服务端同时撤销现有租约，原控制端必须重新完成授权才能继续。

在硬件模式下，安全桥不等待普通 `pylebai` 后端锁，而是默认通过控制器 `3031` 端口的独立 HTTP JSON-RPC 连接请求 `stop_move`，默认软件 deadline 为 200 ms。该 deadline 只约束桥接器的停止请求等待时间；Windows、Python、网络和机器人控制器都不是硬实时链路，不能据此保证 200 ms 内物理停止。普通 `pylebai` RPC 也可能延迟，因此有限命令持续时间、独立 stop、物理急停和现场监护必须同时存在。

## 6. 夹爪

```json
{
  "protocol": "lm3-teleop.v1",
  "type": "gripper.set",
  "seq": 31,
  "sent_at_ms": 1786200002800,
  "body": {
    "lease_id": "a1c4...",
    "deadman": true,
    "position_pct": 45.0
  }
}
```

`position_pct` 范围为 0–100。夹爪动作同样要求有效租约、deadman，以及当前 TCP XYZ/姿态仍位于配置运动包络内；越界时不调用夹爪，并按 `WORKSPACE_LIMIT` 或 `ORIENTATION_LIMIT` 停止、撤租。这里的 deadman 只授权**下发一次目标**。当前已确认的 `motion.stop/stop_move` 只停止机械臂运动，尚未验证能中途停止 LMG-90 夹爪，因此松手、后台或断线不能被解释为夹爪会立即停住。服务端记录命令与实际反馈，不能把“命令已接受”当作“夹爪已到位”；真机启用夹爪前必须验证停止/保持策略、夹持力、间隙和人工解困流程。

## 7. 示范录制

开始：

```json
{
  "protocol": "lm3-teleop.v1",
  "type": "recording.start",
  "seq": 40,
  "sent_at_ms": 1786200010000,
  "body": {
    "lease_id": "a1c4...",
    "task": "拿起红色方块放入左侧盒子",
    "episode_id": "optional-human-readable-id",
    "cameras": ["camera_top", "camera_wrist"]
  }
}
```

停止：

```json
{"lease_id":"a1c4...","reason":"task_complete"}
```

桥接器返回 `recording.status`，字段至少包括 `recording`、`episode_id`、`frame_count`、`started_at_ms`、`path` 和可选 `reason`。运动停止与录制停止是两个动作：发生安全停止时录制应保留，并在元数据中标记异常，不应静默删除。

`cameras` 中的名称必须唯一、安全且已在服务端配置；首版不能请求任意文件路径或未配置相机。完成 episode 的 `manifest.sha256` 必须精确覆盖元数据、帧记录和全部图像，不能包含绝对路径、目录穿越、重复项、自引用或逃逸链接。离线导出以服务端单调时间按声明 FPS 做固定频率最近邻重采样；时间不单调、采样间隙、manifest 不完整或图像不可解码时必须失败关闭。

## 8. 状态、确认与错误

`robot.state` 的 `body`：

```json
{
  "robot_state": "IDLE",
  "estop_reason": "",
  "joint_position_rad": [0, 0, 0, 0, 0, 0],
  "joint_velocity_rad_s": [0, 0, 0, 0, 0, 0],
  "tcp_pose": {"x":0.4,"y":0.0,"z":0.3,"rx":0.0,"ry":3.14,"rz":0.0},
  "gripper_pct": 50.0,
  "base_locked": true,
  "watchdog_ok": true,
  "recording": false
}
```

`ack` 的 `body`：

```json
{"ack_seq":25,"ack_type":"motion.cartesian_velocity","accepted":true,"clamped":false,"detail":""}
```

`error` 的 `body`：

```json
{"ack_seq":25,"code":"LEASE_REQUIRED","message":"valid control lease required","recoverable":true}
```

`safety.event` 的 `body`：

```json
{"severity":"error","code":"WATCHDOG_TIMEOUT","message":"no valid motion command for 300 ms","action":"stop"}
```

常用错误码：`AUTH_FAILED`、`PROTOCOL_MISMATCH`、`INVALID_MESSAGE`、`OUT_OF_ORDER`、`STALE_MESSAGE`、`RATE_LIMITED`、`LEASE_BUSY`、`LEASE_REQUIRED`、`LEASE_EXPIRED`、`DEADMAN_REQUIRED`、`BASE_NOT_LOCKED`、`ROBOT_NOT_READY`、`WORKSPACE_LIMIT`、`ORIENTATION_LIMIT`、`UNSUPPORTED_MODE` 和 `BACKEND_ERROR`。服务端在任一路径发现过期租约时都会先执行统一安全停止、撤租并清除姿态基线；`acquire`/`renew` 不得静默吞掉过期事件。

## 9. 手机 Rotation Vector 3DoF 增量控制

能力名为 `pose_sample`，消息类型为 `pose.sample`。Android/HarmonyOS 客户端在启用该模式前必须确认设备存在硬件陀螺仪，缺失时不得发送姿态命令；随后才把系统融合后的 Rotation Vector 转换为 LM3 TCP 的 `Rx/Ry/Rz` 增量控制。该模式不接受手机位置，也不控制机械臂 `X/Y/Z` 或 UP 底盘。它不是完整 6DoF 空间跟踪，不能把 IMU 姿态描述成 ARCore/WebXR 一类的位置跟踪。

`body` 必须**精确**包含以下字段，不允许缺字段或扩展字段：

```json
{
  "lease_id": "a1c4...",
  "deadman": true,
  "frame": "phone_calibrated",
  "mapping": "tcp_orientation",
  "calibration_id": "9f31...",
  "sensor_timestamp_ms": 123456,
  "tracking_state": "tracking",
  "confidence": 0.95,
  "angular_delta_rad": {"rx": 0.01, "ry": 0.0, "rz": 0.0}
}
```

客户端计算规则：

1. 操作者显式归零后，计算 `q_rel = inverse(q_zero) * q_current`。
2. 对相邻**成功写入 WebSocket** 的相对姿态计算 `q_delta = q_rel_current * inverse(q_rel_previous)`。
3. 将四元数规范到 `w >= 0`，再转换为最短旋转矢量。
4. 中性安装约定为手机屏幕朝上、手机顶部指向机器人基座 `+X`；手机旋转 `[x,y,z]` 映射为 TCP `[rx,ry,rz]=[y,-x,z]`。现场仍必须低速核对控制器轴向和手机安装方向。
5. 每次按下姿态 DEADMAN 或生成新的 `calibration_id` 后，首帧必须发送零增量，只用于 priming；只有发送成功后，客户端才能提交该帧为下一次差分基线。随后若收到拒绝、超时或断连，必须失败关闭并丢弃该基线。

桥接器固定执行以下服务端检查：

- `deadman=true`，持有有效控制租约，`frame="phone_calibrated"`，`mapping="tcp_orientation"`。
- `calibration_id` 非空、首尾无空白且最长 128 个字符；`tracking_state="tracking"`；`confidence` 在 `[0.8,1.0]`。
- `sensor_timestamp_ms` 是手机启动时钟的正整数。在同一 `calibration_id` 内必须严格递增，相邻已接受样本间隔必须在 `20-150 ms`。
- `angular_delta_rad` 必须精确包含 `rx/ry/rz` 三个有限数值，单帧范数不得超过 `0.25 rad`；由 `delta / interval` 得到的输入角速度范数不得超过 `6.0 rad/s`。
- 所有 `pose.sample`，包括 priming 帧，都占用同一个容量为 1 的 20 Hz motion token bucket；不断切换 `calibration_id` 不能绕过限流。
- 首帧或新 `calibration_id` 必须携带零旋转增量，只建立基线，不调用 `speedl`；但桥接器仍会重新读取真机快照并检查当前 XYZ/姿态包络，越界同样失败关闭。非零 priming 会失败关闭。若切换标定时已有运动，桥接器先执行软件停止，保留原租约，再重新检查包络并建立新基线。
- 后续帧将 `X/Y/Z` 强制为零，以传感器间隔作为有限命令时长，再经过与 `motion.cartesian_velocity` 相同的角速度限幅、机器人/关节状态、TCP 当前/预测工作空间、TCP 当前/预测姿态包络、安全 epoch、watchdog 和反馈停滞检查。默认运行角速度上限仍是安全配置中的 `0.15 rad/s`；`6.0 rad/s` 只是拒绝异常输入的前置上限，不是可执行速度。

任一 schema、deadman、租约、时间、置信度、跳变、限流、安全检查或后端执行错误都会失败关闭：停止机械臂、撤销租约、清除姿态基线，并返回 `error`。低置信度、传感器跳变、页面隐藏、App 后台、松开 DEADMAN 或断连时，客户端也必须立即清除本地基线并尽力发送 `motion.stop`。

`sensor_timestamp_ms` 与服务端墙钟没有共同纪元，因此服务端只能验证同一标定内的顺序和间隔，不能独立证明传感器样本的绝对年龄。客户端必须用本机同源单调时钟做样本年龄检查；服务端另外只用信封 `sent_at_ms` 检查网络消息时效。两者不能互相替代。

## 10. 当前验证边界

协议和桥接器自动化测试仅覆盖模拟后端与本地临时数据，不连接 LM3-UP。当前尚未完成 Python 3.11、LM3-UP 真机，以及官方 LeRobot v0.4.2 + FFmpeg Windows 导出/回读端到端验证；这些项目必须在独立验收记录中给出实际环境、日志、停止时延和失败路径证据。
