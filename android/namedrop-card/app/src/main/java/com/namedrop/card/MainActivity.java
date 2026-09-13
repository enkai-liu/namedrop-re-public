package com.namedrop.card;

import android.app.Activity;
import android.app.PendingIntent;
import android.content.ComponentName;
import android.content.Intent;
import android.graphics.Color;
import android.graphics.Typeface;
import android.nfc.NfcAdapter;
import android.nfc.cardemulation.CardEmulation;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.WindowManager;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

import java.util.List;

/**
 * While this Activity is in the foreground the phone is a NameDrop card: it cycles ECP
 * emission and answers the bump as an HCE card.
 *
 * Its real job is winning NFC contention. Google Wallet holds the default card routing and
 * only yields while another app is foregrounded as the preferred service, and reader mode
 * (which the emitter needs) is bound to the foreground Activity. Leave the app and the
 * card stops.
 */
public class MainActivity extends Activity {

    private static final String BOOP_AID = "A000000858044F0100";

    private NfcAdapter nfcAdapter;
    private CardEmulation cardEmulation;
    private ComponentName serviceComponent;

    private TextView status;
    private TextView logView;
    private final Handler handler = new Handler(Looper.getMainLooper());
    private boolean phaseOn = false;
    private String routed = "?";

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        serviceComponent = new ComponentName(this, NameDropHceService.class);
        nfcAdapter = NfcAdapter.getDefaultAdapter(this);
        if (nfcAdapter != null) {
            cardEmulation = CardEmulation.getInstance(nfcAdapter);
            try {
                cardEmulation.setShouldDefaultToObserveModeForService(serviceComponent, false);
            } catch (Exception e) {
                Events.line("setShouldDefaultToObserveMode FAILED: " + e);
            }
        }
        if (getActionBar() != null) {
            getActionBar().hide();
        }
        setContentView(buildUi());
        // If the screen sleeps the Activity pauses and Wallet takes the routing back.
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        Events.line("listenerUUID " + SnapBlobs.LISTENER_UUID);
    }

    private LinearLayout buildUi() {
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(Color.BLACK);
        root.setPadding(24, 120, 24, 24);

        status = new TextView(this);
        status.setTextColor(Color.GREEN);
        status.setTypeface(Typeface.MONOSPACE);
        status.setTextSize(13);
        root.addView(status);

        logView = new TextView(this);
        logView.setTextColor(Color.WHITE);
        logView.setTypeface(Typeface.MONOSPACE);
        logView.setTextSize(9);
        ScrollView scroll = new ScrollView(this);
        scroll.addView(logView);
        root.addView(scroll);
        return root;
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (nfcAdapter == null || !nfcAdapter.isEnabled()) {
            Events.line("NFC is unavailable or switched off");
            handler.post(refresh);
            return;
        }
        try {
            Events.line("setPreferredService -> "
                    + cardEmulation.setPreferredService(this, serviceComponent));
            routed = cardEmulation.isDefaultServiceForAid(serviceComponent, BOOP_AID) ? "yes" : "NO";
        } catch (Exception e) {
            Events.line("setPreferredService FAILED: " + e);
        }
        enableForegroundDispatch();
        EcpEmitter.burst(this, nfcAdapter);
        phaseOn = true;
        handler.postDelayed(cycle, EcpEmitter.ON_MS);
        handler.post(refresh);
        Events.line("CARD ARMED  emit " + EcpEmitter.ON_MS + " ms / quiet "
                + EcpEmitter.OFF_MS + " ms");
    }

    @Override
    protected void onPause() {
        super.onPause();
        handler.removeCallbacks(cycle);
        handler.removeCallbacks(refresh);
        if (nfcAdapter == null) {
            return;
        }
        try {
            nfcAdapter.disableForegroundDispatch(this);
        } catch (Exception ignored) {
            // not enabled
        }
        EcpEmitter.quiet(this, nfcAdapter);
        try {
            cardEmulation.unsetPreferredService(this);
        } catch (Exception ignored) {
            // not set
        }
        Events.line("CARD STOPPED (app left the foreground)");
    }

    /**
     * Burst, then quiet. The quiet phase is when we are a card and the iPhone can poll us.
     */
    private final Runnable cycle = new Runnable() {
        @Override
        public void run() {
            if (phaseOn) {
                EcpEmitter.quiet(MainActivity.this, nfcAdapter);
                // enableReaderMode supersedes foreground dispatch and disableReaderMode does
                // not restore it. Without re-arming, a URI tag in the field pops an "open in
                // Chrome?" prompt, which backgrounds us and silently ends the card.
                enableForegroundDispatch();
                phaseOn = false;
                handler.postDelayed(this, EcpEmitter.OFF_MS);
            } else if (NameDropHceService.isInSession()) {
                // Never raise our own field mid-handshake; defer by one quiet period.
                handler.postDelayed(this, EcpEmitter.OFF_MS);
            } else {
                EcpEmitter.burst(MainActivity.this, nfcAdapter);
                phaseOn = true;
                handler.postDelayed(this, EcpEmitter.ON_MS);
            }
        }
    };

    private void enableForegroundDispatch() {
        try {
            PendingIntent pi = PendingIntent.getActivity(this, 0,
                    new Intent(this, getClass()).addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP),
                    PendingIntent.FLAG_MUTABLE);
            nfcAdapter.enableForegroundDispatch(this, pi, null, null);
        } catch (Exception e) {
            Events.line("foregroundDispatch FAILED: " + e);
        }
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        Events.line("stray tag swallowed (" + intent.getAction()
                + ") - move other NFC tags away");
    }

    private final Runnable refresh = new Runnable() {
        @Override
        public void run() {
            status.setText("NameDrop card\n"
                    + "listenerUUID " + SnapBlobs.LISTENER_UUID + "\n"
                    + "nfc=" + (nfcAdapter != null && nfcAdapter.isEnabled())
                    + "  boop AID routed to us=" + routed + "\n"
                    + Events.summary() + "\n"
                    + "Keep this screen open. Tap the iPhone to the phone's back.");
            List<String> lines = Events.snapshot();
            StringBuilder sb = new StringBuilder();
            for (int i = Math.max(0, lines.size() - 60); i < lines.size(); i++) {
                sb.append(lines.get(i)).append('\n');
            }
            logView.setText(sb.toString());
            handler.postDelayed(this, 1000);
        }
    };
}
