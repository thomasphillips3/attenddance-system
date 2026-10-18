# Pitfalls Research

**Domain:** Adding NFC tap-in/tap-out attendance (NTAG fobs, staff phones, USB wedge fallback, parent SMS/email) to an existing Flask + SQLite app on a 256 MB Fly machine
**Researched:** 2026-10-14
**Confidence:** MEDIUM overall. Codebase findings are HIGH (read directly from the repo). Web NFC and Twilio facts are MEDIUM (official docs / vendor docs). iPhone behavior details (new-tab, default browser, PWA cookie isolation on current iOS) are LOW to MEDIUM: sources are old or third-party, so they are flagged "verify on device" and should be tested in the first phase that touches them.

## Phase naming used below

The roadmap does not exist yet. These are suggested phase labels so the mapping is concrete. The roadmapper can merge or split them.

| Label | Scope |
|-------|-------|
| P0 Hardware sourcing | Buy 5 sample tags + the USB reader, validate before bulk order |
| P1 Tag registry + tap core | `nfc_tags` table, token scheme, tap service (check-in/out), class resolution, idempotency |
| P2 Enrollment + lifecycle | Android write flow, read-back verify, lost/replace/retire |
| P3 iPhone tap flow | `GET /t/<token>` landing page, login-then-resume |
| P4 Android kiosk | Continuous Web NFC page |
| P5 USB wedge station | Keyboard-wedge page, UID path |
| P6 Parent notifications | Outbox, SMS/email, consent, opt-out |
| P7 Kiosk flags + privacy + rollout | Balance/waiver flags, COPPA/privacy/my-data, staff training, pilot |

## Critical Pitfalls

### Pitfall 1: Second tap = check-out turns every accidental double tap into a "left class" text

**What goes wrong:**
A kid taps twice (or the phone re-reads a fob held against it, or Safari reloads the tap URL). The first tap checks in, the second inside two seconds checks out. The parent gets "Ava arrived" and "Ava left" in the same minute. Attendance now shows a 2-second visit.

**Why it happens:**
"First tap = in, next tap = out" is a pure toggle with no time or state guard. The legacy `RFIDService` dedup does not help: it stores only ONE `last_scan_uid` in process memory, so taps A, B, A inside 5 seconds all pass. It also resets on every Fly auto-stop/start (`auto_stop_machines = 'stop'`, `min_machines_running = 0`).

**How to avoid:**
- Dedup in the database, not memory. Per-tag cooldown (suggest 5-10 s) keyed on `tag_id` using the last tap time, returning HTTP 200 with `status: "duplicate"` and NO side effects.
- Minimum dwell before a tap counts as check-out (suggest the later of 10 minutes after check-in or X% of class length; make it a `Setting`). A tap inside the dwell window returns "already checked in, tap again after HH:MM to check out" and does not toggle.
- Model the result as an explicit state machine (`not_arrived -> present -> departed`) and treat a tap after `departed` as "already left" with a staff confirm, not a re-check-in.
- Client-side debounce on `serialNumber` (Web NFC) and on the buffered UID (wedge) as a second layer, never the only layer.

**Warning signs:**
Attendance rows with `check_out_time - check_in_time` under 5 minutes. Two notifications for one dancer inside 60 seconds. Staff saying "it checked them out when they walked in."

**Phase to address:** P1 (state machine + DB-backed cooldown). Re-verify in P3/P4/P5 because each entry path adds its own double-fire source.

---

### Pitfall 2: Reload / back / tab-restore re-fires the tap (GET with side effects)

**What goes wrong:**
The iPhone opens `https://.../t/<token>` in Safari. If the GET itself records the tap, then pull-to-refresh, back-button, Safari tab restore after memory eviction, or a link preview/prefetch of the URL, records another tap. With toggle semantics that is a surprise check-out. The token also lingers in the tab URL and Safari history.

**Why it happens:**
The obvious implementation is `@bp.route('/t/<token>') def tap(): record(); return render(...)`. The iOS flow has no way to POST from a tag, so developers accept the GET side effect.

**How to avoid:**
- `GET /t/<token>` returns a tiny page with NO side effects. The page's inline JS does `fetch('/api/tap', {method:'POST', body:{token}})` on load (same-origin, so the existing `_csrf_origin_guard` Origin check passes), then `location.replace('/tap/result/<event_id>')`. Back/reload then lands on an idempotent result page, not a new tap.
- Add `Cache-Control: no-store` on both pages, and `X-Robots-Tag: noindex`.
- Make the landing template standalone (like `auth/login.html`, no CDN Tailwind/Alpine/fonts). The base template pulls three CDNs; on a weak community-center connection a CDN stall delays the POST by many seconds and users double-tap.
- Keep the existing `Referrer-Policy: strict-origin-when-cross-origin` so the token path never reaches a CDN as a referrer.
- Keep tokens out of access logs where possible (do not log request paths at INFO; never log the full token, log the first 6 chars of a hash).

**Warning signs:**
Two `RFIDLog`/tap-event rows with the same token a few seconds apart and different request IDs. Staff reports "I scrolled up and it checked them out."

**Phase to address:** P3.

---

### Pitfall 3: Unique day index means tap-out MUST update the existing row, and startup de-dupe will delete a second row

**What goes wrong:**
`app/migrations.py::_enforce_attendance_uniqueness` creates a UNIQUE index on `(student_id, class_id, date(check_in_time))` and, on every app start, runs `DELETE FROM attendance WHERE id NOT IN (SELECT MIN(id) ... GROUP BY student_id, class_id, date(check_in_time))`. If tap-out ever inserts a second row (e.g. "departure" as its own record), the insert fails with IntegrityError, or on a path that bypasses it, the next cold start silently deletes the later row. Because Fly auto-stops the machine, cold starts happen daily.

**Why it happens:**
The model comment on `Attendance.__table_args__` still says "plain index, NOT a uniqueness guarantee"; the real guarantee lives only in migrations. Developers read the model and believe duplicates are possible and design around it, or believe they are free to add rows.

**How to avoid:**
- Check-in creates or flips the single row; check-out only sets `check_out_time` on that row. Never insert a second row per (student, class, day).
- Handle `IntegrityError` on the check-in path exactly as `manual_checkin` does (rollback, treat as already-checked-in, re-query and continue to the check-out branch if appropriate).
- Fix the stale model comment in the same edit (project code-review rule: comments must match the implementation).
- Existing absent/excused rows (`is_present=False`, `is_excused=True`) for the same day: a tap flips the row to present exactly like `manual_checkin` does (clear `is_tardy`/`is_excused`, set `check_in_time=datetime.now()`), and records the previous state in `notes` or the tap-event log so an E is not silently lost.
- Student enrolled in TWO classes the same day gets two rows; decide which class a tap targets (Pitfall 4) and make check-out target the class with an open row (`is_present` and `check_out_time IS NULL`).

**Warning signs:**
Check-outs that vanish after a deploy/restart. `IntegrityError` in logs from the tap endpoint. Attendance count drops by exactly the number of dancers who tapped out.

**Phase to address:** P1.

---

### Pitfall 4: Reusing the legacy `_find_current_class` and `Student.rfid_uid` as-is

**What goes wrong:**
Three legacy behaviors are wrong for taps:
1. `_find_current_class` has a fallback: if no class is inside the -30/+15 minute window, return the first enrolled class that is on today's weekday, any time of day. A tap at 8 pm attributes to a 9 am class. A check-OUT tap 20 minutes after class end (parents are late) falls outside the +15 window and is attributed wrongly or not at all.
2. It ignores `Season` (classes with an inactive season), makeup classes (the app has a makeups feature), Company rehearsals and recital rehearsals (not `DanceClass` rows), and students in two back-to-back classes.
3. `Student.rfid_uid` is a single unique `String(50)`. The legacy reader wrote `str(int)` decimal from SimpleMFRC522, not hex. Reusing the column for the NTAG UID mixes two formats, allows only one tag per student (no fob + card, no replacement history), and `assign-rfid` rejects an already-assigned tag instead of supporting transfer.

**How to avoid:**
- Write a new resolver: candidates = active-season classes the dancer is actively enrolled in whose weekday is today; choose by overlap with `[start - 30 min, end + 60 min]` (make both numbers `Setting`s); if more than one matches, prefer the one with an open row for check-out, else the one starting soonest, else return `ambiguous` and let the kiosk show a class picker. Never fall back to "any class today".
- "No class today" returns a clear result (`no_class`) that the kiosk shows as an amber "No class for Ava today" with a manual-pick button, never a silent failure. Log it.
- New table `nfc_tags` instead of overloading `rfid_uid`: `id, student_id, token_hash, uid (nullable, normalized uppercase hex, no separators), label, status (active|lost|retired|pending), created_by, created_at, retired_at, last_seen_at`. A student may have several rows; only `active` rows resolve. Leave `rfid_uid` and the legacy endpoints alone or retire them deliberately; do not point new code at them.
- Check prod for any existing `rfid_uid` values before deciding to migrate or drop (RFID is disabled in prod, so expect none, but verify).

**Warning signs:**
Taps attributed to a class the dancer is not attending that day. "No class found" in `RFIDLog` for dancers who plainly had class. Staff creating manual check-ins right after a tap.

**Phase to address:** P1.

---

### Pitfall 5: Static, replayable token: a photographed or shared tag URL lets any logged-in staff session fake attendance

**What goes wrong:**
The tag holds a fixed URL. Anyone who photographs the tag's URL (NFC Tools on a phone reads it in one second, and any phone shows the URL on read) can type it into a logged-in staff browser from anywhere and the app records a check-in for that child. A cloned tag (UID or content) does the same. Remote fake check-ins also trigger a "Ava arrived" text to the parent.

**Why it happens:**
NTAG213/215 have no cryptographic authentication in the read path. The tag is a pointer, not a credential. (NTAG21x does have a password feature and NFC counter, and NXP sells NTAG 424 DNA with signed rolling URLs, but none of that can be configured or verified via Web NFC or iPhone background reading.)

**How to avoid (accept the residual risk, make it detectable and low-value):**
- Threat model it explicitly: the attacker needs a staff login plus a stolen tag URL; the payoff is a false attendance row. That is low stakes. Do not over-engineer toward NTAG 424 DNA for v2.0; note it as the upgrade path if the studio ever uses tap-out for pickup authorization (which it should not, see Pitfall 22).
- Require an authenticated staff session AND record who/how: `tap_events(tag_id, student_id, class_id, staff_user_id, method [iphone_url|android_kiosk|usb_wedge|manual], ip, user_agent, created_at, result)`.
- Time-gate: a tap only creates a check-in inside the class window (Pitfall 4), which removes "remote at 3 am" abuse for free.
- Optional cheap hardening: record `request.remote_addr` and flag taps from an IP never seen for that staff user during class hours (report, do not block).
- Never print the tag URL anywhere in the UI after enrollment (no "copy link" button, no QR of it); show only the label.
- Never put names, IDs or sequential numbers in the token. Use `secrets.token_urlsafe(16)` (128 bits). Store only `sha256(token)` so a DB or S3 backup leak does not hand over working tag URLs. Compare with a lookup on the hash.

**Warning signs:**
Check-ins whose `staff_user_id` session IP is not at the studio. A tag with `last_seen_at` while the staff member's kiosk was idle. Parent reports "I got an arrival text but my child was not there."

**Phase to address:** P1 (token + hash + event log), P7 (IP/anomaly report).

---

### Pitfall 6: Non-staff scanners (parents, other kids, strangers who find a lost fob) and parent-role users

**What goes wrong:**
Anyone's phone can read the fob and open the URL. If the route is not staff-gated BEFORE the token lookup, a parent or stranger sees a name, a "checked in" confirmation, or a different response for valid versus invalid tokens (enumeration oracle). A logged-in PARENT account that scans their own child's fob would hit a staff action.

**How to avoid:**
- Order of operations in the GET handler: check `current_user.is_authenticated and current_user.is_staff` first. If not, redirect to login with `next=/t/<token>` (the existing `_is_safe_next` allows same-site relative paths) and say nothing about the token. Do the token lookup only after the staff check. Same response shape and similar timing for known/unknown tokens.
- A parent-role user gets a neutral "This tag is for studio staff" page, no lookup. (The repo's default-deny parent write-guard already blocks POST for parents; the new POST endpoint must be `_staff_only()` and must NOT be added to the parent allowlist.)
- The login page after a stranger scans must show nothing child-specific. Do not put the studio name plus "tag for <child>" on any unauthenticated page.
- Rate limit per IP on `/t/<token>` for unauthenticated hits (the app has only an in-memory login lockout; there is no Flask-Limiter). 128-bit tokens make enumeration infeasible, so the goal is just to avoid log/DB noise. A small in-process counter keyed by IP is enough on a single worker.
- Log unknown-token hits (hash prefix, IP) for staff to review; a burst indicates someone probing.

**Warning signs:**
Different HTTP status or body length for valid vs invalid tokens to an anonymous caller. Child names visible in any unauthenticated response or in `<title>`.

**Phase to address:** P3 (and the shared POST endpoint in P1).

---

### Pitfall 7: iPhone background tag reading is not "tap and done"

**What goes wrong:**
Staff expect hands-free like the Android kiosk. On iPhone it is not:
- A notification banner appears and the user must TAP the banner for each tag. That is two actions per child. With 15 kids arriving in 5 minutes the iPhone becomes the bottleneck.
- Reading is suppressed while a Core NFC reader session is active, while Wallet/Apple Pay is in use, while the camera is in use, and in Airplane Mode (Apple tech talk 702; MEDIUM, iOS 12 era, re-verify on current iOS). A staff member who has the Camera app or a QR scanner open sees nothing happen.
- The phone must have been unlocked once since boot, and if the screen is locked the user must unlock first. Auto-Lock at 30 seconds means constant Face ID prompts.
- Only the FIRST URL record in the NDEF message is processed, and it must be a proper URI record.
- Each read opens a new Safari tab (LOW confidence, tab behavior not confirmed in an authoritative source; verify). Over a 40-tap class there are 40 tabs, with memory pressure and tab-restore reloads (Pitfall 2).
- The default-browser setting: whether iOS routes tag URLs to Safari regardless of default browser is unconfirmed (LOW). If it routes to Chrome, the staff login cookie is in a different browser.

**How to avoid:**
- Position iPhone as the lower-throughput path in the plan and docs. Primary at busy drop-off: Android kiosk page or the USB station. Do not promise "tap and it's done" for iPhone.
- Staff setup checklist (put it in the app's help page and on a laminated card): set Auto-Lock to 5 minutes or Never during class, close Camera/Wallet, Safari is the browser that is logged in, "Keep me signed in" ticked at login, add nothing to Home Screen for tap use.
- Test on real iPhones (XS or newer) in P3 before building anything on top: banner count, tab behavior, default-browser behavior, behavior with a case on, Low Power Mode.
- Write the NDEF as a single URI-type record (Pitfall 12) and nothing else.
- Make the result page full-screen, large, high-contrast, auto-readable in under a second ("Ava - checked in 5:02 PM", green; or red problem state) so staff glance and move on. Consider an optional iOS Shortcuts NFC automation as an escape hatch for "no banner tap" (LOW confidence, untested here; evaluate in P3 only if throughput is a real complaint).
- Rewrite the dismissal path: put a "Done" link that goes to `/` rather than expecting `window.close()` (browsers refuse to close tabs not opened by script).

**Warning signs:**
Lines at the table; staff skipping taps and doing manual check-ins; tabs count in the dozens; taps "doing nothing" because the Camera app is open.

**Phase to address:** P3 (verify-on-device spike first), documentation in P7.

---

### Pitfall 8: Staff login lives in the wrong place (PWA vs Safari, session expiry, `next` handling)

**What goes wrong:**
- A Home Screen (standalone) web app on iOS has its own cookie/storage container, separate from Safari (WebKit bug 181849 and others; the sources are iOS 11-14 era, MEDIUM, re-verify on current iOS). Staff who installed AttenDANCE to their Home Screen and logged in there will find that every tag tap, which opens in Safari, shows the login screen. The app ships a manifest (`display: standalone`) and a service worker, so staff will install it.
- `PERMANENT_SESSION_LIFETIME` is 8 hours and the remember cookie is 14 days. A tap mid-class after expiry stalls the line at a password prompt.
- Flask-Login has `login_view = 'auth.login'` and no `unauthorized_handler`. A `fetch` to `/api/...` with an expired session gets a 302 to the login HTML; `res.json()` throws and the kiosk shows nothing or a generic error while the fob beeps.
- `SESSION_COOKIE_SAMESITE = 'Lax'` is correct for this flow: opening a URL from an NFC tag is a top-level navigation, so Lax cookies are sent on the GET. Changing it to Strict to "harden" would drop the cookie on tag-initiated navigations from outside the site. Do not change it. The follow-up `fetch` POST from the loaded page is same-site, so it carries the cookie. (Lax behavior is HIGH confidence; navigation-from-an-NFC-tag context specifically is MEDIUM, verify in P3.)

**How to avoid:**
- Staff onboarding: log in once IN SAFARI with "remember me" before class. Tell staff the Home Screen app and tag taps do not share a login.
- The tap endpoints return JSON `401 {"error":"signed_out"}` (use `request.accept`/path check or a blueprint-specific unauthorized handler), and the page/kiosk renders a loud red "Signed out - tap here to sign in" state with a sound. After login, `next` returns to `/t/<token>` and completes the original tap (this works with the existing safe-next code, so test it).
- Consider a longer remember-me for staff only (the 14-day remember cookie already covers most cases; confirm the login form defaults "remember me" on for staff phones).
- Kiosk: on 401, stop scanning loop and show the banner; do not keep POSTing.

**Warning signs:**
Staff report "it asks me to log in every time." Tap events missing for a window of time after the 8-hour mark. Kiosk console full of `Unexpected token '<'`.

**Phase to address:** P3 (login-then-resume, 401 JSON), P4 (kiosk handling), P7 (staff checklist).

---

### Pitfall 9: Notification fan-out blocks the single worker and floods parents

**What goes wrong:**
Sending SMS (15 s timeout in `send_sms`) and email (20 s SMTP timeout, new connection + STARTTLS + login per call in `send_email`) inside the tap request holds one of the 4 gthread threads for up to 35 s per tap. Four simultaneous taps exhaust the worker; login and every other page 502 or hang (the same failure mode documented for the 256 MB machine). Siblings arriving together generate N texts to the same phone within seconds. A double tap that slips through sends two texts. A check-out text arrives when the child was only misread.

**How to avoid:**
- Tap request does: write attendance + tap event + insert a `notification_outbox` row (dedupe key `student_id|class_id|date|event`), then returns. A single background drain (the app already starts daemon threads for reminders and is careful about not spawning per request) picks up rows. Because Fly can stop the machine, the outbox must be durable in SQLite and drained on startup too, not held in memory.
- Coalesce by household: one message per household per event per 60-90 s window ("Ava and Zoe arrived at 5:02 PM"). Reuse the existing household grouping used by `_notify_group_balance`.
- Send email by default and SMS only when consented (Pitfall 20). Reuse one SMTP connection per drain run rather than one per message.
- Idempotent: a unique constraint on the dedupe key makes a double tap impossible to double-send.
- Short, first-name-only content: "Ava arrived at 5:02 PM." No last name, no class room, no balance, no "left" language that implies custody (Pitfall 22).
- Failures: retry with backoff, cap attempts, mark `failed`, never block attendance.

**Warning signs:**
`gunicorn` worker timeouts or 502s while a class is arriving. Parents replying "why did I get 3 texts." Outbox rows stuck `pending` after a machine restart.

**Phase to address:** P6, with the "tap never sends inline" rule enforced from P1 (a stub notification hook only).

---

### Pitfall 10: Cold start and bad connectivity make taps vanish silently

**What goes wrong:**
`fly.toml` has `auto_stop_machines = 'stop'` and `min_machines_running = 0`. The first tap of the day wakes a stopped machine; boot runs `run_migrations`, `db.create_all`, recurring-charge processing, and the dedupe DELETE. That can take many seconds, during which Safari spins and the child (and staff) tap again (Pitfall 1). Separately, the community-center Wi-Fi is unreliable. On iPhone, a failed page load means the tap is lost with no queue (the page never loaded, so there is no JS to queue it). On the Android kiosk the fetch fails and the scan is dropped.

**How to avoid:**
- Set `min_machines_running = 1` during class hours or add a scheduled keep-warm ping (the app already has a cron endpoint, `api.cron_run`). The cost of one always-on 256 MB machine is small compared with failed first taps. Alternatively, a keep-warm call 10 minutes before the first class of the day.
- Make the tap route and its landing page light: no heavy imports, no startup work, no CDN.
- Recommend staff phones use cellular data, not the center's Wi-Fi, for the tap pages. Say so in the staff checklist.
- Android kiosk: queue taps in IndexedDB with `{token, client_epoch_ms}`, retry with backoff, show a "N taps pending" counter. Server converts client time to studio-local naive using `zoneinfo.ZoneInfo('America/Detroit')`, never `datetime.fromtimestamp` blindly, and rejects client times more than a few hours old or in the future. The container has `TZ=America/New_York` which matches Detroit rules today, but make the conversion explicit.
- iPhone and USB paths: no offline queue possible for iPhone; state plainly that a failed load means "use the manual attendance card for this kid." The USB wedge page can queue like the kiosk.
- Always show an unmistakable failure state (big red, sound) rather than a spinner that looks like it is working.

**Warning signs:**
Staff say "it was slow the first time." Gaps where dancers were present (manual check-ins by teacher later) but no tap event. Spikes of `duplicate` results right after the first tap of the day.

**Phase to address:** P1 (light route, server-time conversion), P4/P5 (queue), P7 (Fly config + staff checklist).

---

## Moderate Pitfalls

### Pitfall 11: Tags are not what the listing says, or arrive unformatted

**What goes wrong:**
Cheap listings sell MIFARE Classic / FM11RF08 / "NTAG215-compatible" clones labelled NTAG. iPhones cannot read MIFARE Classic at all (already out of scope per PROJECT.md). Clones may have changeable UIDs, a wrong capacity, or a bad originality signature. Some tags come with no NDEF capability container, so Web NFC `write()` throws `NotSupportedError`/`NetworkError`. An unformatted NDEF tag contains a single empty-type record.

**How to avoid:**
- P0: buy a 5-tag sample from a reputable NFC vendor (not a marketplace lot), check each with the free NXP TagInfo app (chip type, capacity, originality signature valid for NTAG21x), then write one with the actual in-app flow on a real Android phone and read it on a real iPhone before ordering 100+.
- Order "NDEF-formatted" NTAG213 or 215. NTAG213 (144 bytes) is plenty for a ~40-character URL; 215 is not needed unless the shopping list finds a price/durability reason.
- Test every tag in the enrollment flow (read-back verify catches dead or clone chips, Pitfall 14).

**Warning signs:** `NotSupportedError` on write; TagInfo shows a MIFARE chip; the signature check fails; capacity differs from the listing.

**Phase to address:** P0, enforced again in P2.

### Pitfall 12: Wrong NDEF record type, extra records, or a locked tag on a domain that may change

**What goes wrong:**
- Web NFC offers `recordType: "url"` and `"absolute-url"`. iPhone background reading needs a proper NFC Forum URI record; an absolute-URI (TNF 3) record may not trigger it (MEDIUM, verify). Writing an extra record (a text record "Ava" for convenience) before the URL violates the "first URL record" rule and puts PII on the tag, which the project forbids.
- `makeReadOnly()` is permanent and irreversible (Chrome docs, HIGH). The URL points at `attenddance.fly.dev`. The studio is moving to an owned building and a custom domain is plausible; a renamed Fly app or domain change would brick every locked tag.
- If NOT locked, anyone with a free NFC app can overwrite a fob (a tween rewriting a friend's fob to a prank URL). Both choices have a cost.

**How to avoid:**
- Decide the permanent hostname BEFORE writing any tag. If a custom domain is coming, buy/point it first and write that. Put a 301 redirect on the old host as a safety net.
- Single URI record (`recordType: "url"`), nothing else. No AAR, no text record.
- Default: do not lock in the pilot (first ~10 tags), because a wrong URL is recoverable. Lock the production batch only after the domain is final and the full flow (iPhone read, Android read, USB read) works. Make locking a separate explicit step with a confirmation that states "cannot be undone," never part of the default write.
- Revocation is server-side (token status), so a locked tag is still safely retireable.

**Warning signs:** iPhone does nothing on a tag that Android reads fine; a tag you cannot rewrite after a hostname change.

**Phase to address:** P2 (record type, no-lock default), P0/P2 gating on the hostname decision.

### Pitfall 13: Enrollment overwrites an already-assigned fob or leaves DB and tag disagreeing

**What goes wrong:**
Web NFC `write()` overwrites by default. A staffer who taps a fob that already belongs to Ava while enrolling Zoe silently replaces it and orphans Ava's binding (still `active` in the DB, now pointing at a tag that holds a different token). A write interrupted when the tag moves away can leave a corrupt NDEF message (the page only resolves when the write completes). The DB then has a token the tag does not carry, or the tag carries a token the DB never saved.

**How to avoid:**
- Student-first flow: choose the student on screen, then tap the tag. Never write a batch of tags and assign them later.
- Before writing, `scan()` the tag; if it already carries one of our tokens bound to an active student, refuse and show "This fob is Ava's."
- Use `{overwrite: false}` for brand-new blanks; use an explicit "replace" flow for reuse, which retires the old tag row.
- Sequence: create `nfc_tags` row as `pending` with the new token hash -> write -> read the tag back and compare the URL -> only then mark `active`. A failure leaves a `pending` row you can garbage-collect.
- Capture and store the tag's `serialNumber` (normalized) at this moment (needed for the USB path, Pitfall 16). Enrollment on iPhone is impossible (Safari has no Web NFC and background reading does not expose the UID), so state clearly: enrollment requires an Android phone or the USB reader.
- Show an immediate echo: "Enrolled fob T-014 to Ava Smith. Tap it again to test." with a test-tap button that does not record attendance.

**Warning signs:** a student with an active tag that returns "unknown tag" on test; two students resolving from one UID.

**Phase to address:** P2.

### Pitfall 14: Losing track of which tag belongs to which kid during bulk enrollment

**What goes wrong:**
Handing out 80 identical fobs at once. Labels written on fobs are PII on a hardware object (violates "tag carries no student data" in spirit and in the COPPA constraint). Without any marking, a fob found on the floor cannot be traced to a kid except by tapping it.

**How to avoid:**
- Give each tag a short, non-identifying label shown in the UI (`T-014`) and, if the supplier can engrave or print it, on the fob. Keep a reconciliation list: "Tags issued, tags unassigned, tags lost."
- Enroll per class at the studio, one child at a time, with the student-first flow. Provide a "bulk enroll a class" screen that walks the roster, shows who still has no tag, and advances automatically after a successful verified write.
- Roster report of dancers with no active tag, and of tags with no tap in 30 days.
- Do not use a child's name, initials, or birthday on the fob.

**Warning signs:** staff keep a handwritten list; fobs left in a drawer with no label; a dancer with two active tags.

**Phase to address:** P2.

### Pitfall 15: Web NFC kiosk behavior (Android)

**What goes wrong:**
Chrome docs (HIGH): the page must be visible; NFC is blocked when the display is off or the device is locked; scanning pauses when the page is hidden and resumes on visibility; permission is requested via a user gesture; `readingerror` fires for incompatible tags; Android handles tags at the OS level before Web NFC becomes active (so a tag read while Chrome is not foreground may open the URL in a new Chrome tab, which actually still works but bypasses the kiosk UI). Practical failure modes:
- Screen times out mid-class and the kiosk silently stops. Staff think it is running.
- Same tag fires multiple `reading` events while held near the phone.
- Permission prompt appears on first use and again after site data is cleared or in incognito.
- `scan()` called twice (e.g., on a visibility re-init) stacks two readers and double-fires every tap.
- `AbortController` not used, so there is no clean stop.

**How to avoid:**
- Start with a big "Start scanning" button (the user gesture) and show a live status chip: "Scanning" green, "Paused (screen hidden)" amber, "Signed out" red, "Offline - N queued" amber.
- Use the Screen Wake Lock API to keep the display on, and re-acquire it on `visibilitychange` (the lock is released when the tab hides). Staff should also set the phone to stay awake while charging.
- One `NDEFReader` instance, one `AbortController`; on visibility restore, abort and re-scan rather than stacking.
- Client debounce on `serialNumber` plus the server cooldown (Pitfall 1).
- Beep + vibrate + big colored name on every read. Audio needs the initial gesture to unlock playback.
- Serve over HTTPS only (already forced by `force_https`).
- Feature-detect: `'NDEFReader' in window`; on Chrome for Android only (also fails on Firefox, Samsung Internet behavior unverified). Show a clear "Use Chrome" notice otherwise.
- Do not poll the server from the kiosk to refresh rosters more often than every 30-60 s: three kiosks polling every 2 s starve the 4 threads and keep the Fly machine awake.

**Warning signs:** kiosk "stopped working" reports after 1-2 minutes; two tap events per tag; the status chip never changes.

**Phase to address:** P4.

### Pitfall 16: USB keyboard-wedge reader typing into the wrong place

**What goes wrong:**
The reader types the UID as keystrokes into whatever has focus, then usually Enter. If focus is a search box, a notes field, or a button, you get garbage in the field, an accidental form submit, or an Enter keypress that activates a focused "Delete" / "Confirm" button. Other issues:
- Keyboard layout: readers emit US scancodes. On a non-US layout hex letters can come out wrong. Caps Lock flips A-F case.
- UID format: some readers output 8 hex chars (4 bytes), some 14 (7 bytes), some a 10-digit DECIMAL of only 4 bytes, some with colons, and some in reversed byte order (LSB first). A reader that reports only 4 bytes for a 7-byte NTAG UID is reporting the first cascade level (starting `88`), which has about 16 bits of entropy and WILL collide across a batch of 100+ tags and offers no usable identity. Legacy values in `Student.rfid_uid` are decimal ints from MFRC522, a third format.
- UID is public and cloneable; the USB path authenticates on UID alone, a weaker path than the token (acceptable for attendance, state it).
- iPad on-screen keyboard behavior when a hidden input is focused varies; iPadOS may not keep focus.

**How to avoid:**
- Dedicated `/station` page with NO other inputs. Capture with a document-level `keydown` listener and a buffer, not input focus: accept a burst as a scan only if inter-key gaps are under ~50 ms and it ends with Enter; ignore human typing speed. Prevent default on Enter. Refocus handling not needed if there is no input.
- Normalize on the server: strip `:`/spaces/`-`, uppercase, validate with a regex for 8 or 14 hex chars, store canonical uppercase hex of the full 7-byte UID, and keep a `uid_byte_order` setting if the reader reverses it. Detect and reject 10-digit-decimal-only output and tell the operator to reconfigure the reader (most readers have a config tool to choose hex, byte order and terminator).
- P0 acceptance test for the chosen reader: scan 3 different NTAG tags and confirm the output is 14 hex chars and matches what Web NFC `serialNumber` returned during enrollment.
- Capture the UID at enrollment (Web NFC `serialNumber` on Android, or the wedge reader itself on the station page). Enrollment from iPhone cannot give a UID.
- Station page shows each scan result large with name and class, same as the kiosk.

**Warning signs:** UIDs with colons vs without in the DB; two tags matching one UID; stray digits showing up in other fields; scans that "work" for some tags and not others (byte order or 4-byte truncation).

**Phase to address:** P0 (reader acceptance test), P5.

### Pitfall 17: Physical NFC realities (range, interference, durability)

**What goes wrong:**
Read range is about 1-4 cm. Metal phone cases/plates, MagSafe wallets with cards, and tags stuck on or near metal (a metal zipper, a key ring, a metal dance bag) fail to read or read intermittently. Antenna location differs per phone (iPhone near the top back, Android varies). Young children hold the fob at the wrong spot or too briefly. Epoxy-coated fobs survive being dropped and washed better than thin PVC cards; ABS cases crack; adhesive stickers peel. Cards in a pocket next to a phone/other NFC card can collide. A 3-year-old will chew it.

**How to avoid:**
- Staff phone: remove metal cases/wallet-backs; mark the phone's NFC spot with a sticker or place a small physical cradle/stand so a fob "taps the target."
- Choose epoxy or silicone-wrapped fobs on lanyard clips or sewn into dance-bag zips for the youngest classes; thin cards for adults. Order a spare 10-15% (lost/broken).
- Have staff do the tap for the youngest dancers (ages 3-5) as an explicit workflow rather than expecting the child to do it.
- Include "tag does not read" in the staff checklist: try the other side, remove case, use manual card.

**Warning signs:** "works only if I hold it for 3 seconds"; failures concentrated on certain dancers/phones.

**Phase to address:** P0 (shopping), P7 (staff checklist).

### Pitfall 18: Time zone and date logic for check-in/out times

**What goes wrong:**
`check_in_time` is a studio-local naive datetime (`datetime.now()` with container `TZ=America/New_York`). `rfid_assigned_at` is UTC (`utcnow`). A new `check_out_time` or tap-event timestamp set with `utcnow()` lands on the wrong day in the evening and breaks `date(check_in_time)` comparisons, the unique index, and duration math (`check_out_time - check_in_time` becomes hours off, or negative). Kiosk-queued client timestamps are an extra source of mixing. Parent messages ("arrived at 5:02 PM") built from the wrong basis show wrong times.

**How to avoid:**
- One rule: every new datetime column for attendance/tap data is studio-local naive via `datetime.now()` (or an explicit `zoneinfo` conversion), documented in the model comment. Name columns so the basis is obvious, and keep `created_at`-style audit fields in the same basis, or state otherwise.
- Do not copy `rfid_assigned_at = datetime.utcnow()`; for new fields follow the attendance convention.
- Add a unit test with the clock mocked to 8:30 PM local that asserts a tap-out dates to the same calendar day and the duration is positive.
- Format message times server-side in studio local time.

**Warning signs:** negative `duration`; check-outs dated tomorrow; evening taps missing from the day's roster.

**Phase to address:** P1.

### Pitfall 19: Tardiness, excused status and teacher's manual card collide with taps

**What goes wrong:**
Teachers already mark P/A/T/E on the attendance card. Auto-marking tardy from a tap time silently changes their convention; a teacher who marked A then sees the dancer flip to present; an E gets overwritten. The attendance list (`attendance/list.html`) and the dashboard branch on `check_in_method === 'rfid'` to pick an icon and label, so new method strings (`nfc_phone`, `nfc_kiosk`, `usb`) render as the "hand" icon, undefined behavior.

**How to avoid:**
- Auto-tardy is OFF by default; a `Setting` with a grace minutes value turns it on. When the tap flips an A/E row, write the previous state to the tap event/notes, and show the flip to staff on the kiosk.
- Keep `check_in_method` values to a short controlled set; update both templates in the same change (or use a single value `nfc` plus a separate `source` column on the tap event).
- Show check-out time on the attendance card and roster, otherwise `check_out_time` exists but staff never see it.

**Warning signs:** teachers re-marking after taps; unknown method strings in the UI.

**Phase to address:** P1 (semantics), P7 (UI wiring check).

---

## Notification / compliance pitfalls

### Pitfall 20: SMS consent, opt-out, registration and cost

**What goes wrong:**
- US carriers block unregistered A2P 10DLC traffic (Twilio error 30034, and blocked messages are still billed); campaign approval can take 2-3 weeks (Twilio support/changelog, MEDIUM). If the studio's Twilio number was never registered, the balance reminders may already be failing silently, and arrival texts at 10x the volume will make it obvious. Verify the existing number's status first.
- STOP: Twilio handles STOP/UNSUBSCRIBE on long-code and toll-free numbers by default and rejects further sends with error 21610. `send_sms()` returns False and logs an error on every attempt, so the app keeps retrying on every tap forever and the staff never learn the parent opted out.
- Consent: TCPA expects prior express consent for automated texts. A phone number given at registration is not obviously consent to arrival/departure alerts. This is a legal-adjacent call for the studio; flag it, capture explicit opt-in, do not treat the code as the compliance answer.
- Recipient selection: the existing `_student_phone` returns `parent_phone`, else family primary phone, else the student's OWN phone. Adults (ages up to 83) and teens would be texted about themselves, and a stale or wrong number would deliver a child's name and location to a stranger.
- Cost: roughly 2 events x each dancer x each class. At 100 dancers x 2 classes/week that is ~400 texts per week, plus carrier fees and the 10DLC monthly campaign charge. Email is free, so the default should be email.
- Gmail SMTP: `send_email` uses the studio Gmail account. Consumer Gmail has a daily sending cap (about 500 recipients/day) and the same account sends receipts and blasts. Two emails per dancer per class can exhaust the cap and break receipts for the day.

**How to avoid:**
- Per-household notification preferences on the family/guardian record, default OFF, enabled by an explicit parent action in the portal (or at registration with clear wording that includes "Reply STOP to opt out"). Per-event toggles (arrival, departure).
- Only message guardian numbers (`parent_phone`, `secondary_phone`), never the dancer's own, and skip notification entirely for dancers marked adult (date of birth or an `is_adult`/self-managed flag).
- Persist opt-outs: on Twilio 21610, store `sms_opted_out_at` for that number and fall back to email; add an inbound webhook only if needed (Twilio already enforces STOP).
- Message template: first name only, studio name, time, "Reply STOP to opt out" on the first message and periodically.
- Respect quiet hours (do not send departure texts at 11 pm for late adult classes).
- Budget: show the studio an estimated monthly count in settings; add a global kill switch `Setting` and a daily cap.
- Cap email volume: prefer one digest-style or coalesced message per household (Pitfall 9) and monitor the Gmail daily limit; consider a transactional email provider if volume grows.
- Run the P6 spike: send one real arrival SMS from the production number and one email to confirm registration status and deliverability before building the feature on top.

**Warning signs:** Twilio error codes 30034/21610 in logs; delivery failures only for some carriers; parents complaining about unsolicited texts; Gmail "daily limit exceeded".

**Phase to address:** P6 (design), P0/P6 spike for Twilio registration.

### Pitfall 21: COPPA / privacy: tap data is new PII and the PII rule applies

**What goes wrong:**
Arrival/departure times and the dancer-to-tag link are location/presence data about minors. The project rule says any new PII field updates the model, `privacy.html`, and the `/api/my-data` export in the same commit. Tap events and tag labels stored without that update break the stated policy and the deletion-request flow. Kiosk screens facing a line of families show names. Balance/waiver flags on a screen visible to the queue expose financial status ("owes $240") publicly.

**How to avoid:**
- Treat `nfc_tags` and `tap_events` as PII tables: add to the privacy page, `/api/my-data`, and the deletion-request workflow (decide retention: e.g. delete raw tap events after N months, keep attendance).
- Tag carries no PII (token only). Logs and Fly output carry hash prefixes, never names or full tokens.
- Kiosk flags show a neutral icon and short text ("See front desk") with NO dollar amount and no waiver names; amounts only on the staff-only student page. Auto-clear the result card after ~5 s. Prefer first name + initial on the shared screen, full name on a long-press / detail.
- Ask: does the studio need parents' consent for notifications and electronic check-in recording? Add a line to the registration/waiver flow (waivers already support opt-out forms).
- Do not store or display photos in this milestone (new PII, new risk).

**Warning signs:** privacy page not mentioning check-in records; the data export missing tap history; a screenshot of a kiosk showing a balance.

**Phase to address:** P7, with the schema in P1 planned to include the retention field.

### Pitfall 22: Selling it as a safety or pickup system

**What goes wrong:**
Fob-based "left class" texts invite the expectation that the studio verified who took the child. The tap proves a fob was presented to a phone, nothing more. Kids swap, lose and share fobs; a 3-year-old taps because a staff member held the fob to the phone. A parent relying on "Ava left" as proof of safe pickup creates liability.

**How to avoid:**
- Copy and docs say "attendance record" and "arrival/departure notice," never "pickup verification." Departure text wording: "Ava was checked out at 5:58 PM," and do not send it automatically for the youngest age group unless staff confirm (consider a `confirm departure` staff tap for under-N-year-olds).
- Staff always see the dancer's name and class on tap (the swap defense). Physical handoff stays a staff responsibility.
- Lost/found fob flow (Pitfall 23) shows a red "LOST TAG - take it" alert instead of a silent failure.

**Phase to address:** P6 (wording/confirmation), P7 (staff training copy).

### Pitfall 23: Lost, replaced, swapped, or retired tags

**What goes wrong:**
A lost fob that a staff member later taps (found on the floor, handed in) silently checks in a dancer who is not there. Replacement under the old model (`assign-rfid` rejecting a duplicate, `remove-rfid` nulling) loses history. Kids swap fobs and no one notices.

**How to avoid:**
- `status` values `active|lost|retired|pending`. A student page action "Report lost" sets `lost` (not delete). A tap on a `lost` or `retired` tag does not record attendance and shows a red alert with the dancer's name and "take this tag."
- "Replace tag" flow = retire old row + enroll new in one action, history kept.
- Show last-tap time and a count per tag on the student page; flag a tag used in two classes at the same time or by a dancer shown absent by the teacher.
- The kiosk shows the name large so staff can compare with the child standing in front of them; train staff to glance.

**Phase to address:** P2 (lifecycle), P4/P5 (alert display).

---

## Technical Debt Patterns

| Shortcut | Immediate Benefit | Long-term Cost | When Acceptable |
|----------|-------------------|----------------|-----------------|
| Store token in plain `Student.rfid_uid` | No migration | One tag per student, mixed formats, no history, leaks working URLs from DB backups | Never |
| Store raw token instead of hash | Can display/rewrite URL later | Backup leak yields usable tag URLs | Only if you need re-display, which you do not |
| Process tap in GET handler | One round trip | Reload/prefetch double taps (Pitfall 2) | Never |
| In-memory dedup (dict / last-scan variable) | Quick | Lost on Fly restart; broken with A,B,A taps | Only as a second layer on top of DB dedup |
| Send SMS/email inline in the request | Simple code | Worker starvation on a single gthread worker | Never |
| `makeReadOnly` on every write | "Secure" tags | Bricked fleet if the domain changes | Only after hostname is final, for the production batch |
| Skip the read-back verify after writing | Faster enroll | Orphan or corrupt tags | Never |
| Reuse `_find_current_class` fallback | No new code | Wrong-class attribution | Never |
| Hard-code windows (30 min, 15 min, dwell) | Fast | Studio cannot tune per class | MVP only, move to `Setting` quickly |
| Skip `nfc_tags.status` and just delete | Simple | Loses audit trail, lost-tag alerts impossible | Never |

## Integration Gotchas

| Integration | Common Mistake | Correct Approach |
|-------------|----------------|------------------|
| Flask-Login | `/api` 302-redirects to login on expiry, JSON clients choke | Return JSON 401 for tap endpoints; render a clear signed-out state |
| `_csrf_origin_guard` | Calling the tap POST from a non-same-origin context (e.g., a prefetch or an external page) | Fetch from the same origin page; keep Origin check; do not exempt the tap endpoint |
| Fly auto-stop | First tap wakes a cold machine | `min_machines_running=1` in class hours or keep-warm ping |
| SQLite unique day index | Insert second row for check-out | Update the same row, catch `IntegrityError` |
| Twilio | Ignore 21610 / 30034 | Persist opt-out, register 10DLC, surface failures |
| Gmail SMTP | Per-message connect + login; shared daily cap | Reuse one connection per drain, coalesce, watch cap |
| Web NFC | Treating `serialNumber` as always present or in one format | It may be empty; normalize colon-separated hex |
| iOS Safari | Assuming same session as the PWA | Separate container; log in in Safari |
| Service worker (`sw.js`) | Navigations network-first with an offline notice | Fine for tap pages, but never cache them; keep `no-store` |
| Existing privacy/export | Forget `/api/my-data` and `privacy.html` | Update in the same commit (project rule) |

## Performance Traps

| Trap | Symptoms | Prevention | When It Breaks |
|------|----------|------------|----------------|
| Inline notification send | Login 502 during arrival rush | Outbox + background drain | 4 concurrent taps (4 gthread threads) |
| Kiosk polling | Machine never sleeps, thread starvation | No polling or 30-60 s interval | Multiple kiosks at 1-2 s |
| Heavy base template on tap page | 3-10 s load on weak Wi-Fi, double taps | Standalone minimal page | Any CDN stall |
| `RFIDLog` and `tap_events` unbounded growth | SQLite volume and S3 backup grow | Retention job; log only meaningful events | Months of daily taps |
| Cold start migrations | First tap of the day takes many seconds | Keep warm; light route | Every morning with auto-stop |
| Per-tap full roster queries | Slow tap | Index on `nfc_tags.token_hash`, single-row lookups | Hundreds of dancers |

## Security Mistakes

| Mistake | Risk | Prevention |
|---------|------|------------|
| Token lookup before auth check | Enumeration oracle, info leak | Staff check first, uniform responses |
| Predictable or short tokens | Guessable | `secrets.token_urlsafe(16)` or longer |
| Token in logs / history / `title` | Replay by log reader | Hash-prefix logging, redirect to token-free result page |
| Trusting UID alone on the wedge path | Cloning with magic UID cards | Accept as weaker path; log method; staff glance check |
| Parent-role user can hit tap routes | Parent writes attendance | `_staff_only()`, keep out of the parent allowlist |
| Writing the tag URL on an unfinalized host | Bricked tags | Fix hostname first (Pitfall 12) |
| No audit of who tapped | Cannot investigate fake check-ins | `tap_events` with staff user, method, IP, result |
| Kiosk left logged in and unlocked | Names visible to anyone | Auto-clear result card, lock phone, short idle sign-out for kiosk mode |

## UX Pitfalls

| Pitfall | User Impact | Better Approach |
|---------|-------------|-----------------|
| Tiny result text | Staff cannot glance | Full-screen color + big name + time |
| Silence on failure | Child walks in unrecorded | Red + sound + haptic where available |
| No explicit "no class today" | Staff think the tap worked | Amber state with manual pick |
| Spinner on cold start | Double taps | Immediate "working..." with elapsed time, idempotent server |
| Balance shown on a shared screen | Embarrassment, privacy | Neutral "See front desk" icon |
| iPhone banner requires tap | Slow line | Document it; offer Android/USB for peak |
| Asking staff to log in mid-line | Stall | Pre-login in Safari with remember me |
| Parent gets "left" for a misread | Panic | Dwell time and cooldown (Pitfall 1) |

## "Looks Done But Isn't" Checklist

- [ ] **Tap on iPhone:** works on your phone but not tested with Camera open, locked screen, Low Power Mode, case on, Chrome as default browser. Verify each on a real device.
- [ ] **Login-then-resume:** logged-out stranger flow sends through login and completes the original tap. Verify with `next` carrying the token path.
- [ ] **Expired session on the kiosk:** shows a red signed-out state. Verify by deleting the session cookie.
- [ ] **Double tap / reload / back:** exactly one attendance row, one outbox row, zero extra texts. Verify by tapping twice and reloading.
- [ ] **Restart safety:** a check-out survives a machine restart (the dedupe DELETE). Verify by tapping out, stopping and starting the machine.
- [ ] **No class today / two classes / late pickup:** each gives a clear result. Verify with seeded data.
- [ ] **A/E row flip:** teacher-marked absent/excused is handled and logged.
- [ ] **Tag write:** read-back verified, student-first, refuses to overwrite an enrolled fob.
- [ ] **USB reader output:** 14 hex chars for 7-byte UIDs, matches the Web NFC `serialNumber`.
- [ ] **Lost tag:** tap shows red alert, no attendance row.
- [ ] **Notifications:** one coalesced message per household, consent respected, 21610 handled, 10DLC registered, Gmail cap watched.
- [ ] **Privacy:** `privacy.html`, `/api/my-data`, deletion request, retention updated.
- [ ] **Offline/queue (Android):** queued taps replay with correct local times.
- [ ] **Stale comments:** `Attendance.__table_args__` comment corrected; new columns documented with their time basis.
- [ ] **Hostname final** before any production tag is written or locked.

## Recovery Strategies

| Pitfall | Recovery Cost | Recovery Steps |
|---------|---------------|----------------|
| Duplicate/accidental check-outs | LOW | Query rows with duration under 5 min, null `check_out_time`, resend nothing; fix dwell rule |
| Tags written to wrong host | HIGH if locked, LOW if not | Unlocked: rewrite in the enrollment flow. Locked: add a redirect from the old host forever; reorder tags |
| Orphan/duplicate token bindings | MEDIUM | Reconcile report; retire pending rows; re-enroll affected dancers |
| Wrong class attributions | LOW | Re-run resolver over the day's tap events and correct rows (tap events keep raw data) |
| Twilio blocked / opted-out | MEDIUM | Complete 10DLC registration; mark opt-outs; fall back to email |
| Gmail cap exceeded | LOW | Pause tap emails via kill switch; coalesce; consider a transactional provider |
| Leaked tag URLs | MEDIUM | Retire and re-enroll affected tags (server-side revocation; tokens hashed so a DB leak exposes less) |
| Counterfeit batch | HIGH | Return/replace; re-enroll; keep sample-test gate in P0 for any reorder |

## Pitfall-to-Phase Mapping

| Pitfall | Prevention Phase | Verification |
|---------|------------------|--------------|
| 1 Double tap = surprise check-out | P1 (+P3/P4/P5) | Tap twice rapidly: one row, no checkout; dwell test |
| 2 GET side effects / reload | P3 | Reload and back after a tap: no new event |
| 3 Unique index / one row | P1 | Tap in, out, restart: row intact |
| 4 Legacy class resolver / rfid_uid | P1 | Seeded scenarios incl. no class, two classes, late out |
| 5 Replayable token | P1, P7 | Event log shows who/how; token hashed in DB |
| 6 Non-staff scanners | P3 | Anonymous and parent-role requests reveal nothing |
| 7 iPhone behavior | P3 | On-device matrix (camera, lock, case, default browser) |
| 8 Login/PWA/session | P3, P4, P7 | Expired-session JSON 401; login-resume works |
| 9 Notification fan-out | P6 | Rush test: 10 taps in 30 s, no 502, coalesced |
| 10 Cold start / connectivity | P1, P4, P5, P7 | Cold-start tap; offline queue replay |
| 11 Counterfeit/unformatted tags | P0, P2 | TagInfo signature check; write and read test |
| 12 Record type / locking / hostname | P0, P2 | iPhone opens tag; domain decision recorded |
| 13 Enrollment overwrite/verify | P2 | Attempt to overwrite an enrolled fob is blocked |
| 14 Bulk enrollment tracking | P2 | Roster shows who has no tag; labels non-identifying |
| 15 Web NFC kiosk behavior | P4 | 10-minute soak with screen timeout; one event per tap |
| 16 USB wedge | P0, P5 | 3-tag reader test; focus-independent capture |
| 17 Physical NFC realities | P0, P7 | Trial with real fobs, cases, young children |
| 18 Time zone | P1 | Mocked 8:30 pm test |
| 19 Tardy/A/E collisions | P1, P7 | Flip tests; UI method icons |
| 20 SMS consent/10DLC/Gmail cap | P6 | Real send test; 21610 handled; counts per week |
| 21 COPPA/privacy | P7 | Privacy page, export, delete flow updated |
| 22 Safety framing | P6, P7 | Copy review |
| 23 Lost/swapped/retired tags | P2, P4/P5 | Lost tag tap shows red alert, no row |

## Sources

- Codebase (HIGH): `app/models.py` (Student.rfid_uid, Attendance, RFIDLog), `rfid/service.py` (single `last_scan_uid` dedup, `_find_current_class` fallback), `rfid/reader.py` (decimal `str(uid)`), `app/migrations.py::_enforce_attendance_uniqueness`, `app/sms.py`, `app/email.py`, `app/__init__.py` (CSRF origin guard, security headers, login_view with no unauthorized handler), `config/config.py` (session 8 h, remember 14 d, SameSite Lax), `fly.toml` (auto_stop, 256 MB), `Dockerfile` (TZ=America/New_York), `app/api/routes.py` (`manual_checkin`, `_student_phone`, `_staff_only`), `app/static/sw.js`, `app/static/manifest.json`.
- Chrome Web NFC documentation, https://developer.chrome.com/docs/capabilities/nfc (HIGH): permission by user gesture, visibility and display-off behavior, `makeReadOnly` permanence, `overwrite` option, HTTPS only, empty unformatted record, `serialNumber` may be empty.
- Apple tech talk 702 on background tag reading, https://developer.apple.com/videos/play/tech-talks/702 and GoToTags summary, https://gototags.com/help/ios/nfc/reading/background (MEDIUM, iOS 12 era): banner tap, suppression during Core NFC session/Wallet/camera/Airplane Mode, first URL record only, Safari handles non-app URLs, unlock needed when locked.
- WebKit bug 181849 and related, https://bugs.webkit.org/show_bug.cgi?id=181849 and netguru write-up (MEDIUM, iOS 11-14 era): Home Screen web apps do not share storage with Safari. Re-verify on current iOS.
- NXP NTAG213/215/216 datasheet, https://nxp.com/docs/en/data-sheet/NTAG213_215_216.pdf and GoToTags originality-signature note, https://gototags.com/help/nfc/chip/features/originality-signature (MEDIUM): ECC originality signature, UID/signature manipulation on configurable clone chips.
- Twilio 10DLC enforcement, https://www.twilio.com/en-us/changelog/-u-s--a2p-10dlc--full-blocking-of-traffic-sent-from-unregistered and opt-out keyword handling, https://support.twilio.com/hc/en-us/articles/223134027 (MEDIUM): unregistered 10DLC blocked (30034), default STOP handling, 21610 on sends to opted-out numbers.
- Keyboard-wedge reader format notes: vendor pages (ACS ACR1281U-C2 config tool, https://rfid.it/en/acs/436-acr1281u-c2-nfc-uid-reader.html) and the NFC UID Keyboard Emulator project, https://github.com/mkapulica/NFC-UID-Keyboard-Emulator (MEDIUM for format/byte-order options; the 4-byte cascade-level/collision reasoning is LOW and derived from the NFC UID structure, verify with the actual reader in P0).
- Unverified, flagged LOW: new Safari tab per tag read; behavior when the iOS default browser is not Safari; iOS Shortcuts NFC automation as a no-banner path; Samsung Internet Web NFC support; TCPA applicability to informational arrival alerts (not legal advice, confirm with the studio's counsel).

---
*Pitfalls research for: NFC tap-in attendance added to AttenDANCE*
*Researched: 2026-10-14*
