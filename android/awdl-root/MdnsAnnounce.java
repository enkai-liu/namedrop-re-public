import android.system.Os;
import android.system.OsConstants;

import java.io.ByteArrayOutputStream;
import java.io.FileDescriptor;
import java.net.Inet6Address;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.NetworkInterface;
import java.util.Enumeration;

/**
 * Root mDNS announcer for our `_asquic._udp` listener on the Pixel's AWDL link (mosey0), the
 * on-phone version of scripts/mdns-advertise.py. Announcing is what makes the iPhone connect:
 * mosey_daemon turns the announcement into a synthetic AWDL service-response TLV, and the
 * iPhone opens QUIC to the AAAA without sending a query. Multicast alone is enough.
 *
 *   CLASSPATH=/data/local/tmp/awdl-root.dex app_process / MdnsAnnounce \
 *       <listenerUUID> <iface> <port> <intervalSecs> <durationSecs> [unicast-fe80 ...]
 *
 * Record shape mirrors the captured real announcement: UPPERCASE instance name, SRV target =
 * <uuid>.local (lowercase), priority/weight 0, a single zero-length TXT string, AAAA = the
 * interface's link-local. ⚠️ Sent FROM port 5353 (RFC 6762 §6.7) — from an ephemeral port every
 * response is discarded on arrival while every "is it transmitting" check still passes.
 */
public final class MdnsAnnounce {
    static void name(ByteArrayOutputStream o, String n) {
        for (String l : n.split("\\.")) {
            if (l.isEmpty()) continue;
            byte[] b = l.getBytes();
            o.write(b.length);
            o.write(b, 0, b.length);
        }
        o.write(0);
    }

    static void u16(ByteArrayOutputStream o, int v) { o.write(v >> 8); o.write(v); }
    static void u32(ByteArrayOutputStream o, long v) { u16(o, (int) (v >> 16) & 0xffff); u16(o, (int) v & 0xffff); }

    static void rr(ByteArrayOutputStream o, String owner, int type, int cls, long ttl, byte[] rdata) {
        name(o, owner);
        u16(o, type);
        u16(o, cls);
        u32(o, ttl);
        u16(o, rdata.length);
        o.write(rdata, 0, rdata.length);
    }

    static byte[] packet(String uuid, int port, byte[] aaaa) {
        String inst = uuid.toUpperCase() + "._asquic._udp.local";
        String host = uuid.toLowerCase() + ".local";
        ByteArrayOutputStream o = new ByteArrayOutputStream();
        u16(o, 0); u16(o, 0x8400); u16(o, 0); u16(o, 4); u16(o, 0); u16(o, 0);
        ByteArrayOutputStream r = new ByteArrayOutputStream();
        name(r, inst);
        rr(o, "_asquic._udp.local", 12, 1, 4500, r.toByteArray());                 // PTR
        r = new ByteArrayOutputStream();
        u16(r, 0); u16(r, 0); u16(r, port); name(r, host);
        rr(o, inst, 33, 0x8001, 120, r.toByteArray());                              // SRV
        rr(o, inst, 16, 0x8001, 4500, new byte[] {0});                              // TXT ""
        rr(o, host, 28, 0x8001, 120, aaaa);                                         // AAAA
        return o.toByteArray();
    }

    public static void main(String[] a) throws Exception {
        String uuid = a[0], ifname = a[1];
        int port = Integer.parseInt(a[2]);
        long interval = Long.parseLong(a[3]) * 1000, duration = Long.parseLong(a[4]) * 1000;
        NetworkInterface ni = null;
        for (int i = 0; i < 40 && ni == null; i++) { ni = NetworkInterface.getByName(ifname); if (ni == null) Thread.sleep(500); }
        if (ni == null) throw new IllegalStateException(ifname + " not present");
        byte[] ll = null;
        for (Enumeration<InetAddress> e = ni.getInetAddresses(); e.hasMoreElements(); ) {
            InetAddress ia = e.nextElement();
            if (ia instanceof Inet6Address && ia.isLinkLocalAddress()) ll = ia.getAddress();
        }
        if (ll == null) throw new IllegalStateException(ifname + " has no link-local");
        byte[] pkt = packet(uuid, port, ll);

        FileDescriptor fd = Os.socket(OsConstants.AF_INET6, OsConstants.SOCK_DGRAM, OsConstants.IPPROTO_UDP);
        Os.setsockoptInt(fd, OsConstants.SOL_SOCKET, OsConstants.SO_REUSEADDR, 1);
        Os.setsockoptInt(fd, OsConstants.SOL_SOCKET, 15 /* SO_REUSEPORT */, 1);
        Os.setsockoptInt(fd, OsConstants.IPPROTO_IPV6, OsConstants.IPV6_MULTICAST_IF, ni.getIndex());
        Os.setsockoptInt(fd, OsConstants.IPPROTO_IPV6, OsConstants.IPV6_MULTICAST_HOPS, 255);
        Os.bind(fd, InetAddress.getByName("::"), 5353);

        java.util.List<InetSocketAddress> dst = new java.util.ArrayList<>();
        dst.add(new InetSocketAddress(Inet6Address.getByAddress(null, InetAddress.getByName("ff02::fb").getAddress(), ni), 5353));
        for (int i = 5; i < a.length; i++)
            dst.add(new InetSocketAddress(Inet6Address.getByAddress(null, InetAddress.getByName(a[i]).getAddress(), ni), 5353));

        System.out.println("ANNOUNCE " + uuid.toUpperCase() + "._asquic._udp.local -> " + uuid + ".local:" + port
                + " AAAA " + InetAddress.getByAddress(ll).getHostAddress() + " on " + ifname + "(" + ni.getIndex() + ") "
                + pkt.length + " B, to " + dst);
        long end = System.currentTimeMillis() + duration;
        int n = 0;
        while (System.currentTimeMillis() < end) {
            for (InetSocketAddress d : dst) {
                try {
                    Os.sendto(fd, pkt, 0, pkt.length, 0, d.getAddress(), 5353);
                    n++;
                } catch (Exception ex) {
                    System.out.println("send to " + d + " failed: " + ex);  // never kill the loop
                }
            }
            if (n % 10 == 0 || n <= 2) System.out.println(System.currentTimeMillis() + " sent " + n);
            Thread.sleep(interval);
        }
        System.out.println("done, sent " + n);
    }
}
