#!/usr/bin/env bash
# Compile MoseyStart and MdnsAnnounce into build/awdl-root.dex for app_process.
# Needs the Android SDK (platform 35 and any build-tools) and a JDK.
set -euo pipefail
cd "$(dirname "$0")"
SDK="${ANDROID_HOME:-}"
[ -n "$SDK" ] || for d in "$HOME/Android/Sdk" "$HOME/Library/Android/sdk"; do [ -d "$d" ] && SDK="$d" && break; done
[ -d "$SDK" ] || { echo "set ANDROID_HOME to your Android SDK" >&2; exit 1; }
JAR="$SDK/platforms/android-35/android.jar"
D8="$(ls -d "$SDK"/build-tools/*/ | sort -V | tail -1)d8"
JAVAC=javac; [ -n "${JAVA_HOME:-}" ] && JAVAC="$JAVA_HOME/bin/javac"

rm -rf build && mkdir -p build
"$JAVAC" --release 11 -cp "$JAR" -d build MoseyStart.java MdnsAnnounce.java
"$D8" --min-api 30 --lib "$JAR" --output build build/*.class
mv build/classes.dex build/awdl-root.dex
echo "built $(pwd)/build/awdl-root.dex"
