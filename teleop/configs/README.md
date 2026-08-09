# 配置文件

- `lm3-up.sim.toml`：默认模拟器配置，只监听本机。
- `lm3-up.hardware.template.toml`：真机故意不可直接启动的模板。必须复制到不提交的本地文件，填写机器人 IP、TCP 位置工作空间、TCP 姿态中心/逐轴容差和六轴限位，并显式确认 `hardware_enabled`、`base_locked`、`workspace_configured`、`orientation_configured` 和 `joint_limits_configured`。
- `lingbot_lm3_up.yaml`：双相机数据集配置，复制到 LingBot-VLA 的 `configs/robot_configs/lm3_up.yaml`，映射 `[q1..q6, gripper]`、`camera_top` 和 `camera_wrist`。
- `lingbot_lm3_up_wrist_only.yaml`：仅腕部相机数据集配置。若采集或导出的 episode 没有 `camera_top`，必须使用这份配置，不能让训练配置声明一个数据集中不存在的图像 feature。

真机同时要求：

```powershell
python -m lm3_teleop_bridge serve `
  --config .\teleop\configs\lm3-up.hardware.local.toml `
  --hardware
```

非环回监听还必须在本地配置中设置 `allow_lan=true` 并同时提供 `--allow-lan`。当前桥接器不提供应用层 token 认证；这两个开关只是防误启动门槛，不是访问控制。局域网验证应使用受控机器人网络，跨不可信网络时应在桥前增加带设备认证的 WSS/TLS 网关。

TCP 姿态包络使用 `orientation_center_rad=[rx,ry,rz]` 与逐轴 `orientation_tolerance_rad`。每个容差必须大于 0 且不超过 `0.35 rad`；服务端按角度环绕后的最短距离检查当前姿态和按命令持续时间预测的姿态。该包络不替代工具几何、线缆和障碍物的现场扫掠验证。
