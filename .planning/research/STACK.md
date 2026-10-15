# Stack Research

**Domain:** NFC tap-in attendance (hardware + browser APIs) added to an existing Flask app
**Milestone:** v2.0 Tap-In (AttenDANCE, LaShelle's School of Dance)
**Researched:** 2026-10-14
**Confidence:** MEDIUM-HIGH (browser/iOS behavior and USB reader config verified against primary docs; product prices verified on vendor pages; Amazon listings could NOT be verified, see Shopping List)

Scope: only what the new features need. Flask, SQLAlchemy, SQLite, Fly, Tailwind, Alpine, service worker, SMTP and Twilio are already validated and not re-researched.

## Headline Decisions

1. **Chip: NTAG213, for both fobs and cards.** A 45-51 char URL needs about 50 bytes of NDEF; NTAG213 holds 144. Same price as NTAG215 at 200-pack volume, and it is the only chip GoToTags stocks in a plain ABS fob.
2. **Vendor: GoToTags (US, "Normally Stocked").** Buy 10 fobs + 10 cards first, test on a real iPhone and a real Android, spot-check with NXP TagInfo, then buy the bulk order.
3. **Fallback reader: ACS ACR1281U-C2 (about $41 at GoToTags).** Verified in the ACS manual to support 4/7/10-byte UIDs, hex or decimal, forward or reversed byte order, and a configurable terminator.
4. **No new Python dependencies.** Token generation is `secrets.token_urlsafe(12)` from the stdlib.
5. **Two hard limits to design around, both verified:**
   - iPhone background tag reading shows a notification the staff member must tap. A tap is tag-then-banner, not tag-only.
   - Safari on iOS has no Web NFC. iPhones cannot write tags from the web app, and iPads cannot read tags in the browser at all.

## Recommended Stack

### Tags (hardware)

| Item | Spec | Price (verified date) | Why |
|------|------|-----------------------|-----|
| ABS key fob, NTAG213 | GoToTags "NFC Key Fob - NTAG213 - Red" (SKU TRKBRTUL2M). 32 x 41 x 4 mm, 144 B, 7-byte UID, IP67. MOQ 10 | $0.51 @10, $0.43 @100, $0.35 @1,000 (2026-10-14) | Cheapest genuine-NXP fob from a US vendor with stock. Fob on a dance-bag zipper or lanyard suits ages 3 to 83. |
| White PVC card, NTAG213 | GoToTags "Glossy White PVC NFC Card - NTAG213 - Overlay". CR80 (85.5 x 54 mm), printable on select card printers | 200-pack $47.98 (about $0.24 each); single $0.30, as low as $0.20 (2026-10-14) | Cheaper than the NTAG215 version ($49.98 per 200-pack, $0.48 single). Staff can wear it on a lanyard or keep it in a phone case. |
| Sample order first | 10 fobs + 10 cards | about $9 | Prove iPhone + Android read/write and TagInfo "genuine" before spending $90. |

**Recommended quantity for 100-200 dancers: 200 cards + 100 fobs (300 tags), about $91 before shipping.** That is one tag per dancer at the top of the range, plus 100 spares for lost tags, siblings who want a second, staff, and failed writes. If enrollment is nearer 100, buy one 200-pack of cards and 50 fobs.

**Custom-printed (studio logo) cards:** offered, but not cheap or fast.
- GoToTags custom NFC cards: "Printed PVC NFC Card (NTAG213)" listed from $11.99 down to "as low as $0.55" at volume; lead time 15 to 20 business days; CMYK, silkscreen, hot stamp; NTAG213 is their default chip. MOQ and setup fee are not published, so request a quote at help@gototags.com or +1 844 632-8247. They state they can pre-encode, but do NOT say whether per-tag unique random URLs are supported. Ask explicitly; if not, encode in-house.
- Custom-printed ABS fobs: MOQ 500 (GoToTags 1-color $0.84 @500, 2-color $0.94 @500). Too many for this studio. Skip.
- DIY alternative: Brainstorm ID Supply sells 50 inkjet-printable white PVC cards with NTAG215 for $39.99 (about $0.80 each; needs a separate PVC card tray; inkjet only). Price taken from a search snippet because the page returned HTTP 403, so treat as MEDIUM-LOW. Only worth it if the studio wants branded cards and owns a compatible inkjet.
- Recommendation: ship v2.0 with blank white cards and plain fobs. Add a printed logo card later if the studio wants it. Printing is cosmetic and does not affect the code.

### Why NTAG213 over NTAG215 (and 216)

| Factor | NTAG213 | NTAG215 | NTAG216 |
|--------|---------|---------|---------|
| User memory (NXP datasheet) | 144 B | 504 B | 888 B |
| Approx. URL capacity | about 130 chars | about 480 chars | about 850 chars |
| GoToTags PVC card 200-pack | $47.98 | $49.98 | not listed |
| GoToTags PVC card single | $0.30 | $0.48 | n/a |
| Plain ABS fob at GoToTags | yes | no NTAG215 fob page found | no |
| iPhone background read / Android Web NFC | identical | identical | identical |

- **Capacity math:** `https://attenddance.fly.dev/t/` is 29 chars; a 12-byte token from `token_urlsafe(12)` is 16 chars; total 45 chars. As an NDEF URI record the `https://` prefix compresses to one byte, so the message is about 42 bytes plus 3 bytes of TLV framing, about 45 bytes. NTAG213 leaves about 100 bytes free. (Confidence HIGH on sizes from NXP datasheet; the byte count is my calculation.)
- **Price:** parity at volume, so cost does not decide it. Availability does: NTAG213 is the one that is stocked as a cheap plain fob.
- **Counterfeit exposure:** NTAG215 is the chip behind the Amiibo-cloning market, so "NTAG215 compatible" listings on marketplaces are a magnet for clones. NTAG213 is not immune (an NXP community post reports Amazon NTAG213 stickers showing "non-genuine" in TagInfo), but there is less pressure. This is an inference; the NXP post is the only evidence (LOW-MEDIUM).
- **Choose NTAG215 instead only if** you can't source NTAG213 fobs, or you later want a second record or a signature on the tag. Nothing in this milestone needs 504 bytes.
- **Do not buy NTAG216.** Larger, costlier, no benefit.
- **Do not buy "NTAG-compatible" or unbranded chips.** Buy from a vendor that names "NXP NTAG213" and is a known US reseller. Verify after delivery (below).

### Genuine-chip check (do this on the 10-tag sample, then spot-check 5 from each bulk box)

- Install **NXP TagInfo** (free, Android; iOS version exists) and scan a tag. Expect IC manufacturer "NXP", type NTAG213, and a passing originality signature.
- A valid signature is strong evidence, not proof (NXP and GoToTags both say so). NXP's guidance is to buy from authorized partners.
- Why it matters here: the UID is the identity for the USB-reader fallback. Clone chips can have a settable UID; genuine NTAG UIDs cannot be changed.
- Confidence: HIGH that the check exists; MEDIUM on how strongly it excludes clones.

### Tag security posture (decision for the roadmap)

NTAG21x is NOT cryptographic. Anyone with an Android phone can read a tag's URL and write the same URL onto another tag. Mitigations that fit this project:
- The URL carries only an opaque token, and `/t/<token>` requires a staff session, so a stranger's tap reveals nothing (matches the COPPA constraint).
- **Lock every tag after writing** with `NDEFReader.makeReadOnly()` (Chrome 100+) so a kid with a phone can't overwrite or erase it. Locking is permanent, which is fine because identity lives in the DB: a locked tag can be re-assigned to another dancer by changing the DB row.
- Cloning a friend's tag to fake their attendance is the residual risk. At a dance studio with staff watching the tap, accept it. If it ever matters, NTAG 424 DNA (GoToTags offers it) adds per-tap cryptographic URLs, but costs more, needs server-side verification code, and is out of scope for v2.0.
- Hostname warning: the hostname is baked into every locked tag. Pick the final domain BEFORE bulk encoding. If the studio might move off `attenddance.fly.dev` (for example to a custom domain), set that domain up first, or keep `fly.dev` serving a redirect for the life of the tags.

### Fallback USB keyboard-wedge reader

| Model | Price | Output | Verdict |
|-------|-------|--------|---------|
| **ACS ACR1281U-C2 NFC UID Reader** | $40.79 @1, $35.99 @100 (GoToTags, 2026-10-14). rfid.it lists about 55 EUR; ambiguous formatting, confirm | HID keyboard class, no driver. UID only. Optional Windows Configuration Tool v1.04 | **Buy this.** |
| ACS ACR1552U | not verified | Types NFC UIDs incl. NTAG21x; format set via Windows tool | Pricier multi-ISO model. Only if you want read/write too. |
| Generic "13.56MHz USB RFID keyboard reader" on marketplaces | cheap | Many are 4-byte-UID only, fixed 10-digit decimal output | Avoid: a 7-byte NTAG UID will not work. One listing explicitly says it does not read 7-byte UID cards. |
| ACS ACR122U | about $40 (not verified) | PC/SC, NOT a keyboard device. Needs a helper app to type UIDs | Not a wedge. Many cheap clones. Skip. |

**ACR1281U-C2 facts verified in the ACS reference manual v1.01 (HIGH):**
- Supports 4, 7 and 10-byte UIDs (so NTAG 7-byte works). The length option must be set to "4, 7, 10" (the manual shows a mode that is 4-byte only).
- Output: hex, decimal, or decimal byte-by-byte; upper or lower case.
- Byte order: default (UID byte 0 first) or reversed.
- Configurable character between bytes and character at the end (Enter, Tab, comma, and so on), or none.
- rfid.it lists OS support as Windows, macOS, Linux, Android. Config tool is Windows.
- Gotcha from the manual: separator and end characters are keyboard-layout dependent (the tool notes AZERTY limits). Use a US layout on the kiosk device.

**Recommended reader configuration:** hex, uppercase, 7-byte capable, default byte order, NO separator, Enter terminator. Result: 14 hex chars + Enter, for example `04A1B2C3D4E5F6`. Normalize server-side to uppercase hex with no separators. This matches what Web NFC reports once normalized (see below), so a tag enrolled on Android can be looked up from a wedge scan.

**Not verified (LOW):** whether the default factory format is already hex/forward. The manual shows how to set it but I did not find the factory default. Plan a 5-minute bench test with a real tag and the Configuration Tool before relying on it, and compare against the Web NFC `serialNumber`.

**iPad / Android tablet compatibility (MEDIUM, test before relying):**
- iPadOS has built-in support for standard HID keyboards, and a developer on the Apple forums reports an RFID reader in HID mode working on a USB-C iPad. The ACR1281U-C2 is USB-A, so an iPad needs a USB-C to USB-A adapter. I found no ACS statement that iPadOS is supported (rfid.it lists only Windows, macOS, Linux, Android), so treat iPad as untested.
- Android tablets: USB-C OTG with an HID keyboard normally works; ACS lists Android.
- A laptop (Mac, Windows, Chromebook) is the lowest-risk fallback host.
- The kiosk page needs an always-focused `<input>`; refocus on blur, submit on Enter, ignore keystrokes arriving slower than a typing threshold only if you want to reject human typing (optional).

### Browser APIs

| API | Status (verified 2026-10-14) | Notes for this project |
|-----|------------------------------|------------------------|
| **Web NFC (`NDEFReader`)** | Chrome for Android only. Shipped in Chrome 89; caniuse shows Chrome for Android 154 supported. No Safari (iOS) support through version 27.2 listed; Safari's position is "Opposed"; Firefox "Harmful". The spec is a W3C Community Group draft (not a standard). | Kiosk + enrollment on Android only. Chrome only, not Samsung Internet or Firefox. HIGH. |
| Secure context | HTTPS only, top-level frame only, no iframes | Fly serves HTTPS. HIGH. |
| Permission / gesture | Requires the `"nfc"` permission; `scan()`, `write()`, `makeReadOnly()` prompt if not yet granted. Chrome's docs say the permission must be requested while handling a user gesture (button tap). | Put a big "Start scanning" button on the kiosk page. Do not try to auto-start on load. HIGH (docs); the W3C spec text itself does not state a gesture rule, Chrome's docs do. |
| Foreground only | NFC is suspended when the page is hidden, the screen is off, or the device is locked. Pending writes abort with `AbortError`. | Kiosk must keep the screen on: use the **Screen Wake Lock API** (Chrome Android supported) and re-request on `visibilitychange`. Staff cannot lock the phone or switch apps while scanning. HIGH. |
| `scan()` | One persistent reader; fires a `reading` event per tag with `serialNumber` and `message.records`. `readingerror` fires when a tag is detected but unreadable. Pass an `AbortSignal` to stop. | Parse the `url` record, extract the token after `/t/`, `fetch()` a JSON endpoint. Do NOT navigate away; navigation would drop the scan. |
| `serialNumber` | ASCII hex bytes joined with `:` (spec: usually 4 or 7 numbers). Empty string if unavailable. Letter case is unspecified. | Normalize: strip `:`, uppercase. Gives the 14-char UID that matches the wedge reader. HIGH on format; case unverified so always normalize. |
| `write()` | `await ndef.write({records:[{recordType:"url", data:"https://..."}]}, {overwrite:true})`. Rejects with `NotAllowedError`, `NotSupportedError`, `NotReadableError`, `NetworkError` (tag moved away), `AbortError`. | Add a 5-10 second `AbortSignal` timeout. Surface `NetworkError` as "hold the tag still". Use `overwrite:false` on new tags to avoid clobbering an enrolled tag. |
| `makeReadOnly()` | Chrome 100+ on Android. Irreversible. Page must be visible and handling a gesture when the tag is tapped. | Not verified whether `write()` and `makeReadOnly()` can both complete on one physical tap or need two. Test on the sample tags. LOW on that detail. |
| Installed PWA | Not verified that Web NFC works inside a Chrome-installed PWA (WebAPK). | Run the kiosk in a normal Chrome tab for v2.0. Flag for phase testing. LOW. |

**Android also opens a tag URL natively** when no page is handling NFC. So `/t/<token>` must work as a plain GET page for staff on either platform; the kiosk page is an optimization that avoids a page load per tap.

### iOS (iPhone) background tag reading

Source: Apple Core NFC docs, "Adding Support for Background Tag Reading" (current 2026 copyright). HIGH.

- Requires **iPhone XS or later**.
- The system **shows a pop-up notification each time it reads a new tag; the user taps the notification** to act on it. If the phone is locked it prompts to unlock first. Not hands-free.
- Only reads while the iPhone is in use. Unavailable even with the display on when: the device has never been unlocked since boot; a Core NFC reader session is in progress; Apple Pay Wallet is in use; the camera is in use; Airplane mode is on.
- It looks for the first NDEF record with well-known type `U` (URI). HTTP/HTTPS URLs are supported. With no app associated to the domain (our case), the link opens in **Safari**.
- No user setting to turn it off is documented.
- **Safari on iOS has no Web NFC** (caniuse, through 27.2).

Consequences for the plan:
- iPhone check-in = hold phone to tag, tap banner, Safari opens `/t/<token>`, the server records the tap from the session cookie, the page shows the result. Two actions per dancer. Set staff expectations; at a busy arrival this is slower than Android continuous scan. Plan the UI so that the result page is fast, minimal, and auto-readable at arm's length.
- **Session cookie trap (MEDIUM, widely observed, I did not find an Apple doc):** Safari and a Home Screen web app have separate storage on iOS, so staff must be logged in in Safari itself, not only in the installed PWA. Use a long-lived session for staff accounts. Needs verification during the iPhone phase.
- **Tag-open by a logged-out phone** must show a login page that reveals nothing about the tag or student, and must not redirect to a URL containing the token in a way that leaks it to third parties (no external assets with referrer on that page).
- **iPhone cannot enroll or write tags from the web app.** Options: (a) designate one Android phone as the enrollment device (recommended; it also captures the UID), (b) use NXP TagWriter (free, iOS) to write a URL by hand, (c) pre-encode in bulk. For (c), the GoToTags Desktop App with a USB reader can encode; whether it batch-imports a CSV of unique URLs was not verified (LOW). Ask GoToTags before assuming.
- A faster iOS path exists in theory (Shortcuts NFC automation), but it binds to one specific tag per automation, which does not scale to 200 tags. Rejected.
- iPhone 7/8/X (pre-XS) cannot background-read at all. Staff with those phones need Android or the wedge reader.

### Server-side Python

**Add nothing.** `requirements.txt` stays as is.

| Need | Use | Notes |
|------|-----|-------|
| Token generation | `secrets.token_urlsafe(12)` (stdlib) | 96 bits, 16 chars, URL-safe. Unique index in the DB; retry on collision (practically never). Produces the 45-char URL above. |
| UID normalization | stdlib `str` ops | `uid.replace(":", "").upper()`; accept 8 (4-byte) or 14 (7-byte) hex chars. |
| Comparing secrets | not needed | Lookup is by unique-indexed token; the token alone grants nothing without a staff session. |
| Duplicate tap window | reuse the 5 second window from `rfid/service.py` | Move into a shared function; do not keep the polling loop. |
| Check-out / current class | reuse `Attendance.check_out_time`, `_find_current_class` logic | Port the logic into a service function that the HTTP endpoint calls. |
| Notifications | existing `app/email.py`, `app/sms.py` | No new dependency. |

**Remove from the milestone's deploy path:** the `mfrc522`, `RPi.GPIO`, `spidev` entries are only in the legacy and Pi requirement files, not in the production `requirements.txt`. Confirmed by reading the three files. Leave `rfid/reader.py` and `rfid/service.py` untouched or retire them in a cleanup phase; the polling loop is incompatible with Fly (no attached reader, one gunicorn worker).

**Integration points / data model hint (for the architecture step):** `Student.rfid_uid` is a single unique String(50) column. A student needs several tags over time (lost, spare, sibling), and a tag needs a token plus a UID. Prefer a new tag table (token unique, uid indexed, student_id, status active/lost/retired, created/locked timestamps) over overloading `rfid_uid`. Any stored tag UID or token tied to a child is a new identifier linked to a minor, so per the project PII rule update the model, `privacy.html`, and `/api/my-data` in the same commit.

## What NOT to Use

| Avoid | Why | Use Instead |
|-------|-----|-------------|
| `nfcpy`, `pyscard`, `mfrc522`, `RPi.GPIO` on the server | Fly cannot host an attached reader; the polling loop would also block the single gthread worker | Phones and the browser as readers; HTTP endpoint on the server |
| `ndeflib` or any NDEF parser | The server never sees raw NDEF. The browser extracts the URL | Parse the URL string with the stdlib |
| Flask-SocketIO, websockets, SSE for scan results | Extra memory on a 256 MB machine, and a normal `fetch()` response already returns the result | Plain JSON POST/GET |
| A native iOS/Android app, Capacitor NFC plugin, React Native NFC | Out of scope per PROJECT.md; staff want the existing web app | Web NFC on Android, native URL handling on iPhone |
| Web NFC polyfills or "universal NFC" JS libraries | They only work through a native shell on iOS; the web cannot read tags in Safari | Do not attempt iOS Web NFC |
| MIFARE Classic, 125 kHz EM4100 cards, "13.56MHz IC cards" with 4-byte UIDs | iPhones cannot read them (already in Out of Scope), and cheap readers that handle only 4-byte UIDs will reject NTAG | NTAG213 + a 7-byte-capable reader |
| NTAG424 DNA for v2.0 | Needs server-side CMAC verification code and costs more. Overkill for attendance | Revisit only if tag cloning becomes a real problem |
| Unlocked tags | Anyone can overwrite or wipe them with a phone | `makeReadOnly()` right after writing |
| Student ID, name, or UID inside the URL | Violates the COPPA/no-PII constraint | Opaque random token only |
| Cheap 4-byte-only USB wedge readers and ACR122U clones | Wrong output for NTAG, or not a keyboard device | ACR1281U-C2 |
| Buying unbranded "NTAG compatible" tags on a marketplace | Clone chips, unverifiable UIDs | GoToTags / named NXP reseller + TagInfo check |

## Alternatives Considered

| Recommended | Alternative | When to Use Alternative |
|-------------|-------------|-------------------------|
| NTAG213 | NTAG215 | Only if you cannot get NTAG213 fobs, or want headroom for a second record. GoToTags NTAG215 card is $49.98/200. |
| GoToTags | Adafruit "Clear NTAG213 Keychain Fob" (product 5459; 36 x 29 x 3.8 mm; $2.95 @1, $2.66 @10, $2.36 @100; in stock) | Tiny test batch from a US hobby retailer. About 5x the per-fob cost. The product page's image captions say "NTAG203", which conflicts with the NTAG213 title, so TagInfo it. |
| GoToTags | Shop NFC, ID-Enhancements, Eccel (UK), USmartcards (UK) | Custom branded or serialized encoding at volume. ID-Enhancements and Eccel advertise custom encoding/numbering; no pricing verified. UK vendors add shipping/duty. |
| GoToTags | eBay US sellers (for example a Georgia seller listing 50 NTAG215 fobs at $29.99) | Cheaper, but genuine-chip status unverified. LOW. |
| GoToTags | Amazon | See below. |
| ACR1281U-C2 | ACS ACR1552U | If you also want the reader to write tags from a desktop. |
| Wedge reader as fallback | Android phone running the Web NFC kiosk page | Prefer the Android phone whenever one is available: no hardware to buy, and it reads NDEF tokens as well as UIDs. |

### Amazon (US): not verified

I could not retrieve Amazon listings, so I cannot give an ASIN, price, or pack size I have actually seen. Generic search results surfaced only sticker packs, a 20-count NTAG213 sticker set, and listings from third-party sellers; none was a verified keyfob/CR80 pack. Honest guidance:
- Prefer GoToTags (prices verified above). If the studio insists on Amazon for speed, filter for the exact words "NXP NTAG213" and "keyfob" or "CR80 PVC card", read the one- and two-star reviews for "non-genuine" or "doesn't hold data", buy ONE pack, and run the TagInfo check before buying more.
- An NXP community post reports Amazon-bought NTAG213 stickers showing "non-genuine" in TagInfo. Amazon is not an NXP-authorized channel by default.
- Timeskey advertises genuine NTAG215 tags; one user suspected a clone batch but the evidence does not tie it to Timeskey. LOW.

## Installation

```bash
# Server: nothing to install.
# Token generation (stdlib):
python3 -c "import secrets; print(secrets.token_urlsafe(12))"

# Phones: free apps for testing and verification
#   NXP TagInfo (Android / iOS)  - originality check, UID, memory layout
#   NXP TagWriter (iOS)          - hand-write a URL to a tag from an iPhone
#   NFC Tools (Android / iOS)    - read what is on a tag
```

Kiosk page skeleton (Android Chrome), shown because the exact API shape is load-bearing:

```js
// Must run from a user gesture (Start button)
const reader = new NDEFReader();
const abort = new AbortController();
await reader.scan({ signal: abort.signal });
reader.onreading = ({ serialNumber, message }) => {
  const uid = serialNumber.replace(/:/g, "").toUpperCase();
  const rec = message.records.find(r => r.recordType === "url");
  const url = rec ? new TextDecoder().decode(rec.data) : null;
  // extract token after "/t/", POST to the tap endpoint with the CSRF header
};
// keep screen on; re-request after visibilitychange
let lock = await navigator.wakeLock.request("screen");
```

## Version Compatibility

| Item | Compatible With | Notes |
|------|-----------------|-------|
| Web NFC read/write | Chrome for Android 89+ | `makeReadOnly()` needs 100+. Current stable is far past both (caniuse lists 154). |
| iPhone background tag read | iPhone XS or later, iOS with Core NFC (current iOS 26) | Opens https URLs in Safari when no app claims the domain. |
| NTAG213 | iPhone XS+, Android NFC phones, ACR1281U-C2 | ISO 14443A, NFC Forum Type 2, 7-byte UID. |
| Chrome Android Web NFC vs Samsung Internet / Firefox | Not compatible | Staff Android phones must use Chrome. |
| USB wedge reader + iPad | HID keyboard class | Unverified for ACR1281U-C2 on iPadOS; needs USB-C to USB-A adapter. Test first. |

## Confidence Summary

| Claim | Confidence | Basis |
|-------|------------|-------|
| NTAG213/215/216 memory sizes | HIGH | NXP datasheet (via search summary + Wakdev) |
| iOS background read behavior, banner tap, blockers | HIGH | Apple Core NFC documentation |
| No Web NFC in Safari iOS | HIGH | caniuse (through 27.2) |
| Web NFC Chrome Android, HTTPS, visibility, gesture | HIGH | Chrome docs, W3C spec, MDN |
| `serialNumber` colon-hex format | HIGH | W3C Web NFC spec (case unspecified) |
| ACR1281U-C2 7-byte, hex/dec, order, terminator | HIGH | ACS reference manual v1.01 (text extracted) |
| GoToTags prices and specs | MEDIUM-HIGH | Vendor product pages, fetched 2026-10-14; prices change |
| Custom printed card MOQ/price | LOW | Not published; quote needed |
| Amazon products | NOT VERIFIED | Could not retrieve listings |
| iPad + ACR1281U-C2 | LOW | No vendor statement |
| write + makeReadOnly in one tap; Web NFC in installed PWA | LOW | Not documented; test |
| iOS Safari vs Home Screen app separate cookies | MEDIUM | Widely observed; no Apple doc found |

## Sources

- Apple, Adding Support for Background Tag Reading: https://developer.apple.com/documentation/corenfc/adding-support-for-background-tag-reading (HIGH)
- GoToTags, iOS Background NFC Tag Reading: https://gototags.com/?p=4363 (MEDIUM, corroborates Apple)
- caniuse Web NFC: https://caniuse.com/webnfc (HIGH for support matrix)
- Chrome for Developers, Interact with NFC devices: https://developer.chrome.com/docs/capabilities/nfc (HIGH; page shows an old "last updated" date, version notes cross-checked against caniuse)
- W3C Web NFC Community Group spec: https://w3c-cg.github.io/web-nfc/ (HIGH; read first 100k chars)
- MDN NDEFReader, write(), serialNumber: https://developer.mozilla.org/en-US/docs/Web/API/NDEFReader (HIGH; flags "limited availability")
- NXP NTAG213/215/216 datasheet: https://nxp.com/docs/en/data-sheet/NTAG213_215_216.pdf (HIGH); Wakdev summaries https://www.wakdev.com/en/knowledge-base/nfc-chips/nxp-ntag213.html (MEDIUM)
- GoToTags products: https://store.gototags.com/nfc-key-fob-ntag213-red/ , https://store.gototags.com/nfc-pvc-card-ntag215/ , https://store.gototags.com/nfc-pvc-card-ntag215-200-pack/ , https://store.gototags.com/nfc-tags/nfc-cards/blank-nfc-cards/ , https://store.gototags.com/nfc-tags/nfc-cards/custom-nfc-cards/ , https://store.gototags.com/custom-key-fob (MEDIUM-HIGH)
- GoToTags ACR1281U-C2: https://store.gototags.com/acs-acr1281u-c2-nfc-tag-uid-reader/ ; rfid.it listing https://rfid.it/en/acs/436-acr1281u-c2-nfc-uid-reader.html (MEDIUM-HIGH)
- ACS ACR1281U-C2 Reference Manual v1.01: http://www.acs.com.hk/download-manual/14166/REF-ACR1281U-C2-1.01.pdf (HIGH, sections 5.1 to 5.3 and 6)
- Adafruit NTAG213 clear keychain fob: https://www.adafruit.com/product/5459 (MEDIUM; caption inconsistency)
- GoToTags, NFC chip originality signature: https://gototags.com/help/nfc/chip/features/originality-signature ; NXP community, authorized partners and TagInfo: https://community.nxp.com/t5/NFC/Intro-to-NTAG-originality-check/m-p/1816232 (MEDIUM)
- Screen Wake Lock: https://developer.chrome.com/docs/capabilities/web-apis/wake-lock (MEDIUM)
- Apple Developer Forums, HID keyboard support on iPadOS: https://developer.apple.com/forums/thread/756763 (LOW-MEDIUM, forum)
- eBay / Brainstorm ID Supply listings (pricing only): LOW

---
*Stack research for: NFC tap-in attendance (AttenDANCE v2.0)*
*Researched: 2026-10-14*
