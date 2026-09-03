# Proxmark3 standalone modes

Two custom [Iceman-fork](https://github.com/RfidResearchGroup/proxmark3) standalone modes.
They run entirely on the PM3's ARM — no USB in the timing loop — because the NFC bump's
timing budget (a 38.66 ms frame-waiting time, activations ~19–45 ms apart) is far tighter
than a host round-trip.

| File | Mode | Role |
|---|---|---|
| `hf_namedrop.c` | `HF_NAMEDROP` | **Receiver side.** Listen as a card, hear the iPhone's NameDrop ECP poll, emit the NameDrop-TCI **and** AirDrop-TCI ECP frames back, then be an ISO-DEP card and run the `com.apple.boop.SNAP` handshake. The NameDrop-TCI frame is what got iOS to classify a non-Apple card as a **peer** instead of a tag. |
| `hf_namedrop_snap.h` | — | Generated. The pre-minted SNAP `ServerInfo` / `Capabilities` / redirect blobs `hf_namedrop.c` answers with. |

## 1. Mint your own card identity

**Do this first.** The header in this repo carries a public key blob whose private half is
*not* published, so it is useless as-is — the transport leg needs both halves.

```bash
.venv/bin/python scripts/build-snap-serverinfo.py \
  --header ~/proxmark3/armsrc/Standalone/hf_namedrop_snap.h
```

That writes the C header into your PM3 checkout, keeps a copy at `firmware/hf_namedrop_snap.h`,
and saves the private half to `snap-identity.json` at the repo root (gitignored, mode 0600).

**The identity must be shared with whatever answers the QUIC.** After the bump, iOS resolves
`<bonjourListenerUUID>._asquic._udp` and requires the TLS certificate to carry SNAP key 1 —
both of which live in that JSON file. Regenerate the card's half alone and iOS resolves a UUID
nobody is serving, which on the wire is indistinguishable from the bump never working.

The generator's positive control re-encodes a real bump's own CBOR frames and demands
byte-identical output before emitting anything. Those reference traces are research captures
and are not shipped here, so the control is skipped unless you drop your own PM3
`trace list -t 14a` captures at the paths named in `REAL_TAKES`.

## 2. Build into a Proxmark3 checkout

```bash
git clone https://github.com/RfidResearchGroup/proxmark3 ~/proxmark3
cd ~/proxmark3
git apply /path/to/namedrop-re/patches/proxmark3-standalone-namedrop.patch
cp /path/to/namedrop-re/firmware/hf_namedrop.c armsrc/Standalone/
# hf_namedrop_snap.h is already there if you ran step 1 with --header

echo "STANDALONE=HF_NAMEDROP" >> Makefile.platform
make clean && make -j
./pm3-flash-all
```

The patch registers `HF_NAMEDROP` in `armsrc/Standalone/Makefile.hal` and `Makefile.inc`.
Only one standalone mode fits on the device at a time.

`firmware/patches/iso14443a-namedrop.patch` patches the PM3's ISO14443-A layer itself — apply it too, or the card cannot hold the timing the transaction needs.

## 3. Run a take

```bash
cd ~/proxmark3 && ./pm3
pm3 --> hw standalone
```

The mode then runs on the ARM and streams `[#]` debug lines. Bump the iPhone against the
PM3 antenna. Afterwards, in the same session:

```
pm3 --> trace list -t 14a
```

Reading a take:

- **`HF_NAMEDROP` positive** — iOS SELECTs the 14-byte AID
  `A0 00 00 08 58 04 4F 53 45 2E 43 48 2E 01` (`com.apple.boop`), i.e. it classified us as a
  **peer**, and then runs the two-activation `com.apple.boop.SNAP` handshake. That AID splits
  as Apple's 5-byte RID `A0 00 00 08 58` + a 9-byte PIX (`04`, then ASCII **`OSE.CH.`**, then
  `01`) — the `CH` matching the ECP2.0 **Connection Handover** frame that got us classified in the first
  place. "boop" is Apple's own name for it, not ours: the literal string
  `com.apple.boop.SNAP` is in ServerInfo key 1 on the wire.
- **`HF_NAMEDROP` negative** — iOS SELECTs `D2 76 00 00 85 01 01` (NDEF): it is reading us
  as an ordinary **tag**.
- **VOID** — we never heard the iPhone's ECP frame, or it never polled our card. That is
  geometry, not a result; reseat the phone against the coil and re-run.

The visible correlate of a satisfied reader is the warp completing with a **pop and no
"AirDrop: Hold Close to Share"** — that hang is the documented failure mode when the card
answers nothing, so its absence is the tell.

## 4. The card is only half of it

A completed SNAP handshake hands the iPhone your `bonjourListenerUUID` and then **iOS has
somewhere to go**: it resolves `<uuid>._asquic._udp` and opens QUIC. If nothing is listening,
you get NDEF fall-backs and a phone that sits at "Waiting".

Bring up **both** `scripts/mdns-advertise.py` and `scripts/asquic-receiver.py` on the Linux
box — keyed to this same SNAP identity — before you spend a bump. See the main
[README](../README.md).

## Licensing

These files are derived from the Proxmark3 project and are **GPL-3.0-or-later**, per the
header on each file — not the repository's MIT license.
