package com.hrishikesh.carnage

import android.app.PendingIntent
import android.appwidget.AppWidgetManager
import android.appwidget.AppWidgetProvider
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.os.Build
import android.util.SizeF
import android.widget.RemoteViews

/**
 * Jarvis on the home screen: a pane of glass with his core in it.
 *
 * Small (2x2) is the core and his name; wide (4x2 and up) adds CARNAGE and
 * what he is doing — or his last reply, when there are no headphones to say
 * it in. A tap anywhere opens a call; the screen closes itself when the call
 * ends, so it feels like the widget answered.
 *
 * The core lights up while he listens or speaks: the service reports every
 * change of state here (see [show]).
 */
class CarnageWidget : AppWidgetProvider() {

    override fun onUpdate(context: Context, manager: AppWidgetManager, ids: IntArray) {
        render(context, manager, ids)
    }

    override fun onAppWidgetOptionsChanged(
        context: Context, manager: AppWidgetManager, id: Int, options: android.os.Bundle,
    ) {
        render(context, manager, intArrayOf(id))
    }

    companion object {
        private const val PREFS = "carnage_widget"

        /** Called on every change of call state; cheap when no widget exists. */
        fun show(context: Context, state: String, reply: String? = null) {
            val prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            prefs.edit().apply {
                putString("state", state)
                if (reply != null) putString("reply", reply)
                if (state == "connecting") remove("reply")      // a new call
            }.apply()
            val manager = AppWidgetManager.getInstance(context)
            val ids = manager.getAppWidgetIds(ComponentName(context, CarnageWidget::class.java))
            if (ids.isNotEmpty()) render(context, manager, ids)
        }

        private fun render(context: Context, manager: AppWidgetManager, ids: IntArray) {
            val prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            val state = if (LiveVoice.active) prefs.getString("state", "listening") ?: "listening"
                        else "off"
            val reply = prefs.getString("reply", null)
            val live = state == "listening" || state == "speaking" || state == "connecting"
            val line = when {
                state == "connecting" -> context.getString(R.string.connecting)
                state == "listening" -> context.getString(R.string.listening)
                state == "speaking" && !Headphones.connected(context) && reply != null -> reply
                state == "speaking" -> context.getString(R.string.speaking)
                // After a silent (no headphones) call, keep his answer readable.
                !reply.isNullOrBlank() && !Headphones.connected(context) -> reply
                else -> context.getString(R.string.tap_to_talk)
            }

            fun build(layout: Int): RemoteViews = RemoteViews(context.packageName, layout).apply {
                setImageViewResource(
                    R.id.widget_orb, if (live) R.drawable.orb_core_live else R.drawable.orb_widget
                )
                if (layout == R.layout.widget_wide) setTextViewText(R.id.widget_state, line)
                setOnClickPendingIntent(R.id.widget_root, talk(context))
            }

            val views = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                // The launcher picks whichever fits as he is resized.
                RemoteViews(mapOf(
                    SizeF(110f, 110f) to build(R.layout.widget_small),
                    SizeF(220f, 110f) to build(R.layout.widget_wide),
                ))
            } else {
                build(R.layout.widget_wide)
            }
            ids.forEach { manager.updateAppWidget(it, views) }
        }

        private fun talk(context: Context): PendingIntent = PendingIntent.getActivity(
            context, 7,
            Intent(context, MainActivity::class.java)
                .setAction(MainActivity.ACTION_TALK)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
    }
}
