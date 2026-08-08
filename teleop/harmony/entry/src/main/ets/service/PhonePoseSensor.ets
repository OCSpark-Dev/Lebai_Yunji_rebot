import { sensor } from '@kit.SensorServiceKit';

export class PhonePoseReading {
  x: number;
  y: number;
  z: number;
  w: number;
  accuracy: number;
  sensorTimestampMs: number;

  constructor(x: number, y: number, z: number, w: number, accuracy: number, sensorTimestampMs: number) {
    this.x = x;
    this.y = y;
    this.z = z;
    this.w = w;
    this.accuracy = accuracy;
    this.sensorTimestampMs = sensorTimestampMs;
  }
}

export type PhonePoseReadingListener = (reading: PhonePoseReading) => void;
export type PhonePoseFailureListener = (reason: string) => void;

export class PhonePoseSensor {
  private active: boolean = false;
  private readingListener: PhonePoseReadingListener | undefined = undefined;
  private failureListener: PhonePoseFailureListener | undefined = undefined;

  private readonly rotationVectorCallback = (value: sensor.RotationVectorResponse): void => {
    if (!this.active || this.readingListener === undefined) {
      return;
    }
    // HarmonyOS reports boot-relative sensor timestamps in ns. Divide before storing/sending so
    // long-running devices stay inside JavaScript's exact integer range at millisecond precision.
    const sensorTimestampMs = Math.floor(value.timestamp / 1000000);
    if (!Number.isSafeInteger(sensorTimestampMs) || sensorTimestampMs <= 0) {
      const failureListener = this.failureListener;
      if (failureListener !== undefined) {
        failureListener('rotation_vector_timestamp_invalid');
      }
      return;
    }
    this.readingListener(new PhonePoseReading(
      value.x,
      value.y,
      value.z,
      value.w,
      value.accuracy,
      sensorTimestampMs
    ));
  };

  private hasHardwareGyroscope(): boolean {
    try {
      sensor.getSingleSensorSync(sensor.SensorId.GYROSCOPE);
      return true;
    } catch (error) {
      try {
        sensor.getSingleSensorSync(sensor.SensorId.GYROSCOPE_UNCALIBRATED);
        return true;
      } catch (fallbackError) {
        return false;
      }
    }
  }

  start(readingListener: PhonePoseReadingListener, failureListener: PhonePoseFailureListener): boolean {
    this.stop();
    this.readingListener = readingListener;
    this.failureListener = failureListener;
    if (!this.hasHardwareGyroscope()) {
      this.readingListener = undefined;
      this.failureListener = undefined;
      failureListener('hardware_gyroscope_unavailable');
      return false;
    }
    try {
      sensor.getSingleSensorSync(sensor.SensorId.ROTATION_VECTOR);
      this.active = true;
      sensor.on(sensor.SensorId.ROTATION_VECTOR, this.rotationVectorCallback, { interval: 'game' });
      return true;
    } catch (error) {
      this.active = false;
      this.readingListener = undefined;
      const detail = `rotation_vector_unavailable:${JSON.stringify(error)}`;
      this.failureListener = undefined;
      failureListener(detail);
      return false;
    }
  }

  stop(): boolean {
    const wasActive = this.active;
    this.active = false;
    this.readingListener = undefined;
    this.failureListener = undefined;
    if (!wasActive) {
      return true;
    }
    try {
      sensor.off(sensor.SensorId.ROTATION_VECTOR, this.rotationVectorCallback);
      return true;
    } catch (error) {
      return false;
    }
  }

  isActive(): boolean {
    return this.active;
  }
}
