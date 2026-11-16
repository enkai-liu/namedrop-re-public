# namedrop-re

An implementation of Apple's NameDrop protocol. Tap an iPhone against a Proxmark3 or an
Android phone, receive the iPhone's contact card over QUIC, and send one back. On a rooted
Pixel 9 the whole exchange runs on the phone alone.

https://github.com/user-attachments/assets/d89eb796-a2ed-4275-a1fc-01187c6cd0de

There are two ways to run it:

| Setup | NFC card | AWDL + QUIC | Guide |
|---|---|---|---|
| **Rooted Pixel only** | Pixel 9 (`namedrop-card` app) | the same Pixel, as root, on Google's built-in AWDL stack | [android/awdl-root/README.md](android/awdl-root/README.md) |
| **Linux + card** | Proxmark3 (Iceman fork) or an unrooted Android phone (tested on a Pixel 9) | Linux (I use Ubuntu 24.04) with an AR9271 USB Wi-Fi adapter | this README, plus [firmware/README.md](firmware/README.md) or [android/namedrop-card/README.md](android/namedrop-card/README.md) |

In the Linux setup the NFC card only handles the bump, and the contact exchange runs over AWDL
on the USB adapter. The AR9271 works but is slow; [docs/hardware.md](docs/hardware.md#wi-fi--monitor-mode-and-frame-injection)
lists a faster MT7612U option that I haven't verified yet.

The rest of this README covers the Linux setup. For the rooted Pixel, follow its guide.

To read about the protocol, skip to [How it works](#how-it-works).

## Setup

```bash
git clone https://github.com/enkai-liu/namedrop-re-public.git namedrop-re
cd namedrop-re
./scripts/setup-linux.sh      # apt deps, patch and build OWL, create .venv
./scripts/check-hardware.sh
```

`setup-linux.sh` can be re-run. It patches OWL with `patches/owl-passive-monitor-fallback.patch`,
because the AR9271 only supports passive monitor mode and OWL otherwise exits.

## Running it

### 1. Create an identity

The firmware, the TLS certificate and the mDNS name are all generated from one identity, so
do this first:

```bash
.venv/bin/python scripts/build-snap-serverinfo.py \
  --header ~/proxmark3/armsrc/Standalone/hf_namedrop_snap.h    # Android: --header ""
```

The private key is saved to `scratchpad/snap-identity.json` (gitignored, mode 0600). Re-running
the script reuses it. `--regenerate` creates a new one, after which you have to rebuild the
card, re-run step 3, and restart the receivers.

The identity ends up in three places that must match:

| File | Used by |
|---|---|
| `hf_namedrop_snap.h` (in your Proxmark3 checkout) | Proxmark3 firmware |
| `android/namedrop-card/.../SnapBlobs.java` (gitignored) | Android app |
| `scratchpad/snap-identity.json` | mDNS name and TLS key |

### 2. Set up the card

Proxmark3: follow [firmware/README.md](firmware/README.md). In short, apply
`patches/proxmark3-standalone-namedrop.patch` to an Iceman checkout, copy in
`firmware/hf_namedrop.c`, set `STANDALONE=HF_NAMEDROP`, then `make && ./pm3-flash-all`.

Android: follow [android/namedrop-card/README.md](android/namedrop-card/README.md). In short,
`./gradlew assembleDebug`, `adb install`, and keep the app open in the foreground.

### 3. Create the TLS certificate

```bash
./scripts/mint-snapkey-cert.sh
```

This writes a self-signed certificate and key to `scratchpad/asquic-keys/`. iOS only checks
that the certificate's public key is the one sent over NFC, so subject and issuer are left
empty, as on a real iPhone. The script fails if the key doesn't match the identity.

### 4. Start AWDL and the receivers

In three terminals:

```bash
sudo ./scripts/awdl-up.sh                         # AWDL via OWL, Ctrl-C to stop
.venv/bin/python scripts/mdns-advertise.py        # advertises <uuid>._asquic._udp
.venv/bin/python scripts/asquic-receiver.py       # QUIC/HTTP-3 server
```

Both receivers are needed: `asquic-receiver.py` does not do mDNS, and without
`mdns-advertise.py` the iPhone can't find it.

Check that you're discoverable before bumping:

```bash
.venv/bin/python scripts/receiver-preflight.py    # exit 0 = ready
```

If OWL restarts, `awdl0` is recreated and the advertiser silently stops working even though
the process is still running. Restart it and re-run the preflight.

### 5. Bump

Proxmark3: unlock the iPhone, hold its top edge to the antenna, and tap Share on the NameDrop
prompt.

Android: keep the iPhone on its home screen with no share sheet open. Tap the iPhone's top
edge against the phone's NFC antenna every few seconds instead of holding it there. On a Pixel
the antenna is in the middle of the back, and it works best with the Pixel on top. See
[the app's README](android/namedrop-card/README.md#4-bump).

Received cards are saved to `received/`. The card sent back is `samples/contact.vcf`; pass
`--vcard` to `asquic-receiver.py` to use your own.

## How it works

```
ECP   iPhone polls with a NameDrop ECP frame; we answer with NameDrop-TCI and AirDrop-TCI
      frames, which makes iOS treat us as a peer instead of a tag
SNAP  iOS selects the boop applet and both sides exchange ServerInfo. Ours carries
      key 1 (our P-256 public key) and key 2 (our listenerUUID)
UI    iPhone shows the NameDrop prompt; the user taps Share
mDNS  iPhone resolves <listenerUUID>._asquic._udp to our link-local address
QUIC  iPhone connects to [fe80::...]:60192, ALPN h3, TLS 1.3, no SNI
TLS   iOS accepts our certificate because its public key matches key 1
HTTP3 POST /Hello, POST /Ask, POST /Exchange (their vCard in, ours out)
```

<details>
<summary>Terms</summary>

| Term | Meaning |
|---|---|
| ECP | Apple's extension to ISO 14443-A polling: a frame sent before any card is selected that says what kind of reader this is. See [docs/ecp-frame.md](docs/ecp-frame.md). |
| TCI | Three bytes in an ECP frame that identify the type: `01 00 01` NameDrop, `01 00 00` AirDrop. |
| ISO-DEP | ISO 14443-4, the card protocol that carries APDUs. |
| boop AID | The applet iOS selects for a peer: `A0 00 00 08 58 04 4F 53 45 2E 43 48 2E 01`. "boop" is Apple's name. |
| SNAP | `com.apple.boop.SNAP`, the CBOR-over-APDU handshake on that applet. |
| ServerInfo | The CBOR map each side sends during SNAP. Key 1 is a P-256 public key, key 2 is a listenerUUID. |
| listenerUUID | The instance name of the `_asquic._udp` service iOS looks up after the bump. |
| AWDL | Apple Wireless Direct Link, the Wi-Fi link mDNS and QUIC run over. [OWL](https://github.com/seemoo-lab/owl) is an open implementation. |

</details>

### No Apple trust needed

iOS doesn't validate a certificate chain. It accepts any certificate whose public key matches
the one sent in ServerInfo key 1, and since you generated that key, a self-signed certificate
works. No Apple ID, keychain extraction, or Mac is involved.

### What isn't fully tested

The firmware change that first got iOS to select the boop applet changed three things at
once: it added the NameDrop-TCI frame, stopped answering iOS's NDEF read, and sent fewer ECP
frames per burst. I haven't isolated which of these matters, and haven't tried swapping the
order of the two frames. [firmware/hf_namedrop.c](firmware/hf_namedrop.c) lists the tests
that would.

### AWDL on a Pixel

Pixels already speak AWDL: Google's Quick Share uses it for AirDrop interop, through a stack
called `mosey` that exposes the link as `mosey0`. Apps can't use it, but root can. We raise the
link by calling Google's `IMoseyService` directly, announce our `_asquic` instance on it, add the
routing rule the link is missing, and run the QUIC/HTTP-3 receiver in Java on the phone.
Details in [android/awdl-root/README.md](android/awdl-root/README.md#why-this-works).

### Why not a PN532

The bump needs a device that acts as an NFC card. A PN532 can only be a reader, so the iPhone
shows the NameDrop animation and then waits at "Keep Holding Nearby to Share" indefinitely.
Android's host card emulation is a card, and an ordinary app can send ECP frames by briefly
switching to reader mode. More in [docs/hardware.md](docs/hardware.md).

## Layout

```
firmware/    Proxmark3 standalone mode (GPL-3.0)
android/     namedrop-card: the NFC card app (+ the on-phone receiver)
             awdl-root: root tools that run AWDL and the receiver on a Pixel
scripts/     setup, AWDL, identity and cert generation, receivers, preflight
docs/        hardware notes, ECP frame reference
patches/     OWL patch (applied by setup-linux.sh), Proxmark3 Makefile patch, Flupke patch
samples/     the contact card sent back
```

Code comments refer to test runs by name (e.g. `asquic-quic-connect-20260814`). Those captures
aren't published; the comment states the result.

## Prior art

| Layer | Project |
|---|---|
| AWDL | [seemoo-lab/owl](https://github.com/seemoo-lab/owl) |
| NFC ECP | [kormax/apple-enhanced-contactless-polling](https://github.com/kormax/apple-enhanced-contactless-polling) |
| Proxmark3 | [RfidResearchGroup/proxmark3](https://github.com/RfidResearchGroup/proxmark3) |
| QUIC / HTTP-3 | [aiortc/aioquic](https://github.com/aiortc/aioquic), and on Android [ptrd/kwik](https://github.com/ptrd/kwik) + [ptrd/flupke](https://github.com/ptrd/flupke) |

## License

MIT, see [LICENSE](LICENSE). `firmware/hf_namedrop.c` is GPL-3.0-or-later, per its header.
`patches/flupke-0.9.4-no-authority.patch` modifies Flupke and is LGPL-3.0, like Flupke.

This is experimental research software for exchanging contacts between devices you own.
Apple, AirDrop and NameDrop are trademarks of Apple Inc. This project is not affiliated with
or endorsed by Apple.
