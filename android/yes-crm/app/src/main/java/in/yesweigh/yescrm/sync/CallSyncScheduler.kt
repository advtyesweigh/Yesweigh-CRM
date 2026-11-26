package `in`.yesweigh.yescrm.sync

import android.content.Context
import androidx.work.BackoffPolicy
import androidx.work.Constraints
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import java.util.concurrent.TimeUnit

object CallSyncScheduler {
    private const val UNIQUE_PERIODIC = "yes_crm_call_sync_periodic"
    private const val UNIQUE_NOW = "yes_crm_call_sync_now"

    private val networkConstraints: Constraints
        get() = Constraints.Builder()
            .setRequiredNetworkType(NetworkType.CONNECTED)
            .build()

    fun enqueueNow(context: Context) {
        val req = OneTimeWorkRequestBuilder<CallSyncWorker>()
            .setConstraints(networkConstraints)
            .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 30, TimeUnit.SECONDS)
            .build()
        WorkManager.getInstance(context).enqueueUniqueWork(
            UNIQUE_NOW,
            ExistingWorkPolicy.REPLACE,
            req,
        )
    }

    /** Backup when FGS is killed by OEM — not a busy loop (min 15 min). */
    fun enqueuePeriodic(context: Context) {
        val req = PeriodicWorkRequestBuilder<CallSyncWorker>(15, TimeUnit.MINUTES)
            .setConstraints(networkConstraints)
            .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 30, TimeUnit.SECONDS)
            .build()
        WorkManager.getInstance(context).enqueueUniquePeriodicWork(
            UNIQUE_PERIODIC,
            ExistingPeriodicWorkPolicy.UPDATE,
            req,
        )
    }

    fun cancel(context: Context) {
        WorkManager.getInstance(context).cancelUniqueWork(UNIQUE_PERIODIC)
        WorkManager.getInstance(context).cancelUniqueWork(UNIQUE_NOW)
    }
}
