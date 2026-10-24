package com.pocketmic.app

import com.pocketmic.app.MicPermission.OnStart
import com.pocketmic.app.MicPermission.Outcome
import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * The permission states below are what Android reports in each situation: the rationale flag is
 * true only after a denial that left Android willing to ask again.
 */
class MicPermissionTest {

    @Test
    fun startStreamsAtOnceWhenTheMicIsAllowed() {
        assertEquals(OnStart.STREAM, MicPermission.onStart(granted = true, showRationale = false))
    }

    @Test
    fun startAsksAndroidWhenNothingWasDecidedYet() {
        // First run, an expired "Only this time" grant, "Ask every time", or a dialog dismissed
        // with Back: all report "not granted, no rationale", and Android still shows its dialog.
        assertEquals(OnStart.ASK, MicPermission.onStart(granted = false, showRationale = false))
    }

    @Test
    fun startExplainsFirstAfterAnEarlierDenial() {
        assertEquals(OnStart.EXPLAIN_THEN_ASK, MicPermission.onStart(granted = false, showRationale = true))
    }

    @Test
    fun anyGrantStreams() {
        for (before in listOf(false, true)) {
            assertEquals(Outcome.STREAM, MicPermission.outcome(granted = true, rationaleBefore = before, rationaleAfter = false))
        }
    }

    @Test
    fun aFirstDenialIsNotPermanent() {
        assertEquals(Outcome.DENIED, MicPermission.outcome(granted = false, rationaleBefore = false, rationaleAfter = true))
    }

    @Test
    fun aDismissedDialogIsNotPermanent() {
        // Back on the system dialog changes no permission flags.
        assertEquals(Outcome.DENIED, MicPermission.outcome(granted = false, rationaleBefore = false, rationaleAfter = false))
        assertEquals(Outcome.DENIED, MicPermission.outcome(granted = false, rationaleBefore = true, rationaleAfter = true))
    }

    @Test
    fun aSecondDenialBlocksFurtherRequests() {
        assertEquals(Outcome.BLOCKED, MicPermission.outcome(granted = false, rationaleBefore = true, rationaleAfter = false))
    }
}
