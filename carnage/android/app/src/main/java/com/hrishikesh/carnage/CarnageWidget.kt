package com.hrishikesh.carnage

import android.app.PendingIntent
import android.appwidget.AppWidgetManager
import android.appwidget.AppWidgetProvider
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.os.Build
import android.util.SizeF
import android.view.View
import android.widget.RemoteViews

/**
 * Jarvis on the home screen: only the bubble.
 *
 * It breathes while idle and wobbles while he listens or speaks — frame
 * animations (orb_wobble_*.xml) inside an indeterminate ProgressBar, which is
 * the one way a home-screen widget can animate on its own. A tap talks right
 * there, through the service; the app never opens.
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
        @Volatile private var live = false

        /** Called by the service on every change of call state. */
        fun show(context: Context, state: String) {
            val now = state == "connecting" || state == "listening" || state == "speaking"
            if (now == live) return
            live = now
            val manager = AppWidgetManager.getInstance(context)
            val ids = manager.getAppWidgetIds(ComponentName(context, CarnageWidget::class.java))
            if (ids.isNotEmpty()) render(context, manager, ids)
        }

        private fun render(context: Context, manager: AppWidgetManager, ids: IntArray) {
            fun build(layout: Int) = RemoteViews(context.packageName, layout).apply {
                setViewVisibility(R.id.widget_idle, if (live) View.GONE else View.VISIBLE)
                setViewVisibility(R.id.widget_live, if (live) View.VISIBLE else View.GONE)
                setOnClickPendingIntent(R.id.widget_root, talk(context))
            }
            val views = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                // The launcher picks the largest bubble that fits as it is resized.
                RemoteViews(mapOf(
                    SizeF(110f, 110f) to build(R.layout.widget_orb_s),
                    SizeF(170f, 170f) to build(R.layout.widget_orb_m),
                    SizeF(220f, 220f) to build(R.layout.widget_orb_l),
                ))
            } else {
                build(R.layout.widget_orb_m)
            }
            ids.forEach { manager.updateAppWidget(it, views) }
        }

        /** Straight to the service: talk (or hang up) without opening the app. */
        private fun talk(context: Context): PendingIntent {
            val intent = Intent(context, CarnageService::class.java)
                .setAction(CarnageService.ACTION_TALK)
            val flags = PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
            return if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                PendingIntent.getForegroundService(context, 7, intent, flags)
            } else {
                PendingIntent.getService(context, 7, intent, flags)
            }
        }
    }
}
