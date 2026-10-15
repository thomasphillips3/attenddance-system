# Project Research Summary

**Project:** AttenDANCE v2.0 Tap-In (NFC tap-in attendance)
**Domain:** NFC check-in/check-out for a kids' dance studio, added to an existing Flask/SQLite app on Fly.io
**Researched:** 2026-10-14
**Confidence:** MEDIUM-HIGH (codebase fit and hardware specs are solid; iPhone and Android runtime behavior still needs on-device confirmation)

## Executive Summary

Tap-In is a thin hardware layer on an app that already has most of what it needs. The tag is a dumb pointer: an NTAG213 holding one NDEF URL with a random token and no student data. The staff phone is the reader:

- iPhone XS+ reads the tag in the background and opens the URL in Safari.
- Android Chrome runs a continuous Web NFC kiosk page.
- A USB keyboard-wedge reader is the fallback. It identifies by chip UID.

No new Python dependencies, no new processes.

Build one framework-free `process_tap()` service in a new `app/tap/` package, called by thin adapters per input path. It sits on three new tables:

- `nfc_tags`: the credential, many per student, with a lifecycle.
- `tap_events`: an append-only ledger that drives in/out and idempotency.
- `tap_notifications`: a durable outbox.

`GET /t/<token>` never changes state. It renders a page whose JS does an idempotent POST. Non-staff sessions get a generic page with no token lookup. Parent notices go through the outbox and a single drain thread, never inline, because the Fly machine is 256 MB with one worker.

The main risks are behavioral:

- A toggle-style second tap turns double taps into false "left class" texts. Dedup must live in the DB, with a minimum dwell.
- The legacy `_find_current_class` "first class today" fallback must be rewritten, not reused.
- iPhone takes two actions (tag, then banner), and Safari does not share login with the Home Screen app.
- The hostname is baked into every locked tag, so it must be final before locking.
- SMS needs consent and Twilio 10DLC verification. Email first.

## Decisions Where Researchers Disagreed

| Topic | Decision | Why |
|---|---|---|
| Tag table name | `nfc_tags` (model `NfcTag`). Ledger `tap_events`, outbox `tap_notifications`, package `app/tap/` | The table holds the physical object; two of three docs used NFC |
| Token size | `secrets.token_urlsafe(16)`, store only `sha256(token)` | About 55 bytes NDEF, fits NTAG213's 144; 128 bits |
| Tardy auto-mark | Off by default (`tap_tardy_grace_minutes` Setting, 0 = off). Always store the real `check_in_time` | Teachers own T/A/E. Studio policy call |
| Check-in window | Start minus 30 min through class end (Settings). Check-out resolves from the open session, no window | Late pickups must work |
| Dedup and dwell | 10 s DB-backed dedup, plus a 5 min minimum dwell before a tap counts as check-out | Blocks accidents without trapping early pickups |
| Direction logic | From today's successful `tap_events`, not `Attendance` | Teachers pre-mark P before kids tap |
| Tag locking | Pilot tags unlocked. Lock production only after the hostname is final and reads are verified on iPhone, Android and USB. Lock is a separate explicit step | A wrong URL on a locked tag means a reorder |
| Notifications | Email on (household opt-out). SMS off studio-wide until 10DLC is verified, then explicit per-household opt-in, guardian numbers only | Cost and consent risk, about 400 texts per week |
| Back-to-back classes | Build behind a Setting: when class A ends and B's window is open, close A at its scheduled end (`rolled_over`, no departure text) and open B | Common pattern; confirm with studio |
| Phase split | 7 phases. Hardware sourcing runs as a parallel non-code track in Phase 1. Result UX and flags merge into the iPhone phase | The result card is the landing page |
| Privacy | Model, `privacy.html` and `/api/my-data` change in the same commit as any PII change. Final audit in the last phase | Standing PII rule |
| USB reader | Optional, not in the first order | The Android kiosk reads token and UID with no extra hardware |

## Key Findings

### Recommended Stack

Nothing added to `requirements.txt`. Detail in STACK.md.

- **NTAG213 for fobs and cards.** 144 B fits the URL. Same price as NTAG215, and the only chip GoToTags stocks in a plain ABS fob.
- **GoToTags (US), single vendor.** Names NXP NTAG213. Verify chips with NXP TagInfo before the bulk order.
- **Web NFC (`NDEFReader`), Chrome Android only.** Continuous read, write and `makeReadOnly`. Needs HTTPS, a user-gesture Start button, Screen Wake Lock and a foreground page.
- **iPhone background tag reading (XS+).** Opens the first NDEF URL record in Safari after a banner tap. Suppressed if the phone hasn't been unlocked since boot, or while Camera, Wallet, a Core NFC session or Airplane mode is active. No Safari Web NFC, so iPhones cannot write tags from the web app.
- **ACS ACR1281U-C2 (optional).** HID wedge, 7-byte UIDs. Configure hex, uppercase, no separator, Enter terminator. Avoid 4-byte-only wedges and the ACR122U, which is not a wedge.

### Expected Features

Detail in FEATURES.md.

**Must have (v2.0):**

- `NfcTag` table with assign, deactivate, replace and audit.
- Window/season/enrollment resolver with a no-class staff picker. The legacy fallback is deleted.
- DB debounce plus dwell. A second tap means check-out.
- One `Attendance` row per class per day. A tap flips absent or excused to present, tap-out sets `check_out_time`, and `check_in_method='tap'`.
- Full-screen color result: green in, blue out, amber attention, red unknown.
- Async parent notice, first name only, household opt-out.
- Neutral staff-only flags using the household-net balance. Never block a child.
- Unknown, lost and non-staff pages reveal nothing.
- Undo and re-open.

**Should have (v2.x):** coalesced sibling notices, per-class window overrides, missed-checkout report, staff-only photo.

**Defer (v3+):** guardian pickup tags, NTAG 424 DNA.

**Do not build:**

- Dollar amounts on the kiosk.
- Blocking check-in.
- Auto-marking non-tappers absent.
- Shared sibling tags.
- PII or UID in the URL.
- Fabricated check-out times.
- "Pickup verification" language.
- A parent live-location page.

### Architecture Approach

`process_tap(TapRequest) -> TapResult` has no Flask request and no `current_user`, and takes `occurred_at` as a parameter. It is called by `/api/tap` and `/api/tap/batch`. Routes go on the existing `api` blueprint so the parent write guard applies. Return JSON 401, not a redirect. Detail in ARCHITECTURE.md.

1. `app/tap/credentials.py`: mint, hash, normalize UID, resolve, lifecycle.
2. `app/tap/service.py`: dedup, resolver, state machine, one-transaction writes, undo.
3. `app/tap/outbox.py`: single daemon drain thread, re-kicked on boot, capped retries, taps more than 20 min late skipped.
4. `app/tap/flags.py`: balance (`calc_balance_bulk` household net) and waiver (an active template with no signature).
5. Templates and JS: standalone no-CDN landing, byte-identical generic page, no-PII cacheable station shell, IndexedDB queue.
6. Legacy `rfid/`: untouched until the last phase. `rfid_uid` is backfilled into `nfc_tags`; the column is never dropped.

### Critical Pitfalls

Full list in PITFALLS.md.

1. **Double tap becomes a false check-out text.** Fix: DB dedup, dwell, an explicit state machine and an idempotent `event_key`.
2. **GET with side effects.** Reload, restore and prefetch re-fire. `GET /t/<token>` only renders, and JS POSTs with a unique key.
3. **Legacy reuse.**
   - The fallback misattributes classes.
   - One column can't hold lost-tag history.
   - Tap-out must UPDATE the existing row. A unique day index exists in `app/migrations.py`, and the startup de-dupe DELETE keeps only the earliest row, so a second row would vanish.
4. **iPhone is not hands-free, and its login lives in Safari.** Fix: a device spike, a staff checklist, JSON 401 and login-then-resume via `next`.
5. **Notification fan-out and SMS compliance.** Inline `send_sms`/`send_email` blocks up to 15/20 s and would starve the 4 threads. Unregistered 10DLC is blocked (30034), 21610 must persist as an opt-out, and Gmail has a daily cap.

Also load-bearing:

- Hostname permanence.
- A byte-identical non-staff response.
- Studio-local naive datetimes.
- Legacy `rfid_uid` values are decimal, not hex.

## Shopping List

GoToTags prices fetched 2026-10-14, before shipping and tax. Amazon unverified; don't buy there without a TagInfo check.

| Item | Spec | Qty | Unit | Total |
|---|---|---|---|---|
| **Step 1: sample order** | | | | |
| ABS key fob, NTAG213 | GoToTags "NFC Key Fob - NTAG213 - Red", SKU TRKBRTUL2M, 32x41x4 mm, IP67, MOQ 10 | 10 | $0.51 | $5.10 |
| White PVC card, NTAG213 | GoToTags "Glossy White PVC NFC Card - NTAG213 - Overlay", CR80 | 10 | $0.30 | $3.00 |
| **Step 2: bulk (after samples pass)** | | | | |
| White PVC card, NTAG213 | 200-pack | 200 | $47.98/pack | $47.98 |
| ABS key fob, NTAG213 | same SKU | 100 | $0.43 | $43.00 |
| **Optional** | | | | |
| ACS ACR1281U-C2 USB wedge reader | HID keyboard, 7-byte UID | 1 | $40.79 | $40.79 |
| Custom-printed PVC cards | 15-20 business days, "as low as $0.55", MOQ/setup unpublished | quote | quote | help@gototags.com |

- About $9 for samples and about $91 bulk for 300 tags. At around 100 dancers: one 200-card pack plus 50 fobs.
- Fobs for the youngest (clip to the bag). Cards for teens, adults and staff.
- Skip: NTAG215/216, MIFARE Classic, 125 kHz, 4-byte readers, the ACR122U, "NTAG compatible" lots, printed fobs (MOQ 500), NTAG 424 DNA.
- Free apps: NXP TagInfo (originality), NXP TagWriter (iOS), NFC Tools.
- At least one Android NFC phone or tablet is required for enrollment. Scope a purchase if nobody has one.
- Ask GoToTags whether they pre-encode unique per-tag URLs from a CSV.
- Don't write or lock production tags until the hostname is final.

## Open Questions for the Studio

Owner decisions go to Wilkanda (short bulleted text). Setup and technical items go to Carollette.

1. How many dancers and staff need tags?
2. What phones do staff carry? Is at least one Android device available?
3. Where does tap-in happen (front desk vs classroom door)? Is cell signal OK at both community centers?
4. Do dancers take back-to-back classes?
5. Do you want tardy auto-marked? If so, after how many minutes?
6. Is a staff picker OK for taps outside class windows?
7. Email, text or both? Is parent opt-in acceptable for text?
8. Should departure notices for the youngest go out automatically, or after staff confirm?
9. Should adults and teens get notices?
10. Fob or card for the youngest?
11. Hostname: keep `attenddance.fly.dev` or move to a custom domain? This is Thomas's call, needed before locking.
12. Do you want a staff-only student photo, and does the photo release waiver cover it?
13. How long should raw tap history be kept? Proposed: 12 months.

## Implications for Roadmap

### Phase 1: Tap core and hardware gate

All the risky logic, no UI. The sample order and device spike run in parallel.

- Models and the `rfid_uid` backfill.
- `credentials.py` and `process_tap()`: resolver, dedup, dwell, state machine, rollover, undo.
- `POST /api/tap` (staff only, JSON 401).
- `check_out_time` in `attendance_to_dict`.
- `check_in_method='tap'` in the UI.
- Fix the stale `Attendance.__table_args__` comment.
- Non-code track: samples, TagInfo, phone matrix, Twilio 10DLC check.

### Phase 2: Tag management and enrollment

- Mint, activate, assign and revoke, plus the student-page UI.
- Android Web NFC write with `overwrite:false`, read-back verify, `pending` to `active`.
- Refuse to overwrite enrolled tags.
- Lost, replace and retire.
- Non-identifying labels.
- A no-tag roster.
- Move `has_rfid` consumers to tags.
- Pilot tags stay unlocked.

### Phase 3: iPhone URL flow, result screen and staff flags

- Side-effect-free `GET /t/<token>`, standalone landing and generic pages.
- Login-then-resume.
- `sw.js` shows "NOT RECORDED" when offline.
- Result states, no-class picker, undo/re-open.
- Neutral flags.
- Dashboard moved to `TapEvent`.
- Fly `min_machines_running` decision.
- Staff checklist.

### Phase 4: Parent notifications

- Outbox, drain thread, boot recovery, retries, stale suppression.
- Email on, SMS gated.
- Household opt-out with privacy and my-data updated in the same commit.
- 21610 recorded as an opt-out.
- Kill switch and daily cap.

### Phase 5: Android kiosk and offline queue

- `/tap` station shell.
- Continuous Web NFC with a Start button.
- Wake Lock.
- One reader with an AbortController.
- Tones and haptics, status chip.
- IndexedDB queue, `/api/tap/batch`, `/api/tap/ping`.
- Bounded server-side time conversion.

### Phase 6: USB wedge station

- Document-level keydown burst capture.
- 8 or 14 hex normalization.
- Reader config doc and bench test.

### Phase 7: Legacy removal, rollout and audit

- Remove `rfid/`, `/api/rfid/*`, `assign-rfid`, `remove-rfid`, `RFID_*` config, Pi requirements, and rewrite the README. Edit the smoke audit to match.
- Keep `RFIDLog` and `rfid_uid` as history.
- Privacy and retention audit, training copy.
- 2-3 week pilot, then bulk order, lock and hand-out.

### Research Flags

- **Phase 2:** does `write()` plus `makeReadOnly()` fit in one tap? Does permission persist?
- **Phase 3:** iPhone banner and new-tab behavior, cookie isolation, default-browser routing, tab restore.
- **Phase 4:** 10DLC campaign and cost, consent wording.
- **Phase 5:** Wake Lock on staff devices, whether `scan()` suppresses system dispatch, behavior inside a PWA.
- **Phase 6:** reader factory format, iPad HID.
- Standard patterns (no extra research): Phase 1, Phase 7.

## Confidence Assessment

| Area | Confidence | Notes |
|---|---|---|
| Stack | MEDIUM-HIGH | Primary docs verified; vendor prices are a one-day snapshot; Amazon unverifiable |
| Features | MEDIUM | Codebase HIGH; competitor behavior from summaries |
| Architecture | HIGH fit, MEDIUM runtime | Direct code reads; device behavior inferred |
| Pitfalls | MEDIUM | Codebase HIGH; iOS sources are older |

**Overall confidence:** MEDIUM-HIGH

### Gaps to Address

- iPhone real-device behavior: spike in Phase 1. Don't promise "no typing" on iPhone until measured.
- Android write plus lock sequence: test in Phase 2.
- Genuine-chip check on all samples.
- GoToTags pre-encoding.
- Reader format and iPad support, only if the reader is bought.
- Twilio 10DLC status: existing reminders may be failing silently.
- Gmail daily cap.
- Fly cold start.
- Hostname decision.
- Tag cloning is an accepted residual risk: a cloned tag only works on a logged-in staff phone inside the class window.

## Sources

- **Primary:**
  - Apple Core NFC, background tag reading.
  - Chrome for Developers Web NFC and the W3C draft.
  - caniuse Web NFC.
  - NXP NTAG213/215/216 datasheet.
  - ACS ACR1281U-C2 Reference Manual v1.01.
  - Codebase files.
- **Secondary:**
  - GoToTags product and help pages.
  - Twilio changelog (30034) and opt-out docs (21610).
  - Jackrabbit, iClassPro, Procare, Brightwheel and Mindbody help pages.
  - WebKit bug 181849.
- **Tertiary:** Apple Dev Forums, Adafruit and eBay listings, unverified iOS routing claims.

---
*Research completed: 2026-10-14*
*Ready for roadmap: yes*
