package com.hrishikesh.carnage

import android.app.Application
import android.util.Log
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform

/**
 * Starts CPython once, for the life of the process.
 *
 * Chaquopy's runtime must be started before any Python is touched, and exactly
 * once — doing it here rather than in the Activity means a rotation or a
 * back-press cannot restart the interpreter underneath a running assistant.
 */
class CarnageApp : Application() {

    override fun onCreate() {
        super.onCreate()
        Bridge.attach(this)
        if (!Python.isStarted()) {
            Python.start(AndroidPlatform(this))
            Log.i("carnage", "python started")
        }
    }
}
