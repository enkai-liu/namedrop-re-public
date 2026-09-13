package com.namedrop.card;

import android.nfc.cardemulation.HostApduService;
import android.os.Bundle;

/**
 * The card half of the bump.
 *
 * A NameDrop bump is an ISO-DEP card transaction: after the ECP frames the iPhone
 * anticollision-selects the peer and sends SELECT-AID + GET DATA. Android runs REQA,
 * anticollision and RATS itself, so the first thing we see is the SELECT APDU, with no
 * leading ISO-DEP PCB byte.
 *
 * Timing budget: the negotiated FWT is ~38.7 ms, so processCommandApdu() must not touch
 * disk or IPC. Every answer is a static blob from {@link SnapBlobs}.
 */
public class NameDropHceService extends HostApduService {

    private static final byte[] SW_OK = {(byte) 0x90, 0x00};
    private static final byte[] SW_FILE_NOT_FOUND = {0x6A, (byte) 0x82};

    /**
     * Per-activation SNAP state, reset at deactivation. The two-activation handshake still
     * works across that reset: SnapApplet falls back to SnapBlobs.CAPS on resume.
     */
    private final SnapApplet snap = new SnapApplet();

    private static volatile boolean inSession = false;
    private static String lastApduHex = null;

    /**
     * Is an ISO-DEP transaction in flight right now?
     *
     * Read by MainActivity so the emit phase never raises our own reader field mid-handshake.
     * That teardown is reported as reason=0 LINK_LOSS, byte-identical to the phone walking
     * away, so it would be impossible to tell apart from bad geometry.
     */
    public static boolean isInSession() {
        return inSession;
    }

    @Override
    public byte[] processCommandApdu(byte[] apdu, Bundle extras) {
        inSession = true;
        String hex = Events.hex(apdu);
        boolean repeat = hex.equals(lastApduHex);
        lastApduHex = hex;

        byte[] resp = respond(apdu, !repeat);
        if (!repeat) {
            Events.line("APDU <- " + hex);
            Events.line("APDU -> " + Events.hex(resp));
        }
        return resp;
    }

    @Override
    public void onDeactivated(int reason) {
        // 0 = DEACTIVATION_LINK_LOSS, 1 = DEACTIVATION_DESELECTED
        Events.line("DEACTIVATED reason=" + reason + "   " + Events.summary());
        lastApduHex = null;
        inSession = false;
        snap.reset();
    }

    private byte[] respond(byte[] apdu, boolean narrate) {
        if (apdu == null || apdu.length < 4) {
            return SW_OK;
        }
        boolean isSelectByAid = apdu[0] == 0x00 && apdu[1] == (byte) 0xA4 && apdu[2] == 0x04;
        if (isSelectByAid) {
            Events.countActivation();
        }

        // 🔑 REFUSE BACKGROUND TAG READING. On the home screen an iPhone asks any card it
        // meets for NDEF; a satisfying answer lets iOS finish as a tag read and never escalate
        // to NameDrop (measured: all NDEF, 0 boop, and the iPhone offering to open our URL).
        // The Proxmark3 firmware refuses it the same way (ND_ANSWER_NDEF 0). Refuse the whole
        // Type 4 ladder, not just the application SELECT: iOS sometimes selects the files
        // directly on a re-activation.
        if (isSelectByAid && Events.hex(apdu).contains("D276000085010")) {
            Events.countNdefRefused();
            return SW_FILE_NOT_FOUND;
        }
        boolean isSelectFile = apdu[0] == 0x00 && apdu[1] == (byte) 0xA4 && apdu[2] == 0x00;
        boolean isReadBinary = apdu[0] == 0x00 && apdu[1] == (byte) 0xB0;
        if (isSelectFile || isReadBinary) {
            return SW_FILE_NOT_FOUND;
        }

        SnapApplet.Result r = snap.dispatch(apdu);
        if (r == null) {
            return SW_OK;
        }
        if (r.boopSelect) {
            Events.countBoopSelect();
        } else if (r.serverInfoPush) {
            Events.countServerInfoPush();
        } else if (apdu[1] == (byte) 0xCA && apdu[2] == 0x01 && apdu[3] == 0x04) {
            Events.countResume();
        }
        if (narrate) {
            Events.line("    [SNAP] " + r.label);
            if (r.serverInfoPush) {
                Events.line("    [SNAP] peer listenerUUID = " + snap.getPeerUuid());
            }
        }
        return r.response;
    }
}
