# `namedrop-card` — an Android phone as the NFC card

This app does the Proxmark3's job on an **unrooted Android phone**: it emits the Apple ECP
frames, answers the bump as an ISO-DEP card, and runs the `com.apple.boop.SNAP` handshake.
The AWDL/QUIC half still runs on Linux with the AR9271, just as it does in the Proxmark3 setup.

```
iPhone ──NFC bump──▶ Android phone (this app)       SNAP: hands over key 1 + listenerUUID
iPhone ──AWDL/QUIC─▶ Linux + AR9271 (receivers)     serves that listenerUUID with that key
```

Both halves work because both are built from **one SNAP identity**. The bind is
cryptographic, not physical, so the device answering the NFC does not have to be the one
answering the QUIC.

**Tested on:** Pixel 9 (Android 16 and 17), unrooted, as an ordinary app. iPhones on iOS 26.6
and 26.6.1, two different handsets and Apple IDs, bidirectional contact exchange confirmed.

## Requirements

- **Android 15+ (API 35)** with NFC host card emulation.
- An NFC controller that implements AOSP's polling-loop annotation
  (`READER_TECH_A_POLLING_LOOP_ANNOTATION`). The Pixel 9's does. Other phones are untested:
  if the annotation is ignored, the phone is still a selectable card but iOS reads it as a
  plain NFC tag, and NameDrop never starts.
- The Android SDK + a JDK 17 to build (Android Studio's bundled JBR works), and `adb`.

## 1. Mint the identity and generate the app's blobs

Run this **on the Linux receiver**, from the repo root. It keeps the private half there:

```bash
.venv/bin/python scripts/build-snap-serverinfo.py --header ""
```

`--header ""` skips writing into a Proxmark3 checkout. The script writes:

| Output | Used by |
|---|---|
| `android/namedrop-card/app/src/main/java/com/namedrop/card/SnapBlobs.java` | this app (gitignored) |
| `scratchpad/snap-identity.json` | `mdns-advertise.py`, `asquic-receiver.py`, the TLS cert |

If you build the APK on a different machine, copy `SnapBlobs.java` across. **Rebuild and
reinstall the app, and re-run `mint-snapkey-cert.sh`, whenever you run with `--regenerate`.** The app bakes in the identity; if it
drifts from the receiver's, iOS resolves a UUID nobody is serving. The app shows its
`listenerUUID` on screen, so you can check it against `snap-identity.json`.

## 2. Build and install

```bash
cd android/namedrop-card
export ANDROID_HOME=~/Android/Sdk        # or ~/Library/Android/sdk on a Mac
./gradlew assembleDebug
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

The build fails with `cannot find symbol SnapBlobs` until step 1 has run.

## 3. Bring up the Linux receivers

Same as the Proxmark3 setup: make the cert carrying SNAP key 1
(`./scripts/mint-snapkey-cert.sh`), then bring up AWDL,
`mdns-advertise.py` and `asquic-receiver.py`, and run `receiver-preflight.py` until it
passes. See steps 3 and 4 of the [main README](../../README.md#running-a-namedrop).

## 4. Bump

**Android phone:**
- NFC on. Open **NameDrop Card** and **leave it in the foreground**. The status line should
  read `boop AID routed to us=yes`.
- The app is only a card while it is on screen. Leaving it hands card routing back to Google
  Wallet and stops ECP emission. The app keeps the screen on by itself.

**iPhone:**
- Unlocked, AirDrop set to **Everyone**, on the **home screen with no share sheet up**. A share
  sheet makes the iPhone the *sender*, which is a different flow.

**The tap:**
- Line the iPhone's top edge up with the Android phone's **NFC antenna**. On a Pixel that is
  **mid-body on the back**, not at the top like an iPhone's. Other phones vary.
- I found it works best with the **Pixel on top**: iPhone face-up on the table, Pixel's back
  lowered onto its top edge.
- **Tap, don't park.** Touch, lift a few cm, and come back every 3–5 s. The phone is only
  selectable in the 2 s quiet window between 0.5 s emission bursts.
- When the NameDrop prompt appears, tap **Share**.

Healthy output in `adb logcat -s NDCARD` looks like this:

```
[SNAP] SELECT com.apple.boop -- PEER FLOW
[SNAP] iOS pushed its ServerInfo (238 B) -> ours (238 B), echoing com.apple.boop.SNAP
[SNAP] capabilities push (85 B) -> redirect 6A88 + session id
[SNAP] SELECT com.apple.boop -- PEER FLOW
[SNAP] iOS RESUMED our session -> capabilities blob (85 B)
```

That is one full handshake: two boop selects, one ServerInfo, one resume. After that the
NFC half is done, and `asquic-receiver.py` should log `/Ask` → `/Exchange` within seconds.

## Troubleshooting

| Symptom | Cause |
|---|---|
| Only `D2760000850101` selects and `ndefRefused` climbing, no `boop` | iOS is reading you as a tag: it never heard the ECP frames. Usually geometry (try mid-body), or a phone whose NFC controller ignores the annotation. A few NDEF refusals *between* boop selects are normal on the home screen. |
| ServerInfo label is `com.apple.airdrop.sharesheet` (248 B) | The iPhone had a share sheet up. Go back to the home screen. |
| Handshake completes, iPhone shows "Waiting", then "Error Sharing" | The Linux side is not serving. Most often the AR9271 re-enumerated on USB and the receivers are bound to a dead `awdl0`. Restart `awdl-up.sh` and both receivers, then re-run `receiver-preflight.py`. Also check that the app's `listenerUUID` matches `snap-identity.json`. |
| `boop AID routed to us=NO` | Another app holds the preferred service. Make sure this app is in the foreground, and that NFC is on. |
| "stray tag swallowed" in the log | Something readable (a Proxmark3, a card) is in the field. Move it away. |
| `INSTALL_FAILED_UPDATE_INCOMPATIBLE` | Installed before from another machine's debug keystore. `adb uninstall com.namedrop.card` first. |

## How it works, briefly

- **`EcpEmitter`** puts the ECP frame in reader mode's polling-loop annotation. An
  unprivileged app can't emit and listen as a card in one call, because that configuration
  (`flags 0x1000`, tech mask 0) is privilege-gated. So it **toggles**: 500 ms of reader mode
  (`flags 0x1181`) to emit, then 2000 ms with reader mode off to act as a card. NameDrop-TCI
  and AirDrop-TCI frames alternate across bursts, because only one annotation slot works. The
  NameDrop payload rotates each burst, because iOS debounces a repeated frame. The controller
  appends CRC_A itself.
- **`NameDropHceService`** claims the boop AID prefix `A000000858` and refuses NFC Forum Type 4
  reads with `6A82`, just as the Proxmark3 firmware does. Answering Background Tag Reading
  lets iOS finish as a tag read and never escalate to NameDrop.
- **`SnapApplet`** is a straight port of the handshake in `firmware/hf_namedrop.c`. It has no
  Android imports, so it can be replayed on a desktop JVM. It echoes the reader's service
  label and timestamp, as a real card does.
- **`MainActivity`** holds the preferred-service slot and foreground dispatch. It also never
  starts an emission burst while an ISO-DEP session is in flight, because raising our own field
  would tear the handshake down.
