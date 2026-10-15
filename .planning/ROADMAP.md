# Roadmap: AttenDANCE v2.0 Tap-In

## Overview

Dancers tap an NFC fob or card on a staff phone when they arrive and leave, and AttenDANCE records it with no typing. The build starts with the risky logic and no UI: a framework-free tap service, the tag data model and the legacy `rfid_uid` migration, run in parallel with a hardware sourcing and device-spike track. Tag enrollment on Android comes next, then the iPhone URL flow with the staff result screen and flags. Parent notifications follow through a durable outbox, then the Android kiosk with offline queue, then the USB reader station and the missed-checkout report. The last phase removes the legacy `rfid/` package, runs the pilot on unlocked tags, audits privacy, and only then locks and hands out production tags.

## Phases

**Phase Numbering:**
- Integer phases (1, 2, 3): Planned milestone work
- Decimal phases (2.1, 2.2): Urgent insertions (marked with INSERTED)

Decimal phases appear between their surrounding integers in numeric order.

- [ ] **Phase 1: Tap core and hardware gate** - Tag data model, `process_tap()` service and staff tap API, plus the sample-tag and hostname decisions
- [ ] **Phase 2: Tag management and enrollment** - Staff write, enroll, lose, replace and lock tags from the app on Android
- [ ] **Phase 3: iPhone flow, result screen and staff flags** - Tap a tag URL on a staff iPhone and get a full-screen result with neutral flags, undo and an optional photo
- [ ] **Phase 4: Parent notifications** - Arrival and departure email through a durable outbox, with opt-outs and gated SMS
- [ ] **Phase 5: Android kiosk and offline queue** - Continuous Web NFC tap station with tones, wake lock and an offline replay queue
- [ ] **Phase 6: USB station and missed-checkout report** - Keyboard-wedge reader station by chip UID, and the end-of-day never-tapped-out list
- [ ] **Phase 7: Legacy removal, pilot and rollout** - Remove the old RFID code, audit privacy, run the pilot, then bulk order, lock and hand out

## Phase Details

### Phase 1: Tap core and hardware gate
**Goal**: A tap resolves to the right dancer, class and direction, and is recorded exactly once, through a staff-only API with no UI. In parallel, the physical tags are verified and the permanent hostname is decided.
**Depends on**: Nothing (first phase)
**Requirements**: HW-01, HW-02, HW-03, TAG-01, TAG-02, TAG-08, TAP-01, TAP-02, TAP-03, TAP-04, TAP-05, TAP-06, TAP-07, TAP-08, TAP-10
**Success Criteria** (what must be TRUE):
  1. A staff-authenticated tap on an active tag during a class window (start minus 30 minutes through class end) marks the dancer present with `check_in_method='tap'`; a tap outside every window, or a dancer with several candidate classes, returns a "pick a class" outcome and writes no attendance until a class is chosen.
  2. A second tap 10 seconds or less after the first is ignored, even across restarts; a tap 5 or more minutes after check-in sets `check_out_time` on the same attendance row and never creates a second row, even when a teacher already pre-marked P.
  3. Replaying the same request with the same idempotency key never double-records, and every tap, accepted or rejected, appears in the append-only tap event log.
  4. Back-to-back classes roll over when the setting is on, and tardy auto-mark sets T only when its grace setting is non-zero (off by default).
  5. Every existing `Student.rfid_uid` appears as an active tag for that student, the legacy column is untouched, and the database stores only a hashed token and a normalized UID per tag.
  6. The shopping list is verified, sample tags pass the NXP TagInfo originality check and read on one iPhone XS+ and one Android phone, and the permanent tag hostname is decided and recorded.
**Plans**: TBD

Notes:
- Non-code track runs alongside the code: sample order (GoToTags NTAG213 fob and card), TagInfo checks, phone matrix, and the Twilio 10DLC status check that Phase 4 depends on.
- Real-device spike: read a sample tag on iPhone to measure the banner and Safari behavior. Phase 3 consumes the result. Do not promise "no typing" on iPhone until measured.
- HW-03 (hostname) must be recorded before any tag is written for keeps. Phase 2 enrollment writes the hostname into tags, and TAG-09 (lock) must not run on a production tag until HW-03 is done.
- Resolver must be rewritten, not reused: the legacy `_find_current_class` first-class-today fallback is replaced. Tap-out must UPDATE the existing attendance row (unique day index in `app/migrations.py`).
- Legacy `rfid_uid` values are decimal, not hex; normalize during backfill.
- PII rule applies here, not just in Phase 7: the new `nfc_tags` and `tap_events` tables are data about minors, so `privacy.html` and the `/api/my-data` export change in the same commit as the models. ROLL-03 is the final audit.
- TAP-09 (undo) is a result-screen feature and sits in Phase 3; the service-level undo hook is built here so Phase 3 only adds UI.

### Phase 2: Tag management and enrollment
**Goal**: Staff can write, enroll, replace and retire tags from the student page, without ever overwriting another dancer's tag.
**Depends on**: Phase 1
**Requirements**: TAG-03, TAG-04, TAG-05, TAG-06, TAG-07, TAG-09
**Success Criteria** (what must be TRUE):
  1. On an Android phone, staff open a student and enroll a tag: the app writes the URL, reads it back to verify, captures the UID, and the tag becomes active for that student.
  2. Enrolling a tag that is already active for another student is refused with a clear message, and the existing assignment is untouched.
  3. Staff can mark a tag lost or retired, and a later tap on it is rejected and logged; replacing a lost tag deactivates the old one and enrolls the new one in a single flow.
  4. Staff can open a roster of active students who have no active tag.
  5. Locking a tag is a separate, explicit step that is never run as part of enrollment; pilot tags stay unlocked.
**Plans**: TBD
**UI hint**: yes

Notes:
- Real-device spike on Android: does `write()` plus `makeReadOnly()` fit in one tap, and does the NFC permission persist across taps?
- Enrollment writes the URL on the decided hostname (HW-03). Use test tags only until HW-03 is recorded.
- Tags carry non-identifying labels. Move existing `has_rfid` consumers to read from tags.

### Phase 3: iPhone flow, result screen and staff flags
**Goal**: A staff member taps a dancer's tag with an iPhone and sees a large, color-coded result in seconds, with neutral flags and a way to undo.
**Depends on**: Phase 1 (tap service), Phase 2 (active tags to tap)
**Requirements**: TAPUI-01, TAPUI-02, TAPUI-03, TAPUI-04, TAPUI-05, TAPUI-06, FLAG-01, FLAG-02, FLAG-03, TAP-09, PHOTO-01
**Success Criteria** (what must be TRUE):
  1. Opening a tag URL in a logged-in staff Safari records exactly one tap; reloading, restoring the tab or prefetching the URL records nothing extra.
  2. Opening the same URL from a parent or logged-out session shows a generic page that is identical for valid, unknown and revoked tokens and reveals nothing about the dancer.
  3. A staff member with an expired session signs in and lands back on the pending tap, and an offline phone shows "NOT RECORDED" instead of failing silently.
  4. The result screen is readable at arm's length: first name, class and time, green for in, blue for out, amber for needs attention, red for unknown or lost.
  5. Staff see a neutral "Check account" badge when the household-net balance is owed and a "Forms needed" badge when an active waiver is unsigned, with no dollar amounts, and neither ever blocks a check-in.
  6. Staff can undo the last tap or reopen a checked-out session from the result screen, and can see a staff-only student photo there once one is added.
**Plans**: TBD
**UI hint**: yes

Notes:
- Real-device spike on iPhone: banner and new-tab behavior, Safari vs Home Screen cookie isolation, default-browser routing, tab restore, Auto-Lock. The written staff checklist (TAPUI-06) comes from this.
- PHOTO-01 updates the model, `privacy.html` and the `/api/my-data` export in the same commit (standing PII rule).
- Open decision during planning: Fly `min_machines_running`, because a cold start delays the first tap of the day.

### Phase 4: Parent notifications
**Goal**: Parents hear about arrival and departure by email without ever slowing down a tap, and nobody gets a notice they opted out of.
**Depends on**: Phase 3 (taps flowing through real staff flows)
**Requirements**: NOTIF-01, NOTIF-02, NOTIF-03, NOTIF-04, NOTIF-05, NOTIF-06
**Success Criteria** (what must be TRUE):
  1. A check-in or check-out tap results in an email to the household with first name, class and time, and the tap request returns without waiting on the send.
  2. A household that opts out of tap notices receives none, and the opt-out holds across restarts.
  3. SMS goes only to guardian numbers that explicitly opted in, stays off studio-wide until 10DLC is verified, and a Twilio STOP (error 21610) is saved as an opt-out and never retried.
  4. Siblings who tap within a short window produce one combined notice for the household.
  5. A notice that would go out more than 20 minutes late (after an offline replay or a restart) is suppressed, and staff have a studio kill switch and a daily send cap.
**Plans**: TBD

Notes:
- Twilio 10DLC check: confirm campaign status and cost before enabling any SMS. Existing reminders may be failing silently today (error 30034).
- Gmail has a daily send cap; the daily cap setting must sit below it.
- Opt-out and notification preferences are new PII-adjacent fields: model, `privacy.html` and `/api/my-data` change in the same commit.
- Single drain thread, re-kicked on boot, capped retries; no inline `send_email`/`send_sms` in the tap request (256 MB machine, 1 worker, 4 threads).

### Phase 5: Android kiosk and offline queue
**Goal**: A staff Android phone or tablet runs a hands-free tap station that keeps working through bad Wi-Fi.
**Depends on**: Phase 1 (tap API and batch semantics), Phase 3 (result states), Phase 4 (so replayed taps respect stale-notice suppression)
**Requirements**: KIOSK-01, KIOSK-02, KIOSK-03, KIOSK-04
**Success Criteria** (what must be TRUE):
  1. Staff tap Start once in Chrome on Android, and dancers then tap one after another with no banner tap per dancer.
  2. The screen stays awake, in, out and error each play a distinct tone, and a status chip shows connected, offline or queued.
  3. Taps made with the phone offline are queued on the device, replayed in order on reconnect, and recorded with the time the tap actually happened.
  4. Closing the station leaves no student data on the device; the cached shell holds none.
**Plans**: TBD
**UI hint**: yes

Notes:
- Real-device spike: Wake Lock on staff devices, whether `scan()` suppresses system tag dispatch, behavior inside a PWA, and keeping one reader with an AbortController.
- Server converts offline timestamps within a bounded window; studio-local naive datetimes throughout.
- Community center Wi-Fi is not guaranteed, so test the offline path on site conditions.

### Phase 6: USB station and missed-checkout report
**Goal**: A tablet or laptop with a USB keyboard-wedge reader works as a tap station, and staff can see who never tapped out.
**Depends on**: Phase 5 (station shell and queue)
**Requirements**: USB-01, USB-02, RPT-01
**Success Criteria** (what must be TRUE):
  1. On a tablet or laptop with the reader plugged in, tapping a tag records the tap by its normalized chip UID with no input field focused.
  2. The reader configuration (hex, uppercase, no separator, Enter terminator) is documented, and UIDs the reader types match the UIDs Web NFC captured at enrollment.
  3. At the end of the day staff can open a list of dancers who tapped in but never tapped out.
**Plans**: TBD
**UI hint**: yes

Notes:
- Real-device spike (only if the optional ACR1281U-C2 is bought): reader factory output format, 4-byte vs 7-byte UIDs (8 or 14 hex), iPad HID behavior.
- If no reader is purchased, USB-01 and USB-02 can be verified against a borrowed reader or deferred by explicit decision; the Android kiosk already covers the main path.

### Phase 7: Legacy removal, pilot and rollout
**Goal**: The old RFID code is gone, privacy coverage is complete, and the studio goes live on tags that were proven in a pilot before anything is locked.
**Depends on**: Phases 1 through 6 (new flows live before the old one is removed)
**Requirements**: ROLL-01, ROLL-02, ROLL-03
**Success Criteria** (what must be TRUE):
  1. The `rfid/` package, `/api/rfid/*`, `assign-rfid`, `remove-rfid` and `RFID_*` config are gone, `RFIDLog` and `rfid_uid` remain as read-only history, and the app boots and the smoke audit passes.
  2. The README and smoke audit describe the tap flow, not the Raspberry Pi reader.
  3. The privacy page and the `/api/my-data` export cover tap events, tag assignments, notification preferences and check-out times.
  4. A 2 to 3 week pilot on unlocked tags runs with real classes, and only after it passes are the bulk order placed and the production tags locked and handed out.
**Plans**: TBD

Notes:
- Standard patterns, no extra research.
- Retention for raw tap history (proposed 12 months) is settled with the studio during this phase.
- Locking production tags depends on HW-03 (Phase 1) and verified reads on iPhone, Android and USB.

## Progress

**Execution Order:**
Phases execute in numeric order: 1 → 2 → 3 → 4 → 5 → 6 → 7

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 1. Tap core and hardware gate | 0/TBD | Not started | - |
| 2. Tag management and enrollment | 0/TBD | Not started | - |
| 3. iPhone flow, result screen and staff flags | 0/TBD | Not started | - |
| 4. Parent notifications | 0/TBD | Not started | - |
| 5. Android kiosk and offline queue | 0/TBD | Not started | - |
| 6. USB station and missed-checkout report | 0/TBD | Not started | - |
| 7. Legacy removal, pilot and rollout | 0/TBD | Not started | - |
