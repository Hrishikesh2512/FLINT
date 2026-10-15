package com.hrishikesh.shield

import java.net.Inet4Address
import java.net.NetworkInterface

/** Where the API can be reached from: Tailscale first, then the local network. */
object Net {
    /** (label, address) pairs, the one to use first. */
    fun addresses(): List<Pair<String, String>> = runCatching {
        NetworkInterface.getNetworkInterfaces().toList()
            .filter { it.isUp && !it.isLoopback }
            .flatMap { nic -> nic.inetAddresses.toList().filterIsInstance<Inet4Address>().map { nic to it } }
            .map { (nic, address) ->
                val ip = address.hostAddress.orEmpty()
                val label = when {
                    tailscale(ip) -> "Tailscale, from anywhere"
                    nic.name.startsWith("wlan") -> "Wi-Fi, same network only"
                    else -> nic.name
                }
                label to ip
            }
            .filter { (_, ip) -> ip.isNotEmpty() && !ip.startsWith("169.254") }
            .sortedBy { (label, _) -> if (label.startsWith("Tailscale")) 0 else 1 }
    }.getOrDefault(emptyList())

    /** Tailscale hands out addresses from 100.64.0.0/10. */
    private fun tailscale(ip: String): Boolean {
        val parts = ip.split('.').mapNotNull { it.toIntOrNull() }
        return parts.size == 4 && parts[0] == 100 && parts[1] in 64..127
    }
}
