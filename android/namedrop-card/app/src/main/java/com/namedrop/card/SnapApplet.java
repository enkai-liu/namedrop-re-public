package com.namedrop.card;

/**
 * The com.apple.boop.SNAP applet, ported from firmware/hf_namedrop.c.
 *
 * Once ECP emission gets iOS to classify the phone as a peer, it selects the boop AID and
 * pushes its SNAP ServerInfo. Answering that push with a bare {@code 9000} makes iOS give up
 * and fall back to reading us as an NDEF tag; this class gives the answer a real card does.
 *
 * <h2>Deliberately free of Android imports</h2>
 *
 * Nothing here touches android.*, so the whole dispatch runs on a desktop JVM. That is how
 * it was verified: real iPhone bump traces replayed through this code, byte-for-byte
 * against the Proxmark3 applet. Keep it that way.
 *
 * <h2>The timing budget is real</h2>
 *
 * Our ATS negotiates FWT = 38.66 ms and a real iPhone card answers in 10.6–15.7 ms.
 * processCommandApdu() must not touch disk or IPC, so every blob is a static in
 * {@link SnapBlobs} (baked into the dex, decoded once at class-init) and the work here is
 * array copies. The largest answer is head + a 30-byte label + tail = 248 B, framed as
 * 1 PCB + 248 + 2 SW + 2 CRC = 253 B — inside FSC/FSD 256, so no ISO-DEP chaining.
 */
public final class SnapApplet {

    /** com.apple.boop — matched by PREFIX, as the firmware does. */
    private static final byte[] BOOP_AID_PREFIX = {
            (byte) 0xA0, 0x00, 0x00, 0x08, 0x58
    };

    /**
     * What a real boop card answers to the AID SELECT. Byte-identical to the real card's
     * frame in both bump takes, CRC included — confirmed against the traces, not assumed.
     */
    private static final byte[] SELECT_RESP = {
            (byte) 0xA5, 0x03, (byte) 0xCE, 0x01, 0x01, (byte) 0x90, 0x00
    };

    private static final byte[] SW_OK = {(byte) 0x90, 0x00};
    private static final byte[] SW_FILE_NOT_FOUND = {0x6A, (byte) 0x82};
    /** 6A88 is a REDIRECT, not a failure: the reader returns on a fresh activation. */
    private static final byte[] SW_REDIRECT = {0x6A, (byte) 0x88};

    /**
     * The reader's ServerInfo map(6) opens {@code A6 00 44} = map(6), key 0, bytes(4).
     * Everything after it sits at a fixed offset, so the peer's fields can be lifted
     * without a CBOR parser — the firmware does this because an ARM7TDMI cannot afford
     * one inside the FWT, and we keep it so both implementations stay comparable.
     */
    private static final byte[] SI_ANCHOR = {(byte) 0xA6, 0x00, 0x44};
    private static final int SI_OFF_TS = 3;      // bytes(4)  CFAbsoluteTime
    private static final int SI_OFF_UUID = 103;  // bytes(16) after `02 50`
    private static final int SI_OFF_TOK = 121;   // bytes(6)  after `03 46`

    /** Longest service label we will echo. The firmware's peer_label[] is 64 B. */
    private static final int MAX_LABEL = 64;

    /** A GET DATA this large is the ServerInfo push; smaller is the capabilities push. */
    private static final int SERVERINFO_MIN_LC = 150;

    /** What the dispatch did, and what to say about it. */
    public static final class Result {
        public final byte[] response;
        public final String label;
        /** True only for the boop AID SELECT — the gap-#1 signal worth shouting about. */
        public final boolean boopSelect;
        /** True when iOS pushed us its ServerInfo — the gap-#2 signal. */
        public final boolean serverInfoPush;

        Result(byte[] response, String label, boolean boopSelect, boolean serverInfoPush) {
            this.response = response;
            this.label = label;
            this.boopSelect = boopSelect;
            this.serverInfoPush = serverInfoPush;
        }

        Result(byte[] response, String label) {
            this(response, label, false, false);
        }
    }

    // ---- per-activation state -------------------------------------------------------

    /**
     * The reader's own capabilities blob. A real card hands this back VERBATIM when the
     * reader resumes the session, which is automatically right for either flow — so we
     * keep the reader's bytes rather than answering with our own.
     */
    private byte[] capsFromReader;
    private byte[] peerUuid;
    private byte[] peerTs;
    private String peerLabel;

    public void reset() {
        capsFromReader = null;
        peerUuid = null;
        peerTs = null;
        peerLabel = null;
    }

    public String getPeerUuid() {
        return peerUuid == null ? null : uuidString(peerUuid);
    }

    public String getPeerLabel() {
        return peerLabel;
    }

    /** True if the APDU is a SELECT-by-AID of anything under com.apple.boop. */
    public static boolean isBoopSelect(byte[] apdu) {
        if (apdu == null || apdu.length < 5 + BOOP_AID_PREFIX.length) {
            return false;
        }
        if (apdu[0] != 0x00 || apdu[1] != (byte) 0xA4 || apdu[2] != 0x04) {
            return false;
        }
        return startsWith(apdu, 5, BOOP_AID_PREFIX);
    }

    /**
     * The dispatch. Mirrors hf_namedrop.c's switch on INS/P1/P2.
     *
     * @return the response body INCLUDING the two status bytes, or null when this APDU is
     *         not ours — the caller then falls through to its own NDEF handling, which is
     *         what keeps the tag path working as a control.
     */
    public Result dispatch(byte[] apdu) {
        // The degenerate 1-byte I-block iOS sends ~200 ms after our SELECT response when
        // it has nothing to push (5/5 in the PM3 take, spread 1.6 ms — a fixed timeout,
        // not computation). ND_PROBE_ACTION=1: answer with our ServerInfo unprompted.
        if (apdu != null && apdu.length > 0 && apdu.length < 4) {
            return new Result(cat(spliceServerInfo(null), SW_OK),
                    "probe (" + apdu.length + "-byte I-block) -> our ServerInfo");
        }
        if (apdu == null || apdu.length < 4) {
            return null;
        }

        int ins = apdu[1] & 0xFF;
        int p1 = apdu[2] & 0xFF;
        int p2 = apdu[3] & 0xFF;
        int lc = apdu.length > 4 ? (apdu[4] & 0xFF) : 0;
        int bodyMax = Math.max(0, apdu.length - 5);
        if (lc > bodyMax) {
            lc = bodyMax;
        }

        if (ins == 0xA4 && p1 == 0x04) {
            if (isBoopSelect(apdu)) {
                return new Result(SELECT_RESP.clone(),
                        "SELECT com.apple.boop -- PEER FLOW", true, false);
            }
            return null;   // not ours; let the NDEF path answer
        }

        if (ins == 0xCA && p1 == 0x01 && p2 == 0x03) {
            byte[] body = slice(apdu, 5, lc);
            if (lc >= SERVERINFO_MIN_LC) {
                scrapeServerInfo(body);
                byte[] si = spliceServerInfo(body);
                return new Result(cat(si, SW_OK),
                        "iOS pushed its ServerInfo (" + lc + " B) -> ours (" + si.length
                                + " B), echoing " + (peerLabel == null ? "<default>" : peerLabel),
                        false, true);
            }
            // The capabilities push. A real card answers {1:719, 2:session} with 6A88.
            if (lc > 0) {
                capsFromReader = body;
            }
            return new Result(cat(SnapBlobs.REDIRECT, SW_REDIRECT),
                    "capabilities push (" + lc + " B) -> redirect 6A88 + session id");
        }

        if (ins == 0xCA && p1 == 0x01 && p2 == 0x04) {
            byte[] caps = capsFromReader != null ? capsFromReader : SnapBlobs.CAPS;
            return new Result(cat(caps, SW_OK),
                    "iOS RESUMED our session -> capabilities blob (" + caps.length + " B)");
        }

        if (ins == 0xCA) {
            return new Result(SW_FILE_NOT_FOUND.clone(),
                    String.format("unexpected GET DATA P1=%02X P2=%02X Lc=%d", p1, p2, lc));
        }
        return null;
    }

    // ---- the two pieces of real work ------------------------------------------------

    /**
     * Lift the peer's CFAbsoluteTime and bonjourListenerUUID out of its ServerInfo.
     *
     * Verifies the fixed sub-tags before trusting the offsets, exactly as the firmware
     * does — a blind copy at a fixed index would silently produce garbage against any
     * ServerInfo shaped even slightly differently.
     */
    private boolean scrapeServerInfo(byte[] body) {
        int a = indexOf(body, SI_ANCHOR);
        if (a < 0 || a + SI_OFF_TOK + 6 > body.length) {
            return false;
        }
        if (body[a + 7] != 0x01 || body[a + 8] != 0x58 || body[a + 9] != 0x5B) {
            return false;
        }
        if (body[a + 101] != 0x02 || body[a + 102] != 0x50) {
            return false;
        }
        peerTs = slice(body, a + SI_OFF_TS, 4);
        peerUuid = slice(body, a + SI_OFF_UUID, 16);
        return true;
    }

    /**
     * head + the READER's key-1 text item + tail, with the reader's timestamp echoed in.
     *
     * We echo the service label rather than choosing one: a NameDrop contact exchange
     * offers `com.apple.boop.SNAP` (20 B CBOR) and a share sheet offers
     * `com.apple.airdrop.sharesheet` (30 B), which is the ONLY difference between the
     * 238/248 and 85/95 payload shapes seen on the air. The bump that cleared gap #1 on
     * this device offered the sharesheet flavour, so answering a hardcoded 238 would have
     * been a service mismatch on the very first real exchange.
     *
     * @param body the reader's ServerInfo, or null to answer with our baked-in default
     */
    private byte[] spliceServerInfo(byte[] body) {
        int hl = SnapBlobs.SI_HEAD.length;
        byte[] label = null;

        if (body != null && body.length > hl && startsWith(body, 0, SnapBlobs.SI_HEAD)) {
            int t = body[hl] & 0xFF;
            int labelLen = 0;
            if (t >= 0x60 && t < 0x78) {
                labelLen = 1 + (t & 0x1F);              // CBOR text, inline length
            } else if (t == 0x78 && body.length > hl + 1) {
                labelLen = 2 + (body[hl + 1] & 0xFF);   // CBOR text, 1-byte length
            }
            if (labelLen > 0 && hl + labelLen <= body.length && labelLen <= MAX_LABEL) {
                label = slice(body, hl, labelLen);
            }
        }

        if (label == null) {
            // Unparseable, or no reader ServerInfo at all -> our own baked-in label.
            label = slice(SnapBlobs.SERVERINFO, hl,
                    SnapBlobs.SERVERINFO.length - hl - SnapBlobs.SI_TAIL.length);
            peerLabel = null;
        } else {
            peerLabel = asciiOf(label);
        }

        byte[] out = new byte[hl + label.length + SnapBlobs.SI_TAIL.length];
        System.arraycopy(SnapBlobs.SI_HEAD, 0, out, 0, hl);
        System.arraycopy(label, 0, out, hl, label.length);
        System.arraycopy(SnapBlobs.SI_TAIL, 0, out, hl + label.length, SnapBlobs.SI_TAIL.length);

        // A real card echoes the reader's CFAbsoluteTime; ours is a placeholder otherwise.
        if (peerTs != null) {
            System.arraycopy(peerTs, 0, out,
                    hl + label.length + SnapBlobs.SI_TAIL_TS_OFF, 4);
        }
        return out;
    }

    // ---- small helpers ---------------------------------------------------------------

    private static boolean startsWith(byte[] hay, int at, byte[] needle) {
        if (hay == null || at + needle.length > hay.length) {
            return false;
        }
        for (int i = 0; i < needle.length; i++) {
            if (hay[at + i] != needle[i]) {
                return false;
            }
        }
        return true;
    }

    private static int indexOf(byte[] hay, byte[] needle) {
        if (hay == null || needle.length > hay.length) {
            return -1;
        }
        outer:
        for (int i = 0; i + needle.length <= hay.length; i++) {
            for (int j = 0; j < needle.length; j++) {
                if (hay[i + j] != needle[j]) {
                    continue outer;
                }
            }
            return i;
        }
        return -1;
    }

    private static byte[] slice(byte[] src, int off, int len) {
        byte[] out = new byte[len];
        System.arraycopy(src, off, out, 0, len);
        return out;
    }

    private static byte[] cat(byte[] a, byte[] b) {
        byte[] out = new byte[a.length + b.length];
        System.arraycopy(a, 0, out, 0, a.length);
        System.arraycopy(b, 0, out, a.length, b.length);
        return out;
    }

    /** The label minus its CBOR text header, for logging only. */
    private static String asciiOf(byte[] item) {
        int skip = ((item[0] & 0xFF) == 0x78) ? 2 : 1;
        StringBuilder sb = new StringBuilder(item.length - skip);
        for (int i = skip; i < item.length; i++) {
            sb.append((char) (item[i] & 0xFF));
        }
        return sb.toString();
    }

    private static String uuidString(byte[] u) {
        StringBuilder sb = new StringBuilder(36);
        for (int i = 0; i < 16; i++) {
            if (i == 4 || i == 6 || i == 8 || i == 10) {
                sb.append('-');
            }
            sb.append(String.format("%02x", u[i]));
        }
        return sb.toString();
    }
}
