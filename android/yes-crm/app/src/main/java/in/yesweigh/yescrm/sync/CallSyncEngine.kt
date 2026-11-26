package `in`.yesweigh.yescrm.sync

import android.content.Context
import `in`.yesweigh.yescrm.data.ApiClient
import `in`.yesweigh.yescrm.data.Prefs
import `in`.yesweigh.yescrm.data.db.LocalStore
import java.io.IOException
import kotlinx.coroutines.runBlocking

object CallSyncEngine {
    /** Soft presence ping; failures are ignored (WorkManager / FG service retry). */
    fun heartbeatQuietly() {
        val token = Prefs.token
        if (token.isBlank() || !Prefs.syncEnabled) return
        try {
            ApiClient.postHeartbeat(token)
        } catch (_: IOException) {
            // offline — next periodic tick or call sync will retry
        }
    }

    @Synchronized
    fun syncOnce(context: Context): SyncResult {
        val token = Prefs.token
        if (token.isBlank()) return SyncResult(false, "Not signed in")
        if (!CallLogReader.hasPermission(context)) {
            return SyncResult(false, "Call log permission missing")
        }

        heartbeatQuietly()

        var pushed = 0
        var failed = 0

        // 1) Drain SQLite offline queue first
        val queued = runBlocking { LocalStore.pendingCalls() }
        if (queued.isNotEmpty()) {
            val (okIds, leftover) = pushEvents(token, queued)
            pushed += okIds.size
            failed += leftover.size
            runBlocking {
                if (okIds.isNotEmpty()) LocalStore.removeQueued(okIds)
                if (leftover.isNotEmpty()) {
                    LocalStore.enqueueCalls(leftover, "retry pending")
                }
            }
            leftover.maxOfOrNull { it.startedAtMs }?.let { advanceWatermark(it) }
            okIds.mapNotNull { id -> queued.find { it.clientEventId == id } }
                .maxOfOrNull { it.startedAtMs }
                ?.let { advanceWatermark(it) }
        }

        // 2) New calls from device CallLog since watermark
        val since = Prefs.lastSeenCallDateMs
        val events = CallLogReader.readNewCalls(context, since)
        if (events.isEmpty() && queued.isEmpty()) {
            Prefs.lastSyncAt = System.currentTimeMillis()
            Prefs.lastSyncMessage = "Up to date"
            runBlocking { LocalStore.mirrorSessionFromPrefs() }
            return SyncResult(true, "Up to date", 0)
        }

        if (events.isNotEmpty()) {
            val (okIds, leftover) = pushEvents(token, events)
            pushed += okIds.size
            failed += leftover.size
            val okEvents = events.filter { it.clientEventId in okIds.toSet() }
            okEvents.maxOfOrNull { it.startedAtMs }?.let { advanceWatermark(it) }
            if (leftover.isNotEmpty()) {
                runBlocking { LocalStore.enqueueCalls(leftover, "offline / upload failed") }
            }
        }

        Prefs.lastSyncAt = System.currentTimeMillis()
        val queueLeft = runBlocking { LocalStore.queueCount() }
        val msg = when {
            failed == 0 && queueLeft == 0 -> "Synced $pushed call(s)"
            else -> "Synced $pushed · $queueLeft queued offline"
        }
        Prefs.lastSyncMessage = msg
        runBlocking { LocalStore.mirrorSessionFromPrefs() }
        return SyncResult(failed == 0 && queueLeft == 0, msg, pushed)
    }

    private fun pushEvents(token: String, events: List<CallEvent>): Pair<List<String>, List<CallEvent>> {
        val ok = ArrayList<String>()
        val failed = ArrayList<CallEvent>()
        var idx = 0
        while (idx < events.size) {
            val ev = events[idx]
            try {
                ApiClient.postEvent(token, ev)
                ok.add(ev.clientEventId)
                idx++
            } catch (_: IOException) {
                // Try batch for remainder
                val rest = events.drop(idx)
                try {
                    ApiClient.postBatch(token, rest)
                    ok.addAll(rest.map { it.clientEventId })
                    return ok to emptyList()
                } catch (_: IOException) {
                    failed.addAll(rest)
                    return ok to failed
                }
            }
        }
        return ok to failed
    }

    private fun advanceWatermark(ms: Long) {
        if (ms > Prefs.lastSeenCallDateMs) {
            Prefs.lastSeenCallDateMs = ms
        }
    }

    /** First enable: seed watermark to now so we don't dump entire history. */
    fun seedWatermarkIfNeeded() {
        if (Prefs.lastSeenCallDateMs <= 0L) {
            Prefs.lastSeenCallDateMs = System.currentTimeMillis()
        }
    }
}

data class SyncResult(
    val ok: Boolean,
    val message: String,
    val count: Int = 0,
)
