# AttenDANCE

Studio management for LaShelle's School of Dance in Oak Park, Michigan: attendance, billing, the parent portal, rules and waivers, messaging, registration, the Performance Company and the recital. It replaces the studio's Jackrabbit setup.

It's a Flask app running on Fly.io at https://attenddance.fly.dev.

## Features

- **Tap-In check-in**: dancers tap an NFC key fob or card on a staff phone to check in and out. Parents can get an email or text when it happens. See [docs/tap-in.md](docs/tap-in.md).
- **Attendance card**: present, absent, tardy (T) and excused (E) per class per week, plus manual check-in.
- **Billing**: charges, payments, recurring tuition, Square invoices, payment plans, late fees, aging, statements.
- **Parent portal**: each family sees their dancers' schedule, attendance, balance and forms, and can report payments.
- **Rules and waivers**: per-rule initials and digital forms per dancer.
- **Messages**: email blasts with attachments, Twilio SMS, automatic balance reminders.
- **Registration and waitlists**, **Performance Company**, **Recital Hub**, **analytics**.

### Billing funds

Every charge and payment belongs to one of two funds: **Studio** (tuition,
costumes, dancewear, registration, other) or **Company / Foundation** (competition,
convention, transportation, lodging, observer, costume rental, company dues,
company tickets). Company money is owed to the LSODance Foundation, so the two
are never netted: a Company payment only ever settles Company charges and every
balance surface (billing table, ledgers, parent portal, reminders, late fees,
statements, CSV exports) shows the two funds separately. The category-to-fund
map lives in `app/funds.py` and is the only place it is defined.

Foundation payment destinations (Zelle, Cash App, Square link) are set in the
"Company / Foundation payments" block on `/settings`; parents see them when they
pay a Company balance. If a parent pays the wrong account, an admin can use
"Move money between funds" on the student's ledger, which posts two offsetting
`transfer` rows sharing a reference and writes an audit entry.

## User manual

Teachers and admins can open **User Manual** from the portal sidebar or Dashboard,
or visit `/manual` while signed in. The guide covers daily teacher tasks and studio
administration, with search, a linked contents list, and Print / Save PDF support.
Its source is [`app/templates/manual.html`](app/templates/manual.html); update the
review date in `manual_page` when changing the instructions.

## Running it locally

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
FLASK_PORT=5050 python3 run.py
```

The app creates `data/attendance.db`, runs its migrations on boot, and seeds an `admin` / `admin123` login for local dev only. Production has its own password.

## Deploying

Fly.io builds the `Dockerfile`, which installs `requirements-deploy.txt` and runs one gunicorn worker with 4 threads on a 256 MB machine. Keep it at one worker: two workers ran out of memory and raced on SQLite migrations.

```bash
fly deploy
```

Settings that change in the UI (payment methods, Square, Twilio, Tap-In) live in the database `settings` table, not in environment variables. Secrets set on Fly:

| Variable | What it is |
|---|---|
| `SECRET_KEY` | Signs sessions and encrypts stored credentials. Production refuses to boot without a strong one. |
| `MAIL_SERVER`, `MAIL_PORT`, `MAIL_USERNAME`, `MAIL_PASSWORD` | SMTP for receipts, reminders, blasts and check-in emails |
| `CRON_TOKEN` | Lets the nightly backup job pull a snapshot |
| `PORTAL_URL` | Public address used in emailed links (defaults to https://attenddance.fly.dev) |

The SQLite database lives on a Fly volume at `/data/attendance.db`. A GitHub Action pulls a snapshot to S3 every night (see `backups/README.md`).

## Project structure

```
app/
  __init__.py      app factory, boot-time jobs, security headers
  models.py        every table
  migrations.py    ALTER TABLE migrations that run on boot
  funds.py         the studio vs Company category map
  helpers.py       balances, ledgers, recipients, serializers
  api/routes.py    the REST API
  api/tap_routes.py  Tap-In API: taps, tags, notices, settings
  main/routes.py   page routes
  auth/            sign-in, password reset, parent registration
  tap/             Tap-In core: credentials, tap service, flags, notice outbox
  templates/       Jinja pages (tap/ holds the Tap-In pages)
  static/          service worker, PWA manifest, tap station JS/CSS
config/config.py   defaults (database settings override them)
tests/             test harnesses (CI runs all of them)
docs/tap-in.md     Tap-In hardware, setup and rollout
run.py             entry point (gunicorn imports run:app)
```

## Tests

Every harness is a plain script that prints PASS/FAIL lines and exits non-zero on a failure. CI runs them all on each push and PR.

```bash
python3 tests/smoke_audit.py
python3 tests/test_tap.py
python3 tests/test_tap_flows.py
python3 tests/test_tap_notices.py
```

## Security

- Change the default admin password on any real deployment.
- Parents can only write through an explicit allowlist (`_PARENT_WRITE_ALLOWED` in `app/api/routes.py`). A new parent-facing write endpoint has to be added there.
- Adding a field that holds personal information means updating the model, `app/templates/privacy.html` and the `/api/my-data` export in the same change.
