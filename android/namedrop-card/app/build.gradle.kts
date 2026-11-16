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

    // Agent15 registers its own KeyExchangeFactory via ServiceLoader, and its X25519 can't run on
    // Android (see h3/AndroidKeyExchangeFactory.java). pickFirst keeps the app's own service file.
    packaging {
        resources.pickFirsts += "META-INF/services/tech.kwik.agent15.engine.KeyExchangeFactory"
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

// The on-phone receiver (h3/NameDropReceiver.java): QUIC + HTTP/3, run as root from
// android/awdl-root/. Flupke 0.9.4 was built against kwik 0.10.8, but 0.10.x parses only the first
// packet of a datagram before a connection exists, so a ClientHello split across two coalesced
// Initials never completes. 0.12.1 loops over the datagram. Kwik, Agent15, Flupke and qpack are
// LGPL-3.0; dd-plist is MIT.
dependencies {
    implementation("tech.kwik:kwik:0.12.1")
    implementation("tech.kwik:agent15:4.0")
    // Flupke with one class patched to accept a request with no :authority, which iOS omits.
    // Build it first with flupke-patch/build.sh.
    implementation(files("../flupke-patch/flupke-0.9.4-namedrop.jar"))
    implementation("tech.kwik:qpack:2.0.1")
    implementation("com.googlecode.plist:dd-plist:1.28")
}
