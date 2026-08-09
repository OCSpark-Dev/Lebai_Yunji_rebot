package com.lebai.lm3teleop.core

import java.net.URI

data class EndpointValidation(
    val valid: Boolean,
    val normalizedUrl: String? = null,
    val warning: String? = null,
    val error: String? = null,
)

object NetworkPolicy {
    fun validate(rawUrl: String, allowCleartextLan: Boolean): EndpointValidation {
        val trimmed = rawUrl.trim()
        if (trimmed.isEmpty()) return EndpointValidation(false, error = "请输入 WebSocket 地址")

        val uri = try {
            URI(trimmed)
        } catch (_: Exception) {
            return EndpointValidation(false, error = "WebSocket 地址格式无效")
        }
        val scheme = uri.scheme?.lowercase()
        if (scheme != "ws" && scheme != "wss") {
            return EndpointValidation(false, error = "地址必须使用 ws:// 或 wss://")
        }
        if (uri.userInfo != null) {
            return EndpointValidation(false, error = "不要把凭据写在 URL 中")
        }
        if (uri.rawQuery != null) {
            return EndpointValidation(false, error = "WebSocket 地址不能包含查询参数或凭据")
        }
        if (uri.fragment != null) {
            return EndpointValidation(false, error = "WebSocket 地址不能包含 fragment")
        }
        val host = uri.host ?: return EndpointValidation(false, error = "地址缺少主机名")

        if (scheme == "ws") {
            if (!allowCleartextLan) {
                return EndpointValidation(false, error = "release 构建只允许 WSS")
            }
            if (!isPrivateOrLocalHost(host)) {
                return EndpointValidation(false, error = "明文 ws:// 仅允许私有局域网、环回或 .local 主机")
            }
            return EndpointValidation(
                valid = true,
                normalizedUrl = trimmed,
                warning = "当前使用局域网明文 WS；生产环境必须切换 WSS",
            )
        }

        return EndpointValidation(valid = true, normalizedUrl = trimmed)
    }

    internal fun isPrivateOrLocalHost(rawHost: String): Boolean {
        val host = rawHost.trim().removePrefix("[").removeSuffix("]").lowercase()
        if (host == "localhost" || host.endsWith(".local")) return true
        if (!host.contains('.') && !host.contains(':')) return true

        val ipv4 = host.split('.')
        if (ipv4.size == 4) {
            val octets = ipv4.map { it.toIntOrNull() ?: return false }
            if (octets.any { it !in 0..255 }) return false
            return octets[0] == 10 ||
                octets[0] == 127 ||
                (octets[0] == 169 && octets[1] == 254) ||
                (octets[0] == 172 && octets[1] in 16..31) ||
                (octets[0] == 192 && octets[1] == 168)
        }

        if (host.contains(':')) {
            return host == "::1" ||
                host.startsWith("fc") ||
                host.startsWith("fd") ||
                host.matches(Regex("^fe[89ab].*"))
        }
        return false
    }

}
