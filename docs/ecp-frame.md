# Enhanced Contactless Polling (ECP)

Source: [kormax/apple-enhanced-contactless-polling](https://github.com/kormax/apple-enhanced-contactless-polling).

ECP is Apple's extension to ISO 14443-A polling. A reader broadcasts an extra frame during
polling, before any card is selected, telling nearby Apple devices what kind of field this is
(Apple Pay, transit, AirDrop, NameDrop...). For NameDrop, it is what makes iOS treat our card
as a peer and select the boop AID instead of reading it as an NFC tag (most likely; see
[the README](../README.md#the-ecp-line-is-the-least-isolated-claim-here)).

## Frame layout

```
 6a   02   89   05   00   01 00 01   <6-byte payload>   <CRC-A>
 │    │    │    │    │    │          │                  │
 │    │    │    │    │    │          │                  └ ISO 14443-A CRC_A
 │    │    │    │    │    │          └ payload (see below)
 │    │    │    │    │    └ TCI (Terminal Capabilities Identifier)
 │    │    │    │    └ subtype
 │    │    │    └ type 05 = AirDrop (NameDrop is a special case of it)
 │    │    └ config
 │    └ version 02 (ECP v2)
 └ header 6a
```

## The two frames

| Frame | Config | TCI | Payload |
|---|---|---|---|
| NameDrop | `89` | `01 00 01` | 6 bytes |
| AirDrop | `89` | `01 00 00` | six `00` bytes (CRC `95 25`) |

An unlocked iPhone sends the NameDrop frame on its own every few seconds, with a payload that
changes each time. It looks like a BLE address but is not one, and what it encodes is unknown.

**The TCI is what tells the two frames apart, not the config byte.** Config is
`[flags nibble][payload length nibble]`, and the length is TCI (3) + payload (6) = 9 for both,
so both are `89`. Changing config to `19` made no difference on hardware.

## What we send

- **Proxmark3** (`firmware/hf_namedrop.c`): waits for the iPhone's NameDrop frame, then sends
  NameDrop frames carrying the iPhone's own payload, then AirDrop frames, then drops its field
  so the iPhone can select it as a card.
- **Android** (`EcpEmitter`): alternates NameDrop and AirDrop frames, 500 ms on and 2 s off.
  The NameDrop payload is `c0 ff` plus 4 random bytes, new each burst, because iOS
  ignores a repeated frame.

**CRC:** the Proxmark3 appends CRC_A itself (`AddCrc14A`). On Android the NFC controller
appends it, so `EcpEmitter` passes the 14-byte frame without it.

A PN532 can send these frames too, and the iPhone shows the warp animation, but it can never be
selected as a card, so the bump stops there. See [hardware.md](hardware.md).
