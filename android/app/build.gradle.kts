import java.util.Properties

// COCFARM for Android: the same bot (../../farmbot) running on the phone,
// in Python through Chaquopy. The Java side gives it the screen (screen
// capture) and the fingers (an accessibility service).
plugins {
    id("com.android.application")
    id("com.chaquo.python")
}

// This PC's Python 3.10, for preparing the app's Python packages: "python.path=" in
// local.properties, or else Chaquopy looks for "py -3.10" / python3.10 itself.
val localProperties = Properties().apply {
    rootProject.file("local.properties").takeIf { it.exists() }?.inputStream()?.use { load(it) }
}

// The release key: "release.keystore=" in local.properties names its keystore.properties (storeFile,
// keyAlias), kept outside the repository; its password is in the COCFARM_KEY_PASSWORD environment
// variable, not in any file. Phones only install an update signed with the same key as the app they
// have, so it never changes.
val releasePassword: String? = System.getenv("COCFARM_KEY_PASSWORD")
val releaseKey = localProperties.getProperty("release.keystore")?.let { file(it) }?.takeIf { it.exists() }?.let { path ->
    Properties().apply { path.inputStream().use { load(it) } }
}

// The app's version is COCFARM's (farmbot/__init__.py): updates compare it with GitHub's releases.
val cocfarmVersion = Regex("__version__ = \"([0-9.]+)\"")
    .find(rootProject.file("../farmbot/__init__.py").readText())!!.groupValues[1]

android {
    namespace = "com.cocfarm.app"
    compileSdk = 36

    defaultConfig {
        applicationId = "com.cocfarm.app"
        minSdk = 24  // Python 3.10's OpenCV for Android needs Android 7
        targetSdk = 36
        versionCode = cocfarmVersion.split(".").fold(0) { code, part -> code * 100 + part.toInt() }  // 0.6.1 -> 601
        versionName = cocfarmVersion
        ndk {
            // Phones, and x86_64 emulators such as LDPlayer and BlueStacks.
            abiFilters += listOf("arm64-v8a", "x86_64")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    signingConfigs {
        create("release") {
            if (releaseKey != null) {
                storeFile = file(releaseKey.getProperty("storeFile"))
                storePassword = releasePassword
                keyAlias = releaseKey.getProperty("keyAlias")
                keyPassword = releasePassword
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = signingConfigs.getByName("release")
        }
    }
}

// A release build without the key would make an app that can never be updated.
tasks.matching { it.name == "assembleRelease" || it.name == "packageRelease" }.configureEach {
    doFirst {
        check(releaseKey != null) {
            "No release key: set release.keystore= in android/local.properties to the key's keystore.properties"
        }
        check(!releasePassword.isNullOrEmpty()) {
            "No release key password: set the COCFARM_KEY_PASSWORD environment variable (then open a new terminal)"
        }
    }
}

chaquopy {
    defaultConfig {
        version = "3.10"  // the newest Python with an Android build of OpenCV
        localProperties.getProperty("python.path")?.let { buildPython(it) }
        pip {
            install("opencv-python")  // brings NumPy
            install("tomli")  // reads farmbot.toml (tomllib is built in from 3.11)
        }
    }
}

// The bot itself: COCFARM's farmbot package, minus the Windows-only parts.
val botSources = layout.buildDirectory.dir("botsrc")
val copyBot by tasks.registering(Sync::class) {
    from(rootProject.file("../farmbot")) {
        into("farmbot")
        exclude("**/__pycache__/**", "gui.py", "window.py", "bluestacks.py", "ldplayer.py", "cli.py", "__main__.py")
    }
    into(botSources)
}
chaquopy.sourceSets.getByName("main").srcDir(botSources)
tasks.matching { it.name.startsWith("merge") && it.name.endsWith("PythonSources") }.configureEach { dependsOn(copyBot) }

dependencies {
    // The look: Material 3 cards, switches, sliders, text fields and dialogs.
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("com.google.android.material:material:1.12.0")
    implementation("androidx.core:core-splashscreen:1.0.1")  // the launch screen, on every Android version
}
