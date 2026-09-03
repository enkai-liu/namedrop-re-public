#!/usr/bin/env bash
# Verify the Linux/Pi box has the radios the live demo actually uses. This deliberately
# checks the AR9271/ath9k_htc path that awdl-up.sh configures, plus the toolchain and the
# two Python deps the receivers need.
#
# Usage:
#   ./scripts/check-hardware.sh
#
# The card in this rig is a Proxmark3, which is checked with the pm3 client, not from here.
set -uo pipefail

pass() { printf '  \033[32mPASS\033[0m  %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; FAILED=1; }
warn() { printf '  \033[33mWARN\033[0m  %s\n' "$1"; }
have() { command -v "$1" >/dev/null 2>&1; }

FAILED=0
case "${1:-}" in
  "") ;;
  -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
  *) echo "usage: $0" >&2; exit 2 ;;
esac

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$REPO/.venv/bin/python"

echo "== NameDrop hardware check =="

echo "[1/2] Wi-Fi monitor mode + injection (required by OWL)"
AWDL_IFACE=""
for d in /sys/class/net/wl*; do
  [ -e "$d" ] || continue
  driver="$(basename "$(readlink -f "$d/device/driver" 2>/dev/null)" 2>/dev/null)"
  if [ "$driver" = "ath9k_htc" ]; then
    AWDL_IFACE="$(basename "$d")"
    break
  fi
done
if [ -n "$AWDL_IFACE" ]; then
  pass "AR9271/ath9k_htc is attached as $AWDL_IFACE"
elif have iw && iw list 2>/dev/null | grep -qi 'monitor'; then
  fail "monitor mode exists, but no AR9271/ath9k_htc interface is attached (awdl-up.sh needs it)"
else
  fail "no supported AWDL Wi-Fi adapter found (plug in the AR9271; see docs/hardware.md)"
fi
if have aireplay-ng; then
  warn "injection is not exercised automatically; the received AR9271 was previously verified"
else
  warn "aircrack-ng not installed; can't auto-test injection (apt install aircrack-ng)"
fi

echo "[2/2] Toolchain + Python deps (to build OWL and run the receivers)"
for t in gcc cmake python3 ip iw; do
  if have "$t"; then pass "$t present"; else fail "$t missing"; fi
done
[ -x "$REPO/third_party/owl/build/daemon/owl" ] \
  && pass "OWL build present" \
  || fail "OWL build missing (run scripts/setup-linux.sh)"
[ -x "$PY" ] \
  && "$PY" -c 'import opendrop' >/dev/null 2>&1 \
  && pass "OpenDrop present (mdns-advertise.py needs it)" \
  || fail "OpenDrop is unavailable in .venv (run scripts/setup-linux.sh)"
[ -x "$PY" ] \
  && "$PY" -c 'import aioquic' >/dev/null 2>&1 \
  && pass "aioquic present (asquic-receiver.py needs it)" \
  || fail "aioquic is unavailable in .venv (run scripts/setup-linux.sh)"

echo
if [ "$FAILED" -eq 0 ]; then
  echo "READY: all hardware required for this mode is attached and usable."
else
  echo "NOT READY: fix the FAIL item(s) above. See docs/hardware.md."
fi
exit "$FAILED"
