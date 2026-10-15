# Architecture Research

**Domain:** NFC tap-in attendance added to an existing Flask/SQLite studio-management app (AttenDANCE v2.0)
**Researched:** 2026-10-14
**Confidence:** HIGH on how it fits the existing code (read the code directly). MEDIUM on iOS Safari and Android Web NFC runtime behavior (docs + search, needs real-device confirmation, flagged inline).

Scope: only what the Tap-In milestone needs. Everything below is grounded in the files read: `app/models.py`, `app/__init__.py`, `app/migrations.py`, `app/api/routes.py` (RFID endpoints ~L1347-1400, `assign_rfid` L480, `manual_checkin` L1279, `toggle_attendance`), `rfid/service.py`, `app/static/sw.js`, `app/helpers.py`, `app/email.py`, `app/sms.py`.

## Verdict in one paragraph

Do not reuse `Student.rfid_uid`. Add a `tap_tags` table (many tags per student, token and/or UID, status lifecycle), an append-only `tap_events` ledger (replaces `RFIDLog`, gives idempotency and the in/out state), and a `tap_notifications` outbox. Put all logic in one framework-free function, `process_tap()`, in a new `app/tap/` package, called by three thin adapters (iPhone URL page, Android/wedge kiosk API, offline batch replay). `GET /t/<token>` has no side effects: it renders a page whose JS does an idempotent POST. Non-staff get a generic page with no DB lookup at all. Notifications go through a DB outbox drained by one daemon thread (same pattern the app already uses for reminders, made durable). The kiosk queues offline taps in IndexedDB; the iPhone URL flow cannot queue and must fail loudly. `rfid/` is deleted in the last phase, after its three useful ideas are lifted into the new service.

## Standard Architecture

### System Overview

```
 INPUTS (staff-authenticated, same Flask session cookie)
┌──────────────────┐ ┌──────────────────────┐ ┌─────────────────────┐
│ iPhone Safari    │ │ Android Chrome kiosk │ │ Tablet + USB wedge  │
│ GET /t/<token>   │ │ /tap (Web NFC read:  │ │ /tap (hidden focused│
│ -> page JS POST  │ │ URL record + serial) │ │ input, UID + Enter) │
└────────┬─────────┘ └──────────┬───────────┘ └──────────┬──────────┘
         │ POST /api/tap        │ POST /api/tap          │ POST /api/tap
         │                      │ (offline: IndexedDB    │ (same queue)
         │                      │  -> POST /api/tap/batch)
         ▼                      ▼                        ▼
┌─────────────────────────────────────────────────────────────────────┐
│ app/api/tap_routes.py  (registered on the EXISTING api blueprint,    │
│ so the default-deny parent write guard + _staff_only apply)          │
│  - JSON 401 (not login redirect), builds TapRequest, calls service   │
└───────────────────────────────┬─────────────────────────────────────┘
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│ app/tap/service.py  process_tap(TapRequest) -> TapResult             │
│  credentials.resolve -> dedup -> class resolver -> state machine ->  │
│  Attendance write -> TapEvent write -> enqueue TapNotification       │
│  (one DB transaction, no Flask request/current_user, no threads)     │
└──────────┬──────────────────────────────────────┬───────────────────┘
           ▼                                      ▼ after commit
┌────────────────────────────┐        ┌─────────────────────────────┐
│ SQLite (Fly volume)        │        │ app/tap/outbox.py           │
│ tap_tags, tap_events,      │        │ kick(): start ONE drain     │
│ tap_notifications,         │        │ thread; sends via           │
│ attendance (existing)      │        │ app/email.py + app/sms.py   │
└────────────────────────────┘        └─────────────────────────────┘

 NON-STAFF PATH: GET /t/<token> by anyone not staff -> generic page,
 NO token lookup, identical response for valid/invalid/revoked tokens.
```

### Component Responsibilities

| Component | Responsibility | New / Modified |
|-----------|----------------|----------------|
| `app/tap/credentials.py` | mint token, hash, normalize UID, resolve (token or UID) -> `TapTag`, assign/activate/revoke | NEW |
| `app/tap/service.py` | `process_tap()`: dedup, class resolution, in/out state machine, writes, flags | NEW (logic lifted from `rfid/service.py`) |
| `app/tap/outbox.py` | enqueue in the tap transaction, single drain thread, retry, stale suppression | NEW |
| `app/tap/__init__.py` | `tap` blueprint at root: `GET /t/<token>`, `GET /tap` (station shell), tag-management pages | NEW |
| `app/api/tap_routes.py` | `/api/tap`, `/api/tap/batch`, `/api/tap/ping`, `/api/tap/undo`, `/api/tags/*` | NEW (kept out of the 7,980-line `routes.py`) |
| `app/templates/tap/*.html` | `landing.html` (iPhone result), `generic.html` (non-staff), `station.html` (kiosk shell), tag management | NEW |
| `app/static/js/tap-queue.js`, `tap-nfc.js` | IndexedDB queue + replay, Web NFC reader/writer, wedge input handler | NEW |
| `app/models.py` | `TapTag`, `TapEvent`, `TapNotification`; `Student.has_rfid()` reads tags | MODIFIED |
| `app/migrations.py` | `rfid_uid` -> `tap_tags` backfill step | MODIFIED |
| `app/__init__.py` | register `tap` blueprint; call `outbox.drain_on_boot()` next to `_process_auto_reminders()` | MODIFIED |
| `app/static/sw.js` | bump cache version, `/t/` offline message, cache the no-PII station shell | MODIFIED |
| `app/main/routes.py` + `dashboard.html` | swap `RFIDLog` widget and `students_without_rfid` for tap equivalents | MODIFIED |
| `app/helpers.py` | `attendance_to_dict` adds `check_out_time`; new `household_flags(student)`; `_student_phone` moves here from `routes.py` | MODIFIED |
| `rfid/`, `/api/rfid/*`, `assign-rfid`, `remove-rfid`, `RFID_*` config | deleted in the final phase | REMOVED |

## Recommended Project Structure

```
app/
├── tap/
│   ├── __init__.py        # blueprint "tap" (pages: /t/<token>, /tap, /tags)
│   ├── credentials.py     # mint/hash/normalize/resolve/assign/revoke
│   ├── service.py         # process_tap(), TapRequest, TapResult, resolver
│   ├── outbox.py          # enqueue, kick, drain, drain_on_boot
│   └── flags.py           # staff-only balance/waiver flag computation
├── api/
│   └── tap_routes.py      # imported at the bottom of app/api/__init__.py
├── templates/tap/         # landing.html generic.html station.html tags.html
└── static/js/             # tap-queue.js tap-nfc.js
tests/
├── test_tap_service.py    # state machine, class resolution, dedup (bulk of value)
├── test_tap_credentials.py
├── test_tap_routes.py     # /t/ non-staff leak test, parent guard, idempotency
└── test_tap_outbox.py
```

### Structure Rationale

- **`app/tap/` package, not more of `routes.py`:** `routes.py` is already ~8k lines. The service must be importable without Flask request context so tests, the batch replay and any future caller share it.
- **API module registered on the existing `api` blueprint:** the parent write guard (`_restrict_parent_writes`, allowlist `_PARENT_WRITE_ALLOWED`) is a `before_request` on that blueprint only. A new blueprint under `/api` would silently bypass it. Importing `tap_routes` from `app/api/__init__.py` keeps default-deny. Do NOT add any tap endpoint to `_PARENT_WRITE_ALLOWED`.
- **Pages outside `/api`:** `/t/<token>` and `/tap` are not under the api blueprint, so they must do their own staff check (reuse `staff_required` from `app/main`).

## Data Model

### Decision: new table, do not reuse `Student.rfid_uid`

`rfid_uid` is one column, UID-only, unique, and has no lifecycle. The milestone needs several tags per student (fob + card), a token and a UID per physical tag, lost/revoked state that must still be recognized on scan, labels, and tags minted before a student is attached. None of that fits a column on `students`. Keep the old column physically (SQLite `DROP COLUMN` with a unique index is not worth the risk) but stop reading and writing it.

### `tap_tags`

| Column | Type | Notes |
|--------|------|-------|
| `id` | int PK | |
| `student_id` | FK students, **nullable**, indexed | NULL = pre-minted, unassigned |
| `token_hash` | String(64), unique, nullable | sha256 hex of the URL token. Plain token is never stored (the tag URL is written once at mint time and returned once) |
| `uid` | String(32), unique, nullable | canonical: uppercase hex, no separators. Learned/filled by Android enroll, kiosk read, or wedge enroll |
| `label` | String(20) | fob / card / wristband / other |
| `status` | String(12), indexed | `unassigned`, `active`, `lost`, `retired` |
| `is_locked` | bool | NDEF made read-only (Android `makeReadOnly`) |
| `created_at/by`, `assigned_at/by`, `revoked_at/by`, `revoke_reason` | | audit trail; mirrors the audit style of `rfid_assigned_*` |
| `last_seen_at` | DateTime (studio-local) | updated on each successful resolve |

Rules:
- A student may have any number of `active` tags. A tap by any of them is the same student.
- Revoked rows keep their `token_hash` and `uid`. A scan of a lost fob must say "LOST TAG, belongs to <student>" to staff, not "unknown tag". Plain unique constraints are fine because a revoked value is never reissued.
- A tag can carry both identifiers. Token is authoritative for URL/Web NFC reads; UID is what a wedge reader sends. If a kiosk read supplies token + UID and the tag has no UID, **learn it** (only if no other tag owns that UID). If both exist and disagree, process by token and log `uid_mismatch` on the event (possible cloned tag; do not block a 4-year-old at the door).
- Token: `secrets.token_urlsafe(16)` (128 bits, 22 chars). URL is `https://attenddance.fly.dev/t/<token>` = ~52 bytes, fits NTAG213 (144 bytes user memory) with room to spare.
- UIDs are not secret and are clonable. Treat UID auth as the weaker fallback, not a security boundary. Tokens are also bearer values (anyone holding the fob can read the URL), which is acceptable only because the token is useless without a staff session (see Auth).

### `tap_events` (append-only ledger; replaces `RFIDLog`)

`id`, `tag_id` (nullable), `student_id` (nullable), `attendance_id` (nullable), `class_id` (nullable), `source` (`url` / `kiosk_nfc` / `wedge` / `manual_fix`), `actor_user_id`, `event_key` (**unique**, the idempotency key), `occurred_at` (studio-local naive datetime, same basis as `Attendance.check_in_time`), `received_at`, `outcome` (see state machine), `detail` (short, no PII), `stale` bool, `anomaly` (nullable string, e.g. `uid_mismatch`).

Why a ledger and not just `Attendance`: `Attendance` has a unique index per (student, class, day) (`ix_attendance_unique_day`), so it can hold only the first in and the last out. The ledger holds every raw tap, is the source of truth for "what phase is this student in", gives idempotency via `event_key`, and gives the admin a scan history including failures (unknown, lost, no class). Log entity ids only, never names: `tests/smoke_audit.py::run_no_pii_in_logs` fails the build on PII in log lines and it scans `app/`.

### `tap_notifications` (outbox)

`id`, `tap_event_id` (unique FK), `student_id`, `kind` (`arrival`/`departure`), `status` (`pending`/`sending`/`sent`/`failed`/`skipped`), `attempts`, `claimed_at`, `sent_at`, `channels_used`, `last_error` (truncated, no PII), `created_at`.

### `Attendance` (existing): no schema change

Already has `check_out_time` (unused until now) and `check_in_method` (a free `String(20)`, currently `rfid`/`manual`/`backfill`). Write `check_in_method='tap'` for all three inputs (the per-input `source` lives on `TapEvent`). Existing consumers: `attendance/list.html` and `dashboard.html` branch on `== 'rfid'` for the icon, so update both to treat `tap` like `rfid` in the same commit. `attendance_to_dict` must start emitting `check_out_time` (this also feeds `/api/my-data`).

### Migration of existing `rfid_uid`

Add to `run_migrations()` after `db.create_all()` (which already runs first and creates the new tables): `_migrate_rfid_uid_to_tap_tags`. For each student with `rfid_uid IS NOT NULL`, insert a `tap_tags` row (`kind` = UID only, `token_hash` NULL, `label='card'`, `status='active'`, `assigned_at/by` copied, UID normalized by the same `normalize_uid()` the runtime uses). Skip if a tag with that UID already exists (idempotent, same style as `_seed_default_season`). Do it in a short Python loop, not SQL, because normalization must be shared code; row count is tiny (RFID has been off in prod). Log collisions by student id. Never drop `students.rfid_uid`; leave the model attribute with a `# deprecated, read by migration only` comment. `Student.has_rfid()` becomes "has an active tag" (it is read by `student_to_dict` ~L950 and `students/list.html`), and `students_without_rfid` on the dashboard becomes a NOT EXISTS subquery.

## Architectural Patterns

### Pattern 1: One service, three thin adapters

```python
# app/tap/service.py - no flask.request, no current_user, no threads
@dataclass
class TapRequest:
    kind: str                 # 'token' | 'uid'
    value: str
    uid: str | None           # kiosk sends both
    source: str               # 'url' | 'kiosk_nfc' | 'wedge'
    actor_id: int
    event_key: str
    occurred_at: datetime     # studio-local naive; injectable for tests/replay
    mode: str = 'auto'        # 'auto' | 'in' | 'out' (lobby station switch)
    class_id: int | None = None   # staff override for the no_class picker

def process_tap(req: TapRequest) -> TapResult: ...
```

Adapters: the `/t/` page JS and the kiosk both POST `/api/tap`; the offline queue posts `/api/tap/batch` (sorted by `occurred_at`, each item processed by the same function). `occurred_at` is a parameter, not `datetime.now()`, so replayed and tested taps resolve classes and dedup by the time the tap happened. Lift these three things from `rfid/service.py` and nothing else: the near-simultaneous-scan `IntegrityError` recovery, the +/- window class match, and the studio-local-time rule (container is `TZ=America/New_York`, so `datetime.now()` is already studio-local; keep that basis for `Attendance` and `TapEvent`).

Do NOT refactor `rfid/service.py` in place. It is a hardware polling loop with instance state, a global singleton, and its own `create_app()` call. Nothing in it is reusable as a class.

### Pattern 2: Idempotency by event key, not by time alone

`tap_events.event_key` is unique. A retry, a double POST, or a reloaded landing page re-sends the same key and gets the stored outcome back with no second toggle. On top of that, a time-window dedup (default 10 s, a Setting) catches a new key for the same student from a re-presented tag (kid holds the fob against the phone twice). Window dedup returns outcome `duplicate` with the prior result, and is evaluated against `occurred_at` so it also works on replay.

### Pattern 3: Outbox + single drain thread

Enqueue the `TapNotification` row inside the tap transaction. After commit, `outbox.kick(app)` starts one daemon thread if none is alive; it claims `pending` rows one at a time (status -> `sending`, `claimed_at`), sends, marks `sent`/`failed`, loops until empty, exits. See Notification Dispatch.

## The `/t/<token>` route

### Semantics: GET is side-effect free; the page POSTs

```
GET /t/<token>
  not authenticated        -> generic.html (200), NO DB lookup on the token
  authenticated, parent    -> generic.html (200), NO DB lookup
  authenticated, staff     -> landing.html, no-store, embeds a fresh event_key
                              JS: POST /api/tap {token, event_key, source:'url'}
                              then history.replaceState(null,'','/tap/done')
```

Why not "GET records the tap": link prefetchers, link-preview fetchers, restored tabs, and double-opens (the iOS notification banner tapped twice) all issue GETs. A GET that mutates state will eventually check a kid out by accident. Why not a confirm button: staff want zero extra taps, and the page is on a staff phone. Auto-POST from JS with a unique key is confirm-free and safe against everything that does not execute JS.

Guards on the landing page JS (all cheap):
- `Cache-Control: no-store` on the GET so Safari does not serve a stale copy.
- Auto-POST only when `performance.getEntriesByType('navigation')[0].type === 'navigate'` and `document.visibilityState === 'visible'`. A tab restore/reload (`reload`, `back_forward`) does not fire a new tap; it shows "Open the tag again to tap".
- The embedded `event_key` means that even if the same HTML re-runs, the server returns the original result instead of toggling.
- `history.replaceState` strips the token from the address bar and Safari history.
- A Setting `tap_require_confirm_url` (default off) renders a big "Confirm tap" button instead of auto-POST, as an escape hatch if the studio sees accidental double-opens in practice.

Residual risk: a Safari tab restore that issues a brand-new GET (not a reload entry) would be a new key. The no-store + navigation-type check covers the known paths; verify on a real iPhone (MEDIUM confidence, see Pitfalls).

### Auth for non-staff scanners (a parent phone scanning a kid's fob must reveal nothing)

- **Zero lookup for non-staff.** The route decides staff-or-not first from the session; for anyone else it renders `generic.html` without touching `tap_tags`. Valid, unknown, revoked and unassigned tokens produce the byte-identical response, so there is no oracle and no timing difference.
- `generic.html` shows only the studio name, public contact (existing `STUDIO_URL`/studio phone Setting) and "If you found this tag, please return it to the studio", plus a "Staff sign in" link to `/auth/login?next=/t/<token>`. `_is_safe_next` in `app/auth/routes.py` already accepts a same-site relative path, so staff who open a tag with an expired session sign in and land back on the tap. This also means a logged-in PARENT (`is_parent`) gets the generic page, never even their own child's name.
- Add `<meta name="robots" content="noindex">`. The app's existing `Referrer-Policy: strict-origin-when-cross-origin` already keeps the path (token) out of cross-origin Referer headers to the Tailwind/Alpine CDNs. The token will appear in Fly access logs; acceptable because a token without a staff session grants nothing.
- Tag page for staff uses `Cache-Control: no-store`; the SW already never caches navigations.
- iOS caveat that shapes the whole iPhone flow: the tag URL opens in **Safari**, and a Home Screen web app has separate cookie storage from Safari. Staff must be signed in **in Safari** on that phone. Do not tell staff to use the installed PWA for tapping. The login-redirect-with-`next` path above is what makes this livable (MEDIUM, from WebKit bug 181849 and Apple docs; confirm on device).

## Check-in / check-out state machine

State is derived from **today's successful `tap_events` for the student**, not from the `Attendance` row. This matters because teachers also mark present by hand on the attendance card; if the row alone decided direction, a teacher pre-marking P before kids tap would make the kid's arrival tap register as a departure.

```
phase = last successful tap_event today for this student:
  none            -> next tap = CHECK IN
  checked_in      -> next tap = CHECK OUT
  checked_out     -> next tap = ALREADY_OUT (no state change; staff sees
                     "left at 5:42 PM" and an explicit [Re-open] button)
mode 'in' / 'out' (lobby station switch) overrides 'auto' and makes a
wrong-direction tap an error instead of a toggle.
```

| Situation | Attendance effect | Outcome |
|-----------|-------------------|---------|
| No row for (student, class, today) | create row `is_present=True`, `check_in_time=occurred_at`, `check_in_method='tap'` | `checked_in` |
| Row exists, `is_present=False` (marked A or E earlier) | flip to present, clear `is_tardy`/`is_excused` (mirrors `manual_checkin` L1302-1313) | `checked_in` |
| Row exists present from a manual P, no prior tap today | adopt it: keep `check_in_time`, set method note, no new row | `checked_in` |
| Phase `checked_in` | set `check_out_time=occurred_at` on the row of the class chosen at check-in | `checked_out` |
| Phase `checked_out` | none | `already_out` |
| Same student within dedup window | none | `duplicate` |
| No class resolves | none; ledger row only | `no_class` (staff picker) |
| Tag status `lost`/`retired` | none | `lost_tag` (red, staff-only detail) |
| Tag `unassigned` | none | `unassigned_tag` (staff assign UI, then re-tap) |
| Token/UID unknown | none | `unknown_tag` |
| Student `is_active=False` | none | `inactive_student` |

Rules and choices:
- **Tardy:** the tap does not set `is_tardy`. Auto-flagging minors late is a studio policy call, and teachers own the T on the card. Optional Setting `tap_tardy_grace_minutes` (default off) for later.
- **Undo:** `POST /api/tap/undo {event_id}` reverses the last action (delete the row it created, restore the row it flipped, or clear `check_out_time`) and writes a `manual_fix` ledger entry. A wrong tap at the door happens daily; without undo staff will fix it via the attendance card, which deletes the row and loses `check_out_time`.
- **Re-open:** third tap of the day is not auto-treated as a new visit (one Attendance row per class per day cannot represent two visits). Staff taps "Re-open" which clears `check_out_time` and writes a ledger entry.
- **Back-to-back classes** (student in the 5:00 and 6:00 class): default toggle behavior would close class A and never open class B. Ask the studio whether this happens. If yes, add one rule: when the phase is `checked_in` for class A, A has ended, and the student has an enrolled class B in its window with no row, close A and open B. Not built unless the studio confirms (open question).
- Concurrency: the unique (student, class, day) index and unique `event_key` are the real guards, as in `toggle_attendance`. On `IntegrityError` rollback, re-run `process_tap` once; it will land in `duplicate`/idempotent replay.

### Class resolution (check-in)

Candidates = the student's active `ClassEnrollment`s, restricted through `live_class_query()` (active-season gate in `app/helpers.py`; do NOT use bare `is_active`, the helper's docstring says every operational view must use it), with `day_of_week == occurred_at.weekday()` and `start - early <= time <= end + late`. Defaults 30 min early, 15 min late, taken from the legacy constants but moved to Settings (`tap_early_minutes`, `tap_late_minutes`).

- Several candidates (back-to-back): pick the class whose start is nearest `occurred_at`; tie -> earlier start.
- Include today's `MakeupClass` rows (`status` scheduled, `makeup_date == today`, `makeup_class_id`) as candidates even without enrollment.
- **Drop the legacy fallback** ("no window match -> first enrolled class today"). It would stamp a 9 AM tap onto a 5 PM class. No match returns `no_class` with the student's classes for today as a one-tap picker (the picked `class_id` re-submits with the same `event_key`), plus "other class today" for drop-ins.
- Check-out uses the class recorded on the student's check-in event, not the time window (departure can be well after `end + 15`).
- Optional station mode (differentiator, later): kiosk session pins a class; students not enrolled in it return `not_enrolled` with a staff confirm.

### Staff-only flags

`flags.household_flags(student)` returns `{balance_due: bool, waiver_missing: bool}` plus detail fetched only when staff taps "details". Balance via `calc_balance_bulk` over the household's active students (the same net-per-fund semantics the reminders use; per fund, studio and company stay separate). Waiver = any active `WaiverTemplate` without a `WaiverSignature` for the student (one query). Flags are only ever in `/api/tap` responses (staff-only endpoint). Show a neutral badge ("See front desk"), not dollar amounts, on the result card, since the screen is visible to kids and parents standing at the door.

## Notification Dispatch (256 MB, one gunicorn gthread worker)

Options weighed:

| Option | Verdict |
|--------|---------|
| Synchronous send in the tap request | No. `sms.send_sms` timeout is 15 s and SMTP is slower; the kiosk would stall on every kid, and 4 threads exhaust fast during a drop-off burst |
| One `threading.Thread` per tap (existing `_send_email_async` pattern) | Works at low volume, but 20 kids in 3 minutes = 20 concurrent TLS SMTP sessions on 256 MB, and a thread killed by Fly auto-stop loses the message silently |
| Real queue (Celery/RQ/Redis) | No. Extra process/memory, out of constraint |
| **Outbox table + single drain thread** | Yes |

Design:
1. `process_tap` inserts `TapNotification(status='pending')` in the same transaction as the attendance write. Atomic: no tap without its notice and no notice for a rolled-back tap.
2. After commit, `outbox.kick(app)`: module-level lock + "thread alive" flag; start one daemon thread if idle. It loops: claim next `pending` row (`UPDATE ... SET status='sending', claimed_at=now WHERE id=... AND status='pending'`), build message, send, mark result, until none left. Sends are sequential, so memory is flat regardless of burst size.
3. Recovery: `drain_on_boot()` called in `create_app()` beside `_process_auto_reminders()` re-kicks pending rows and resets `sending` rows whose `claimed_at` is older than 5 min. Fly stops/starts the machine all day (`auto_stop_machines='stop'`), so durability matters here more than for monthly reminders. `kick()` also runs opportunistically on every tap request.
4. Retries: max 3 attempts, then `failed`. Errors stored truncated and PII-free.
5. **Stale suppression:** if `received_at - occurred_at` > 20 min (offline replay), mark `skipped`. A "Maya arrived" text delivered at 9 PM is noise and alarming.
6. Content: first name, class name, time. "Maya arrived at Hip Hop 5:02 PM." / "Maya left Hip Hop at 5:50 PM." No who-picked-up, no balance, no location detail. Not sent for `duplicate`, `already_out`, `no_class`, `unknown_tag`.
7. Recipients: `helpers.student_emails(student)` (both parents plus household); phone from `_student_phone` (move it from `routes.py` into `helpers.py`; the drain thread must not import private helpers out of the 8k-line routes module).
8. Channels and consent: global Settings `tap_notify_email` (default on) and `tap_notify_sms` (default off), per-household opt-out. SMS-per-tap is a different cost and consent profile from monthly balance reminders; confirm with the studio before enabling SMS. Mention the new notice in `privacy.html` (it currently says Twilio sends "SMS balance reminders").
9. Gate on existing `email_service.is_configured()` and `sms_service.is_configured()`; unconfigured channels are silently skipped like the reminder code does.

## Offline Handling

### Android kiosk (queue)

- The station page (`/tap`) is a **static, no-PII shell**: it renders no user data and authenticates via the session cookie on its API calls. That lets `sw.js` cache it and serve it offline, a deliberate exception to the "never cache authenticated HTML" rule that is safe only because the shell is identical for everyone. The rest of `sw.js` is unchanged: `/api/` calls bypass the SW (`return` without `respondWith`), which is what we want for POSTs.
- Page-driven queue, not Background Sync. Web NFC only scans while the page is foreground, so a service-worker sync buys nothing and is Chrome-only.
- On each read: build `{event_key (uuid), token, uid, source, occurred_at: epoch ms}`, write to IndexedDB first, show "Saved" immediately, then try to send. Remove from the queue only on a definitive server answer (any 2xx with JSON, including `duplicate`, `unknown_tag`; a 4xx validation error is dropped with a visible count). Network error or 5xx -> keep and retry with backoff, and flush on the `online` event. Flush through `/api/tap/batch` sorted by `occurred_at` so toggles replay in the right order.
- Auth expiry while offline: the 8 h session can lapse. Because API endpoints return the login page (HTML redirect) by default, `fetch` would see a 200 HTML and wrongly "succeed". Tap endpoints use their own decorator that returns **JSON 401**; the client also defends (`res.redirected` or non-JSON content type -> treat as auth-expired). On 401 the queue pauses, keeps everything, and shows "Sign in again" (login in another tab; same-origin cookie; the queue is in IndexedDB so nothing is lost). A 5-minute `GET /api/tap/ping` keeps the session alive during class (Flask refreshes a permanent session each request; verify `session.permanent` is set at login).
- Server side of replay: `occurred_at` arrives as epoch ms, is validated (not > 2 min in the future, not older than 24 h), converted with `zoneinfo` to studio-local naive to match `Attendance`, and sets `stale` if late. Class resolution, dedup and phase all use it.
- No student roster is cached on the device (minors' PII on a phone, COPPA constraint). Offline the screen can only say "Saved, will sync (N waiting)"; names and flags appear only for online taps.
- Call `navigator.storage.persist()` and a screen Wake Lock on the station page so Chrome does not evict the queue or sleep the display (MEDIUM; standard web APIs, verify on the staff Android devices).

### iPhone URL flow (cannot queue)

There is no page, SW or JS in play when iOS opens the tag URL offline: Safari simply fails to load (or the SW serves its offline notice if it has previously been registered). Nothing can be recorded. Design for honesty, not a workaround:
- Update `sw.js` so a navigation to `/t/*` that fails returns an explicit "NOT RECORDED. You're offline. Mark attendance on the card or tap again when back online." page (the current generic "You're offline" text does not tell staff the tap was lost).
- Document it to the studio as a limitation; staff fall back to the Android kiosk or the manual attendance card.
- Fly cold start: `min_machines_running = 0` with auto-stop means the first tap after idle waits for a machine boot (several seconds on a page the user expects to be instant). Evaluate `min_machines_running = 1` during class hours or overall (small recurring cost on `shared-cpu-1x`/256 MB; check current Fly pricing). Flag for the roadmap rather than decide here.

## Data Flow

### Request flow (iPhone)

```
tag tap -> iOS banner -> user taps banner -> Safari GET /t/<token>
 -> [staff session?] no -> generic page / sign-in link
                    yes -> landing.html (+event_key)
 -> JS POST /api/tap -> api guard (staff) -> process_tap()
    -> resolve tag -> dedup -> class -> phase -> write Attendance + TapEvent
       + TapNotification (1 txn) -> commit -> outbox.kick()
 -> JSON TapResult -> landing renders card (green in / blue out / amber / red)
 -> replaceState; auto-dismiss
```

### Request flow (kiosk / wedge)

```
Web NFC 'reading' (serialNumber + url record)  OR  wedge input + Enter
 -> tap-queue.js: assign event_key, store in IndexedDB, POST /api/tap
 -> same process_tap() -> result card + beep/vibrate
 -> failure -> stays queued -> /api/tap/batch on reconnect
```

### Key data flows

1. **Enrollment (Android, writes tag):** `POST /api/tags/mint {count,label}` returns tokens+URLs once -> page calls `ndef.write({records:[{recordType:'url', data:url}]})` and reads `serialNumber` in the same session -> `POST /api/tags/<id>/activate {uid}` -> optionally `makeReadOnly()` -> `POST /api/tags/<id>/assign {student_id}`.
2. **Enrollment (iPhone-only staff, cannot write NDEF):** tags are pre-encoded in a batch (Android phone, desktop NFC tool, or a vendor encoding service) from minted URLs. First staff tap of an `unassigned` tag lands on the assign-student picker, then proceeds as a normal tap. No UID is captured on iPhone; the kiosk or wedge learns it on first use.
3. **Lost tag replacement:** staff opens the student, marks the old tag `lost` (kept, for the red scan warning), mints/assigns a new one. Old tag taps return `lost_tag` to staff only.
4. **Return to attendance card:** the card reads `Attendance` as before; `check_out_time` can be surfaced later as a small "left 5:42" annotation (optional UI).

## Anti-Patterns

### Anti-Pattern 1: GET with side effects
**Why bad:** prefetch, preview, tab restore and double-open all issue GETs. **Instead:** render-only GET, idempotent POST with a unique key.

### Anti-Pattern 2: Deciding direction from the `Attendance` row
**Why bad:** manual P marks and the attendance card's delete-on-clear break the toggle. **Instead:** derive phase from the tap ledger.

### Anti-Pattern 3: "Nearest class today" fallback
**Why bad:** wrong class, wrong billing/analytics. **Instead:** `no_class` + staff picker.

### Anti-Pattern 4: Extending `rfid/service.py`
**Why bad:** hardware loop, global singleton, `create_app()` per service, cannot run on Fly. **Instead:** new service, delete the package last.

### Anti-Pattern 5: Name or student data anywhere a non-staff request can reach
**Why bad:** violates the COPPA constraint. **Instead:** non-staff branch never queries; add a test that fetches `/t/<valid>`, `/t/<bogus>`, `/t/<revoked>` as anonymous and as a parent and asserts identical bodies.

### Anti-Pattern 6: New `/api` blueprint
**Why bad:** bypasses the parent write guard. **Instead:** register on the existing `api` blueprint.

### Anti-Pattern 7: Caching a student roster on the kiosk for offline names
**Why bad:** minors' PII on a personal phone. **Instead:** "Saved, will sync."

## Scaling Considerations

The real load shape is a burst (15-30 taps in ~5 minutes at class start and end), not user count.

| Concern | One class burst | Whole studio (~hundreds of students) | Multi-studio |
|---------|-----------------|--------------------------------------|--------------|
| SQLite writes | 3 inserts per tap, trivial; WAL + 10 s busy timeout already set | same | would need Postgres; out of scope |
| Request threads (4 gthread) | each tap is ~10 ms of DB work; no network I/O in request | fine | |
| Notifications | sequential drain, flat memory; 20 SMS at ~1-2 s each finishes in under a minute | outbox backlog is bounded by the sequential drain | |
| `tap_events` growth | ~tens of rows/day; attachments none | prune/archive after a season if desired | |
| Backup size | negligible vs the attachment blobs the memory notes warn about | | |

## Integration Points

### External services

| Service | Pattern | Notes |
|---------|---------|-------|
| SMTP (`app/email.py`) | outbox drain thread calls `send_email` | needs app context in the thread (use `with app.app_context()` like `_send_balance_reminders`) |
| Twilio (`app/sms.py`) | outbox drain thread calls `send_sms` | never raises; returns bool; 15 s timeout, which is why it is off the request path |
| Browser Web NFC | `NDEFReader.scan()/write()`, needs HTTPS (already forced by Fly) and a user gesture/permission prompt to start | Android Chrome only |
| iOS background tag reading | iPhone XS+ opens the first NDEF URL record via a notification the user must tap | a plain https URL opens Safari, not the Home Screen app |

### Internal boundaries

| Boundary | Communication | Notes |
|----------|---------------|-------|
| `tap_routes` -> `service` | function call with `TapRequest` | `actor_id` passed in; service never reads `current_user` |
| `service` -> `outbox` | row insert in-transaction, then `kick()` post-commit | |
| `service` -> `helpers` | `live_class_query`, `calc_balance_bulk`, `student_emails` | existing code, reused |
| `/api/my-data` | add `check_out_time` (via `attendance_to_dict`) and the student's tap history | PII rule: update model + `privacy.html` + export in the same commit |
| `privacy.html` | L41 "RFID badge ID" -> NFC tag token/UID; Attendance line gains check-out time; Twilio line gains arrival/departure notices | |
| `tests/smoke_audit.py` | scans `("app","rfid")` dirs and lists `/api/rfid/logs` | edit when `rfid/` and the endpoint are removed |

## Legacy `rfid/` package: what to do

Delete it, in the last phase, after cutover. Specifically:
- Remove `rfid/` (`reader.py`, `service.py`, `__init__.py`), the `get_rfid_service` import block (~L90-94 in `routes.py`), `/api/rfid/status`, `/api/rfid/simulate`, `/api/rfid/logs`, `assign-rfid`, `remove-rfid` (no template calls the assign/remove endpoints; only `has_rfid` and `rfid_uid[:8]` in the dashboard log widget are used).
- Remove `RFID_*` config in `config/config.py`, the `mfrc522`/`RPi.GPIO`/`spidev` lines in `requirements.txt` (prod already uses `requirements-deploy.txt` without them) and `requirements-legacy.txt` if it only serves the Pi, `RFID_ENABLED` references in `tests/smoke_audit.py`, and rewrite the README (it still describes a Raspberry Pi system).
- Keep `RFIDLog` and `students.rfid_uid` tables/columns as read-only history; do not drop them. The dashboard widget switches to `TapEvent` earlier (the iPhone-flow phase).
- Do the swap behind the new code, not before: removing `/api/rfid/*` first would break the smoke audit and the dashboard.

## Suggested Build Order (with dependencies)

| # | Phase | Delivers | Depends on |
|---|-------|----------|-----------|
| 1 | **Data + service core** | `TapTag`/`TapEvent`/`TapNotification` models, `rfid_uid` backfill migration, `credentials.py`, `process_tap()` with resolver, dedup, state machine, undo; `POST /api/tap` (staff, JSON 401); `attendance_to_dict` + `check_out_time`; `privacy.html`/`my-data` for check-out time. Heavy unit tests (`test_tap_service.py`) | nothing |
| 2 | **Tag management** | mint/assign/activate/revoke API + minimal UI on student detail; replace `has_rfid` consumers; Android Web NFC write + `makeReadOnly` | 1 |
| 3 | **iPhone URL flow** | `GET /t/<token>`, `landing.html`, `generic.html`, login `next` round trip, nav-type guards, `sw.js` `/t/` offline message, dashboard widget -> `TapEvent`, assign-on-first-tap | 1, 2 (needs minted tags) |
| 4 | **Result UX + staff flags** | `flags.py`, neutral badges, no_class picker, re-open/undo UI | 1, 3 |
| 5 | **Notifications** | outbox, drain thread, boot recovery, Settings, stale suppression, `privacy.html` Twilio line | 1 (event ids). Can run parallel to 3-4 |
| 6 | **Android kiosk + offline** | `/tap` station shell, Web NFC continuous read, IndexedDB queue, `/api/tap/batch`, `/api/tap/ping`, SW shell caching, Wake Lock | 1, 3 (result card components) |
| 7 | **USB wedge station** | hidden-input handler on the same `/tap` page, `normalize_uid()` contract, UID-learning | 6 |
| 8 | **Legacy removal** | delete `rfid/`, endpoints, config, deps, README, smoke-audit edits | 3 and 6 live in prod |

Ordering rationale: the service has all the risky logic and zero UI, so build and test it first. The iPhone flow ships next because it needs no new hardware and is the primary path. Notifications are independent of input adapters once events exist. The kiosk/offline work is the largest browser-side piece and is safe to defer because the iPhone path already delivers the milestone's core value. Wedge reuses the kiosk page. Deletion is last so nothing breaks mid-milestone.

### Research flags per phase

| Phase | Flag |
|-------|------|
| 1 | Standard patterns; mirror `tests/test_attendance_card.py` style. Needs a studio answer on back-to-back classes and tardy policy |
| 2 | Needs a real Android phone: NDEF write + `makeReadOnly` behavior on NTAG213 vs 215 |
| 3 | Needs a real iPhone XS+ (and a locked phone, banner tap, Safari vs Home Screen session, tab restore behavior) before sign-off |
| 5 | Decide SMS consent/cost with the studio; Twilio per-segment cost at tap volume |
| 6 | Needs deeper research: Web NFC foreground/wake-lock behavior, whether an active `scan()` suppresses Android's system URL dispatch to a second Chrome tab, IndexedDB persistence on the staff devices |
| 7 | Needs the actual reader: wedge readers commonly emit decimal or byte-reversed UIDs; the contract and `normalize_uid()` must match what Web NFC `serialNumber` produces (colon-separated hex) |

## Sources

- Chrome for Developers, Interact with NFC devices on Chrome for Android (user gesture + permission prompt, HTTPS, top-level frames only, `serialNumber` and `message` on read events): https://developer.chrome.com/docs/capabilities/nfc (HIGH for permission/secure-context; screen-on behavior NOT confirmed in sources, treat as MEDIUM and test)
- Apple, Adding support for background tag reading (iPhone XS and later; only the first URL record processed; user taps a notification): https://developer.apple.com/documentation/corenfc/adding-support-for-background-tag-reading (HIGH)
- Apple Tech Talk 702 on background tag reading: https://developer.apple.com/videos/play/tech-talks/702 (HIGH)
- WebKit Bug 181849, Home Screen web apps do not share storage with Safari: https://bugs.webkit.org/show_bug.cgi?id=181849 (MEDIUM, long-standing, "by design" per comments; confirm on current iOS)
- Codebase (primary): `app/models.py` Attendance/Student/RFIDLog, `app/migrations.py`, `app/__init__.py` (single worker, reminder thread pattern, CSRF origin guard, security headers), `app/api/routes.py` (`_restrict_parent_writes`, `manual_checkin`, `toggle_attendance`, `_send_email_async`), `rfid/service.py`, `app/static/sw.js`, `Dockerfile` (`TZ=America/New_York`), `fly.toml` (auto-stop, 256 MB), `tests/smoke_audit.py` (no-PII-in-logs guard). HIGH.
- Not independently verified (training knowledge, MEDIUM/LOW): Wake Lock API behavior on the specific staff Android devices, Fly pricing for always-on, Safari tab-restore re-fetch behavior.
