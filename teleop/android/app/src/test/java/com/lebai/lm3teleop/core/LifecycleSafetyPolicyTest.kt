package com.lebai.lm3teleop.core

import org.junit.Assert.assertTrue
import org.junit.Test

class LifecycleSafetyPolicyTest {
    @Test
    fun normalDestroyAlwaysDestroysController() {
        assertTrue(LifecycleSafetyPolicy.shouldDestroyController(isChangingConfigurations = false))
    }

    @Test
    fun configurationChangeAlsoDestroysController() {
        assertTrue(LifecycleSafetyPolicy.shouldDestroyController(isChangingConfigurations = true))
    }
}
