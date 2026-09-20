---
phase: quick-260919-phq
plan: 01
subsystem: billing
tags: [funds, company, foundation, ledger, payments, statements, exports]
requires: []
provides:
  - app/funds.py (single category-to-fund map)
  - Transaction.fund + PendingPayment.fund with idempotent startup backfill
  - per-fund calc_balance / calc_balance_bulk / allocate_family_payment / build_ledger
  - POST /api/students/<id>/fund-transfer (admin, audited)
  - company_payments_* settings + per-fund /api/payment-options
  - per-fund statements and transactions CSV
affects: [billing table, ledgers, families list, student detail, parent portal, reminders, late fees, aging, revenue, statements, CSV exports, settings]
tech-stack:
  added: []
  patterns: [one map in app/funds.py rendered through a jinja macro; per-fund dict shape everywhere; append-only transfer as two offsetting rows]
key-files:
  created: [app/funds.py, tests/test_company_fund.py]
  modified: [app/models.py, app/migrations.py, app/helpers.py, app/__init__.py, app/api/routes.py, app/main/routes.py, app/templates/_components.html, app/templates/transactions/list.html, app/templates/transactions/ledger.html, app/templates/families/list.html, app/templates/families/ledger.html, app/templates/students/detail.html, app/templates/parent/dashboard.html, app/templates/payments/pending.html, app/templates/settings/payments.html, app/templates/statements/student.html, app/templates/statements/family.html, app/templates/reports/aging.html, app/templates/reports/revenue.html, tests/smoke_audit.py, tests/test_billing.py, .github/workflows/tests.yml, README.md]
decisions:
  - Hard wall between studio and company funds; no combined balance rendered anywhere
  - Company balance hidden on every page (admin and parent) for dancers with no Company history and a zero Company balance
  - Transfer is two offsetting rows sharing an xfer- reference plus an AuditLog entry; never an edit
  - PendingPayment.fund defaults server-side to studio when the client omits it
  - Payment plans stay a studio tool (picker shows the studio balance)
  - Square API invoicing and the Square webhook stay studio-only; Company Square is link-only
metrics:
  duration: two executor sessions (first killed by a rate limit mid Task 2)
  completed: 2026-09-19
---

# Quick Task 260919-phq: Separate Company Money From Studio Money Summary

Company / Foundation money now lives behind a hard fund wall: every transaction and pending payment carries `fund` (studio or company), every balance is computed per fund, the Foundation has its own payment destinations, and an audited two-row transfer covers the wrong-account mistake.

## Commits

| Task | Commit | Subject |
| ---- | ------- | ------- |
| 1 | `9faab95` | feat(billing): add a fund column and the category-to-fund map |
| 2 | `6a8ddb3` | feat(billing): compute every balance per fund with a hard wall |
| 3 | `c5bf2a8` | feat(billing): per-fund ledgers and an audited transfer between funds |
| 4 | `b428501` | feat(payments): give the Company fund its own payment destinations |
| 5 | `3a24ecc` | feat(reports): split statements and exports by fund |

## What changed

**Task 1 (prior session).** `app/funds.py` is the only place category names map to funds (`FUNDS`, `FUND_LABELS`, `CATEGORIES`, `CATEGORY_FUND`, `COMPANY_CATEGORIES`, `SYSTEM_CATEGORIES`, `fund_for_category`, `empty_fund_totals`). `Transaction.fund` (indexed) and `PendingPayment.fund` added; `run_migrations` adds the columns, backfills from category in one UPDATE bound to `COMPANY_CATEGORIES`, and creates `ix_transactions_fund`; a second boot touches zero rows. Every `Transaction(` construction site passes `fund=`. All five category dropdowns render from the `category_options` jinja macro. `GET /api/transactions?fund=` filter.

**Task 2.** `calc_balance` / `calc_balance_bulk` return `{studio: {...}, company: {...}}`; `allocate_family_payment(ids, amount, fund)` allocates within one fund and raises on an unknown fund; `has_company_activity_bulk` (company row or active `CompanyMembership`). Callers moved to the per-fund shape rather than shimmed: `/api/balances` (`funds` + `has_company`, flat keys removed), billing table (Studio / Company columns, muted dash for studio-only dancers), `/api/families` (`funds` + `has_company`), families list, student detail Account card, parent dashboard (per-fund badges, tiles, one Pay button per owed fund, one combined-pay banner per (family, fund)), aging (one row per (student, fund), `by_fund` totals, Fund column in JSON, CSV and page), revenue (`outstanding` per fund, `by_fund` collected), students CSV (Studio balance / Company balance), reminders (one line per owing fund, threshold per fund, shared `_reminder_body` used by manual and auto reminders), late fees (per fund with per-fund idempotency), Square invoice (studio balance only). Payment-plan picker reads the studio balance.

**Task 3.** `build_ledger` keeps a running balance per fund, returns `funds` and `by_category[cat].fund`, no flat keys. Student and family ledger pages: summary row per fund (Company hidden for studio-only), per-fund balance badges, Fund column, "Fund balance" running column, category breakdown grouped by fund, Record Payment gains a required fund select (preselects Company when it is the only fund owed), Send Invoice uses the studio balance. New `POST /api/students/<id>/fund-transfer` (admin only; `amount`, `from_fund`, `to_fund`, optional `note`) posts a `transfer` charge in the source fund and a `transfer` payment in the destination sharing an `xfer-` reference, plus a `fund.transfer` AuditLog entry. "Move money between funds" button and modal on the student ledger.

**Task 4.** New settings keys `company_payments_zelle_enabled/name/memo`, `company_payments_zelle_qr_data`, `company_payments_cashapp_enabled/tag`, `company_payments_square_enabled/link`. `/settings` gains a "Company / Foundation payments" block (ids prefixed `co-`). Both Square link keys validated with the same host allowlist on write and on read. Zelle QR upload/delete take `?fund=`; the image whitelist is factored into `_read_image_data_uri`. `/api/payment-options` returns `{studio: [...], company: [...]}` from one `_options_for_fund` helper (Company Square never reports `configured`). Parent pay modal is per fund (title, destinations, empty state); claim body sends `fund`; `claim_payment` allowlists it (default studio); the admin notify email and the receipt name the fund; the pending inbox has a Fund column and the confirm prompt says which balance it reduces.

**Task 5.** `_statement_rows` returns per-fund prior / totals / running; `_statement_context` adds `ending_balance` per fund and `has_company`. Both statement templates: Fund column, carry-forward row per fund, per-fund subtotal blocks with the "payable to the LSODance Foundation" note, no combined ending balance. `GET /api/reports/transactions.csv` gains a Fund column, `?fund=` filter and per-fund `Subtotal charges / payments` rows appended while streaming. README "Billing funds" section and Billing API list.

## Test results

All twelve harnesses in `.github/workflows/tests.yml` were run locally after each task (`source venv/bin/activate`, `RFID_ENABLED=false`). Final run after Task 5:

```
test_company_fund            SUMMARY: 74/74 passed, 0 failed.
test_billing                 SUMMARY: 22/22 passed, 0 failed.
smoke_audit                  SUMMARY: 447/447 passed, 0 failed (0 P0).
test_seasons                 SUMMARY: 19/19 passed, 0 failed.
test_parent_login            SUMMARY: 51/51 passed, 0 failed.
test_parent_emails           SUMMARY: 24/24 passed, 0 failed.
test_registration_fields     SUMMARY: 66/66 passed, 0 failed.
test_message_recipients      SUMMARY: 27/27 passed, 0 failed.
test_message_attachments     SUMMARY: 29/29 passed, 0 failed.
test_attendance_card         SUMMARY: 52/52 passed, 0 failed.
test_backup                  SUMMARY: 7/7 passed, 0 failed.
test_template_escaping       SUMMARY: 4/4 passed, 0 failed.
```

Sweep checks: em dashes added across the whole change set (`git diff 9faab95^ --unified=0 | grep '^+' | grep -c $'\xe2\x80\x94'`) = 0; `grep -rn 'option value="tuition"' app/templates` empty; every `calc_balance` / `calc_balance_bulk` / `allocate_family_payment` / `build_ledger` call site reads a fund key; `create_app` boots twice cleanly.

`tests/test_company_fund.py` covers: tagging, backfill idempotency + index, single-source map, dropdowns from the map, hard wall, per-fund allocation, per-fund late fee, company visibility (helper, `/api/balances`, detail page), ledger shape + page, transfer (rows, reference, balances, audit, 400s, parent 403), payment options per fund + link validation + QR fund allowlist, pending fund default/400 + confirm into the company fund only, receipt naming the fund, statement per fund (incl. studio-only hides Company), CSV Fund column + subtotals + `?fund=` filter.

## Deviations from plan

- **Task 2 template placement.** `has_company` on the parent dashboard and student detail also shows the Company block when the Company balance is non-zero even without membership or a Company row (a credit from a transfer must stay visible). Plan allowed discretion here.
- **Task 2, payment plans.** `app/templates/payments/pending.html` read `b.balance` from `/api/balances`, which the plan did not list. Updated the picker to read `b.funds.studio.balance` (Rule 3, blocking: the old key no longer exists). Payment plans stay a studio-only tool; Company installments are out of scope.
- **Task 2, `tests/test_billing.py`.** Not in the plan's file list but calls `allocate_family_payment` and reads `calc_balance(...)["balance"]`; updated to pass `"studio"` and index the studio fund (Rule 3).
- **Task 2, aging totals row.** The page renders one totals row per fund above the grand row (instead of only a Fund column) so the Foundation's receivables reconcile on their own.
- **Task 3, `_valid_amount` return shape.** The plan text implied it returns a message; it returns a response tuple. `fund_transfer` returns it directly (caught by Test 10 in this session).
- **Task 4, admin notify email.** Also names the fund label (plan only required the receipt to); cheap and useful for the inbox triage.
- **Task 5, README.** The README had no billing section; added a "Billing funds" subsection under Features and a "Billing" block in API Endpoints.
- No pre-existing test failures were found; nothing was papered over.

## Studio configuration after deploy

On `/settings`, in the new **Company / Foundation payments** block:

1. Turn on Zelle and enter the Foundation's Zelle display name; optionally adjust the memo (default: "Put your dancer's full name and 'Company' in the memo.") and upload the Foundation's Zelle QR (stored under `company_payments_zelle_qr_data`).
2. Turn on Cash App and enter the Foundation's cashtag (e.g. `LSODF`).
3. Optionally turn on the Square payment link and paste a checkout link from the Foundation's Square account (must be a `square.link`, `checkout.square.site` or `squareup.com` URL). Emailed Square invoices and auto-reconcile remain studio-only.

Existing studio settings (`payments_*`), reminders, and late-fee thresholds are unchanged; the reminder and late-fee thresholds now apply to each fund independently. Existing rows are all studio categories, so the boot-time backfill is a no-op in prod.

## Known stubs

None. Every new surface is wired to real data.

## Threat flags

| Flag | File | Description |
|------|------|-------------|
| threat_flag: new endpoint | app/api/routes.py | `POST /api/students/<id>/fund-transfer` creates money rows; admin-only, amount capped by `_valid_amount`, funds allowlisted, audited (T-phq-02/03 mitigated). |
| threat_flag: new setting rendered to parents | app/api/routes.py | `company_payments_square_link` / `company_payments_cashapp_tag` render into hrefs on the parent portal; same host allowlist and cashtag scrub as the studio keys via shared helpers (T-phq-04/05 mitigated). |

## Self-Check: PASSED

- `app/funds.py`, `tests/test_company_fund.py` exist.
- Commits `9faab95`, `6a8ddb3`, `c5bf2a8`, `b428501`, `3a24ecc` are on `main`.
