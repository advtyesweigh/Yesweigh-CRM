package `in`.yesweigh.yescrm.sync

import android.app.Notification
import android.app.PendingIntent
import android.app.Service
import android.content.Intent
import android.database.ContentObserver
import android.net.Uri
import android.os.Handler
import android.os.HandlerThread
import android.os.IBinder
import android.os.Looper
import android.provider.CallLog
import androidx.core.app.NotificationCompat
import `in`.yesweigh.yescrm.MainActivity
import `in`.yesweigh.yescrm.R
import `in`.yesweigh.yescrm.YesCrmApp
import `in`.yesweigh.yescrm.data.Prefs
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Persistent foreground service: ContentObserver for immediate call sync +
 * light 60s heartbeat. No call-log polling. Work off main thread.
 */
class CallObserverService : Service() {
    private var observer: ContentObserver? = null
    private val mainHandler = Handler(Looper.getMainLooper())
    private var workerThread: HandlerThread? = null
    private var workerHandler: Handler? = null
    private val intentionalStop = AtomicBoolean(false)

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        intentionalStop.set(false)
        val thread = HandlerThread("yes-crm-sync").also { it.start() }
        workerThread = thread
        workerHandler = Handler(thread.looper)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP) {
            intentionalStop.set(true)
            stopSelf()
            return START_NOT_STICKY
        }
        intentionalStop.set(false)
        startForeground(NOTIF_ID, buildNotification())
        registerObserver()
        CallSyncScheduler.enqueuePeriodic(this)
        CallSyncScheduler.enqueueNow(this)
        scheduleHeartbeat()
        return START_STICKY
    }

    private fun scheduleHeartbeat() {
        workerHandler?.removeCallbacks(heartbeatRunnable)
        workerHandler?.post(heartbeatRunnable)
    }

    private val heartbeatRunnable = object : Runnable {
        override fun run() {
            if (Prefs.syncEnabled && Prefs.token.isNotBlank()) {
                CallSyncEngine.heartbeatQuietly()
            }
            workerHandler?.postDelayed(this, HEARTBEAT_INTERVAL_MS)
        }
    }

    private fun registerObserver() {
        if (observer != null) return
        observer = object : ContentObserver(mainHandler) {
            override fun onChange(selfChange: Boolean) {
                onChange(selfChange, null)
            }

            override fun onChange(selfChange: Boolean, uri: Uri?) {
                if (!Prefs.syncEnabled || Prefs.token.isBlank()) return
                // Light debounce so burst CallLog writes after a call don't spam WorkManager.
                mainHandler.removeCallbacks(syncRunnable)
                mainHandler.postDelayed(syncRunnable, CALL_CHANGE_DEBOUNCE_MS)
            }
        }
        contentResolver.registerContentObserver(
            CallLog.Calls.CONTENT_URI,
            true,
            observer!!,
        )
    }

    private val syncRunnable = Runnable {
        CallSyncScheduler.enqueueNow(applicationContext)
    }

    private fun buildNotification(): Notification {
        val open = PendingIntent.getActivity(
            this,
            0,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        return NotificationCompat.Builder(this, YesCrmApp.CHANNEL_SYNC)
            .setContentTitle(getString(R.string.fg_notification_title))
            .setContentText(getString(R.string.fg_notification_text))
            .setSmallIcon(R.drawable.ic_launcher)
            .setContentIntent(open)
            .setOngoing(true)
            .setSilent(true)
            .setForegroundServiceBehavior(NotificationCompat.FOREGROUND_SERVICE_IMMEDIATE)
            .build()
    }

    override fun onTaskRemoved(rootIntent: Intent?) {
        super.onTaskRemoved(rootIntent)
        // Swipe-away from recents: keep sync alive when still enabled.
        if (Prefs.syncEnabled && Prefs.token.isNotBlank() && !intentionalStop.get()) {
            start(applicationContext)
        }
    }

    override fun onDestroy() {
        observer?.let { contentResolver.unregisterContentObserver(it) }
        observer = null
        mainHandler.removeCallbacks(syncRunnable)
        workerHandler?.removeCallbacks(heartbeatRunnable)
        workerThread?.quitSafely()
        workerThread = null
        workerHandler = null
        val shouldRestart =
            !intentionalStop.get() && Prefs.syncEnabled && Prefs.token.isNotBlank()
        super.onDestroy()
        if (shouldRestart) {
            start(applicationContext)
        }
    }

    companion object {
        private const val NOTIF_ID = 4101
        private const val ACTION_STOP = "in.yesweigh.yescrm.STOP_SYNC"
        /** CRM online window is 2.5 min; 60s is a normal cadence (not aggressive). */
        private const val HEARTBEAT_INTERVAL_MS = 60_000L
        /** Debounce ContentObserver bursts without delaying push much. */
        private const val CALL_CHANGE_DEBOUNCE_MS = 400L

        fun start(context: android.content.Context) {
            val i = Intent(context, CallObserverService::class.java)
            context.startForegroundService(i)
        }

        fun stop(context: android.content.Context) {
            val i = Intent(context, CallObserverService::class.java).setAction(ACTION_STOP)
            try {
                context.startService(i)
            } catch (_: Exception) {
                context.stopService(Intent(context, CallObserverService::class.java))
            }
        }
    }
}
