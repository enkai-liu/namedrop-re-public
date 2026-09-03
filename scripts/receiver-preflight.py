#!/usr/bin/env python3
"""Is our AirDrop receiver ACTUALLY discoverable on awdl0 right now?

Why this exists: take pm3-namedrop-gap1-20260814-123628 burned 75 s of bumping while our
mDNS was dead. OWL had restarted, awdl0 was recreated with a new ifindex, and the receiver's
zeroconf socket stayed bound to the OLD scope id -- it logged one `OSError: [Errno 101]
Network is unreachable` 40 s before the take and then sat there, process alive, listening
socket open, advertising NOTHING. Every symptom of a healthy rig; zero packets on the air.

`ps` says nothing useful here, and neither does the listening socket. The only honest check is
to browse for our own service the way the iPhone would, so that is what this does.

Exit 0 = discoverable (safe to spend a take). Exit 1 = NOT discoverable (restart the receiver).
"""
import argparse
import socket
import sys
import time

try:
    from zeroconf import IPVersion, ServiceBrowser, ServiceListener, Zeroconf
except ImportError:
    print("FAIL: zeroconf not importable -- run this with the project venv", file=sys.stderr)
    sys.exit(2)

SERVICE = "_airdrop._tcp.local."


def ifindex(name):
    try:
        return socket.if_nametoindex(name)
    except OSError:
        return None


class Collector(ServiceListener):
    def __init__(self):
        self.found = {}

    def _record(self, zc, type_, name):
        try:
            info = zc.get_service_info(type_, name, timeout=2000)
        except Exception:
            return
        if info:
            self.found[name] = info

    add_service = _record
    update_service = _record

    def remove_service(self, zc, type_, name):
        self.found.pop(name, None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-i", "--interface", default="awdl0")
    ap.add_argument("-t", "--seconds", type=float, default=6.0)
    ap.add_argument(
        "--expect-host",
        default=None,
        help="SRV target hostname we require (without .local). Defaults to the "
        "bonjourListenerUUID in snap-identity.json -- the UUID the PM3 hands "
        "the phone over NFC. A mismatch here means the bump has no peer to resolve.",
    )
    args = ap.parse_args()

    idx = ifindex(args.interface)
    if idx is None:
        print("FAIL: %s does not exist -- OWL is down" % args.interface)
        return 1
    print("%s is ifindex %d" % (args.interface, idx))

    expect = args.expect_host
    if expect is None:
        try:
            import json
            import os

            here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            with open(os.path.join(here, "scratchpad", "snap-identity.json")) as fh:
                expect = json.load(fh)["bonjour_listener_uuid"]
        except Exception as exc:
            print("note: no SNAP identity to compare against (%s)" % exc)

    # Bind exactly as opendrop's receiver does (server.py:67-69). The default Zeroconf() is
    # IPv4-only, and awdl0 carries nothing but an IPv6 link-local -- browsing with the default
    # finds nothing and looks identical to a dead receiver. That false FAIL is worse than no
    # check at all, so mirror the receiver's own binding.
    try:
        from opendrop.util import AirDropUtil

        addr = AirDropUtil.get_ip_for_interface(args.interface, ipv6=True)
    except Exception as exc:
        print("FAIL: cannot resolve %s's IPv6 address (%s)" % (args.interface, exc))
        return 1
    if addr is None:
        print("FAIL: %s has no IPv6 address -- OWL is not up" % args.interface)
        return 1
    print("browsing on %s (IPv6, as the receiver binds)" % addr)

    zc = Zeroconf(interfaces=[str(addr)], ip_version=IPVersion.V6Only)
    collector = Collector()
    ServiceBrowser(zc, SERVICE, collector)
    time.sleep(args.seconds)
    zc.close()

    if not collector.found:
        print("FAIL: no %s advertised on the network at all." % SERVICE)
        print("      Our receiver is not on the air. Restart it BEFORE spending a take:")
        print("      the classic cause is an OWL restart recreating awdl0 while the")
        print("      receiver's zeroconf socket stays bound to the dead ifindex.")
        return 1

    ok = False
    for name, info in sorted(collector.found.items()):
        target = (info.server or "").rstrip(".")
        short = target[:-6] if target.endswith(".local") else target
        addrs = []
        for raw in info.addresses:
            fam = socket.AF_INET6 if len(raw) == 16 else socket.AF_INET
            addrs.append(socket.inet_ntop(fam, raw))
        flags = info.properties.get(b"flags", b"?").decode(errors="replace")
        mine = expect is not None and short == expect
        print(
            "  %s %s\n      SRV -> %s  port %s  flags %s  addr %s"
            % ("<== OURS" if mine else "        ", name, target, info.port, flags,
               ",".join(addrs) or "(none)")
        )
        ok = ok or mine

    if expect is None:
        print("PASS(weak): something is advertising, but no expected host to match against.")
        return 0
    if not ok:
        print("FAIL: nothing advertises SRV target %s.local" % expect)
        print("      That UUID is the bind -- the PM3 hands it to the phone as SNAP key 2,")
        print("      and sharingd resolves the bumped peer by exactly this hostname.")
        return 1

    print("PASS: we are discoverable as %s.local -- safe to spend a take." % expect)
    return 0


if __name__ == "__main__":
    sys.exit(main())
