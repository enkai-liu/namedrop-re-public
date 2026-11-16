package com.namedrop.card.h3;

import com.dd.plist.BinaryPropertyListParser;
import com.dd.plist.BinaryPropertyListWriter;
import com.dd.plist.NSData;
import com.dd.plist.NSDictionary;
import com.dd.plist.NSObject;
import com.dd.plist.NSString;

import java.io.ByteArrayInputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Paths;
import java.security.KeyFactory;
import java.security.KeyStore;
import java.security.PrivateKey;
import java.security.cert.Certificate;
import java.security.cert.CertificateFactory;
import java.security.spec.PKCS8EncodedKeySpec;
import java.util.Base64;
import java.util.Locale;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;

import tech.kwik.core.QuicConnection;
import tech.kwik.core.log.SysOutLogger;
import tech.kwik.core.server.ApplicationProtocolConnection;
import tech.kwik.core.server.ApplicationProtocolConnectionFactory;
import tech.kwik.core.server.ServerConnectionConfig;
import tech.kwik.core.server.ServerConnector;
import tech.kwik.flupke.server.Http3ApplicationProtocolFactory;
import tech.kwik.flupke.server.HttpServerRequest;
import tech.kwik.flupke.server.HttpServerResponse;

/**
 * The on-phone NameDrop receiver: QUIC + HTTP/3 on the `_asquic` port. A port of
 * scripts/asquic-receiver.py with the same response shapes and the same close after /Exchange.
 *
 *   CLASSPATH=$(pm path com.namedrop.card | cut -d: -f2) app_process / \
 *       com.namedrop.card.h3.NameDropReceiver <port> <cert.pem> <key.pem> <listenerUUID> <outdir> <our.vcf>
 *
 * Started as root by android/awdl-root/namedrop-pixel.sh: a root-raised mosey0 needs the routing
 * rule that script adds, and the rest of the stack (MoseyStart, MdnsAnnounce) is root too.
 */
public final class NameDropReceiver {
    static final String COMPUTER_NAME = "namedrop-re";
    static final String COMPUTER_MODEL = "OpenDrop";
    static final int DEVICE_SUPPORT_FLAGS = 0x1B3FB;
    static final long EXCHANGE_CLOSE_DELAY_MS = 1000;

    private final String listenerUuid;
    private final File outDir;
    /** The card we hand back in /Exchange, and the FN / EMAIL pulled out of it. */
    private final byte[] ourVcard;
    private String ourFullName = "namedrop-re";
    private String ourEmail = "namedrop-re@example.invalid";
    /** Flupke hands the request handler no connection, so remember each one by peer address. */
    private final Map<InetSocketAddress, QuicConnection> conns = new ConcurrentHashMap<>();
    private final ScheduledExecutorService timer = Executors.newSingleThreadScheduledExecutor();

    NameDropReceiver(String listenerUuid, File outDir, byte[] vcard) {
        this.listenerUuid = listenerUuid.toUpperCase(Locale.ROOT);
        this.outDir = outDir;
        String text = new String(vcard, StandardCharsets.UTF_8).replace("\r\n", "\n");
        for (String line : text.split("\n")) {
            int colon = line.indexOf(':');
            if (colon < 0 || colon == line.length() - 1) continue;
            String prop = line.substring(0, colon).split(";", 2)[0].toUpperCase(Locale.ROOT);
            if (prop.equals("FN")) ourFullName = line.substring(colon + 1);
            else if (prop.equals("EMAIL")) ourEmail = line.substring(colon + 1);
        }
        this.ourVcard = text.replace("\n", "\r\n").getBytes(StandardCharsets.UTF_8);
    }

    static void log(String s) {
        System.out.println(java.time.LocalTime.now() + " ndrx: " + s);
    }

    void handle(HttpServerRequest req, HttpServerResponse resp) throws java.io.IOException {
        String path = req.path();
        byte[] body = req.body().readAllBytes();
        log("*** H3 REQUEST *** " + req.method() + " " + path + " from " + req.remoteAddress()
                + "  ua=" + req.headers().firstValue("user-agent").orElse("?") + "  body " + body.length + " B");
        String p = path.replaceAll("/+$", "").toLowerCase(Locale.ROOT);
        String stem = p.substring(p.lastIndexOf('/') + 1);
        save(System.currentTimeMillis() / 1000 + "-h3-" + (stem.isEmpty() ? "root" : stem) + ".body.raw", body);
        NSDictionary rq = parse(body);

        int status = 200;
        NSDictionary out = null;
        if (p.endsWith("hello")) {
            out = new NSDictionary();
            NSDictionary id = new NSDictionary();
            id.put("id", listenerUuid);
            out.put("id", id);
            out.put("featureFlags", rq != null && rq.containsKey("featureFlags") ? rq.get("featureFlags") : NSObject.fromJavaObject(3));
            NSObject ctx = rq != null ? rq.get("contextType") : null;
            if (ctx == null) { NSDictionary u = new NSDictionary(); u.put("unknown", new NSDictionary()); ctx = u; }
            out.put("contextType", ctx);
        } else if (p.endsWith("exchange")) {
            NSObject vc = rq != null ? rq.get("VCardData") : null;
            if (vc instanceof NSData) {
                String name = rq.containsKey("FullName") ? rq.get("FullName").toJavaObject().toString() : "card";
                name = name.replace('\n', ' ').replace('/', '_');
                save("RECEIVED-" + name + ".vcf", ((NSData) vc).bytes());
                log("  *** RECEIVED CONTACT CARD over NameDrop: " + name + " (" + ((NSData) vc).length() + " B) ***");
            }
            out = new NSDictionary();
            NSObject tid = rq != null ? rq.get("TransferID") : null;
            if (tid == null) { NSDictionary t = new NSDictionary(); t.put("id", ""); tid = t; }
            out.put("TransferID", tid);
            out.put("FullName", ourFullName);
            NSDictionary email = new NSDictionary();
            email.put("_0", ourEmail);
            NSDictionary handle = new NSDictionary();
            handle.put("email", email);
            out.put("Handle", handle);
            out.put("VCardData", new NSData(ourVcard));
            log("  *** /Exchange -- answering 200 with our own card ***");
            scheduleClose(req.remoteAddress());
        } else if (p.endsWith("discover")) {
            out = new NSDictionary();
            out.put("ReceiverMediaCapabilities", new NSData("{\"Version\": 1}".getBytes(StandardCharsets.UTF_8)));
            out.put("ReceiverComputerName", COMPUTER_NAME);
            out.put("ReceiverModelName", COMPUTER_MODEL);
            out.put("IsAirDropable", true);
            out.put("DeviceSupportFlags", DEVICE_SUPPORT_FLAGS);
        } else if (p.endsWith("ask")) {
            log("  *** /Ask OVER QUIC -- ACCEPTING ***");
            out = new NSDictionary();
            out.put("ReceiverModelName", COMPUTER_MODEL);
            out.put("ReceiverComputerName", COMPUTER_NAME);
        } else if (p.endsWith("upload")) {
            log("  *** /Upload OVER QUIC -- " + body.length + " bytes ***");
        } else {
            log("  !! UNKNOWN PATH " + path + " -- 404. This is new protocol; record it.");
            status = 404;
        }

        byte[] payload = out == null ? new byte[0] : BinaryPropertyListWriter.writeToArray(out);
        resp.setStatus(status);
        resp.addHeader("content-type", "application/octet-stream");
        try (OutputStream o = resp.getOutputStream()) {
            o.write(payload);
        }
        log("  -> " + status + " (" + payload.length + " bytes)");
    }

    /** A completed /Exchange ends the session: close cleanly, after a beat so the 200 is acked. */
    private void scheduleClose(InetSocketAddress peer) {
        timer.schedule(() -> {
            QuicConnection c = conns.remove(peer);
            if (c != null) {
                log("  *** closing the QUIC connection after /Exchange (clean session teardown) ***");
                c.close();
            }
        }, EXCHANGE_CLOSE_DELAY_MS, TimeUnit.MILLISECONDS);
    }

    private static NSDictionary parse(byte[] b) {
        if (b.length == 0) return null;
        try {
            NSObject o = BinaryPropertyListParser.parse(new ByteArrayInputStream(b));
            return o instanceof NSDictionary ? (NSDictionary) o : null;
        } catch (Exception e) {
            return null;
        }
    }

    private void save(String name, byte[] b) {
        try (FileOutputStream f = new FileOutputStream(new File(outDir, name))) {
            f.write(b);
        } catch (Exception e) {
            log("  could not save " + name + ": " + e);
        }
    }

    static KeyStore keyStore(String certPem, String keyPem, char[] pw) throws Exception {
        String k = new String(Files.readAllBytes(Paths.get(keyPem)))
                .replaceAll("-----[A-Z ]+-----", "").replaceAll("\\s", "");
        PrivateKey priv = KeyFactory.getInstance("EC").generatePrivate(new PKCS8EncodedKeySpec(Base64.getDecoder().decode(k)));
        Certificate cert;
        try (InputStream in = new FileInputStream(certPem)) {
            cert = CertificateFactory.getInstance("X.509").generateCertificate(in);
        }
        KeyStore ks = KeyStore.getInstance("PKCS12");
        ks.load(null, null);
        ks.setKeyEntry("snap", priv, pw, new Certificate[] {cert});
        return ks;
    }

    public static void main(String[] a) throws Exception {
        int port = Integer.parseInt(a[0]);
        String uuid = a[3];
        File out = new File(a[4]);
        byte[] vcard = Files.readAllBytes(Paths.get(a[5]));
        out.mkdirs();
        // A post-quantum key share (X25519MLKEM768) makes the ClientHello span two Initials; Kwik
        // drops a non-final Initial with < 700 B of CRYPTO data unless this is off.
        System.setProperty("tech.kwik.core.min-crypto-data-check", "disabled");
        SysOutLogger klog = new SysOutLogger();
        klog.logInfo(true);
        klog.logWarning(true);
        if (System.getenv("H3_DEBUG") != null) { klog.logDebug(true); klog.logPackets(true); }

        char[] pw = "snap".toCharArray();
        // ⚠️ Agent15's PEM path is RSA-only; the SNAP key is P-256, so go through a KeyStore.
        ServerConnector srv = ServerConnector.builder()
                .withPort(port)
                .withKeyStore(keyStore(a[1], a[2], pw), "snap", pw, "secp256r1")
                .withConfiguration(ServerConnectionConfig.builder()
                        .maxIdleTimeoutInSeconds(30)
                        .maxOpenPeerInitiatedBidirectionalStreams(50)
                        .maxOpenPeerInitiatedUnidirectionalStreams(10)
                        .build())
                .withLogger(klog)
                .build();

        NameDropReceiver rx = new NameDropReceiver(uuid, out, vcard);
        Http3ApplicationProtocolFactory h3 = new Http3ApplicationProtocolFactory(rx::handle);
        srv.registerApplicationProtocol("h3", new ApplicationProtocolConnectionFactory() {
            @Override
            public ApplicationProtocolConnection createConnection(String protocol, QuicConnection c) {
                InetSocketAddress peer = c instanceof tech.kwik.core.server.impl.ServerConnectionImpl
                        ? ((tech.kwik.core.server.impl.ServerConnectionImpl) c).getInitialRemoteAddress() : null;
                log("*** QUIC HANDSHAKE COMPLETED *** alpn=" + protocol + " peer=" + peer);
                if (peer != null) rx.conns.put(peer, c);
                return h3.createConnection(protocol, c);
            }
            @Override public int maxConcurrentPeerInitiatedUnidirectionalStreams() { return h3.maxConcurrentPeerInitiatedUnidirectionalStreams(); }
            @Override public long maxTotalPeerInitiatedUnidirectionalStreams() { return h3.maxTotalPeerInitiatedUnidirectionalStreams(); }
            @Override public int maxConcurrentPeerInitiatedBidirectionalStreams() { return h3.maxConcurrentPeerInitiatedBidirectionalStreams(); }
            @Override public int minUnidirectionalStreamReceiverBufferSize() { return h3.minUnidirectionalStreamReceiverBufferSize(); }
            @Override public long maxUnidirectionalStreamReceiverBufferSize() { return h3.maxUnidirectionalStreamReceiverBufferSize(); }
        });
        srv.start();
        log("serving HTTP/3 on [::]:" + port + " (ALPN h3), listener " + rx.listenerUuid + ", our card " + rx.ourFullName + ", received -> " + out);
        log("waiting for the bump to route a QUIC connection here...");
        Thread.sleep(Long.MAX_VALUE);
    }
}
