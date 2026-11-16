# `awdl-root` — NameDrop on a rooted Pixel, no Linux

With root, the Pixel can do both halves itself: the `namedrop-card` app answers the NFC bump,
and these tools run the AWDL/QUIC half on the phone's own radio. No AR9271, Linux machine or
Proxmark3.

```
iPhone ──NFC bump──▶ Pixel: namedrop-card app (foreground)     SNAP: key 1 + listenerUUID
iPhone ──AWDL/QUIC─▶ Pixel: mosey0 + MdnsAnnounce + NameDropReceiver (root)
```

**Tested on:** Pixel 9, rooted with Magisk v31.1, against a real iPhone. Full bidirectional
exchange: the iPhone's card was received and ours showed up on the iPhone.

## Why this works

Google already ships AWDL on the Pixel, for Quick Share's AirDrop interop. The stack is called
`mosey`: `mosey_server` drives the radio and exposes the link as `mosey0`. Ordinary apps can't
reach it (`IMoseyService` only accepts Google's privileged app, and a listener on `mosey0` can't
send replies). As root:

- **`MoseyStart`** calls `IMoseyService.start()` directly, with the same arguments Google's app
  sends when a Quick Share session opens (captured with Frida). `mosey0` comes up on channel 149
  with Quick Share closed, and stays up for as long as our process holds the death-token Binder.
- **`MdnsAnnounce`** multicasts `<listenerUUID>._asquic._udp` from port 5353 on `mosey0` every
  2 s. `mosey_daemon` turns that into an AWDL service response. Without it the bumped iPhone joins
  the link but sends only sync frames; with it, the iPhone opens QUIC to us within tens of ms.
- **`namedrop-pixel.sh`** adds an IPv6 rule pointing `mosey0` at its routing table. A link
  raised this way has no Android `Network` behind it, so without the rule every reply fails
  with `ENETUNREACH` and the iPhone ends in "Error Sharing".
- **`NameDropReceiver`** (in the `namedrop-card` APK, `h3/`) is the QUIC/HTTP-3 server, a Java
  port of `scripts/asquic-receiver.py` on Kwik + Flupke. See [Receiver notes](#receiver-notes).

## Requirements

- A Pixel 9 (other Pixels with `mosey` probably work, untested), bootloader unlocked and rooted
  with Magisk. **Unlocking the bootloader wipes the phone.** Leave automatic system updates off
  while patched, and never relock the bootloader with a patched image.
- In Magisk → Superuser, grant root to **Shell**. Magisk may not prompt; without the grant
  `adb shell su` just fails.
- A computer with `adb`, the Android SDK, a JDK 17, and Python 3 with `cryptography`
  (for the identity and cert scripts). It doesn't have to be Linux.

## Steps

From the repo root.

**1. Identity and cert.** Same as the other setups:

```bash
python3 -m pip install cryptography    # or use the .venv that setup-linux.sh creates
python3 scripts/build-snap-serverinfo.py --header ""
./scripts/mint-snapkey-cert.sh
```

**2. Build and install the app**, including the patched Flupke jar the receiver needs:

```bash
./android/namedrop-card/flupke-patch/build.sh
cd android/namedrop-card && ./gradlew assembleDebug && adb install -r app/build/outputs/apk/debug/app-debug.apk && cd -
```

**3. Bring up the AWDL side on the phone:**

```bash
./android/awdl-root/pixel-up.sh
```

It builds `awdl-root.dex` if needed, pushes it with the TLS cert, key and `samples/contact.vcf`
to `/data/local/tmp/namedrop/`, and starts `namedrop-pixel.sh` as root. It should print
`up: mosey0 fe80::...`. The link stays up for `HOLD` seconds (default 600); re-run to extend.

**4. Bump.** Open **NameDrop Card** on the Pixel and keep it in the foreground, then bump as
described in [the app's README](../namedrop-card/README.md#4-bump). **Keep Quick Share closed:**
when it is in the foreground (split screen counts), Google Play services takes the NFC card
slot away from our app and answers the bump itself.

**5. Collect and stop:**

```bash
./android/awdl-root/pixel-up.sh --pull    # received cards -> received/
./android/awdl-root/pixel-up.sh --stop
```

Logs are in `/data/local/tmp/namedrop/` (`receiver.log`, `announce.log`, `mosey.log`,
`state.log`); read them with `adb shell su -c 'cat /data/local/tmp/namedrop/receiver.log'`.
A good exchange logs `QUIC HANDSHAKE COMPLETED`, then `/Hello`, `/Ask`, `/Exchange` and
`RECEIVED CONTACT CARD`.

| Variable | Default | |
|---|---|---|
| `HOLD` | `600` | Seconds to hold `mosey0` up. |
| `COUNTRY` | the phone's Wi-Fi country | Passed to `mosey` before `start`, as Google's app does. |
| `VCARD` | `samples/contact.vcf` | The card we send back. |
| `ID`, `KEYS` | `scratchpad/...` | As in `mint-snapkey-cert.sh`. |

The TLS private key is left on the phone in `/data/local/tmp/namedrop/`, readable by root and
the shell user. Delete the folder when you're done if that matters to you.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `mosey0 did not come up` | Quick Share or another Google session already holds the link (`mosey.log` says the native handle is already allocated). Close Quick Share and retry. |
| Handshake completes on NFC, iPhone shows "Waiting", then "Error Sharing" | Check `receiver.log`. No QUIC lines at all: the announcement isn't going out (`announce.log`). QUIC Initials repeating with no handshake: the routing rule is missing (`ip -6 rule` should show `pref 9999`). |
| Only NDEF selects, no boop, while Quick Share is open | Quick Share took the card. Close it. |
| It takes many taps | Expected for now. The unrooted card puts the ECP frame on the air for about 0.5 s of every 2.5 s, and a tap outside that window reads as a plain tag. Doing emission and card emulation at once needs privilege the app doesn't use yet. |

## Receiver notes

`NameDropReceiver` runs on Kwik 0.12.1, Agent15 4.0 and Flupke 0.9.4. Three things had to change
for it to work on Android with an iPhone:

- **EC certificate.** Agent15 only loads RSA keys from PEM, and the SNAP key is P-256, so the key
  and cert go in through a PKCS12 `KeyStore`.
- **X25519.** The iPhone's ClientHello uses X25519. Agent15's implementation needs JDK APIs
  (`NamedParameterSpec`, `XECPublicKey`) that Android's Conscrypt doesn't provide, so
  `AndroidKeyExchangeFactory` replaces it with Conscrypt's own `XDH`.
- **No `:authority`.** iOS sends HTTP/3 requests without `:authority` or `Host`. Upstream Flupke
  rejects those as malformed; `patches/flupke-0.9.4-no-authority.patch` relaxes the check, the
  same way `asquic-receiver.py` relaxes aioquic's.

The `java.lang.management`, `javax.management` and `java.net.http` classes under `app/src/main/java`
are small stand-ins for JDK APIs that Kwik and Flupke reference and Android lacks. Kwik 0.12.1
(rather than the 0.10.x Flupke was built against) is needed because the iPhone's post-quantum
key share splits its ClientHello across two Initial packets.
