package com.lebai.lm3teleop.core

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class NetworkPolicyTest {
    @Test
    fun wssIsAcceptedForAnyNormalHost() {
        assertTrue(NetworkPolicy.validate("wss://robot.example.com/teleop", false).valid)
    }

    @Test
    fun debugWsAcceptsPrivateLan() {
        assertTrue(NetworkPolicy.validate("ws://192.168.10.201:8765/teleop", true).valid)
        assertTrue(NetworkPolicy.validate("ws://10.0.2.2:8765/teleop", true).valid)
        assertTrue(NetworkPolicy.validate("ws://gateway.local/teleop", true).valid)
    }

    @Test
    fun debugWsRejectsPublicHost() {
        assertFalse(NetworkPolicy.validate("ws://example.com/teleop", true).valid)
        assertFalse(NetworkPolicy.validate("ws://8.8.8.8/teleop", true).valid)
    }

    @Test
    fun releaseRejectsAllCleartext() {
        assertFalse(NetworkPolicy.validate("ws://192.168.10.201/teleop", false).valid)
    }

    @Test
    fun credentialsInUrlAreRejected() {
        assertFalse(NetworkPolicy.validate("wss://token@example.com/teleop", false).valid)
    }

    @Test
    fun allQueryParametersAreRejected() {
        listOf(
            "token",
            "auth_token",
            "access_token",
            "password",
            "secret",
            "AUTH_TOKEN",
            "auth%5Ftoken",
        ).forEach { name ->
            assertFalse(NetworkPolicy.validate("wss://robot.example.com/teleop?$name=value", false).valid)
        }
        assertFalse(NetworkPolicy.validate("wss://robot.example.com/teleop?site=lab-a", false).valid)
    }
}
