#!/usr/bin/env python3
"""Is our receiver ACTUALLY discoverable on awdl0 right now?

Why this exists: take pm3-namedrop-gap1-20260814-123628 burned 75 s of bumping while our
mDNS was dead. OWL had restarted, awdl0 was recreated with a new ifindex, and the receiver's
zeroconf socket stayed bound to the OLD scope id -- it logged one `OSError: [Errno 101]
Network is unreachable` 40 s before the take and then sat there, process alive, listening
socket open, advertising NOTHING. Every symptom of a healthy rig; zero packets on the air.

`ps` says nothing useful here, and neither does the listening socket. The only honest check is
to browse for our own service the way the iPhone would, so that is what this does.

Exit 0 = discoverable (safe to bump). Exit 1 = NOT discoverable (restart the advertiser).
"""
import argparse
import ipaddress
import json
import os
import socket
import sys
import time

try:
    import ifaddr
    from zeroconf import IPVersion, ServiceBrowser, ServiceListener, Zeroconf
except ImportError:
    print("FAIL: zeroconf not importable -- run this with the project venv", file=sys.stderr)
    sys.exit(2)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVICE = "_asquic._udp.local."


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


def link_local_ipv6(interface):
    for adapter in ifaddr.get_adapters():
        if adapter.name == interface:
            for ip in adapter.ips:
                if ip.is_IPv6:
                    return ipaddress.IPv6Address(ip.ip[0])
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-i", "--interface", default="awdl0")
    ap.add_argument("-t", "--seconds", type=float, default=6.0)
    args = ap.parse_args()

    try:
        idx = socket.if_nametoindex(args.interface)
    except OSError:
        print("FAIL: %s does not exist -- OWL is down" % args.interface)
        return 1
    print("%s is ifindex %d" % (args.interface, idx))

    # The instance name we require: the bonjourListenerUUID the card hands the phone over NFC.
    ident = os.path.join(REPO, "scratchpad", "snap-identity.json")
    try:
        with open(ident) as fh:
            uuid = json.load(fh)["bonjour_listener_uuid"]
    except (OSError, KeyError, ValueError) as exc:
        print("FAIL: no SNAP identity at %s (%s)" % (ident, exc))
        return 1
    want = "%s.%s" % (uuid.upper(), SERVICE)

    # Bind exactly as mdns-advertise.py does. The default Zeroconf() is IPv4-only, and awdl0
    # carries nothing but an IPv6 link-local -- browsing with the default finds nothing and
    # looks identical to a dead advertiser.
    addr = link_local_ipv6(args.interface)
    if addr is None:
        print("FAIL: %s has no IPv6 address -- OWL is not up" % args.interface)
        return 1
    print("browsing on %s for %s" % (addr, want))

    zc = Zeroconf(interfaces=[str(addr)], ip_version=IPVersion.V6Only)
    collector = Collector()
    ServiceBrowser(zc, SERVICE, collector)
    time.sleep(args.seconds)
    zc.close()

    ok = False
    print("_asquic._udp instances:")
    if not collector.found:
        print("  (none)")
    for name, info in sorted(collector.found.items()):
        mine = name.lower() == want.lower()
        print("  %s %s\n      SRV -> %s  port %s"
              % ("<== OURS" if mine else "        ", name,
                 (info.server or "").rstrip("."), info.port))
        ok = ok or mine

    if not ok:
        print("FAIL: nothing advertises %s" % want)
        print("      Restart mdns-advertise.py. The classic cause is an OWL restart recreating")
        print("      awdl0 while its zeroconf socket stays bound to the dead ifindex.")
        return 1

    print("PASS: discoverable as %s -- safe to bump." % want)
    return 0


if __name__ == "__main__":
    sys.exit(main())
