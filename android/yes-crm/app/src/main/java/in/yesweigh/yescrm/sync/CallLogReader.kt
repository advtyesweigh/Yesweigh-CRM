package `in`.yesweigh.yescrm.sync

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.database.Cursor
import android.os.Build
import android.provider.CallLog
import androidx.core.content.ContextCompat

object CallLogReader {
    fun hasPermission(context: Context): Boolean {
        return ContextCompat.checkSelfPermission(
            context,
            Manifest.permission.READ_CALL_LOG,
        ) == PackageManager.PERMISSION_GRANTED
    }

    /**
     * Read calls newer than [sinceExclusiveMs] (CallLog DATE column is ms epoch).
     * Maps Android types → missed / received / rejected / outbound.
     */
    fun readNewCalls(context: Context, sinceExclusiveMs: Long, limit: Int = 100): List<CallEvent> {
        if (!hasPermission(context)) return emptyList()

        val projection = arrayOf(
            CallLog.Calls._ID,
            CallLog.Calls.NUMBER,
            CallLog.Calls.CACHED_NAME,
            CallLog.Calls.TYPE,
            CallLog.Calls.DATE,
            CallLog.Calls.DURATION,
        )
        val selection = "${CallLog.Calls.DATE} > ?"
        val args = arrayOf(sinceExclusiveMs.toString())
        val sort = "${CallLog.Calls.DATE} ASC"

        val out = ArrayList<CallEvent>()
        context.contentResolver.query(
            CallLog.Calls.CONTENT_URI,
            projection,
            selection,
            args,
            sort,
        )?.use { cursor ->
            while (cursor.moveToNext() && out.size < limit) {
                mapRow(cursor)?.let { out.add(it) }
            }
        }
        return out
    }

    /**
     * Recent calls for the in-app Call list UI (not limited to sync watermark).
     * [fromInclusiveMs] / [toExclusiveMs] filter by CallLog DATE.
     */
    fun readCallsInRange(
        context: Context,
        fromInclusiveMs: Long,
        toExclusiveMs: Long,
        limit: Int = 500,
    ): List<CallEvent> {
        if (!hasPermission(context)) return emptyList()

        val projection = arrayOf(
            CallLog.Calls._ID,
            CallLog.Calls.NUMBER,
            CallLog.Calls.CACHED_NAME,
            CallLog.Calls.TYPE,
            CallLog.Calls.DATE,
            CallLog.Calls.DURATION,
        )
        val selection = "${CallLog.Calls.DATE} >= ? AND ${CallLog.Calls.DATE} < ?"
        val args = arrayOf(fromInclusiveMs.toString(), toExclusiveMs.toString())
        val sort = "${CallLog.Calls.DATE} DESC"

        val out = ArrayList<CallEvent>()
        context.contentResolver.query(
            CallLog.Calls.CONTENT_URI,
            projection,
            selection,
            args,
            sort,
        )?.use { cursor ->
            while (cursor.moveToNext() && out.size < limit) {
                mapRow(cursor)?.let { out.add(it) }
            }
        }
        return out
    }

    private fun mapRow(cursor: Cursor): CallEvent? {
        val id = cursor.getLong(cursor.getColumnIndexOrThrow(CallLog.Calls._ID))
        val number = cursor.getString(cursor.getColumnIndexOrThrow(CallLog.Calls.NUMBER))?.trim().orEmpty()
        if (number.isBlank()) return null
        val name = cursor.getString(cursor.getColumnIndexOrThrow(CallLog.Calls.CACHED_NAME))?.trim().orEmpty()
        val type = cursor.getInt(cursor.getColumnIndexOrThrow(CallLog.Calls.TYPE))
        val dateMs = cursor.getLong(cursor.getColumnIndexOrThrow(CallLog.Calls.DATE))
        val duration = cursor.getLong(cursor.getColumnIndexOrThrow(CallLog.Calls.DURATION)).toInt().coerceAtLeast(0)

        val callType = mapType(type, duration) ?: return null
        return CallEvent(
            phoneNumber = number,
            startedAtMs = dateMs,
            callType = callType,
            durationSeconds = duration,
            contactName = name,
            androidCallId = id.toString(),
            androidTypeCode = type,
            clientEventId = "${id}_${dateMs}_$callType",
        )
    }

    private fun mapType(androidType: Int, duration: Int): String? {
        return when (androidType) {
            CallLog.Calls.MISSED_TYPE -> "missed"
            CallLog.Calls.OUTGOING_TYPE -> "outbound"
            CallLog.Calls.INCOMING_TYPE -> if (duration > 0) "received" else "missed"
            CallLog.Calls.REJECTED_TYPE -> "rejected"
            else -> {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.N &&
                    androidType == CallLog.Calls.BLOCKED_TYPE
                ) {
                    "rejected"
                } else {
                    null
                }
            }
        }
    }
}
