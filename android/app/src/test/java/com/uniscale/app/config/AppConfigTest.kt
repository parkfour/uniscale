package com.uniscale.app.config

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class AppConfigTest {

    @Test
    fun testDefaultConfig() {
        val config = AppConfig()
        assertFalse("mTLS should be disabled by default", config.enableMtls)
        assertNull("clientCertAlias should be null by default", config.clientCertAlias)
        assertEquals("system", config.themeMode)
        assertEquals("https://health.example.com", config.hubUrl)
    }

    @Test
    fun testJsonSerializationWithMtlsAndTheme() {
        val config = AppConfig(
            hubUrl = "https://health.example.com",
            apiToken = "test-token",
            enableMtls = true,
            clientCertAlias = "test-cert-alias",
            themeMode = "dark"
        )

        val json = config.toJson()
        assertEquals(true, json.getBoolean("enable_mtls"))
        assertEquals("test-cert-alias", json.getString("client_cert_alias"))
        assertEquals("dark", json.getString("theme_mode"))

        val deserialized = AppConfig.fromJson(json)
        assertTrue(deserialized.enableMtls)
        assertEquals("test-cert-alias", deserialized.clientCertAlias)
        assertEquals("dark", deserialized.themeMode)
    }

    @Test
    fun testBackwardCompatibilityCertAliasInfersMtls() {
        val json = JSONObject()
        json.put("hub_url", "https://health.example.com")
        json.put("client_cert_alias", "legacy-alias")
        // Note: enable_mtls is NOT present

        val deserialized = AppConfig.fromJson(json)
        assertTrue("mTLS should be true if client_cert_alias is set and enable_mtls omitted", deserialized.enableMtls)
        assertEquals("legacy-alias", deserialized.clientCertAlias)
        assertEquals("system", deserialized.themeMode)
    }

    @Test
    fun testExplicitMtlsFalseOverridesCertAlias() {
        val json = JSONObject()
        json.put("enable_mtls", false)
        json.put("client_cert_alias", "ignored-alias")

        val deserialized = AppConfig.fromJson(json)
        assertFalse("Explicit enable_mtls=false must be respected", deserialized.enableMtls)
    }

    @Test
    fun testThemeModes() {
        listOf("system", "light", "dark").forEach { mode ->
            val json = JSONObject()
            json.put("theme_mode", mode)
            val config = AppConfig.fromJson(json)
            assertEquals(mode, config.themeMode)
        }
    }
}
