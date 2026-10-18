# AttenDANCE

## What This Is

AttenDANCE is the studio management system for LaShelle's School of Dance (Oak Park, Michigan). It covers attendance, billing, the parent portal, rules and waivers, messaging, registration, the Performance Company and the recital. It replaces the studio's Jackrabbit setup. It is a Flask app deployed at https://attenddance.fly.dev. Staff (admin and teachers) run the studio from it, and parents use the portal.

## Core Value

Staff can record who is at class, and bill families correctly, without fighting the software.

## Current Milestone: v2.0 Tap-In

**Goal:** Dancers tap an NFC fob or card on a staff member's phone when they arrive and when they leave, and AttenDANCE records it with no typing.

**Target features:**
- NTAG213/215 fobs and cards. Each one holds an NDEF URL with a random, opaque token. The tag carries no student data.
- Staff write and enroll tags from inside the app, and replace lost tags.
- Check-in on a staff phone:
  - iPhone opens the tag URL natively; the staff session records the tap.
  - Android runs a continuous Web NFC kiosk page.
- Fallback station: a tablet or laptop with a USB keyboard-wedge reader, using the tag UID.
- A tap checks the dancer in to today's class. A second tap checks them out.
- The kiosk shows staff-only flags for an outstanding balance or a missing waiver.
- Parents get a text or email on arrival and departure.
- A researched shopping list for tags and the optional USB reader.

## Requirements

### Validated

- ✓ Billing: charges, payments, per-category and per-fund balances, recurring billing, Square invoices, payment plans, late fees
- ✓ Parent portal: invite codes, read-only child view, payment claims, data export
- ✓ Rules and digital waivers with per-student acknowledgment
- ✓ Messaging: email blasts with attachments, Twilio SMS, auto-reminders
- ✓ Attendance card: present, absent, tardy (T), excused (E); manual check-in
- ✓ Public registration, waitlists, Performance Company, Recital Hub, analytics
- ✓ Launched to the studio 2026-07-06; SMTP live 2026-08-08

### Active

- [ ] v2.0 Tap-In (see REQUIREMENTS.md)

### Out of Scope

- Native iOS/Android app: staff want to use the existing web app on their own phones.
- Server-attached reader (the current in-process MFRC522 polling): prod runs on Fly.io, so no reader can attach to the server.
- 125 kHz (EM4100) and MIFARE Classic tags: iPhones cannot read them.

## Context

- The app is Flask + SQLAlchemy + SQLite on a 256 MB Fly machine with 1 gunicorn gthread worker. Settings live in the DB `Setting` table.
- Legacy RFID code exists in `rfid/reader.py` and `rfid/service.py`. It runs an MFRC522 on a Raspberry Pi over SPI, inside the Flask process. It is disabled in prod (`RFID_ENABLED=false`).
- RFID code that can be reused:
  - `Student.rfid_uid` (unique), plus the `assign-rfid` and `remove-rfid` endpoints
  - `RFIDLog`
  - the duplicate-scan window
  - the "find the current class" logic
  - `Attendance.check_out_time`, which exists but is unused
- Classes run at community centers (Oak Park and Lathrup Village) during the move to an owned building, so Wi-Fi is not guaranteed.
- Students range from age 3 to 83. Staff program and hand out the tags.
- Web NFC works only in Chrome on Android. iPhone XS and newer read NDEF URL tags natively in the background and open the URL in Safari.

## Constraints

- **Hardware**: staff use their own iPhones or Android phones. A tablet with a USB reader is the fallback.
- **Privacy (COPPA)**: no PII on the tag. A tag scanned by a non-staff phone must reveal nothing.
- **Hosting**: the 256 MB Fly machine, so no extra processes.
- **PII rule**: any new PII field must update the model, `privacy.html` and the `/api/my-data` export in the same commit.

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| NTAG213/215 tags with NDEF URL tokens | Readable by iPhone (background reading), Android Web NFC and USB readers | — Pending |
| Staff phone as the primary station | No hardware purchase for staff; uses the existing app | — Pending |
| Opaque token in the URL, not a student ID | A lost or scanned tag leaks nothing | — Pending |

## Evolution

This document evolves at phase transitions and milestone boundaries.

**After each phase transition** (via `/gsd-transition`):
1. Requirements invalidated? → Move to Out of Scope with reason
2. Requirements validated? → Move to Validated with phase reference
3. New requirements emerged? → Add to Active
4. Decisions to log? → Add to Key Decisions
5. "What This Is" still accurate? → Update if drifted

**After each milestone** (via `/gsd-complete-milestone`):
1. Full review of all sections
2. Core Value check — still the right priority?
3. Audit Out of Scope — reasons still valid?
4. Update Context with current state

---
*Last updated: 2026-10-14 after starting milestone v2.0 Tap-In*
