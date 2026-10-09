#!/usr/bin/env python3
"""Serve HTTP/3 on the `_asquic._udp` port the bump routes to.

Why this exists: evidence take asquic-quic-connect-20260814. After the NFC handshake the iPhone
resolves `<bonjourListenerUUID>._asquic._udp.local` and opens QUIC to it -- 548 Initial/CRYPTO
packets in one take, many distinct connections, all retried. Our kernel ICMP-rejected every one
because nothing listens there. `_airdrop._tcp` + HTTPS is the legacy path; that is why every prior
take stalled after `/Discover` waiting for an `/Ask` on a transport the bump does not use.

Read straight off those CRYPTO frames (no guessing): **ALPN `h3`, QUIC v1 (0x00000001), TLS 1.3,
and NO SNI** -- so there is no hostname for the client to match against our certificate.

It answers the NameDrop sequence -- /Hello, /Ask, /Exchange -- and logs method, path, every
header and a summary of the body of each request. An unknown path is logged loudly and 404'd
rather than guessed at.
"""
import argparse
import asyncio
import json
import logging
import os
import plistlib

from aioquic.asyncio import QuicConnectionProtocol, serve
from aioquic.h3.connection import H3_ALPN, H3Connection
from aioquic.h3.events import DataReceived, HeadersReceived
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.events import ConnectionTerminated, HandshakeCompleted, ProtocolNegotiated

# iOS's AirDrop HTTP/3 request omits the :authority pseudo-header, which aioquic's RFC-9114
# validator rejects with H3_MESSAGE_ERROR (270) -- closing the connection right after the TLS
# handshake succeeds, which on the phone reads as "Waiting". We are reverse-engineering a real
# client, not enforcing the spec on it, so relax the requirement to just :method.
import aioquic.h3.connection as _h3c


def _lenient_validate_request_headers(headers, stream=None):
    _h3c.validate_headers(
        headers,
        allowed_pseudo_headers=frozenset(
            (b":method", b":scheme", b":authority", b":path", b":protocol")
        ),
        required_pseudo_headers=frozenset((b":method",)),
        stream=stream,
    )


_h3c.validate_request_headers = _lenient_validate_request_headers

log = logging.getLogger("asquic")

# Seconds to wait after the /Exchange 200 before sending CONNECTION_CLOSE, so the response is
# on the wire and acked first.
EXCHANGE_CLOSE_DELAY = 1.0

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Contact cards received over /Exchange land here.
RECEIVED_DIR = os.path.join(REPO, "received")

# The contact card we hand back in /Exchange, loaded from --vcard at startup. The shipped
# default (samples/contact.vcf) is the "Genuine Apple" test card -- it is what proves the
# exchange completed, and it is obviously a joke on the receiving phone. Point --vcard at
# your own .vcf to send a real card.
OUR_VCARD = None
OUR_FULL_NAME = "namedrop-re"
OUR_EMAIL = "namedrop-re@example.invalid"


def _load_our_card(path):
    """Read the vCard we send, and pull FullName/EMAIL out of it for the response fields."""
    global OUR_VCARD, OUR_FULL_NAME, OUR_EMAIL
    with open(path, "rb") as fh:
        OUR_VCARD = fh.read().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    for raw in OUR_VCARD.decode("utf-8", "replace").split("\r\n"):
        prop, _, value = raw.partition(":")
        name = prop.split(";", 1)[0].upper()
        if name == "FN" and value:
            OUR_FULL_NAME = value
        elif name == "EMAIL" and value:
            OUR_EMAIL = value


def _our_listener_uuid():
    """The bonjourListenerUUID we serve over NFC, read from the identity the applet was built from.

    Must never be hardcoded: it rotates, and a stale copy here would answer /Hello with an identity
    that no longer matches the one the bump committed.
    """
    path = os.path.join(REPO, "scratchpad", "snap-identity.json")
    try:
        with open(path) as fh:
            return json.load(fh)["bonjour_listener_uuid"].upper()
    except (OSError, KeyError, ValueError) as exc:
        log.warning("no SNAP identity at %s (%s) -- /Hello will answer with an empty id", path, exc)
        return ""
COMPUTER_NAME = "namedrop-re"
COMPUTER_MODEL = "OpenDrop"


def _plist(obj):
    return plistlib.dumps(obj, fmt=plistlib.FMT_BINARY)


def _describe_body(data):
    """Say something useful about a body without assuming it is a plist."""
    if not data:
        return "(empty)"
    head = data[:8]
    if head.startswith(b"bplist00"):
        try:
            parsed = plistlib.loads(data)
            return "bplist: %s" % json.dumps(
                {k: ("<%d bytes>" % len(v) if isinstance(v, bytes) else v) for k, v in parsed.items()},
                default=str,
            )[:400]
        except Exception as exc:
            return "bplist (unparsed: %s)" % exc
    return "%d bytes, head=%s" % (len(data), head.hex())


class AirDropH3(QuicConnectionProtocol):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._http = None
        self._streams = {}
        self._peer = None

    # ---- QUIC layer ----------------------------------------------------------------
    def quic_event_received(self, event):
        if isinstance(event, ProtocolNegotiated):
            log.info("ALPN negotiated: %r", event.alpn_protocol)
            if event.alpn_protocol in H3_ALPN:
                self._http = H3Connection(self._quic)
        elif isinstance(event, HandshakeCompleted):
            log.info("*** QUIC HANDSHAKE COMPLETED *** alpn=%r", event.alpn_protocol)
        elif isinstance(event, ConnectionTerminated):
            log.info("connection terminated: code=%s reason=%r", event.error_code, event.reason_phrase)

        if self._http is not None:
            for h3_event in self._http.handle_event(event):
                self._h3_event(h3_event)

    # ---- HTTP/3 layer --------------------------------------------------------------
    def _h3_event(self, event):
        if isinstance(event, HeadersReceived):
            headers = {k.decode("utf8", "replace"): v.decode("utf8", "replace") for k, v in event.headers}
            self._streams[event.stream_id] = {"headers": headers, "body": b""}
            log.info(
                "*** H3 REQUEST *** %s %s",
                headers.get(":method", "?"),
                headers.get(":path", "?"),
            )
            for k, v in headers.items():
                log.info("      %s: %s", k, v)
            if event.stream_ended:
                self._respond(event.stream_id)
        elif isinstance(event, DataReceived):
            st = self._streams.setdefault(event.stream_id, {"headers": {}, "body": b""})
            st["body"] += event.data
            if event.stream_ended:
                self._respond(event.stream_id)

    def _respond(self, stream_id):
        st = self._streams.pop(stream_id, None)
        if st is None:
            return
        path = st["headers"].get(":path", "")
        body = st["body"]
        log.info("  body: %s", _describe_body(body))

        status, payload, ctype = self._handle(path, body)
        log.info("  -> %s (%d bytes)", status, len(payload))
        self._http.send_headers(
            stream_id,
            [(b":status", str(status).encode()), (b"content-type", ctype)],
        )
        self._http.send_data(stream_id, payload, end_stream=True)
        self.transmit()

        # A completed /Exchange ends the session -- so END IT. Nothing here ever closed the QUIC
        # connection: we answered 200, ended the stream, and left the connection dangling until
        # idle timeout. Measured 2026-08-14 (evidence take pm3-dedup-arm{A,B}-*): bump 1 completes
        # the full exchange, bump 2 stalls at "Waiting" having sent ZERO packets -- iOS does not
        # even try, because as far as sharingd is concerned the boop session is still open. An
        # AirDrop off/on toggle (which tears that state down) restores it, same identity, and that
        # is what the "iOS dedups on the listener UUID" reading actually was. Close cleanly, after
        # a beat so the 200 is on the wire and acked first.
        if path.rstrip("/").lower().endswith("exchange"):
            asyncio.get_running_loop().call_later(EXCHANGE_CLOSE_DELAY, self._close_session)

    def _close_session(self):
        """Clean CONNECTION_CLOSE so iOS retires the boop session and the next bump can start one."""
        log.info("  *** closing the QUIC connection after /Exchange (clean session teardown) ***")
        self._quic.close(error_code=0)
        self.transmit()

    def _handle(self, path, body):
        """Answer /Hello, /Ask and /Exchange. Unknown paths are 404'd and logged, not guessed."""
        p = path.rstrip("/").lower()
        if p.endswith("hello"):
            # AirDrop-over-QUIC opens with POST /Hello:
            #   { "id": {"id": <per-conn UUID>}, "featureFlags": 3, "contextType": {"unknown": {}} }
            # First RE attempt: echo the same shape back with our own id, 200. If the iPhone
            # proceeds to a next request, that reveals the next step; if it errors, the shape is
            # wrong and the error tells us how.
            log.info("  *** /Hello OVER QUIC -- answering to advance the handshake ***")
            # Read the identity; do NOT hardcode it. This line used to be a literal
            # C3D12677-... -- our listener UUID as of 2026-08-14, stale within a day of being
            # written, and the third instance of that trap in this repo (see
            # evidence take asquic-h3-client-loopback-20260814).
            our_id = _our_listener_uuid()
            try:
                req = plistlib.loads(body) if body else {}
            except Exception:
                req = {}
            resp = {
                "id": {"id": our_id},
                "featureFlags": req.get("featureFlags", 3),
                "contextType": req.get("contextType", {"unknown": {}}),
            }
            return 200, _plist(resp), b"application/octet-stream"
        if p.endswith("exchange"):
            # THE payload step of NameDrop: the iPhone POSTs its contact card here as VCardData,
            # alongside FullName / Handle / IdentityShareInfo. Save the received card, then answer
            # 200 with OUR card in the same shape (NameDrop is a mutual exchange, so the iPhone
            # expects the peer's card back).
            try:
                req = plistlib.loads(body)
            except Exception:
                req = {}
            vc = req.get("VCardData")
            if isinstance(vc, bytes):
                name = (req.get("FullName") or "card").replace("\n", " ").replace("/", "_")
                dst = os.path.join(RECEIVED_DIR, "RECEIVED-%s.vcf" % name)
                with open(dst, "wb") as fh:
                    fh.write(vc)
                log.info("  *** RECEIVED CONTACT CARD over NameDrop: %s (%d B) -> %s ***",
                         req.get("FullName"), len(vc), os.path.basename(dst))
            resp = {
                "TransferID": req.get("TransferID", {"id": ""}),
                "FullName": OUR_FULL_NAME,
                "Handle": {"email": {"_0": OUR_EMAIL}},
                "VCardData": OUR_VCARD,
            }
            log.info("  *** /Exchange -- answering 200 with our own card to complete the exchange ***")
            return 200, _plist(resp), b"application/octet-stream"
        if p.endswith("ask"):
            log.info("  *** /Ask OVER QUIC -- ACCEPTING ***")
            return 200, _plist({
                "ReceiverModelName": COMPUTER_MODEL,
                "ReceiverComputerName": COMPUTER_NAME,
            }), b"application/octet-stream"
        log.warning("  !! UNKNOWN PATH %r -- 404. This is new protocol; record it.", path)
        return 404, b"", b"application/octet-stream"


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="::", help="bind address (default :: = all IPv6)")
    ap.add_argument("--port", type=int, default=60192,
                    help="must match the port advertised in the _asquic SRV record")
    ap.add_argument("--cert", default=os.path.join(REPO, "scratchpad", "asquic-keys", "snapkey-cert.pem"),
                    help="TLS cert carrying SNAP key 1 (default: what mint-snapkey-cert.sh writes)")
    ap.add_argument("--key", default=os.path.join(REPO, "scratchpad", "asquic-keys", "snapkey-key.pem"))
    ap.add_argument("--vcard", default=os.path.join(REPO, "samples", "contact.vcf"),
                    help="the contact card we send back in /Exchange (default: samples/contact.vcf)")
    args = ap.parse_args()

    os.makedirs(RECEIVED_DIR, exist_ok=True)
    _load_our_card(args.vcard)

    config = QuicConfiguration(is_client=False, alpn_protocols=H3_ALPN)
    config.load_cert_chain(args.cert, args.key)

    await serve(args.host, args.port, configuration=config, create_protocol=AirDropH3)
    log.info("serving HTTP/3 on [%s]:%d  (ALPN %s)", args.host, args.port, H3_ALPN)
    log.info("cert: %s", args.cert)
    log.info("received cards -> %s", RECEIVED_DIR)
    log.info("our card: %s (%s, %d B)", args.vcard, OUR_FULL_NAME, len(OUR_VCARD))
    log.info("waiting for the bump to route a QUIC connection here...")
    await asyncio.Future()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
