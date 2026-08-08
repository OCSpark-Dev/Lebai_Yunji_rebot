# 配置文件

- `lm3-up.sim.toml`：默认模拟器配置，只监听本机。
- `lm3-up.hardware.template.toml`：真机故意不可直接启动的模板。必须复制到不提交的本地文件，填写机器人 IP、TCP 工作空间，并显式确认 `hardware_enabled`、`base_locked` 和 `workspace_configured`。
- `lingbot_lm3_up.yaml`：双相机数据集配置，复制到 LingBot-VLA 的 `configs/robot_configs/lm3_up.yaml`，映射 `[q1..q6, gripper]`、`camera_top` 和 `camera_wrist`。
- `lingbot_lm3_up_wrist_only.yaml`：仅腕部相机数据集配置。若采集或导出的 episode 没有 `camera_top`，必须使用这份配置，不能让训练配置声明一个数据集中不存在的图像 feature。

共享 token 只能通过配置中的 `auth_token_env` 对应环境变量提供，不能写入 TOML 或 Git。

真机同时要求：

```powershell
$env:LM3_TELEOP_TOKEN = '<至少 16 字符的随机值>'
python -m lm3_teleop_bridge serve `
  --config .\teleop\configs\lm3-up.hardware.local.toml `
  --hardware
```

非环回监听还必须在本地配置中设置 `allow_lan=true` 并同时提供 `--allow-lan`。这两个开关只是防误操作门槛；生产仍应使用隔离控制网和 WSS/TLS。
