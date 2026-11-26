package `in`.yesweigh.yescrm

import android.Manifest
import android.annotation.SuppressLint
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.PowerManager
import android.provider.Settings
import android.text.SpannableString
import android.text.Spanned
import android.text.style.ForegroundColorSpan
import android.view.LayoutInflater
import android.view.View
import android.widget.ArrayAdapter
import android.widget.EditText
import android.widget.Spinner
import android.widget.TextView
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.fragment.app.Fragment
import androidx.lifecycle.lifecycleScope
import com.google.android.material.bottomnavigation.BottomNavigationView
import `in`.yesweigh.yescrm.data.Prefs
import `in`.yesweigh.yescrm.data.db.LocalStore
import `in`.yesweigh.yescrm.sync.CallLogReader
import `in`.yesweigh.yescrm.sync.CallObserverService
import `in`.yesweigh.yescrm.sync.CallSyncEngine
import `in`.yesweigh.yescrm.sync.CallSyncScheduler
import `in`.yesweigh.yescrm.ui.AiFragment
import `in`.yesweigh.yescrm.ui.AttendanceFragment
import `in`.yesweigh.yescrm.ui.CallsFragment
import `in`.yesweigh.yescrm.ui.FormatUtils
import `in`.yesweigh.yescrm.ui.HomeFragment
import `in`.yesweigh.yescrm.ui.Page
import `in`.yesweigh.yescrm.ui.UiHost
import `in`.yesweigh.yescrm.ui.WhatsAppFragment
import java.text.SimpleDateFormat
import java.util.Calendar
import java.util.Date
import java.util.Locale
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

class MainActivity : AppCompatActivity(), UiHost {
    private lateinit var syncStrip: View
    private lateinit var syncStripText: TextView
    private lateinit var btnStripAction: TextView
    private lateinit var bottomNav: BottomNavigationView

    private val permissionLauncher = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions(),
    ) {
        refreshSyncStrip()
        maybeAutoStartSync()
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        if (Prefs.token.isBlank()) {
            startActivity(Intent(this, LoginActivity::class.java))
            finish()
            return
        }
        setContentView(R.layout.activity_main)

        styleBrand()
        findViewById<TextView>(R.id.btnAvatar).text =
            FormatUtils.initials(FormatUtils.agentDisplayName())
        findViewById<View>(R.id.btnNotices).setOnClickListener { openNotices() }
        findViewById<View>(R.id.btnAvatar).setOnClickListener { openProfile() }

        syncStrip = findViewById(R.id.syncStrip)
        syncStripText = findViewById(R.id.syncStripText)
        btnStripAction = findViewById(R.id.btnStripAction)
        bottomNav = findViewById(R.id.bottomNav)

        bottomNav.setOnItemSelectedListener { item ->
            val page = when (item.itemId) {
                R.id.nav_home -> Page.HOME
                R.id.nav_whatsapp -> Page.WHATSAPP
                R.id.nav_calls -> Page.CALLS
                R.id.nav_ai -> Page.AI
                R.id.nav_attendance -> Page.ATTENDANCE
                else -> return@setOnItemSelectedListener false
            }
            showPage(page, fromNav = true)
            true
        }

        if (savedInstanceState == null) {
            showPage(Page.HOME, fromNav = true)
            bottomNav.selectedItemId = R.id.nav_home
        }

        if (!hasAllRequiredPermissions()) {
            requestNeededPermissions()
        } else {
            maybeAutoStartSync()
        }
    }

    override fun onResume() {
        super.onResume()
        refreshSyncStrip()
        maybeAutoStartSync()
        lifecycleScope.launch(Dispatchers.IO) { LocalStore.mirrorSessionFromPrefs() }
    }

    private fun styleBrand() {
        val tv = findViewById<TextView>(R.id.brandTitle)
        val text = "YESWEIGH"
        val span = SpannableString(text)
        span.setSpan(
            ForegroundColorSpan(Color.parseColor("#FF6A25")),
            0,
            3,
            Spanned.SPAN_EXCLUSIVE_EXCLUSIVE,
        )
        span.setSpan(
            ForegroundColorSpan(ContextCompat.getColor(this, R.color.yes_navy)),
            3,
            text.length,
            Spanned.SPAN_EXCLUSIVE_EXCLUSIVE,
        )
        tv.text = span
    }

    private fun showPage(page: Page, fromNav: Boolean) {
        val fragment: Fragment = when (page) {
            Page.HOME -> HomeFragment()
            Page.WHATSAPP -> WhatsAppFragment()
            Page.CALLS -> CallsFragment()
            Page.AI -> AiFragment()
            Page.ATTENDANCE -> AttendanceFragment()
        }
        supportFragmentManager.beginTransaction()
            .replace(R.id.navHost, fragment)
            .commit()
        if (!fromNav) {
            val id = when (page) {
                Page.HOME -> R.id.nav_home
                Page.WHATSAPP -> R.id.nav_whatsapp
                Page.CALLS -> R.id.nav_calls
                Page.AI -> R.id.nav_ai
                Page.ATTENDANCE -> R.id.nav_attendance
            }
            if (bottomNav.selectedItemId != id) {
                bottomNav.selectedItemId = id
            }
        }
    }

    override fun navigateTo(page: Page) = showPage(page, fromNav = false)

    override fun launchWhatsApp() {
        val intents = listOf(
            packageManager.getLaunchIntentForPackage("com.whatsapp"),
            packageManager.getLaunchIntentForPackage("com.whatsapp.w4b"),
        )
        val launch = intents.firstOrNull { it != null }
        if (launch != null) {
            startActivity(launch)
        } else {
            try {
                startActivity(Intent(Intent.ACTION_VIEW, Uri.parse("https://web.whatsapp.com/")))
            } catch (_: Exception) {
                showToast("WhatsApp is not installed")
            }
        }
    }

    override fun showToast(message: String) {
        Toast.makeText(this, message, Toast.LENGTH_SHORT).show()
    }

    override fun openNotices() {
        val body = buildString {
            appendLine("• Attendance reminder — today's check-out is pending (demo).")
            appendLine()
            if (!hasAllRequiredPermissions()) {
                appendLine("• Call permissions needed — sync starts automatically after allow.")
            } else if (Prefs.syncEnabled) {
                appendLine("• Call sync active — last: ${Prefs.lastSyncMessage.ifBlank { "OK" }}")
            } else {
                appendLine("• Call sync is stopped.")
            }
            val q = try {
                kotlinx.coroutines.runBlocking { LocalStore.queueCount() }
            } catch (_: Exception) {
                0
            }
            if (q > 0) appendLine("• $q call(s) queued offline in SQLite.")
        }
        AlertDialog.Builder(this)
            .setTitle("Notifications")
            .setMessage(body.trim())
            .setPositiveButton("OK", null)
            .show()
    }

    override fun openProfile() {
        val view = LayoutInflater.from(this).inflate(R.layout.dialog_profile, null, false)
        view.findViewById<TextView>(R.id.profileAvatar).text =
            FormatUtils.initials(FormatUtils.agentDisplayName())
        view.findViewById<TextView>(R.id.profileName).text = FormatUtils.agentDisplayName()
        view.findViewById<TextView>(R.id.profileMeta).text =
            "${Prefs.agentRole.ifBlank { "Staff" }} · ${Prefs.agentEmail}"
        view.findViewById<TextView>(R.id.profileSync).text = when {
            !hasAllRequiredPermissions() -> "Permissions required — sync auto-starts after allow"
            Prefs.syncEnabled -> "Sync active · ${Prefs.lastSyncMessage.ifBlank { "watching call log" }}"
            Prefs.syncStoppedByUser -> "Sync stopped"
            else -> "Starting sync…"
        }
        if (Prefs.lastSyncAt > 0L) {
            view.findViewById<TextView>(R.id.profileLastSync).text =
                SimpleDateFormat("dd MMM yyyy, hh:mm a", Locale.getDefault())
                    .format(Date(Prefs.lastSyncAt))
        }
        val dialog = AlertDialog.Builder(this).setView(view).create()
        view.findViewById<View>(R.id.btnProfileBattery).setOnClickListener {
            openBatterySettings()
        }
        view.findViewById<View>(R.id.btnProfileSyncToggle).setOnClickListener {
            if (Prefs.syncEnabled) {
                Prefs.syncStoppedByUser = true
                Prefs.syncEnabled = false
                CallObserverService.stop(this)
                CallSyncScheduler.cancel(this)
                showToast("Sync stopped")
            } else {
                if (!hasAllRequiredPermissions()) {
                    requestNeededPermissions()
                } else {
                    Prefs.syncStoppedByUser = false
                    enableAndStartSync()
                    showToast("Sync resumed")
                }
            }
            dialog.dismiss()
            refreshSyncStrip()
            lifecycleScope.launch(Dispatchers.IO) { LocalStore.mirrorSessionFromPrefs() }
        }
        view.findViewById<TextView>(R.id.btnProfileSyncToggle).text =
            if (Prefs.syncEnabled) getString(R.string.stop_sync) else getString(R.string.resume_sync)
        view.findViewById<View>(R.id.btnProfileLogout).setOnClickListener {
            Prefs.syncEnabled = false
            CallObserverService.stop(this)
            CallSyncScheduler.cancel(this)
            Prefs.clearSession()
            lifecycleScope.launch {
                withContext(Dispatchers.IO) { LocalStore.clearSession() }
                startActivity(Intent(this@MainActivity, LoginActivity::class.java))
                finish()
            }
            dialog.dismiss()
        }
        view.findViewById<View>(R.id.btnProfileClose).setOnClickListener { dialog.dismiss() }
        dialog.show()
    }

    override fun openLeaveDialog() {
        val view = LayoutInflater.from(this).inflate(R.layout.dialog_leave, null, false)
        val type = view.findViewById<Spinner>(R.id.leaveType)
        type.adapter = ArrayAdapter(
            this,
            android.R.layout.simple_spinner_dropdown_item,
            listOf("Casual leave", "Sick leave", "Other leave"),
        )
        val tomorrow = Calendar.getInstance().apply { add(Calendar.DAY_OF_MONTH, 1) }
        val iso = SimpleDateFormat("yyyy-MM-dd", Locale.US).format(tomorrow.time)
        view.findViewById<EditText>(R.id.leaveFrom).setText(iso)
        view.findViewById<EditText>(R.id.leaveTo).setText(iso)
        val dialog = AlertDialog.Builder(this).setView(view).create()
        view.findViewById<View>(R.id.btnCloseLeave).setOnClickListener { dialog.dismiss() }
        view.findViewById<View>(R.id.btnSubmitLeave).setOnClickListener {
            val from = view.findViewById<EditText>(R.id.leaveFrom).text?.toString().orEmpty()
            val to = view.findViewById<EditText>(R.id.leaveTo).text?.toString().orEmpty()
            val reason = view.findViewById<EditText>(R.id.leaveReason).text?.toString().orEmpty().trim()
            if (from.isBlank() || to.isBlank() || reason.isBlank()) {
                showToast("Fill all leave fields")
                return@setOnClickListener
            }
            if (to < from) {
                showToast("End date must be on or after start date")
                return@setOnClickListener
            }
            lifecycleScope.launch {
                withContext(Dispatchers.IO) {
                    LocalStore.addLeave(type.selectedItem.toString(), from, to, reason)
                }
                dialog.dismiss()
                showToast("Leave request saved · Pending approval")
                navigateTo(Page.ATTENDANCE)
                (supportFragmentManager.findFragmentById(R.id.navHost) as? AttendanceFragment)
                    ?.showLeaveTab()
            }
        }
        dialog.show()
    }

    /** Login + permissions → auto-enable sync (unless admin stopped). */
    private fun maybeAutoStartSync() {
        if (Prefs.token.isBlank()) return
        if (!hasAllRequiredPermissions()) return
        if (Prefs.syncStoppedByUser) return
        enableAndStartSync()
    }

    private fun enableAndStartSync() {
        Prefs.syncEnabled = true
        Prefs.syncStoppedByUser = false
        CallSyncEngine.seedWatermarkIfNeeded()
        startSyncPipeline()
        refreshSyncStrip()
        lifecycleScope.launch(Dispatchers.IO) { LocalStore.mirrorSessionFromPrefs() }
    }

    private fun startSyncPipeline() {
        CallSyncScheduler.enqueuePeriodic(this)
        CallSyncScheduler.enqueueNow(this)
        CallObserverService.start(this)
    }

    private fun refreshSyncStrip() {
        val permsOk = hasAllRequiredPermissions()
        when {
            !permsOk -> {
                syncStrip.visibility = View.VISIBLE
                syncStripText.text = "Call permissions required — sync starts automatically after allow"
                btnStripAction.text = "Allow"
                btnStripAction.setOnClickListener { requestNeededPermissions() }
            }
            Prefs.syncStoppedByUser && !Prefs.syncEnabled -> {
                syncStrip.visibility = View.VISIBLE
                syncStripText.text = "Call sync stopped"
                btnStripAction.text = "Resume"
                btnStripAction.setOnClickListener {
                    Prefs.syncStoppedByUser = false
                    enableAndStartSync()
                }
            }
            else -> {
                // Keep strip hidden when healthy; status lives in Home + Profile
                syncStrip.visibility = View.GONE
            }
        }
    }

    @SuppressLint("BatteryLife")
    private fun openBatterySettings() {
        try {
            val pm = getSystemService(PowerManager::class.java)
            if (pm != null && !pm.isIgnoringBatteryOptimizations(packageName)) {
                startActivity(
                    Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS).apply {
                        data = Uri.parse("package:$packageName")
                    },
                )
                return
            }
        } catch (_: Exception) {
            // fall through
        }
        try {
            startActivity(
                Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS).apply {
                    data = Uri.parse("package:$packageName")
                },
            )
        } catch (_: Exception) {
            // ignore
        }
    }

    private fun hasAllRequiredPermissions(): Boolean {
        if (!CallLogReader.hasPermission(this)) return false
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.READ_PHONE_STATE)
            != PackageManager.PERMISSION_GRANTED
        ) {
            return false
        }
        if (Build.VERSION.SDK_INT >= 33) {
            if (ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED
            ) {
                return false
            }
        }
        return true
    }

    private fun requestNeededPermissions() {
        val needed = mutableListOf(
            Manifest.permission.READ_CALL_LOG,
            Manifest.permission.READ_PHONE_STATE,
        )
        if (Build.VERSION.SDK_INT >= 33) {
            needed.add(Manifest.permission.POST_NOTIFICATIONS)
        }
        permissionLauncher.launch(needed.toTypedArray())
    }
}
