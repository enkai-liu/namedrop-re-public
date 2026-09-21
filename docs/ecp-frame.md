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

`firmware/hf_namedrop.c` and the Android `EcpEmitter` build this frame. The MAC was meant to match the
address the phone was expected to hunt for next. (The BLE leg turned out to be a dead end --
a real bump emits no forgeable AirDrop identity beacon at all -- so nothing in this repo
advertises it; the field is documented here because the frame carries it.)

> **CRC ownership:** the Proxmark3 firmware appends CRC_A itself (`AddCrc14A`); on Android the
> NFC controller appends it, so `EcpEmitter` passes the 14-byte frame without it.
>
> ⚠️ **Still to verify on hardware:** the exact `config`/`subtype` semantics against a capture
> of two real iPhones. Treat those bytes as the documented starting point, not gospel.

> ✅ **HARDWARE-PROVEN (2026-07-08):** emitting this exact frame from a PN532 (MAC
> `de:ad:be:ef:69:69`) made a live iPhone fire the **NameDrop warp/glow animation**. The frame here
> is correct as-is — see "The NameDrop handshake" below.

## NameDrop frame vs AirDrop frame — don't confuse them

There are **two distinct ECP frames**.
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
