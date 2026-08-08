# LM3-UP 手机遥操与 VLA 数据链路

本目录提供一套面向 **乐白 LM3-UP 复合机器人机械臂**的原生手机遥操、失败关闭安全桥和示范数据导出实现。首版只覆盖 LM3 六轴机械臂与夹爪，**不向云迹 UP 底盘发送任何低层运动命令**。

当前已实现的范围（模拟后端、协议和结构性路径可在本机验证）：

- Android 原生 Kotlin 触屏遥操客户端；
- HarmonyOS 原生 ArkTS Stage 客户端；
- 默认模拟后端的 Python WebSocket 安全桥；
- 原始 episode、图像时间和 SHA-256 清单；
- 使用官方 LeRobot API 的 LeRobot v3 导出器实现，目标格式供 LingBot-VLA 后训练使用；当前尚未完成官方 LeRobot v0.4.2 + FFmpeg 在 Windows 上的完整导出/回读 E2E；
- 协议、租约、DEADMAN、限速、工作空间、关节限位和 watchdog 测试。

当前没有连接 LM3-UP 真机，也没有验证无线延迟、实体触控、机械臂运动、夹爪、相机同步或 LingBot 真机端到端执行。软件停止不能替代物理急停。

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
  现场检查 + 1500 ms 长按解锁 + 按住式 DEADMAN
                    |
                    | lm3-teleop.v1 / WebSocket / 20 Hz
                    v
Python 安全桥
  token + 单写入者租约 + 时序校验 + 限速 + TCP/关节边界
  + 300 ms watchdog + 原始示范录制
                    |
                    | 默认 SimulatorBackend
                    | 真机需配置与 CLI 双重显式启用
                    v
LM3 机械臂 / LMG-90 夹爪

UP 底盘：首版动作空间之外，由独立确定性流程停止并锁定
```

桥接器不会调用 `start_sys()`、`stop_sys()`、解除急停、关机、地图或底盘接口。任何已认证会话都可以请求停止；运动权限则只授予一个满足全部安全条件的租约持有者。

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

设置至少 16 个字符的随机共享 token，并只监听本机：

```powershell
$env:LM3_TELEOP_TOKEN = 'replace-with-a-random-secret'
& .\tmp\lm3-teleop-venv\Scripts\python.exe -m lm3_teleop_bridge serve `
  --config .\teleop\configs\lm3-up.sim.toml
```

默认入口为 `ws://127.0.0.1:8765/ws`。token 不应写入源码、Git、URL 或普通日志。

模拟配置中的相机段默认是注释状态，因此遥操与状态测试可直接运行，但 `recording.start` 会按设计拒绝未配置的相机名。要测试录制，先在本地 TOML 中显式配置 `camera_wrist`，可选再配置 `camera_top`，并安装 camera extra；当前没有“无图像 episode”录制模式。

## 手机端

Android 和 HarmonyOS 客户端具有相同的操作语义：

1. 输入网关 URL、终端名称和当次 token；
2. 收到兼容的 `session.welcome` 和新鲜 `robot.state`；
3. 确认底盘停止、工作区清空、急停可触达和工具固定；
4. 长按 1500 ms 申请控制租约；
5. 选择单一笛卡尔轴向，并持续按住 DEADMAN 点动；
6. 松手、切后台、断线、状态过期或安全事件时立即本地清零并尽力发送停止。

具体构建方式见 [`android/README.md`](android/README.md) 和 [`harmony/README.md`](harmony/README.md)。手机 6DoF `pose.sample` 只保留协议空间；完成两端独立标定、跳变检测、重定位处理和真机验收前不会发送或执行。

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
- 配置强 token，并在受控网络中使用 WSS/TLS；
- 配置文件 `hardware_enabled=true`，启动时再传 `--hardware`；
- 现场受训人员监护、物理急停可触达、最低速度、空载和小工作空间。

首次真机应依次完成离线回放、无动作影子模式、固定底盘低速空载和小工作空间单任务。完整记录见 [`../docs/真机联调与验收清单.md`](../docs/真机联调与验收清单.md)。
