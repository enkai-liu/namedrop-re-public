#!/usr/bin/env bash
# Build flupke-0.9.4-namedrop.jar: the Maven Central tech.kwik:flupke:0.9.4 jar with one class,
# Http3ServerConnectionImpl, rebuilt from the 0.9.4 sources with patches/flupke-0.9.4-no-authority.patch.
#
# Why: iOS's AirDrop client sends HTTP/3 requests with no :authority and no Host header. Upstream
# Flupke rejects those with H3_MESSAGE_ERROR before the handler runs, so the iPhone finishes TLS,
# gets no answer to /Hello, and shows "Error Sharing". The patch requires only :method and :path.
#
# Needs a JDK (javac), curl, unzip, zip and patch. Run it once before ./gradlew assembleDebug.
set -euo pipefail
cd "$(dirname "$0")"
REPO="$(cd ../../.. && pwd)"
M=https://repo1.maven.org/maven2
J=build/jars
rm -rf build/cls build/src && mkdir -p "$J" build/cls build/src

sha1() { if command -v sha1sum >/dev/null; then sha1sum "$1"; else shasum -a 1 "$1"; fi | cut -c1-40; }
fetch() {  # fetch <maven path without .jar> <out>
  [ -f "$2" ] || curl -sfL -o "$2" "$M/$1.jar"
  [ "$(curl -sfL "$M/$1.jar.sha1" | cut -c1-40)" = "$(sha1 "$2")" ] || { echo "SHA-1 mismatch: $2" >&2; exit 1; }
}
fetch tech/kwik/flupke/0.9.4/flupke-0.9.4         "$J/flupke-0.9.4.jar"
fetch tech/kwik/flupke/0.9.4/flupke-0.9.4-sources "$J/flupke-0.9.4-sources.jar"
fetch tech/kwik/kwik/0.12.1/kwik-0.12.1           "$J/kwik-0.12.1.jar"
fetch tech/kwik/agent15/4.0/agent15-4.0           "$J/agent15-4.0.jar"
fetch tech/kwik/qpack/2.0.1/qpack-2.0.1           "$J/qpack-2.0.1.jar"

F=tech/kwik/flupke/server/impl/Http3ServerConnectionImpl.java
(cd build/src && unzip -q "../../$J/flupke-0.9.4-sources.jar" "$F" && patch -s -p1 < "$REPO/patches/flupke-0.9.4-no-authority.patch")

JAVAC=javac; [ -n "${JAVA_HOME:-}" ] && JAVAC="$JAVA_HOME/bin/javac"
"$JAVAC" --release 11 -nowarn \
  -cp "$J/flupke-0.9.4.jar:$J/kwik-0.12.1.jar:$J/agent15-4.0.jar:$J/qpack-2.0.1.jar" \
  -d build/cls "build/src/$F"

cp "$J/flupke-0.9.4.jar" build/out.jar
# Replace the class and all of its inner/anonymous classes.
zip -q -d build/out.jar 'tech/kwik/flupke/server/impl/Http3ServerConnectionImpl*.class'
(cd build/cls && zip -q ../out.jar tech/kwik/flupke/server/impl/Http3ServerConnectionImpl*.class)
mv build/out.jar flupke-0.9.4-namedrop.jar
echo "built $(pwd)/flupke-0.9.4-namedrop.jar"
