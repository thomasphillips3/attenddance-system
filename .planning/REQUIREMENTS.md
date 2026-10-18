# Requirements: AttenDANCE v2.0 Tap-In

**Defined:** 2026-10-14
**Core Value:** Staff can record who is at class, and bill families correctly, without fighting the software.

## v2.0 Requirements

### Hardware (HW)

- [x] **HW-01**: Staff have a verified shopping list: NTAG213 fob and card SKUs, quantities and prices, plus the optional ACR1281U-C2 reader.
- [ ] **HW-02**: Sample tags pass the NXP TagInfo originality check, and they read on at least one iPhone XS+ and one Android phone before the bulk order.
- [ ] **HW-03**: The permanent tag hostname is decided and recorded before any production tag is locked.

### Tag management (TAG)

- [x] **TAG-01**: A student can have more than one tag (fob and card). Each tag has a status: pending, active, lost or retired.
- [x] **TAG-02**: A tag stores only a hashed random token and a normalized chip UID. No student data is written to the tag, and none appears in its URL.
- [x] **TAG-03**: Staff can write and enroll a tag from the student page on an Android phone. The flow writes the URL, reads it back to verify, captures the UID, and activates the tag.
- [x] **TAG-04**: Enrollment refuses to overwrite a tag that is already active for another student.
- [x] **TAG-05**: Staff can mark a tag lost or retired. A tap on a lost or retired tag is rejected and logged.
- [x] **TAG-06**: Staff can replace a lost tag in one flow: the old tag is deactivated and a new one is enrolled.
- [x] **TAG-07**: Staff can see a roster of active students who have no active tag.
- [x] **TAG-08**: Existing `Student.rfid_uid` values are migrated into the tag table, and the legacy column is kept as read-only history.
- [x] **TAG-09**: Staff can lock a tag (make it read-only) as a separate, explicit step.

### Tap core (TAP)

- [x] **TAP-01**: A tap during a class window (start minus 30 minutes through class end) checks the dancer in to that class.
- [x] **TAP-02**: A dancer with several classes today is resolved to one class deterministically. A tap outside every window shows staff a class picker and writes no attendance until staff pick a class.
- [x] **TAP-03**: A repeat tap within 10 seconds is ignored, using dedup stored in the DB rather than in memory.
- [x] **TAP-04**: A tap at least 5 minutes after check-in checks the dancer out. It sets `check_out_time` on the existing attendance row and never creates a second row.
- [x] **TAP-05**: A tap flips an existing absent or excused attendance row to present, and records `check_in_method='tap'`.
- [x] **TAP-06**: Whether a tap is a check-in or a check-out comes from today's tap history, not from the attendance card. A teacher pre-marking P does not turn an arrival tap into a departure.
- [x] **TAP-07**: Back-to-back classes roll over. When class A has ended and class B's window is open, A closes at its scheduled end with no departure notice, and B opens. This is controlled by a setting.
- [x] **TAP-08**: Tardy auto-mark sets T when the tap lands after class start plus a grace period. It is off by default and controlled by a setting.
- [x] **TAP-09**: Staff can undo the last tap or reopen a checked-out session from the result screen.
- [x] **TAP-10**: Every tap is recorded in an append-only tap event log with an idempotency key, so a replayed request never double-records.

### iPhone flow and result screen (TAPUI)

- [x] **TAPUI-01**: Opening a tag URL on a staff phone records the tap. GET has no side effects; the page records the tap with a single idempotent POST.
- [x] **TAPUI-02**: Opening a tag URL from a non-staff or parent session shows a generic page that is identical for valid, unknown and revoked tokens.
- [x] **TAPUI-03**: A staff member whose session has expired can sign in and land back on the pending tap.
- [x] **TAPUI-04**: The result screen is full-screen and readable at arm's length. It shows the dancer's first name, the class and the time, in a distinct color for each case: green for in, blue for out, amber for needs attention, red for unknown or lost.
- [x] **TAPUI-05**: When the phone is offline, the iPhone flow says "NOT RECORDED" instead of failing silently.
- [x] **TAPUI-06**: Staff get a written checklist for iPhone setup: log in in Safari, check Auto-Lock, close the Camera.

### Staff flags (FLAG)

- [x] **FLAG-01**: The result screen shows a neutral "Check account" badge to staff when the dancer's household-net balance is owed. It never shows dollar amounts.
- [x] **FLAG-02**: The result screen shows a neutral "Forms needed" badge when an active waiver template has no signature for that dancer.
- [x] **FLAG-03**: Flags never block a check-in.

### Parent notifications (NOTIF)

- [x] **NOTIF-01**: Parents get an email on check-in and on check-out, containing first name, class and time. Sends run asynchronously through a durable outbox and never inside the tap request.
- [x] **NOTIF-02**: A household can opt out of tap notices.
- [x] **NOTIF-03**: Parents can get SMS tap notices only after explicitly opting in. SMS goes to guardian numbers only, and stays off studio-wide until Twilio 10DLC is verified.
- [x] **NOTIF-04**: A Twilio opt-out (STOP / error 21610) is saved as an opt-out and is not retried.
- [x] **NOTIF-05**: Siblings who tap within a short window produce one combined notice per household.
- [x] **NOTIF-06**: Notices that would go out more than 20 minutes late (for example, after an offline replay or a restart) are suppressed. A studio kill switch and a daily send cap exist.

### Android kiosk (KIOSK)

- [x] **KIOSK-01**: Staff can start a continuous Web NFC tap station in Chrome on Android, with no banner tap per dancer.
- [x] **KIOSK-02**: The station keeps the screen awake, plays a distinct tone for in, out and error, and shows a connection status chip.
- [x] **KIOSK-03**: Taps made while offline are queued on the device and replayed in order when the station reconnects. They are recorded with the time the tap happened.
- [x] **KIOSK-04**: The station shell stores no student data on the device.

### USB reader station (USB)

- [x] **USB-01**: A tablet or laptop with a USB keyboard-wedge reader can run the station page. Taps are identified by the normalized chip UID, without a focused input field.
- [ ] **USB-02**: Reader configuration (hex, uppercase, no separator, Enter terminator) is documented and bench-tested against UIDs captured by Web NFC.

### Reporting (RPT)

- [x] **RPT-01**: Staff can see an end-of-day list of dancers who tapped in but never tapped out.

### Student photo (PHOTO)

- [x] **PHOTO-01**: Staff can add a student photo that only staff can see on the result screen. The model, `privacy.html` and the `/api/my-data` export are updated in the same commit.

### Rollout and cleanup (ROLL)

- [x] **ROLL-01**: The legacy `rfid/` package, `/api/rfid/*`, `assign-rfid`, `remove-rfid` and the `RFID_*` config are removed after the new flows are live. `RFIDLog` and `rfid_uid` are kept as history. The smoke audit and README are updated.
- [ ] **ROLL-02**: A pilot on unlocked tags runs before the bulk order, lock and hand-out.
- [x] **ROLL-03**: The privacy page and the `/api/my-data` export cover tap events, tag assignments, notification preferences and check-out times.

## Future Requirements (v2.x+)

- Per-class overrides for the check-in window and tardy grace
- Family or guardian pickup tags
- NTAG 424 DNA anti-clone tags
- Custom-printed logo cards (cosmetic; needs a GoToTags quote)

## Out of Scope

| Feature | Reason |
|---------|--------|
| Native iOS/Android app | Staff use the existing web app on their own phones |
| Dollar amounts on the kiosk | The screen faces the line; the badge stays neutral |
| Blocking check-in for balance or waivers | Never turn a child away at the door |
| Auto-marking non-tappers absent | Would overwrite the teacher's T/A/E card |
| Shared sibling tags | Ambiguous identity; one tag per dancer |
| PII or UID in the tag URL | A lost or scanned tag must leak nothing |
| "Pickup verification" or safety-system claims | It's attendance, not custody verification |
| Parent live "where is my child" page | Privacy; notices are enough |
| 125 kHz and MIFARE Classic tags | iPhones cannot read them |
| Server-attached reader (MFRC522 on Fly) | Hardware can't attach to Fly |

## Traceability

| Requirement | Phase | Status |
|-------------|-------|--------|
| HW-01 | Phase 1 | Built (2026-10-15) |
| HW-02 | Phase 1 | Waiting on hardware (sample tags + phones) |
| HW-03 | Phase 1 | Waiting on decision (permanent tag address) |
| TAG-01 | Phase 1 | Built (2026-10-15) |
| TAG-02 | Phase 1 | Built (2026-10-15) |
| TAG-03 | Phase 2 | Built (2026-10-15) |
| TAG-04 | Phase 2 | Built (2026-10-15) |
| TAG-05 | Phase 2 | Built (2026-10-15) |
| TAG-06 | Phase 2 | Built (2026-10-15) |
| TAG-07 | Phase 2 | Built (2026-10-15) |
| TAG-08 | Phase 1 | Built (2026-10-15) |
| TAG-09 | Phase 2 | Built (2026-10-15) |
| TAP-01 | Phase 1 | Built (2026-10-15) |
| TAP-02 | Phase 1 | Built (2026-10-15) |
| TAP-03 | Phase 1 | Built (2026-10-15) |
| TAP-04 | Phase 1 | Built (2026-10-15) |
| TAP-05 | Phase 1 | Built (2026-10-15) |
| TAP-06 | Phase 1 | Built (2026-10-15) |
| TAP-07 | Phase 1 | Built (2026-10-15) |
| TAP-08 | Phase 1 | Built (2026-10-15) |
| TAP-09 | Phase 3 | Built (2026-10-15) |
| TAP-10 | Phase 1 | Built (2026-10-15) |
| TAPUI-01 | Phase 3 | Built (2026-10-15) |
| TAPUI-02 | Phase 3 | Built (2026-10-15) |
| TAPUI-03 | Phase 3 | Built (2026-10-15) |
| TAPUI-04 | Phase 3 | Built (2026-10-15) |
| TAPUI-05 | Phase 3 | Built (2026-10-15) |
| TAPUI-06 | Phase 3 | Built (2026-10-15) |
| FLAG-01 | Phase 3 | Built (2026-10-15) |
| FLAG-02 | Phase 3 | Built (2026-10-15) |
| FLAG-03 | Phase 3 | Built (2026-10-15) |
| NOTIF-01 | Phase 4 | Built (2026-10-15) |
| NOTIF-02 | Phase 4 | Built (2026-10-15) |
| NOTIF-03 | Phase 4 | Built (2026-10-15) |
| NOTIF-04 | Phase 4 | Built (2026-10-15) |
| NOTIF-05 | Phase 4 | Built (2026-10-15) |
| NOTIF-06 | Phase 4 | Built (2026-10-15) |
| KIOSK-01 | Phase 5 | Built (2026-10-15) |
| KIOSK-02 | Phase 5 | Built (2026-10-15) |
| KIOSK-03 | Phase 5 | Built (2026-10-15) |
| KIOSK-04 | Phase 5 | Built (2026-10-15) |
| USB-01 | Phase 6 | Built (2026-10-15) |
| USB-02 | Phase 6 | Code + config doc done; bench test waits on reader |
| RPT-01 | Phase 6 | Built (2026-10-15) |
| PHOTO-01 | Phase 3 | Built (2026-10-15) |
| ROLL-01 | Phase 7 | Built (2026-10-15) |
| ROLL-02 | Phase 7 | Waiting on pilot |
| ROLL-03 | Phase 7 | Built (2026-10-15) |

**Coverage:**
- v2.0 requirements: 48 total
- Mapped to phases: 48
- Unmapped: 0

---
*Requirements defined: 2026-10-14*
*Last updated: 2026-10-15 after the build (44 of 48 built; 4 wait on hardware, a decision or the pilot)*
