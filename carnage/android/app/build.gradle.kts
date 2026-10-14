import java.net.URI

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.chaquo.python")
}

android {
    namespace = "com.hrishikesh.carnage"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.hrishikesh.carnage"
        minSdk = 26          // foreground services + notification channels
        targetSdk = 34
        versionCode = 1
        versionName = "0.1.0"

        // One CPython runtime is bundled per ABI. These two cover every Android
        // phone in use; adding the x86 variants would inflate the APK for
        // emulators nobody here runs.
        ndk { abiFilters += listOf("arm64-v8a", "armeabi-v7a") }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
        }
        debug {
            // The debug APK is the shipped artifact: CI signs it with the
            // standard debug key so it installs without anyone managing a
            // keystore. Fine for a personal build; a Play release would not be.
            isMinifyEnabled = false
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }

    packaging {
        resources.excludes += setOf("META-INF/*.kotlin_module", "META-INF/LICENSE*")
    }
}

chaquopy {
    defaultConfig {
        version = "3.11"
        pip {
            // Everything flint_core and carnage actually need. Both are pure
            // Python, which is the whole reason this is viable on a phone.
            install("requests")
            install("websockets")
        }
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("com.google.android.material:material:1.12.0")
    implementation("androidx.constraintlayout:constraintlayout:2.1.4")
    implementation("androidx.lifecycle:lifecycle-service:2.8.4")
    implementation("com.google.android.gms:play-services-location:21.3.0")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.8.1")
    // The Live API websocket for voice calls (LiveVoice.kt).
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    // "Hey Jarvis": the Pi's openWakeWord models, run on the phone.
    implementation("com.microsoft.onnxruntime:onnxruntime-android:1.20.0")
}

// ── the Python she actually is ──────────────────────────────────────────────
// flint_core and carnage are not vendored into this directory; they are copied
// in at build time from the repo that owns them. Copying rather than importing
// keeps one source of truth: the phone runs the same files as the Pi, and
// there is no second copy to drift.
val pythonSource = layout.projectDirectory.file("src/main/python").asFile
val repoRoot = rootProject.projectDir.parentFile.parentFile

val syncPythonPackages by tasks.registering(Copy::class) {
    description = "Copy flint_core and carnage into the APK's Python sources"
    into(pythonSource)
    from("$repoRoot/packages/flint-core/src/flint_core") { into("flint_core") }
    from("$repoRoot/carnage/src/carnage") { into("carnage") }
    exclude("**/__pycache__/**", "**/*.pyc")
}

// Chaquopy's merge task consumes the directory the copy writes, so the
// dependency has to be declared or Gradle is entitled to run them in either
// order — and does, failing the build rather than silently shipping an APK
// with no Python in it, which is the better of the two outcomes.
//
// Matched by name rather than by type: the task is Chaquopy's own
// `OutputDirTask`, not AGP's `MergeSourceSetFolders`, and naming the class
// would couple this to a plugin internal that is free to be renamed.
tasks.matching { it.name.matches(Regex("merge.*PythonSources")) }.configureEach {
    dependsOn(syncPythonPackages)
}
tasks.named("preBuild") { dependsOn(syncPythonPackages) }

// ── the wake word ───────────────────────────────────────────────────────────
// The same openWakeWord v0.5.1 models the Pi downloads (sha256-identical),
// fetched at build time rather than committed: a few MB of binaries nobody can
// review do not belong in the tree. Skipped when already present.
val wakeWordAssets = layout.projectDirectory.dir("src/main/assets/wakeword").asFile
val fetchWakeWord by tasks.registering {
    description = "Download the openWakeWord models into assets/wakeword"
    outputs.dir(wakeWordAssets)
    doLast {
        wakeWordAssets.mkdirs()
        val release = "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1"
        for (name in listOf("melspectrogram.onnx", "embedding_model.onnx", "hey_jarvis_v0.1.onnx")) {
            val target = wakeWordAssets.resolve(name)
            if (target.exists() && target.length() > 0) continue
            URI("$release/$name").toURL().openStream().use { input ->
                target.outputStream().use { input.copyTo(it) }
            }
        }
    }
}
tasks.named("preBuild") { dependsOn(fetchWakeWord) }
