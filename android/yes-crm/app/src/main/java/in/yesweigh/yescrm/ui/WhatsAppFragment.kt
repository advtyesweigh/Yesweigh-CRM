package `in`.yesweigh.yescrm.ui

import android.os.Bundle
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import androidx.fragment.app.Fragment
import `in`.yesweigh.yescrm.R

class WhatsAppFragment : Fragment() {
    private val host: UiHost?
        get() = activity as? UiHost

    override fun onCreateView(
        inflater: LayoutInflater,
        container: ViewGroup?,
        savedInstanceState: Bundle?,
    ): View = inflater.inflate(R.layout.fragment_whatsapp, container, false)

    override fun onViewCreated(view: View, savedInstanceState: Bundle?) {
        super.onViewCreated(view, savedInstanceState)
        view.findViewById<View>(R.id.btnOpenWhatsApp).setOnClickListener { host?.launchWhatsApp() }
        view.findViewById<View>(R.id.waOpenAi).setOnClickListener { host?.navigateTo(Page.AI) }
        view.findViewById<View>(R.id.waOpenCalls).setOnClickListener { host?.navigateTo(Page.CALLS) }
    }
}
