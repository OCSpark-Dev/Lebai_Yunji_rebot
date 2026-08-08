# LM3-UP 复合机器人开发资料与 SDK

本仓库面向 **上海乐白 LM3-UP 复合机器人**。整机由乐白 LM3 六轴协作机械臂、LMG-90 夹爪、RK3588S2 应用/视觉主机、RGB 相机和云迹 UP 移动底盘组成。

仓库保存两类内容：

- 原始交付资料：PDF、DOCX、AprilTag 打印文件和乐白场景程序，保持原样归档。
- 上游开发资源：乐白 SDK、ROS/LeRobot/插件仓库，以及目前可确认的云迹相关公开接口资料，使用 Git 子模块固定版本。

> 当前状态是“资料与 SDK 基线”，不是已经完成的整机控制程序。仓库没有声称通过 LM3-UP 真机运动、地图、抓取或 VLA 端到端验证。

## 快速开始

不要使用 `git clone --recurse-submodules`。云迹旧版 `open-api` 上游包含一个缺失映射的 Git link，会导致无差别递归初始化报错。

```powershell
git clone https://github.com/OCSpark-Dev/Lebai_Yunji_rebot.git
Set-Location Lebai_Yunji_rebot
& .\scripts\manage-sdks.ps1
```

只验证本地 SDK 状态：

```powershell
& .\scripts\manage-sdks.ps1 -VerifyOnly
```

显式更新到 `.gitmodules` 所配置分支的最新提交：

```powershell
& .\scripts\manage-sdks.ps1 -UpdateRemote
git status --short
```

远程更新会改变主仓库记录的子模块提交，必须审阅后再提交。

## 文档

| 文档 | 用途 |
| --- | --- |
| [文档中心](docs/README.md) | 原始手册索引、阅读顺序和证据边界 |
| [LM3-UP 开发指南](docs/LM3-UP开发指南.md) | 整机组成、网络、控制分层和实施路线 |
| [SDK 与子模块指南](docs/SDK与子模块.md) | SDK 分级、初始化、版本选择和已知缺陷 |
| [LingBot-VLA 接入评估](docs/LingBot-VLA接入评估.md) | 能否用于 LM3-UP、缺口、建议架构和安全门槛 |
| [真机联调与验收清单](docs/真机联调与验收清单.md) | 首次连接、低速测试、地图、视觉和 VLA 验收记录 |
| [CLAUDE.md](CLAUDE.md) | 面向代码助手的完整手册事实、风险与操作约束 |
| [vendor/README.md](vendor/README.md) | 上游仓库来源、许可证和固定版本说明 |

## 目录

```text
LM3-UP复合机器人开发资料/   原始交付手册、场景和标签文件
docs/                       针对本机型整理的开发文档
examples/                   默认只读的安全示例
scripts/                    SDK 初始化和完整性验证脚本
vendor/lebai/               乐白官方 SDK、ROS、插件与示例
vendor/yunji/               云迹公开云 API 及乐白发布的 WATER 参考客户端
```

## 当前接口结论

- LM3 机械臂的正式主入口是 `vendor/lebai/sdk/lebai-sdk`，Python 包名为 `pylebai`。
- UP 底盘的权威本地接口依据是交付的 `water_api手册.pdf`。公开目录中没有找到云迹官方发布的 WATER/UP SDK 或 ROS 驱动。
- `vendor/yunji/integrations/yunji_agv_skill` 只能作为接口覆盖参考；其 socket 接收模型和分帧假设不适合直接投产。
- `vendor/lebai/lerobot/lerobot_lebai` 是实验性 LeRobot 适配器，当前固定版本存在 Python 语法错误，也不包含 UP 底盘动作空间。
- LingBot-VLA 可以作为二次研发候选，但不能把公开权重或动作直接发送给这台机器人。必须先完成本机数据、单臂动作映射、控制桥接和独立安全层。

## 安全边界

- 默认 IP、口令和截图参数只来自交付资料，不代表现场当前值；投产前应发现实际网络并更改默认凭据。
- 每个运动设备只能有一个命令写入者。底盘导航、底盘遥控和机械臂/VLA 控制不得由多个客户端并发抢占。
- 软件急停不能替代物理急停。任何真机运动前都要有现场监护、可触达的物理急停、清空的工作区和低速空载验证。
- API 返回 `OK` 只表示请求被接受，不表示动作已安全完成。
