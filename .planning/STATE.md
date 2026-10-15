---
gsd_state_version: 1.0
milestone: v2.0
milestone_name: Tap-In
status: built_awaiting_hardware
last_updated: "2026-10-15T06:30:00.000Z"
last_activity: 2026-10-15
progress:
  total_phases: 7
  completed_phases: 0  # code for all 7 phases is in; phases close after hardware checks
  total_plans: 0
  completed_plans: 0
  percent: 0
---

# Project State

## Project Reference

See: .planning/PROJECT.md (updated 2026-10-14)

**Core value:** Staff can record who is at class, and bill families correctly, without fighting the software.
**Current focus:** Hardware checks and the pilot (all Tap-In code is built on feature/v2-tap-in)

## Current Position

Phase: code for phases 1-7 built; hardware gates open
Status: Built, awaiting hardware checks, the hostname decision and the pilot
Last activity: 2026-10-15 - Built Tap-In end to end on branch feature/v2-tap-in (not pushed, not deployed)

What's left is not code:
- HW-02: order sample NTAG213 fobs and cards, run NXP TagInfo, read them on one iPhone and one Android phone.
- HW-03: decide the permanent tag address and save it in Tap-In settings (locking is refused until then).
- USB-02: bench-test the ACR1281U-C2 settings in docs/tap-in.md, if the reader is bought.
- ROLL-02: 2-3 week pilot on unlocked tags, then bulk order, lock, hand out.
- Before turning texts on: confirm the Twilio number's 10DLC registration.

Progress: [█████████░] code complete; 4 of 48 requirements wait on hardware, a decision or the pilot

## Performance Metrics

**Velocity:**
- Total plans completed: 0
- Average duration: - min
- Total execution time: 0.0 hours

**By Phase:**

| Phase | Plans | Total | Avg/Plan |
|-------|-------|-------|----------|
| - | - | - | - |

**Recent Trend:**
- Last 5 plans: -
- Trend: -

*Updated after each plan completion*

## Accumulated Context

### Decisions

Decisions are logged in PROJECT.md Key Decisions table.
Recent decisions affecting current work:

- [Roadmap]: HW-03 (permanent hostname) sits in Phase 1 and must be recorded before any production tag is written for keeps or TAG-09 (lock) is run.
- [Roadmap]: TAP-09 (undo) lives in Phase 3 with the result screen; the service-level hook is built in Phase 1.
- [Roadmap]: PHOTO-01 lives in Phase 3 (staff-only photo on the result screen).
- [Roadmap]: RPT-01 rides with Phase 6; it needs only tap events, so it can move earlier if wanted.
- [Research]: Table names `nfc_tags`, `tap_events`, `tap_notifications`; package `app/tap/`; token `secrets.token_urlsafe(16)`, store sha256 only.
- [Research]: Tardy auto-mark off by default; 10 s DB dedup; 5 min dwell before check-out; direction from tap_events, not Attendance.

### Pending Todos

None yet.

### Blockers/Concerns

- Real-device spikes needed before committing UX promises: iPhone (Phase 1 spike feeding Phase 3), Android write + lock (Phase 2), Android kiosk Wake Lock / PWA (Phase 5), USB reader format and iPad HID (Phase 6).
- Twilio 10DLC status unknown; existing SMS reminders may be failing silently. Check in Phase 1 non-code track, needed before any SMS in Phase 4.
- Hostname decision (HW-03) is Thomas's call and blocks tag locking.
- At least one Android NFC phone or tablet is required for enrollment. Confirm with the studio (open question 2).
- Open studio questions (13) are listed in .planning/research/SUMMARY.md. Owner decisions go to Wilkanda, setup tasks to Carollette.

## Deferred Items

Items acknowledged and carried forward from previous milestone close:

| Category | Item | Status | Deferred At |
|----------|------|--------|-------------|
| *(none)* | | | |

## Session Continuity

Last session: 2026-10-15
Stopped at: Tap-In built and tested (test_tap, test_tap_flows, test_tap_notices, smoke_audit all green); branch not pushed
Resume file: None
