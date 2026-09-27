package com.namedrop.card;

import android.app.Activity;
import android.nfc.NfcAdapter;
import android.os.Bundle;

import java.util.Random;

/**
 * Emits Apple ECP frames via the reader-mode polling-loop annotation. Without them iOS treats
 * the phone as a plain NFC tag and asks for NDEF; the NameDrop frame is what makes it select
 * the boop AID.
 *
 * <p>An unprivileged app can't emit and keep HCE listening at the same time, so this
 * alternates: {@link #burst} (reader mode on, frame on the air), then {@link #quiet} (reader
 * mode off, we're a card again and the iPhone can select us). No root needed.
 */
public final class EcpEmitter {

    /**
     * Literal rather than NfcAdapter.EXTRA_READER_TECH_A_POLLING_LOOP_ANNOTATION: that symbol
     * is behind a @FlaggedApi stub and will not compile against a public SDK. The client
     * forwards the Bundle untouched, so the string is all that is needed.
     */
    private static final String EXTRA_ANNOTATION =
            "android.nfc.extra.READER_TECH_A_POLLING_LOOP_ANNOTATION";

    private static final int FLAG_READER_NFC_A = 0x1;
    private static final int FLAG_READER_SKIP_NDEF_CHECK = 0x80;
    private static final int FLAG_READER_NO_PLATFORM_SOUNDS = 0x100;
    /** The "keep listening, stop polling" value NfcService special-cases. */
    private static final int FLAG_POLLING_DISABLE = 0x1000;

    /** flags = 0x1181. Non-zero tech mask, so no privilege check. */
    static final int FLAGS = FLAG_READER_NFC_A | FLAG_POLLING_DISABLE
            | FLAG_READER_SKIP_NDEF_CHECK | FLAG_READER_NO_PLATFORM_SOUNDS;

    /** Emission only happens on (re)configuring reader mode; below ~450 ms nothing goes out. */
    public static final int ON_MS = 500;
    /**
     * Our field suppresses the iPhone's polling, so a long OFF phase buys activations.
     * 500 on / 2000 off was the measured knee.
     */
    public static final int OFF_MS = 2000;

    /**
     * NameDrop ECP frame (TCI 01 00 01) header; a 6-byte payload follows. No CRC: the NFC
     * controller appends CRC_A, so the frame is 14 bytes, never 16.
     */
    private static final String NAMEDROP_HEADER = "6a02890500010001";
    /**
     * iOS debounces the warp on a repeated identical frame, so the payload rotates every
     * burst. The first two bytes stay {@code c0ff} so a sniffer can tell our frames from an
     * Apple device's.
     */
    private static final String PAYLOAD_SENTINEL = "c0ff";
    /** AirDrop ECP frame (TCI 01 00 00). The all-zero payload is real, not a placeholder. */
    private static final String FRAME_AIRDROP = "6a02890500010000000000000000";

    private static final Random RNG = new Random();
    /** Only one annotation slot works (the vendor-extension slot suppresses it), so frames alternate. */
    private static boolean nextIsAirdrop = false;

    private EcpEmitter() {
    }

    /** Reader mode on with the next frame in the NameDrop/AirDrop alternation. */
    public static void burst(Activity activity, NfcAdapter adapter) {
        String hex = nextIsAirdrop ? FRAME_AIRDROP : nextNamedropFrame();
        nextIsAirdrop = !nextIsAirdrop;
        Bundle extras = new Bundle();
        extras.putByteArray(EXTRA_ANNOTATION, hexToBytes(hex));
        try {
            // Returns void and does not throw on a server-side refusal; NfcService logs
            // "setReaderMode: ... flags: 4481, annotation: <hex>" when it accepts the call.
            adapter.enableReaderMode(activity, tag -> { }, FLAGS, extras);
        } catch (Exception e) {
            Events.line("EMIT enableReaderMode THREW: " + e);
        }
    }

    /** Reader mode off: our field falls and HCE routing returns, so we are a card. */
    public static void quiet(Activity activity, NfcAdapter adapter) {
        try {
            adapter.disableReaderMode(activity);
        } catch (Exception e) {
            Events.line("EMIT disableReaderMode FAILED: " + e);
        }
    }

    private static String nextNamedropFrame() {
        StringBuilder sb = new StringBuilder(NAMEDROP_HEADER).append(PAYLOAD_SENTINEL);
        for (int i = 0; i < 4; i++) {
            sb.append(String.format("%02x", RNG.nextInt(256)));
        }
        return sb.toString();
    }

    private static byte[] hexToBytes(String h) {
        byte[] out = new byte[h.length() / 2];
        for (int i = 0; i < out.length; i++) {
            out[i] = (byte) Integer.parseInt(h.substring(i * 2, i * 2 + 2), 16);
        }
        return out;
    }
}
