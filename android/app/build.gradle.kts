import java.util.Properties

plugins {
    alias(libs.plugins.android.application)
}

// One version for the whole project, kept in the repo-root VERSION file (release.ps1 sets it).
val appVersion = providers.fileContents(rootProject.layout.projectDirectory.file("../VERSION"))
    .asText.get().trim()
val (versionMajor, versionMinor, versionPatch) =
    requireNotNull(Regex("""(\d+)\.(\d+)\.(\d+)""").matchEntire(appVersion)) {
        "VERSION must look like 1.2.3, got '$appVersion'"
    }.destructured.toList().map { it.toInt() }

// The release key lives outside the repo. Obtainium only installs updates signed with the same
// key, so every release must use it. Without it, assembleRelease produces an unsigned APK.
val keystoreProperties = Properties().apply {
    val path = providers.environmentVariable("POCKETMIC_KEYSTORE_PROPERTIES")
        .orElse(providers.systemProperty("user.home").map { "$it/.pocketmic/keystore.properties" })
        .get()
    val file = File(path)
    if (file.isFile) file.inputStream().use { load(it) }
}

android {
    namespace = "com.pocketmic.app"
    compileSdk {
        version = release(36)
    }

    defaultConfig {
        applicationId = "com.pocketmic.app"
        minSdk = 26
        targetSdk = 36
        versionCode = versionMajor * 10000 + versionMinor * 100 + versionPatch
        versionName = appVersion
    }

    signingConfigs {
        if (!keystoreProperties.isEmpty) {
            create("release") {
                storeFile = file(keystoreProperties.getProperty("storeFile"))
                storePassword = keystoreProperties.getProperty("storePassword")
                keyAlias = keystoreProperties.getProperty("keyAlias")
                keyPassword = keystoreProperties.getProperty("keyPassword")
            }
        }
    }

    buildTypes {
        release {
            signingConfig = signingConfigs.findByName("release")
            optimization {
                enable = false
            }
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    buildFeatures {
        viewBinding = true
    }
}

dependencies {
    implementation(libs.androidx.appcompat)
    implementation(libs.androidx.core.ktx)
    implementation(libs.material)
    testImplementation(libs.junit)
}
