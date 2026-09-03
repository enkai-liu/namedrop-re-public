#!/usr/bin/env python3
"""Advertise us on awdl0 so a bump can find us -- and capture the legacy HTTPS leg.

THIS IS LOAD-BEARING FOR NAMEDROP, despite the AirDrop machinery below. After the NFC
handshake iOS resolves `<bonjourListenerUUID>._asquic._udp`, and this is what publishes that
record (from snap-identity.json, so it matches the UUID the card handed over). Without it the
bump completes at the NFC layer and then has nowhere to go.

Run it alongside scripts/asquic-receiver.py, which serves the QUIC/HTTP-3 the bump routes to.

It is also a full AirDrop receiver over the legacy `_airdrop._tcp`/HTTPS path.

We are the RECEIVER, so we own the TLS private key and terminate the AWDL HTTPS
session -> we see the *plaintext* /Discover, /Ask and /Upload a working modern
sender emits. This wraps OpenDrop's own AirDropServerHandler with a transparent
tee over self.rfile: it records each request's raw plaintext body + headers to a
uniquely-named file (so Mac / Pixel / iPad captures never clobber one another),
while the underlying handler still parses/extracts exactly as stock OpenDrop.

Real run (needs owl up on awdl0):
    sudo ./scripts/awdl-up.sh            # bring up awdl0 (AR9271, ch6)
    .venv/bin/python scripts/mdns-advertise.py -i awdl0 -n "namedrop-re"
  then AirDrop a file TO "namedrop-re" from the Mac / Pixel / iPad.

Self-test (no hardware, loopback):
    .venv/bin/python scripts/mdns-advertise.py --selftest
"""
import argparse
import ipaddress
import logging
import os
import socket
import sys
import threading
import time

from opendrop import server as od_server
from opendrop.config import AirDropConfig

logger = logging.getLogger("capture")

CAP_DIR = None
_counter = [0]
_lock = threading.Lock()


class TeeReader:
    """Wrap a readable file object, appending every consumed byte to .buf.

    Transparent: the wrapped handler reads exactly as before (readline/read for
    chunked bodies, read(n) for Content-Length bodies); we just also keep a copy.
    """

    def __init__(self, inner):
        self.inner = inner
        self.buf = bytearray()

    def read(self, *a, **k):
        d = self.inner.read(*a, **k)
        if d:
            self.buf += d
        return d

    def readline(self, *a, **k):
        d = self.inner.readline(*a, **k)
        if d:
            self.buf += d
        return d

    def __getattr__(self, name):
        return getattr(self.inner, name)


def _drain_chunked(rfile):
    """Read a full HTTP chunked body into bytes (no Content-Length case).

    Raises ConnectionError if the peer closes before the terminating zero-chunk.
    An empty readline()/short read means EOF, NOT a blank line — without this guard
    a mid-stream drop (common on the lossy passive-monitor AWDL link) would spin
    this loop forever on b'', hanging the whole (non-threaded) server.
    """
    data = bytearray()
    while True:
        line = rfile.readline()
        if line == b"":  # EOF: peer closed before the 0-chunk
            raise ConnectionError(
                "chunked body truncated: connection closed before terminating 0-chunk"
            )
        size_line = line.strip()
        if not size_line:  # stray blank line between chunks
            continue
        size = int(size_line.split(b";")[0], 16)
        if size == 0:
            rfile.readline()  # trailing CRLF
            break
        chunk = rfile.read(size)
        data += chunk
        if len(chunk) < size:  # EOF part-way through a chunk
            raise ConnectionError(
                "chunked body truncated: connection closed mid-chunk (%d/%d bytes)"
                % (len(chunk), size)
            )
        rfile.readline()  # CRLF after each chunk
    return bytes(data)


def _dvzip_to_cpio(raw):
    """Reassemble a modern AirDrop `application/x-dvzip` payload into the cpio
    archive inside it. dvzip framing = a sequence of [4-byte big-endian length]
    [zlib stream] blocks; concatenating the inflated blocks yields a standard
    ODC ('070707') cpio archive. Returns the cpio bytes, or None if `raw` is not
    dvzip-framed. (namedrop-re: RE'd from a real macOS /Upload, 2026-07-08)"""
    import struct
    import zlib

    if len(raw) < 6 or raw[4:6] != b"\x78\x9c":  # 4-byte len then zlib header
        return None
    off = 0
    out = bytearray()
    try:
        while off + 4 <= len(raw):
            (ln,) = struct.unpack(">I", raw[off:off + 4])
            off += 4
            out += zlib.decompress(raw[off:off + ln])
            off += ln
    except Exception as e:
        logger.warning("dvzip reassembly failed at offset %d: %s", off, e)
        return None
    return bytes(out)


def _try_extract(raw, stamp):
    """Extract `raw` into CAP_DIR. Handles modern AirDrop dvzip (length-prefixed
    zlib blocks wrapping a cpio) by reassembling first, then hands the result to
    libarchive (auto-detects zip/cpio/tar/gz/...). Returns extracted entry names,
    or [] if it can't be read. Logs leading magic to identify unknown containers."""
    magic = raw[:8]
    logger.info("upload payload %d bytes, magic=%s (%r)", len(raw), magic.hex(), magic)
    payload = _dvzip_to_cpio(raw)
    if payload is not None:
        logger.info("dvzip -> reassembled %d-byte cpio (magic %r)", len(payload), payload[:6])
    else:
        payload = raw
    try:
        import libarchive
    except Exception as e:  # pragma: no cover
        logger.warning("libarchive unavailable: %s", e)
        return []
    names = []
    try:
        with libarchive.memory_reader(payload) as archive:
            for entry in archive:
                names.append(entry.pathname)
                if entry.isdir:
                    continue
                # write the entry out so a successfully-parsed payload lands
                try:
                    out = os.path.join(CAP_DIR, os.path.basename(entry.pathname) or (stamp + ".entry"))
                    with open(out, "wb") as f:
                        for blk in entry.get_blocks():
                            f.write(blk)
                except Exception as e:
                    logger.warning("  entry %s write failed: %s", entry.pathname, e)
        logger.info("EXTRACTED %d entrie(s): %s", len(names), names)
    except Exception as e:
        logger.warning("libarchive could not parse payload: %s", e)
    return names


def _make_capturing_handler(base):
    class CapturingHandler(base):
        def handle_upload(self):
            """Accept ANY /Upload content-type (stock OpenDrop 406s non-cpio before
            reading the body, so we never saw the dvzip bytes). We drain + save the
            full payload, try to extract it, and always answer 200 so a modern Mac
            reports success and we get a clean, complete capture to RE. (namedrop-re)"""
            ct = self.headers.get("content-type", "").lower()
            if ct == "application/x-cpio":
                return super().handle_upload()  # stock cpio path still works

            logger.info("non-cpio /Upload content-type=%s -> draining + capturing", ct)
            if self.headers.get("expect", "").lower() == "100-continue":
                self.send_response(100)
                self.send_header("Content-Length", 0)
                self.end_headers()

            # Bound the body read: on the lossy AWDL link a sender can vanish
            # mid-stream. Without a timeout the socket read blocks forever; the
            # chunked path used to busy-spin on EOF. Time-box it and fail cleanly
            # so this non-threaded server stays responsive for the next attempt.
            prev_timeout = self.connection.gettimeout()
            self.connection.settimeout(30)
            te = self.headers.get("transfer-encoding", "").lower()
            try:
                if "chunked" in te:
                    raw = _drain_chunked(self.rfile)
                else:
                    cl = self.headers.get("content-length")
                    raw = self.rfile.read(int(cl)) if cl is not None else b""
            except (socket.timeout, ConnectionError, OSError) as e:
                logger.warning(
                    "upload aborted: %s — sender likely dropped on the lossy link; "
                    "waiting for the next attempt", e
                )
                self.close_connection = True
                return
            finally:
                try:
                    self.connection.settimeout(prev_timeout)
                except OSError:
                    pass

            stamp = "%d-upload-payload" % int(time.time())
            try:
                raw_path = os.path.join(CAP_DIR, stamp + ".dvzip.raw")
                with open(raw_path, "wb") as f:
                    f.write(raw)
                logger.info("saved raw /Upload payload -> %s", os.path.basename(raw_path))
            except Exception as e:
                logger.warning("raw payload save failed: %s", e)

            _try_extract(raw, stamp)

            self.send_response(200)
            self.send_header("Content-Length", 0)
            self.send_header("Connection", "close")
            self.end_headers()

        def do_POST(self):
            with _lock:
                _counter[0] += 1
                idx = _counter[0]
            try:
                client = self.client_address[0]
            except Exception:
                client = "unknown"
            path = self.path.lstrip("/").replace("/", "_") or "root"
            # sortable, collision-free, tells senders apart by client addr
            stamp = "%d-%03d-%s-%s" % (int(time.time()), idx, path, client.replace(":", "-"))

            # 1) headers + request line straight to disk
            try:
                hdr_path = os.path.join(CAP_DIR, stamp + ".headers.txt")
                with open(hdr_path, "w") as f:
                    f.write("%s %s %s\n" % (self.command, self.path, self.request_version))
                    f.write("client=%s\n\n" % (self.client_address,))
                    f.write(str(self.headers))
            except Exception as e:
                print("!! header capture failed:", e, file=sys.stderr, flush=True)

            # 2) tee the raw plaintext body while the stock handler consumes it
            tee = TeeReader(self.rfile)
            self.rfile = tee
            try:
                super().do_POST()
            finally:
                self.rfile = tee.inner
                try:
                    body_path = os.path.join(CAP_DIR, stamp + ".body.raw")
                    with open(body_path, "wb") as f:
                        f.write(bytes(tee.buf))
                    print(
                        "captured %s  %d bytes body -> %s"
                        % (self.path, len(tee.buf), os.path.basename(body_path)),
                        flush=True,
                    )
                except Exception as e:
                    print("!! body capture failed:", e, file=sys.stderr, flush=True)

    return CapturingHandler


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
    iPhone advertised `1466011cc7c0._airdrop._tcp.local` with SRV target
    `cade955f-7ce4-477e-aadf-dfbbd005c495.local`, and the sender resolved exactly that
    host. Ours advertised `tao-MNCA-XX.local` -- the one shape that did not match.

    Leading (unproven) hypothesis: that hostname is the SNAP bonjourListenerUUID we hand
    the phone over NFC, which is how the sender knows which mDNS peer is the bumped one.
    So default to the very UUID baked into the PM3's SNAP blob.
    """
    ident = os.path.join(os.path.dirname(os.path.abspath(__file__)), "snap-identity.json")
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
    2026-08-15 on the Pixel hotspot, `413C042B-...._asquic._udp` -> taotekiiPhone.local ->
    192.168.168.126, empty TXT. Publishing only fe80:: would ask the phone to route somewhere it
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
    # on an infrastructure link the iPhone publishes an IPv4 (measured: 192.168.68.134), and
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
    global CAP_DIR
    CAP_DIR = outdir
    os.makedirs(CAP_DIR, exist_ok=True)
    # extraction lands received files in cwd; keep them alongside captures
    os.chdir(CAP_DIR)

    # swap the module-global handler class BEFORE AirDropServer bakes it into httpd
    od_server.AirDropServerHandler = _make_capturing_handler(od_server.AirDropServerHandler)

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
    print("captures + received files -> %s" % CAP_DIR, flush=True)
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


def run_selftest():
    """Loopback: drive OpenDrop's own client at our capturing receiver over ::1."""
    global CAP_DIR
    import socket
    import tempfile
    from opendrop.client import AirDropClient

    CAP_DIR = tempfile.mkdtemp(prefix="airdrop-capture-selftest-")
    vcard = os.path.abspath("./samples/contact.vcf")
    if not os.path.exists(vcard):
        vcard = os.path.join(CAP_DIR, "contact.vcf")
        with open(vcard, "w") as f:
            f.write("BEGIN:VCARD\nVERSION:3.0\nFN:Self Test\nEND:VCARD\n")
    os.chdir(CAP_DIR)

    Handler = _make_capturing_handler(od_server.AirDropServerHandler)
    rconf = AirDropConfig(interface="lo", computer_name="cap-recv", debug=True)
    Handler.config = rconf
    httpd = od_server.HTTPServerV6(("::1", 0), Handler)
    httpd.socket = rconf.get_ssl_context().wrap_socket(sock=httpd.socket, server_side=True)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print("selftest receiver up on [::1]:%d, captures -> %s" % (port, CAP_DIR), flush=True)

    sconf = AirDropConfig(interface="lo", computer_name="cap-send")
    sconf.interface = None
    client = AirDropClient(sconf, ("::1", port))
    socket.setdefaulttimeout(15)
    print("send_ask ->", client.send_ask(vcard), flush=True)
    print("send_upload ->", client.send_upload(vcard), flush=True)
    time.sleep(0.5)
    httpd.shutdown()

    caps = sorted(os.listdir(CAP_DIR))
    print("\ncapture dir contents:", flush=True)
    for c in caps:
        p = os.path.join(CAP_DIR, c)
        print("  %6d  %s" % (os.path.getsize(p), c), flush=True)
    have_ask = any("-Ask-" in c and c.endswith(".body.raw") for c in caps)
    have_upload = any("-Upload-" in c and c.endswith(".body.raw") for c in caps)
    landed = os.path.exists(os.path.join(CAP_DIR, "contact.vcf"))
    ok = have_ask and have_upload and landed
    print("\nASK body captured=%s  UPLOAD body captured=%s  file landed=%s"
          % (have_ask, have_upload, landed), flush=True)
    print("SELFTEST", "PASS" if ok else "FAIL", flush=True)
    sys.exit(0 if ok else 1)


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
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    # configure logging so OpenDrop's own logger (zeroconf announce, mDNS errors,
    # TLS identity) is actually visible -- otherwise we run blind.
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    if args.selftest:
        run_selftest()
    else:
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
