"""Build (and, on hardware, emit) the NameDrop Enhanced Contactless Polling frame.

The frame layout is documented in docs/ecp-frame.md and comes from
https://github.com/kormax/apple-enhanced-contactless-polling

    6a 02 89 05 00 01 00 01 <6-byte BLE MAC> <CRC-A>

The frame builder here is pure and unit-tested. Actually pushing it onto the air needs a
PN532 in raw-TX mode, which is the hardware TODO at the bottom.
"""
from __future__ import annotations

import time

# ECP v2 frame = 6a | 02 | <config> | 05 | 00 | <TCI, 3 B> | <data> | CRC_A
#   6a = header, 02 = version (ECPv2), 05 = type (AirDrop), 00 = subtype.
#
# CONFIG IS DERIVED, NOT A DISCRIMINATOR. It is [flags][payload length], where the length
# counts TCI + data only (type/subtype are not payload). Both the NameDrop and the AirDrop
# frame carry TCI(3) + data(6) = 9, so both land on 0x89 — which is why sweeping the config
# byte was a dead end (the project notes). The real discriminators are the TCI and the data.
ECP_START = 0x6A
ECP_VERSION = 0x02
ECP_TYPE_AIRDROP = 0x05
ECP_SUBTYPE = 0x00
ECP_FLAGS = 0x8  # high nibble of the config byte; 0x8 is what a real iPhone emits

# Type 05 IS AirDrop and NameDrop is a special case of it (kormax) — the two frames differ
# only in the last TCI byte. Both were captured off real iPhones in Session B.
NAMEDROP_TCI = bytes.fromhex("010001")
AIRDROP_TCI = bytes.fromhex("010000")

# Everything up to and including the TCI, kept as named constants because they are what a
# capture is grepped for.
NAMEDROP_PREFIX = bytes.fromhex("6a02890500010001")
AIRDROP_PREFIX = bytes.fromhex("6a02890500010000")

# An observed AirDrop-frame payload. Session B saw `346508dc895b`, `000000000000` and
# `79fc3b9b7f98` in the same slot across takes, so all-zeros is real but is NOT the only
# real form and must not be treated as "the" value.
ZERO_PAYLOAD = bytes(6)


def crc_a(data: bytes) -> bytes:
    """ISO/IEC 14443-A CRC ("CRC_A"), little-endian, as appended to 14443-A frames.

    Polynomial 0x1021 reflected (0x8408), initial value 0x6363. Returned low byte first,
    matching the on-air order.
    """
    crc = 0x6363
    for byte in data:
        b = byte ^ (crc & 0xFF)
        b = (b ^ (b << 4)) & 0xFF
        crc = ((crc >> 8) ^ (b << 8) ^ (b << 3) ^ (b >> 4)) & 0xFFFF
    return bytes((crc & 0xFF, (crc >> 8) & 0xFF))


def parse_mac(mac: str | bytes) -> bytes:
    """Normalize a BLE MAC ('de:ad:be:ef:69:69', 'deadbeef6969', or 6 raw bytes) to bytes."""
    if isinstance(mac, bytes):
        if len(mac) != 6:
            raise ValueError(f"BLE MAC must be 6 bytes, got {len(mac)}")
        return mac
    cleaned = mac.replace(":", "").replace("-", "").replace(" ", "")
    raw = bytes.fromhex(cleaned)
    if len(raw) != 6:
        raise ValueError(f"BLE MAC must be 6 bytes, got {len(raw)} from {mac!r}")
    return raw


def build_ecp_frame(tci: bytes, data: bytes, *, append_crc: bool = True) -> bytes:
    """Return a raw ECP v2 frame for an arbitrary TCI and payload.

    The config byte is COMPUTED from the payload length rather than passed in, because that
    is what it is: `[flags][len(tci) + len(data)]`. Hand-supplying it is how the config-byte
    sweep wasted a session on a value that cannot vary independently.

    Raises ValueError if the payload will not fit the 4-bit length nibble.
    """
    length = len(tci) + len(data)
    if not 0 <= length <= 0x0F:
        raise ValueError(
            f"ECP payload is {length} bytes; the config byte's length nibble holds 0-15"
        )
    body = bytes(
        (ECP_START, ECP_VERSION, (ECP_FLAGS << 4) | length, ECP_TYPE_AIRDROP, ECP_SUBTYPE)
    ) + bytes(tci) + bytes(data)
    return body + crc_a(body) if append_crc else body


def build_airdrop_ecp(
    payload: str | bytes, *, append_crc: bool = True, reverse_payload: bool = False
) -> bytes:
    """Return the AirDrop-TCI ECP frame (TCI `01 00 00`) carrying `payload` (6 bytes).

    WHY THIS EXISTS. The census of the Session B captures
    (`evidence take session-b-20260802 (ecp-frame-census.txt)`) puts this frame at **4/4 in
    bump positives and 0/2 in negatives**, and it lands immediately before the Apple AID
    select every single time. A lone iPhone emits NameDrop frames indefinitely and never
    produces one — it takes a second device. Whatever else the bump needs, an emitter that
    sends only the NameDrop frame is not replaying what a real peer sends.

    `payload` takes the same forms as a MAC ('de:ad:…', 'deadbeef6969', 6 raw bytes) but is
    NOT known to be an address: the address-type bits rule out a plaintext BLE address in
    either byte order (the project notes, step 1a). It is called a payload here on purpose. Observed
    values vary per take and include all-zeros; pass `ZERO_PAYLOAD` for that form.

    `reverse_payload` mirrors `build_namedrop_ecp(reverse_mac=...)` so a take can hold the
    two frames in the same byte order. Both orders are hardware-tested for the NameDrop
    frame and produce the identical negative — this is here for consistency, not because
    either order is favoured.
    """
    raw = parse_mac(payload)
    return build_ecp_frame(
        AIRDROP_TCI, raw[::-1] if reverse_payload else raw, append_crc=append_crc
    )


def build_namedrop_ecp(
    ble_mac: str | bytes, *, append_crc: bool = True, reverse_mac: bool = False
) -> bytes:
    """Return the NameDrop ECP polling frame for the given BLE MAC.

    The MAC must match the address ble_trigger.py advertises from, or the iPhone will
    look for a Bluetooth peer that never answers.

    `reverse_mac` is an UNRESOLVED A/B, not a preference. We emit the address in display
    order (MSB first) because kormax's example reads `deadbeef6969` — but BLE addresses go
    on air LSB-first, and ble_trigger.mac_to_octets() reverses for exactly that reason.
    Upstream never states the order, and its own example MAC is a dummy that nothing ever
    answered, so "the warp fired" does NOT confirm it: the phone parses the frame before it
    hunts the address. If we have it backwards the phone hunts a MAC nobody advertises,
    which is indistinguishable from our five 2026-07-21 negatives (warp, then iOS's "Keep
    Holding Nearby to Share" forever, and zero LE connections to our MAC).

    Set append_crc=False if the NFC frontend computes CRC_A itself (PN532 can, depending
    on TX framing config). Confirmed on hardware 2026-07-08: we append CRC_A ourselves
    (append_crc=True) and a live iPhone accepted the frame.
    """
    mac = parse_mac(ble_mac)
    return build_ecp_frame(
        NAMEDROP_TCI, mac[::-1] if reverse_mac else mac, append_crc=append_crc
    )


# --- Hardware emission (PN532, HARDWARE-PROVEN 2026-07-08) ------------------------------
# Driving a PN532 to emit a raw ECP poll, replicating kormax's proven nfcpy example
# (apple-enhanced-contactless-polling/examples/implementations/nfcpy), which was verified
# against a real iPhone 14 Pro Max on iOS 17. The register dance is minimal:
#
#   1. Open the PN532 over UART (nfcpy connstring "tty:USB0:pn532" for /dev/ttyUSB0).
#   2. Each cycle: energize the field with a normal NFC-A poll (sense_tta). A real tap
#      leaves the field ON, which is what InCommunicateThru transmits over.
#   3. rf_configuration(0x05, [0xff,0x01,0x00]) -> MaxRetries: disable auto-retry so the
#      poll's retry timing doesn't step on our broadcast.
#   4. write_register("CIU_BitFraming", 0x00) -> transmit whole bytes (no partial-bit TX).
#   5. in_communicate_thru(frame, timeout=0.1) -> raw TX of our exact bytes. A timeout
#      (Chipset.Error errno 0x01) is the EXPECTED result: an ECP broadcast is one-way, the
#      phone reacts over BLE/AWDL, not by answering the NFC frame. Any other errno is real.
#
# CRC: InCommunicateThru in this path does NOT append CRC_A itself (kormax appends it in
# software, and their crc16a == our crc_a byte-for-byte), so we send frames built with
# append_crc=True. This resolves the "who computes CRC_A" question flagged in docs/ecp-frame.md.
#
# nfcpy is an optional extra (`pip install -e .[nfc]`); imported lazily so the pure frame
# builder above stays dependency-free and unit-testable without hardware.

DEFAULT_PN532_DEVICE = "tty:USB0:pn532"  # /dev/ttyUSB0 via a CP2102 (cp210x); see docs/hardware.md


def emit_loop(
    ble_mac: str | bytes,
    *,
    device: str = DEFAULT_PN532_DEVICE,
    interval: float = 0.1,
    append_crc: bool = True,
    continuous_field: bool = False,
    reverse_mac: bool = False,
) -> None:
    """Continuously emit the NameDrop ECP frame from a PN532.

    `continuous_field=True` raises the RF field once and then only re-sends the frame, on the
    theory that a real reader's field never drops. It first produced NO warp (the PN532 drops
    RF once `in_communicate_thru` times out, and with no per-cycle `sense_tta` nothing re-raises
    it); forcing the field on via RFConfiguration item 0x01 fixed that, and it is now
    **HARDWARE-PROVEN (2026-07-21)**: the warp fires ONCE and stays quiet, instead of the
    infinite up-and-down loop the cycled field causes at ~10 drops/second (the phone reads that
    as tap-untap-tap and restarts its state machine). Prefer this over short bursts.

    It did NOT, however, change the outcome: iOS still lands on "Keep Holding Nearby to Share"
    and no transfer follows, so field cycling was never what blocked the bump.

    Runs until interrupted (Ctrl-C). `device` is an nfcpy connstring — the CP2102 UART
    adapter enumerates as /dev/ttyUSB0, i.e. "tty:USB0:pn532". `interval` is the pause
    between polling cycles (seconds); ~0.1 s keeps the frame in front of a tapped phone.

    The emitted MAC MUST match the address ble_trigger.advertise() advertises from, or the
    iPhone will hunt a Bluetooth peer that never answers. Raises RuntimeError if the PN532
    can't be opened, and ImportError (with install hint) if nfcpy isn't present.
    """
    try:
        import nfc  # nfcpy — optional [nfc] extra
        from nfc.clf import RemoteTarget
        from nfc.clf.pn53x import Chipset
    except ImportError as e:  # pragma: no cover - exercised only on the radio box
        raise ImportError(
            "PN532 emission needs nfcpy: `pip install -e .[nfc]` (or `pip install nfcpy`)."
        ) from e

    frame = build_namedrop_ecp(ble_mac, append_crc=append_crc, reverse_mac=reverse_mac)

    clf = nfc.ContactlessFrontend()
    if not clf.open(device):
        raise RuntimeError(
            f"could not open PN532 at {device!r}. Check wiring/DIP=HSU and "
            f"`nfc-list -v` first (docs/hardware.md)."
        )
    try:
        chipset = clf.device.chipset
        if not isinstance(chipset, Chipset):  # pragma: no cover - non-PN53x frontend
            raise RuntimeError(
                f"ECP broadcast needs a PN53x chipset; got {type(chipset).__name__}."
            )
        def energize() -> None:
            clf.device.mute()  # drop the field for a clean start
            try:
                clf.device.sense_tta(RemoteTarget("106A"))  # energize + poll (result unused)
            except Exception:
                pass  # no tag / transient poll error is fine; we only need the field live
            chipset.rf_configuration(0x05, [0xFF, 0x01, 0x00])  # MaxRetries: off
            chipset.write_register("CIU_BitFraming", 0x00)       # whole-byte TX

        if continuous_field:
            energize()  # ONCE — then hold the field up and just re-send the frame

        while True:  # pragma: no cover - hardware loop
            if continuous_field:
                # RFConfiguration item 0x01 = RF field: auto-RFCA off, field ON. Without this
                # the field dies after the first timed-out in_communicate_thru and no warp
                # ever fires.
                chipset.rf_configuration(0x01, [0x01])
            else:
                energize()  # default: drop + re-raise the field every cycle
            try:
                chipset.in_communicate_thru(frame, timeout=0.1)  # raw broadcast
            except Chipset.Error as e:
                if e.errno != 0x01:  # 0x01 == timeout == expected (one-way frame)
                    raise
            time.sleep(interval)
    finally:
        clf.close()


if __name__ == "__main__":  # tiny manual check: print the frame for a sample MAC
    print(build_namedrop_ecp("de:ad:be:ef:69:69").hex())
