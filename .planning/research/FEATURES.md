# Feature Research

**Domain:** NFC tap-in / card check-in attendance for a kids' dance studio (ages 3-83), added to an existing Flask studio system
**Researched:** 2026-10-14
**Confidence:** MEDIUM. Competitor behavior comes from vendor help pages and search summaries (Jackrabbit, iClassPro, Brightwheel, Procare, Mindbody); several help pages returned 403/404 so details like check-in windows and sibling handling are unconfirmed. Codebase findings are HIGH (read directly).

## What the existing code already gives us (and what it gets wrong)

Read from `app/models.py`, `rfid/service.py`, `app/api/routes.py`. These facts drive the complexity ratings below.

- **Attendance is one row per (student, class, day)**, enforced by a unique-day index. `check_out_time` exists and is unused. A tardy dancer is `is_present=True, is_tardy=True`. Absent and excused are rows with `is_present=False` (`is_excused` for E). A blank box on the card means no row.
- **Existing write paths** (`manual_checkin`, `toggle_attendance`) flip an existing absent/excused row to present rather than inserting a second one. Tap-in must do the same, and must respect the card's `expected_state` / 409 pattern so a tap landing while a teacher has the card open does not silently delete a real mark.
- **`Student.rfid_uid` is a single unique column.** It cannot hold a lost-tag history, two tags per dancer (fob + card), a tag in "lost" state, or an opaque token distinct from the chip UID. Lost-card replacement and enroll-blank-tag both force a new `NfcTag` table. This is the biggest structural dependency.
- **Legacy `_find_current_class` is wrong for production.** (1) After the window check fails it falls back to "first enrolled class today", so a 9 AM tap for a 6 PM class, or an 8 PM tap after class, records a check-in. (2) It ignores season (`DanceClass.season_id`). (3) Window is hardcoded 30 min before / 15 min after. (4) It runs on one in-memory `last_scan_uid`, which does not survive multiple gunicorn requests from different phones. Rewrite, do not reuse.
- **Legacy "already checked in" returns success and does nothing.** There is no check-out path. The new second-tap-is-check-out rule is net new behavior.
- **No tardy logic anywhere in the scan path.** `is_tardy` is only set by the teacher via the card.
- **No SMS consent / opt-out field exists.** `app/sms.py` sends to whatever phone is on file via Twilio. `student_emails()` already resolves parent_email, parent_email_2 and household addresses.
- **No student photo field exists.** Adding one is new PII (update model, `privacy.html`, `/api/my-data` in the same commit per project rule).
- **Waivers have no "required" flag.** `WaiverTemplate` has `is_active` and `allow_decline`; `WaiverSignature` has `consent`. "Missing waiver" must be defined as: an active template with no signature row for this dancer. A declined opt-out form (e.g. photo release, `consent=False`) is a signature, not a missing waiver.
- **Balances are per fund with a hard wall** (`calc_balance` returns `studio` and `company`, never combined), and reminders net balances per household (`family_reminder_groups`) so a sibling credit covers a sibling debt. The kiosk flag must use the same household-net rule or it will flag families the reminder job correctly skips.

## Feature Landscape

### Table Stakes (staff will consider Tap-In broken without these)

| Feature | Why Expected | Complexity | Notes |
|---------|--------------|------------|-------|
| Tap resolves to exactly one class using a time window | Every competitor ties check-in to a class. iClassPro "Auto Check-In on Scan" checks in for eligible events; Jackrabbit barcode records date/time against the class | MEDIUM | Window = (start - early, end + late grace). Recommend defaults 30 min early (matches legacy) and class end; studio-level `Setting`, per-class override later. Must filter to active class in active season on today's weekday. Delete the "first class today" fallback |
| Multiple classes same day: pick deterministically | Kids with a ballet + jazz back to back are common | MEDIUM | Rule: among enrolled classes whose window contains now, choose the one with the nearest start at or after now minus grace; if the dancer already has an open session in a class that has ended, a new tap closes it at its end time and opens the next ("Moved to Jazz"). Show the class name on every result so staff catch a wrong guess. Add a one-tap "Wrong class?" correction on the result screen. Do not make staff choose on every tap |
| Tap with no class in window: do not create attendance | Attendance needs a `class_id`; a check-in with no class corrupts reports | LOW | Show amber "No class for Ava right now", log `RFIDLog` action `no_class`. Offer staff-only "Check in to..." with the dancer's classes today (covers makeups, rehearsals, wrong-day) |
| Double-tap debounce | Readers and phones re-report a tag held in range; patents/forums describe 200-500 ms dithering and repeat reads until tag leaves field | LOW | 10 s server-side debounce keyed on the tag in the DB (not in-memory; legacy used a single process variable). A debounced tap returns the previous result, never a check-out. Make it idempotent so a Safari reload of the tag URL does not create a second event |
| Second tap = check out, with a minimum dwell | Stated scope; childcare (Brightwheel, Procare) log both directions | MEDIUM | Sets `Attendance.check_out_time`. Add a minimum dwell (default 5 min, `Setting`) so a second accidental tap 30 s later does not send a departure text. A tap inside the dwell shows "Checked in at 5:02" and changes nothing. Third tap the same day in the same class: show "Already checked out", do not re-open (staff can fix on the card) |
| Tardy auto-mark by grace period | Stated scope; teachers currently hit T by hand | LOW | Present at or before start + grace (default 5 min); after that `is_tardy=True, is_present=True`. Compare tap time to class start, not to the early window. Early arrival is simply present. Never auto-mark absent for non-tappers (see anti-features) |
| Tap on an existing absent/excused row flips it to present | Matches `manual_checkin` | LOW | Preserve E: if staff marked excused earlier and the dancer shows up, flip to present and keep an audit trail. Honor the card's concurrency check |
| Tap result is a big, unambiguous staff-screen result | Every kiosk (iClassPro confirmation screen with timeout, Procare, Brightwheel) shows who was checked in | MEDIUM | Full-screen state: green (checked in), blue (checked out), amber (tardy / needs attention), red (unknown or deactivated tag). First name + last initial + class + time. Icon and text, not color alone. Auto-reset after 3-4 s on the Android kiosk page |
| Audible + haptic feedback on the Android kiosk page | Staff are looking at kids, not the screen | LOW | Distinct tones for in / out / error. Web Audio needs a user gesture to start, so the kiosk page needs a "Start station" button that also requests wake lock. iPhone path opens the tag URL as a new Safari page load, so expect visual-only feedback (no autoplay audio, and iOS Safari has no Vibration API; MEDIUM confidence, verify on device) |
| Unknown / unassigned tag handling | Blank tags will be tapped before enrollment | LOW | Unknown token: red "Tag not assigned", log it. A scan by a non-staff phone (no session) must show a neutral page and reveal nothing (COPPA constraint). Staff session + unassigned tag: offer "Assign to a dancer" (this is the enrollment entry point) |
| Staff enroll a blank tag in-app | Stated scope; Jackrabbit/iClassPro assign codes per student | MEDIUM | Flow: staff picks dancer, writes NDEF URL with a fresh random token (Android Web NFC write), tag row links token to dancer. For iPhone staff, writing is not possible in Safari, so enrollment on iPhone is "scan a pre-written tag, then pick the dancer". Bulk-prewrite blanks on one Android phone or a desktop writer, then assign by tapping. Warn before overwriting a tag already assigned |
| Deactivate a lost tag; issue a replacement | Stated scope; basic credential hygiene | MEDIUM | `NfcTag.status` in (active, lost, retired). A lost tag that is tapped later gets a red "Deactivated tag" result plus a staff-visible alert, and sends no parent text. Replacement creates a new tag row for the same dancer and keeps attendance history, since history hangs off the student, not the tag. Needs `AuditLog` entries for assign / deactivate / replace |
| One dancer per tag | Avoids ambiguity | LOW | See sibling decision below |
| Parent text/email on arrival and departure | Stated scope; Procare and Brightwheel make arrival/departure alerts their core feature | MEDIUM | Send only on a real state transition (never on debounced, dwell-blocked, unknown or deactivated taps). Send off the request thread so a 1 s Twilio call does not stall the single gthread worker; a send failure must never fail the tap. Reuse `student_emails()` and the Twilio helper |
| Notification content: minimal and parent-useful | Messages travel over SMS and sit on lock screens | LOW | "Ava arrived at Ballet 1 at 5:02 PM. - LaShelle's School of Dance" / "Ava was checked out of Ballet 1 at 5:48 PM." First name only, no last name, no balance, no waiver, no links with tokens, no photo. Mark tardy neutrally or leave it out of the text |
| Notification opt-out | Procare/Brightwheel let parents turn off alerts; Twilio STOP handling is automatic for SMS | MEDIUM | Per-household toggles for arrival and departure by channel (SMS, email), in the parent portal and editable by staff. Studio-wide default and master switch in `Setting`. Twilio will block further sends to a number that replies STOP, so surface the failure rather than counting it as sent |
| Staff-only flags: balance owed, missing waiver | Stated scope; Mindbody lets the owner configure front-desk alerts for balance problems, CourtReserve users request flags at check-in | MEDIUM | Reuse household-net balance logic and the `reminders_min_balance` threshold; flag if either fund's household balance is over threshold, never merge funds. Missing waiver = active template with no signature row. Rendered as a neutral "Check account" / "Forms needed" badge, with detail one tap away. Never the dollar amount on the main result screen |
| Tap never blocks a child | A kid should never be turned away at the door by a phone | LOW | Flags inform staff; the check-in still records. Blocking, if the studio ever wants it, is a staff action |
| Scan log / audit trail | Legacy `RFIDLog` already exists; needed to debug "I tapped and nothing happened" | LOW | Extend `RFIDLog` with tag id, method (iphone, web-nfc, usb), result code, station user. Show recent taps on the staff page |
| Staff can fix any tap on the attendance card | A tap will occasionally be wrong (wrong class, forgotten tap) | LOW | Already works via the card; make sure card shows check-in / check-out times and a `check_in_method` of `nfc` so taps are distinguishable from manual marks |
| Tap-in works with spotty Wi-Fi | Community centers have no guaranteed Wi-Fi (PROJECT.md) | MEDIUM-HIGH | Phone on cellular covers most cases. A tap that fails to reach the server must say so loudly (red "Not recorded, try again") rather than appear to succeed. Offline queueing is a v2.x item, see below |

### Differentiators (worth building after the core works)

| Feature | Value Proposition | Complexity | Notes |
|---------|-------------------|------------|-------|
| Auto-rollover for back-to-back classes | Staff never touch the phone between classes | MEDIUM | Included in the multi-class rule above; the differentiator part is closing the prior class at its scheduled end rather than at the tap time, so class duration reports stay honest |
| Coalesced sibling notifications | One text "Ava and Jo arrived" when siblings tap within ~60 s, instead of two texts | MEDIUM | Needs a short delay-and-merge in the send path. Cuts Twilio cost and annoyance. Do after plain notifications are proven |
| Family tag that checks in all siblings with a class now | One fob per household like iClassPro family codes, which act like the family phone number | MEDIUM-HIGH | Needs tag-to-family linkage and a result screen listing each sibling. Risk: a sibling who is not at the studio gets marked present. Only auto-check-in siblings whose class window is open, and show the list so staff can untick. Defer until there is demand; per-dancer tags cost about $1 each |
| Guardian / pickup tap for check-out | Childcare tools (Brightwheel, Procare) authorize pickup per person | MEDIUM-HIGH | A guardian tag tied to `ParentStudent` that checks out that parent's dancers. Treat as a convenience, not identity verification (a fob proves possession only). Needs a decision on who counts as authorized. Defer |
| Student photo on the staff result screen | Staff can confirm the child holding the tag is the tag's owner; the main defense against tag swapping | MEDIUM | New PII: model field, upload UI, `privacy.html`, `/api/my-data` all in one commit. Show only to the logged-in staff device, small, never in notifications. Consider showing it only on flagged or deactivated-tag events to limit exposure on a phone held in public |
| Late-tap / forgotten-checkout report | Staff see who never tapped out and who tapped in with no class | LOW | Query over `Attendance` and `RFIDLog`. Do not fabricate checkout times |
| Per-class early window and grace overrides | Adult classes and performance rehearsals run different rules | LOW | Nullable columns on `DanceClass` falling back to studio `Setting` |
| Tag rotation / NTAG 424 DNA anti-clone | A copied NTAG213 URL replays forever; NTAG 424 DNA appends a per-tap CMAC and counter so the server can detect copies | HIGH | Only protects if the server verifies the CMAC and counter. NTAG213 cannot do this. Overkill for 2 customers and tags that carry no data; revisit only if tag cloning or buddy-tapping shows up. The cheap mitigation is that a cloned URL only works on a logged-in staff phone |
| Offline tap queue | Tap recorded locally and synced when signal returns | HIGH | Needs service worker plus conflict handling against concurrent card edits. Defer unless real community-center dead zones appear |
| Weekly attendance digest to parents | Uses the same plumbing | LOW | Optional; not part of Tap-In scope |

### Anti-Features (do not build)

| Feature | Why Requested | Why Problematic | Alternative |
|---------|---------------|-----------------|-------------|
| Showing balance owed, dollar amounts or "unpaid" on the main kiosk result | Staff want the nudge at the moment of tap | The staff phone screen is visible to the kid and the parent at the door. Public debt display embarrasses families and is a privacy problem | Neutral "Check account" badge, details behind a staff-only tap. Same for waivers: "Forms needed" |
| Blocking check-in when a balance is owed or a waiver is missing | Gym software (Mindbody, Hapana "invalid check-in") does this to adults | Turning a 5-year-old away at the door over a family's billing is the wrong trade for a kids' studio, and the attendance record becomes wrong | Flag and record; staff decide |
| Auto-marking everyone absent when the first dancer taps | iClassPro does this | The card has distinct blank, A and E states and teachers mark them by hand; auto-A would overwrite blanks and create false "absent" signals and E conflicts | Leave non-tappers blank; show "not yet tapped" on the roster |
| Shared tag across siblings with a chooser on tap | Fewer tags to buy | Ages 3-83, kids tapping at speed; a chooser adds a staff decision per tap and mismarks the wrong sibling. Also breaks the one-dancer-one-tag audit trail | One tag per dancer |
| Student name, last name, DOB, balance or any PII on the tag | Lets the tag work offline | Violates the COPPA constraint; a lost tag becomes a data leak | Opaque random token only |
| Using the chip UID as the identifier in the URL | Simple | UIDs are readable by any phone and copyable; tokens are random, revocable and mean nothing | Token in NDEF URL; UID stored only for the USB-reader path |
| Fabricating check-out times at class end for dancers who never tapped out | Makes the data look complete | Departure texts and duration data become lies | Leave `check_out_time` null and report it |
| Departure text sent on every tap or on a forgotten-checkout sweep | "Always notify" | Spam, false reassurance, and Twilio cost | Notify only on real transitions |
| Parent-facing live "where is my child" page | Natural extension of notifications | Real-time location of a minor is a safety and liability surface the studio does not need | Texts only; attendance history is already in the portal |
| Facial recognition or biometric check-in | Fancy | Child biometrics are a legal minefield and out of scope | Photo for human verification only |
| Native app for the station | Better NFC access on iPhone | Out of scope in PROJECT.md; staff use their own phones | Web app plus tag URL |

## Feature Dependencies

```
NfcTag table (token, uid, status, student_id, history)
    |--requires--> Enroll blank tag in app
    |--requires--> Deactivate lost tag / replacement
    |--requires--> Unknown / deactivated tag handling
    +--feeds-----> Tap resolver

Tap resolver (class window + season + enrollment)
    |--requires--> Studio window/grace/dwell Settings
    |--requires--> Debounce (DB-backed, idempotent)
    |--requires--> Check-in writes Attendance (flip absent/excused, honor card concurrency)
    |      +--enhances--> Tardy auto-mark
    +--requires--> Second tap = check-out (Attendance.check_out_time)

Check-in / check-out transition
    +--triggers--> Notification (async, per-household opt-out)
                       +--requires--> Parent consent/opt-out storage (new; none today)
                       +--uses------> student_emails(), app/sms.py, Twilio A2P registration

Staff flags
    |--requires--> Household-net balance (family_reminder_groups, calc_balance_bulk)
    +--requires--> "Missing waiver" definition (WaiverTemplate.is_active vs WaiverSignature)

Staff phone station
    |--iPhone--> staff session + token URL (Safari opens per tap)
    |--Android--> Web NFC continuous kiosk page (needs Start button for audio/wake lock)
    +--USB----> keyboard-wedge page using UID (needs NfcTag.uid)

Student photo --enhances--> result screen (adds PII work: model + privacy.html + my-data)
Family tag / guardian tag --conflicts--> one-dancer-one-tag simplicity (defer)
```

### Dependency Notes

- **Tap resolver requires a rewrite, not a port, of `_find_current_class`.** The legacy fallback to "first class today" must go. It also needs to respect season and the card's concurrency check.
- **Notifications require a consent/opt-out store that does not exist.** Put the toggles on `Family` (with a per-`Student` override only if needed). Adding any new contact preference field means updating `privacy.html` and `/api/my-data` in the same commit.
- **Notifications depend on Twilio A2P 10DLC registration** covering attendance alerts. The studio already sends balance reminders by SMS, so a registered campaign likely exists, but its declared use case should be checked before adding two messages per class per child (LOW confidence; verify with the Twilio console).
- **iPhone path is not truly "no typing, no tapping".** Per Apple's background tag reading notes and third-party docs, the iPhone shows a notification the user must tap before Safari opens the URL, and the screen must be on and the phone unlocked at some point since boot (MEDIUM confidence, needs on-device verification). That makes the iPhone path 2 touches per kid and slower than Android Web NFC. Android kiosk or the USB tablet should be the recommended station for busy drop-off; the iPhone is the lightweight path.
- **Flags depend on the household-net rule**, otherwise the kiosk contradicts the reminder job.
- **Lost-tag deactivation requires the tag table**, because `Student.rfid_uid` has no state or history.

## Edge-Case Decisions (the questions asked)

| Situation | Recommended behavior | Basis |
|-----------|---------------------|-------|
| Enrolled in several classes the same day | Auto-pick the class whose window is open and starts soonest; roll over on back-to-back; show class name; one-tap correction | Competitors auto-check-in "eligible events" (iClassPro); Jackrabbit uses Group/individual PINs per class. For a small studio, deterministic auto-pick is faster than a chooser |
| Tap with no class in window | No attendance row; amber message; staff-only "Check in to..." override | Attendance requires `class_id` |
| Tap twice quickly | 10 s debounce returns prior result; 5 min minimum dwell before a tap counts as check-out | Dithering and repeated-read behavior documented for RFID readers |
| Tap out | Sets `check_out_time`, sends departure text, result screen shows blue "Checked out" | Stated scope |
| Siblings sharing a fob | Not supported in v2.0; one tag per dancer | Simplicity, audit trail |
| Parent / guardian fob | Defer; treat later as convenience, not identity | Possession is not identity |
| Early arrival | Check in as present from 30 min before start (configurable), real tap time stored | Legacy window, common practice |
| Late arrival | Tardy after start + 5 min grace (configurable) | Existing `is_tardy` semantics |
| Kiosk feedback | Name, class, color + icon, tones on Android, auto-reset; photo optional and staff-only | Procare/iClassPro confirmation screens |
| Blank tag enrollment | Staff writes or assigns from the staff page; unassigned tap on a staff session offers "Assign" | Stated scope |
| Lost tag | Mark lost, new tag row, history stays with student, lost-tag taps alert staff and send nothing to parents | Stated scope; no competitor doc found, standard credential practice |
| Notification content | First name, class, time, studio name only | COPPA constraint |
| Notification opt-out | Per-household arrival/departure by channel, studio master switch, honor Twilio STOP | Procare/Brightwheel parent controls |

## MVP Definition

### Launch With (v2.0)

- [ ] `NfcTag` table plus tag assignment, deactivation, replacement - everything else keys off it
- [ ] Tap resolver: window, season, active enrollment, multi-class pick, no-class path - fixes the legacy fallback bug
- [ ] DB-backed debounce and minimum dwell - without it, double taps send false departure texts
- [ ] Check-in, tardy grace, check-out writing `Attendance` and extending `RFIDLog`
- [ ] Staff result screen with color, icon, class, and flags as neutral badges; Android kiosk tones
- [ ] Parent arrival/departure notification with household opt-out and minimal text
- [ ] Staff-only balance and waiver flags (neutral wording, household-net)
- [ ] Unknown / deactivated / non-staff tap pages that reveal nothing

### Add After Validation (v2.x)

- [ ] Coalesced sibling notifications - once staff report text volume complaints
- [ ] Per-class window and grace overrides - once adult or rehearsal classes need different rules
- [ ] Missed-checkout / no-class-tap report - once a few weeks of data exist
- [ ] Student photo on staff result screen - once tag swapping is a real problem

### Future Consideration (v3+)

- [ ] Family tag / guardian pickup tap - needs a clear authorization policy
- [ ] NTAG 424 DNA with server CMAC verification - only if cloning appears
- [ ] Offline tap queue - only if dead zones are common at the community centers

## Feature Prioritization Matrix

| Feature | Staff Value | Cost | Priority |
|---------|-------------|------|----------|
| NfcTag table + assign/deactivate/replace | HIGH | MEDIUM | P1 |
| Tap resolver with windows and multi-class pick | HIGH | MEDIUM | P1 |
| Debounce + dwell | HIGH | LOW | P1 |
| Check-out and tardy grace | HIGH | LOW | P1 |
| Result screen + sounds | HIGH | MEDIUM | P1 |
| Parent notifications + opt-out | HIGH | MEDIUM | P1 |
| Balance / waiver flags | MEDIUM | MEDIUM | P1 |
| Coalesced sibling texts | LOW | MEDIUM | P2 |
| Student photo | MEDIUM | MEDIUM | P2 |
| Per-class overrides | LOW | LOW | P2 |
| Family / guardian tags | LOW | HIGH | P3 |
| NTAG 424 DNA | LOW | HIGH | P3 |
| Offline queue | LOW | HIGH | P3 |

## Competitor Feature Analysis

| Feature | Jackrabbit | iClassPro | Brightwheel / Procare | Our Approach |
|---------|------------|-----------|-----------------------|--------------|
| Credential | Barcode/QR card or student/group PIN; phone-number self check-in | Parent phone number or QR code (student or family) at a kiosk (iPad app or standalone computer) | Kiosk with authorized-pickup name list + PIN | NFC token fob/card per dancer on staff phone |
| Class selection | Per class; group PIN moves a group between classes | Auto check-in for all eligible events when "Auto Check-In on Scan" is on | Room-level sign-in | Auto-pick one class by time window |
| Absent marking | Tracks attendance or absences (account setting) | First check-in marks everyone else absent | n/a | Never auto-absent |
| Parent alerts | Not found in sources | Not found in sources | Email + push on sign-in/out, admin master toggle, parent can disable (Procare); per-family notification types (Brightwheel); "Share with Parents" is per-device | SMS + email, household opt-out, studio master switch |
| Flags at check-in | Badges for makeups, trials, scheduled absences on self check-in | Not confirmed | n/a | Neutral staff-only badges for balance / forms |
| Balance block | n/a | Not confirmed | n/a | Never block |

## Sources

- Jackrabbit, "Take Attendance with a Barcode Scanner": https://help.jackrabbitclass.com/help/attendance-barcode-scanner (via search summary; page fetch 404; MEDIUM)
- Jackrabbit, self check-in blog: https://www.jackrabbitclass.com/blog/self-check-in/ (MEDIUM)
- iClassPro, "How Does the Check-In Kiosk Work?": https://support.iclasspro.com/hc/en-us/articles/360018661754-Check-In-Kiosk (search summary only; fetch 403; MEDIUM)
- iClassPro, "How Do I Configure the Check-In Kiosk?": https://support.iclasspro.com/hc/en-us/articles/360052777874-How-Do-I-Configure-the-Check-In-Kiosk (search summary only; MEDIUM)
- iClassPro, "How Do QR Codes Work with the Check-In Kiosk?": https://support.iclasspro.com/hc/en-us/articles/360024943634-How-Do-QR-Codes-Work-with-the-Check-In-Kiosk (MEDIUM)
- Procare, "School Settings": https://procare-solutions.document360.io/procare-online/docs/school-settings and "Sign In-Out on the Parent Kiosk": https://procare-solutions.document360.io/procare-online/docs/sign-in-out-on-the-parent-kiosk (MEDIUM)
- Brightwheel school settings: https://intercom.help/brightwheel/en/articles/1094101-school-settings ; attendance overview https://mybrightwheel.com/child-care-attendance-tracking/ (MEDIUM). Whether a pickup PIN works across siblings: not confirmed.
- Mindbody class check-in app (front-desk alerts for insufficient funds): https://www.mindbodyonline.com/business/class-check-in-app (LOW-MEDIUM)
- CourtReserve kiosk feedback requests (distinct sounds for overdue/expired, flags at check-in): https://feedback.courtreserve.com/communities/1/topics/2163-additional-kiosk-sounds-at-check-in (LOW, user requests)
- iOS background tag reading: https://gototags.com/help/ios/nfc/reading/background and Apple tech talk https://developer.apple.com/videos/play/tech-talks/702 (MEDIUM)
- NTAG 424 DNA / SUN: https://www.wakdev.com/en/knowledge-base/nfc-chips/nxp-ntag424-dna.html (MEDIUM)
- RFID attendance with parent SMS and debounce: general web results (Nashik school report, student projects, patents, forums); LOW, used only as design input
- Codebase: `app/models.py`, `rfid/service.py`, `app/api/routes.py` (manual_checkin, toggle_attendance, assign/remove-rfid, waiver_compliance, reminders), `app/helpers.py`, `app/sms.py` (HIGH)

## Gaps and items to verify in phase research

- iPhone background tag read UX on the actual staff phones (notification tap required? Safari new-tab per tap? audio/haptics?). Test before promising "no typing" on iPhone.
- Android Web NFC kiosk: tag write and continuous read behavior in Chrome, permission persistence across page reloads.
- Twilio A2P 10DLC campaign use case covers attendance notifications; cost at two texts per kid per class.
- Whether Jackrabbit / iClassPro send parent arrival alerts at all (not found), meaning notifications may be a differentiator against the tools being replaced rather than table stakes for dance studios. Table stakes status comes from childcare tools.
- Photo field and consent: decide with the studio whether a photo release waiver already covers a staff-only photo.
