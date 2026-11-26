package `in`.yesweigh.yescrm.ui

import android.graphics.Typeface
import android.os.Bundle
import android.util.TypedValue
import android.view.Gravity
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.FrameLayout
import android.widget.GridLayout
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.TextView
import androidx.core.content.ContextCompat
import androidx.fragment.app.Fragment
import androidx.lifecycle.lifecycleScope
import `in`.yesweigh.yescrm.R
import `in`.yesweigh.yescrm.data.db.LeaveRequestEntity
import `in`.yesweigh.yescrm.data.db.LocalStore
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.text.SimpleDateFormat
import java.util.Calendar
import java.util.Locale

class AttendanceFragment : Fragment() {
    private val host: UiHost? get() = activity as? UiHost

    private var month = 10 // Nov 2026 demo default
    private var year = 2026
    private val lates = mapOf(3 to 12, 7 to 15, 12 to 20, 18 to 30, 25 to 8)

    override fun onCreateView(
        inflater: LayoutInflater,
        container: ViewGroup?,
        savedInstanceState: Bundle?,
    ): View = inflater.inflate(R.layout.fragment_attendance, container, false)

    override fun onViewCreated(view: View, savedInstanceState: Bundle?) {
        super.onViewCreated(view, savedInstanceState)
        val now = Calendar.getInstance()
        // Keep demo Nov 2026 if we're in that month/year context from mockup; else current
        if (!(now.get(Calendar.YEAR) == 2026 && now.get(Calendar.MONTH) == Calendar.NOVEMBER)) {
            // still show demo month for sample ADMS calendar as in mockup
            month = Calendar.NOVEMBER
            year = 2026
        }

        view.findViewById<View>(R.id.attTab).setOnClickListener { showPanel(true) }
        view.findViewById<View>(R.id.leaveTab).setOnClickListener { showPanel(false) }
        view.findViewById<View>(R.id.btnPrevMonth).setOnClickListener { changeMonth(-1) }
        view.findViewById<View>(R.id.btnNextMonth).setOnClickListener { changeMonth(1) }
        view.findViewById<View>(R.id.btnApplyLeaveTop).setOnClickListener { host?.openLeaveDialog() }
        view.findViewById<View>(R.id.btnApplyLeaveLink).setOnClickListener { host?.openLeaveDialog() }
        view.findViewById<View>(R.id.btnNewLeave).setOnClickListener { host?.openLeaveDialog() }

        renderAtt()
        refreshLeaves()
        arguments?.getBoolean(ARG_LEAVE)?.let { if (it) showPanel(false) }
    }

    override fun onResume() {
        super.onResume()
        refreshLeaves()
    }

    fun showLeaveTab() = showPanel(false)

    private fun showPanel(attendance: Boolean) {
        val v = view ?: return
        val attTab = v.findViewById<TextView>(R.id.attTab)
        val leaveTab = v.findViewById<TextView>(R.id.leaveTab)
        attTab.setBackgroundResource(if (attendance) R.drawable.bg_tab_active else 0)
        leaveTab.setBackgroundResource(if (!attendance) R.drawable.bg_tab_active else 0)
        attTab.setTextColor(ContextCompat.getColor(requireContext(), if (attendance) R.color.yes_blue else R.color.yes_muted))
        leaveTab.setTextColor(ContextCompat.getColor(requireContext(), if (!attendance) R.color.yes_blue else R.color.yes_muted))
        v.findViewById<View>(R.id.attPanel).visibility = if (attendance) View.VISIBLE else View.GONE
        v.findViewById<View>(R.id.leavePanel).visibility = if (attendance) View.GONE else View.VISIBLE
    }

    private fun changeMonth(delta: Int) {
        val cal = Calendar.getInstance().apply {
            set(Calendar.YEAR, year)
            set(Calendar.MONTH, month)
            set(Calendar.DAY_OF_MONTH, 1)
            add(Calendar.MONTH, delta)
        }
        year = cal.get(Calendar.YEAR)
        month = cal.get(Calendar.MONTH)
        renderAtt()
    }

    private fun renderAtt() {
        val v = view ?: return
        val isDemo = month == Calendar.NOVEMBER && year == 2026
        val labelCal = Calendar.getInstance().apply {
            set(Calendar.YEAR, year)
            set(Calendar.MONTH, month)
            set(Calendar.DAY_OF_MONTH, 1)
        }
        v.findViewById<TextView>(R.id.monthTitle).text =
            SimpleDateFormat("MMMM yyyy", Locale.ENGLISH).format(labelCal.time)

        val grid = v.findViewById<GridLayout>(R.id.calendar)
        grid.removeAllViews()
        listOf("M", "T", "W", "T", "F", "S", "S").forEach { d ->
            grid.addView(
                TextView(requireContext()).apply {
                    text = d
                    gravity = Gravity.CENTER
                    setTextSize(TypedValue.COMPLEX_UNIT_SP, 10f)
                    setTypeface(typeface, Typeface.BOLD)
                    setTextColor(ContextCompat.getColor(requireContext(), R.color.yes_muted))
                    layoutParams = cellLp()
                    setPadding(0, dp(4), 0, dp(4))
                },
            )
        }

        val first = (labelCal.get(Calendar.DAY_OF_WEEK) + 5) % 7 // Monday=0
        val days = labelCal.getActualMaximum(Calendar.DAY_OF_MONTH)
        repeat(first) {
            grid.addView(View(requireContext()).apply { layoutParams = cellLp() })
        }
        for (d in 1..days) {
            val dayCal = Calendar.getInstance().apply {
                set(year, month, d)
            }
            val sunday = dayCal.get(Calendar.DAY_OF_WEEK) == Calendar.SUNDAY
            var cls = if (sunday) "off" else ""
            if (isDemo && d <= 25 && !sunday) {
                cls = when {
                    d == 2 -> "off"
                    d == 16 -> "leave"
                    lates.containsKey(d) -> "late"
                    else -> "present"
                }
            }
            val current = isDemo && d == 25
            grid.addView(dayCell(d, cls, current))
        }

        val totals = v.findViewById<GridLayout>(R.id.attTotals)
        totals.removeAllViews()
        val nums = if (isDemo) listOf("19", "1 day", "5", "1h 25m") else listOf("—", "—", "—", "—")
        val specs = listOf(
            Triple(R.drawable.bg_symbol_green, R.drawable.ic_nav_attendance to R.color.yes_green, "Present days"),
            Triple(R.drawable.bg_symbol_violet, R.drawable.ic_calendar to R.color.yes_violet, "Approved leave"),
            Triple(R.drawable.bg_symbol_amber, R.drawable.ic_clock to R.color.yes_amber, "Late days"),
            Triple(R.drawable.bg_symbol_red, R.drawable.ic_clock to R.color.yes_red, "Total late hours"),
        )
        specs.forEachIndexed { i, (bg, iconTint, label) ->
            val card = layoutInflater.inflate(R.layout.item_stat_card, totals, false)
            val lp = GridLayout.LayoutParams().apply {
                width = 0
                height = ViewGroup.LayoutParams.WRAP_CONTENT
                columnSpec = GridLayout.spec(i % 2, 1f)
                setMargins(if (i % 2 == 0) 0 else dp(5), if (i < 2) 0 else dp(10), if (i % 2 == 0) dp(5) else 0, 0)
            }
            card.layoutParams = lp
            card.findViewById<FrameLayout>(R.id.statSymbol).setBackgroundResource(bg)
            val iv = card.findViewById<ImageView>(R.id.statIcon)
            iv.setImageResource(iconTint.first)
            iv.imageTintList = ContextCompat.getColorStateList(requireContext(), iconTint.second)
            card.findViewById<TextView>(R.id.statLabel).text = label
            card.findViewById<TextView>(R.id.statValue).text = nums[i]
            totals.addView(card)
        }

        val rows = v.findViewById<LinearLayout>(R.id.attRows)
        rows.removeAllViews()
        if (isDemo) {
            listOf(
                listOf("25 Nov · Today", "09:38 AM", "—", "Late 8m"),
                listOf("24 Nov · Tue", "09:25 AM", "05:30 PM", "Present"),
                listOf("23 Nov · Mon", "09:29 AM", "05:32 PM", "Present"),
                listOf("21 Nov · Sat", "09:28 AM", "05:00 PM", "Present"),
            ).forEachIndexed { idx, row ->
                rows.addView(attRow(row[0], "${row[1]} → ${row[2]}", row[3], idx == 3))
            }
        } else {
            rows.addView(
                TextView(requireContext()).apply {
                    text = "No demonstration records for this month."
                    gravity = Gravity.CENTER
                    setPadding(dp(14), dp(25), dp(14), dp(25))
                    setTextColor(ContextCompat.getColor(requireContext(), R.color.yes_muted))
                },
            )
        }
    }

    private fun dayCell(day: Int, cls: String, current: Boolean): View {
        val tv = TextView(requireContext()).apply {
            text = day.toString()
            gravity = Gravity.CENTER
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 11f)
            setTypeface(typeface, Typeface.BOLD)
            minHeight = dp(33)
            layoutParams = cellLp().also { it.setMargins(dp(2), dp(2), dp(2), dp(2)) }
            when (cls) {
                "present" -> {
                    setBackgroundResource(R.drawable.bg_day_present)
                    setTextColor(ContextCompat.getColor(requireContext(), R.color.yes_green))
                }
                "late" -> {
                    setBackgroundResource(R.drawable.bg_day_late)
                    setTextColor(ContextCompat.getColor(requireContext(), R.color.yes_amber))
                }
                "leave" -> {
                    setBackgroundResource(R.drawable.bg_day_leave)
                    setTextColor(ContextCompat.getColor(requireContext(), R.color.yes_blue))
                }
                "off" -> {
                    setBackgroundResource(R.drawable.bg_day)
                    setTextColor(ContextCompat.getColor(requireContext(), R.color.yes_muted))
                }
                else -> {
                    setBackgroundResource(R.drawable.bg_day)
                    setTextColor(ContextCompat.getColor(requireContext(), R.color.yes_ink))
                }
            }
            if (current) {
                foreground = ContextCompat.getDrawable(requireContext(), R.drawable.bg_day_current)
            }
        }
        return tv
    }

    private fun attRow(title: String, sub: String, status: String, last: Boolean): View {
        val row = LinearLayout(requireContext()).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setPadding(dp(12), dp(13), dp(12), dp(13))
            if (!last) setBackgroundResource(R.drawable.bg_row_divider)
        }
        val present = status == "Present"
        val symbol = FrameLayout(requireContext()).apply {
            layoutParams = ViewGroup.LayoutParams(dp(31), dp(31))
            setBackgroundResource(if (present) R.drawable.bg_symbol_green else R.drawable.bg_symbol_amber)
        }
        symbol.addView(
            ImageView(requireContext()).apply {
                layoutParams = FrameLayout.LayoutParams(dp(16), dp(16), Gravity.CENTER)
                setImageResource(R.drawable.ic_nav_attendance)
                imageTintList = ContextCompat.getColorStateList(
                    requireContext(),
                    if (present) R.color.yes_green else R.color.yes_amber,
                )
            },
        )
        val grow = LinearLayout(requireContext()).apply {
            orientation = LinearLayout.VERTICAL
            layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f).also {
                it.marginStart = dp(11)
            }
        }
        grow.addView(
            TextView(requireContext()).apply {
                text = title
                setTypeface(typeface, Typeface.BOLD)
                setTextSize(TypedValue.COMPLEX_UNIT_SP, 13f)
                setTextColor(ContextCompat.getColor(requireContext(), R.color.yes_ink))
            },
        )
        grow.addView(
            TextView(requireContext()).apply {
                text = sub
                setTextSize(TypedValue.COMPLEX_UNIT_SP, 11f)
                setTextColor(ContextCompat.getColor(requireContext(), R.color.yes_muted))
            },
        )
        val chip = TextView(requireContext()).apply {
            text = status
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 10f)
            setTypeface(typeface, Typeface.BOLD)
            setPadding(dp(9), dp(5), dp(9), dp(5))
            setBackgroundResource(if (present) R.drawable.bg_status_green else R.drawable.bg_status_amber)
            setTextColor(
                ContextCompat.getColor(
                    requireContext(),
                    if (present) R.color.yes_green else R.color.yes_amber,
                ),
            )
        }
        row.addView(symbol)
        row.addView(grow)
        row.addView(chip)
        return row
    }

    private fun refreshLeaves() {
        viewLifecycleOwner.lifecycleScope.launch {
            val leaves = withContext(Dispatchers.IO) { LocalStore.allLeaves() }
            val v = view ?: return@launch
            val approved = leaves.count { it.status == "Approved" }
            val pending = leaves.count { it.status == "Pending" }
            bindStat(v.findViewById(R.id.leaveApprovedStat), R.drawable.bg_symbol_green, R.drawable.ic_nav_attendance, R.color.yes_green, "Approved this month", if (approved == 1) "1 day" else "$approved days")
            bindStat(v.findViewById(R.id.leavePendingStat), R.drawable.bg_symbol_amber, R.drawable.ic_clock, R.color.yes_amber, "Pending requests", pending.toString())
            val rows = v.findViewById<LinearLayout>(R.id.leaveRows)
            rows.removeAllViews()
            leaves.forEachIndexed { i, leave ->
                rows.addView(leaveRow(leave, i == leaves.lastIndex))
            }
        }
    }

    private fun leaveRow(leave: LeaveRequestEntity, last: Boolean): View {
        val row = LinearLayout(requireContext()).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setPadding(dp(12), dp(13), dp(12), dp(13))
            if (!last) setBackgroundResource(R.drawable.bg_row_divider)
        }
        val (bg, color) = when (leave.status) {
            "Approved" -> R.drawable.bg_symbol_green to R.color.yes_green
            "Rejected" -> R.drawable.bg_symbol_red to R.color.yes_red
            else -> R.drawable.bg_symbol_amber to R.color.yes_amber
        }
        val symbol = FrameLayout(requireContext()).apply {
            layoutParams = ViewGroup.LayoutParams(dp(38), dp(38))
            setBackgroundResource(bg)
        }
        symbol.addView(
            ImageView(requireContext()).apply {
                layoutParams = FrameLayout.LayoutParams(dp(18), dp(18), Gravity.CENTER)
                setImageResource(R.drawable.ic_calendar)
                imageTintList = ContextCompat.getColorStateList(requireContext(), color)
            },
        )
        val grow = LinearLayout(requireContext()).apply {
            orientation = LinearLayout.VERTICAL
            layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f).also {
                it.marginStart = dp(11)
            }
        }
        grow.addView(
            TextView(requireContext()).apply {
                text = leave.leaveType
                setTypeface(typeface, Typeface.BOLD)
                setTextSize(TypedValue.COMPLEX_UNIT_SP, 13f)
                setTextColor(ContextCompat.getColor(requireContext(), R.color.yes_ink))
            },
        )
        val range = if (leave.fromDate == leave.toDate) fmt(leave.fromDate) else "${fmt(leave.fromDate)} – ${fmt(leave.toDate)}"
        grow.addView(
            TextView(requireContext()).apply {
                text = range
                setTextSize(TypedValue.COMPLEX_UNIT_SP, 11f)
                setTextColor(ContextCompat.getColor(requireContext(), R.color.yes_muted))
            },
        )
        grow.addView(
            TextView(requireContext()).apply {
                text = leave.reason
                setTextSize(TypedValue.COMPLEX_UNIT_SP, 11f)
                setTextColor(ContextCompat.getColor(requireContext(), R.color.yes_muted))
            },
        )
        val chipBg = when (leave.status) {
            "Approved" -> R.drawable.bg_status_green
            "Rejected" -> R.drawable.bg_status_red
            else -> R.drawable.bg_status_amber
        }
        val chip = TextView(requireContext()).apply {
            text = leave.status
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 10f)
            setTypeface(typeface, Typeface.BOLD)
            setPadding(dp(9), dp(5), dp(9), dp(5))
            setBackgroundResource(chipBg)
            setTextColor(ContextCompat.getColor(requireContext(), color))
        }
        row.addView(symbol)
        row.addView(grow)
        row.addView(chip)
        return row
    }

    private fun bindStat(root: View, bg: Int, icon: Int, tint: Int, label: String, value: String) {
        root.findViewById<FrameLayout>(R.id.statSymbol).setBackgroundResource(bg)
        val iv = root.findViewById<ImageView>(R.id.statIcon)
        iv.setImageResource(icon)
        iv.imageTintList = ContextCompat.getColorStateList(requireContext(), tint)
        root.findViewById<TextView>(R.id.statLabel).text = label
        root.findViewById<TextView>(R.id.statValue).text = value
    }

    private fun fmt(iso: String): String {
        return try {
            val p = iso.split("-").map { it.toInt() }
            val cal = Calendar.getInstance().apply { set(p[0], p[1] - 1, p[2]) }
            SimpleDateFormat("d MMM yyyy", Locale.ENGLISH).format(cal.time)
        } catch (_: Exception) {
            iso
        }
    }

    private fun cellLp() = GridLayout.LayoutParams().apply {
        width = 0
        height = ViewGroup.LayoutParams.WRAP_CONTENT
        columnSpec = GridLayout.spec(GridLayout.UNDEFINED, 1f)
    }

    private fun dp(v: Int): Int =
        TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, v.toFloat(), resources.displayMetrics).toInt()

    companion object {
        const val ARG_LEAVE = "show_leave"
    }
}
