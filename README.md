# namedrop-re

An implementation of Apple's **NameDrop** protocol — bump a Proxmark3 or an Android phone
with an iPhone, get the iPhone's contact card back over QUIC, and send one in return.

https://github.com/user-attachments/assets/d89eb796-a2ed-4275-a1fc-01187c6cd0de

**Hardware:** **AR9271 USB Wi-Fi adapter**, any Ubuntu 24.04 machine, and one NFC card:

| NFC card | Setup |
|---|---|
| **Proxmark3** (Iceman fork) | [firmware/README.md](firmware/README.md) |
| **Android phone**, (I used a Pixel 9) | [android/namedrop-card/README.md](android/namedrop-card/README.md) |

The phone or Proxmark3 only initiates the NFC bump. The contact exchange itself runs over AWDL
using the USB Wi-Fi adapter. I am currently working on getting NameDrop working fully on Android.

If you want to read about the protocol, skip to **[How NameDrop actually works](#how-namedrop-actually-works)**.

---

## Setup

```bash
git clone https://github.com/enkai-liu/namedrop-re-public.git namedrop-re
cd namedrop-re
./scripts/setup-linux.sh      # apt deps, build OWL, apply patches for OWL and OpenDrop, create .venv
./scripts/check-hardware.sh
```

`setup-linux.sh` is safe to re-run and applies the patches in `patches/`:

| Patch | Fixes |
|---|---|
| `owl-passive-monitor-fallback` | The AR9271 (`ath9k_htc`) supports only *passive* monitor mode while OWL demands active by default and aborts. |
| `opendrop-py312-zeroconf-compat` | Python 3.12 dropped `key_file`/`cert_file` from `HTTPSConnection`; modern zeroconf needs an `update_service` listener. |
| `opendrop-chunked-receive` | Modern senders POST chunked with no `Content-Length`; stock OpenDrop crashed on `int(None)`. |
| `opendrop-discover-capability-fields` | `/Discover` must return the capability fields a real receiver does. |
| `opendrop-util-libarchive5` | libarchive-c 5.x changed `ArchiveEntry.__init__`, breaking OpenDrop's archive helper on import-adjacent paths. |
| `opendrop-vr-identity` | Makes OpenDrop's self-signed vs supplied-identity choice explicit and logged. |

## Running a NameDrop

### 1. Mint one SNAP identity

Do this **first**: the firmware header, the TLS cert, and the mDNS name are all minted
from it. One identity is good for as many sessions and bumps as you like — re-running the
script reuses the existing `scratchpad/snap-identity.json` rather than minting a new one.
`--regenerate` forces a new one — which means reflashing and restarting the advertiser, since
otherwise iOS resolves a UUID nobody is serving, which results in the share page appearing
but getting stuck at Share. That last part still needs verification.

```bash
.venv/bin/python scripts/build-snap-serverinfo.py \
  --header ~/proxmark3/armsrc/Standalone/hf_namedrop_snap.h    # Android only: --header ""
```

Writes the private half to `scratchpad/snap-identity.json` (gitignored, mode 0600).

The same identity is used in several places and must stay in sync:

| Artifact | Used by |
|---|---|
| `firmware/hf_namedrop_snap.h` | the Proxmark3 standalone mode |
| `android/namedrop-card/.../SnapBlobs.java` | the Android card app (gitignored) |
| `scratchpad/snap-identity.json` | the mDNS instance name, and the TLS cert's key |

### 2. Set up the card: Proxmark3 *or* Android

**Proxmark3:** see **[firmware/README.md](firmware/README.md)**. Short version: apply
`patches/proxmark3-standalone-namedrop.patch` to an Iceman checkout, drop in
`firmware/hf_namedrop.c`, set `STANDALONE=HF_NAMEDROP`, `make && ./pm3-flash-all`.

**Android phone:** see **[android/namedrop-card/README.md](android/namedrop-card/README.md)**.
Short version: `./gradlew assembleDebug`, `adb install`, open the app, and keep it in the
foreground. Rebuild it whenever the identity changes.

### 3. Make a certificate carrying SNAP key 1

Self-signed. Subject and issuer do not matter since a real iPhone's own cert leaves both empty.
The only thing that matters is that the public key is the P-256 key in `scratchpad/snap-identity.json`.

```bash
./scripts/mint-snapkey-cert.sh
```

Writes `scratchpad/asquic-keys/snapkey-cert.pem` and `snapkey-key.pem` (key mode 0600), built
from the identity's own private key. It exits non-zero rather than leave a cert whose public
key differs from SNAP key 1. Re-run it after every `--regenerate`.

### 4. Bring up AWDL and both receivers

Three terminals:

```bash
sudo ./scripts/awdl-up.sh                                    # 1. AWDL; foreground, Ctrl-C to stop
.venv/bin/python scripts/mdns-advertise.py -i awdl0           # 2. publishes <uuid>._asquic._udp
.venv/bin/python scripts/asquic-receiver.py \
    --cert scratchpad/asquic-keys/snapkey-cert.pem \
    --key  scratchpad/asquic-keys/snapkey-key.pem              # 3. serves the QUIC/HTTP-3
```

**Both receivers are required.** `asquic-receiver.py` speaks the QUIC the bump routes to, but
it does no mDNS — `mdns-advertise.py` is what publishes the `_asquic` record iOS resolves.
Without it the NFC half completes and then has nowhere to go.

Before spending a bump, confirm you are actually discoverable:

```bash
.venv/bin/python scripts/receiver-preflight.py    # exit 0 = safe to bump
```

If OWL restarts, `awdl0` comes back with a new ifindex and the advertiser stops publishing,
even though the process and its socket still look fine. Browsing for your own service is one
way to check.

### 5. Bump

Unlock the iPhone, hold its top edge to the Proxmark3 antenna, tap **Share** on the NameDrop
prompt. The card you send back is `samples/contact.vcf` — override with `--vcard`.

With an Android phone, stay on the iPhone's **home screen** (no share sheet), and tap the
iPhone's top edge to the phone's **NFC antenna** every few seconds rather than holding it
there. On a Pixel the antenna is in the **middle** of the back, and I found it works best with
the Pixel on top of the iPhone. See [the app's README](android/namedrop-card/README.md#4-bump).

---

## How NameDrop actually works

```
ECP   → hear the iPhone's NameDrop poll, answer NameDrop-TCI then AirDrop-TCI
        — in our testing that is what got iOS to classify us as a peer, not a tag
bump  → iOS selects the boop AID and runs com.apple.boop.SNAP. Both sides push a
        ServerInfo; ours carries key 1 (our P-256 key) and key 2 (our listenerUUID)
UI    → iPhone shows the NameDrop "share your contact card" prompt
tap   → user taps Share                              (the allowed consent tap)
mDNS  → iPhone resolves <listenerUUID>._asquic._udp  → our link-local address
QUIC  → iPhone opens QUIC v1 to [our fe80]:60192, ALPN h3, TLS 1.3, no SNI
TLS   → our cert is accepted because its public key == the key we committed over NFC
HTTP3 → POST /Hello → 200 · POST /Ask → 200 · POST /Exchange {their vCard} → 200 {ours}
```

<details>
<summary><b>Vocabulary</b> — every term in that stack, once</summary>

| Term | What it is |
|---|---|
| **ECP** | Apple's proprietary extension to ISO 14443-A polling: a frame broadcast *before* any card is selected, saying what kind of field this is. [docs/ecp-frame.md](docs/ecp-frame.md) |
| **TCI** | The 3 bytes inside an ECP frame that pick the flavour — `01 00 01` NameDrop, `01 00 00` AirDrop. These are the real discriminator; the config byte is not. |
| **warp** | The glow animation iOS plays when it accepts a NameDrop ECP frame. A trigger, not a success — six takes warped and then went nowhere. |
| **ISO-DEP** | ISO 14443-4, the APDU-carrying card protocol. A bump is a *card* transaction, which is why a reader-only PN532 can never be selected. |
| **boop AID** | The applet iOS selects once it classifies us as a peer: `A0 00 00 08 58` (Apple's RID) + `04` + ASCII `OSE.CH.` + `01`. "boop" is Apple's own name for the bump, not ours. |
| **SNAP** | `com.apple.boop.SNAP`, the CBOR-over-APDU handshake that runs on that applet — two activations: ServerInfo, Capabilities, then a resume. |
| **ServerInfo** | The CBOR map each side pushes during SNAP. **Key 1** = a DER P-256 public key; **key 2** = that side's listenerUUID. |
| **listenerUUID** | ServerInfo key 2. Literally the instance name of the `_asquic._udp` record iOS then resolves. |
| **`_asquic._udp`** | The Bonjour service the bump routes to — *not* `_airdrop._tcp`, which is the legacy path every earlier attempt stalled on. |
| **AWDL** | Apple Wireless Direct Link, the Wi-Fi link the mDNS and QUIC both ride on. [OWL](https://github.com/seemoo-lab/owl) is the open implementation. |

</details>

### Why no Apple trust is needed

The TLS gate is a key binding, not a certificate chain. iOS accepts any certificate whose
public key matches the P-256 key committed in SNAP ServerInfo key 1, and you hold that key
because you minted it. A self-signed cert built from it is accepted; a random-key one is
rejected. **No Apple-signed validation record, no keychain extraction, no Mac.**

### The ECP line is the least isolated claim here

The flash that first produced a boop select changed three things at once: it added the
NameDrop-TCI frame, stopped answering iOS's NDEF read, and shortened the ECP burst. Any of the
three could be the load-bearing one, and the order of the two frames has never been varied.
[firmware/hf_namedrop.c](firmware/hf_namedrop.c) names the control arms worth running.

### Why a Proxmark3 or a phone, and not a PN532

A bump is an ISO-DEP *card* transaction. A PN532 is a reader, and a reader can never be
selected — emit ECP from one and the iPhone plays the warp, then hangs at "Keep Holding
Nearby to Share" forever. Android's host card emulation *is* a card. An unprivileged app can
also emit ECP by toggling reader mode on and off. See [docs/hardware.md](docs/hardware.md).

---

## Layout

```
firmware/    Proxmark3 standalone mode + its patches (GPL-3.0)
android/     namedrop-card: the Android phone as the NFC card
scripts/     setup, AWDL bring-up, identity minting, the two receivers, preflight
src/namedrop pure-logic helpers: ECP frame builder, vCard builder
docs/        hardware, the ECP frame reference
patches/     OWL + OpenDrop patches (auto-applied by setup-linux.sh)
samples/     the contact card we send back
```

Comments cite research takes by name (e.g. `asquic-quic-connect-20260814`). Those point into
a private capture archive that is not published; the finding is stated where it is cited.

## Prior art

| Layer | Project |
|---|---|
| AWDL | [seemoo-lab/owl](https://github.com/seemoo-lab/owl) |
| Monitor mode | [seemoo-lab/nexmon](https://github.com/seemoo-lab/nexmon) |
| AirDrop / mDNS | [seemoo-lab/opendrop](https://github.com/seemoo-lab/opendrop) |
| NFC ECP | [kormax/apple-enhanced-contactless-polling](https://github.com/kormax/apple-enhanced-contactless-polling) |
| Proxmark3 | [RfidResearchGroup/proxmark3](https://github.com/RfidResearchGroup/proxmark3) |
| QUIC / HTTP-3 | [aiortc/aioquic](https://github.com/aiortc/aioquic) |

**Responsible use.** This is interoperability work — letting devices you own share a contact.

## License

MIT — see [LICENSE](LICENSE). The Proxmark3 standalone mode under `firmware/` is
GPL-3.0-or-later, per the header on the file.

Experimental research software. "Apple", "AirDrop", and "NameDrop" are trademarks of Apple
Inc.; this project is not affiliated with or endorsed by Apple.
