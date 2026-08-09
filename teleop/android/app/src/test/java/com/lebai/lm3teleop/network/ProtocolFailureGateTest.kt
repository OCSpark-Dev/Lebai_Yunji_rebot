package com.lebai.lm3teleop.network

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class ProtocolFailureGateTest {
    @Test
    fun onlyFirstFailureEntersUntilReset() {
        val gate = ProtocolFailureGate()
        assertTrue(gate.enterOnce())
        assertFalse(gate.enterOnce())
        assertFalse(gate.enterOnce())

        gate.reset()
        assertTrue(gate.enterOnce())
    }

    @Test
    fun connectionConfigContainsNoCredentialField() {
        val config = ConnectionConfig(
            url = "wss://robot.example.com/teleop",
            clientId = "client-1",
            clientName = "phone",
            appVersion = "1.0",
        )

        assertFalse(config.toString().contains("authToken"))
    }
}
