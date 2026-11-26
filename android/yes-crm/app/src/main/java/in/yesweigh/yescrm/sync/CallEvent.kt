package `in`.yesweigh.yescrm.sync

import android.os.Build
import `in`.yesweigh.yescrm.data.Prefs
import org.json.JSONObject
import java.time.Instant
import java.time.format.DateTimeFormatter

data class CallEvent(
    val phoneNumber: String,
    val startedAtMs: Long,
    val callType: String,
    val durationSeconds: Int,
    val contactName: String = "",
    val androidCallId: String = "",
    val androidTypeCode: Int? = null,
    val clientEventId: String = "",
) {
    fun toJson(): JSONObject {
        val startedIso = DateTimeFormatter.ISO_INSTANT.format(Instant.ofEpochMilli(startedAtMs))
        val direction = if (callType == "outbound") "outbound" else "inbound"
        val meta = JSONObject()
            .put("sdk", Build.VERSION.SDK_INT)
            .put("model", Build.MODEL)
            .put("manufacturer", Build.MANUFACTURER)
        return JSONObject()
            .put("phone_number", phoneNumber)
            .put("started_at", startedIso)
            .put("call_type", callType)
            .put("duration_seconds", durationSeconds)
            .put("contact_name", contactName)
            .put("direction", direction)
            .put("android_call_id", androidCallId)
            .put("android_type_code", androidTypeCode)
            .put("device_id", Prefs.deviceId)
            .put("device_label", "${Build.MANUFACTURER} ${Build.MODEL}".trim())
            .put("client_event_id", clientEventId.ifBlank { "${androidCallId}_${startedAtMs}_$callType" })
            .put("meta", meta)
    }
}
