//-----------------------------------------------------------------------------
// namedrop-re project, 2026.
// Copyright (C) Proxmark3 contributors. See AUTHORS.md for details.
//
// This program is free software: you can redistribute it and/or modify
// it under the terms of the GNU General Public License as published by
// the Free Software Foundation, either version 3 of the License, or
// (at your option) any later version.
//
// This program is distributed in the hope that it will be useful,
// but WITHOUT ANY WARRANTY; without even the implied warranty of
// MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
// GNU General Public License for more details.
//
// See LICENSE.txt for the text of the license.
//-----------------------------------------------------------------------------
// HF_NAMEDROP  --  an Apple NameDrop peer on one Proxmark3.
//
// GAP #1 (CLEARED, take 3, 2026-08-13): make an iPhone classify a non-Apple ISO-DEP
// card as a *peer* (boop) rather than a *tag*. nfcd's discriminator is `chFieldType`
// -- whether it saw an ECP2.0 Connection-Handover frame in its listen window (nfcd RE:
// chFieldType==1 && category==9 routes to the CH consumer, else background tag reading).
// Adding the NameDrop-type CH frame (TCI 01 00 01) to the burst flipped it: iOS then
// SELECTs the full 14-byte boop AID A0 00 00 08 58 04 4F 53 45 2E 43 48 2E 01.
//
// ⚠️ HOW MUCH OF THAT IS PROVEN. Take 2 (AirDrop-TCI only) got 0 boop SELECTs in 61
// activations; take 3 got 5 in 91 (Fisher two-tailed p ~ 0.083 -- suggestive, not
// significant, and one session per arm). But take 3 moved three knobs at once --
// ND_EMIT_NAMEDROP 0->1, ND_ANSWER_NDEF 1->0, ND_EMIT_COUNT 8->3 -- and take 2's own
// failure mode was the tag notification *overriding* the AirDrop UI, so refusing the
// NDEF read is a live alternative explanation. Untested, and worth an arm each:
//   (a) ND_EMIT_NAMEDROP=0 with take 3's other knobs (isolates the NameDrop frame),
//   (b) ND_ANSWER_NDEF=1 with ND_EMIT_NAMEDROP=1 (isolates the NDEF refusal),
//   (c) AirDrop before NameDrop (the frame ORDER has never been a variable).
// The nfcd RE names chFieldType as the gate but does NOT map TCI 01 00 01 -> 1 and
// 01 00 00 -> not-1, so the mechanism above is inference, not a read-off.
//
// GAP #2 (CLEARED): actually run the com.apple.boop.SNAP transaction, so iOS completes
// the handshake instead of deselecting us. The wire format is decoded
// byte-for-byte from two real iPhone<->iPhone bumps, and every expensive byte of our
// answer is pre-minted on the host into hf_namedrop_snap.h (the AT91SAM7S512 has no
// crypto accelerator and could not do P-256 + Ed25519 inside the 38.66 ms FWT):
//
//   00 A4 04 00 0E <boop AID> 00        -> A5 03 CE 01 01 + 9000
//   00 CA 01 03 EE <ServerInfo 238 B>   -> our ServerInfo 238 B + 9000
//   00 CA 01 03 55 <Capabilities 85 B>  -> {1:719, 2:session} + 6A88   (a redirect)
//   ... reader re-activates with a fresh UID ...
//   00 A4 04 00 0E <boop AID> 00        -> A5 03 CE 01 01 + 9000
//   00 CA 01 04 0A {0:"1.1",4:session}  -> the Capabilities blob + 9000
//
// BOTH sides' ServerInfo carries SNAP key 2 = an RFC 4122 v4 UUID = that side's own
// bonjourListenerUUID (4/4 valid across both real takes, p ~ 6e-8 by chance) -- unknown
// #3, in-band, exactly where sharingd's
// startNFCServerWithLocalIdentity:listenerUUID:remotePublicKey: says it should be.
//
// The direction that matters is OURS: iOS takes the UUID out of the ServerInfo we answer
// with, resolves <uuid>._asquic._udp, and opens QUIC to US (evidence takes
// snap-uuid-is-asquic-instance-20260814, asquic-quic-connect-20260814). We never connect
// back to the reader's UUID -- we scrape and log it because it is the same field in the
// other direction and it is what proved the field's meaning, not because the working path
// uses it. Key 1 is the other half of the handle: the P-256 SPKI iOS then demands to see
// in the _asquic TLS certificate.
//
// Card identity == a real iPhone: ATQA 0400, SAK 20, ATS 05 78 80 71 00, random
// 08-prefixed 4-byte UID (evidence take pm3-aid-probe, tag-vs-peer differential).
//-----------------------------------------------------------------------------
#include "standalone.h"
#include "proxmark3_arm.h"
#include "appmain.h"
#include "fpgaloader.h"
#include "util.h"
#include "dbprint.h"
#include "ticks.h"
#include "string.h"
#include "BigBuf.h"
#include "iso14443a.h"
#include "protocols.h"
#include "pm3_cmd.h"
#include "cmd.h"
#include "commonutil.h"
#include "hf_namedrop_snap.h"

// ---------------------------------------------------------------- emit tuning
// A real device-B answers a NameDrop ~93-103 ms later, so we wait, then emit a WIDE
// burst to cover the iPhone's listen-window jitter: predelay (field off) plus
// iso14443a_setup's 50 ms settle puts the first frame ~95 ms after detection.
#define ND_EMIT_PREDELAY_MS 45
#define ND_EMIT_COUNT      3    // per frame type (NameDrop batch, then AirDrop batch)
#define ND_EMIT_GAP_MS     10
// Debounce so we don't re-emit inside one poll window (iPhone NameDrop is ~1.5-6 s apart).
#define ND_EMIT_DEBOUNCE_MS 250

// Emit the NameDrop-type CH frame (TCI 010001) before the AirDrop one. This went in with
// the take-3 flash that cleared gap #1, and every completed bump has run with it on -- but
// it was never isolated from the other two take-3 changes (see the header). Turning it off
// IS the missing control arm; do it deliberately, as a take, not as a tidy-up.
#define ND_EMIT_NAMEDROP   1

// 0 = echo the iPhone's own 6-byte NameDrop payload back (the arm take 3 classified on;
//     whether the echo matters is itself untested).
// 1 = emit our own distinct payload instead. Reserved for the A/B that tests whether
//     echoing collides with the CH initiator/receiver tiebreaker; changing this changes
//     the arm gap #1 was proven on, so leave it at 0 unless running that test.
#define ND_PAYLOAD_OWN     0

// Refuse the NDEF read (6A82) so a "tag" notification cannot override the AirDrop path.
#define ND_ANSWER_NDEF     0

// What to answer the 1-byte I-block ("00") iOS sends ~200 ms after our SELECT response
// when it has nothing to push (observed 5/5 in take 3, spread 1.6 ms -- a fixed timeout,
// not computation). 0 = 6A82 (take-3 control), 1 = our ServerInfo + 9000, 2 = bare 9000.
// SETTLED: 1 is the arm the completed bumps ran on; 0 and 2 are kept as control arms.
#define ND_PROBE_ACTION    1

// How long to sit on the boop SELECT response, in ms. A real iPhone card takes 10.6-15.7 ms
// (4 samples, both bump takes); take 3 answered in 0.92 ms and iOS then stalled 200 ms. That
// is a systematic difference and it is free to remove -- FWT is 38.66 ms, so 10 ms is well
// inside budget. Set to 0 to reproduce take 3's timing exactly.
// SETTLED: 10 ms is the arm the completed bumps ran on. The two knobs stay independently
// observable: iOS stalling logs [PROBE], iOS pushing a ServerInfo fires [SNAP].
#define ND_SELECT_DELAY_MS 10

// The iPhone's NameDrop ECP poll: config 89, TCI 01 00 01. Match the 8-byte header;
// the 6-byte payload and CRC that follow are per-session.
static const uint8_t ecp_namedrop_prefix[8] = { 0x6a, 0x02, 0x89, 0x05, 0x00, 0x01, 0x00, 0x01 };

// The AirDrop-TCI ECP frame we emit: config 89, TCI 01 00 00, all-zero data.
// AddCrc14A() fills [14],[15] -> 95 25 (hardware-proven, byte-identical to a real peer's).
static uint8_t ecp_airdrop[16] = {
    0x6a, 0x02, 0x89, 0x05, 0x00, 0x01, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00
};

// The NameDrop-TCI CH frame we emit. Bytes [8..13] are the 6-byte payload, filled at
// emit time; AddCrc14A fills [14],[15].
static uint8_t ecp_namedrop_tx[16] = {
    0x6a, 0x02, 0x89, 0x05, 0x00, 0x01, 0x00, 0x01,
    0xc0, 0xff, 0xee, 0xbe, 0xef, 0x69, 0x00, 0x00
};

// SELECT-AID prefixes we score.
static const uint8_t apple_aid_prefix[5] = { 0xA0, 0x00, 0x00, 0x08, 0x58 }; // com.apple.boop / SNAP
static const uint8_t ndef_aid_prefix[5]  = { 0xD2, 0x76, 0x00, 0x00, 0x85 }; // NFC Forum Type 4

// What a real boop card answers to the Apple-AID SELECT. Byte-identical to the real
// card's frame in both bump takes, CRC included -- confirmed, not assumed.
static const uint8_t apple_select_resp[] = { 0xA5, 0x03, 0xCE, 0x01, 0x01, 0x90, 0x00 };

// Minimal NFC-Forum Type-4 tag, kept so a background-tag-reading probe stays
// interpretable when classification falls back to NDEF.
static const uint8_t t4t_cc[] = {
    0x00, 0x0F, 0x20, 0x00, 0xFF, 0x00, 0xFF, 0x04, 0x06, 0xE1, 0x04, 0x00, 0xFF, 0x00, 0x00
};
static const uint8_t t4t_ndef[] = {
    0x00, 0x10, 0xD1, 0x01, 0x0C, 0x55, 0x04, 0x65, 0x78, 0x61, 0x6D, 0x70, 0x6C, 0x65, 0x2E,
    0x63, 0x6F, 0x6D
};
enum { NDF_NONE = 0, NDF_CC = 3, NDF_NDEF = 4 };

// The reader's ServerInfo map(6) opens `A6 00 44` = map(6), key 0, bytes(4). Everything
// after it sits at a fixed offset, so we can lift the peer's fields without a CBOR parser.
#define SI_ANCHOR_LEN 3
static const uint8_t si_anchor[SI_ANCHOR_LEN] = { 0xA6, 0x00, 0x44 };
#define SI_OFF_TS     3    // bytes(4)  CFAbsoluteTime
#define SI_OFF_UUID 103    // bytes(16) after `02 50`  <- the bonjourListenerUUID
#define SI_OFF_TOK  121    // bytes(6)  after `03 46`

// What we lifted out of the peer's ServerInfo (printed after the response is on the air,
// never inside the FWT window).
static uint8_t peer_uuid[16];
static uint8_t peer_ts[4];
static bool    peer_info_valid = false;
static bool    peer_info_pending = false;

// The service name the reader offered, echoed back verbatim in our ServerInfo. A share-sheet
// AirDrop offers `com.apple.airdrop.sharesheet`; a NameDrop contact exchange offers
// `com.apple.boop.SNAP`. That string is the ONLY difference between the 248/95 and 238/85
// payload shapes, so answering with the wrong one is a service mismatch.
static uint8_t peer_label[64];
static uint16_t peer_label_len = 0;
static bool    peer_label_pending = false;

// The reader's capabilities push, returned verbatim when it resumes the session -- which is
// exactly what a real card does, and is automatically right for either flow.
static uint8_t caps_buf[160];
static uint16_t caps_len = 0;

void ModInfo(void) {
    DbpString("  HF - NameDrop peer: ECP-emitting ISO-DEP card + com.apple.boop.SNAP applet (namedrop-re)");
}

// Find `needle` in `hay`; -1 if absent.
static int mem_find(const uint8_t *hay, uint16_t hlen, const uint8_t *needle, uint16_t nlen) {
    if (nlen == 0 || hlen < nlen) return -1;
    for (uint16_t i = 0; i + nlen <= hlen; i++) {
        if (memcmp(hay + i, needle, nlen) == 0) return (int)i;
    }
    return -1;
}

// Lift the peer's timestamp + bonjourListenerUUID out of its ServerInfo. Returns the
// offset of the map(6) anchor, or -1. Cheap: one memcmp scan, no allocation.
static int snap_scrape_serverinfo(const uint8_t *body, uint16_t blen) {
    int a = mem_find(body, blen, si_anchor, SI_ANCHOR_LEN);
    if (a < 0 || (uint16_t)(a + SI_OFF_TOK + 6) > blen) return -1;
    // Verify the fixed sub-tags before trusting the offsets.
    if (body[a + 7] != 0x01 || body[a + 8] != 0x58 || body[a + 9] != 0x5B) return -1;
    if (body[a + 101] != 0x02 || body[a + 102] != 0x50) return -1;
    memcpy(peer_ts, body + a + SI_OFF_TS, 4);
    memcpy(peer_uuid, body + a + SI_OFF_UUID, 16);
    peer_info_valid = true;
    peer_info_pending = true;
    return a;
}

// Raise the field, emit our ECP burst, drop the field.
// iso14443a_setup(READER_MOD) settles the field ~50 ms; ReaderTransmit sends the frame
// verbatim (parity added, NO CRC -- our frames already carry theirs).
static void emit_ecp_burst(const uint8_t *nd_payload6) {
#if ND_EMIT_NAMEDROP
#if ND_PAYLOAD_OWN == 0
    if (nd_payload6 != NULL) memcpy(ecp_namedrop_tx + 8, nd_payload6, 6);
#else
    (void)nd_payload6;
#endif
    AddCrc14A(ecp_namedrop_tx, 14);   // recompute over the (possibly new) payload
#else
    (void)nd_payload6;
#endif
    LED_D_ON();
    // Wait with the field still off so the burst lands in the iPhone's listen window,
    // not in its own poll/transition phase right after it emitted NameDrop.
    SpinDelay(ND_EMIT_PREDELAY_MS);
    iso14443a_setup(FPGA_HF_ISO14443A_READER_MOD);   // +50 ms field settle
#if ND_EMIT_NAMEDROP
    for (uint8_t i = 0; i < ND_EMIT_COUNT; i++) {    // announce the handover: NameDrop (010001)
        ReaderTransmit(ecp_namedrop_tx, sizeof(ecp_namedrop_tx), NULL);
        SpinDelay(ND_EMIT_GAP_MS);
    }
#endif
    for (uint8_t i = 0; i < ND_EMIT_COUNT; i++) {    // then "poll me": AirDrop (010000)
        ReaderTransmit(ecp_airdrop, sizeof(ecp_airdrop), NULL);
        SpinDelay(ND_EMIT_GAP_MS);
    }
    // Drop our field so the iPhone can raise its own and poll us as a card.
    FpgaWriteConfWord(FPGA_MAJOR_MODE_OFF);
    g_hf_field_active = false;
    LED_D_OFF();
}

void RunMod(void) {
    StandAloneMode();
    DbpString("");
    DbpString(_CYAN_(">>>") " HF NameDrop peer -- ECP-emitting card + boop/SNAP applet " _CYAN_("<<<"));
    DbpString("  Card params = a real iPhone: ATQA 0400, SAK 20, ATS 0578807100, UID 08..");
    DbpString("  Flow: hear iPhone NameDrop ECP -> emit NameDrop+AirDrop ECP -> boop AID -> SNAP");
    DbpString("  " _GREEN_("A000000858..") " = peer/boop   " _GREEN_("[SNAP]") " = the handshake is running   "
              _YELLOW_("D2760000850101") " = NDEF (tag)");
    DbpString("  Hold the button to exit; then 'trace list -t 14a' for the committed take.");
    DbpString("");

    FpgaDownloadAndGo(FPGA_BITSTREAM_HF);
    BigBuf_free_keep_EM();

    AddCrc14A(ecp_airdrop, 14);   // finalise the AirDrop frame -> CRC 95 25
#if ND_EMIT_NAMEDROP && ND_PAYLOAD_OWN
    AddCrc14A(ecp_namedrop_tx, 14);
#endif

    // ---- Card identity: exactly a real iPhone's activation parameters ----
    uint8_t uid[10]  = { 0x08, 0x1a, 0x66, 0x4f, 0, 0, 0, 0, 0, 0 };  // 08-prefixed, 4 bytes
    uint8_t ats[]    = { 0x05, 0x78, 0x80, 0x71, 0x00 };              // real iPhone ATS (no CRC)

    uint16_t flags = 0;
    FLAG_SET_UID_IN_DATA(flags, 4);
    flags |= FLAG_ATS_IN_DATA;
    flags |= FLAG_ATQA_IN_DATA;
    flags |= FLAG_SAK_IN_DATA;
    iso14a_set_atqa_sak_override(0x0400, 0x20);

    tag_response_info_t *responses = NULL;
    uint32_t cuid = 0;
    uint8_t pages = 0;
    if (SimulateIso14443aInit(11 /* ISO14443-4 JCOP base */, flags, uid, ats, sizeof(ats),
                              &responses, &cuid, &pages, NULL) == false) {
        DbpString(_RED_("SimulateIso14443aInit failed -- aborting"));
        SpinErr(15, 200, 3);
        LEDsoff();
        return;
    }

    // Our largest answer is the 238-byte ServerInfo: 1 PCB + 238 + 2 SW + 2 CRC = 243 B,
    // which fits the reader's FSD of 256 with no chaining. prepare_tag_modulation costs
    // ~9 bytes of modulation per response byte (8 data bits + parity), so 243 B needs
    // ~2.2 KB -- the old 1536-byte buffer would have silently refused to modulate.
    uint8_t *dyn_resp = BigBuf_calloc(320);
    uint8_t *dyn_mod  = BigBuf_calloc(2400);
    uint8_t *rxbuf    = BigBuf_calloc(512);   // reassembly across reader chaining
    if (dyn_resp == NULL || dyn_mod == NULL || rxbuf == NULL) {
        DbpString(_RED_("BigBuf alloc failed -- aborting"));
        SpinErr(15, 200, 3);
        LEDsoff();
        return;
    }
    tag_response_info_t dyn = { .response = dyn_resp, .response_n = 0,
                                .modulation = dyn_mod, .modulation_n = 0 };

    iso14443a_setup(FPGA_HF_ISO14443A_TAGSIM_LISTEN);

    uint8_t receivedCmd[MAX_FRAME_SIZE] = { 0x00 };
    uint8_t receivedCmdPar[MAX_PARITY_SIZE] = { 0x00 };
    int len = 0;
    bool odd_reply = true;

    uint32_t n_namedrop = 0, n_emit = 0, n_select = 0, n_apple = 0, n_ndef = 0;
    uint32_t n_snap_si = 0, n_snap_caps = 0, n_snap_resume = 0, n_probe = 0;
    uint32_t last_emit = 0;
    uint8_t cur_file = NDF_NONE;
    uint16_t rxlen = 0;          // bytes of a chained APDU reassembled so far

    clear_trace();
    set_tracing(true);
    LED_A_ON();

    for (;;) {
        WDT_HIT();

        tag_response_info_t *p_response = NULL;
        uint8_t resp_delay_ms = 0;      // matched-latency hold before we answer

        if (GetIso14443aCommandFromReader(receivedCmd, sizeof(receivedCmd), receivedCmdPar, &len) == false) {
            break;
        }

        // ---- ECP NameDrop detection (the iPhone just polled -> it will now listen) ----
        if (len >= 8 && memcmp(receivedCmd, ecp_namedrop_prefix, 8) == 0) {
            n_namedrop++;
            if (last_emit == 0 || GetTickCountDelta(last_emit) > ND_EMIT_DEBOUNCE_MS) {
                const uint8_t *nd_payload = (len >= 14) ? &receivedCmd[8] : NULL;
                Dbprintf("[NAMEDROP] #%u  iPhone NameDrop ECP seen (len=%d) -> emit NameDrop+AirDrop ECP",
                         n_namedrop, len);
                emit_ecp_burst(nd_payload);
                n_emit++;
                last_emit = GetTickCount();
            }
            LED_A_ON();
            continue;
        }

        // ---- ISO14443-3 activation ----
        if (receivedCmd[0] == ISO14443A_CMD_REQA && len == 1) {
            odd_reply = !odd_reply;
            if (odd_reply) p_response = &responses[RESP_INDEX_ATQA];
            rxlen = 0;
        } else if (receivedCmd[0] == ISO14443A_CMD_WUPA && len == 1) {
            p_response = &responses[RESP_INDEX_ATQA];
            rxlen = 0;
        } else if (receivedCmd[1] == 0x20 && receivedCmd[0] == ISO14443A_CMD_ANTICOLL_OR_SELECT && len == 2) {
            p_response = &responses[RESP_INDEX_UIDC1];
        } else if (receivedCmd[1] == 0x70 && receivedCmd[0] == ISO14443A_CMD_ANTICOLL_OR_SELECT && len == 9) {
            p_response = &responses[RESP_INDEX_SAKC1];
        } else if (receivedCmd[0] == ISO14443A_CMD_PPS) {
            p_response = &responses[RESP_INDEX_PPS];
        } else if (receivedCmd[0] == ISO14443A_CMD_HALT && len == 4) {
            p_response = NULL;
            rxlen = 0;
        } else if (receivedCmd[0] == ISO14443A_CMD_RATS && len == 4) {
            Dbprintf("[RATS]     iOS activated us (RATS) -> ATS 0578807100");
            p_response = &responses[RESP_INDEX_ATS];
            rxlen = 0;

        } else {
            uint8_t pcb = receivedCmd[0];

            // ---- ISO14443-4 I-block (0b000x xx1x) ----
            if ((pcb & 0xE2) == 0x02 && len >= 3) {
                uint8_t hdr = 1;
                if (pcb & 0x08) hdr++;          // CID
                if (pcb & 0x04) hdr++;          // NAD
                int inf_n = len - hdr - 2;      // strip PCB/CID/NAD and CRC
                if (inf_n < 0) inf_n = 0;

                if ((uint16_t)(rxlen + inf_n) <= 512) {
                    memcpy(rxbuf + rxlen, receivedCmd + hdr, inf_n);
                    rxlen += inf_n;
                }

                if (pcb & 0x10) {
                    // Reader is chaining -- acknowledge with R(ACK) and wait for the rest.
                    dyn.response[0] = 0xA2 | (pcb & 0x01);
                    dyn.response_n = 1;
                    if (pcb & 0x08) {
                        dyn.response[0] |= 0x08;
                        dyn.response[1] = receivedCmd[1];
                        dyn.response_n = 2;
                    }
                    AddCrc14A(dyn.response, dyn.response_n);
                    dyn.response_n += 2;
                    if (prepare_tag_modulation(&dyn, 2400)) p_response = &dyn;
                    EmSendPrecompiledCmd(p_response);
                    continue;
                }

                // ---- a complete APDU is in rxbuf[0..rxlen) ----
                const uint8_t *apdu = rxbuf;
                uint16_t alen = rxlen;
                rxlen = 0;

                dyn.response_n = 0;
                uint8_t rhdr = 1;                       // our PCB (+ CID)
                dyn.response[0] = pcb & ~0x10;
                if (pcb & 0x08) {
                    dyn.response[1] = receivedCmd[1];
                    rhdr = 2;
                }

                // A degenerate APDU -- iOS's ~200 ms "nothing to push" probe (a lone 00).
                if (alen < 4) {
                    n_probe++;
                    Dbprintf("[PROBE]    iOS sent a %u-byte I-block (no APDU) -- action %d",
                             alen, ND_PROBE_ACTION);
#if ND_PROBE_ACTION == 1
                    memcpy(dyn.response + rhdr, snap_serverinfo, SNAP_SI_LEN);
                    if (peer_info_valid) memcpy(dyn.response + rhdr + SNAP_SI_TS_OFFSET, peer_ts, 4);
                    dyn.response[rhdr + SNAP_SI_LEN]     = 0x90;
                    dyn.response[rhdr + SNAP_SI_LEN + 1] = 0x00;
                    dyn.response_n = rhdr + SNAP_SI_LEN + 2;
#elif ND_PROBE_ACTION == 2
                    dyn.response[rhdr] = 0x90;
                    dyn.response[rhdr + 1] = 0x00;
                    dyn.response_n = rhdr + 2;
#else
                    dyn.response[rhdr] = 0x6A;
                    dyn.response[rhdr + 1] = 0x82;
                    dyn.response_n = rhdr + 2;
#endif
                } else {
                    uint8_t ins = apdu[1], p1 = apdu[2], p2 = apdu[3];
                    uint16_t lc = (alen > 4) ? apdu[4] : 0;
                    const uint8_t *body = apdu + 5;
                    uint16_t bodymax = (alen > 5) ? (alen - 5) : 0;
                    if (lc > bodymax) lc = bodymax;

                    switch (ins) {
                        case 0xA4: { // SELECT
                            if (p1 == 0x00) {   // SELECT EF by file id (Type-4: E103 CC / E104 NDEF)
                                if (lc >= 2 && body[0] == 0xE1 && body[1] == 0x03)      cur_file = NDF_CC;
                                else if (lc >= 2 && body[0] == 0xE1 && body[1] == 0x04) cur_file = NDF_NDEF;
                                else                                                    cur_file = NDF_NONE;
                                dyn.response[rhdr]     = cur_file ? 0x90 : 0x6A;
                                dyn.response[rhdr + 1] = cur_file ? 0x00 : 0x82;
                                dyn.response_n = rhdr + 2;
                                break;
                            }

                            n_select++;
                            bool is_apple = (lc >= 5 && memcmp(body, apple_aid_prefix, 5) == 0);
                            bool is_ndef  = (lc >= 5 && memcmp(body, ndef_aid_prefix, 5) == 0);

                            if (is_apple) {
                                n_apple++;
                                LED_C_ON();
                                DbpString(_GREEN_("*** [APPLE-AID] iOS SELECTED com.apple.boop -- peer flow ***"));
                                memcpy(dyn.response + rhdr, apple_select_resp, sizeof(apple_select_resp));
                                dyn.response_n = rhdr + sizeof(apple_select_resp);
                                resp_delay_ms = ND_SELECT_DELAY_MS;
                            } else if (is_ndef && ND_ANSWER_NDEF) {
                                n_ndef++;
                                cur_file = NDF_NONE;
                                dyn.response[rhdr] = 0x90;
                                dyn.response[rhdr + 1] = 0x00;
                                dyn.response_n = rhdr + 2;
                            } else {
                                if (is_ndef) {
                                    n_ndef++;
                                    DbpString(_YELLOW_("[NDEF]     iOS selected NDEF -> background tag reading (a tag)"));
                                }
                                dyn.response[rhdr] = 0x6A;
                                dyn.response[rhdr + 1] = 0x82;
                                dyn.response_n = rhdr + 2;
                            }
                        }
                        break;

                        case 0xB0: { // READ BINARY (Type-4)
                            const uint8_t *f = NULL;
                            uint16_t flen = 0;
                            if (cur_file == NDF_CC)   { f = t4t_cc;   flen = sizeof(t4t_cc); }
                            if (cur_file == NDF_NDEF) { f = t4t_ndef; flen = sizeof(t4t_ndef); }
                            uint16_t off = ((uint16_t)p1 << 8) | p2;
                            uint8_t le = (alen > 4) ? apdu[4] : 0;
                            if (f == NULL || off >= flen) {
                                dyn.response[rhdr] = 0x6A;
                                dyn.response[rhdr + 1] = 0x82;
                                dyn.response_n = rhdr + 2;
                            } else {
                                uint16_t n = flen - off;
                                if (le && n > le) n = le;
                                memcpy(dyn.response + rhdr, f + off, n);
                                dyn.response[rhdr + n]     = 0x90;
                                dyn.response[rhdr + n + 1] = 0x00;
                                dyn.response_n = rhdr + n + 2;
                            }
                        }
                        break;

                        case 0xCA: { // GET DATA -- the boop/SNAP transaction body
                            if (p1 == 0x01 && p2 == 0x03 && lc >= 150) {
                                // The reader pushed its ServerInfo. Mirror the shape with our
                                // own pre-minted key material, echoing its CFAbsoluteTime the
                                // way a real card does.
                                n_snap_si++;
                                int a = snap_scrape_serverinfo(body, lc);
                                DbpString(_GREEN_("*** [SNAP] iOS pushed its ServerInfo -- answering with ours ***"));

                                // Splice the reader's own protocol-string text item between our
                                // fixed head and tail, so we answer on the service it asked for.
                                // Key 1's value starts right after `A4 00 63 "1.1" 01`.
                                uint16_t lbl_len = 0;
                                if (lc > SNAP_SI_HEAD_LEN &&
                                        memcmp(body, snap_si_head, SNAP_SI_HEAD_LEN) == 0) {
                                    uint8_t t = body[SNAP_SI_HEAD_LEN];
                                    if (t >= 0x60 && t < 0x78) {
                                        lbl_len = 1 + (t & 0x1F);
                                    } else if (t == 0x78 && lc > SNAP_SI_HEAD_LEN + 1) {
                                        lbl_len = 2 + body[SNAP_SI_HEAD_LEN + 1];
                                    }
                                    if ((uint16_t)(SNAP_SI_HEAD_LEN + lbl_len) > lc) lbl_len = 0;
                                    if (lbl_len > sizeof(peer_label)) lbl_len = 0;
                                }

                                const uint8_t *lbl;
                                if (lbl_len > 0) {
                                    lbl = body + SNAP_SI_HEAD_LEN;
                                    memcpy(peer_label, lbl, lbl_len);
                                    peer_label_len = lbl_len;
                                    peer_label_pending = true;
                                } else {
                                    // Unparseable -- fall back to the label baked into our blob.
                                    lbl = snap_serverinfo + SNAP_SI_HEAD_LEN;
                                    lbl_len = SNAP_SI_LEN - SNAP_SI_HEAD_LEN - SNAP_SI_TAIL_LEN;
                                }

                                uint16_t n = 0;
                                memcpy(dyn.response + rhdr, snap_si_head, SNAP_SI_HEAD_LEN);
                                n += SNAP_SI_HEAD_LEN;
                                memcpy(dyn.response + rhdr + n, lbl, lbl_len);
                                n += lbl_len;
                                memcpy(dyn.response + rhdr + n, snap_si_tail, SNAP_SI_TAIL_LEN);
                                if (a >= 0) memcpy(dyn.response + rhdr + n + SNAP_SI_TAIL_TS_OFF, peer_ts, 4);
                                n += SNAP_SI_TAIL_LEN;
                                dyn.response[rhdr + n]     = 0x90;
                                dyn.response[rhdr + n + 1] = 0x00;
                                dyn.response_n = rhdr + n + 2;

                            } else if (p1 == 0x01 && p2 == 0x03) {
                                // The capabilities push. A real card answers {1:719, 2:session}
                                // with SW 6A88 -- a redirect, not an error: the reader comes back
                                // on a fresh activation with 00 CA 01 04 quoting the session id.
                                n_snap_caps++;
                                DbpString(_GREEN_("[SNAP]     capabilities push -> redirect (6A88 + session id)"));
                                // Keep it: a real card hands this exact blob back on resume.
                                if (lc > 0 && lc <= sizeof(caps_buf)) {
                                    memcpy(caps_buf, body, lc);
                                    caps_len = lc;
                                }
                                memcpy(dyn.response + rhdr, snap_redirect, SNAP_REDIRECT_LEN);
                                dyn.response[rhdr + SNAP_REDIRECT_LEN]     = 0x6A;
                                dyn.response[rhdr + SNAP_REDIRECT_LEN + 1] = 0x88;
                                dyn.response_n = rhdr + SNAP_REDIRECT_LEN + 2;

                            } else if (p1 == 0x01 && p2 == 0x04) {
                                // Resume: the reader quotes our session id back. A real card
                                // returns the capabilities blob verbatim with 9000.
                                n_snap_resume++;
                                DbpString(_GREEN_("*** [SNAP] iOS RESUMED our session (00 CA 01 04) ***"));
                                const uint8_t *cb = caps_len ? caps_buf : snap_caps;
                                uint16_t cl = caps_len ? caps_len : SNAP_CAPS_LEN;
                                memcpy(dyn.response + rhdr, cb, cl);
                                dyn.response[rhdr + cl]     = 0x90;
                                dyn.response[rhdr + cl + 1] = 0x00;
                                dyn.response_n = rhdr + cl + 2;

                            } else {
                                Dbprintf("[SNAP]     unexpected GET DATA P1=%02X P2=%02X Lc=%u", p1, p2, lc);
                                dyn.response[rhdr] = 0x6A;
                                dyn.response[rhdr + 1] = 0x82;
                                dyn.response_n = rhdr + 2;
                            }
                        }
                        break;

                        default:
                            dyn.response[rhdr] = 0x6A;
                            dyn.response[rhdr + 1] = 0x82;
                            dyn.response_n = rhdr + 2;
                    }
                }

            } else if (pcb == 0xC2 || pcb == 0xCA) {   // S-block DESELECT (no CID / CID)
                dyn.response[0] = pcb;
                dyn.response[1] = 0x00;
                dyn.response_n = 2;
                rxlen = 0;

            } else {
                dyn.response_n = 0;                    // unknown PCB -> stay silent
            }

            if (dyn.response_n > 0) {
                AddCrc14A(dyn.response, dyn.response_n);
                dyn.response_n += 2;
                if (prepare_tag_modulation(&dyn, 2400)) {
                    p_response = &dyn;
                } else {
                    Dbprintf(_RED_("[ERR] modulation buffer too small for a %u-byte answer"), dyn.response_n);
                }
            }
        }

        // Hold before answering, to sit where a real iPhone card sits inside the FWT window.
        if (resp_delay_ms) SpinDelay(resp_delay_ms);

        EmSendPrecompiledCmd(p_response); // NULL-safe

        // Printing is deferred to here, AFTER our answer is on the air, so a USB write can
        // never eat into the 38.66 ms FWT the ATS negotiates.
        if (peer_label_pending) {
            peer_label_pending = false;
            Dbprintf("[SNAP]     service the reader offered (echoed back in our answer), %u B:",
                     peer_label_len);
            Dbhexdump(peer_label_len, peer_label, false);
        }
        if (peer_info_pending) {
            peer_info_pending = false;
            DbpString(_GREEN_("[SNAP]     peer bonjourListenerUUID (SNAP key 2 = the AWDL handle):"));
            Dbhexdump(16, peer_uuid, false);
            DbpString("[SNAP]     peer CFAbsoluteTime (key 0, echoed back in our answer):");
            Dbhexdump(4, peer_ts, false);
        }
    }

    switch_off();
    set_tracing(false);

    DbpString("");
    Dbprintf("[SUMMARY]  NameDrop=%u  emits=%u  SELECTs=%u  APPLE-AID=%u  NDEF=%u",
             n_namedrop, n_emit, n_select, n_apple, n_ndef);
    Dbprintf("[SUMMARY]  SNAP: serverinfo=%u  caps=%u  resume=%u  probes=%u",
             n_snap_si, n_snap_caps, n_snap_resume, n_probe);
    if (n_snap_resume > 0) {
        DbpString(_GREEN_("VERDICT: GAP #2 -- the boop/SNAP handshake RAN TO RESUME. Take the AWDL leg."));
    } else if (n_snap_si > 0) {
        DbpString(_GREEN_("VERDICT: GAP #2 PROGRESS -- iOS pushed its ServerInfo and we answered."));
    } else if (n_apple > 0) {
        DbpString(_YELLOW_("VERDICT: gap #1 only -- boop AID selected, but iOS never pushed a ServerInfo."));
    } else if (n_ndef > 0) {
        DbpString(_YELLOW_("VERDICT: iOS selected NDEF only -- classified as a tag (timing/field tolerance)"));
    } else {
        DbpString(_YELLOW_("VERDICT: no SELECT captured -- void/geometry take (check warp + coupling)"));
    }
    DbpString(_CYAN_("Exit HF_NAMEDROP.") "  'trace list -t 14a' to dump the take.");
    SpinErr(15, 200, 3);
    LEDsoff();
}
