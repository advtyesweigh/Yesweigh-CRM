package `in`.yesweigh.yescrm.sync

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import `in`.yesweigh.yescrm.data.Prefs

/** Restarts FGS + WorkManager after reboot / package update when logged in and sync is on. */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent?) {
        val action = intent?.action ?: return
        if (action !in ALLOWED) return
        Prefs.init(context.applicationContext)
        // syncEnabled is set true after first login+permissions (and stays on across reboot).
        if (Prefs.token.isBlank() || !Prefs.syncEnabled) return
        CallSyncScheduler.enqueuePeriodic(context)
        CallSyncScheduler.enqueueNow(context)
        CallObserverService.start(context)
    }

    companion object {
        private val ALLOWED = setOf(
            Intent.ACTION_BOOT_COMPLETED,
            Intent.ACTION_LOCKED_BOOT_COMPLETED,
            Intent.ACTION_MY_PACKAGE_REPLACED,
            "android.intent.action.QUICKBOOT_POWERON",
            "com.htc.intent.action.QUICKBOOT_POWERON",
        )
    }
}
