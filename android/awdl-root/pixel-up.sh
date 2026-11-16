#!/usr/bin/env bash
# Run NameDrop entirely on a rooted Pixel: push the AWDL tools, the TLS key and the card we send,
# then start namedrop-pixel.sh on the phone as root. Run from a computer with adb.
#
#   ./android/awdl-root/pixel-up.sh               # start (holds the link for HOLD=600 s)
#   ./android/awdl-root/pixel-up.sh --stop        # tear down
#   ./android/awdl-root/pixel-up.sh --pull        # copy received cards to received/
#
# Env: HOLD (seconds), COUNTRY (Wi-Fi country code passed to mosey, default from the phone),
#      VCARD (the card we send, default samples/contact.vcf), ID, KEYS (as in mint-snapkey-cert.sh).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
D=/data/local/tmp/namedrop
ID="${ID:-$REPO/scratchpad/snap-identity.json}"
KEYS="${KEYS:-$REPO/scratchpad/asquic-keys}"
VCARD="${VCARD:-$REPO/samples/contact.vcf}"
HOLD="${HOLD:-600}"

case "${1:-}" in
  --stop) adb shell "touch $D/STOP"; echo "stop requested"; exit 0 ;;
  --pull) adb shell su -c "'chmod -R a+rX $D/received'"
          mkdir -p "$REPO/received"; adb pull "$D/received/." "$REPO/received/"; exit 0 ;;
esac

adb shell su -c id | grep -q 'uid=0' || {
  echo "no root over adb. In Magisk's Superuser tab, grant Shell root first." >&2; exit 1; }
adb shell pm path com.namedrop.card >/dev/null || {
  echo "install the namedrop-card app first (android/namedrop-card/README.md)" >&2; exit 1; }
[ -f "$KEYS/snapkey-cert.pem" ] || { echo "no cert in $KEYS; run scripts/mint-snapkey-cert.sh" >&2; exit 1; }
UUID=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['bonjour_listener_uuid'])" "$ID")
COUNTRY="${COUNTRY:-$(adb shell cmd wifi get-country-code 2>/dev/null | grep -oE '[A-Z]{2}' | tail -1 || true)}"
COUNTRY="${COUNTRY:-US}"

[ -f "$HERE/build/awdl-root.dex" ] || "$HERE/build.sh"
adb shell "mkdir -p $D && chmod 700 $D"
adb push -q "$HERE/build/awdl-root.dex" "$HERE/namedrop-pixel.sh" "$KEYS/snapkey-cert.pem" "$KEYS/snapkey-key.pem" "$D/"
adb push -q "$VCARD" "$D/contact.vcf"
adb shell "chmod 600 $D/snapkey-key.pem"

# Push a script and run it with su -c 'sh file': a command line passed to su is re-parsed by the
# device's sh first, which silently breaks $(...) and loops.
adb shell su -c "'nohup setsid sh $D/namedrop-pixel.sh $UUID $HOLD $COUNTRY > $D/run.log 2>&1 &'"
for i in $(seq 1 20); do
  sleep 1
  out=$(adb shell su -c "'cat $D/run.log'" 2>/dev/null || true)
  case "$out" in *up:*|*"did not come up"*) break ;; esac
done
echo "$out"
echo "listener $UUID. Open NameDrop Card on the phone and bump. Logs: adb shell su -c 'cat $D/receiver.log'"
