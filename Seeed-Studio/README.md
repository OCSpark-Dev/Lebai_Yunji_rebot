# Seeed Studio 手机遥操参考资料

本目录保存用户提供的分析文档，以及调研时使用的 Seeed Studio Wiki 固定版本。

- `Seeed_Studio_Wiki项目分析与手机遥操机械臂原理.docx`：对参考项目、手机遥操链路和 LM3-UP 适配边界的中文分析。
- `wiki-documents/`：Git 子模块，固定在 Seeed Studio Wiki 的 `docusaurus-version` 分支提交 `8654f5812b95286ce8101d1df2c36e95e61b7718`。

## 调研结论

Seeed Studio Wiki 是文档站点，不是直接控制 LM3-UP 的手机应用。Wiki 中关联的 Phosphobot 方案采用“手机网页输入 → 高频相对笛卡尔命令 → 机器人侧坐标变换/逆解/硬件执行”的模式。该思路适合作为交互参考，但其默认监听、CORS 和运动授权策略不能原样用于复合机器人。

本仓库的实际实现位于 [`../teleop/`](../teleop/)，采用原生 Android、原生 HarmonyOS 与独立 Python 安全桥，并额外加入：

- 默认仿真、真机显式启用；
- 共享令牌和单一控制租约；
- 长按解锁、按住式 deadman（松手即停）；
- 20 Hz 限速和 300 ms 失联看门狗；
- 乱序、过期、非有限数值和越界命令失败关闭；
- 要求由独立流程确认 UP 底盘停止/锁定（当前桥接器仅接收启动前静态声明），首版完全禁止手机或 VLA 输出底盘轮速；
- 原始示范记录与 LeRobot v3/LingBot-VLA 离线转换。

完整设计和证据边界见 [`../docs/手机遥操与VLA数据采集.md`](../docs/手机遥操与VLA数据采集.md)。

`wiki-documents/` 的上游根许可证为 GNU GPL v3；本仓库只是固定其公开快照，不改变上游许可。
