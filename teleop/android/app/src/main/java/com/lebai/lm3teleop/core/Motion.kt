package com.lebai.lm3teleop.core

import com.lebai.lm3teleop.protocol.Vector3

enum class SpeedGear(
    val displayName: String,
    val linearMps: Double,
    val angularRps: Double,
) {
    CREEP("爬行", 0.005, 0.02),
    LOW("低速", 0.010, 0.05),
    CAREFUL("谨慎上限", 0.020, 0.10),
}

enum class AxisDirection(
    val displayName: String,
    private val sign: Double,
) {
    X_POS("X+", 1.0),
    X_NEG("X−", -1.0),
    Y_POS("Y+", 1.0),
    Y_NEG("Y−", -1.0),
    Z_POS("Z+", 1.0),
    Z_NEG("Z−", -1.0),
    RX_POS("Rx+", 1.0),
    RX_NEG("Rx−", -1.0),
    RY_POS("Ry+", 1.0),
    RY_NEG("Ry−", -1.0),
    RZ_POS("Rz+", 1.0),
    RZ_NEG("Rz−", -1.0),
    ;

    fun command(gear: SpeedGear): CartesianCommand = when (this) {
        X_POS, X_NEG -> CartesianCommand(linear = Vector3(x = sign * gear.linearMps))
        Y_POS, Y_NEG -> CartesianCommand(linear = Vector3(y = sign * gear.linearMps))
        Z_POS, Z_NEG -> CartesianCommand(linear = Vector3(z = sign * gear.linearMps))
        RX_POS, RX_NEG -> CartesianCommand(angular = Vector3(x = sign * gear.angularRps))
        RY_POS, RY_NEG -> CartesianCommand(angular = Vector3(y = sign * gear.angularRps))
        RZ_POS, RZ_NEG -> CartesianCommand(angular = Vector3(z = sign * gear.angularRps))
    }
}

data class CartesianCommand(
    val linear: Vector3 = Vector3(),
    val angular: Vector3 = Vector3(),
)
