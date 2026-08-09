# LM3-UP 手机遥操与 VLA 数据链路

本目录提供一套面向 **乐白 LM3-UP 复合机器人机械臂**的原生手机遥操、失败关闭安全桥和示范数据导出实现。首版只覆盖 LM3 六轴机械臂与夹爪，**不向云迹 UP 底盘发送任何低层运动命令**。

当前已实现的范围（模拟后端、协议和结构性路径可在本机验证）：

- Android 原生 Kotlin 客户端：系统 Rotation Vector 控制 TCP 姿态，触屏控制 XYZ 平移；
- HarmonyOS 原生 ArkTS Stage 客户端：`SensorId.ROTATION_VECTOR` 控制 TCP 姿态，触屏控制 XYZ 平移；
- 默认模拟后端的 Python WebSocket 安全桥；
- 原始 episode、图像时间和 SHA-256 清单；
- 使用官方 LeRobot API 的 LeRobot v3 导出器实现，目标格式供 LingBot-VLA 后训练使用；当前尚未完成官方 LeRobot v0.4.2 + FFmpeg 在 Windows 上的完整导出/回读 E2E；
- 协议、租约、DEADMAN、限速、工作空间、关节限位和 watchdog 测试。

2026-08-09 已完成一轮 LM3-UP 真机联调：Windows Bridge 通过 `pylebai` 读取到控制器 `IDLE`、六轴零速和当前 TCP；Android 真机通过局域网 WebSocket 完成握手，按当前静止姿态刷新本地小包络后，控制租约已实际授予并连续续租超过 60 秒。静止持租验证后，现场曾按下运动 DEADMAN；旧客户端以 20 Hz 发送、而真机状态读取约需 139 ms，积压帧触发 `STALE_MESSAGE` 并安全撤租，因此本轮没有完成运动方向验收。事后多次复核均为六轴零速、TCP 无变化，也没有发送夹爪动作。部署单槽 ACK credit 客户端和有界快照 Bridge 后，又在当前静止 TCP 小包络内完成超过 2 分钟的 Android 真机静止持租验证，期间没有 `STALE_MESSAGE`、`LEASE_REQUIRED`、watchdog、撤租或断线；尚未据此宣称非零运动已经验收。

同日后续极小姿态验向确认 `speedl` 已到达机械臂，TCP 实际发生约 0.15 mm / 0.00077 rad 的小幅变化；旧反馈逻辑随后把低速 IMU 噪声和不足采样分辨率的变化误判为 `FEEDBACK_STALLED` 并撤租，手机收到的 `LEASE_REQUIRED` 是撤租后的连锁结果。当前 Bridge 已改为累计“可观测的预期位移”，只认可与笛卡尔命令方向一致的 TCP 进度，或位移与反馈速度相互一致的关节进度；姿态角速度范数低于 `1e-3 rad/s` 时只 ACK/续租而不调用 `speedl`，方向切换也不能反复重置停滞窗口。Android/HarmonyOS 同时修复了失败申请和租约错误后的幽灵 pending/lease 状态。新版 Android APK 已安装到现场手机，但最终非零方向复验尚未完成：复验前控制器的网页和 JSON-RPC 服务出现“TCP 可连接但不返回数据”，`pylebai` 报未连接，因此 Bridge 按设计没有启动，等待受控恢复控制器服务后继续。

当前仍未验收实体机械臂运动方向、无线运动时延/停止距离、夹爪中途停止、相机同步或 LingBot 真机端到端执行。软件停止不能替代物理急停。

夹爪特别说明：当前 `gripper.set` 的 DEADMAN 只授权发送一次目标；松手、切后台或断线时发送的 `motion.stop/stop_move` 只针对机械臂轨迹，不能承诺中途停止 LMG-90。完成真机停止/保持、夹持力和人工解困验证前，夹爪控制只视为实验接口。

## 目录

| 路径 | 用途 |
| --- | --- |
| [`PROTOCOL.md`](PROTOCOL.md) | `lm3-teleop.v1` WebSocket 协议与失败关闭规则 |
| [`bridge/`](bridge/) | Python 安全桥、模拟/真机后端、录制与 LeRobot 导出 |
| [`android/`](android/) | Android 原生 Kotlin 客户端 |
| [`harmony/`](harmony/) | HarmonyOS 原生 ArkTS 客户端 |
| [`configs/`](configs/) | 模拟器、真机模板和 LingBot 维度映射 |
| [`NOTICE.md`](NOTICE.md) | 上游参考项目与许可证说明 |

整体原理、Seeed/Phosphobot 调研和训练路线见 [`../docs/手机遥操与VLA数据采集.md`](../docs/手机遥操与VLA数据采集.md)。

## 安全架构

```text
Android / HarmonyOS
  现场检查 + 1500 ms 长按解锁
  Rotation Vector + 归零 + 按住式陀螺仪 DEADMAN
  触屏轴键 + 原有平移 DEADMAN
                    |
                    | lm3-teleop.v1 / WebSocket / 20 Hz
                    v
Python 安全桥
  会话握手 + 单写入者租约 + 时序校验 + 限速 + TCP/关节边界
  + 300 ms watchdog + 原始示范录制
                    |
                    | 默认 SimulatorBackend
                    | 真机需配置与 CLI 双重显式启用
                    v
LM3 机械臂 / LMG-90 夹爪

UP 底盘：首版动作空间之外，由独立确定性流程停止并锁定
```

桥接器不会调用 `start_sys()`、`stop_sys()`、解除急停、关机、地图或底盘接口。任何已完成 `session.hello` 的会话都可以请求停止；运动权限则只授予一个满足全部安全条件的租约持有者。

## 快速验证

在仓库根目录执行统一脚本：

```powershell
& .\scripts\verify-teleop.ps1
```

脚本依次执行：

1. SDK 子模块固定状态检查；
2. Python `compileall` 和安全桥测试；
3. Android JVM 测试、debug APK 构建和 lint；
4. HarmonyOS 静态协议检查和带 ArkTS 类型检查的 unsigned HAP 构建；
5. `git diff --check`。

Android 构建需要完整 JDK 17+（包含 `jlink.exe`）、Android SDK Platform 35 和 Build Tools 35.0.0。HarmonyOS 构建需要 DevEco Studio 及 SDK `6.1.1(24)`。脚本优先使用环境变量，并能识别本仓库 `tmp/` 下的便携 Android 工具和 DevEco Studio 默认安装位置；这些本机依赖均被 Git 忽略。

只做不依赖移动端 SDK 的桥接器验证：

```powershell
& .\scripts\verify-teleop.ps1 -SkipSdkCheck -SkipAndroid -SkipHarmonyBuild
```

## 模拟桥启动

首次创建隔离 Python 环境：

```powershell
python -m venv .\tmp\lm3-teleop-venv
& .\tmp\lm3-teleop-venv\Scripts\python.exe -m pip install -e ".\teleop\bridge[test]"
```

直接启动并只监听本机：

```powershell
& .\tmp\lm3-teleop-venv\Scripts\python.exe -m lm3_teleop_bridge serve `
  --config .\teleop\configs\lm3-up.sim.toml
```

默认入口为 `ws://127.0.0.1:8765/ws`。当前桥接器不提供应用层 token 认证，客户端建立连接时只需 WebSocket 地址。

模拟配置中的相机段默认是注释状态，因此遥操与状态测试可直接运行，但 `recording.start` 会按设计拒绝未配置的相机名。要测试录制，先在本地 TOML 中显式配置 `camera_wrist`，可选再配置 `camera_top`，并安装 camera extra；当前没有“无图像 episode”录制模式。

## 手机端

Android 和 HarmonyOS 客户端具有相同的操作语义：

1. 只输入网关 WebSocket URL；终端名称由客户端根据设备型号自动生成；
2. 收到兼容的 `session.welcome` 和新鲜 `robot.state`；
3. 确认底盘停止、工作区清空、急停可触达和工具固定；
4. 长按 1500 ms 申请控制租约；
5. 旋转遥操要求手机实际存在硬件陀螺仪；客户端门禁通过后再点“陀螺仪归零”，保持当前手机姿态作为中位，然后持续按住陀螺仪 DEADMAN；
6. 标准握持为屏幕朝上、手机顶部指向机器人基坐标 `+X`，固定映射 `[tcp_rx,tcp_ry,tcp_rz]=[phone_y,-phone_x,phone_z]`；首次使用必须在模拟器逐轴验向；
7. 手机相邻姿态增量以最多 20 Hz 控制 TCP `Rx/Ry/Rz`；触屏轴键继续控制 `X/Y/Z`，两种运动输入不能并发；
8. 松手、切后台、断线、传感器陈旧/跳变、状态过期或安全事件时立即本地清零并尽力发送停止。

具体构建方式见 [`android/README.md`](android/README.md) 和 [`harmony/README.md`](harmony/README.md)。`pose.sample` 已用于手机 3DoF 旋转增量，不包含手机平移。Rotation Vector 是系统融合姿态；它不能替代 ARKit/ARCore/WebXR 的空间位置跟踪，也不能被描述成完整 6DoF。

### 控制权立即丢失或出现 `LEASE_REQUIRED`

- 先看 App 保留的首个 `control.status` / `safety.event`，以及 Bridge 的 `control.acquire`、`safe_stop`、`protocol.error` 日志；heartbeat 和自动清理 ACK 不再覆盖首因。
- 如果机械臂曾被手动拖动或重新上电后姿态改变，旧 `.local.toml` 中围绕上一次 TCP 采样的小工作空间/姿态包络会立即失效。必须在机械臂静止、底盘锁定、现场安全时重新采样当前 TCP，更新本地包络并重启 Bridge；仅修改文件而不重启不会更新运行中配置。
- 真机后端明显慢于手机 20 Hz 输入时，旧客户端会把运动帧排进 WebSocket，最终先触发 `STALE_MESSAGE`、再因安全撤租出现 `LEASE_REQUIRED`。Android/HarmonyOS 现对笛卡尔速度与姿态共用一个 ACK credit，同一时刻最多一条连续运动帧在途；Bridge 使用有界新鲜快照、在同一后端锁内完成校验和 `speedl`，并在执行前重新检查消息年龄与首帧在途 watchdog。
- 安全层先撤租后，旧客户端继续发送 `control.release` 曾会收到二级 `LEASE_REQUIRED`，容易被误认为首因。当前 Bridge 已把“当前无租约”的旧 release 作为幂等成功处理，但绝不会允许一个会话释放其他会话的有效租约。
- `FEEDBACK_STALLED` 表示 Bridge 已累计到足以观察的非零命令，但没有看到与笛卡尔命令方向一致的 TCP 进度，也没有看到位移与反馈速度相互一致的关节进度。当前实现不会让 `1e-6` 级随机抖动、正交/反向噪声或反复换向掩盖冻结反馈；低于 `1e-3 rad/s` 的姿态噪声只续租，不会触发机械臂命令。
- Android/HarmonyOS 收到 `LEASE_REQUIRED` 或 `LEASE_EXPIRED` 后会无条件本地失败关闭并清除待申请、租约、在途 ACK、DEADMAN 和姿态基线，因此可以重新申请。重新取得控制权后必须再次“姿态归零”；如果期间出现 USB 系统窗口、切后台或断连，还必须重新勾选四项现场确认。这是失败关闭规则，不是按钮卡死。
- 如果 `ping` 正常、80/3030/3031 端口能建立 TCP，但控制页、JSON-RPC 和 `pylebai` 都超时，应按“控制器服务不响应”处理：不要启动 Bridge 或放宽 watchdog；取得明确授权后按现场流程恢复控制器服务，再重新只读确认 `IDLE`、急停为空、六轴零速和 TCP 位于本地包络。

## 数据与 LingBot-VLA

桥接器先保存可审计的原始 episode，再离线导出 LeRobot v3。训练映射为：

```text
observation.state = [q1, q2, q3, q4, q5, q6, gripper]
action            = 下一帧实际 [q1, q2, q3, q4, q5, q6, gripper]
```

机械臂维度建议 `subtract_state: true`，夹爪维度使用 `subtract_state: false`。被拒绝、钳制或未实际到达的手机命令不能作为训练真值。LingBot 推理建议运行在外部 NVIDIA/CUDA 主机，并且只能把候选动作送入同一安全桥；公开 RoboTwin 权重不能直接驱动 LM3-UP。

训练配置必须与导出的图像 feature 完全一致：双相机 episode 使用 [`configs/lingbot_lm3_up.yaml`](configs/lingbot_lm3_up.yaml)；只有 `camera_wrist` 的 episode 使用 [`configs/lingbot_lm3_up_wrist_only.yaml`](configs/lingbot_lm3_up_wrist_only.yaml)。不要用双相机 YAML 训练缺少 `camera_top` 的数据集。

## 真机启用边界

真机模式必须同时满足：

- 从模板创建不提交的本地配置，填入现场确认的 LM3 地址；
- 配置真实 TCP 工作空间、六轴软限位和安全余量；
- 由独立流程确认 UP 底盘停止并锁定；
- 当前桥接器无应用层认证，只允许部署在受控机器人网络；跨不可信网络时在桥前增加带设备认证的 WSS/TLS 网关；
- 配置文件 `hardware_enabled=true`，启动时再传 `--hardware`；
- 现场受训人员监护、物理急停可触达、最低速度、空载和小工作空间。

首次真机应依次完成离线回放、无动作影子模式、固定底盘低速空载和小工作空间单任务。完整记录见 [`../docs/真机联调与验收清单.md`](../docs/真机联调与验收清单.md)。
