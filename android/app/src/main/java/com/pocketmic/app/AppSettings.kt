package com.pocketmic.app

import android.content.Context
import android.os.Build
import androidx.core.content.edit
import com.pocketmic.app.protocol.Protocol
import java.security.SecureRandom

/** Which Android audio source to record from; the order matches R.array.mic_modes. */
enum class MicMode { STANDARD, NOISE_SUPPRESSION, RAW }

/** User settings and this install's identity, kept in SharedPreferences. */
class AppSettings(context: Context) {
    private val prefs = context.applicationContext.getSharedPreferences(FILE_NAME, Context.MODE_PRIVATE)

    /** Random non-zero u32 that identifies this phone to PCs across IP changes and restarts. */
    val phoneId: Long
        get() = synchronized(ID_LOCK) {
            prefs.getLong(KEY_PHONE_ID, 0L).takeIf { it != 0L }
                ?: newPhoneId().also { id -> prefs.edit { putLong(KEY_PHONE_ID, id) } }
        }

    var phoneName: String
        get() = prefs.getString(KEY_PHONE_NAME, null)?.takeIf { it.isNotBlank() } ?: Build.MODEL
        set(value) = prefs.edit { putString(KEY_PHONE_NAME, value.trim()) }

    var port: Int
        get() = prefs.getInt(KEY_PORT, Protocol.DEFAULT_PORT).takeIf(::isValidPort) ?: Protocol.DEFAULT_PORT
        set(value) {
            require(isValidPort(value)) { "invalid port $value" }
            prefs.edit { putInt(KEY_PORT, value) }
        }

    var micMode: MicMode
        get() = MicMode.entries.firstOrNull { it.name == prefs.getString(KEY_MIC_MODE, null) } ?: MicMode.STANDARD
        set(value) = prefs.edit { putString(KEY_MIC_MODE, value.name) }

    companion object {
        /** Unprivileged apps cannot bind below 1024. */
        const val MIN_PORT = 1024
        const val MAX_PORT = 65535

        fun isValidPort(port: Int): Boolean = port in MIN_PORT..MAX_PORT

        private const val FILE_NAME = "pocketmic"
        private const val KEY_PHONE_ID = "phone_id"
        private const val KEY_PHONE_NAME = "phone_name"
        private const val KEY_PORT = "port"
        private const val KEY_MIC_MODE = "mic_mode"
        private val ID_LOCK = Any()

        private fun newPhoneId(): Long {
            val random = SecureRandom()
            while (true) {
                val id = random.nextInt().toLong() and Protocol.U32_MAX
                if (id != 0L) return id
            }
        }
    }
}
