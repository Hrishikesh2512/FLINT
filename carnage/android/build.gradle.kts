// Versions live here so the app module reads as configuration rather than
// version arithmetic. Chaquopy is the reason this project exists at all: it
// embeds CPython in the APK, so flint_core and carnage run unmodified on the
// phone instead of being reimplemented in Kotlin.
plugins {
    id("com.android.application") version "8.5.2" apply false
    id("org.jetbrains.kotlin.android") version "1.9.24" apply false
    id("com.chaquo.python") version "15.0.1" apply false
}
