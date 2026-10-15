package com.hrishikesh.shield

import android.app.Application

class ShieldApp : Application() {
    override fun onCreate() {
        super.onCreate()
        Store.init(this)
    }
}
