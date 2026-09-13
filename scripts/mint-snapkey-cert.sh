#!/usr/bin/env bash
# Mint the _asquic TLS certificate FROM scratchpad/snap-identity.json.
#
# The TLS gate is the NFC key binding, not Apple trust: iOS accepts our cert iff its public
# key is the P-256 key the card hands over as SNAP ServerInfo key 1. So the cert must be
# minted from the identity's `p256_pkcs8_der`, never from a fresh openssl key. A drifted
# cert is invisible on the wire -- it looks exactly like iOS refusing us.
#
# Re-run this every time build-snap-serverinfo.py --regenerate runs.
#
# Usage:
#   ./scripts/mint-snapkey-cert.sh
#   ID=path/to/snap-identity.json OUT=path/to/dir ./scripts/mint-snapkey-cert.sh
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
ID="${ID:-$REPO/scratchpad/snap-identity.json}"
OUT="${OUT:-$REPO/scratchpad/asquic-keys}"

[ -f "$ID" ] || { echo "FATAL: no identity at $ID -- run scripts/build-snap-serverinfo.py first" >&2; exit 1; }
mkdir -p "$OUT"

UUID=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['bonjour_listener_uuid'])" "$ID")
(umask 077; python3 - "$ID" "$OUT/snapkey-key.pem" <<'PY'
import base64, json, sys
der = bytes.fromhex(json.load(open(sys.argv[1]))["p256_pkcs8_der"])
b64 = base64.encodebytes(der).decode().strip()
open(sys.argv[2], "w").write("-----BEGIN PRIVATE KEY-----\n%s\n-----END PRIVATE KEY-----\n" % b64)
PY
)
chmod 600 "$OUT/snapkey-key.pem"

cat > "$OUT/ext.cnf" <<CNF
[req]
distinguished_name=dn
[dn]
[v3]
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth,clientAuth
subjectAltName=DNS:${UUID}.local
CNF

# Empty subject and issuer, as a real iPhone's _asquic leaf carries: the NFC exchange already
# said which key to expect. The SAN is belt-and-braces; no SNI is sent on this path.
openssl req -new -x509 -key "$OUT/snapkey-key.pem" -out "$OUT/snapkey-cert.pem" \
    -days 3650 -subj "/" -config "$OUT/ext.cnf" -extensions v3 2>/dev/null
rm -f "$OUT/ext.cnf"

# Refuse to leave a drifted pair behind.
python3 - "$ID" "$OUT/snapkey-cert.pem" <<'PY'
import base64, json, subprocess, sys
want = bytes.fromhex(json.load(open(sys.argv[1]))["p256_spki_der"])
pem = subprocess.run(["openssl", "x509", "-in", sys.argv[2], "-noout", "-pubkey"],
                     capture_output=True, text=True, check=True).stdout
got = base64.b64decode("".join(l for l in pem.splitlines() if "KEY" not in l))
if got != want:
    sys.exit("FATAL: cert public key != SNAP key 1 -- iOS would reject this cert")
print("  cert public key == SNAP key 1  OK")
PY
echo "  minted $OUT/snapkey-cert.pem + snapkey-key.pem for listenerUUID $UUID"
