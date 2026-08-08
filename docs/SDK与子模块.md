# SDK 与子模块指南

## 1. 初始化和验证

主仓库固定 14 个顶层子模块：13 个 `vendor/` SDK/参考仓库和 1 个 `Seeed-Studio/wiki-documents` 文档站快照。使用统一脚本初始化真正需要的嵌套依赖，并在本地检查父仓 `160000` gitlink、配置的 `origin` URL、固定提交、文件和已有 ROS 远端引用；`-VerifyOnly` 不联网验证上游可取性：

```powershell
& .\scripts\manage-sdks.ps1
```

验证而不更新：

```powershell
& .\scripts\manage-sdks.ps1 -VerifyOnly
```

脚本故意不对所有仓库执行递归初始化。`yunji/cloud/open-api` 上游错误地把生成目录记录为 Git link，却没有 `.gitmodules` 映射；真正需要初始化的嵌套模块只有 `lebai-sdk.rs/proto/lebai-proto`。

## 2. LM3-UP 选型矩阵

### 核心

| 路径 | 定位 | LM3-UP 建议 |
| --- | --- | --- |
| `vendor/lebai/sdk/lebai-sdk` | 乐白当前统一 SDK；C++、Python、.NET、Java | **机械臂正式主入口**。Python 优先使用 `pylebai.Robot`；控制器软件要求至少 3.1.5 |
| `vendor/lebai/sdk/lebai-proto` | 乐白 RPC/协议定义 | SDK 开发和协议核对使用；根目录无许可证，不能默认允许再发布 |
| `LM3-UP复合机器人开发资料/water_api手册.pdf` | WATER TCP API v1.8.9 | **UP 底盘的本地权威接口依据**。它不是代码仓库，生产客户端需要自行按现场分帧行为实现 |

### 可选

| 路径 | 适用场景 | 注意事项 |
| --- | --- | --- |
| `vendor/lebai/ros/lebai-ros-sdk` | ROS 2、MoveIt、机械臂模型 | `main` 是文档入口，代码在 `humble-dev`、`jazzy-dev`、`lyrical-dev`；没有 UP/Nav2/WATER 驱动 |
| `vendor/lebai/integrations/plugin` | RK 相机、标定、AprilTag、夹爪和视觉插件 | 与交付 RK 指南/LBD 配合使用；没有统一现场配置 |
| `vendor/lebai/examples/example` | Lua/Python 示例 | 示例参数不能直接用于真机 |
| `vendor/lebai/examples/jsonrpc-demo` | JSON-RPC 与 UI 示例 | 用于理解接口，不是 LM3-UP 整机控制器 |
| `vendor/lebai/sdk/lebai-sdk.rs` | Rust、Lua、AsyncIO、WASM/Node 等旧/扩展绑定 | 需要这些语言时使用；当前主项目优先统一 SDK 2.x |

### 实验参考，不能直接投产

| 路径 | 已确认问题 |
| --- | --- |
| `vendor/lebai/lerobot/lerobot_lebai` | 固定版本 `dfcbc92f7a46` 的 `lebai.py:76` 存在 Python `SyntaxError`；连接时自动 `start_sys()`、断开时自动 `stop_sys()`；没有校准、动作限幅或 UP 动作空间；关节动作分支会提前返回，不能同时执行夹爪动作 |
| `vendor/yunji/integrations/yunji_agv_skill` | 发布方是乐白而非云迹；监听线程与同步命令在同一 socket 上同时 `recv`，响应会竞争；硬假设换行分帧，但交付 WATER 手册没有定义终止符；不能作为生产控车层 |
| `vendor/lebai/integrations/OpenClawSkill` | 智能体 API 封装，缺少 LM3-UP 整机安全仲裁和根许可证 |

### 历史资料

- `vendor/lebai/legacy/lebai-dotnet-sdk`：旧独立 .NET SDK。
- `vendor/lebai/legacy/lebai-mindplus`：已归档 Mind+ Beta。
- `vendor/yunji/cloud/open-api`：旧机器人服务生云端业务 API，不是 UP/WATER 控制接口。

完整来源、许可证和排除项见 [vendor/README.md](../vendor/README.md)。

### Python 包名不要混用

- 当前统一 SDK 的安装包是 `pylebai`，应用层入口是 `from pylebai import Robot`。
- 一些较早或实验性仓库导入的是 `lebai_sdk`，对应 `lebai-sdk.rs` 的旧/扩展绑定，不是 `pylebai` 的同名替代。
- 当前 `lerobot_lebai` 的 `pyproject.toml` 依赖 `lebai-sdk`，源码也导入 `lebai_sdk`；除了语法错误外，迁移到统一 SDK 2.x 仍需要接口适配。
- 交付的 LMG-90 优先通过 LM3 控制器的 `set_claw/get_claw` 路径使用。统一 SDK 里的直接 RS485 `Gripper` 是另一条通信路径，未核对实物接线前不要同时启用。

## 3. 机械臂 Python 的安全起点

Windows 真机绑定优先使用 **CPython 3.11 或 3.12**，并确认下载到与解释器 ABI 匹配的官方 `pylebai` wheel；不要因为本机已有更新版 Python 就假定原生扩展可用。若安装了 Python Launcher，可使用：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install pylebai
```

若没有 `py` 命令，改用已安装的 Python 3.11/3.12 `python.exe` 完整路径创建虚拟环境。安装后必须在目标机器执行 `python -c "from pylebai import Robot"`，确认原生扩展可导入；仓库中的 SDK 源码目录不能代替已构建 wheel。

先运行只读示例，不启动系统、不发送运动：

```powershell
python .\examples\read_lm3_status.py --ip <现场实际LM3地址>
```

示例只读取系统信息、机器人状态和急停原因。任何 `start_sys()`、`movej()`、`movel()`、夹爪、IO 或场景调用都属于下一阶段，必须按 [真机联调与验收清单](真机联调与验收清单.md) 执行。

## 4. ROS 分支选择

| Ubuntu | ROS | 上游分支 |
| --- | --- | --- |
| 22.04 | ROS 2 Humble | `origin/humble-dev` |
| 24.04 | ROS 2 Jazzy | `origin/jazzy-dev` |
| 26.04 | ROS 2 Lyrical | `origin/lyrical-dev` |
| 20.04 | ROS Noetic / ROS 2 Galactic | 历史分支 `origin/noetic-dev` / `origin/galactic-dev` |
| 18.04 | ROS Melodic | 历史分支 `origin/melodic-dev` |

选择分支前先确认部署主机操作系统。切换子模块工作树会让主仓库显示子模块变化，不要误把临时 checkout 当成版本升级提交。

这些 ROS 分支只描述 LM3 机械臂侧，未包含 UP 底盘、Nav2/WATER、收纳柜、相机线缆或完整 LM3-UP 碰撞模型。夹爪模型也需要根据 LMG-90 的实际 CAD、TCP、负载和安装方向复核。

```powershell
git -C vendor/lebai/ros/lebai-ros-sdk switch --detach origin/humble-dev
```

恢复主仓库固定版本：

```powershell
git submodule update -- vendor/lebai/ros/lebai-ros-sdk
```

## 5. UP 客户端的最低生产要求

新实现不能照搬当前实验客户端，至少需要：

- 单一 socket 读取循环；由该循环解析并分派 `response`、`callback`、`notification`。
- 通过 `uuid` 关联请求和响应，导航任务另保存 `task_id`。
- 在现场抓包后确定编码、终止符、粘包/拆包和最大消息长度；在证据不足时使用有上限的增量 JSON 解码器。
- 超时、重连、心跳/状态轮询、最大缓冲保护和异常 JSON 处理。
- 一个底盘运动写入者；导航与 `joy_control` 不能由不同客户端并发控制。
- 断连、急停、错误码非零、地图变化和状态超时均失败关闭。
- `/api/set_params` 后回读；地图切换和软件重启后重新连接并确认地图。
- `joy_control` 本地看门狗和零速停止；不能依赖命令自然过期作为唯一安全机制。

在拿到现场 WATER 软件版本和通信抓包前，本仓库不声称已经提供可生产使用的 UP SDK。

## 6. 更新策略

- 普通开发使用主仓库固定的子模块提交，不追随上游浮动分支。
- 执行 `manage-sdks.ps1 -UpdateRemote` 后，脚本允许并报告父仓索引与新子模块 HEAD 的差异，但不会自动暂存 gitlink；必须用 `git diff --submodule=log` 逐仓库检查 API、许可证和兼容性，再只暂存确认过的路径。
- 不要在子模块内创建只存在本机的提交再让主仓库引用；远端克隆将无法取得该提交。
- 对上游缺陷应提交到可访问的 fork，或在主仓库维护独立适配层和补丁说明。
