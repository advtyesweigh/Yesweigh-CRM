package `in`.yesweigh.yescrm.ui

import android.graphics.Typeface
import android.os.Bundle
import android.util.TypedValue
import android.view.Gravity
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.view.inputmethod.EditorInfo
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.TextView
import androidx.core.content.ContextCompat
import androidx.fragment.app.Fragment
import `in`.yesweigh.yescrm.R

class AiFragment : Fragment() {
    private var mode = "ask"

    private val askPrompts = listOf(
        R.drawable.ic_chart to "Show my call performance today",
        R.drawable.ic_nav_attendance to "How many late hours this month?",
        R.drawable.ic_message to "Which dealers need follow-up?",
        R.drawable.ic_sparkles to "Show pending stamping requests",
    )
    private val taskPrompts = listOf(
        R.drawable.ic_check to "Prepare my dealer follow-up list",
        R.drawable.ic_message to "Draft a quotation follow-up",
        R.drawable.ic_chart to "Prepare today’s work summary",
        R.drawable.ic_calendar to "Prepare a pending commitments report",
    )

    override fun onCreateView(
        inflater: LayoutInflater,
        container: ViewGroup?,
        savedInstanceState: Bundle?,
    ): View = inflater.inflate(R.layout.fragment_ai, container, false)

    override fun onViewCreated(view: View, savedInstanceState: Bundle?) {
        super.onViewCreated(view, savedInstanceState)
        bindFeature(view.findViewById(R.id.feat1), R.drawable.bg_symbol_violet, R.drawable.ic_message, R.color.yes_violet, "Conversation intelligence", "Summaries, suggested replies and next actions")
        bindFeature(view.findViewById(R.id.feat2), R.drawable.bg_symbol_green, R.drawable.ic_mic, R.color.yes_green, "Voice & translation", "Transcribe and translate synced messages in CRM")
        bindFeature(view.findViewById(R.id.feat3), R.drawable.bg_symbol_amber, R.drawable.ic_check, R.color.yes_amber, "Commitments & follow-ups", "Track promises and prepare business tasks")
        bindFeature(view.findViewById(R.id.feat4), R.drawable.bg_symbol, R.drawable.ic_chart, R.color.yes_blue, "Dealer & service intelligence", "Enquiries, orders, payments, stamping and support")

        view.findViewById<View>(R.id.askMode).setOnClickListener { setMode("ask") }
        view.findViewById<View>(R.id.taskMode).setOnClickListener { setMode("task") }
        view.findViewById<View>(R.id.btnSendAi).setOnClickListener { send() }
        view.findViewById<EditText>(R.id.aiInput).setOnEditorActionListener { _, actionId, _ ->
            if (actionId == EditorInfo.IME_ACTION_DONE || actionId == EditorInfo.IME_ACTION_SEND) {
                send(); true
            } else false
        }

        val log = view.findViewById<LinearLayout>(R.id.chatLog)
        log.removeAllViews()
        log.addView(bubble("How can I help you today? Ask about your work or choose a task below.", false))
        setMode("ask")
    }

    private fun setMode(m: String) {
        mode = m
        val v = view ?: return
        val ask = v.findViewById<TextView>(R.id.askMode)
        val task = v.findViewById<TextView>(R.id.taskMode)
        ask.setBackgroundResource(if (m == "ask") R.drawable.bg_mode_btn_active else R.drawable.bg_mode_btn)
        task.setBackgroundResource(if (m == "task") R.drawable.bg_mode_btn_active else R.drawable.bg_mode_btn)
        ask.setTextColor(ContextCompat.getColor(requireContext(), if (m == "ask") R.color.yes_blue else R.color.yes_ink))
        task.setTextColor(ContextCompat.getColor(requireContext(), if (m == "task") R.color.yes_blue else R.color.yes_ink))
        v.findViewById<TextView>(R.id.aiPromptTitle).text =
            if (m == "ask") "Suggested questions" else "Suggested tasks"
        v.findViewById<EditText>(R.id.aiInput).hint =
            if (m == "ask") "Ask about your work…" else "Describe a task…"
        renderPrompts()
    }

    private fun renderPrompts() {
        val box = view?.findViewById<LinearLayout>(R.id.aiPrompts) ?: return
        box.removeAllViews()
        val items = if (mode == "ask") askPrompts else taskPrompts
        items.forEachIndexed { index, (icon, text) ->
            val row = LinearLayout(requireContext()).apply {
                orientation = LinearLayout.HORIZONTAL
                gravity = Gravity.CENTER_VERTICAL
                setBackgroundResource(R.drawable.bg_card)
                setPadding(dp(10), dp(11), dp(10), dp(11))
                val lp = LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT,
                    ViewGroup.LayoutParams.WRAP_CONTENT,
                )
                if (index > 0) lp.topMargin = dp(7)
                layoutParams = lp
                isClickable = true
                isFocusable = true
                setOnClickListener {
                    view?.findViewById<EditText>(R.id.aiInput)?.setText(text)
                    view?.findViewById<EditText>(R.id.aiInput)?.setSelection(text.length)
                }
            }
            row.addView(
                ImageView(requireContext()).apply {
                    layoutParams = ViewGroup.LayoutParams(dp(16), dp(16))
                    setImageResource(icon)
                    imageTintList = ContextCompat.getColorStateList(requireContext(), R.color.yes_blue)
                },
            )
            row.addView(
                TextView(requireContext()).apply {
                    this.text = text
                    layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f).also {
                        it.marginStart = dp(9)
                    }
                    setTextSize(TypedValue.COMPLEX_UNIT_SP, 12f)
                    setTextColor(ContextCompat.getColor(requireContext(), R.color.yes_ink))
                },
            )
            row.addView(
                ImageView(requireContext()).apply {
                    layoutParams = ViewGroup.LayoutParams(dp(16), dp(16))
                    setImageResource(R.drawable.ic_chevron_right)
                    imageTintList = ContextCompat.getColorStateList(requireContext(), R.color.yes_muted)
                },
            )
            box.addView(row)
        }
    }

    private fun send() {
        val input = view?.findViewById<EditText>(R.id.aiInput) ?: return
        val q = input.text?.toString()?.trim().orEmpty()
        if (q.isBlank()) return
        val log = view?.findViewById<LinearLayout>(R.id.chatLog) ?: return
        log.addView(bubble(q, true))
        input.setText("")
        val l = q.lowercase()
        val answer = when {
            mode == "task" ->
                "Task draft prepared for review.\n\nRequested task: $q\n\nNo live action has been executed. In production, the AI would validate permissions and request approval where needed."
            l.contains("call") ->
                "Call activity is loaded from this phone’s call log and synced to crm.yesweigh.in. Open Call list for filters, talk time and follow-ups."
            l.contains("late") || l.contains("attendance") ->
                "Attendance UI shows sample ADMS data this month (19 present days, 1h 25m late). Live ADMS wiring can replace the demo calendar."
            l.contains("follow") || l.contains("stamping") ->
                "I would check your assigned dealer conversations and verified CRM records. This on-device demo has no live conversation or stamping data."
            else ->
                "This on-device assistant can demo the AI interface. Live CRM AI answers need authorised server access."
        }
        log.addView(bubble(answer, false))
    }

    private fun bubble(text: String, user: Boolean): View {
        val box = LinearLayout(requireContext()).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundResource(if (user) R.drawable.bg_bubble_user else R.drawable.bg_bubble)
            setPadding(dp(14), dp(12), dp(14), dp(12))
            val lp = LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT,
                ViewGroup.LayoutParams.WRAP_CONTENT,
            )
            lp.topMargin = dp(10)
            lp.gravity = if (user) Gravity.END else Gravity.START
            if (user) lp.marginStart = dp(40) else lp.marginEnd = dp(12)
            layoutParams = lp
        }
        if (!user) {
            box.addView(
                TextView(requireContext()).apply {
                    this.text = "YESWEIGH AI"
                    setTextSize(TypedValue.COMPLEX_UNIT_SP, 10f)
                    setTypeface(typeface, Typeface.BOLD)
                    setTextColor(ContextCompat.getColor(requireContext(), R.color.yes_muted))
                    setPadding(0, 0, 0, dp(5))
                },
            )
        }
        box.addView(
            TextView(requireContext()).apply {
                this.text = text
                setTextSize(TypedValue.COMPLEX_UNIT_SP, 13f)
                setTextColor(
                    ContextCompat.getColor(
                        requireContext(),
                        if (user) R.color.yes_white else R.color.yes_ink,
                    ),
                )
            },
        )
        return box
    }

    private fun bindFeature(root: View, bg: Int, icon: Int, tint: Int, title: String, sub: String) {
        root.findViewById<FrameLayout>(R.id.featSymbol).setBackgroundResource(bg)
        val iv = root.findViewById<ImageView>(R.id.featIcon)
        iv.setImageResource(icon)
        iv.imageTintList = ContextCompat.getColorStateList(requireContext(), tint)
        root.findViewById<TextView>(R.id.featTitle).text = title
        root.findViewById<TextView>(R.id.featSub).text = sub
    }

    private fun dp(v: Int): Int =
        TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, v.toFloat(), resources.displayMetrics).toInt()
}
