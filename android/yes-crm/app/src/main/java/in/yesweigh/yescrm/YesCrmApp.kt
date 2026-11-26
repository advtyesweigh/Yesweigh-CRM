package `in`.yesweigh.yescrm

import android.app.Application
import android.app.NotificationChannel
import android.app.NotificationManager
import android.os.Build
import `in`.yesweigh.yescrm.data.Prefs
import `in`.yesweigh.yescrm.data.db.LocalStore
import `in`.yesweigh.yescrm.sync.CallObserverService
import `in`.yesweigh.yescrm.sync.CallSyncScheduler
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch

class YesCrmApp : Application() {
    private val appScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    override fun onCreate() {
        super.onCreate()
        Prefs.init(this)
        LocalStore.init(this)
        appScope.launch { LocalStore.mirrorSessionFromPrefs() }
        createNotificationChannel()
        // After login+permissions, syncEnabled is persisted; restore FGS on process death.
        if (Prefs.token.isNotBlank() && Prefs.syncEnabled) {
            CallSyncScheduler.enqueuePeriodic(this)
            try {
                CallObserverService.start(this)
            } catch (_: Exception) {
                // OEM may delay FGS until user opens app — WorkManager still backs up.
            }
        }
    }

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val channel = NotificationChannel(
            CHANNEL_SYNC,
            getString(R.string.fg_channel_name),
            NotificationManager.IMPORTANCE_LOW,
        )
        channel.description = "Keeps YES CRM call sync reliable while you work"
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(channel)
    }

    companion object {
        const val CHANNEL_SYNC = "yes_crm_sync"
    }
}
