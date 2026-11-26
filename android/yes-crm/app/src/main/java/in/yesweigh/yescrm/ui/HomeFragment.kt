package `in`.yesweigh.yescrm.ui

import android.os.Bundle
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.FrameLayout
import android.widget.ImageView
import android.widget.TextView
import androidx.core.content.ContextCompat
import androidx.fragment.app.Fragment
import androidx.lifecycle.lifecycleScope
import `in`.yesweigh.yescrm.R
import `in`.yesweigh.yescrm.data.Prefs
import `in`.yesweigh.yescrm.data.db.LocalStore
import `in`.yesweigh.yescrm.sync.CallLogReader
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

class HomeFragment : Fragment() {
    private val host: UiHost?
        get() = activity as? UiHost

    override fun onCreateView(
        inflater: LayoutInflater,
        container: ViewGroup?,
        savedInstanceState: Bundle?,
    ): View = inflater.inflate(R.layout.fragment_home, container, false)

    override fun onViewCreated(view: View, savedInstanceState: Bundle?) {
        super.onViewCreated(view, savedInstanceState)
        view.findViewById<TextView>(R.id.homeEyebrow).text = FormatUtils.todayEyebrow()
        view.findViewById<TextView>(R.id.homeGreeting).text = FormatUtils.greeting()

        bindStat(view.findViewById(R.id.statPresent), R.drawable.bg_symbol_green, R.drawable.ic_nav_attendance, R.color.yes_green, "Present days", "19")
        bindStat(view.findViewById(R.id.statLate), R.drawable.bg_symbol_amber, R.drawable.ic_clock, R.color.yes_amber, "Late hours", "1h 25m")
        bindStat(view.findViewById(R.id.statLeave), R.drawable.bg_symbol_violet, R.drawable.ic_calendar, R.color.yes_violet, "Approved leave", "1 day")

        view.findViewById<View>(R.id.homeViewAtt).setOnClickListener { host?.navigateTo(Page.ATTENDANCE) }
        view.findViewById<View>(R.id.homeMonthLink).setOnClickListener { host?.navigateTo(Page.ATTENDANCE) }
        view.findViewById<View>(R.id.homeCallsLink).setOnClickListener { host?.navigateTo(Page.CALLS) }
        view.findViewById<View>(R.id.qaWhatsApp).setOnClickListener { host?.launchWhatsApp() }
        view.findViewById<View>(R.id.qaCalls).setOnClickListener { host?.navigateTo(Page.CALLS) }
        view.findViewById<View>(R.id.qaAi).setOnClickListener { host?.navigateTo(Page.AI) }
        view.findViewById<View>(R.id.qaLeave).setOnClickListener { host?.openLeaveDialog() }
        view.findViewById<View>(R.id.homeAiBanner).setOnClickListener { host?.navigateTo(Page.AI) }

        refresh()
    }

    override fun onResume() {
        super.onResume()
        view?.let { refresh() }
    }

    private fun refresh() {
        val v = view ?: return
        val syncLine = when {
            !CallLogReader.hasPermission(requireContext()) -> "Allow call permissions — sync starts automatically"
            Prefs.syncEnabled -> "Sync active · call log → CRM"
            Prefs.syncStoppedByUser -> "Sync stopped — resume from notifications / profile"
            else -> "Starting sync…"
        }
        v.findViewById<TextView>(R.id.homeAttFoot).text = syncLine

        viewLifecycleOwner.lifecycleScope.launch {
            val pending = withContext(Dispatchers.IO) { LocalStore.pendingLeaveCount() }
            bindStat(
                v.findViewById(R.id.statPending),
                R.drawable.bg_symbol_red,
                R.drawable.ic_clock,
                R.color.yes_red,
                "Pending leave",
                pending.toString(),
            )

            val (from, to) = FormatUtils.periodRange("Today")
            val calls = withContext(Dispatchers.IO) {
                if (CallLogReader.hasPermission(requireContext())) {
                    CallLogReader.readCallsInRange(requireContext(), from, to)
                } else {
                    emptyList()
                }
            }
            val received = calls.count { it.callType == "received" }
            val outbound = calls.count { it.callType == "outbound" }
            val missed = calls.count { it.callType == "missed" }
            val rejected = calls.count { it.callType == "rejected" }
            val talk = calls.sumOf { it.durationSeconds }
            val connected = calls.count { it.durationSeconds > 0 }
            val ratio = if (calls.isEmpty()) 0f else connected.toFloat() / calls.size

            v.findViewById<TextView>(R.id.homeCallCount).text = "${calls.size} calls"
            v.findViewById<TextView>(R.id.homeCallBreakdown).text =
                if (!CallLogReader.hasPermission(requireContext())) {
                    "Waiting for call log permission"
                } else {
                    "$received received · $outbound outbound"
                }
            v.findViewById<TextView>(R.id.homeCallMissed).text = "$missed missed · $rejected rejected"
            v.findViewById<TextView>(R.id.homeCallTalk).text = "${FormatUtils.formatDuration(talk)} talk time"
            val bar = v.findViewById<View>(R.id.homeCallProgress)
            bar.post {
                val parent = bar.parent as View
                val lp = bar.layoutParams
                lp.width = (parent.width * ratio).toInt().coerceAtLeast(if (calls.isEmpty()) 0 else 8)
                bar.layoutParams = lp
            }
        }
    }

    private fun bindStat(
        root: View,
        bg: Int,
        icon: Int,
        tint: Int,
        label: String,
        value: String,
    ) {
        root.findViewById<FrameLayout>(R.id.statSymbol).setBackgroundResource(bg)
        val iv = root.findViewById<ImageView>(R.id.statIcon)
        iv.setImageResource(icon)
        iv.imageTintList = ContextCompat.getColorStateList(requireContext(), tint)
        root.findViewById<TextView>(R.id.statLabel).text = label
        root.findViewById<TextView>(R.id.statValue).text = value
    }
}
