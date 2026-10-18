plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

// Release signing comes from CI secrets; without them the build falls back to the
// debug key so contributors can still build and install locally.
val releaseKeystore: String? = System.getenv("PM_KEYSTORE")

android {
    namespace = "io.github.nanorex.phonemirror"
    compileSdk = 35

    defaultConfig {
        applicationId = "io.github.nanorex.phonemirror"
        minSdk = 26
        targetSdk = 35
        versionCode = (project.findProperty("pmVersionCode") as String?)?.toInt() ?: 1
        versionName = (project.findProperty("pmVersionName") as String?) ?: "0.2.0-dev"
    }

    signingConfigs {
        create("release") {
            if (releaseKeystore != null) {
                storeFile = file(releaseKeystore)
                storeType = "pkcs12"
                storePassword = System.getenv("PM_KEYSTORE_PASSWORD")
                keyAlias = System.getenv("PM_KEY_ALIAS")
                keyPassword = System.getenv("PM_KEYSTORE_PASSWORD")
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = signingConfigs.getByName(if (releaseKeystore != null) "release" else "debug")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
    testOptions {
        unitTests.all { test ->
            System.getenv("PM_INTEROP_PORT")?.let { test.environment("PM_INTEROP_PORT", it) }
        }
    }
}

dependencies {
    implementation("com.journeyapps:zxing-android-embedded:4.3.0")
    testImplementation("junit:junit:4.13.2")
}
