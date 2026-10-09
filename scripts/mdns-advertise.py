#!/usr/bin/env python3
"""Advertise us on awdl0 so a bump can find us.

After the NFC handshake iOS resolves `<bonjourListenerUUID>._asquic._udp`, and this is what publishes that
record (from snap-identity.json, so it matches the UUID the card handed over). Without it the
bump completes at the NFC layer and then has nowhere to go.

Run it alongside scripts/asquic-receiver.py, which serves the QUIC/HTTP-3 the bump routes to.

It also runs OpenDrop's stock AirDrop receiver on the legacy `_airdrop._tcp`/HTTPS path.

Run (needs owl up on awdl0):
    sudo ./scripts/awdl-up.sh            # bring up awdl0 (AR9271, ch6)
    .venv/bin/python scripts/mdns-advertise.py -i awdl0
"""
import argparse
import logging
import os
import threading
import time

from opendrop import server as od_server
from opendrop.config import AirDropConfig

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Must match asquic-receiver.py's --port. 60192 is the port a real iPhone advertised.
ASQUIC_PORT = 60192

# Seconds between mDNS re-announcements; see _start_repeat_announcer.
ANNOUNCE_INTERVAL = 2.0

# Advertised receiver flags: macOS's 0x3fb minus SUPPORTS_DVZIP (0x02), so legacy senders use
# cpio /Upload (which OpenDrop can extract) instead of dvzip (which it 406s).
FLAGS = 0x3F9


def _describe_service(srv):
    """Log exactly what we advertise, so we can confirm the mDNS record went out
    awdl0 (name, scoped address, port, flags) instead of guessing."""
    si = srv.service_info
    try:
        addrs = si.parsed_addresses()
    except Exception:
        addrs = ["<unparsed>"]
    logging.info("ADVERTISING _airdrop._tcp:")
    logging.info("  service name : %s", si.name)
    logging.info("  server       : %s", si.server)
    logging.info("  addresses    : %s  (scope = %%%s on the wire)", addrs, srv.config.interface)
    logging.info("  port         : %s", si.port)
    logging.info("  properties   : %s", si.properties)
    logging.info("  flags        : 0x%x", srv.config.flags)


def _default_host_name():
    """A real iPhone's AWDL hostname is a UUID, and its _airdrop._tcp SRV points at it.

    Measured in evidence take awdl-real-bump-20260813-232720-sharesheet: the receiving
    iPhone advertised `<12 hex digits>._airdrop._tcp.local` with SRV target `<uuid>.local`,
    and the sender resolved exactly that host. Ours advertised the machine's own hostname --
    the one shape that did not match.

    Leading (unproven) hypothesis: that hostname is the SNAP bonjourListenerUUID we hand
    the phone over NFC, which is how the sender knows which mDNS peer is the bumped one.
    So default to the very UUID baked into the PM3's SNAP blob.
    """
    ident = os.path.join(REPO, "scratchpad", "snap-identity.json")
    try:
        import json

        with open(ident) as fh:
            return json.load(fh)["bonjour_listener_uuid"]
    except Exception as exc:  # no identity minted yet -- fall back to the real hostname
        logging.warning("no SNAP identity at %s (%s); using the system hostname", ident, exc)
        return None


def _register_asquic(srv, host_name, port):
    """Advertise `<listenerUUID>._asquic._udp` the way a real device does.

    Ground truth (evidence take snap-uuid-is-asquic-instance-20260814): the UUID an iPhone hands
    over NFC as SNAP key 2 is the name of its `_asquic._udp` SERVICE INSTANCE -- not its
    `_airdrop._tcp` SRV hostname, which is a different UUID entirely. We had been stamping our
    listener UUID onto the SRV hostname only, i.e. in the one place the ground truth says it does
    not go. This publishes it where a real device publishes it, ALONGSIDE the hostname we already
    use, so we match under either reading of the bind.

    Mirrors the captured announcement exactly: UPPERCASE instance name, EMPTY TXT, SRV target =
    our hostname, priority/weight 0.
    """
    from zeroconf import ServiceInfo

    if not host_name:
        logging.warning("no UUID host name -- skipping _asquic (nothing to name the instance)")
        return None
    instance = host_name.upper()
    # The same awdl0 link-local address opendrop publishes for _airdrop._tcp.
    addresses = [srv.ip_addr.packed]
    try:
        info = ServiceInfo(
            "_asquic._udp.local.",
            "%s._asquic._udp.local." % instance,
            addresses=addresses,
            port=port,
            properties={},  # the real one carries a single zero-length TXT string
            server="%s.local." % host_name,
        )
        srv.zeroconf.register_service(info)
    except Exception as exc:
        logging.error("could not register _asquic instance: %s", exc)
        return None
    logging.info("ADVERTISING _asquic._udp:")
    logging.info("  instance     : %s._asquic._udp.local.", instance)
    logging.info("  server       : %s.local.  port %d  TXT empty", host_name, port)
    logging.info("  address      : %s", srv.ip_addr)
    logging.info("  ^ this is where a real iPhone puts the NFC bonjourListenerUUID")
    return info


def _start_repeat_announcer(srv, infos, interval):
    """Re-announce our mDNS records on a timer instead of answering each browse ONCE.

    Why: measured 2026-08-14 (evidence take pm3-dedup-arm{A,B,C}-*). A bump only completes if the
    iPhone actually receives our `_asquic` SRV, and the wire says which takes it did. mDNS
    Known-Answer Suppression makes a querier list records it already holds, so the phone's own
    queries report whether it has ours:

        arm A (stall)   50 phone queries,  0 carrying our record
        arm B (SUCCESS) 32 phone queries, 14 carrying our record
        arm C (stall)   30 phone queries,  0 carrying our record

    0/80 across the failing arms: the phone never held the record, i.e. our answers were not
    arriving -- NOT that it saw us and declined (that is what killed the "iOS dedups on the
    listener UUID" reading). We inject through a passive monitor vif, so there is no L2 ACK and no
    retransmission for anything we send; a one-shot multicast answer fired while the phone is
    inside an AWDL power-save window is simply lost. Repetition is our only retransmit.
    """
    infos = [i for i in infos if i is not None]
    if not infos:
        return None

    def loop():
        n = 0
        while True:
            time.sleep(interval)
            n += 1
            for info in infos:
                try:
                    srv.zeroconf.update_service(info)
                except Exception as exc:  # a re-announce failing must never kill the receiver
                    logging.debug("re-announce failed for %s: %s", info.name, exc)
            if n % 30 == 0:
                logging.info("repeat-announcer: %d rounds, %d service(s) per round", n, len(infos))

    t = threading.Thread(target=loop, name="repeat-announcer", daemon=True)
    t.start()
    logging.info("REPEAT-ANNOUNCER: every %.1fs for %d service(s)", interval, len(infos))
    return t


def run_real(interface, name, host_name):
    # OpenDrop lands received files in cwd
    outdir = os.path.join(REPO, "received")
    os.makedirs(outdir, exist_ok=True)
    os.chdir(outdir)

    config = AirDropConfig(
        interface=interface,
        computer_name=name,
        host_name=host_name,
        debug=True,
    )
    if host_name:
        logging.info("advertising SRV target %s.local (a real iPhone uses a UUID here)", host_name)
    config.flags = FLAGS
    srv = od_server.AirDropServer(config)

    srv.start_service()
    _describe_service(srv)
    asquic_info = _register_asquic(srv, host_name, ASQUIC_PORT)
    _start_repeat_announcer(
        srv, [asquic_info, getattr(srv, "service_info", None)], ANNOUNCE_INTERVAL
    )
    print(
        'RECEIVING as "%s" on %s. AirDrop a file TO it from Mac / Pixel / iPad. Ctrl+C to stop.'
        % (name, interface),
        flush=True,
    )
    try:
        srv.start_server()
    except KeyboardInterrupt:
        srv.stop()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("-i", "--interface", default="awdl0")
    ap.add_argument("-n", "--name", default="namedrop-re")
    ap.add_argument("-v", "--verbose", action="store_true", help="DEBUG-level logging")
    args = ap.parse_args()

    # configure logging so OpenDrop's own logger (zeroconf announce, mDNS errors,
    # TLS identity) is actually visible -- otherwise we run blind.
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    run_real(args.interface, args.name, _default_host_name())
