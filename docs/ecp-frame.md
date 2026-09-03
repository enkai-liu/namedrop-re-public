# The NameDrop NFC trigger: Enhanced Contactless Polling (ECP)

Source: [kormax/apple-enhanced-contactless-polling](https://github.com/kormax/apple-enhanced-contactless-polling).
This is the one piece that is NameDrop-specific (everything below it is plain AirDrop).

> **How this repo uses it.** The Proxmark3 standalone mode builds and emits these frames
> itself — there is no PN532 on the working path. The PN532 history below is kept because it
> is how the frame was verified byte-for-byte against a real iPhone's own.

## What ECP is

ECP is an Apple-proprietary extension to the ISO/IEC 14443-A polling sequence. A reader
broadcasts a custom frame *during polling*, before any tag is selected, telling nearby
Apple devices what kind of field this is (Apple Pay, transit, access key, AirDrop,
NameDrop…). The device waits one polling cycle, decides how to respond, then engages on
the next cycle.

For NameDrop, the reader advertises "this is an AirDrop/NameDrop field, and
the BLE MAC to continue on is X". The iPhone reacts by showing the NameDrop UI and
starting AirDrop discovery for that MAC.

## The NameDrop frame (ECP v2)

```
 6a   02   89   05   00   01 00 01   <6-byte BLE MAC>   <CRC-A>
 │    │    │    │    │    │          │                  │
 │    │    │    │    │    │          │                  └ ISO 14443-A CRC (CRC_A)
 │    │    │    │    │    │          └ BLE MAC the iPhone should look for next
 │    │    │    │    │    └ TCI (Terminal Capabilities Identifier) = 01 00 01
 │    │    │    │    └ Subtype = 00
 │    │    │    └ Type = 05  (AirDrop / NameDrop)
 │    │    └ Config = 89
 │    └ Version = 02  (ECP v2)
 └ Header = 6a  (constant ECP marker)
```

Example (BLE MAC `de:ad:be:ef:69:69`):

```
6a 02 89 05 00 01 00 01 de ad be ef 69 69  <CRC-A>
```

`src/namedrop/nfc_ecp.py` builds this frame and appends CRC_A. The MAC must match the
address the phone was expected to hunt for next. (The BLE leg turned out to be a dead end --
a real bump emits no forgeable AirDrop identity beacon at all -- so nothing in this repo
advertises it; the field is documented here because the frame carries it.)

> ✅ **CRC ownership RESOLVED (2026-07-06):** we append CRC_A **ourselves** (`append_crc=True`,
> the default). Confirmed against kormax's proven nfcpy example
> (`examples/implementations/nfcpy`, verified on a real iPhone 14 Pro Max / iOS 17): it appends
> `crc16a` in software before `InCommunicateThru`, i.e. the PN532's raw-TX path in this
> configuration does **not** add CRC_A. Their `crc16a` is byte-for-byte our `crc_a`.
>
> ⚠️ **Still to verify on hardware:** the exact `config`/`subtype` semantics against a capture
> of two real iPhones. Treat those bytes as the documented starting point, not gospel.

> ✅ **HARDWARE-PROVEN (2026-07-08):** emitting this exact frame from a PN532 (then `namedrop ecp-emit
> de:ad:be:ef:69:69`) made a live iPhone fire the **NameDrop warp/glow animation**. The frame here
> is correct as-is — see "Hardware bring-up" and "The NameDrop handshake" below.

## NameDrop frame vs AirDrop frame — don't confuse them

There are **two distinct ECP frames**, and `nfc_ecp.build_namedrop_ecp` already emits the right one.
Confirmed against kormax's docs (2026-07-08):

| Frame | Config | TCI | Data | Role |
|---|---|---|---|---|
| **NameDrop** | `89` | `01 00 01` | 6-byte **BLE MAC** | the poller emits this; **triggers the warp animation** on the other device |
| **AirDrop**  | `89` | `01 00 00` | six `00` bytes     | a **response** frame a device sends *after* it has *seen* a NameDrop frame |
| *Ignore*     | ECP1 | `cf 00 00` | —                  | collision-avoidance: tells other Apple devices "don't pop a payment card at my field" |

- **`89` / `01 00 01` / MAC is the NameDrop trigger.** That is exactly what we send. The warp glow
  we observed IS NameDrop initiating — it is **not** a "generic AirDrop" fallback.
- **Both frames are config `89` — the config byte is NOT the discriminator** (corrected 2026-07-21;
  this table said `85` for AirDrop, which was wrong and never sourced). kormax's table has no config
  column at all: config is *derived* as `[flags nibble][payload-length nibble]`, and the length counts
  **TCI (3) + data (6) = 9** for both frames. `0x85` would claim a 5-byte payload, which cannot hold
  them. The real discriminators are **TCI** (`01 00 01` vs `01 00 00`) and **data** (real MAC vs zeros).
- **Type `05` *is* AirDrop; NameDrop is a special case of it.** Per kormax's use-case text: "AirDrop …
  used to negotiate an AirDrop session. NameDrop is a special case of AirDrop and it triggers a warp
  animation." So the frame we emit already *is* the AirDrop-session negotiation — there is no separate
  "NFC-initiated AirDrop" frame to look for. Whether a tap becomes a contact exchange or a content
  share is **phone-side state** (idle vs. content open), not a different ECP frame.
  - **Don't confuse the frame with the deliverable.** "NFC-initiated AirDrop" as the *goal* is defined
    by the project notes' **Acceptance criterion** (the bump must escalate the phone to *our* peer; an Accept
    tap is fine, human peer-selection is not), NOT by finding a special frame here. We already emit the
    right frame; what's unproven is whether the phone escalates to us after it — a layer above ECP.
- **Tuning the config byte is a dead end.** We swept `config=0x19` on hardware (via
  an emitter with config byte `0x19`) → **identical** reaction to `0x89`, as expected,
  because `0x89` was already the correct NameDrop config. Do not chase AirDrop→NameDrop via ECP
  bytes; the ECP layer is already right.
- **Why the phone still presented "AirDrop", not the contact-card NameDrop sheet:** we ran
  **NFC-reaction-only** with a dummy MAC (`deadbeef6969`) that nothing answered. The warp is only
  the *trigger*; it becomes the contact-card exchange only once a **real BLE peer answers that MAC**
  and the **AWDL→AirDrop** link completes. The remaining work is that peer/transport (step 5), NOT
  the NFC frame.

## The NameDrop handshake — the ECP role model (why "Apple devices are responders" isn't a paradox)

kormax's line "Apple devices act as responders, not initiators — they don't emit ECP frames" is
about an iPhone's **default resting NFC state**, not a permanent constraint. Reconciling it with
two-iPhone NameDrop:

- **Idle NFC is a poll/listen *loop*, not static listen.** Per the NFC Forum spec, an iPhone
  time-slices: mostly listen/card-emulation (so it can be a payment/transit/key card when a
  *terminal* polls it — this is the "responder" role), but it **periodically dips into a brief
  reader-poll burst** (this is how it reads a tag/poster with no app open). **ECP is emitted during
  that poll burst.** So a phone already becomes a transient reader on its own, a few times a second.
- **Symmetry breaks by overlapping windows.** Two phones run this loop **asynchronously**; the loops
  drift until phone A's reader-poll burst fires while phone B is in its listen window. At that
  instant A's field energizes B, B sees A's ECP → B reacts (warp). **Whoever is polling at the
  overlap is "the reader"** for that exchange. The **ignore frame** (`cf 00 00`) keeps a poller's
  burst from mis-triggering *other* nearby Apple devices.
- **A device that *sees* a NameDrop frame does emit one back** — the **AirDrop response frame**
  (`89` / `01 00 00`). So "Apple devices never emit ECP" is an over-broad summary; in P2P they
  briefly take the reader role to acknowledge.
- **What makes it *NameDrop* (not a random tap)** is gated on **non-NFC** conditions: both devices
  **unlocked + awake**, held **top-to-top within ~1 cm**. Confirming fact: **NameDrop runs on
  iPhones with no U1/UWB chip** (XR/XS-era), so the trigger is **NFC field-detection + the
  accelerometer/proximity gesture, not ultra-wideband.**
- **Honest gap (kormax's too):** the exact Apple-private signal that *elevates* a detected NFC
  contact into the NameDrop sheet, and how the two phones *arbitrate who reads first*, is not public.

**Why this makes our job easier:** we don't play the loop — **we are a *continuous* reader**,
emitting the NameDrop frame nonstop, so we skip the "wait for polling windows to overlap / decide to
become the reader" dance entirely. The iPhone only has to be in its (frequent, default) listen
window to catch our persistent field → reliable single-tap warp. We've removed the timing luck two
real phones depend on.

## Receiving/observing ECP — the PN532 CANNOT (but reason 1 below was WRONG)

A natural idea is "let the iPhone be the initiator and we be the receiver at the NFC layer." This was
written off in 2026-07-08 for two reasons. **Only the second one survived contact with a capture:**

1. ⚠️ **FALSIFIED 2026-07-23 by the Pixel Observe Mode capture — do not rely on this.** The claim was:
   *"An iPhone emits ECP only when running a reader API (`NFCVASReaderSession` /
   `PaymentCardReaderSession`); there is no mode where it hands us a NameDrop frame to consume."*
   **An unlocked iPhone emits `ECP2_HANDOVER` continuously**, with a
   header/config/TCI of `6a02 89 0500010001` — **byte-identical to what we emit** — carrying a
   rotating 6-byte payload (`24201646b15b` → `ca831ea61b8d` → `4948a445dd84`).
   ⚠️ **Rate corrected 2026-08-02: ~1 frame per 1.5–4.7 s, NOT "several times a second"** (which is
   what this line said, unmeasured). Measured off the raw export recovered from the Pixel —
   `evidence take pixel-phase1-2-20260723`. Each frame is followed by a FeliCa `WILDCARD 00ffff0000`
   at a near-invariant **+22.73 ms**; that pairing is the idle-loop baseline. Caveat: the Pixel only
   captures while coupled, so poor coupling could undersample. It hands us a NameDrop
   frame constantly, with no reader API and no share sheet involved. (Arming Share→AirDrop and
   bumping emits a *strict subset* of the idle set, so share intent changes nothing at this layer.)
   **Consequence: the receive-side direction is open, not closed** — if that payload is the phone's
   own BLE address, we have had the bump backwards all along, broadcasting *our* MAC and waiting for
   the phone to come to us while it broadcasts *its* address expecting the peer to come to *it*.
   ⚠️ **Answered 2026-07-31, and the answer kills that reading: the payload is NOT a plaintext BLE
   address.** Under display order the three captured payloads give three *different* address types
   (a rotating phone does not cycle subtypes); reversed, two of three are the **invalid** type. See
   "THE 6-BYTE DATA FIELD IS NOT A BLE MAC" in the project notes. What the six bytes *are* is now the
   open question.
2. **The PN532 can only *transmit* ECP, not observe it — this still holds**, and is why the Pixel
   exists. Its target/emulation mode does not surface
   the reader's pre-selection polling frames. Capturing a real iPhone's ECP needs a **Proxmark3**
   (`hf 14a sniff` → `hf 14a list`) or an **Android 15+ phone with Observe Mode**. The Pixel 9 has
   since done exactly that (`evidence take session-b-20260802`), with one hard limit: Observe Mode
   hears **pollers only**, never a card's response.

**One framing of "we're the receiver" is one layer up:** the ECP tap is only the trigger; right after
the warp the **iPhone becomes the initiator of BLE→AWDL→AirDrop and reaches out to the MAC in our
frame.** We just have to *be* at that MAC (BLE peer + AWDL + AirDrop receiver — the side we're
strongest on). See the project notes step 5.

⚠️ **But this has now failed 6/6 on hardware** (warp fires, iOS parks on "Keep Holding Nearby to
Share", zero LE connections to our MAC — the project notes). Given point 1 above, the *other* framing is live
again and untested: **read the address out of the iPhone's own ECP frame and have the laptop go to
it.** ⚠️ **That reframe is dead as stated** — the 6-byte payload is not an address (above), so there
is nothing in the field to read and escalate to. The framing that survived contact with real data is
neither: a real bump is an **ISO-DEP card transaction**, and we were never selectable
(`evidence take session-b-20260802`).

## Emitting it

ECP requires *low-level* control of the NFC frontend (raw frame TX during polling), which
the high-level Android NFC API does not expose. Confirmed-capable frontends: **PN532**,
PN5180, ST25R3916(B), MFRC522, plus PC/SC readers. We target the **PN532** (cheap, UART/
I²C/SPI, well documented) driven from the Pi/Linux box.

The PN532 is driven via **nfcpy** (the `[nfc]` extra), replicating kormax's proven flow.
`nfc_ecp.emit_loop()` implements it; run it with **`namedrop ecp-emit <ble-mac>`**. The
per-cycle register sequence (all via nfcpy's PN53x chipset):

```
sense_tta("106A")                        # energize the field with a normal NFC-A poll
rf_configuration(0x05, [0xff,0x01,0x00]) # MaxRetries: off (don't step on the broadcast)
write_register("CIU_BitFraming", 0x00)   # whole-byte TX
in_communicate_thru(frame, timeout=0.1)  # raw TX; a timeout (errno 0x01) is EXPECTED —
                                         # ECP is one-way, the phone reacts over BLE/AWDL
```

nfcpy on Linux may bump the UART baud above 115200, which some cheap PN532 clones can't
handle (timeout right after the version banner prints). Fix per kormax: set
`change_baudrate = False` in the installed `nfc/clf/pn532.py`, or use a better UART adapter.

## Hardware bring-up (proven 2026-07-08 — PN532 V3 + CP2102 on the Linux laptop)

The whole path Just Worked, `emit_loop` unmodified. Reproduce:

1. **Wire + DIP.** PN532 DIP → **HSU (UART)**; cross TX/RX; CP2102 `5V`→`VCC`, `GND`→`GND`,
   `TXD`→`RXD`, `RXD`→`TXD` (see `docs/hardware.md`).
2. **Plug in.** CP2102 enumerates as USB `10c4:ea60` → **`/dev/ttyUSB0`** (in-kernel `cp210x`,
   no setup). nfcpy opens it as **`PN532v1.6`**, firmware `32 01 06 07`.
3. **Fix permissions.** `/dev/ttyUSB0` is `root:dialout` mode 660. The user must be in `dialout`.
   `sudo usermod -aG dialout <user>` (permanent, **needs a re-login**) + `sudo chmod a+rw
   /dev/ttyUSB0` (this session, re-run after each replug until the re-login takes effect). Both
   `sudo` steps must be run by a human at a terminal (a non-interactive agent can't enter the
   password) — suggest the `! <cmd>` prompt prefix.
4. **Verify via nfcpy, NOT `nfc-list`.** `nfc-list` says "No NFC device found" because libnfc's
   autoscan doesn't probe `pn532_uart` by default — irrelevant, our path is nfcpy. Confirm with
   `clf.open('tty:USB0:pn532')`. (If you *want* `nfc-list` to work, set
   `device.connstring="pn532_uart:/dev/ttyUSB0"` in `/etc/nfc/libnfc.conf`.)
5. **Emit.** `.venv/bin/namedrop ecp-emit de:ad:be:ef:69:69` → unlock the iPhone, bring its **top
   edge** onto the PN532 coil, hold ~2 s → warp glow.

**No clone baud issue on this module** — nfcpy's default baud bump worked, so the
`change_baudrate=False` fix above was NOT needed here.

**Wedge/replug gotcha:** killing the emitter with SIGTERM mid-`in_communicate_thru` can leave the
PN532 unresponsive (next `clf.open` → `ETIMEDOUT`; a raw GetFirmwareVersion gets no reply). DTR/RTS
toggling does **not** reset it (these boards don't wire the UART adapter's control lines to the
PN532 reset pin). **Fix = physically unplug/replug the CP2102** (cuts the 5 V, cold-resets the
module); then re-`chmod` `/dev/ttyUSB0`.

**Parametrized emitter for experiments:** a parametrized variant emitter overrides
config/subtype/type/TCI/MAC and can run for N seconds — used to prove the `config` byte is not the
AirDrop↔NameDrop lever.

## Android note (stretch / Milestone D)

Android 15 added "Observe Mode" (watch polling frames before responding) — that's the
*receive* side. Actively *emitting* a custom ECP poll from a stock Android NFC controller
is not exposed, so on Android we'd still drive a PN532 over USB-OTG, or do firmware-level
work. Deferred to the porting phase.
