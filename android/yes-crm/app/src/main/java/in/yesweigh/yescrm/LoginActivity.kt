package `in`.yesweigh.yescrm

import android.content.Intent
import android.os.Bundle
import android.view.View
import android.widget.ProgressBar
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import com.google.android.material.button.MaterialButton
import com.google.android.material.textfield.TextInputEditText
import `in`.yesweigh.yescrm.data.ApiClient
import `in`.yesweigh.yescrm.data.Prefs
import `in`.yesweigh.yescrm.data.db.LocalStore
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

class LoginActivity : AppCompatActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        if (Prefs.token.isNotBlank()) {
            startActivity(Intent(this, MainActivity::class.java))
            finish()
            return
        }
        setContentView(R.layout.activity_login)

        findViewById<TextView>(R.id.loginBrand).let { brand ->
            val text = "YESWEIGH"
            val span = android.text.SpannableString(text)
            span.setSpan(
                android.text.style.ForegroundColorSpan(android.graphics.Color.parseColor("#FF6A25")),
                0,
                3,
                android.text.Spanned.SPAN_EXCLUSIVE_EXCLUSIVE,
            )
            brand.text = span
        }

        val inputUser = findViewById<TextInputEditText>(R.id.inputUser)
        val inputPassword = findViewById<TextInputEditText>(R.id.inputPassword)
        val btnLogin = findViewById<MaterialButton>(R.id.btnLogin)
        val err = findViewById<TextView>(R.id.loginError)
        val progress = findViewById<ProgressBar>(R.id.loginProgress)

        btnLogin.setOnClickListener {
            val user = inputUser.text?.toString().orEmpty().trim()
            val pass = inputPassword.text?.toString().orEmpty()
            if (user.isBlank() || pass.isBlank()) {
                err.visibility = View.VISIBLE
                err.text = "Enter CRM email/Aadhaar and password"
                return@setOnClickListener
            }
            err.visibility = View.GONE
            progress.visibility = View.VISIBLE
            btnLogin.isEnabled = false
            lifecycleScope.launch {
                try {
                    val result = withContext(Dispatchers.IO) {
                        ApiClient.mobileLogin(user, pass)
                    }
                    Prefs.token = result.accessToken
                    Prefs.agentName = result.name
                    Prefs.agentEmail = result.email
                    Prefs.agentRole = result.role
                    withContext(Dispatchers.IO) { LocalStore.mirrorSessionFromPrefs() }
                    startActivity(Intent(this@LoginActivity, MainActivity::class.java))
                    finish()
                } catch (e: Exception) {
                    err.visibility = View.VISIBLE
                    err.text = e.message ?: "Login failed"
                } finally {
                    progress.visibility = View.GONE
                    btnLogin.isEnabled = true
                }
            }
        }
    }
}
