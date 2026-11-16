package com.namedrop.card.h3;

import java.security.KeyFactory;
import java.security.KeyPair;
import java.security.KeyPairGenerator;
import java.security.PrivateKey;
import java.security.PublicKey;
import java.security.spec.X509EncodedKeySpec;
import java.util.Arrays;
import java.util.List;

import javax.crypto.KeyAgreement;

import tech.kwik.agent15.TlsConstants;
import tech.kwik.agent15.alert.IllegalParameterAlert;
import tech.kwik.agent15.engine.KeyExchange;
import tech.kwik.agent15.engine.KeyExchangeFactory;
import tech.kwik.agent15.engine.impl.KeyExchangeFactoryImpl;

/**
 * Agent15's key exchange, with an X25519 that works on Android.
 *
 * ⚠️ WHY (2026-11-15): the iPhone's ClientHello keys on X25519, and Agent15's XDHKeyExchange does
 * `KeyPairGenerator.getInstance("XDH").initialize(new NamedParameterSpec("X25519"))` and casts to
 * java.security.interfaces.XECPublicKey. Conscrypt rejects the NamedParameterSpec
 * (InvalidAlgorithmParameterException → a bare RuntimeException mid-handshake, connection aborted,
 * the iPhone sits on "Waiting…") and its X25519 keys are not XECPublicKeys anyway. Conscrypt DOES do
 * "XDH" with its default curve (X25519) and X.509-wrapped keys, so this uses exactly that.
 *
 * Registered through META-INF/services (src/main/resources); Agent15's own service file is excluded
 * in app/build.gradle.kts so this is the only factory ServiceLoader finds. EC groups delegate to
 * Agent15's implementation, which works on Android.
 */
public final class AndroidKeyExchangeFactory implements KeyExchangeFactory {
    private final KeyExchangeFactoryImpl agent15 = new KeyExchangeFactoryImpl();

    @Override
    public KeyExchange forGroup(TlsConstants.NamedGroup group) {
        if (group == TlsConstants.NamedGroup.x25519) return new X25519();
        return agent15.forGroup(group);
    }

    @Override
    public List<TlsConstants.NamedGroup> getSupportedGroups() {
        return List.of(TlsConstants.NamedGroup.x25519, TlsConstants.NamedGroup.secp256r1);
    }

    @Override
    public void init() {
        agent15.init();
    }

    /** RFC 8410 SubjectPublicKeyInfo prefix for an X25519 key: SEQ { SEQ { OID 1.3.101.110 }, BIT STRING }. */
    static final byte[] SPKI_PREFIX = {0x30, 0x2a, 0x30, 0x05, 0x06, 0x03, 0x2b, 0x65, 0x6e, 0x03, 0x21, 0x00};

    static final class X25519 implements KeyExchange {
        private PrivateKey priv;
        private byte[] pubRaw;

        private void generate() {
            try {
                KeyPair kp = KeyPairGenerator.getInstance("XDH").generateKeyPair();  // Conscrypt default = X25519
                priv = kp.getPrivate();
                byte[] enc = kp.getPublic().getEncoded();                             // X.509 SPKI, 44 B
                pubRaw = Arrays.copyOfRange(enc, enc.length - 32, enc.length);
            } catch (Exception e) {
                throw new IllegalStateException("X25519 key generation failed", e);
            }
        }

        private byte[] agree(byte[] peerRaw) throws IllegalParameterAlert {
            if (peerRaw.length != 32) throw new IllegalParameterAlert("Invalid x25519 key length: " + peerRaw.length);
            try {
                byte[] spki = Arrays.copyOf(SPKI_PREFIX, SPKI_PREFIX.length + 32);
                System.arraycopy(peerRaw, 0, spki, SPKI_PREFIX.length, 32);
                PublicKey peer = KeyFactory.getInstance("XDH").generatePublic(new X509EncodedKeySpec(spki));
                KeyAgreement ka = KeyAgreement.getInstance("XDH");
                ka.init(priv);
                ka.doPhase(peer, true);
                return ka.generateSecret();
            } catch (Exception e) {
                throw new IllegalParameterAlert("invalid x25519 public key: " + e);
            }
        }

        @Override public void generateClientKeyPair() { generate(); }
        @Override public byte[] getClientKeyShare() { return pubRaw; }
        @Override public byte[] clientComputeSharedSecret(byte[] serverShare) throws IllegalParameterAlert { return agree(serverShare); }
        @Override public byte[] serverProcessClientKeyShare(byte[] clientShare) throws IllegalParameterAlert {
            if (priv == null) generate();
            return agree(clientShare);
        }
        @Override public byte[] getServerKeyShare() { return pubRaw; }
    }

    /** On-device self-test: two X25519 instances must agree.  app_process … AndroidKeyExchangeFactory */
    public static void main(String[] a) throws Exception {
        X25519 c = new X25519(), s = new X25519();
        c.generateClientKeyPair();
        byte[] ss = s.serverProcessClientKeyShare(c.getClientKeyShare());
        byte[] cs = c.clientComputeSharedSecret(s.getServerKeyShare());
        System.out.println("x25519 self-test: " + (Arrays.equals(ss, cs) && ss.length == 32 ? "AGREE (32 B)" : "MISMATCH"));
        for (tech.kwik.agent15.engine.KeyExchangeFactory f : java.util.ServiceLoader.load(tech.kwik.agent15.engine.KeyExchangeFactory.class))
            System.out.println("ServiceLoader factory: " + f.getClass().getName() + " groups=" + f.getSupportedGroups());
    }
}
