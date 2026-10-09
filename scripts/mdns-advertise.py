#!/usr/bin/env python3
"""Advertise us on awdl0 so a bump can find us.

After the NFC handshake iOS resolves `<bonjourListenerUUID>._asquic._udp`, and this is what publishes that
record (from snap-identity.json, so it matches the UUID the card handed over). Without it the
bump completes at the NFC layer and then has nowhere to go.

Run it alongside scripts/asquic-receiver.py, which serves the QUIC/HTTP-3 the bump routes to.

Run (needs owl up on awdl0):
    sudo ./scripts/awdl-up.sh            # bring up awdl0 (AR9271, ch6)
    .venv/bin/python scripts/mdns-advertise.py -i awdl0
"""
import argparse
import ipaddress
import json
import logging
import os
import sys
import time

import ifaddr
from zeroconf import IPVersion, ServiceInfo, Zeroconf

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Must match asquic-receiver.py's --port. 60192 is the port a real iPhone advertised.
ASQUIC_PORT = 60192

# Seconds between mDNS re-announcements; see the main loop.
ANNOUNCE_INTERVAL = 2.0


def link_local_ipv6(interface):
    """The interface's IPv6 address. awdl0 has nothing else -- no IPv4, no global prefix."""
    for adapter in ifaddr.get_adapters():
        if adapter.name == interface:
            for ip in adapter.ips:
                if ip.is_IPv6:
                    # ip.ip is (addr, flowinfo, scope_id); the record carries the bare address
                    return ipaddress.IPv6Address(ip.ip[0])
    return None


def listener_uuid():
    """The SNAP bonjourListenerUUID the card hands the phone over NFC."""
    ident = os.path.join(REPO, "scratchpad", "snap-identity.json")
    try:
        with open(ident) as fh:
            return json.load(fh)["bonjour_listener_uuid"]
    except (OSError, KeyError, ValueError) as exc:
        sys.exit("no SNAP identity at %s (%s) -- run scripts/build-snap-serverinfo.py" % (ident, exc))


def asquic_record(uuid, addr):
    """`<listenerUUID>._asquic._udp`, the way a real device publishes it.

    Ground truth (evidence take snap-uuid-is-asquic-instance-20260814): the UUID an iPhone hands
    over NFC as SNAP key 2 is the name of its `_asquic._udp` SERVICE INSTANCE.

    Mirrors the captured announcement exactly: UPPERCASE instance name, EMPTY TXT, SRV target =
    our hostname, priority/weight 0.
    """
    return ServiceInfo(
        "_asquic._udp.local.",
        "%s._asquic._udp.local." % uuid.upper(),
        addresses=[addr.packed],
        port=ASQUIC_PORT,
        properties={},  # the real one carries a single zero-length TXT string
        server="%s.local." % uuid,
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-i", "--interface", default="awdl0")
    ap.add_argument("-v", "--verbose", action="store_true", help="DEBUG-level logging")
    args = ap.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    addr = link_local_ipv6(args.interface)
    if addr is None:
        sys.exit("%s has no IPv6 address -- is owl running?" % args.interface)
    uuid = listener_uuid()

    zc = Zeroconf(interfaces=[str(addr)], ip_version=IPVersion.V6Only)
    info = asquic_record(uuid, addr)
    zc.register_service(info)
    logging.info("ADVERTISING %s", info.name)
    logging.info("  server %s  port %d  address %s  TXT empty", info.server, info.port, addr)

    # Re-announce on a timer instead of answering each browse ONCE.
    #
    # Why: measured 2026-08-14 (evidence take pm3-dedup-arm{A,B,C}-*). A bump only completes if the
    # iPhone actually receives our `_asquic` SRV, and the wire says which takes it did. mDNS
    # Known-Answer Suppression makes a querier list records it already holds, so the phone's own
    # queries report whether it has ours:
    #
    #     arm A (stall)   50 phone queries,  0 carrying our record
    #     arm B (SUCCESS) 32 phone queries, 14 carrying our record
    #     arm C (stall)   30 phone queries,  0 carrying our record
    #
    # 0/80 across the failing arms: the phone never held the record, i.e. our answers were not
    # arriving -- NOT that it saw us and declined. We inject through a passive monitor vif, so
    # there is no L2 ACK and no retransmission for anything we send; a one-shot multicast answer
    # fired while the phone is inside an AWDL power-save window is simply lost. Repetition is our
    # only retransmit.
    logging.info("re-announcing every %.1fs. Ctrl+C to stop.", ANNOUNCE_INTERVAL)
    try:
        while True:
            time.sleep(ANNOUNCE_INTERVAL)
            try:
                zc.update_service(info)
            except Exception as exc:  # a re-announce failing must never kill the advertiser
                logging.debug("re-announce failed: %s", exc)
    except KeyboardInterrupt:
        pass
    finally:
        zc.unregister_all_services()
        zc.close()


if __name__ == "__main__":
    main()
