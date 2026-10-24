package com.pocketmic.app

import android.content.Context
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import android.os.Handler
import android.os.Looper
import android.util.Log

/**
 * Keeps this app's sockets on the phone's Wi-Fi network while it streams. When the Wi-Fi has no
 * internet Android makes mobile data the default network, and a VPN becomes the default while it
 * runs; packets from a socket that is not bound to the Wi-Fi would then leave over that network
 * and never reach the PC. Requesting the Wi-Fi network, rather than only listening for it, also
 * stops Android from treating it as unneeded, which would forbid binding to it.
 *
 * Sockets keep the network they were opened on, so [onChange] (main thread) must reopen them.
 */
class WifiBinding(context: Context, private val onChange: () -> Unit) {
    private val connectivity = context.applicationContext.getSystemService(ConnectivityManager::class.java)
    private var network: Network? = null
    private var requested = false

    private val callback = object : ConnectivityManager.NetworkCallback() {
        override fun onAvailable(network: Network) = bindTo(network)

        override fun onLost(network: Network) {
            // A replacement Wi-Fi network may already have been reported.
            if (network == this@WifiBinding.network) bindTo(null)
        }
    }

    fun start() {
        if (requested || connectivity == null) return
        // The request's default NOT_VPN capability matches the Wi-Fi itself, not a VPN over it.
        val request = NetworkRequest.Builder()
            .addTransportType(NetworkCapabilities.TRANSPORT_WIFI)
            .build()
        connectivity.requestNetwork(request, callback, Handler(Looper.getMainLooper()))
        requested = true
    }

    fun stop() {
        if (!requested || connectivity == null) return
        connectivity.unregisterNetworkCallback(callback)
        connectivity.bindProcessToNetwork(null)
        network = null
        requested = false
    }

    private fun bindTo(network: Network?) {
        val connectivity = connectivity ?: return
        if (network == this.network) return
        this.network = network
        if (!connectivity.bindProcessToNetwork(network)) {
            // An always-on VPN that apps may not bypass refuses this; its network is all that is left.
            Log.w(TAG, "Could not bind to Wi-Fi network $network")
            connectivity.bindProcessToNetwork(null)
        }
        onChange()
    }

    private companion object {
        const val TAG = "PocketMic"
    }
}
