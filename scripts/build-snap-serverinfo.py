#!/usr/bin/env python3
"""Build the com.apple.boop.SNAP ServerInfo blobs our card answers with (gap #2).

Why a host-side generator: the PM3's AT91SAM7S512 (ARM7TDMI, no crypto accelerator)
cannot generate a P-256 keypair and an Ed25519 signature inside the 38.66 ms FWT the
ATS negotiates. So every expensive byte is minted HERE, baked into a C header, and the
firmware's answer to the boop GET DATA becomes a memcpy.

The wire format is not guessed -- it is decoded byte-for-byte out of two real
iPhone<->iPhone bumps sniffed with a Proxmark3. Those traces are research captures and are not
shipped here; drop your own at the paths in REAL_TAKES and the POSITIVE CONTROL re-encodes the
reader's own frames and asserts the result is byte-identical before emitting anything of ours.
Without them the control is skipped.

    ServerInfo (238 B, sent by BOTH sides -- reader pushes first, card mirrors):
      { 0: "1.1",
        1: "com.apple.boop.SNAP",
        2: [ {0: "Bonjour", 1: 0} ],
        3: { 0: bytes(4)  CFAbsoluteTime, little-endian  -- IDENTICAL on both sides;
                          the card ECHOES the reader's, so the firmware patches it at runtime
             1: bytes(91) DER SubjectPublicKeyInfo, EC P-256   -- per-side ephemeral
             2: bytes(16) RFC 4122 v4 UUID = the bonjourListenerUUID (unknown #3)
             3: bytes(6)  rotating token
             4: bytes(64) Ed25519 signature under a key that never crosses the air
             5: 1 } }

    Capabilities (85 B, the reader's 2nd GET DATA; the card returns it verbatim on resume):
      { 0: "1.1", 1: "com.apple.boop.SNAP", 2: [],
        3: { 5: 2, "RPKnownIdentityKey": 0, "RPSupportsApplicationLabelKey": 1 } }

    Redirect (9 B + SW 6A88, the card's answer to the capabilities push):
      { 1: <n>, 2: <session id> }   -- the reader echoes the session id back in 00 CA 01 04

Field 4 is deliberately self-signed: evidence take snap-field4-signature-20260813 proves
neither peer can verify the other's field 4 at NFC time (the verification key never crosses
the air), and evidence take sharing-framework-inventory-20260813 finds no unforgeable
receiver gate in Everyone mode. Well-formed is all that is required.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scratchpad"))

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---------------------------------------------------------------- CBOR (definite lengths)


def _hd(major: int, v: int) -> bytes:
    if v < 24:
        return bytes([major << 5 | v])
    if v < 0x100:
        return bytes([major << 5 | 24, v])
    if v < 0x10000:
        return bytes([major << 5 | 25]) + v.to_bytes(2, "big")
    if v < 0x100000000:
        return bytes([major << 5 | 26]) + v.to_bytes(4, "big")
    return bytes([major << 5 | 27]) + v.to_bytes(8, "big")


def cbor_enc(o) -> bytes:
    if isinstance(o, bool):
        return bytes([0xF5 if o else 0xF4])
    if o is None:
        return b"\xf6"
    if isinstance(o, int):
        return _hd(0, o) if o >= 0 else _hd(1, -1 - o)
    if isinstance(o, bytes):
        return _hd(2, len(o)) + o
    if isinstance(o, str):
        e = o.encode()
        return _hd(3, len(e)) + e
    if isinstance(o, list):
        return _hd(4, len(o)) + b"".join(cbor_enc(x) for x in o)
    if isinstance(o, dict):
        return _hd(5, len(o)) + b"".join(cbor_enc(k) + cbor_enc(v) for k, v in o.items())
    raise TypeError(type(o))


def cbor_dec(b: bytes, i: int = 0):
    m, ai = b[i] >> 5, b[i] & 0x1F
    i += 1
    if ai < 24:
        v = ai
    elif ai == 24:
        v = b[i]; i += 1
    elif ai == 25:
        v = int.from_bytes(b[i:i + 2], "big"); i += 2
    elif ai == 26:
        v = int.from_bytes(b[i:i + 4], "big"); i += 4
    elif ai == 27:
        v = int.from_bytes(b[i:i + 8], "big"); i += 8
    else:
        raise ValueError(f"additional info {ai}")
    if m == 0:
        return v, i
    if m == 1:
        return -1 - v, i
    if m == 2:
        return b[i:i + v], i + v
    if m == 3:
        return b[i:i + v].decode("utf-8", "replace"), i + v
    if m == 4:
        out = []
        for _ in range(v):
            x, i = cbor_dec(b, i)
            out.append(x)
        return out, i
    if m == 5:
        out = {}
        for _ in range(v):
            k, i = cbor_dec(b, i)
            x, i = cbor_dec(b, i)
            out[k] = x
        return out, i
    raise ValueError(f"major {m}")


# ---------------------------------------------------------------- PM3 trace parsing (control)

_ROW = re.compile(r"^\s*(\d+)?\s*\|\s*(\d+)?\s*\|\s*(Rdr|Tag)?\s*\|(.*?)\|\s*(ok|nok)?\s*\|(.*)$")


def parse_trace(path: str):
    rows, cur = [], None
    with open(path, errors="replace") as fh:
        for line in fh.read().splitlines():
            m = _ROW.match(line)
            if not m:
                continue
            st, en, src, data, crc, ann = m.groups()
            if src:
                cur = {"src": src, "hex": data.strip(),
                       "start": int(st) if st else 0, "end": int(en) if en else 0,
                       "crc": crc or "", "ann": ann.strip()}
                rows.append(cur)
            elif cur is not None:
                cur["hex"] += " " + data.strip()
    for r in rows:
        out = []
        for t in r["hex"].split():
            # Short frames are rendered with their bit count, e.g. REQA as `26(7)`.
            m = re.fullmatch(r"([0-9A-Fa-f]{2})(?:\(\d\))?", t)
            if m:
                out.append(int(m.group(1), 16))
        r["bytes"] = bytes(out)
    return rows


# Proxmark3 `trace list -t 14a` captures of a real iPhone<->iPhone bump, used only by the
# positive control below. Our own research captures; NOT shipped. Drop your own here and the
# control runs, otherwise it is skipped.
REAL_TAKES = [
    "captures/bump-namedrop-01.txt",
    "captures/bump-namedrop-02.txt",
]


def positive_control() -> bool:
    """Decode every real SNAP frame and re-encode it; demand byte-identical output.

    Returns True (passed), False (failed) or None (no reference traces, control not run).

    If this fails, our CBOR writer does not match Apple's canonical form and nothing
    downstream may be trusted.
    """
    print("[control] re-encoding Apple's own SNAP frames with our CBOR writer")
    total, bad = 0, 0
    for rel in REAL_TAKES:
        path = os.path.join(REPO, rel)
        if not os.path.exists(path):
            print(f"  -- no reference trace at {rel}; skipping the control")
            return None
        for r in parse_trace(path):
            b = r["bytes"]
            if len(b) < 40:
                continue
            body = b[6:6 + b[5]] if r["src"] == "Rdr" else b[1:-4]
            try:
                obj, n = cbor_dec(body)
            except Exception:
                continue
            if n != len(body):
                continue
            total += 1
            if cbor_enc(obj) != body:
                bad += 1
                print(f"  MISMATCH {r['src']} {len(body)}B")
    print(f"  {total - bad}/{total} frames round-trip byte-identical")
    return total >= 4 and bad == 0


# ---------------------------------------------------------------- identity minting


def load_identity(path: str):
    """Rebuild the blobs from an EXISTING identity instead of minting a new one.

    The card's SNAP key 2 and the receiver's mDNS SRV hostname must be the SAME UUID -- that
    pairing is the whole bump->AirDrop bind (evidence take sharingd-airdrop-bind-20260814).
    Minting a fresh UUID on every run silently breaks it: the header gets a new UUID, the
    running receiver keeps advertising the old one, and the card then promises a host nobody
    advertises. Reusing by default makes the two ends impossible to desync by accident.
    """
    with open(path) as fh:
        p = json.load(fh)
    spki = bytes.fromhex(p["p256_spki_der"])
    listener = uuid.UUID(p["bonjour_listener_uuid"]).bytes
    token6 = bytes.fromhex(p["token6"])
    sig = bytes.fromhex(p["field4_signature"])
    assert len(spki) == 91 and len(token6) == 6 and len(sig) == 64
    return spki, listener, token6, sig, p


def mint_identity(seed_hex: str | None):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec, ed25519

    p256 = ec.generate_private_key(ec.SECP256R1())
    spki = p256.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    assert len(spki) == 91, f"SPKI is {len(spki)} B, real bumps carry 91"

    ed = ed25519.Ed25519PrivateKey.generate()

    listener_uuid = uuid.uuid4()
    token6 = os.urandom(6)

    # Field 4's signed message is unknown -- 64k verifications over the on-air corpus found
    # no match (snap-field4-signature-20260813), and no peer can verify it at NFC time. Sign
    # a stable, timestamp-independent transcript so the value is reproducible from the saved
    # key: the firmware patches key 0 at runtime, so key 0 must not be under the signature.
    msg = spki + listener_uuid.bytes + token6
    sig = ed.sign(msg)
    assert len(sig) == 64

    priv = {
        "p256_pkcs8_der": p256.private_bytes(
            serialization.Encoding.DER,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).hex(),
        "p256_spki_der": spki.hex(),
        "ed25519_seed": ed.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        ).hex(),
        "ed25519_public": ed.public_key()
        .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        .hex(),
        "bonjour_listener_uuid": str(listener_uuid),
        "token6": token6.hex(),
        "field4_signed_message": msg.hex(),
        "field4_signature": sig.hex(),
    }
    return spki, listener_uuid.bytes, token6, sig, priv


# ---------------------------------------------------------------- blob assembly

PLACEHOLDER_TS = bytes([0xAA, 0xBB, 0xCC, 0xDD])  # patched at runtime with the reader's key 0


def build_serverinfo(spki: bytes, listener: bytes, token6: bytes, sig: bytes) -> bytes:
    return cbor_enc(
        {
            0: "1.1",
            1: "com.apple.boop.SNAP",
            2: [{0: "Bonjour", 1: 0}],
            3: {0: PLACEHOLDER_TS, 1: spki, 2: listener, 3: token6, 4: sig, 5: 1},
        }
    )


def build_caps() -> bytes:
    return cbor_enc(
        {
            0: "1.1",
            1: "com.apple.boop.SNAP",
            2: [],
            3: {5: 2, "RPKnownIdentityKey": 0, "RPSupportsApplicationLabelKey": 1},
        }
    )


def build_redirect(session_id: int) -> bytes:
    # A real card answered {1: 719, 2: 31279} / {1: 723, 2: 1119} with SW 6A88; the reader
    # echoes key 2 back in the follow-up 00 CA 01 04. Key 1's meaning is unknown -- it sat
    # at 719/723 across both takes, so we hold it in that range.
    return cbor_enc({1: 719, 2: session_id})


def c_array(name: str, data: bytes, per_line: int = 12) -> str:
    out = [f"static const uint8_t {name}[{len(data)}] = {{"]
    for i in range(0, len(data), per_line):
        out.append("    " + " ".join(f"0x{b:02x}," for b in data[i:i + per_line]))
    out.append("};")
    return "\n".join(out)


def java_array(name: str, data: bytes) -> str:
    """A byte[] as a hex string decoded once at class-init.

    Deliberately NOT a `{(byte)0xa4, ...}` literal: a 238-element array initialiser
    compiles to 238 bytecode stores in <clinit>, and the 64 KB method limit is a silly
    thing to walk towards. A hex string is one constant-pool entry, and the decode runs
    at class load -- never inside processCommandApdu(), which must not do real work.
    """
    return (f'    /** {len(data)} bytes. */\n'
            f'    public static final byte[] {name} = h(\n'
            + "\n".join(f'            "{data.hex()[i:i + 96]}"' + ("" if i + 96 >= len(data.hex()) else " +")
                        for i in range(0, len(data.hex()), 96))
            + ");")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--header", default=os.path.expanduser("~/proxmark3/armsrc/Standalone/hf_namedrop_snap.h"),
                    help="C header to write (default: the PM3 standalone dir)")
    ap.add_argument("--repo-copy", default=os.path.join(REPO, "firmware", "hf_namedrop_snap.h"),
                    help="second copy kept in-repo for reproducibility")
    ap.add_argument("--identity", default=os.path.join(REPO, "scratchpad", "snap-identity.json"),
                    help="where to save the private key material (gitignored)")
    ap.add_argument("--java", default=os.path.join(
                        REPO, "android", "namedrop-card", "app", "src", "main", "java",
                        "com", "namedrop", "card", "SnapBlobs.java"),
                    help="generated Java blobs for the Android HCE applet. Emitted from the SAME "
                         "identity as the C header, because the phone answers the NFC while Linux "
                         "answers the QUIC and the two are bound by SNAP key 1 + the listener UUID: "
                         "if they drift, iOS resolves a UUID nobody is serving.")
    ap.add_argument("--session-id", type=int, default=0x5AA5, help="our SNAP session id")
    ap.add_argument("--skip-control", action="store_true", help="skip the CBOR positive control")
    ap.add_argument("--regenerate", action="store_true",
                    help="mint a NEW identity (new listenerUUID + keys). By default an existing "
                         "--identity file is REUSED, because the card's SNAP key 2 and the "
                         "receiver's mDNS SRV hostname must stay the same UUID; regenerating "
                         "means you must also restart the receiver so both ends re-read it.")
    args = ap.parse_args()

    if not args.skip_control:
        result = positive_control()
        if result is False:
            print("\nPOSITIVE CONTROL FAILED -- refusing to emit blobs.", file=sys.stderr)
            return 2
        print("  control PASSED\n" if result else "")

    if os.path.exists(args.identity) and not args.regenerate:
        spki, listener, token6, sig, priv = load_identity(args.identity)
        print(f"[id  ] REUSING the existing identity at {args.identity}")
        print( "        (pass --regenerate to mint a new one -- then restart the receiver too)")
    else:
        spki, listener, token6, sig, priv = mint_identity(None)
        print("[id  ] minted a NEW identity"
              + ("" if not os.path.exists(args.identity) else " (--regenerate)"))
        print( "        ** restart scripts/mdns-advertise.py so the SRV hostname matches **")
    si = build_serverinfo(spki, listener, token6, sig)
    caps = build_caps()
    redir = build_redirect(args.session_id)

    if len(si) != 238:
        print(f"!! ServerInfo is {len(si)} B, real bumps carry 238", file=sys.stderr)
        return 2
    if len(caps) != 85:
        print(f"!! Capabilities is {len(caps)} B, real bumps carry 85", file=sys.stderr)
        return 2

    ts_off = si.find(PLACEHOLDER_TS)
    if ts_off < 0 or si.count(PLACEHOLDER_TS) != 1:
        print("!! timestamp placeholder is not uniquely locatable", file=sys.stderr)
        return 2

    # ---- split the blob so the firmware can echo the READER's protocol string ----
    # A real bump carries a different service name per flow: `com.apple.boop.SNAP` for a
    # NameDrop contact exchange, `com.apple.airdrop.sharesheet` for a share-sheet AirDrop.
    # That string is the ONLY difference between the two payload shapes (it accounts for
    # 248-238 and 95-85 exactly), so rather than pick one we emit head + tail and let the
    # firmware splice the reader's own text item in between -- correct for any flow, and
    # for a service name we have never seen.
    head = cbor_enc({})[:0] + b"\xa4\x00" + cbor_enc("1.1") + b"\x01"
    if si[:len(head)] != head:
        print("!! ServerInfo does not start with the expected map(4)/key0/'1.1'/key1 head", file=sys.stderr)
        return 2
    label_item = cbor_enc("com.apple.boop.SNAP")
    tail = si[len(head) + len(label_item):]
    tail_ts_off = ts_off - len(head) - len(label_item)
    if tail[tail_ts_off:tail_ts_off + 4] != PLACEHOLDER_TS:
        print("!! timestamp offset does not survive the head/tail split", file=sys.stderr)
        return 2
    print(f"[blob] head {len(head)} B + <reader's label> + tail {len(tail)} B "
          f"(timestamp at tail offset {tail_ts_off})")

    print(f"[blob] ServerInfo   {len(si)} B   key-0 timestamp at offset {ts_off}")
    print(f"[blob] Capabilities {len(caps)} B")
    print(f"[blob] Redirect     {len(redir)} B  session id 0x{args.session_id:04x}")
    print(f"[id  ] bonjourListenerUUID {priv['bonjour_listener_uuid']}")
    print(f"[id  ] P-256 SPKI          {spki.hex()[:48]}...")

    # A card frame is PCB(1) + body + SW(2) + CRC(2); the reader's RATS advertised
    # FSD = 256, so the 238-byte ServerInfo answer fits in ONE frame with no chaining.
    frame = 1 + len(si) + 2 + 2
    print(f"[wire] largest card frame {frame} B  (FSD 256 -> {'no chaining' if frame <= 256 else 'CHAINING REQUIRED'})")

    hdr = f"""// Generated by scripts/build-snap-serverinfo.py -- DO NOT EDIT BY HAND.
//
// com.apple.boop.SNAP blobs for the HF_NAMEDROP standalone mode (gap #2).
// Shapes decoded byte-for-byte from two real iPhone<->iPhone bumps; the generator's
// positive control re-encodes Apple's own frames and demands identical bytes.
//
// The AT91SAM7S512 has no crypto accelerator, so the P-256 keypair, the Ed25519
// signature, the listener UUID and the token are all minted on the host and baked in
// here: answering the boop GET DATA is then a memcpy, comfortably inside the 38.66 ms
// FWT our ATS negotiates.
//
// bonjourListenerUUID (SNAP key 2) = {priv['bonjour_listener_uuid']}
//   -- this is the handle iOS uses to find us over AWDL/Bonjour after the bump.
#ifndef HF_NAMEDROP_SNAP_H__
#define HF_NAMEDROP_SNAP_H__

#include "common.h"

// Our ServerInfo. Key 0 (4 bytes at SNAP_SI_TS_OFFSET) is a placeholder: a real card
// ECHOES the reader's CFAbsoluteTime, so the firmware copies it out of the incoming
// frame before answering.
{c_array('snap_serverinfo', si)}
#define SNAP_SI_LEN         {len(si)}
#define SNAP_SI_TS_OFFSET   {ts_off}

// The same ServerInfo, split either side of the protocol-string text item, so the card can
// splice in whatever service name the READER offered. A real bump carries
// `com.apple.boop.SNAP` for a NameDrop contact exchange and `com.apple.airdrop.sharesheet`
// for a share-sheet AirDrop -- that string is the ONLY difference between the two payload
// shapes (it accounts for 248-238 and 95-85 exactly). Answering with the wrong one is a
// service mismatch, so we echo instead of choosing.
{c_array('snap_si_head', head)}
#define SNAP_SI_HEAD_LEN    {len(head)}
{c_array('snap_si_tail', tail)}
#define SNAP_SI_TAIL_LEN    {len(tail)}
#define SNAP_SI_TAIL_TS_OFF {tail_ts_off}

// The reader's 2nd GET DATA payload; a real card returns it verbatim on resume (00 CA 01 04).
{c_array('snap_caps', caps)}
#define SNAP_CAPS_LEN       {len(caps)}

// Answer to the capabilities push: {{1: 719, 2: session}} with SW 6A88 (a redirect, not an error).
{c_array('snap_redirect', redir)}
#define SNAP_REDIRECT_LEN   {len(redir)}
#define SNAP_SESSION_ID     0x{args.session_id:04x}

#endif // HF_NAMEDROP_SNAP_H__
"""

    for path in filter(None, [args.header, args.repo_copy]):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(hdr)
        print(f"[out ] header   -> {path}")

    if args.java:
        jsrc = f"""// Generated by scripts/build-snap-serverinfo.py -- DO NOT EDIT BY HAND.
//
// com.apple.boop.SNAP blobs for the Android HCE applet, emitted from the SAME
// identity as firmware/hf_namedrop_snap.h and snap-identity.json.
//
// 🔑 WHY ONE IDENTITY ACROSS THREE ARTIFACTS. After the bump iOS resolves
// <bonjourListenerUUID>._asquic._udp and requires the TLS cert to carry SNAP key 1. The
// bind is cryptographic, not physical, so the device answering the NFC need NOT be the
// device answering the QUIC -- the phone can be the card while Linux is the receiver.
// But they must be built from one identity: regenerate half of it and iOS resolves a
// UUID nobody is serving, which on the wire looks exactly like the bump never worked.
//
// bonjourListenerUUID (SNAP key 2) = {priv['bonjour_listener_uuid']}
package com.namedrop.card;

public final class SnapBlobs {{

    private SnapBlobs() {{
    }}

    /** Decode at class-init, never per-APDU. */
    private static byte[] h(String s) {{
        byte[] out = new byte[s.length() / 2];
        for (int i = 0; i < out.length; i++) {{
            out[i] = (byte) Integer.parseInt(s.substring(i * 2, i * 2 + 2), 16);
        }}
        return out;
    }}

    public static final String LISTENER_UUID = "{priv['bonjour_listener_uuid']}";
    public static final int SESSION_ID = 0x{args.session_id:04x};

    /** Key 0 is a placeholder: a real card ECHOES the reader's CFAbsoluteTime. */
{java_array('SERVERINFO', si)}
    public static final int SI_TS_OFFSET = {ts_off};

    /**
     * The ServerInfo split either side of the key-1 protocol string, so we can splice in
     * whatever service the READER offered -- `com.apple.boop.SNAP` (20 B) for a NameDrop
     * contact exchange, `com.apple.airdrop.sharesheet` (30 B) for a share sheet. That
     * string is the ONLY difference between the 238/248 and 85/95 shapes, so we echo
     * rather than choose. The bump that cleared gap #1 on the Pixel offered the
     * sharesheet flavour, so hardcoding 238 would have been a service mismatch.
     */
{java_array('SI_HEAD', head)}
{java_array('SI_TAIL', tail)}
    public static final int SI_TAIL_TS_OFF = {tail_ts_off};

    /** The reader's 2nd GET DATA payload; a real card returns it verbatim on resume. */
{java_array('CAPS', caps)}

    /** Answer to the capabilities push: {{1: 719, 2: session}}, sent with SW 6A88. */
{java_array('REDIRECT', redir)}
}}
"""
        os.makedirs(os.path.dirname(args.java), exist_ok=True)
        with open(args.java, "w") as fh:
            fh.write(jsrc)
        print(f"[out ] java     -> {args.java}")

    os.makedirs(os.path.dirname(args.identity), exist_ok=True)
    priv["serverinfo_hex"] = si.hex()
    priv["serverinfo_ts_offset"] = ts_off
    priv["session_id"] = args.session_id
    with open(args.identity, "w") as fh:
        json.dump(priv, fh, indent=2)
    os.chmod(args.identity, 0o600)
    print(f"[out ] identity -> {args.identity}  (private keys; keep for the AWDL leg)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
