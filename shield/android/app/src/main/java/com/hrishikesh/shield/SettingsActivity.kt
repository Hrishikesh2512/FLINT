package com.hrishikesh.shield

import android.app.AlertDialog
import android.content.ClipData
import android.content.ClipboardManager
import android.graphics.Typeface
import android.os.Bundle
import android.view.Gravity
import android.widget.ImageButton
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity

/**
 * How his AIs reach Shield, and who has: the address (Tailscale first), the
 * token, a ready-to-paste line for any Python AI, and every AI that has used
 * it with when it was last seen. Also where the Trash is emptied.
 */
class SettingsActivity : AppCompatActivity() {

    private lateinit var column: LinearLayout

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        Store.init(this)
        column = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(16), dp(10), dp(16), dp(32))
        }
        setContentView(ScrollView(this).apply {
            fitsSystemWindows = true
            addView(column)
        })
        build()
    }

    override fun onResume() {
        super.onResume()
        build()
    }

    private fun build() {
        column.removeAllViews()
        column.addView(LinearLayout(this).apply {
            gravity = Gravity.CENTER_VERTICAL
            addView(ImageButton(context).apply {
                setImageResource(R.drawable.ic_back)
                imageTintList = android.content.res.ColorStateList.valueOf(getColor(R.color.ink))
                setBackgroundResource(R.drawable.glass_pill)
                contentDescription = getString(R.string.up)
                setOnClickListener { finish() }
            }, LinearLayout.LayoutParams(dp(44), dp(44)))
            addView(TextView(context).apply {
                setText(R.string.settings_title)
                textSize = 26f
                typeface = Typeface.create("sans-serif-light", Typeface.NORMAL)
                setTextColor(getColor(R.color.ink))
                setPadding(dp(12), 0, 0, 0)
            })
        })

        val token = Token.get(this)
        val addresses = Net.addresses()
        val best = addresses.firstOrNull()?.second ?: "PHONE-ADDRESS"

        card("YOUR AIs REACH SHIELD AT") {
            text(if (ShieldService.running) "Running on port ${Api.PORT}" else "Starting…",
                if (ShieldService.running) R.color.shield else R.color.dim)
            if (addresses.none { it.first.startsWith("Tailscale") }) {
                text("Tailscale isn't on. Install it from the Play Store and sign in on this phone " +
                    "and your other devices, then they reach Shield from anywhere, privately.", R.color.ai)
            }
            addresses.forEach { (label, ip) -> text("$label\nhttp://$ip:${Api.PORT}", R.color.ink, mono = true) }
        }

        val owner = Token.owner(this)
        card("ON YOUR LAPTOP") {
            text("Open this in any browser on a device that can reach your phone:", R.color.dim)
            text("http://$best:${Api.PORT}", R.color.ink, mono = true)
            text("Then paste your key. It acts as you, so keep it to yourself; " +
                "your AIs use the token below instead.", R.color.dim)
            button("Copy address") { copy("Shield address", "http://$best:${Api.PORT}") }
            button("Copy key") { copy("Shield key", owner) }
            button("Make a new key") {
                Token.renewOwner(this@SettingsActivity); build()
            }
        }

        card("TOKEN") {
            text("${token.take(6)}••••••••••••${token.takeLast(4)}", R.color.ink, mono = true)
            text("Every AI needs this. Jarvis on this phone gets it by itself.", R.color.dim)
            button("Copy token") { copy("Shield token", token) }
            button("Make a new token") {
                AlertDialog.Builder(this@SettingsActivity)
                    .setTitle("Make a new token?")
                    .setMessage("Every AI using the old one is locked out until you give it the new one. " +
                        "Jarvis on this phone picks it up by itself.")
                    .setPositiveButton("New token") { _, _ -> Token.renew(this@SettingsActivity); build() }
                    .setNegativeButton(android.R.string.cancel, null)
                    .show()
            }
        }

        val snippet = """from shield_client import Shield

shield = Shield("http://$best:${Api.PORT}",
                token="$token", agent="my-ai")
shield.search("passport")"""
        card("FOR ANY AI YOU BUILD (PYTHON)") {
            text(snippet, R.color.ink, mono = true)
            button("Copy") { copy("Shield for Python", snippet) }
        }

        card("FILLS ITSELF") {
            val on = AutoFill.isOn(this@SettingsActivity)
            text(if (on) "On. New screenshots, downloads, documents and WhatsApp documents come " +
                "into Inbox by themselves, and Jarvis names, sorts and reads them."
            else "Off.", if (on) R.color.shield else R.color.dim)
            if (on && !PhoneFiles.allowed(this@SettingsActivity)) {
                text("It needs \"All files access\" first: open the Phone tab and tap the message there.",
                    R.color.ai)
            }
            button(if (on) "Turn off" else "Turn on") {
                AutoFill.setOn(this@SettingsActivity, !on); build()
            }
        }

        card("WHO USES SHIELD") {
            val agents = Store.agents()
            if (agents.isEmpty()) text("No AI has used it yet.", R.color.dim)
            agents.forEach { (name, at) -> text("${Ui.who(name)}, last seen ${Ui.ago(at)}", R.color.ai) }
        }

        card("TRASH") {
            val trashed = Store.list(null, trash = true).size
            text(if (trashed == 0) "Empty." else "$trashed things in the Trash.", R.color.dim)
            if (trashed > 0) button("Empty the Trash") {
                AlertDialog.Builder(this@SettingsActivity)
                    .setTitle("Empty the Trash?")
                    .setMessage("$trashed things will be gone for good.")
                    .setPositiveButton("Empty") { _, _ -> Store.emptyTrash("you"); build() }
                    .setNegativeButton(android.R.string.cancel, null)
                    .show()
            }
        }
    }

    // ── building blocks ─────────────────────────────────────────────────────
    private fun card(title: String, fill: LinearLayout.() -> Unit) {
        column.addView(TextView(this).apply {
            text = title
            letterSpacing = 0.2f
            textSize = 11f
            setTextColor(getColor(R.color.dim))
            setPadding(dp(4), dp(22), 0, dp(8))
        })
        column.addView(LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundResource(R.drawable.glass_card)
            setPadding(dp(18), dp(12), dp(18), dp(14))
            fill()
        })
    }

    private fun LinearLayout.text(value: String, color: Int, mono: Boolean = false) {
        addView(TextView(context).apply {
            text = value
            textSize = if (mono) 13f else 14f
            if (mono) typeface = Typeface.MONOSPACE
            setTextColor(getColor(color))
            setTextIsSelectable(true)
            setPadding(0, dp(6), 0, dp(6))
        })
    }

    private fun LinearLayout.button(label: String, onClick: () -> Unit) {
        addView(TextView(context).apply {
            text = label
            textSize = 14f
            gravity = Gravity.CENTER
            setTextColor(getColor(R.color.ink))
            setBackgroundResource(R.drawable.glass_pill)
            setPadding(dp(16), dp(11), dp(16), dp(11))
            setOnClickListener { onClick() }
        }, LinearLayout.LayoutParams(-1, -2).apply { topMargin = dp(10) })
    }

    private fun copy(label: String, value: String) {
        getSystemService(ClipboardManager::class.java).setPrimaryClip(ClipData.newPlainText(label, value))
        Ui.toast(this, "Copied")
    }

    private fun dp(value: Int) = Ui.dp(this, value)
}
