package com.lebai.lm3teleop.core

object LifecycleSafetyPolicy {
    fun shouldDestroyController(
        @Suppress("UNUSED_PARAMETER") isChangingConfigurations: Boolean,
    ): Boolean = true
}
