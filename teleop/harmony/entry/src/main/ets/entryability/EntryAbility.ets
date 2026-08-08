import { AbilityConstant, UIAbility, Want } from '@kit.AbilityKit';
import { window } from '@kit.ArkUI';
import { TeleopClient } from '../service/TeleopClient';

export default class EntryAbility extends UIAbility {
  onCreate(want: Want, launchParam: AbilityConstant.LaunchParam): void {
    TeleopClient.getInstance().appForeground();
  }

  onDestroy(): void {
    TeleopClient.getInstance().disconnect('ability_destroyed');
  }

  onWindowStageCreate(windowStage: window.WindowStage): void {
    try {
      windowStage.loadContent('pages/Index');
    } catch (error) {
      TeleopClient.getInstance().disconnect('page_load_failed');
    }
  }

  onWindowStageDestroy(): void {
    TeleopClient.getInstance().disconnect('window_stage_destroyed');
  }

  onForeground(): void {
    TeleopClient.getInstance().appForeground();
  }

  onBackground(): void {
    TeleopClient.getInstance().appBackground();
  }
}
