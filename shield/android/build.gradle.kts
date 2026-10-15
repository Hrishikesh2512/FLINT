// The same plugin versions as Carnage, so the two apps build with one
// toolchain. Shield is plain Kotlin: no Python in it.
plugins {
    id("com.android.application") version "8.5.2" apply false
    id("org.jetbrains.kotlin.android") version "1.9.24" apply false
}
