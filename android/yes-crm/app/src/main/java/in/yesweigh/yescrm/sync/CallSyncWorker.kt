package `in`.yesweigh.yescrm.sync

import android.content.Context
import androidx.work.CoroutineWorker
import androidx.work.WorkerParameters
import `in`.yesweigh.yescrm.data.Prefs
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

class CallSyncWorker(
    appContext: Context,
    params: WorkerParameters,
) : CoroutineWorker(appContext, params) {
    override suspend fun doWork(): Result = withContext(Dispatchers.IO) {
        if (Prefs.token.isBlank() || !Prefs.syncEnabled) return@withContext Result.success()
        val result = CallSyncEngine.syncOnce(applicationContext)
        if (result.ok) Result.success() else Result.retry()
    }
}
