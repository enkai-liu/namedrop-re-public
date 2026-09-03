# AWDL channel 6 in rotation — VERIFIED (2026-07-04)

**Result: GO for a 2.4 GHz-only AWDL card (AR9271).** Both a modern iPhone 16 and an
iPhone 15 keep **channel 6 (2437 MHz)** as a permanent member of their AWDL social channel
rotation while an AirDrop share sheet is open. This retires the Phase-1 hardware risk "what if
current iPhones dropped 2.4 GHz from AWDL, stranding a 2.4-only injector."

## Why this mattered

The AR9271 is **2.4 GHz-only** — it can inject/receive on channel 6 but never on the 5 GHz
AWDL social channels (44 / 149). If a current iPhone only ever dwelled on 5 GHz, a 2.4-only
card could never share an availability window with it and OWL could never link. So before
buying, we had to prove ch6 is really in the rotation of the phones we care about.

## Captures

Two monitor-mode (radiotap) captures, card **locked to channel 6** the entire time
(never hopped), taken with the AirDrop share sheet open:

- `recording1.pcap` — iPhone 16, share sheet sharing an image.
- `recording2.pcap` — iPhone 16 + iPhone 15, both share sheets open sharing an image.

(Both are ad-hoc test captures, not committed; regenerate with a card in monitor mode pinned
to ch6.)

## Evidence — three independent confirmations

Card was pinned to 2437 MHz: recording1 = 6446/6446 frames @ 2437 MHz; recording2 =
13551/13551 @ 2437 MHz. So everything below was *physically received on channel 6*.

| Evidence | recording1 (iPhone 16) | recording2 (iPhone 16 + 15) |
|---|---|---|
| AWDL action frames heard on ch6 | 1,262 | 3,476 |
| `awdl.datastate.social_channel_map.ch6 == True` | 1,227 / 1,227 datastate frames | 3,386 / 3,387 |
| `awdl.channelseq.channel.number` == 6 entries | 3,240 | 7,530 |

1. **Physically received thousands of AWDL frames while parked on ch6** — the phones were
   transmitting AWDL there, live.
2. **`social_channel_map.ch6` flag set on ~100% of datastate frames** — the phone itself
   declares "ch6 is one of my social channels." The full map is `ch6 | ch44 | ch149`, all
   `True`.
3. **The advertised channel-sequence TLV contains channel 6** thousands of times, alongside
   the 5 GHz channels (132/134, 149/151, 157/159 clusters). Stray single-count channel numbers
   are malformed-frame misparses — ignore them.

## How to reproduce (Wireshark / tshark)

tshark lives at `/Applications/Wireshark.app/Contents/MacOS/tshark` on the Mac dev box.

```sh
TS=/Applications/Wireshark.app/Contents/MacOS/tshark
# card really was single-channel:
$TS -r recording1.pcap 2>/dev/null | grep -oE '[0-9]{4} MHz' | sort | uniq -c
# the phone's own ch6 declaration (the definitive check):
$TS -r recording1.pcap -Y 'awdl.datastate.social_channel_map.ch6==1' \
    -T fields -e awdl.datastate.social_channel_map.ch6 2>/dev/null | sort | uniq -c
# channel 6 present in the advertised sequence:
$TS -r recording1.pcap -Y 'awdl.channelseq.channel.number' \
    -T fields -e awdl.channelseq.channel.number 2>/dev/null \
    | tr ', ' '\n' | grep -E '^[0-9]+$' | sort -n | uniq -c
```

GUI display filters: `awdl` (all AWDL), `awdl.datastate.social_channel_map.ch6 == 1`
(== 0 returns nothing), `radiotap.channel.freq == 2437`. To read the raw sequence: pick an
`awdl` frame → *Apple Wireless Direct Link → Channel Sequence → Channel List*.

## Honest caveat

Because the card was locked to ch6, this capture cannot measure *dwell time* (how long each
phone sits on ch6 vs. 5 GHz per cycle). Measuring that needs a second monitor card on ch149 to
compare. Not required for the buy decision — we only needed to prove ch6 is in the set, and it
is. If OWL linking later proves flaky, low ch6 dwell would be the thing to measure next.
