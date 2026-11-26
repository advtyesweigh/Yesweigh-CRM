package `in`.yesweigh.yescrm.ui

import `in`.yesweigh.yescrm.data.Prefs
import java.text.SimpleDateFormat
import java.util.Calendar
import java.util.Date
import java.util.Locale
import java.util.concurrent.TimeUnit

object FormatUtils {
    fun initials(name: String): String {
        val parts = name.trim().split(Regex("\\s+")).filter { it.isNotBlank() }
        return when {
            parts.isEmpty() -> "YW"
            parts.size == 1 -> parts[0].take(2).uppercase(Locale.getDefault())
            else -> "${parts[0].first()}${parts[1].first()}".uppercase(Locale.getDefault())
        }
    }

    fun agentDisplayName(): String {
        return Prefs.agentName.ifBlank { Prefs.agentEmail.ifBlank { "Agent" } }
    }

    fun greeting(): String {
        val hour = Calendar.getInstance().get(Calendar.HOUR_OF_DAY)
        val part = when {
            hour < 12 -> "Good morning"
            hour < 17 -> "Good afternoon"
            else -> "Good evening"
        }
        val first = agentDisplayName().split(" ").firstOrNull().orEmpty()
        return "$part, $first"
    }

    fun todayEyebrow(): String {
        return SimpleDateFormat("EEEE · d MMMM", Locale.ENGLISH).format(Date())
    }

    fun formatDuration(seconds: Int): String {
        if (seconds < 60) return "${seconds}s"
        val m = seconds / 60
        val s = seconds % 60
        return if (s == 0) "${m}m" else "${m}m ${s}s"
    }

    fun formatTime(ms: Long): String {
        return SimpleDateFormat("h:mm a", Locale.ENGLISH).format(Date(ms))
    }

    fun formatPhone(raw: String): String {
        val digits = raw.filter { it.isDigit() }
        return when {
            digits.length == 10 -> "+91 ${digits.take(5)} ${digits.drop(5)}"
            digits.length > 10 -> "+$digits"
            else -> raw
        }
    }

    fun typeLabel(callType: String): String = when (callType) {
        "missed" -> "Missed"
        "received" -> "Received"
        "rejected" -> "Rejected"
        "outbound" -> "Outbound"
        else -> callType.replaceFirstChar { it.uppercase() }
    }

    fun dayStartMs(cal: Calendar = Calendar.getInstance()): Long {
        return (cal.clone() as Calendar).apply {
            set(Calendar.HOUR_OF_DAY, 0)
            set(Calendar.MINUTE, 0)
            set(Calendar.SECOND, 0)
            set(Calendar.MILLISECOND, 0)
        }.timeInMillis
    }

    fun periodRange(period: String): Pair<Long, Long> {
        val now = System.currentTimeMillis()
        val end = now + TimeUnit.DAYS.toMillis(1)
        val cal = Calendar.getInstance()
        return when (period) {
            "Last 24h" -> now - TimeUnit.HOURS.toMillis(24) to end
            "Today" -> dayStartMs(cal) to end
            "This week" -> {
                cal.firstDayOfWeek = Calendar.MONDAY
                cal.set(Calendar.DAY_OF_WEEK, Calendar.MONDAY)
                dayStartMs(cal) to end
            }
            "This month" -> {
                cal.set(Calendar.DAY_OF_MONTH, 1)
                dayStartMs(cal) to end
            }
            "This year" -> {
                cal.set(Calendar.DAY_OF_YEAR, 1)
                dayStartMs(cal) to end
            }
            else -> 0L to end // Lifetime / Custom handled by caller
        }
    }
}
