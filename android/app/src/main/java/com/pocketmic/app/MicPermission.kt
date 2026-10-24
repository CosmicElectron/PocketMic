package com.pocketmic.app

/**
 * How Start handles the microphone permission. Kept free of Android calls so it can be unit tested.
 *
 * Before asking, Android 11+ gives no way to tell "never asked" from "will not ask again":
 * shouldShowRequestPermissionRationale() is also false once an "Only this time" grant has expired,
 * after the dialog was dismissed with Back, and while the mic is set to "Ask every time". So Start
 * always lets Android ask, and only the outcome of a request can show that Android stopped asking.
 * The notification permission is optional and never holds Start up.
 */
object MicPermission {
    enum class OnStart {
        STREAM,

        /** The user denied the microphone before, so explain why it is needed, then ask. */
        EXPLAIN_THEN_ASK,
        ASK,
    }

    enum class Outcome {
        STREAM,

        /** Not allowed this time; the next Start asks again. */
        DENIED,

        /** Android will not show its dialog again; only the app's settings can allow the mic. */
        BLOCKED,
    }

    fun onStart(granted: Boolean, showRationale: Boolean): OnStart = when {
        granted -> OnStart.STREAM
        showRationale -> OnStart.EXPLAIN_THEN_ASK
        else -> OnStart.ASK
    }

    /**
     * [rationaleBefore] and [rationaleAfter] are shouldShowRequestPermissionRationale() just before
     * the request was launched and once its result arrived.
     */
    fun outcome(granted: Boolean, rationaleBefore: Boolean, rationaleAfter: Boolean): Outcome = when {
        granted -> Outcome.STREAM
        // Only a further denial after a first one makes Android stop asking ("Don't ask again" up
        // to Android 10), and that is the only way the rationale flag goes from true to false.
        rationaleBefore && !rationaleAfter -> Outcome.BLOCKED
        // A first denial, a dismissed dialog, or a request Android answered without asking at all:
        // these look alike, so Start keeps asking and the denial message links to the settings.
        else -> Outcome.DENIED
    }
}
