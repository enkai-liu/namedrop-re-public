plugins { id("com.android.application") }

android {
    namespace = "com.namedrop.card"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.namedrop.card"
        // 35 = Android 15, for setShouldDefaultToObserveModeForService.
        minSdk = 35
        targetSdk = 35
        versionCode = 1
        versionName = "0.1.0"
    }

    buildTypes {
        release { isMinifyEnabled = false }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}
