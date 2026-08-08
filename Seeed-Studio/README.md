# Seeed Studio 手机遥操参考资料

本目录保存用户提供的分析文档，以及调研时使用的 Seeed Studio Wiki 固定版本。

- `Seeed_Studio_Wiki项目分析与手机遥操机械臂原理.docx`：对参考项目、手机遥操链路和 LM3-UP 适配边界的中文分析。
- `wiki-documents/`：Git 子模块，固定在 Seeed Studio Wiki 的 `docusaurus-version` 分支提交 `8654f5812b95286ce8101d1df2c36e95e61b7718`。

## 调研结论

Seeed Studio Wiki 是文档站点，不是直接控制 LM3-UP 的手机应用。Wiki 明确展示了键盘/鼠标、拖动、Leader 主臂和 VR 遥操；没有给出通过手机陀螺仪控制机械臂的实现。

进一步核对 Phosphobot 源码后，`/move/teleop/ws` 接收的 `AppControlData` 被明确描述为 Meta Quest 数据，控制页也列出 Meta Quest 2/Pro/3/3S。它能作为“头显/手柄位姿 → 机器人侧坐标变换、逆解与硬件执行”的架构参考，但不能作为“已有手机陀螺仪源码”的证据。先前把它描述成手机网页持续发送 X/Y/Z/Rx/Ry/Rz 的说法不准确，现已纠正。

本仓库的实际实现位于 [`../teleop/`](../teleop/)，采用原生 Android、原生 HarmonyOS 与独立 Python 安全桥，并额外加入：

- 默认仿真、真机显式启用；
- 共享令牌和单一控制租约；
- 长按解锁、按住式 deadman（松手即停）；
- Android 与 HarmonyOS 先确认设备存在硬件陀螺仪，缺失时禁用姿态模式；随后原生读取系统融合后的 Rotation Vector：手机姿态增量只控制 LM3 TCP 的 `Rx/Ry/Rz`，触屏按钮继续控制 `X/Y/Z`；
- 每次进入陀螺仪模式都显式归零，首个样本只建立基线；传感器陈旧、低置信度、时间倒退或姿态跳变时立即停止；
- 20 Hz 限速和 300 ms 失联看门狗；
- 乱序、过期、非有限数值和越界命令失败关闭；
- 要求由独立流程确认 UP 底盘停止/锁定（当前桥接器仅接收启动前静态声明），首版完全禁止手机或 VLA 输出底盘轮速；
- 原始示范记录与 LeRobot v3/LingBot-VLA 离线转换。

完整设计和证据边界见 [`../docs/手机遥操与VLA数据采集.md`](../docs/手机遥操与VLA数据采集.md)。

`wiki-documents/` 的上游根许可证为 GNU GPL v3；本仓库只是固定其公开快照，不改变上游许可。
