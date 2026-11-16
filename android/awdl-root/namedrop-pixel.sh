#!/system/bin/sh
# Runs ON the rooted Pixel, as root. pixel-up.sh pushes it and starts it; you shouldn't need to
# run it by hand. Usage: sh namedrop-pixel.sh <listenerUUID> [holdSecs] [country]
#
# 1. Raise mosey0, the Pixel's AWDL link, through Google's IMoseyService (MoseyStart).
# 2. Add a routing rule for it. A root-raised mosey0 has no ConnectivityService network behind
#    it, so nothing points at its routing table and every reply fails with ENETUNREACH. The
#    iPhone then retransmits its QUIC Initial until it gives up with "Error Sharing".
# 3. Start the QUIC/HTTP-3 receiver from the namedrop-card APK (NameDropReceiver).
# 4. Announce <listenerUUID>._asquic._udp on mosey0 (MdnsAnnounce).
#
# Stop: touch $D/STOP (pixel-up.sh --stop does this). Everything is torn down and the rule removed.
D=/data/local/tmp/namedrop
UUID="$1"; HOLD="${2:-600}"; COUNTRY="${3:-US}"; PORT=60192
[ -n "$UUID" ] || { echo "usage: $0 <listenerUUID> [holdSecs] [country]"; exit 1; }
rm -f $D/STOP $D/*.log $D/*.pid; mkdir -p $D/received

# The StartMoseyParam config Google's app sends, replayed verbatim. mosey_daemon decodes it as
# radio tuning (is_dbs_supported, sta_channel_freq=5200, ...), nothing per-peer. Channel 149.
CFG=080110d028300142060000000000004802500a580a600a6802700480010a9001804098018040a00110a80120b00120b80140c00180808008c80101
CLASSPATH=$D/awdl-root.dex app_process / MoseyStart start 149 $CFG $HOLD $COUNTRY > $D/mosey.log 2>&1 &
echo $! > $D/mosey.pid
for i in $(seq 1 20); do [ -e /sys/class/net/mosey0 ] && break; sleep 0.5; done
[ -e /sys/class/net/mosey0 ] || { echo "mosey0 did not come up; see $D/mosey.log"; kill $(cat $D/mosey.pid); exit 1; }

sleep 1
T=$(ip -6 route show table all | grep "fe80::/64 dev mosey0 table" | awk '{print $5}')
ip -6 rule del pref 9999 2>/dev/null
ip -6 rule add pref 9999 oif mosey0 lookup $T
echo "$(date +%s) rule: oif mosey0 -> table $T" >> $D/state.log

APK=$(pm path com.namedrop.card | cut -d: -f2)
CLASSPATH=$APK app_process / com.namedrop.card.h3.NameDropReceiver $PORT \
  $D/snapkey-cert.pem $D/snapkey-key.pem $UUID $D/received $D/contact.vcf > $D/receiver.log 2>&1 &
echo $! > $D/receiver.pid

sleep 1
CLASSPATH=$D/awdl-root.dex app_process / MdnsAnnounce $UUID mosey0 $PORT 2 $HOLD > $D/announce.log 2>&1 &
echo $! > $D/announce.pid
echo "up: mosey0 $(ip -6 addr show mosey0 | grep -o 'fe80[^ ]*'), listener $UUID:$PORT, for $HOLD s"

# Watch until STOP or the hold runs out, then tear everything down.
END=$(( $(date +%s) + HOLD ))
while [ ! -e $D/STOP ] && [ $(date +%s) -lt $END ]; do
  echo "$(date +%s) $(cat /sys/class/net/mosey0/operstate 2>/dev/null || echo absent)" >> $D/state.log
  sleep 2
done
kill $(cat $D/receiver.pid) $(cat $D/announce.pid) $(cat $D/mosey.pid) 2>/dev/null
ip -6 rule del pref 9999 2>/dev/null
echo "$(date +%s) stopped" >> $D/state.log
