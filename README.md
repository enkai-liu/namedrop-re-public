# namedrop-re

An implementation of Apple's **NameDrop** protocol — bump a Proxmark3 with an iPhone, get the
iPhone's contact card back over QUIC, and send one in return.

**Hardware:** Proxmark3, **AR9271 USB Wi-Fi adapter**, any Ubuntu 24.04 machine.

If you want the protocol first, skip to **[How NameDrop actually works](#how-namedrop-actually-works)**.

---

## Setup

```bash
git clone --recurse-submodules https://github.com/enkai-liu/namedrop-re-public.git namedrop-re
cd namedrop-re
./scripts/setup-linux.sh      # apt deps, build OWL, apply patches, create .venv
./scripts/check-hardware.sh   # PASS/FAIL before you sink time into OWL
```

`setup-linux.sh` is idempotent and applies the patches in `patches/`:

| Patch | Fixes |
|---|---|
| `owl-passive-monitor-fallback` | The AR9271 (`ath9k_htc`) supports only *passive* monitor mode; stock OWL demands active and aborts. |
| `opendrop-py312-zeroconf-compat` | Python 3.12 dropped `key_file`/`cert_file` from `HTTPSConnection`; modern zeroconf needs an `update_service` listener. |
| `opendrop-chunked-receive` | Modern senders POST chunked with no `Content-Length`; stock OpenDrop crashed on `int(None)`. |
| `opendrop-discover-capability-fields` | `/Discover` must return the capability fields a real receiver does. |
| `opendrop-util-libarchive5` | libarchive-c 5.x changed `ArchiveEntry.__init__`, breaking OpenDrop's archive helper on import-adjacent paths. |
| `opendrop-vr-identity` | Makes OpenDrop's self-signed vs supplied-identity choice explicit and logged. |

## Running a NameDrop

### 1. Mint one SNAP identity

Everything downstream keys off this. Do it once per rig, **first**.

```bash
.venv/bin/python scripts/build-snap-serverinfo.py \
  --header ~/proxmark3/armsrc/Standalone/hf_namedrop_snap.h
```

Writes the private half to `snap-identity.json` (gitignored, mode 0600).

The same identity feeds two places that must stay in sync:

| Artifact | Used by |
|---|---|
| `firmware/hf_namedrop_snap.h` | the Proxmark3 standalone mode |
| `snap-identity.json` | the mDNS instance name, and the TLS cert's key |

Regenerate one half without the other and iOS resolves a UUID nobody is serving — which on
the wire is indistinguishable from the bump never working.

### 2. Build and flash the Proxmark3

See **[firmware/README.md](firmware/README.md)**. Short version: apply
`patches/proxmark3-standalone-namedrop.patch` to an Iceman checkout, drop in
`firmware/hf_namedrop.c`, set `STANDALONE=HF_NAMEDROP`, `make && ./pm3-flash-all`.

### 3. Make a certificate carrying SNAP key 1

Self-signed. Subject and issuer are irrelevant — a real iPhone's own cert leaves both empty.
The only thing that matters is that the public key is the P-256 key in `snap-identity.json`.

### 4. Bring up AWDL and both receivers

Three terminals:

```bash
sudo ./scripts/awdl-up.sh                                    # 1. AWDL; foreground, Ctrl-C to stop
.venv/bin/python scripts/mdns-advertise.py -i awdl0           # 2. publishes <uuid>._asquic._udp
.venv/bin/python scripts/asquic-receiver.py \
    --cert snapkey-cert.pem --key snapkey-key.pem             # 3. serves the QUIC/HTTP-3
```

**Both receivers are required.** `asquic-receiver.py` speaks the QUIC the bump routes to, but
it does no mDNS — `mdns-advertise.py` is what publishes the `_asquic` record iOS resolves.
Without it the NFC half completes and then has nowhere to go.

Before spending a bump, confirm you are actually discoverable:

```bash
.venv/bin/python scripts/receiver-preflight.py    # exit 0 = safe to bump
```

This exists because a take once burned 75 seconds of bumping while mDNS was silently dead:
OWL had restarted, `awdl0` came back with a new ifindex, and the advertiser's socket stayed
bound to the old scope id. Process alive, socket open, advertising nothing. `ps` and the
listening socket both look healthy — the only honest check is to browse for your own service
the way the iPhone would.

### 5. Bump

Unlock the iPhone, hold its top edge to the Proxmark3 antenna, tap **Share** on the NameDrop
prompt. The card you send back is `samples/contact.vcf` — override with `--vcard`.

### Two reliability fixes you would otherwise rediscover

1. **Close the QUIC connection after `/Exchange`.** Answering 200 and letting the connection
   dangle to idle timeout breaks the *next* bump. Send `CONNECTION_CLOSE` about a second later
   (`--exchange-close-delay 0` restores the old behaviour as a control arm).
2. **Send mDNS responses *from* port 5353** (RFC 6762 §6.7), and re-announce every ~2 s,
   multicast **and** unicast. We inject through a passive-monitor vif, so nothing we send is
   retransmitted at L2 — repetition is the only retransmit available. Responses sent from an
   ephemeral port are discarded on arrival, which is the nastiest version of this bug: the
   packets really are on the wire, so every "is it transmitting?" check passes.

Reflashing between bumps is **not** necessary. "iOS dedups on our listener UUID so repeat
bumps need a fresh identity" was tested and refuted — one identity completed 8 exchanges, with
iOS echoing that very `SenderID` back at us.

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

### Three mechanism facts

Each was load-bearing, and each contradicts the obvious model:

1. **The bind is the `_asquic._udp` instance name** — literally the `bonjourListenerUUID`
   from SNAP ServerInfo key 2. Not the SRV hostname, not `_airdrop._tcp`.
2. **The transport is QUIC + HTTP/3.** Every attempt before this stalled after `/Discover`,
   waiting for an `/Ask` on a transport the bump never uses.
3. **The TLS gate is a key binding, not Apple trust.** The certificate must carry the same
   P-256 key handed over in SNAP ServerInfo key 1 — which you already hold, because you
   minted it. A self-signed cert built from that key is accepted; a random-key one is
   rejected. **No Apple-signed validation record, no keychain extraction, no Mac.**

   The binding is **symmetric**: the iPhone's own `_asquic` certificate carries the exact key
   it committed in that same bump, with an **empty subject and empty issuer**. The
   certificate needs no name because the NFC exchange already said which key to expect.

Because the bind is cryptographic rather than physical, the device answering the NFC need not
be the device answering the QUIC. That is what makes a future phone-as-card port viable.

### The ECP line is the least isolated claim here

The flash that first produced a boop select changed three things at once: it added the
NameDrop-TCI frame, stopped answering iOS's NDEF read, and shortened the ECP burst. Any of the
three could be the load-bearing one, and the order of the two frames has never been varied.
[firmware/hf_namedrop.c](firmware/hf_namedrop.c) names the control arms worth running.

### Why a Proxmark3 and not a PN532

A bump is an ISO-DEP *card* transaction. A PN532 is a reader, and a reader can never be
selected — emit ECP from one and the iPhone plays the warp, then hangs at "Keep Holding
Nearby to Share" forever. See [docs/hardware.md](docs/hardware.md).

---

## Layout

```
firmware/    Proxmark3 standalone mode + its patches (GPL-3.0)
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

The QUIC/HTTP-3 application protocol (`/Hello` → `/Ask` → `/Exchange`) is not publicly
documented anywhere. It was implemented request-by-request by observing each one and
answering it.

**Responsible use.** This is interoperability work — letting devices you own share a contact.
It is not a tool for tracking, de-anonymizing, or harvesting AirDrop users. The known AirDrop
hashed-identifier leaks are explicitly out of scope.

## License

MIT — see [LICENSE](LICENSE). The Proxmark3 standalone mode under `firmware/` is
GPL-3.0-or-later, per the header on the file.

Experimental research software. "Apple", "AirDrop", and "NameDrop" are trademarks of Apple
Inc.; this project is not affiliated with or endorsed by Apple.
