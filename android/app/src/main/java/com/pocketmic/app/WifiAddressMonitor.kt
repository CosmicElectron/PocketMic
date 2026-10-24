package com.pocketmic.app

import android.content.Context
import android.net.ConnectivityManager
import android.net.LinkProperties
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import android.os.Handler
import android.os.Looper
import java.net.Inet4Address

/**
 * Tracks this phone's IPv4 address on Wi-Fi (null when not on Wi-Fi) and reports changes on the
 * main thread. Watches every Wi-Fi network, not just the default one, because Android may route
 * internet traffic over mobile data while still connected to a Wi-Fi without internet.
 */
class WifiAddressMonitor(context: Context, private val onChange: (String?) -> Unit) {
    private val connectivity = context.applicationContext.getSystemService(ConnectivityManager::class.java)
    private val addresses = LinkedHashMap<Network, String?>()
    private var registered = false

    private val callback = object : ConnectivityManager.NetworkCallback() {
        override fun onLinkPropertiesChanged(network: Network, linkProperties: LinkProperties) {
            addresses[network] = ipv4Of(linkProperties)
            report()
        }

        override fun onLost(network: Network) {
            addresses.remove(network)
            report()
        }
    }

    fun start() {
        if (registered || connectivity == null) return
        // Seed from the current default network so the first frame does not flash "no Wi-Fi". A
        // VPN running over Wi-Fi also has the Wi-Fi transport, but its address is the tunnel's.
        connectivity.activeNetwork?.let { network ->
            val capabilities = connectivity.getNetworkCapabilities(network)
            val isWifi = capabilities != null &&
                capabilities.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) &&
                !capabilities.hasTransport(NetworkCapabilities.TRANSPORT_VPN)
            val properties = connectivity.getLinkProperties(network)
            if (isWifi && properties != null) addresses[network] = ipv4Of(properties)
        }
        // The request's default NOT_VPN capability keeps VPNs out of the callback too.
        val request = NetworkRequest.Builder()
            .addTransportType(NetworkCapabilities.TRANSPORT_WIFI)
            .build()
        connectivity.registerNetworkCallback(request, callback, Handler(Looper.getMainLooper()))
        registered = true
        report()
    }

    fun stop() {
        if (!registered) return
        connectivity?.unregisterNetworkCallback(callback)
        registered = false
        addresses.clear()
    }

    private fun report() = onChange(addresses.values.firstOrNull { it != null })

    private fun ipv4Of(properties: LinkProperties): String? =
        properties.linkAddresses
            .map { it.address }
            .firstOrNull { it is Inet4Address && !it.isLoopbackAddress }
            ?.hostAddress
}
