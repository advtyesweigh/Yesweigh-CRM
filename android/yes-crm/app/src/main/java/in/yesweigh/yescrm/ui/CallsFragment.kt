package `in`.yesweigh.yescrm.ui

import android.graphics.Typeface
import android.os.Bundle
import android.text.Editable
import android.text.TextWatcher
import android.util.TypedValue
import android.view.Gravity
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.TextView
import androidx.core.content.ContextCompat
import androidx.fragment.app.Fragment
import androidx.lifecycle.lifecycleScope
import `in`.yesweigh.yescrm.R
import `in`.yesweigh.yescrm.sync.CallEvent
import `in`.yesweigh.yescrm.sync.CallLogReader
import `in`.yesweigh.yescrm.sync.CallSyncScheduler
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.util.Calendar

class CallsFragment : Fragment() {
    private val host: UiHost? get() = activity as? UiHost

    private var callType = "All"
    private var callPeriod = "Last 24h"
    private var allCalls: List<CallEvent> = emptyList()

    private val types = listOf("All", "Missed", "Received", "Rejected", "Outbound")
    private val typeColors = listOf(
        R.color.yes_blue,
        R.color.yes_red,
        R.color.yes_green,
        R.color.yes_amber,
        R.color.yes_violet,
    )
    private val periods = listOf(
        "Last 24h", "Today", "This week", "This month", "This year", "Lifetime", "Custom",
    )

    override fun onCreateView(
        inflater: LayoutInflater,
        container: ViewGroup?,
        savedInstanceState: Bundle?,
    ): View = inflater.inflate(R.layout.fragment_calls, container, false)

    override fun onViewCreated(view: View, savedInstanceState: Bundle?) {
        super.onViewCreated(view, savedInstanceState)
        view.findViewById<TextView>(R.id.agentName).text = FormatUtils.agentDisplayName()
        view.findViewById<TextView>(R.id.agentAvatar).text = FormatUtils.initials(FormatUtils.agentDisplayName())

        view.findViewById<View>(R.id.btnRefreshCalls).setOnClickListener {
            CallSyncScheduler.enqueueNow(requireContext())
            loadCalls()
            host?.showToast("Call list refreshed")
        }
        view.findViewById<EditText>(R.id.callSearch).addTextChangedListener(simpleWatcher { render() })
        view.findViewById<EditText>(R.id.callFrom).addTextChangedListener(simpleWatcher { render() })
        view.findViewById<EditText>(R.id.callTo).addTextChangedListener(simpleWatcher { render() })

        renderPeriods()
        loadCalls()
    }

    override fun onResume() {
        super.onResume()
        loadCalls()
    }

    private fun loadCalls() {
        viewLifecycleOwner.lifecycleScope.launch {
            allCalls = withContext(Dispatchers.IO) {
                if (!CallLogReader.hasPermission(requireContext())) return@withContext emptyList()
                val end = System.currentTimeMillis() + 86_400_000L
                val startCal = Calendar.getInstance().apply { add(Calendar.YEAR, -2) }
                CallLogReader.readCallsInRange(requireContext(), startCal.timeInMillis, end)
            }
            render()
        }
    }

    private fun filteredBase(): List<CallEvent> {
        val (from, to) = when (callPeriod) {
            "Custom" -> {
                val fromStr = view?.findViewById<EditText>(R.id.callFrom)?.text?.toString().orEmpty()
                val toStr = view?.findViewById<EditText>(R.id.callTo)?.text?.toString().orEmpty()
                val fromMs = parseDateStart(fromStr) ?: 0L
                val toMs = (parseDateStart(toStr) ?: System.currentTimeMillis()) + 86_400_000L
                fromMs to toMs
            }
            "Lifetime" -> 0L to (System.currentTimeMillis() + 86_400_000L)
            else -> FormatUtils.periodRange(callPeriod)
        }
        var base = allCalls.filter { it.startedAtMs in from until to }
        val q = view?.findViewById<EditText>(R.id.callSearch)?.text?.toString()?.trim()?.lowercase().orEmpty()
        if (q.isNotBlank()) {
            val digits = q.filter { it.isDigit() }
            base = base.filter {
                it.contactName.lowercase().contains(q) ||
                    (digits.isNotBlank() && it.phoneNumber.filter { c -> c.isDigit() }.contains(digits))
            }
        }
        return base
    }

    private fun render() {
        val v = view ?: return
        val base = filteredBase()
        renderTiles(base)
        val arr = base.filter {
            callType == "All" || FormatUtils.typeLabel(it.callType) == callType
        }
        val sum = arr.sumOf { it.durationSeconds }
        val connected = arr.count { it.durationSeconds > 0 }
        v.findViewById<TextView>(R.id.totalCalls).text = arr.size.toString()
        v.findViewById<TextView>(R.id.totalTalk).text = FormatUtils.formatDuration(sum)
        v.findViewById<TextView>(R.id.avgTalk).text =
            FormatUtils.formatDuration(if (connected == 0) 0 else sum / connected)
        v.findViewById<TextView>(R.id.callCountLabel).text = "${arr.size} calls"
        v.findViewById<TextView>(R.id.agentCalls).text = "${arr.size} calls"

        val rows = v.findViewById<LinearLayout>(R.id.callRows)
        rows.removeAllViews()
        if (arr.isEmpty()) {
            rows.addView(emptyRow("No calls match these filters."))
        } else {
            arr.take(40).forEachIndexed { index, call ->
                rows.addView(callRow(call, index == arr.lastIndex || index == 39))
            }
        }

        val followups = base.filter { it.callType == "missed" || it.callType == "rejected" }
        v.findViewById<TextView>(R.id.callFollowText).text = when {
            followups.isEmpty() -> "No missed or rejected calls in this range."
            else -> "${followups.size} missed/rejected — follow up recommended."
        }
    }

    private fun renderTiles(base: List<CallEvent>) {
        val tiles = view?.findViewById<LinearLayout>(R.id.callTiles) ?: return
        tiles.removeAllViews()
        types.forEachIndexed { i, label ->
            val count = base.count { label == "All" || FormatUtils.typeLabel(it.callType) == label }
            val tile = LinearLayout(requireContext()).apply {
                orientation = LinearLayout.VERTICAL
                gravity = Gravity.CENTER_HORIZONTAL
                layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f).also {
                    if (i < types.lastIndex) it.marginEnd = dp(6)
                }
                setBackgroundResource(
                    if (callType == label) R.drawable.bg_call_tile_active else R.drawable.bg_call_tile,
                )
                setPadding(dp(3), dp(10), dp(3), dp(10))
                isClickable = true
                isFocusable = true
                setOnClickListener {
                    callType = label
                    render()
                }
            }
            val icon = ImageView(requireContext()).apply {
                layoutParams = ViewGroup.LayoutParams(dp(17), dp(17))
                setImageResource(R.drawable.ic_layers)
                imageTintList = ContextCompat.getColorStateList(requireContext(), typeColors[i])
            }
            val strong = TextView(requireContext()).apply {
                text = count.toString()
                setTextSize(TypedValue.COMPLEX_UNIT_SP, 21f)
                setTypeface(typeface, Typeface.BOLD)
                setTextColor(
                    ContextCompat.getColor(
                        requireContext(),
                        if (callType == label) R.color.yes_blue else R.color.yes_ink,
                    ),
                )
            }
            val caption = TextView(requireContext()).apply {
                text = label
                setTextSize(TypedValue.COMPLEX_UNIT_SP, 10f)
                setTypeface(typeface, Typeface.BOLD)
                setTextColor(
                    ContextCompat.getColor(
                        requireContext(),
                        if (callType == label) R.color.yes_blue else R.color.yes_muted,
                    ),
                )
            }
            tile.addView(icon)
            tile.addView(strong)
            tile.addView(caption)
            tiles.addView(tile)
        }
    }

    private fun renderPeriods() {
        val row = view?.findViewById<LinearLayout>(R.id.callPeriods) ?: return
        row.removeAllViews()
        periods.forEachIndexed { i, label ->
            val pill = TextView(requireContext()).apply {
                text = label
                setTextSize(TypedValue.COMPLEX_UNIT_SP, 12f)
                setTypeface(typeface, Typeface.BOLD)
                setPadding(dp(12), dp(8), dp(12), dp(8))
                val lp = LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.WRAP_CONTENT,
                    ViewGroup.LayoutParams.WRAP_CONTENT,
                )
                if (i > 0) lp.marginStart = dp(7)
                layoutParams = lp
                setBackgroundResource(
                    if (callPeriod == label) R.drawable.bg_pill_active else R.drawable.bg_pill,
                )
                setTextColor(
                    ContextCompat.getColor(
                        requireContext(),
                        if (callPeriod == label) R.color.yes_blue else R.color.yes_muted,
                    ),
                )
                setOnClickListener {
                    callPeriod = label
                    view?.findViewById<View>(R.id.customRange)?.visibility =
                        if (label == "Custom") View.VISIBLE else View.GONE
                    renderPeriods()
                    render()
                }
            }
            row.addView(pill)
        }
    }

    private fun callRow(call: CallEvent, last: Boolean): View {
        val row = LinearLayout(requireContext()).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setPadding(dp(12), dp(13), dp(12), dp(13))
            if (!last) {
                setBackgroundResource(R.drawable.bg_row_divider)
            }
        }
        val symbolBg = if (call.callType == "received") R.drawable.bg_symbol_green else R.drawable.bg_symbol_violet
        val tint = if (call.callType == "received") R.color.yes_green else R.color.yes_violet
        val symbol = FrameLayout(requireContext()).apply {
            layoutParams = ViewGroup.LayoutParams(dp(31), dp(31))
            setBackgroundResource(symbolBg)
        }
        symbol.addView(
            ImageView(requireContext()).apply {
                layoutParams = FrameLayout.LayoutParams(dp(16), dp(16), Gravity.CENTER)
                setImageResource(R.drawable.ic_phone_small)
                imageTintList = ContextCompat.getColorStateList(requireContext(), tint)
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
                text = call.contactName.ifBlank { "Unknown" }
                setTypeface(typeface, Typeface.BOLD)
                setTextSize(TypedValue.COMPLEX_UNIT_SP, 13f)
                setTextColor(ContextCompat.getColor(requireContext(), R.color.yes_ink))
                maxLines = 1
            },
        )
        grow.addView(
            TextView(requireContext()).apply {
                text = "${FormatUtils.formatPhone(call.phoneNumber)} · ${FormatUtils.typeLabel(call.callType)}"
                setTextSize(TypedValue.COMPLEX_UNIT_SP, 11f)
                setTextColor(ContextCompat.getColor(requireContext(), R.color.yes_muted))
            },
        )
        val end = LinearLayout(requireContext()).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.END
        }
        end.addView(
            TextView(requireContext()).apply {
                text = FormatUtils.formatDuration(call.durationSeconds)
                setTypeface(typeface, Typeface.BOLD)
                setTextSize(TypedValue.COMPLEX_UNIT_SP, 12f)
                setTextColor(ContextCompat.getColor(requireContext(), R.color.yes_ink))
            },
        )
        end.addView(
            TextView(requireContext()).apply {
                text = FormatUtils.formatTime(call.startedAtMs)
                setTextSize(TypedValue.COMPLEX_UNIT_SP, 10f)
                setTextColor(ContextCompat.getColor(requireContext(), R.color.yes_muted))
            },
        )
        row.addView(symbol)
        row.addView(grow)
        row.addView(end)
        return row
    }

    private fun emptyRow(msg: String): View {
        return TextView(requireContext()).apply {
            text = msg
            gravity = Gravity.CENTER
            setPadding(dp(14), dp(25), dp(14), dp(25))
            setTextColor(ContextCompat.getColor(requireContext(), R.color.yes_muted))
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 12f)
        }
    }

    private fun parseDateStart(s: String): Long? {
        if (!Regex("""\d{4}-\d{2}-\d{2}""").matches(s)) return null
        return try {
            val parts = s.split("-").map { it.toInt() }
            Calendar.getInstance().apply {
                set(parts[0], parts[1] - 1, parts[2], 0, 0, 0)
                set(Calendar.MILLISECOND, 0)
            }.timeInMillis
        } catch (_: Exception) {
            null
        }
    }

    private fun dp(v: Int): Int =
        TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, v.toFloat(), resources.displayMetrics).toInt()

    private fun simpleWatcher(onChange: () -> Unit) = object : TextWatcher {
        override fun beforeTextChanged(s: CharSequence?, start: Int, count: Int, after: Int) {}
        override fun onTextChanged(s: CharSequence?, start: Int, before: Int, count: Int) {}
        override fun afterTextChanged(s: Editable?) = onChange()
    }
}
