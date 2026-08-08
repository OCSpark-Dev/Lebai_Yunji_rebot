# 乐白与云迹公开 SDK / 集成仓库清单

核验日期：2026-08-09

本目录使用 **Git 子模块** 保存上游仓库。主仓库记录每个子模块的固定提交，因此其他机器可以复现当前版本；`.gitmodules` 中的 `branch` 仅用于以后显式执行远程更新。

## 初始化

克隆主仓库并初始化 SDK：

```powershell
git clone https://github.com/OCSpark-Dev/Lebai_Yunji_rebot.git
Set-Location Lebai_Yunji_rebot
& .\scripts\manage-sdks.ps1
```

如果已经克隆了主仓库：

```powershell
& .\scripts\manage-sdks.ps1
```

## 乐白

以下仓库均来自乐白官方 GitHub 组织 [`lebai-robotics`](https://github.com/lebai-robotics)。

| 本地路径 | 跟踪分支 | 用途 | 根目录许可证状态 |
| --- | --- | --- | --- |
| `lebai/sdk/lebai-sdk` | `master` | 当前统一 SDK，包含 C++、Python（pylebai）、.NET、Java | Apache-2.0 |
| `lebai/sdk/lebai-sdk.rs` | `master` | Rust SDK，并包含 Lua、Python AsyncIO、WASM/Node 等绑定 | MulanPSL-2.0 |
| `lebai/sdk/lebai-proto` | `next` | RPC/协议定义及生成代码来源 | 未发现根目录许可证 |
| `lebai/ros/lebai-ros-sdk` | `main` | ROS/ROS 2 与 MoveIt；`main` 是文档入口，实际代码在各 ROS 发行版分支 | Apache-2.0 |
| `lebai/lerobot/lerobot_lebai` | `main` | 实验性 LeRobot 适配参考；当前固定版本存在 Python 语法错误，不能直接运行 | Apache-2.0 |
| `lebai/examples/example` | `main` | Lua、Python 等编程示例 | 未发现根目录许可证 |
| `lebai/examples/jsonrpc-demo` | `main` | Flutter/JavaScript JSON-RPC 示例 | 未发现根目录许可证 |
| `lebai/integrations/OpenClawSkill` | `main` | 面向智能体的乐白 API 封装 | 未发现根目录许可证 |
| `lebai/integrations/plugin` | `main` | 相机、标定、AprilTag、夹爪、YOLO 等 L Master 插件；不是传统 SDK | 未发现根目录许可证 |
| `lebai/legacy/lebai-dotnet-sdk` | `main` | 旧版独立 .NET SDK，仅作兼容和历史参考 | 未发现根目录许可证 |
| `lebai/legacy/lebai-mindplus` | `main` | 已归档的 Mind+ Beta 集成，仅作历史参考 | MIT |

没有纳入 `aiot-rust`、`ethercat`、`lua-src-rs`、`mdns-sd`、`mlua`、`serialport-rs`、`tokio-modbus`、`auto-ssl`、`lmc_output` 等仓库：它们是内部基础设施、依赖分支、生成文档或非机器人 SDK，不应被误认为产品 SDK。大小写不同且归属无法确认的旧组织 `LebaiRobotics` 也未纳入。

## 云迹

| 本地路径 | 上游与分支 | 用途 | 许可证/可信度说明 |
| --- | --- | --- | --- |
| `yunji/cloud/open-api` | [`yunji-ai/open-api`](https://github.com/yunji-ai/open-api), `master` | 云迹旧版云端开放平台文档，以及 Java、JavaScript、Python 请求签名示例 | 根目录没有 LICENSE；`package.json` 声明 ISC。属于 legacy cloud API，使用前必须重新确认服务域名、账号体系和接口是否仍可用 |
| `yunji/integrations/yunji_agv_skill` | [`lebai-robotics/yunji_agv_skill`](https://github.com/lebai-robotics/yunji_agv_skill), `main` | WATER 软件 API v1.8.9 的 Python/OpenClaw 客户端，覆盖移动、遥控、点位和状态等操作 | 发布者是乐白，不是云迹；根目录没有 LICENSE，只有子目录 README 声称 MIT。仅按实验参考处理 |

截至核验日期：高可信的云迹组织 `yunjirobot` 和 `YunjiAI` 没有公开仓库；云迹账号 `yunji-ai` 的另一个仓库 `open-api-example` 是空仓库。未发现云迹官方公开的 WATER/UP 底盘 SDK、ROS 驱动或完整设备控制 SDK，因此不能把 `yunji_agv_skill` 表述为“云迹官方 SDK”。如果厂商提供私有下载包、开发者门户权限或新版协议文档，应在取得后另行归档并核验授权。

云迹 `open-api` 上游把生成目录 `docs/.vuepress/dist` 错误记录成了 Git link，却没有提供对应 `.gitmodules` 映射。因此不要对主仓库直接执行递归子模块初始化；上面的两步命令会完整初始化真正需要的 `lebai-sdk.rs/proto/lebai-proto`，同时避开这个不影响源文档和示例的上游缺陷。

另有节卡官方 `JAKARobotics/JAKA_Lumi` 中的云迹 AGV 集成示例，以及个人仓库 `Wel2018/agv_server`。前者属于另一整机厂商的大型集成项目，后者没有许可证；两者都不是云迹官方 SDK，也不属于本项目“乐白/云迹官方及直接整机集成”的拉取范围。

## 使用边界

- “已拉取”只表示源代码和历史可用，不表示已经在真实机器人上完成连接、动作或安全验证。
- 没有明确根许可证的仓库，不应默认允许复制、修改或对外再发布；商用前需向权利方确认。
- `yunji_agv_skill` 直接通过 TCP 控制底盘，但监听线程与同步命令会在同一 socket 上竞争 `recv`，还假设换行分帧；当前实现只适合代码审阅和隔离实验。
- `lerobot_lebai` 固定版本 `dfcbc92f7a46` 的 `lebai.py:76` 无法通过 Python AST 解析；同时没有 UP 动作空间、校准、动作限幅、等待完成或安全状态机。它只能作为重写 LM3 单臂适配器的参考。
- LM3-UP 的正式机械臂入口、WATER 客户端要求和 VLA 边界见 [`docs/SDK与子模块.md`](../docs/SDK与子模块.md)。

## 更新与核验

查看当前固定提交：

```powershell
git submodule status
```

显式拉取 `.gitmodules` 所配置分支的最新提交：

```powershell
& .\scripts\manage-sdks.ps1 -UpdateRemote
git status --short
```

远程更新会改变主仓库记录的子模块提交。更新后应分别审阅上游变更、许可证和真实设备兼容性，再决定是否提交。
