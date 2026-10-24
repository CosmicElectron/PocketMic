package com.pocketmic.app

import android.annotation.SuppressLint
import android.content.Context
import android.net.wifi.WifiManager
import android.os.Build
import android.os.PowerManager

/**
 * Keeps the CPU running, Wi-Fi out of power save and broadcast DISCOVER packets arriving while
 * the screen is off. Without the multicast lock many phones drop broadcasts in doze.
 */
class StreamLocks(context: Context) {
    private val appContext = context.applicationContext
    private val wakeLock = appContext.getSystemService(PowerManager::class.java)
        ?.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "PocketMic:stream")
        ?.apply { setReferenceCounted(false) }
    private val wifiManager = appContext.getSystemService(WifiManager::class.java)
    private val wifiLock = wifiManager?.createWifiLock(wifiLockMode(), "PocketMic:stream")
        ?.apply { setReferenceCounted(false) }
    private val multicastLock = wifiManager?.createMulticastLock("PocketMic:discovery")
        ?.apply { setReferenceCounted(false) }

    // No timeout: the locks live exactly as long as the user keeps PocketMic on, and release()
    // runs from MicService.onDestroy.
    @SuppressLint("WakelockTimeout")
    fun acquire() {
        wakeLock?.acquire()
        wifiLock?.acquire()
        multicastLock?.acquire()
    }

    fun release() {
        if (multicastLock?.isHeld == true) multicastLock.release()
        if (wifiLock?.isHeld == true) wifiLock.release()
        if (wakeLock?.isHeld == true) wakeLock.release()
    }

    private fun wifiLockMode(): Int =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            WifiManager.WIFI_MODE_FULL_LOW_LATENCY
        } else {
            @Suppress("DEPRECATION")
            WifiManager.WIFI_MODE_FULL_HIGH_PERF
        }
}
