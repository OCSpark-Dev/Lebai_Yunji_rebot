package com.lebai.lm3teleop.sensor

import android.content.Context
import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.hardware.SensorManager
import com.lebai.lm3teleop.core.OrientationSensorSource
import com.lebai.lm3teleop.core.OrientationSensorSample
import com.lebai.lm3teleop.core.UnitQuaternion
import com.lebai.lm3teleop.core.selectOrientationSensorSource

class PhoneOrientationSensor(
    context: Context,
    private val onSample: (OrientationSensorSample) -> Unit,
    private val onFault: (String) -> Unit,
) : SensorEventListener {
    private val sensorManager = context.getSystemService(Context.SENSOR_SERVICE) as SensorManager
    private val gyroscopeSensor: Sensor? =
        sensorManager.getDefaultSensor(Sensor.TYPE_GYROSCOPE)
            ?: sensorManager.getDefaultSensor(Sensor.TYPE_GYROSCOPE_UNCALIBRATED)
    private val gameRotationVectorSensor = sensorManager.getDefaultSensor(Sensor.TYPE_GAME_ROTATION_VECTOR)
    private val rotationVectorSensor = sensorManager.getDefaultSensor(Sensor.TYPE_ROTATION_VECTOR)
    private val source = selectOrientationSensorSource(
        hasGyroscope = gyroscopeSensor != null,
        hasGameRotationVector = gameRotationVectorSensor != null,
        hasRotationVector = rotationVectorSensor != null,
    )
    private val selectedSensor: Sensor? = when (source) {
        OrientationSensorSource.GAME_ROTATION_VECTOR -> gameRotationVectorSensor
        OrientationSensorSource.ROTATION_VECTOR -> rotationVectorSensor
        OrientationSensorSource.NONE -> null
    }
    private val quaternionBuffer = FloatArray(4)
    private var registered = false

    val available: Boolean
        get() = selectedSensor != null

    val description: String
        get() = when {
            gyroscopeSensor == null -> "设备无可用硬件陀螺仪，姿态遥操已禁用"
            source == OrientationSensorSource.GAME_ROTATION_VECTOR ->
                "GAME_ROTATION_VECTOR（陀螺仪+加速度计融合）"
            source == OrientationSensorSource.ROTATION_VECTOR ->
                "ROTATION_VECTOR（含磁力计融合回退）"
            else -> "设备无可用姿态旋转矢量传感器"
        }

    fun start(): Boolean {
        if (registered) return true
        val sensor = selectedSensor ?: return false
        registered = sensorManager.registerListener(
            this,
            sensor,
            SensorManager.SENSOR_DELAY_GAME,
            0,
        )
        if (!registered) onFault("姿态传感器注册失败")
        return registered
    }

    fun stop() {
        if (registered) sensorManager.unregisterListener(this)
        registered = false
    }

    override fun onSensorChanged(event: SensorEvent) {
        if (event.sensor.type != selectedSensor?.type) return
        if (event.timestamp <= 0L || event.values.size < 3) {
            onFault("姿态传感器返回无效时间戳或数据长度")
            return
        }
        if (event.values.any { !it.isFinite() }) {
            onFault("姿态传感器返回非有限数值")
            return
        }
        if (event.accuracy == SensorManager.SENSOR_STATUS_UNRELIABLE) {
            onFault("姿态传感器精度不可靠")
            return
        }

        try {
            SensorManager.getQuaternionFromVector(quaternionBuffer, event.values)
        } catch (error: RuntimeException) {
            onFault("姿态四元数转换失败：${error.javaClass.simpleName}")
            return
        }
        val confidence = when (event.accuracy) {
            SensorManager.SENSOR_STATUS_ACCURACY_HIGH -> 1.0
            SensorManager.SENSOR_STATUS_ACCURACY_MEDIUM -> 0.8
            SensorManager.SENSOR_STATUS_ACCURACY_LOW -> 0.6
            else -> 0.5
        }
        onSample(
            OrientationSensorSample(
                quaternion = UnitQuaternion(
                    w = quaternionBuffer[0].toDouble(),
                    x = quaternionBuffer[1].toDouble(),
                    y = quaternionBuffer[2].toDouble(),
                    z = quaternionBuffer[3].toDouble(),
                ),
                timestampNs = event.timestamp,
                confidence = confidence,
            ),
        )
    }

    override fun onAccuracyChanged(sensor: Sensor?, accuracy: Int) {
        if (sensor?.type == selectedSensor?.type && accuracy == SensorManager.SENSOR_STATUS_UNRELIABLE) {
            onFault("姿态传感器精度变为不可靠")
        }
    }
}
