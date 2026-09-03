"""`namedrop` command-line entry point.

Two pure-logic helpers that need no hardware: `ecp` prints the bump frame for a MAC, and
`vcard` prints a sample contact payload. The live NameDrop path is the Proxmark3 standalone
mode plus the two receiver processes -- see the README.
"""
from __future__ import annotations

import argparse
import sys

from . import nfc_ecp
from .contact import Contact


def _cmd_ecp(args: argparse.Namespace) -> int:
    """Print an ECP frame for a BLE MAC / payload (no hardware needed)."""
    build = (nfc_ecp.build_airdrop_ecp if args.frame == "airdrop"
             else nfc_ecp.build_namedrop_ecp)
    frame = build(args.mac, append_crc=not args.no_crc,
                  **({"reverse_payload": args.reverse_mac} if args.frame == "airdrop"
                     else {"reverse_mac": args.reverse_mac}))
    print(frame.hex())
    return 0


def _cmd_vcard(args: argparse.Namespace) -> int:
    """Emit a sample vCard so you can eyeball the payload format."""
    c = Contact(
        first_name=args.first,
        last_name=args.last,
        organization=args.org,
        phones=[args.phone] if args.phone else [],
        emails=[args.email] if args.email else [],
    )
    sys.stdout.write(c.to_vcard())
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="namedrop", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("ecp", help="build the NameDrop ECP frame for a BLE MAC")
    e.add_argument("mac", help="BLE MAC, e.g. de:ad:be:ef:69:69")
    e.add_argument("--no-crc", action="store_true", help="omit CRC_A (frontend adds it)")
    e.add_argument("--frame", choices=["namedrop", "airdrop"], default="namedrop",
                   help="which ECP frame to build. namedrop = TCI 010001; airdrop = TCI "
                        "010000, the frame that is 4/4 in bump positives and 0/2 in "
                        "negatives and immediately precedes every SELECT (see the census "
                        "in evidence take session-b-20260802).")
    e.add_argument("--reverse-mac", action="store_true", help="A/B: put the MAC in the frame LSB-first (BLE on-air order) instead of display order. UNRESOLVED — kormax never states the order and only ever tested a dummy MAC, so the warp firing proves nothing about it.")
    e.set_defaults(func=_cmd_ecp)

    v = sub.add_parser("vcard", help="print a sample vCard payload")
    v.add_argument("--first", default="Ada")
    v.add_argument("--last", default="Lovelace")
    v.add_argument("--org", default="")
    v.add_argument("--phone", default="+15551234567")
    v.add_argument("--email", default="ada@example.com")
    v.set_defaults(func=_cmd_vcard)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
