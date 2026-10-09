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
import ipaddress
import logging
import os
import socket
import threading
import time

from opendrop import server as od_server
from opendrop.config import AirDropConfig


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
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ident = os.path.join(repo, "scratchpad", "snap-identity.json")
    try:
        import json

        with open(ident) as fh:
            return json.load(fh)["bonjour_listener_uuid"]
    except Exception as exc:  # no identity minted yet -- fall back to the real hostname
        logging.warning("no SNAP identity at %s (%s); using the system hostname", ident, exc)
        return None


def _iface_packed_addresses(interface, fallback):
    """Every address on `interface`, packed for zeroconf, IPv4 first.

    opendrop hardcodes ipv6=True and takes the FIRST IPv6 it finds (server.py:51). On awdl0 that
    is the only sensible answer -- there is no IPv4 there. On an infrastructure link it picks the
    LINK-LOCAL fe80::, while a real iPhone on that same link publishes its IPv4: measured
    2026-08-15 on the Pixel hotspot, `<UUID>._asquic._udp` -> `<name>.local` -> a private
    IPv4 address, empty TXT. Publishing only fe80:: would ask the phone to route somewhere it
    does not advertise itself, and a bump that fails for that reason is indistinguishable on the
    wire from one iOS ignored.

    So advertise every address the interface actually has and let the phone choose. IPv4 first
    because that is the family the iPhone publishes here; zeroconf emits an A and a AAAA either
    way. Falls back to opendrop's single choice if the interface cannot be read.
    """
    try:
        import ifaddr
    except ImportError:
        return [fallback.packed]

    v4, v6 = [], []
    for adapter in ifaddr.get_adapters():
        if adapter.name != interface:
            continue
        for ip in adapter.ips:
            try:
                if ip.is_IPv4:
                    v4.append(ipaddress.IPv4Address(ip.ip).packed)
                else:
                    # ip.ip is (addr, flowinfo, scope_id); the record carries the bare address
                    v6.append(ipaddress.IPv6Address(ip.ip[0]).packed)
            except (ipaddress.AddressValueError, ValueError, TypeError):
                continue
    packed = v4 + v6
    return packed or [fallback.packed]


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
    addresses = _iface_packed_addresses(srv.config.interface, srv.ip_addr)
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
    logging.info(
        "  addresses    : %s",
        ", ".join(str(ipaddress.ip_address(a)) for a in addresses),
    )
    logging.info("  ^ this is where a real iPhone puts the NFC bonjourListenerUUID")
    return info


def _start_repeat_announcer(srv, infos, interval, unicast_addr=None):
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

    Multicast repetition is the fix with the real rationale. The unicast copy is opportunistic --
    it dodges multicast-specific AWDL scheduling -- and is NOT a second L2-retry path; there is no
    such thing on this rig for either mode.
    """
    if interval <= 0:
        logging.info("repeat-announcer DISABLED (--announce-interval 0) -- one-shot answers only")
        return None

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
                if unicast_addr:
                    try:
                        _unicast_records(srv, info, unicast_addr)
                    except Exception as exc:
                        # Loud on the FIRST failure, then quiet. A silently swallowed send is how
                        # a dead leg gets mistaken for a tested one -- it already cost a restart.
                        if not getattr(loop, "_warned", False):
                            loop._warned = True
                            logging.warning("unicast announce FAILING (%s: %s) -- multicast only",
                                            type(exc).__name__, exc)
            if n % 30 == 0:
                logging.info("repeat-announcer: %d rounds, %d service(s) per round", n, len(infos))

    t = threading.Thread(target=loop, name="repeat-announcer", daemon=True)
    t.start()
    logging.info(
        "REPEAT-ANNOUNCER: every %.1fs for %d service(s)%s",
        interval, len(infos),
        (" + unicast to %s" % unicast_addr) if unicast_addr else "",
    )
    return t


_UNICAST_SOCK = {}  # address family -> socket bound to 5353 in that family


def _unicast_records(srv, info, addr):
    """Send this service's records straight to one peer, alongside the multicast announcement.

    We do the sendto ourselves rather than calling Zeroconf.send(out, addr=...): its transports are
    bound for the multicast group and the unicast copy never leaves the box (measured -- 0 frames
    on the wire, while a plain UDP datagram to the same address goes out fine).
    """
    global _UNICAST_SOCK
    from zeroconf import DNSOutgoing
    from zeroconf.const import _FLAGS_AA, _FLAGS_QR_RESPONSE

    out = DNSOutgoing(_FLAGS_QR_RESPONSE | _FLAGS_AA, multicast=False)
    for rec in (info.dns_pointer(), info.dns_service(), info.dns_text(), *info.dns_addresses()):
        out.add_answer_at_time(rec, 0)

    # Family follows the TARGET. On awdl0 the peer is an fe80:: address so this was AF_INET6-only;
    # on an infrastructure link the iPhone publishes an IPv4 (measured), and
    # getaddrinfo(v4_literal, AF_INET6) RAISES -- which, inside the announcer thread, would surface
    # as "unicast announce FAILING ... multicast only" and quietly void the whole experiment.
    family = socket.AF_INET6 if ":" in addr else socket.AF_INET
    sock = _UNICAST_SOCK.get(family)
    if sock is None:
        # MUST send FROM 5353. RFC 6762 6.7: an mDNS response arriving from an ephemeral source
        # port is a "legacy unicast" reply and a conforming responder discards it. Measured
        # 2026-08-14: our first cut sent from 34607 and all 57 unicast announcements in that take
        # were dead on arrival -- they were on the wire, which is exactly what makes it a trap.
        # SO_REUSEPORT because zeroconf already holds 5353 for the multicast socket.
        sock = socket.socket(family, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        sock.bind(("::" if family == socket.AF_INET6 else "", 5353))
        _UNICAST_SOCK[family] = sock
    sockaddr = socket.getaddrinfo(addr, 5353, family, socket.SOCK_DGRAM)[0][4]
    for pkt in out.packets():
        sock.sendto(pkt, sockaddr)


def run_real(interface, name, model, outdir, flags, host_name, caps=None):
    # OpenDrop lands received files in cwd
    os.makedirs(outdir, exist_ok=True)
    os.chdir(outdir)

    config = AirDropConfig(
        interface=interface,
        computer_name=name,
        computer_model=model,
        host_name=host_name,
        debug=True,
    )
    if host_name:
        logging.info("advertising SRV target %s.local (a real iPhone uses a UUID here)", host_name)
    if flags is not None:
        logging.info("overriding advertised flags 0x%x -> 0x%x", config.flags, flags)
        config.flags = flags

    # /Discover- and /Ask-response capability fields (see the server.py patch). These are read
    # off self.config by the patched handler, so setting them here is the whole wiring.
    for attr, value in (caps or {}).items():
        setattr(config, attr, value)
    dsf = getattr(config, "device_support_flags", 0x1B3FB)
    logging.info(
        "response capabilities: IsAirDropable=%r  DeviceSupportFlags=%s  SupportsContactExchange=%r",
        getattr(config, "is_airdropable", True),
        "omitted" if dsf is None else "0x%x" % dsf,
        getattr(config, "supports_contact_exchange", False),
    )
    print("received files -> %s" % outdir, flush=True)
    srv = od_server.AirDropServer(config)

    # opendrop builds its Zeroconf as ip_version=V6Only bound to the interface's first IPv6
    # (server.py:66). On awdl0 that is the only choice. On an infrastructure link it means we
    # announce ONLY to ff02::fb and, worse, we never HEAR a query sent to 224.0.0.251 -- so if the
    # iPhone browses over IPv4 we simply never answer, and "the phone never saw us" is
    # indistinguishable from "the link is fine but we were deaf". The phone publishes its own
    # _asquic record with an IPv4 address on this link, so IPv4 mDNS is live here. Go dual-stack
    # whenever the interface has both families; awdl0 has no IPv4, so that arm is untouched.
    _addrs = [str(ipaddress.ip_address(a)) for a in _iface_packed_addresses(config.interface, srv.ip_addr)]
    if len(_addrs) > 1:
        from zeroconf import IPVersion, Zeroconf

        try:
            srv.zeroconf.close()
            srv.zeroconf = Zeroconf(interfaces=_addrs, ip_version=IPVersion.All)
            logging.info("mDNS rebound DUAL-STACK on %s", ", ".join(_addrs))
        except Exception as exc:
            srv.zeroconf = Zeroconf(
                interfaces=[str(srv.ip_addr)], ip_version=IPVersion.V6Only
            )
            logging.warning("dual-stack mDNS failed (%s) -- back to V6Only", exc)

    # Same fix as _iface_packed_addresses, applied to opendrop's own _airdrop._tcp record before
    # it is announced: server.py:86 advertises only self.ip_addr, which on an infrastructure link
    # is the fe80::. The HTTPS server itself binds ("::", port) and bindv6only=0 here, so it
    # already accepts IPv4 -- only the RECORD was IPv6-only. Rebuilt rather than patched in the
    # submodule so the AWDL arm (where the interface has no IPv4) is bit-identical to before.
    _extra = _iface_packed_addresses(config.interface, srv.ip_addr)
    if len(_extra) > 1:
        from zeroconf import ServiceInfo as _SI

        old = srv.service_info
        srv.service_info = _SI(
            "_airdrop._tcp.local.",
            old.name,
            port=old.port,
            properties=old.properties,
            server=old.server,
            addresses=_extra,
        )
        logging.info(
            "_airdrop._tcp addresses: %s",
            ", ".join(str(ipaddress.ip_address(a)) for a in _extra),
        )

    srv.start_service()
    _describe_service(srv)
    asquic_info = None
    if caps is None or caps.get("asquic", True):
        asquic_info = _register_asquic(srv, host_name, (caps or {}).get("asquic_port", 60192))
    _start_repeat_announcer(
        srv,
        [asquic_info, getattr(srv, "service_info", None)],
        (caps or {}).get("announce_interval", 2.0),
        (caps or {}).get("announce_unicast"),
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
    ap.add_argument("-m", "--model", default=None)
    ap.add_argument("-o", "--outdir", default=os.path.abspath("./captures"))
    ap.add_argument(
        "--flags",
        default="0x3f9",
        help="advertised receiver flags (hex). default 0x3f9 = macOS 0x3fb minus "
        "SUPPORTS_DVZIP(0x02), so senders use cpio /Upload (which OpenDrop can "
        "extract) instead of dvzip (which it 406s). Pass 0x3fb to capture the "
        "modern dvzip /Upload, or 0x88 for stock-OpenDrop discovery.",
    )
    ap.add_argument(
        "--host-name",
        default=None,
        help="mDNS SRV target (without .local). Defaults to the bonjourListenerUUID in "
        "snap-identity.json -- the UUID the PM3 hands the phone over NFC -- "
        "because a real iPhone's AirDrop SRV points at a UUID hostname. Pass 'system' to "
        "use the machine's own hostname instead (the pre-2026-08-13 behaviour).",
    )
    ap.add_argument(
        "--device-support-flags",
        default="0x1b3fb",
        help="DeviceSupportFlags returned in the /Discover response (hex), the same bitfield "
        "family as --flags. Default 0x1b3fb is what a real iPhone sends US in every /Discover "
        "request (4/4 captures); its low 10 bits are 0x3fb, the documented macOS "
        "defaultSFNodeFlags. Pass 'omit' for the stock-OpenDrop control arm.",
    )
    ap.add_argument(
        "--no-airdropable",
        action="store_true",
        help="omit IsAirDropable from the /Discover response (control arm). By default we "
        "return IsAirDropable=true, which the sender parses and logs.",
    )
    ap.add_argument(
        "--supports-contact-exchange",
        action="store_true",
        help="return SupportsContactExchange=true in the /Ask response. sharingd's send state "
        "machine branches CONTACTS START vs CONTACTS SKIPPED on this. OFF by default: the "
        "one-way milestone does not want a CONTACTS stage we cannot serve yet.",
    )
    ap.add_argument(
        "--no-asquic",
        action="store_true",
        help="do NOT advertise <listenerUUID>._asquic._udp (control arm). By default we do, "
        "because that is where a real iPhone puts the UUID it hands over NFC -- see "
        "evidence take snap-uuid-is-asquic-instance-20260814.",
    )
    ap.add_argument(
        "--asquic-port",
        type=int,
        default=60192,
        help="SRV port for the _asquic instance (default 60192, the captured value). Arbitrary: "
        "we publish the record for the bind, we do not serve QUIC on it.",
    )
    ap.add_argument(
        "--announce-interval",
        type=float,
        default=2.0,
        help="seconds between mDNS re-announcements of our records (default 2.0; 0 disables, "
        "which restores the one-shot behaviour and is the control arm). Measured 2026-08-14: in "
        "the two takes that stalled the iPhone never once carried our record in Known-Answer "
        "Suppression (0/80 queries) while the take that COMPLETED carried it 14/32 -- i.e. our "
        "one-shot answers were being lost, not ignored. We inject via a passive monitor vif, so "
        "repetition is the only retransmit we have.",
    )
    ap.add_argument(
        "--announce-unicast",
        default=None,
        metavar="ADDR",
        help="also send our records unicast to this peer (e.g. the iPhone's awdl0 link-local, "
        "'fe80::...%%awdl0') alongside the multicast announcement. Opportunistic: it dodges "
        "multicast-specific AWDL scheduling. It is NOT an extra L2-retry path -- monitor-mode "
        "injection has none for unicast either.",
    )
    ap.add_argument("-v", "--verbose", action="store_true", help="DEBUG-level logging")
    args = ap.parse_args()

    # configure logging so OpenDrop's own logger (zeroconf announce, mDNS errors,
    # TLS identity) is actually visible -- otherwise we run blind.
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    flags = int(args.flags, 0) if args.flags else None
    if args.host_name == "system":
        host_name = None
    elif args.host_name:
        host_name = args.host_name
    else:
        host_name = _default_host_name()
    dsf = args.device_support_flags
    caps = {
        "is_airdropable": None if args.no_airdropable else True,
        "device_support_flags": (
            None if dsf is None or dsf.lower() == "omit" else int(dsf, 0)
        ),
        "supports_contact_exchange": args.supports_contact_exchange,
        "asquic": not args.no_asquic,
        "asquic_port": args.asquic_port,
        "announce_interval": args.announce_interval,
        "announce_unicast": args.announce_unicast,
    }
    run_real(
        args.interface, args.name, args.model, args.outdir, flags, host_name, caps
    )
