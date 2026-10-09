# Hardware

Three things, plus an iPhone. The radio stack runs on a **Linux PC or Raspberry Pi** —
macOS can't do this, and stock Android has no API for the AWDL/QUIC leg.

| Part | Cost | Role |
|---|---|---|
| **Atheros AR9271 USB Wi-Fi** | ~$20 | AWDL. The one part with no substitute. |
| **Proxmark3** *or* an **Android phone** | ~$50–90 / — | The card. Answers the NFC bump. |
| **Linux PC / Raspberry Pi** | — | Runs OWL and the two receiver processes. |
| **iPhone, iOS 17+** | — | The peer. |

## Wi-Fi — monitor mode *and* frame injection

OWL will not run without both. This is the part people get wrong.

| Option | Notes |
|---|---|
| **Atheros AR9271 USB adapter** (`ath9k_htc`) | The recommended path. Rock-solid monitor mode + injection on any Linux PC. e.g. Alfa AWUS036NHA, TP-Link TL-WN722N **v1 only** — v2/v3 are a different chipset and will not work. |
| **Raspberry Pi 3/4 onboard Wi-Fi + [Nexmon](https://github.com/seemoo-lab/nexmon)** | No extra adapter, but you must flash patched firmware. |
| **MediaTek MT7612U USB adapter** (`mt76x2u`) | The faster alternative. Dual-band 802.11ac, in-kernel driver, monitor mode + injection. e.g. Alfa AWUS036ACM. Not yet verified end-to-end with this repo. |
| Other MediaTek mt76 USB adapters | Often work; less battle-tested for AWDL specifically. |

> **Built-in Wi-Fi will not save you — Intel included.** Intel cards (AX200/201/210) have
> mature Linux drivers, but frame injection is essentially unsupported by `iwlwifi`, and OWL
> *requires* injection. A Linux'd Intel laptop still needs the AR9271.

**2.4 GHz-only is fine.** AWDL uses channels 6, 44 and 149, and the AR9271 is 2.4 GHz only —
but modern iPhones keep **channel 6 in their AWDL social rotation**, so it shares an
availability window every cycle. A monitor-mode capture pinned to channel 6 with the share
sheet open (iPhone 15 and 16) heard thousands of AWDL frames, and all but one of the ~4,600
that carried a channel map listed channel 6 alongside 44 and 149.

**Known limitation.** `ath9k_htc` has no *active* monitor mode, so we never ACK at L2 and the
peer sees no retransmissions from us. For NameDrop's small payloads this is survivable, but it
is the main source of intermittency: some bump windows simply never route to us. It is also
why the mDNS advertiser repeats itself rather than answering a browse once. The card also
warms up under load — a USB extension cable to keep it off the chassis measurably helps.

**It is also slow.** The AR9271 is a 1x1 802.11n, USB 2.0 part, and on top of that we only
meet the iPhone on channel 6 for part of each AWDL cycle, and never ACK, so the iPhone burns
airtime retransmitting. Contact cards are small enough that this rarely matters, but larger
transfers crawl and a bump can take several seconds to connect. If you want more headroom, try
a **MediaTek MT7612U** adapter such as the Alfa AWUS036ACM: it is dual-band, so it can also
follow AWDL onto channels 44 and 149 (`awdl-up.sh -c 44`), and `mt76` supports active monitor
mode, so it can ACK. It hasn't been verified end-to-end here yet, and `awdl-up.sh` only
auto-detects `ath9k_htc`, so pass the interface with `-i` (`check-hardware.sh` will also flag it).

`scripts/check-hardware.sh` verifies the adapter is attached and bound to `ath9k_htc`.

## NFC — the Proxmark3 is the card

A bump is an **ISO-DEP card transaction**, not a broadcast. After the ECP frames the phone
halts the peer, wakes it, anticollision-selects it and sends `SELECT-AID`. So the device
answering the bump must be *selectable as a card*.

This is why a **PN532 does not work** for the bump: it is a reader, and a reader can never be
selected. Emitting the ECP frame from one makes the iPhone play the NameDrop warp and then
hang at "Keep Holding Nearby to Share" — it is waiting for a transaction the PN532 is
structurally incapable of joining. Cheap PN532 modules are useful for bench-testing a card,
but they are not on this path.

Any Proxmark3 that runs the [Iceman fork](https://github.com/RfidResearchGroup/proxmark3)
works. See [../firmware/README.md](../firmware/README.md) to build and flash the standalone
mode.

## NFC — or an Android phone is the card

An unrooted Android phone can replace the Proxmark3. Host card emulation makes it a
selectable ISO-DEP card, and the reader-mode polling-loop annotation puts the ECP frames on
the air. See [../android/namedrop-card/README.md](../android/namedrop-card/README.md).

| Requirement | Why |
|---|---|
| Android 15+ (API 35), NFC HCE | The card half. |
| NFC controller that honours `READER_TECH_A_POLLING_LOOP_ANNOTATION` | Without ECP emission, iOS reads the phone as a plain tag. **Tested: Pixel 9** (ST54L, Android 16 and 17). Other phones are untested. |
| No root | The app toggles between emitting and being a card. Only doing both at once needs privilege. |

The phone does the NFC half only. It still needs the AR9271 + Linux machine for AWDL and QUIC.

Pixel antenna placement differs from an iPhone's: the coil is **mid-body** on the back.

> Avoid the **ACR122U**. It is PN532-based, but its CCID firmware wraps the chip and blocks
> the raw framing this needs.

## Quick self-check

```bash
./scripts/check-hardware.sh
```

PASS/FAIL per item, before you sink time into OWL.
