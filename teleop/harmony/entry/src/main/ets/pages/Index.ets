import { AxisName, OPERATOR_HOLD_MS, SafetyAck } from '../model/TeleopProtocol';
import { TeleopClient, TeleopSnapshot } from '../service/TeleopClient';

@Entry
@Component
struct Index {
  private client: TeleopClient = TeleopClient.getInstance();

  @State private snapshot: TeleopSnapshot = new TeleopSnapshot();
  @State private endpoint: string = '';
  @State private authToken: string = '';
  @State private baseStationary: boolean = false;
  @State private workspaceClear: boolean = false;
  @State private estopAccessible: boolean = false;
  @State private toolSecure: boolean = false;
  @State private gripperTarget: number = 0;
  @State private recordingTask: string = '';
  @State private episodeId: string = '';
  @State private camerasText: string = 'camera_top,camera_wrist';

  aboutToAppear(): void {
    this.applySnapshot(this.client.getSnapshot());
    this.gripperTarget = this.snapshot.gripperPositionPct;
    this.client.setListener((snapshot: TeleopSnapshot) => {
      this.applySnapshot(snapshot);
    });
  }

  aboutToDisappear(): void {
    this.client.setListener(undefined);
    this.client.disconnect('page_disappeared');
  }

  private connect(): void {
    const token = this.authToken;
    this.client.connect(this.endpoint, token);
    this.authToken = '';
  }

  private applySnapshot(snapshot: TeleopSnapshot): void {
    this.snapshot = snapshot;
    this.baseStationary = snapshot.baseStationary;
    this.workspaceClear = snapshot.workspaceClear;
    this.estopAccessible = snapshot.estopAccessible;
    this.toolSecure = snapshot.toolSecure;
  }

  private updateSafety(): void {
    const safety = new SafetyAck();
    safety.base_stationary = this.baseStationary;
    safety.workspace_clear = this.workspaceClear;
    safety.estop_accessible = this.estopAccessible;
    safety.tool_secure = this.toolSecure;
    this.client.updateSafety(safety);
  }

  private handleDeadmanTouch(event: TouchEvent): void {
    const touchId = this.changedTouchId(event);
    if (event.type === TouchType.Down) {
      this.client.setDeadman(true, touchId);
    } else if (event.type === TouchType.Up || event.type === TouchType.Cancel) {
      this.client.setDeadman(false, touchId);
    }
  }

  private handlePoseDeadmanTouch(event: TouchEvent): void {
    const touchId = this.changedTouchId(event);
    if (event.type === TouchType.Down) {
      this.client.setPoseDeadman(true, touchId);
    } else if (event.type === TouchType.Up || event.type === TouchType.Cancel) {
      this.client.setPoseDeadman(false, touchId);
    }
  }

  private handleAxisTouch(event: TouchEvent, axis: string, direction: number): void {
    const touchId = this.changedTouchId(event);
    if (event.type === TouchType.Down) {
      this.client.pressAxis(axis, direction, touchId);
    } else if (event.type === TouchType.Up || event.type === TouchType.Cancel) {
      this.client.releaseAxis(axis, touchId);
    }
  }

  private changedTouchId(event: TouchEvent): number {
    if (event.changedTouches.length === 0) {
      return -1;
    }
    return event.changedTouches[0].id;
  }

  private parseCameras(): Array<string> {
    const cameras: Array<string> = [];
    const raw: Array<string> = this.camerasText.split(',');
    for (let index = 0; index < raw.length; index += 1) {
      const camera = raw[index].trim();
      if (camera.length > 0) {
        cameras.push(camera);
      }
    }
    return cameras;
  }

  @Builder
  private axisButton(label: string, axis: string, direction: number) {
    Button(label)
      .width('47%')
      .height(48)
      .fontSize(18)
      .fontWeight(FontWeight.Bold)
      .backgroundColor(this.snapshot.canCommand && !this.snapshot.poseDeadmanHeld ? '#155EEF' : '#8A94A6')
      .enabled(this.snapshot.canCommand && !this.snapshot.poseDeadmanHeld)
      .onTouch((event: TouchEvent) => {
        this.handleAxisTouch(event, axis, direction);
      })
  }

  @Builder
  private safetyItem(label: string, name: string, selected: boolean, onChanged: (value: boolean) => void) {
    Row({ space: 8 }) {
      Checkbox({ name: name })
        .select(selected)
        .onChange((value: boolean) => {
          onChanged(value);
        })
      Text(label)
        .fontSize(15)
        .fontColor('#172B4D')
        .layoutWeight(1)
    }
    .width('100%')
    .height(38)
  }

  build() {
    Scroll() {
      Column({ space: 14 }) {
        Column({ space: 5 }) {
          Text('LM3-UP 安全遥操')
            .fontSize(26)
            .fontWeight(FontWeight.Bold)
            .fontColor('#0B1F33')
          Text('仅控制机械臂与夹爪；UP 底盘必须由独立流程确认停止并锁定')
            .fontSize(13)
            .fontColor('#B42318')
        }
        .width('100%')
        .alignItems(HorizontalAlign.Start)

        Column({ space: 10 }) {
          Text('1. 连接安全网关')
            .fontSize(19)
            .fontWeight(FontWeight.Bold)
          TextInput({ text: this.endpoint, placeholder: 'ws:// 或 wss:// 网关地址（不写死 IP）' })
            .width('100%')
            .height(44)
            .onChange((value: string) => {
              this.endpoint = value;
            })
          TextInput({ text: this.authToken, placeholder: '共享 token（仅本次连接，发送后清空）' })
            .type(InputType.Password)
            .showPasswordIcon(true)
            .width('100%')
            .height(44)
            .onChange((value: string) => {
              this.authToken = value;
            })
          Row({ space: 10 }) {
            Button('连接')
              .layoutWeight(1)
              .backgroundColor('#155EEF')
              .enabled(!this.snapshot.connected)
              .onClick(() => {
                this.connect();
              })
            Button('断开')
              .layoutWeight(1)
              .backgroundColor('#475467')
              .onClick(() => {
                this.client.disconnect('operator_disconnect');
              })
          }
          .width('100%')
          Text(`连接：${this.snapshot.connectionState}  协议：${this.snapshot.welcomed ? '就绪' : '未就绪'}`)
            .fontSize(14)
            .fontColor(this.snapshot.welcomed ? '#067647' : '#B54708')
        }
        .width('100%')
        .padding(14)
        .backgroundColor('#FFFFFF')
        .borderRadius(12)

        Column({ space: 8 }) {
          Text('2. 机器人安全状态')
            .fontSize(19)
            .fontWeight(FontWeight.Bold)
          Text(`模式：${this.snapshot.robotMode}  底盘锁定：${this.snapshot.baseLocked ? '是' : '否'}`)
          Text(`看门狗正常：${this.snapshot.watchdogOk ? '是' : '否'}  急停/故障：${this.snapshot.estop ? '有/未知' : '无'}`)
          Text(`急停原因：${this.snapshot.estopReason}  状态新鲜：${this.snapshot.robotStateFresh ? '是' : '否'}`)
          Text(`看门狗：${this.snapshot.watchdogMs} ms  命令频率：${this.snapshot.commandRateHz} Hz`)
          Text(`TCP：${this.snapshot.tcpText}`)
            .fontSize(13)
          Text(`关节：${this.snapshot.jointText}`)
            .fontSize(13)
          Text(`关节速度：${this.snapshot.jointVelocityText}`)
            .fontSize(13)
          Text(`服务端限制：${this.snapshot.limitsText}`)
            .fontSize(12)
            .fontColor('#475467')
        }
        .width('100%')
        .alignItems(HorizontalAlign.Start)
        .padding(14)
        .backgroundColor('#FFFFFF')
        .borderRadius(12)

        Column({ space: 7 }) {
          Text('3. 现场检查与长按解锁')
            .fontSize(19)
            .fontWeight(FontWeight.Bold)
          this.safetyItem('UP 底盘已经停止并锁定', 'base_stationary', this.baseStationary, (value: boolean) => {
            this.baseStationary = value;
            this.updateSafety();
          })
          this.safetyItem('机械臂工作空间内无人且无障碍', 'workspace_clear', this.workspaceClear, (value: boolean) => {
            this.workspaceClear = value;
            this.updateSafety();
          })
          this.safetyItem('操作者可立即触达物理急停', 'estop_accessible', this.estopAccessible, (value: boolean) => {
            this.estopAccessible = value;
            this.updateSafety();
          })
          this.safetyItem('末端工具、工件与线缆固定可靠', 'tool_secure', this.toolSecure, (value: boolean) => {
            this.toolSecure = value;
            this.updateSafety();
          })
          Button(`持续长按 ${OPERATOR_HOLD_MS / 1000} 秒申请控制权`)
            .width('100%')
            .height(50)
            .fontWeight(FontWeight.Bold)
            .backgroundColor(this.snapshot.canAcquire ? '#067647' : '#8A94A6')
            .enabled(this.snapshot.canAcquire)
            .gesture(
              LongPressGesture({ repeat: false, duration: OPERATOR_HOLD_MS })
                .onAction(() => {
                  this.client.requestControl();
                })
            )
          Text(`租约：${this.snapshot.controlGranted ? '有效' : '无效'}  ID：${this.snapshot.leaseIdDisplay}  剩余：${this.snapshot.leaseRemainingMs} ms`)
            .fontSize(13)
            .fontColor(this.snapshot.controlGranted ? '#067647' : '#B54708')
        }
        .width('100%')
        .padding(14)
        .backgroundColor('#FFFFFF')
        .borderRadius(12)

        Column({ space: 10 }) {
          Text('4. 触屏低速笛卡尔点动')
            .fontSize(19)
            .fontWeight(FontWeight.Bold)
          Row({ space: 8 }) {
            Button('极慢')
              .layoutWeight(1)
              .backgroundColor(this.snapshot.speedName === '极慢' ? '#155EEF' : '#8A94A6')
              .onClick(() => this.client.setSpeedProfile(0))
            Button('低速')
              .layoutWeight(1)
              .backgroundColor(this.snapshot.speedName === '低速' ? '#155EEF' : '#8A94A6')
              .onClick(() => this.client.setSpeedProfile(1))
            Button('调试')
              .layoutWeight(1)
              .backgroundColor(this.snapshot.speedName === '调试' ? '#B54708' : '#8A94A6')
              .onClick(() => this.client.setSpeedProfile(2))
          }
          .width('100%')
          Text(`当前：${this.snapshot.speedName}，线速度 ${this.snapshot.linearMps} m/s，角速度 ${this.snapshot.angularRps} rad/s`)
            .fontSize(13)
          Button(this.snapshot.deadmanHeld ? 'DEADMAN 按住中' : '持续按住 DEADMAN')
            .width('100%')
            .height(64)
            .fontSize(19)
            .fontWeight(FontWeight.Bold)
            .backgroundColor(this.snapshot.deadmanHeld ? '#B42318' : '#D92D20')
            .enabled(this.snapshot.canCommand && !this.snapshot.poseDeadmanHeld)
            .onTouch((event: TouchEvent) => {
              this.handleDeadmanTouch(event);
            })
          Text('必须一只手持续按住 DEADMAN，再用另一只手按住单个轴键；任意松手立即发送 stop。')
            .fontSize(12)
            .fontColor('#B42318')

          Row({ space: 10 }) {
            this.axisButton('X −', AxisName.X, -1)
            this.axisButton('X +', AxisName.X, 1)
          }
          .width('100%')
          Row({ space: 10 }) {
            this.axisButton('Y −', AxisName.Y, -1)
            this.axisButton('Y +', AxisName.Y, 1)
          }
          .width('100%')
          Row({ space: 10 }) {
            this.axisButton('Z −', AxisName.Z, -1)
            this.axisButton('Z +', AxisName.Z, 1)
          }
          .width('100%')
          Row({ space: 10 }) {
            this.axisButton('Rx −', AxisName.RX, -1)
            this.axisButton('Rx +', AxisName.RX, 1)
          }
          .width('100%')
          Row({ space: 10 }) {
            this.axisButton('Ry −', AxisName.RY, -1)
            this.axisButton('Ry +', AxisName.RY, 1)
          }
          .width('100%')
          Row({ space: 10 }) {
            this.axisButton('Rz −', AxisName.RZ, -1)
            this.axisButton('Rz +', AxisName.RZ, 1)
          }
          .width('100%')
          Text(`当前轴：${this.snapshot.activeAxis}`)
            .fontSize(13)
        }
        .width('100%')
        .padding(14)
        .backgroundColor('#FFFFFF')
        .borderRadius(12)

        Column({ space: 10 }) {
          Text('5. 手机陀螺仪姿态旋转（3DoF）')
            .fontSize(19)
            .fontWeight(FontWeight.Bold)
          Text('必须检测到硬件 GYROSCOPE，再读取系统融合 ROTATION_VECTOR；只映射 TCP rx/ry/rz。XYZ 平移仍使用上方触屏轴键，绝不由 IMU 积分推算。')
            .fontSize(12)
            .fontColor('#B42318')
          Row({ space: 10 }) {
            Button('启用陀螺仪姿态')
              .layoutWeight(1)
              .enabled(!this.snapshot.poseSensorActive)
              .backgroundColor('#155EEF')
              .onClick(() => {
                this.client.startPoseSensor();
              })
            Button('停止姿态传感器')
              .layoutWeight(1)
              .enabled(this.snapshot.poseSensorActive)
              .backgroundColor('#475467')
              .onClick(() => {
                this.client.stopPoseSensor();
              })
          }
          .width('100%')
          Text(`传感器：${this.snapshot.poseSensorStatus}`)
            .fontSize(13)
          Text(`跟踪：${this.snapshot.poseTracking ? '稳定' : '不可用'}  精度：${this.snapshot.poseAccuracy}/3  ` +
            `置信度：${this.snapshot.poseConfidence.toFixed(2)}  样本年龄：${this.snapshot.poseSampleAgeMs} ms`)
            .fontSize(12)
          Button('保持标准握持姿态并显式归零')
            .width('100%')
            .enabled(this.snapshot.canCalibratePose)
            .backgroundColor(this.snapshot.poseCalibrated ? '#067647' : '#B54708')
            .onClick(() => {
              this.client.calibratePose();
            })
          Text(`归零：${this.snapshot.poseCalibrated ? '已完成' : '未完成'}  ID：${this.snapshot.poseCalibrationIdDisplay}`)
            .fontSize(13)
          Button(this.snapshot.poseDeadmanHeld ? '姿态 DEADMAN 按住中' : '持续按住姿态 DEADMAN')
            .width('100%')
            .height(64)
            .fontSize(19)
            .fontWeight(FontWeight.Bold)
            .backgroundColor(this.snapshot.poseDeadmanHeld ? '#B42318' : '#D92D20')
            .enabled(this.snapshot.canPoseCommand && !this.snapshot.deadmanHeld &&
              this.snapshot.activeAxis === AxisName.NONE)
            .onTouch((event: TouchEvent) => {
              this.handlePoseDeadmanTouch(event);
            })
          Text(`最近旋转增量：${this.snapshot.poseDeltaText}`)
            .fontSize(12)
          Text('标准握持：屏幕朝上，手机顶部指向机器人底座 +X。手机 +X（屏幕向右）→ TCP -ry，+Y（手机顶部）→ TCP +rx，+Z（屏幕朝外）→ TCP +rz，即 [y,-x,z]。首帧仅 priming；松手、跳变、超过归零姿态 30°、样本陈旧、后台或断线立即停止。必须先在模拟器验向。')
            .fontSize(12)
            .fontColor('#B42318')
        }
        .width('100%')
        .padding(14)
        .backgroundColor('#FFFFFF')
        .borderRadius(12)

        Column({ space: 10 }) {
          Text('6. 夹爪')
            .fontSize(19)
            .fontWeight(FontWeight.Bold)
          Row({ space: 10 }) {
            Slider({ value: this.gripperTarget, min: 0, max: 100, step: 1, style: SliderStyle.OutSet })
              .layoutWeight(1)
              .onChange((value: number) => {
                this.gripperTarget = value;
              })
            Text(`${Math.round(this.gripperTarget)}%`)
              .width(48)
          }
          .width('100%')
          Button('按住 DEADMAN 后发送夹爪目标')
            .width('100%')
            .enabled(this.snapshot.deadmanHeld && this.snapshot.canCommand)
            .backgroundColor('#155EEF')
            .onClick(() => {
              this.client.setGripper(this.gripperTarget);
            })
          Text('注意：松手/断线只会请求 stop_move；当前未验证可中途停止夹爪。')
            .fontSize(12)
            .fontColor('#B42318')
        }
        .width('100%')
        .padding(14)
        .backgroundColor('#FFFFFF')
        .borderRadius(12)

        Column({ space: 10 }) {
          Text('7. VLA 示范录制')
            .fontSize(19)
            .fontWeight(FontWeight.Bold)
          TextInput({ text: this.recordingTask, placeholder: '任务，例如：拿起红色方块放入左侧盒子' })
            .onChange((value: string) => {
              this.recordingTask = value;
            })
          TextInput({ text: this.episodeId, placeholder: 'episode ID（可选）' })
            .onChange((value: string) => {
              this.episodeId = value;
            })
          TextInput({ text: this.camerasText, placeholder: '相机名，逗号分隔' })
            .onChange((value: string) => {
              this.camerasText = value;
            })
          Row({ space: 10 }) {
            Button('开始录制')
              .layoutWeight(1)
              .enabled(this.snapshot.canCommand && !this.snapshot.recording)
              .backgroundColor('#067647')
              .onClick(() => {
                this.client.startRecording(this.recordingTask, this.episodeId, this.parseCameras());
              })
            Button('停止录制')
              .layoutWeight(1)
              .enabled(this.snapshot.recording)
              .backgroundColor('#B54708')
              .onClick(() => {
                this.client.stopRecording('operator_stop');
              })
          }
          .width('100%')
          Text(`录制：${this.snapshot.recording ? '进行中' : '未录制'}  episode：${this.snapshot.episodeId}`)
            .fontSize(13)
        }
        .width('100%')
        .padding(14)
        .backgroundColor('#FFFFFF')
        .borderRadius(12)

        Button('立即停止并释放本地控制状态')
          .width('100%')
          .height(62)
          .fontSize(18)
          .fontWeight(FontWeight.Bold)
          .backgroundColor('#B42318')
          .onClick(() => {
            this.client.emergencyStopFromUi();
          })

        Column({ space: 5 }) {
          Text('最近事件')
            .fontSize(16)
            .fontWeight(FontWeight.Bold)
          Text(this.snapshot.lastEvent)
            .fontSize(13)
            .fontColor('#344054')
        }
        .width('100%')
        .alignItems(HorizontalAlign.Start)
        .padding(14)
        .backgroundColor('#EAECF0')
        .borderRadius(12)

        Text('软件停止不是物理急停。任何异常都应立即松手并按下机器人实体急停。')
          .fontSize(12)
          .fontColor('#B42318')
          .textAlign(TextAlign.Center)
          .width('100%')
          .padding({ bottom: 20 })
      }
      .width('100%')
      .padding({ left: 14, right: 14, top: 18 })
    }
    .width('100%')
    .height('100%')
    .backgroundColor('#F2F4F7')
    .scrollBar(BarState.Auto)
  }
}
