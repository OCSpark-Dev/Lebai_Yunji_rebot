# LM3-UP Python 安全桥

该服务把 Android/HarmonyOS 的 `lm3-teleop.v1` WebSocket 意图转换为受限 LM3 动作，并记录可审计的原始示范。默认只运行模拟器；任何真机模式都需要配置文件和 CLI 双重显式启用。

## 已实现的安全层

- 首帧必须为 `session.hello`，每连接双向 `seq=0` 起严格递增；hello 只建立无租约会话，其 `sent_at_ms` 不做绝对时效校验，welcome 后所有消息恢复严格墙钟校验；`auth_token` 为旧客户端兼容字段，可缺省或为空，服务端不会校验；
- 单一控制租约、1500 ms 客户端长按声明和四项现场检查；租约配置/请求范围为 500–2000 ms，服务端上限始终为 2000 ms；
- 只有乐白官方状态码 `5 (IDLE)` 可取得租约，`7 (MOVING)` 和其他状态一律拒绝；
- 严格单槽 20 Hz token-bucket 限速，不允许客户端靠积攒令牌突发双发；另有 300 ms 独立 watchdog；
- 真机快照优先用一次 `get_kin_data()` 合并读取关节位置、速度和 TCP；连续运动可复用从采样开始计龄不超过 `200 ms` 且不超过 watchdog 的最新快照。快照选择、机器人/包络校验、消息时效复核、租约/安全 epoch 复核与 `speedl` 位于同一次后端锁内，状态轮询不能插入其间；首条 `speedl` 尚未返回时也受独立在途 watchdog 约束，超时通过不等待普通后端锁的 stop 通道撤租停止。
- `pose.sample` 已实现手机 Rotation Vector 的 TCP 旋转 3DoF 增量控制：只执行 `Rx/Ry/Rz`，强制 `X/Y/Z=0`；每个 `calibration_id` 首帧必须为零增量并只做 priming，非零 priming 失败关闭，切换标定时先停止旧运动；priming 帧同样占用 20 Hz motion token，不能靠反复换标定绕过限流；
- 姿态消息只接受精确 v1 schema、`phone_calibrated/tcp_orientation`、`tracking`、`confidence>=0.8`；同一标定内的传感器间隔限 `20 ms` 到 `min(300 ms, watchdog_ms)`，单帧旋转增量范数限 `0.25 rad`，派生输入角速度范数限 `6.0 rad/s`，之后仍按安全配置把实际角速度钳制到默认 `0.15 rad/s`；
- 连续有效的非零笛卡尔命令期间，累计达到可观测预期位移后仍没有与笛卡尔命令方向一致的 TCP 进度，也没有位移与反馈速度相互一致的关节进度，才撤租、停止并报告 `FEEDBACK_STALLED`；随机微抖、正交/反向噪声不算 TCP 进度，反复换向不能重置停滞窗口；
- `pose.sample` 派生角速度范数低于 `1e-3 rad/s` 时只 ACK、续租并记录 `duration_ms=0`，不调用 `speedl` 或 `stop_move`；普通 `motion.cartesian_velocity` 的显式全零速度仍会下发 `speedl(0)`，保留松手停止的控制器侧冗余；
- 会话建立后的过期/未来消息，以及所有乱序消息、非有限数值、错误 deadman、无租约和错误坐标系失败关闭；
- 速度向量范数钳制、有限持续时间、六轴限位余量和 TCP 当前/预测工作空间检查；
- 真机必须配置 TCP `Rx/Ry/Rz` 姿态中心和逐轴容差；授予租约、所有笛卡尔速度和每个 `pose.sample` 都按角度环绕后的最短距离检查当前姿态，非 priming 运动还检查按有限命令时长预测的姿态，越界返回 `ORIENTATION_LIMIT` 并走既有撤租/停止路径；
- 任何已完成 `session.hello` 的 `motion.stop` 都优先执行，即使其 seq 或 body 随后被判无效；非控制者发起停止时还会撤销当前控制租约，防止原控制端沿用旧租约恢复；
- 断连、租约过期、机器人状态异常或后端异常会先撤销控制权，再通过独立停止通道请求 `stop_move()`；
- Bridge 记录 session 打开/关闭、控制权申请 grant/deny、安全停止和协议错误的精简诊断日志，不记录完整控制帧或完整 lease；安全撤租后客户端补发旧 `control.release` 时幂等确认，避免用二级 `LEASE_REQUIRED` 掩盖首个安全原因，但其他会话的有效租约仍不可被释放；
- 不调用 `start_sys()`、`stop_sys()`、解除急停、关机或底盘控制接口；
- `gripper.set` 只在当前 TCP XYZ/姿态位于配置包络内才会下发；它的 deadman 只授权一次目标，当前没有经真机验证的夹爪中途停止路径，`stop_move` 不能据此视为会停止 LMG-90；
- 原始 episode、相机时间、异常标志和覆盖 episode 全部文件的 `manifest.sha256`；
- 使用官方 LeRobot v0.4.2 API 的可选 v3 导出器。

## 安装和模拟启动

在仓库根目录运行：

```powershell
python -m venv .\tmp\lm3-teleop-venv
& .\tmp\lm3-teleop-venv\Scripts\python.exe -m pip install -e ".\teleop\bridge[test]"

& .\tmp\lm3-teleop-venv\Scripts\python.exe -m lm3_teleop_bridge serve `
  --config .\teleop\configs\lm3-up.sim.toml
```

默认地址是 `ws://127.0.0.1:8765/ws`。实体手机无法访问宿主机环回地址；在完成隔离网络和 WSS 前，优先用本机测试客户端或模拟器验证。

桥接器当前不提供应用层身份认证。旧配置中的 `auth_token_env` 会被兼容读取但忽略，旧客户端发送的 `auth_token` 也不会参与鉴权。局域网监听仍需配置 `allow_lan=true` 并传 `--allow-lan`，但这只是监听地址的双重确认，不是访问控制；只能在可信、隔离的机器人网络中使用。需要跨不可信网络时，应在桥接器前部署带设备认证的 WSS/TLS 网关。

## 真机启动门槛

复制 `teleop/configs/lm3-up.hardware.template.toml` 为不提交的 `.local.toml`，完成以下所有字段：

- `robot_ip` 是现场发现的 LM3 控制器地址；
- 安装与当前 Python 版本匹配、已经构建完成的 `pylebai` wheel；如果使用 `pylebai_path`，它必须指向可导入 `pylebai` 且包含原生扩展 `l_master` 的构建产物目录，SDK 源码 `python` 目录本身不能直接使用；
- `base_locked=true` 目前只是启动前由独立底盘流程给出的现场声明，不是持续读取的 UP 硬件互锁；真机接入前必须补独立确定性互锁与状态刷新；
- `workspace_min_m`/`workspace_max_m` 是实际工具、夹具和场景下测得的 TCP 界限；
- `orientation_center_rad`/`orientation_tolerance_rad` 是当前 TCP 姿态的实测安全包络；逐轴容差必须大于 0 且不超过 `0.35 rad`，并完成工具、夹爪、相机和线缆的完整旋转扫掠验证；
- `joint_min_rad`/`joint_max_rad` 是厂商/现场确认的六轴软限位，并保留安全余量；
- `workspace_configured=true`、`orientation_configured=true`、`joint_limits_configured=true`、`hardware_enabled=true`；
- 现场人员已上使能、检查急停并掌握物理急停。

然后仍必须传 `--hardware`。如果监听局域网，还要配置 `allow_lan=true` 并传 `--allow-lan`。桥接器不会替操作者上使能或复位急停。

### 停止时序和现实边界

硬件后端的状态、`speedl` 和夹爪等普通调用仍经过 `pylebai`，SDK/HTTP 调用可能因 Windows 调度、Python 线程、网络或控制器响应而延迟。桥接器因此把停止从普通后端锁中分离，并默认直接向控制器 `3031` 端口发送 `stop_move` JSON-RPC，软件 deadline 为 `200 ms`（由 `emergency_stop_port` 和 `emergency_stop_timeout_ms` 配置）；只有收到匹配 JSON-RPC 版本、请求 ID 和 `result` 的成功响应才视为确认。超时日志会区分 `connect`、`request`、`first_byte` 和 `body` 阶段，并记录总耗时与配置 deadline。停止发生时还会使当前安全 epoch 失效；如果较早的 `speedl` 调用稍后才返回，桥接器会再次请求停止，且不会把旧命令重新标记为活动运动。

这仍然不是硬实时系统，也不是控制器的物理急停保证。真机使用必须同时保留有限的 `speedl` 持续时间、独立软件停止通道、可触达的物理急停和现场监护；不得把“200 ms 软件 deadline”解释为机械臂必然在 200 ms 内物理停住。

## 相机和原始数据

安装 OpenCV extra 后可在 TOML 中配置 `camera_top`/`camera_wrist` 的 UVC index、URL 或视频源：

```powershell
& .\tmp\lm3-teleop-venv\Scripts\python.exe -m pip install -e ".\teleop\bridge[camera]"
```

相机工作线程只更新最新 JPEG，控制/看门狗协程不会等待 UVC 读取。打开失败、读取失败和未配置都会写入原始帧的 `camera_status`，不会伪造图像。

两份模板默认没有启用相机段。桥接器会拒绝未配置或空的相机列表，因此默认模拟启动可验证控制但不能开始 episode 录制；录制前必须显式配置至少 `camera_wrist`（可选 `camera_top`）。当前不提供无图像录制模式。

每个完成 episode 包含：

```text
metadata.json
frames.jsonl
images/<camera>/...
manifest.sha256
```

验证完整性：

```powershell
python -m lm3_teleop_bridge verify .\teleop\data\raw\episode-id
```

校验器要求 manifest 非空、SHA-256 为 64 位十六进制、路径不得绝对化/穿越/逃逸或重复，且 `metadata.json`、`frames.jsonl` 和所有图像等实际文件必须被完整且精确覆盖。录制 `fps` 必须与服务端 `state_hz` 一致，避免元数据声称的采样率与真实状态循环不符。

## 导出 LeRobot v3

导出器调用 LeRobot v0.4.2 的 `LeRobotDataset.create/add_frame/save_episode/finalize`，不会手写 Parquet。它把当前实际状态映射为 `observation.state=[q1..q6,gripper]`，把下一帧实际状态映射为 `action`，并保留手机笛卡尔速度和原始时间特征。

```powershell
python -m pip install -e ".\teleop\bridge[export]"
python -m lm3_teleop_bridge export `
  --episode .\teleop\data\raw\episode-a `
  --episode .\teleop\data\raw\episode-b `
  --output D:\datasets\lm3-up-v1 `
  --repo-id local/lm3_up `
  --camera camera_top `
  --camera camera_wrist
```

导出器先验证 manifest、元数据帧数、连续 frame index、严格递增的墙钟/单调时间、有限状态/动作值及可解码且同路同尺寸的图像。未显式传入 `--camera` 时只选择所有 episode 共有的相机；共有集合为空会失败，不会静默生成 state-only 数据集。随后以服务端单调时间按 episode 声明的固定 FPS 使用整数时间网格做最近邻重采样，只生成不晚于原 episode 末端的目标时间；状态采样间隙或超过默认 100 ms 图像匹配窗口都会令导出失败。导出完成后会重新加载数据集，核对 episode/frame/FPS/features，并要求 `meta/stats.json` 存在且统计值全部有限。当前环境若不安装 LeRobot，可用 `--lerobot-source .\tmp\lerobot-source` 指向已审计的 v0.4.2 源码，但其 Python 依赖仍需安装。

## 测试

```powershell
python -m pytest .\teleop\bridge\tests
```

测试只使用 `SimulatorBackend` 和本地临时目录，不连接真机。

手机的 `sensor_timestamp_ms` 是设备启动时钟。桥接器能验证同一标定内严格递增和相邻间隔，但无法把它与服务端墙钟直接比较来独立证明样本的绝对年龄；手机端还必须用同源单调时钟检查传感器新鲜度。首个 `session.hello` 为了完成时钟偏差下的会话建立而豁免绝对时效校验，welcome 后服务端仍用信封 `sent_at_ms` 严格检查每条网络消息。

2026-08-09 已完成 LM3-UP 真机的只读状态、Android 局域网握手、当前静止 TCP 小包络加载、控制租约授予和超过 60 秒续租验证。随后现场曾按下运动 DEADMAN；旧客户端发送速率高于真机后端吞吐，积压帧触发 `STALE_MESSAGE` 并安全撤租，因此没有完成实体运动方向验收。事后多次复核六轴速度均为 0、TCP 无变化，也没有发送夹爪动作。部署单槽 ACK credit 客户端和有界快照 Bridge 后，又在当前静止 TCP 小包络内完成超过 2 分钟的 Android 真机静止持租验证，期间没有 `STALE_MESSAGE`、`LEASE_REQUIRED`、watchdog、撤租或断线。当前交付仍未完成实体运动方向、真机停机距离/时延、夹爪、Python 3.11 环境，以及官方 LeRobot v0.4.2 + FFmpeg 在 Windows 上的完整导出/回读端到端验证；这些项目通过前不得宣称真机运动或训练数据流水线已经验收。

后续极小姿态测试确认控制器收到 `speedl` 且 TCP 有小幅真实变化，但旧停滞判据随后误撤租；本版已加入姿态死区、累计方向性反馈进度、换向冻结反馈、显式零速冗余和租约恢复回归。新版尚未完成第二次非零真机复验，因为复验前现场控制器的控制页和 JSON-RPC 同时停止响应；Bridge 未在该状态下启动。
