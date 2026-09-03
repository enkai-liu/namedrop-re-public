#!/usr/bin/env bash
# Bring up AWDL on the AR9271 via OWL, so awdl0 gets an IPv6 link-local we can
# ping the iPhone over. Milestone B (DONE). Run with sudo. OWL runs in the
# FOREGROUND — Ctrl+C to stop (cleanup restores the managed interface).
#
#   sudo ./scripts/awdl-up.sh              # auto-detect AR9271, channel 6
#   sudo ./scripts/awdl-up.sh -c 44        # force a 5 GHz social channel
#   sudo ./scripts/awdl-up.sh -i wlxXXXX   # force a specific base interface
#
# WHY THIS IS FIDDLY: the AR9271 (ath9k_htc) supports only *passive* monitor
# mode, and NetworkManager/wpa_supplicant keep a P2P-device vif on the phy that
# makes an in-place "set type monitor" fail with EBUSY (Object busy). So instead
# of letting OWL flip the card, we tear down every vif on the phy (killing wpa's
# grip) and add ONE clean monitor vif, then run OWL with -N (skip its own monitor
# setup). Passive monitor => the peer re-transmits un-ACKed frames up to 7x;
# irrelevant for NameDrop's tiny vCard payloads. (-N does nothing but skip
# set_monitor_mode — verified in daemon/owl.c; the old "breaks ath9k_htc" note
# was wrong. netutils.c is also patched to fall back active->passive.)
#
# AR9271 is 2.4 GHz only -> default channel 6 (triple-confirmed still in the
# iPhone's AWDL social rotation with the share sheet open; docs/awdl-channel6-verified.md).
# Once awdl0 is up, in ANOTHER terminal:
#   ip -6 addr show awdl0                  # confirm fe80::/link-local present
#   ./.venv/bin/opendrop -i awdl0 find     # browse for the iPhone (share sheet open)
#   ping6 -I awdl0 <iphone-link-local>     # Milestone B success = replies
set -euo pipefail

OWL="$(cd "$(dirname "$0")/.." && pwd)/third_party/owl/build/daemon/owl"
MON="awdlmon0"     # fresh monitor vif we create for OWL
CHANNEL=6
IFACE=""

while getopts "c:i:h" opt; do
  case "$opt" in
    c) CHANNEL="$OPTARG" ;;
    i) IFACE="$OPTARG" ;;
    h) sed -n '2,27p' "$0"; exit 0 ;;
    *) exit 2 ;;
  esac
done

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo (need root for monitor mode)." >&2; exit 1; }
[ -x "$OWL" ] || { echo "OWL not built at $OWL (run scripts/setup-linux.sh)" >&2; exit 1; }

# Auto-detect the ath9k_htc interface (don't hardcode the MAC-derived name).
if [ -z "$IFACE" ]; then
  for d in /sys/class/net/wl*; do
    [ -e "$d" ] || continue
    if [ "$(basename "$(readlink -f "$d/device/driver" 2>/dev/null)")" = "ath9k_htc" ]; then
      IFACE="$(basename "$d")"; break
    fi
  done
fi
[ -n "$IFACE" ] || { echo "No ath9k_htc (AR9271) interface found — is it plugged in?" >&2; exit 1; }

PHY="$(cat "/sys/class/net/$IFACE/phy80211/name")"   # e.g. phy1
echo "== AR9271: base iface $IFACE on $PHY, monitor vif $MON, channel $CHANNEL =="

cleanup() {
  [ "${CLEANED:-0}" -eq 1 ] && return
  CLEANED=1
  trap - EXIT INT TERM HUP
  if [ -n "${OWL_PID:-}" ]; then
    kill -TERM "$OWL_PID" 2>/dev/null || true
    wait "$OWL_PID" 2>/dev/null || true
    OWL_PID=""
  fi
  echo; echo "== Cleanup: removing $MON, restoring $IFACE =="
  ip link set "$MON" down 2>/dev/null || true
  iw dev "$MON" del 2>/dev/null || true
  # Recreate a managed vif and hand control back to NetworkManager.
  iw dev "$IFACE" info >/dev/null 2>&1 || iw phy "$PHY" interface add "$IFACE" type managed 2>/dev/null || true
  command -v nmcli >/dev/null 2>&1 && nmcli device set "$IFACE" managed yes >/dev/null 2>&1 || true
}
CLEANED=0
OWL_PID=""
trap cleanup EXIT
trap 'exit 130' INT TERM HUP

# 1. Take the card away from NetworkManager/wpa_supplicant.
command -v nmcli >/dev/null 2>&1 && nmcli device set "$IFACE" managed no >/dev/null 2>&1 || true
rfkill unblock wifi 2>/dev/null || true

# 2. Delete EVERY vif on this phy (base iface + wpa's p2p-dev) so nothing holds it.
#    p2p-device vifs are the usual cause of the EBUSY on set-type-monitor.
mapfile -t VIFS < <(iw dev | awk -v want="$PHY" '
  /^phy#/ { cur=$0; sub("phy#","phy",cur) }
  /[ \t]Interface / { if (cur==want) print $2 }')
for v in "${VIFS[@]}"; do
  echo "   removing vif $v"
  ip link set "$v" down 2>/dev/null || true
  iw dev "$v" del 2>/dev/null || true
done
sleep 1   # let wpa_supplicant settle so it doesn't recreate a vif mid-add

# 3. Add ONE clean monitor vif on the now-idle phy (this succeeds where an
#    in-place iftype change EBUSYs) and bring it up.
iw phy "$PHY" interface add "$MON" type monitor
ip link set "$MON" up
if ! iw dev "$MON" info | grep -q 'type monitor'; then
  echo "ERROR: $MON did not come up in monitor mode" >&2; iw dev "$MON" info >&2; exit 1
fi
echo "   $MON is up in monitor mode"

# 4. Run OWL with -N (we already set monitor mode; don't let it try again).
echo "== Starting OWL on $MON (foreground; Ctrl+C to stop) =="
echo "   awdl0 should appear in a second — check with: ip -6 addr show awdl0"
"$OWL" -N -i "$MON" -c "$CHANNEL" -v &
OWL_PID=$!
wait "$OWL_PID"
