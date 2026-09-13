package com.namedrop.card;

import android.os.SystemClock;
import android.util.Log;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

/**
 * Process-wide log and counters shared by the HCE service and the UI. Static because the
 * service is started by the NFC stack, not by us, so there is nowhere else to hang state.
 *
 * logcat is the record of truth: {@code adb logcat -s NDCARD}.
 */
public final class Events {

    public static final String TAG = "NDCARD";

    private static final List<String> LINES = new ArrayList<>();
    private static final long T0 = SystemClock.elapsedRealtime();

    private static int activations = 0;
    private static int boopSelects = 0;
    private static int serverInfoPushes = 0;
    private static int resumes = 0;
    private static int ndefRefused = 0;

    private Events() {
    }

    public static synchronized void line(String s) {
        String stamped = String.format(Locale.US, "[%7d] %s",
                SystemClock.elapsedRealtime() - T0, s);
        Log.i(TAG, stamped);
        LINES.add(stamped);
        while (LINES.size() > 200) {
            LINES.remove(0);
        }
    }

    public static synchronized List<String> snapshot() {
        return new ArrayList<>(LINES);
    }

    public static synchronized void countActivation() {
        activations++;
    }

    public static synchronized void countBoopSelect() {
        boopSelects++;
    }

    public static synchronized void countServerInfoPush() {
        serverInfoPushes++;
    }

    public static synchronized void countResume() {
        resumes++;
    }

    public static synchronized void countNdefRefused() {
        ndefRefused++;
    }

    /**
     * One complete handshake is two boop SELECTs (two activations), one ServerInfo push and
     * one resume. NDEF refusals are normal on the home screen and are noise, not failure.
     */
    public static synchronized String summary() {
        return "activations=" + activations
                + "  boop=" + boopSelects
                + "  serverInfo=" + serverInfoPushes
                + "  resumes=" + resumes
                + "  ndefRefused=" + ndefRefused;
    }

    public static String hex(byte[] b) {
        if (b == null) {
            return "<null>";
        }
        StringBuilder sb = new StringBuilder(b.length * 2);
        for (byte x : b) {
            sb.append(String.format(Locale.US, "%02X", x));
        }
        return sb.toString();
    }
}
