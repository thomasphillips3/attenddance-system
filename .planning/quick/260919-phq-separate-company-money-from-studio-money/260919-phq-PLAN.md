---
phase: quick-260919-phq
plan: 01
type: execute
wave: 1
depends_on: []
files_modified:
  - app/funds.py
  - app/models.py
  - app/migrations.py
  - app/helpers.py
  - app/__init__.py
  - app/api/routes.py
  - app/main/routes.py
  - app/templates/transactions/list.html
  - app/templates/transactions/ledger.html
  - app/templates/families/list.html
  - app/templates/families/ledger.html
  - app/templates/students/detail.html
  - app/templates/parent/dashboard.html
  - app/templates/payments/pending.html
  - app/templates/settings/payments.html
  - app/templates/statements/student.html
  - app/templates/statements/family.html
  - app/templates/reports/aging.html
  - app/templates/reports/revenue.html
  - tests/test_company_fund.py
  - tests/smoke_audit.py
  - .github/workflows/tests.yml
autonomous: true
requirements: [QUICK-260919-phq]

must_haves:
  truths:
    - "A payment tagged company only reduces the Company balance; a studio credit never offsets a Company charge and vice versa (hard wall, CONTEXT Fund model)"
    - "Every balance surface (billing table, student detail, student and family ledgers, families list, parent portal, reports) shows Studio and Company separately; no combined number is shown anywhere"
    - "A charge posted in a Company category (competition, convention, transportation, lodging, observer, costume_rental, company_dues, company_tickets) lands in fund=company automatically; existing categories stay studio"
    - "Existing rows are backfilled from their category at startup and the backfill is a no-op on a second boot"
    - "Admin can move $X between funds from the ledger page; it creates two offsetting rows sharing a reference and an AuditLog entry, never edits an existing row"
    - "The parent portal shows Foundation payment destinations for a Company balance and studio destinations for a Studio balance; a family owing only one fund sees only that fund"
    - "A parent's 'I sent a payment' claim carries a fund; confirming it records the payment in that fund and the receipt names the fund"
    - "Balance reminders, late fees, aging, statements and CSV exports treat each fund independently"
    - "Category to fund mapping lives only in app/funds.py; every dropdown renders from it"
  artifacts:
    - path: "app/funds.py"
      provides: "Single source of truth: FUNDS, FUND_LABELS, CATEGORIES, CATEGORY_FUND, fund_for_category, categories_for_fund, empty_fund_totals"
      exports: ["FUNDS", "CATEGORIES", "fund_for_category"]
    - path: "app/models.py"
      provides: "Transaction.fund (String(10), not null, default studio, indexed); PendingPayment.fund"
      contains: "fund = db.Column(db.String(10)"
    - path: "app/migrations.py"
      provides: "ALTER TABLE add fund to transactions + pending_payments, idempotent backfill from category, index"
      contains: "_backfill_transaction_fund"
    - path: "app/helpers.py"
      provides: "calc_balance / calc_balance_bulk / build_ledger per fund, allocate_family_payment(student_ids, amount, fund), has_company_activity_bulk"
      contains: "def has_company_activity_bulk"
    - path: "tests/test_company_fund.py"
      provides: "Harness: fund wall, backfill idempotency, per-fund allocation, transfer, payment-options per fund, pending fund"
      min_lines: 150
  key_links:
    - from: "app/migrations.py"
      to: "app/funds.py"
      via: "COMPANY_CATEGORIES import for the backfill CASE"
      pattern: "from app.funds import"
    - from: "app/helpers.py"
      to: "Transaction.fund"
      via: "group_by(Transaction.fund, Transaction.type)"
      pattern: "Transaction\\.fund"
    - from: "app/templates/transactions/list.html"
      to: "app/funds.py"
      via: "FUND_CATEGORIES jinja global renders every category dropdown"
      pattern: "FUND_CATEGORIES"
    - from: "app/api/routes.py get_payment_options"
      to: "Setting company_payments_*"
      via: "per-fund option builder"
      pattern: "company_payments_zelle_enabled"
    - from: "app/templates/parent/dashboard.html"
      to: "/api/payments/claim"
      via: "claim body carries fund"
      pattern: "fund"
---

<objective>
Separate LSODance Company / Foundation money from general studio money in AttenDANCE with a hard fund wall. Add a `fund` dimension (`studio` | `company`) to transactions and pending payments, compute every balance per fund, give the Company fund its own payment destinations, and carry the split through the ledger, parent portal, reminders, late fees, statements, exports and an audited admin transfer action.

Purpose: the studio collects tuition to its own accounts while the Company (run by Carollette, 501(c)(3) LSODance Foundation) collects to the Foundation's accounts. Today both land in one per-student ledger and the money gets mixed. This is the studio's biggest billing pain point.

Output: five sequential commits, app working after each. New `app/funds.py`, `Transaction.fund` + `PendingPayment.fund` with idempotent startup migration, per-fund helpers and API, per-fund UI on every money page, `company_payments_*` settings, `POST /api/students/<id>/fund-transfer`, `tests/test_company_fund.py` wired into CI.
</objective>

<execution_context>
@$HOME/.claude/get-shit-done/workflows/execute-plan.md
@$HOME/.claude/get-shit-done/templates/summary.md
</execution_context>

<context>
@.planning/quick/260919-phq-separate-company-money-from-studio-money/260919-phq-CONTEXT.md
@~/.claude/projects/-Users-thomasphillips-workspace-attendance-system/memory/MEMORY.md
@~/.claude/rules/code-review-lessons.md
@app/models.py
@app/migrations.py
@app/helpers.py
@app/api/routes.py
@app/main/routes.py
@app/__init__.py

<house_rules>
These apply to EVERY task below. Re-read before each commit.

- NO em dashes (U+2014) or en dashes in anything you write: code, comments, docstrings, templates, commit messages, test names. Hyphens only. The existing code has em dashes in old comments; when you edit a line that contains one, replace it with a hyphen. Never add a new one.
- Comments and docstrings must match the implementation in the same edit (a docstring that says "Returns dict with keys: total_charges, total_payments, balance" is wrong once the shape is per fund).
- Conventional commit subjects, imperative mood, under 72 chars. Body explains why. End with the Co-Authored-By line from the session reminder.
- Category to fund mapping lives in app/funds.py ONLY. No new hardcoded `<option value="tuition">` lists, no second dict of company categories anywhere. Render dropdowns from the jinja global; derive fund via `fund_for_category`.
- Prod is a 256MB Fly machine, 1 gunicorn worker. Migrations run at boot via `run_migrations(db)` and must be idempotent and cheap (no per-row Python loops over transactions; one UPDATE).
- Square API invoicing for the Company fund is OUT of scope (link-only). `send_invoice` and the Square webhook stay studio-only.
- Donation model and giving statements are untouched.
- Tests follow the existing harness style (see tests/test_billing.py): standalone script, `record(name, passed, detail)`, `main()` with `sys.exit(1 if fails else 0)`, temp SQLite via DATABASE_URL, run with `RFID_ENABLED=false python tests/<file>.py`. CI runs each harness as a separate step in .github/workflows/tests.yml. There is no pytest runner in CI; do not add one.
- Run the full relevant harnesses before each commit: `RFID_ENABLED=false python tests/test_company_fund.py && RFID_ENABLED=false python tests/test_billing.py && RFID_ENABLED=false python tests/smoke_audit.py`.
</house_rules>

<interfaces>
<!-- Contract for app/funds.py, created in Task 1 and imported everywhere after. -->

From app/funds.py (new, pure module, no app/db imports):
```python
STUDIO = 'studio'
COMPANY = 'company'
FUNDS = (STUDIO, COMPANY)
FUND_LABELS = {STUDIO: 'Studio', COMPANY: 'Company / Foundation'}

# (value, label, fund). Order is dropdown order. These are the user-pickable
# charge categories; a charge's fund is derived from its category.
CATEGORIES = [
    ('tuition', 'Tuition', STUDIO), ('costumes', 'Costumes', STUDIO),
    ('shoes', 'Shoes', STUDIO), ('registration', 'Registration Fee', STUDIO),
    ('other', 'Other', STUDIO),
    ('competition', 'Competition', COMPANY), ('convention', 'Convention', COMPANY),
    ('transportation', 'Transportation', COMPANY), ('lodging', 'Lodging', COMPANY),
    ('observer', 'Observer', COMPANY), ('costume_rental', 'Costume Rental', COMPANY),
    ('company_dues', 'Company Dues', COMPANY), ('company_tickets', 'Company Tickets', COMPANY),
]
# Posted by the app, never picked from a form, and they exist in BOTH funds
# (a late fee or a transfer leg belongs to whichever fund it was assessed in),
# so the writer always passes the fund explicitly for these.
SYSTEM_CATEGORIES = ('late fee', 'transfer')
CATEGORY_FUND = {value: fund for value, _, fund in CATEGORIES}
COMPANY_CATEGORIES = tuple(v for v, _, f in CATEGORIES if f == COMPANY)

def is_fund(value) -> bool
def fund_for_category(category, explicit=None) -> str
    # explicit in FUNDS wins; else CATEGORY_FUND.get(category, STUDIO)
def categories_for_fund(fund) -> list[tuple]
def empty_totals() -> dict          # {'total_charges': 0.0, 'total_payments': 0.0, 'balance': 0.0}
def empty_fund_totals() -> dict     # {fund: empty_totals() for fund in FUNDS}
```

Per-fund balance shape (Task 2 onward), used by every caller:
```python
calc_balance(student_id)      -> {'studio': {...totals}, 'company': {...totals}}
calc_balance_bulk(ids)        -> {student_id: {'studio': {...}, 'company': {...}}}
allocate_family_payment(student_ids, amount, fund) -> [(student_id, amount), ...]  # within fund only
has_company_activity_bulk(student_ids) -> set[int]  # active CompanyMembership OR any fund=company row
build_ledger(txns) -> {
    'ledger': [ {**transaction_to_dict(t), 'fund': ..., 'running_balance': '<running balance of THAT row's fund>'} ],
    'funds': {'studio': {'total_charges','total_payments','balance'}, 'company': {...}},  # 2dp strings
    'by_category': {cat: {'charges','payments','balance','fund'}},
}
```

Existing helpers in app/api/routes.py you will reuse (do not re-implement): `_admin_only()`, `_valid_amount(raw)` -> (amount, err), `_valid_id(raw)`, `_parse_txn_date(raw)`, `_clean_str(value, maxlen)`, `_send_email_async(emails, subject, body)`, `_require_student_money_access(student_id)`, `student_emails(s)`, `family_emails(f)`, `AuditLog.record(user_id, action, detail)` (does not commit).
</interfaces>
</context>

<tasks>

<task type="auto" tdd="true">
  <name>Task 1: Fund map, schema, migration, write path, dropdowns from the map</name>
  <files>app/funds.py, app/models.py, app/migrations.py, app/helpers.py, app/__init__.py, app/api/routes.py, app/templates/transactions/list.html, app/templates/transactions/ledger.html, tests/test_company_fund.py, .github/workflows/tests.yml</files>
  <behavior>
    - Test 1 (tagging): POST /api/transactions charge with category=competition -> stored fund == 'company'; category=tuition -> 'studio'; payment with fund='company' and category=tuition -> 'company'; fund='bogus' -> 400.
    - Test 2 (backfill idempotent): insert via ORM three rows with fund='' (empty string bypasses the model default) for categories tuition, competition, late fee; call `run_migrations(db)`; assert funds are studio, company, studio; snapshot (id, fund, updated) tuple list; call `run_migrations(db)` again; assert identical, no error. Also assert the index `ix_transactions_fund` exists via `sqlalchemy.inspect(db.engine).get_indexes('transactions')`.
    - Test 3 (map is the only source): `set(CATEGORY_FUND) >= {'tuition','costumes','shoes','registration','other'} | set(COMPANY_CATEGORIES)`; `fund_for_category('late fee', 'company') == 'company'`; `fund_for_category('unknown') == 'studio'`.
    - Test 4 (no duplicated dropdowns): read app/templates/transactions/list.html and ledger.html as text, assert `'option value="tuition"'` does not appear (they render from FUND_CATEGORIES) and `'FUND_CATEGORIES'` does.
  </behavior>
  <action>
    Create `app/funds.py` exactly per the `<interfaces>` contract (module docstring: why the map is the single source of truth; no em dashes).

    `app/models.py`: on `Transaction` add `fund = db.Column(db.String(10), nullable=False, default='studio', index=True)` with a comment "studio or company; see app/funds.py". Update the `category` comment (currently "tuition, costumes, shoes, registration, other") to point at app/funds.py instead of listing values. On `PendingPayment` add `fund = db.Column(db.String(10), nullable=False, default='studio')` with comment "which balance the parent says they paid". Add `'fund': t.fund` to `transaction_to_dict` in app/helpers.py and `'fund': p.fund` to `_pending_to_dict` in app/api/routes.py.

    `app/migrations.py`: append `('fund', "VARCHAR(10) DEFAULT 'studio'")` to `TRANSACTION_COLUMNS`; add `PENDING_PAYMENT_COLUMNS = [('fund', "VARCHAR(10) DEFAULT 'studio'")]`. Add `_backfill_transaction_fund(conn)`: one statement, `UPDATE transactions SET fund = CASE WHEN category IN (:c0, :c1, ...) THEN 'company' ELSE 'studio' END WHERE fund IS NULL OR fund = ''`, binding `COMPANY_CATEGORIES` imported from `app.funds` (build the placeholder list from the tuple; never hardcode names here). Then `CREATE INDEX IF NOT EXISTS ix_transactions_fund ON transactions(fund)`. Docstring: today every prod row is a studio category so this is a no-op, but it must exist so a restored pre-fund backup heals on boot, and the WHERE clause makes a second run touch zero rows. In `run_migrations`, inside the existing `if 'transactions' in ...:` block call `_backfill_transaction_fund(conn)` after `_add_missing_columns`; add `if 'pending_payments' in inspector.get_table_names(): _add_missing_columns(conn, inspector, 'pending_payments', PENDING_PAYMENT_COLUMNS)`.

    Write path: set `fund=` at every `Transaction(` construction site so no row ever relies on the column default:
    - `app/api/routes.py create_transaction` (line ~1453): validate `data.get('fund')` with `is_fund` when present (else 400 "fund must be studio or company"); `fund=fund_for_category(data['category'], data.get('fund'))`. Include the fund in the `transaction.create` audit detail.
    - `bulk_charge` (~1881): `fund=fund_for_category(data['category'])`.
    - `_process_recurring_charges` in app/__init__.py (~96): `fund=fund_for_category(rc.category)` (RecurringCharge derives fund from category, no schema change, per CONTEXT).
    - costume charge (~4706): `fund=fund_for_category('costumes')` (stays studio: recital costumes are a studio category).
    - Square webhook (~3838): `fund='studio'` with a comment that Square invoicing is studio-only.
    - `apply_late_fees` (~4989) and `confirm_pending_payment` (~3555): set `fund='studio'` for now with a `# per-fund in a later commit` note; Tasks 2 and 4 replace these.
    - `delete_transaction` audit detail: include `t.fund`.
    - `get_transactions`: accept optional `fund` query param (validated with `is_fund`, filter_by(fund=...)).

    Jinja globals: in `create_app` after the context processors, `from app import funds` and `app.jinja_env.globals.update(FUND_CATEGORIES=funds.CATEGORIES, FUNDS=funds.FUNDS, FUND_LABELS=funds.FUND_LABELS)`.

    Templates: replace the four hardcoded category `<option>` lists in `app/templates/transactions/list.html` (`#filter-category`, `#txn-category`, `#bulk-category`, `#rc-category`) and the one in `app/templates/transactions/ledger.html` (`#lp-category`) with a Jinja loop over `FUND_CATEGORIES` grouped into `<optgroup label="{{ FUND_LABELS[fund] }}">` per fund (keep the leading "All Categories" / "Select..." option and the `selected` default on tuition where it exists today). Add a `#filter-fund` select to the transactions list filter bar (All funds / Studio / Company) and pass `&fund=` in `loadTransactions`. Replace the hardcoded JS `categoryColors` objects in both templates with `const CATEGORY_FUND = {{ FUND_CATEGORIES | tojson }}` reduced to a value->fund map, and a `pillClass(category)` helper: studio categories keep their existing colors where defined, every company category renders `bg-gold-300 text-gold-ink`. Show the fund on each transaction row as a small pill when `t.fund === 'company'`.

    Tests: create `tests/test_company_fund.py` in the harness style with `seed()` (family, two students, admin user via `set_password`) and the four behaviors above; `main()` calls each and exits non-zero on failure. Add a "Company fund harness" step to `.github/workflows/tests.yml` after the billing step, same env.

    Commit: `feat(billing): add a fund column and the category-to-fund map`.
  </action>
  <verify>
    <automated>cd /Users/thomasphillips/workspace/attendance-system && RFID_ENABLED=false python tests/test_company_fund.py && RFID_ENABLED=false python tests/test_billing.py && RFID_ENABLED=false python tests/smoke_audit.py && grep -c 'option value="tuition"' app/templates/transactions/list.html app/templates/transactions/ledger.html | grep -q ':0$' && ! grep -q $'\xe2\x80\x94' app/funds.py tests/test_company_fund.py</automated>
  </verify>
  <done>`app/funds.py` exists and is the only place category names map to funds; `transactions.fund` and `pending_payments.fund` are added by `run_migrations`, backfilled from category in one UPDATE, indexed, and a second run is a no-op (test proves it); every `Transaction(` site passes `fund=`; all five category dropdowns render from `FUND_CATEGORIES`; the new harness is green and wired into CI; existing harnesses stay green.</done>
</task>

<task type="auto" tdd="true">
  <name>Task 2: Fund wall in the balance helpers and every summary caller</name>
  <files>app/helpers.py, app/api/routes.py, app/main/routes.py, app/__init__.py, app/templates/transactions/list.html, app/templates/families/list.html, app/templates/students/detail.html, app/templates/parent/dashboard.html, app/templates/reports/aging.html, app/templates/reports/revenue.html, tests/test_company_fund.py, tests/smoke_audit.py</files>
  <behavior>
    - Test 5 (hard wall): student with studio charge 100, company charge 50, company payment 80 -> `calc_balance` gives studio balance 100.0 and company balance -30.0; the company credit never reduces the studio balance. Then a studio payment of 100 -> studio 0.0, company still -30.0. `calc_balance_bulk([sid])[sid]` equals `calc_balance(sid)`.
    - Test 6 (per-fund allocation): A owes studio 200 + company 30, B owes studio 50. `allocate_family_payment([a, b], 100.0, 'company')` -> only A, `[(a, 100.0)]` (30 owed + 70 credit stays in the company fund), B absent. `allocate_family_payment([a, b], 100.0, 'studio')` -> `[(a, 100.0)]` (largest first), B absent; sum always equals input.
    - Test 7 (late fee per fund): late_fee_min_balance 0, amount 10; student owing company only gets exactly one `late fee` charge with fund=company and none in studio; a second POST in the same month adds nothing.
    - Test 8 (company visibility): `has_company_activity_bulk` returns the student with a company row or an active CompanyMembership and not the studio-only sibling.
  </behavior>
  <action>
    `app/helpers.py`:
    - `calc_balance(student_id)`: group by `(Transaction.fund, Transaction.type)`; return `empty_fund_totals()` filled per fund. Docstring: per-fund shape, "there is never a combined number: a studio credit must not hide a Company debt (CONTEXT: hard wall)".
    - `calc_balance_bulk(student_ids)`: group by `(student_id, fund, type)`; every id present with both funds.
    - `allocate_family_payment(student_ids, amount, fund)`: `fund` required (raise `ValueError` if not `is_fund`); read balances from `calc_balance_bulk(...)[sid][fund]`; same largest-first / cap / leftover-credit algorithm, all inside the one fund. Update docstring.
    - `has_company_activity_bulk(student_ids) -> set[int]`: two queries: distinct `Transaction.student_id` where fund == 'company' and in ids; `CompanyMembership.student_id` where is_active and in ids. Docstring: used to hide the Company balance for studio-only families (CONTEXT: ~90% of families).
    - `has_company_activity(student_id) -> bool` wrapper.

    Update every caller so nothing indexes `['balance']` on the old shape (grep `calc_balance` and `['balance']` in app/ and tests/ and fix each):
    - `get_balances` (`/api/balances`): each row gets `'funds': {fund: {2dp strings}}` and `'has_company': sid in has_company_activity_bulk(...)`; the withdrawn-owing filter is "any fund balance > 0". Remove the flat `total_charges/total_payments/balance` keys. `transactions/list.html renderBalances`: table gains Studio and Company balance columns (Charges/Payments become per-fund tooltips or a second line); a row with `has_company == false` shows a muted dash in the Company column; "activity" filter checks either fund.
    - `_compute_aging`: build `txns_by_student_fund[(sid, fund)]`, call `build_aging` per fund, emit one row per (student, fund) with a `'fund'` key, totals stay grand totals plus a `by_fund` dict; withdrawn filter is any-fund. `export_aging_csv`: add a `Fund` column. `reports/aging.html`: add a Fund column using `FUND_LABELS` (render the labels into a JS const).
    - `export_students_csv`: admin columns become `Studio balance`, `Company balance`.
    - `revenue_report`: `totals.outstanding` becomes `{'studio': x, 'company': y}` plus `by_fund` sums for collected this month / year; `reports/revenue.html` shows both (update the `t-out` line; add a second tile).
    - `send_invoice` (~2065): use `calc_balance(student_id)['studio']['balance']`; comment: Square invoicing is studio-only, the Company fund is link-only.
    - `get_families`: per-family `'funds'` summed across children, `'has_company'` if any child has activity; drop the flat keys. `families/list.html`: render Studio and (when has_company) Company lines. Update the teacher-leak check in `tests/smoke_audit.py` (~line 441) to also assert `"funds" not in f`.
    - `confirm_pending_payment`: pass `fund=p.fund` to `allocate_family_payment` and to the `Transaction(` rows (replaces the Task 1 placeholder).
    - Reminders: `_reminder_body(name, fund_balances)` in routes.py lists one line per fund over threshold: "Studio (LaShelle's School of Dance): $X" and "Company (LSODance Foundation - paid to the Foundation's accounts, see the portal): $Y". `_send_reminders_to`, `send_balance_reminders`, `send_student_reminder`: "owes" means any fund balance > 0; pass the per-fund dict. `app/__init__.py _send_balance_reminders`: same per-fund test against `reminders_min_balance` for each fund independently (CONTEXT) and the same per-fund body; import `FUND_LABELS` for labels rather than a second copy of the strings.
    - `apply_late_fees`: loop `for fund in FUNDS`; threshold test per fund; idempotency query adds `fund=fund`; charge row `fund=fund`, category `'late fee'`, description `f'Late fee ({FUND_LABELS[fund]})'`; audit detail counts per fund.
    - `app/main/routes.py parent_dashboard`: `child_data` gets `'funds': bal` and `'has_company'`; `family_groups` accumulates per fund; `families` (combined-pay offers) becomes a list of `(family, fund, balance)` entries where balance > 0. `parent/dashboard.html`: show a Studio badge and, when `has_company` or the company balance != 0, a Company badge; the Pay buttons pass the fund as a fifth `openPay` argument (the modal itself is reworked in Task 4; for this commit `openPay` stores `fund` in `payTarget` and the claim body sends it).
    - `student_detail` + `students/detail.html`: Account card shows Studio and, when `has_company`, Company.
    - `tests/smoke_audit.py`: every `calc_balance(sid)["balance"]` becomes `["studio"]["balance"]` (lines ~996, 1032, 3071-3099, 4606).

    Add Tests 5-8 to `tests/test_company_fund.py`.

    Commit: `feat(billing): compute every balance per fund with a hard wall`.
  </action>
  <verify>
    <automated>cd /Users/thomasphillips/workspace/attendance-system && RFID_ENABLED=false python tests/test_company_fund.py && RFID_ENABLED=false python tests/test_billing.py && RFID_ENABLED=false python tests/smoke_audit.py && ! grep -rn "\['balance'\]\|\[\"balance\"\]" app/ | grep -v "\['studio'\]\|\['company'\]\|\[fund\]\|\[f\]" </automated>
  </verify>
  <done>`calc_balance`, `calc_balance_bulk`, `allocate_family_payment` are per fund and no caller reads a combined balance; billing table, families list, student detail, parent dashboard badges, aging, revenue, students CSV, reminders and late fees all treat funds independently; the Company balance is hidden for studio-only students on parent-facing and detail pages; harnesses green.</done>
</task>

<task type="auto" tdd="true">
  <name>Task 3: Per-fund ledgers and the audited fund transfer</name>
  <files>app/helpers.py, app/api/routes.py, app/templates/transactions/ledger.html, app/templates/families/ledger.html, tests/test_company_fund.py</files>
  <behavior>
    - Test 9 (ledger shape): `build_ledger` on mixed rows returns `funds.studio.balance` and `funds.company.balance` as 2dp strings, each ledger row carries `fund` and a `running_balance` that only moves for rows of its own fund; `by_category[cat]['fund']` matches the map.
    - Test 10 (transfer): admin POST `/api/students/<id>/fund-transfer` `{amount: 25, from_fund: 'studio', to_fund: 'company', note: 'paid wrong account'}` -> 201; exactly two new Transaction rows, one `charge` fund=studio and one `payment` fund=company, both category `transfer`, both descriptions containing the same `xfer-` reference; studio balance up 25, company down 25; an `AuditLog` row with action `fund.transfer` exists; `from_fund == to_fund` -> 400; amount <= 0 -> 400; a parent user -> 403.
  </behavior>
  <action>
    `app/helpers.py build_ledger(txns)`: keep one running balance per fund (`running = {f: 0.0 for f in FUNDS}`); each row's `running_balance` is `running[t.fund]` after applying that row; totals per fund in `'funds'`; `by_category[cat]` gains `'fund': fund_for_category(cat)` (for `SYSTEM_CATEGORIES` use the fund of the rows, which the loop already knows). Drop the flat `total_charges/total_payments/balance` keys. Docstring updated.

    `app/api/routes.py`:
    - `get_student_ledger` and `get_family_ledger`: spread the new result; add `'has_company': ...` from `has_company_activity_bulk`.
    - New `POST /api/students/<int:student_id>/fund-transfer` (admin only via `_admin_only`): body `amount` (through `_valid_amount`), `from_fund`, `to_fund` (both `is_fund`, must differ), optional `note` (`_clean_str(..., 300)`). `ref = 'xfer-' + secrets.token_hex(4)` (secrets is already imported). Create two rows in one session: `Transaction(type='charge', fund=from_fund, category='transfer', payment_method='n/a', description=f'Transfer to {FUND_LABELS[to_fund]} [{ref}]' + note, transaction_date=date.today(), created_by=current_user.id)` and `Transaction(type='payment', fund=to_fund, category='transfer', payment_method='transfer', description=f'Transfer from {FUND_LABELS[from_fund]} [{ref}]' + note, ...)`. `AuditLog.record(current_user.id, 'fund.transfer', f'{ref}: ${amount:.2f} {from_fund} -> {to_fund} for {student.full_name}' + note)`; commit; return both rows via `transaction_to_dict` and the ref. Docstring: why two offsetting rows and never an edit of an existing row (CONTEXT). Add `'transfer'` to the `methodLabels` maps in both ledger templates.

    `app/templates/transactions/ledger.html`:
    - Summary cards become two rows: Studio (charges / payments / balance) and Company (same); the Company row is hidden when `has_company` is false and the company balance is 0.
    - Balance badge at top shows one pill per fund with a non-zero balance.
    - Ledger table: add a Fund column (pill) before Category; the running Balance column header reads "Fund balance" and a title attribute explains it runs within that row's fund.
    - Category breakdown cards: group under Studio / Company headings using `c.fund`.
    - Record Payment modal: add a required `#lp-fund` select (Studio / Company, rendered from `FUNDS` + `FUND_LABELS`, default studio, preselect company when the company balance > 0 and studio is 0); the body sends `fund`. Keep `#lp-category` (already rendered from the map in Task 1).
    - New "Move money between funds" button (admin only) opening `#transfer-modal` with amount, from, to, note; posts to the new endpoint; on success toast and `loadLedger()`; the confirm text states this creates two offsetting entries.
    - `checkSquare`: use `data.funds.studio.balance` (Square invoice is studio-only).

    `app/templates/families/ledger.html`: same summary-card and Fund-column treatment (no payment or transfer modals there).

    Add Tests 9-10 to the harness.

    Commit: `feat(billing): per-fund ledgers and an audited transfer between funds`.
  </action>
  <verify>
    <automated>cd /Users/thomasphillips/workspace/attendance-system && RFID_ENABLED=false python tests/test_company_fund.py && RFID_ENABLED=false python tests/smoke_audit.py && grep -q "fund-transfer" app/api/routes.py app/templates/transactions/ledger.html</automated>
  </verify>
  <done>Student and family ledger pages show Studio and Company totals, a fund pill per row, and a per-fund running balance; the Record Payment form picks a fund; admins can move money between funds from the ledger and the action leaves two linked rows plus an audit entry; harness green.</done>
</task>

<task type="auto" tdd="true">
  <name>Task 4: Foundation payment destinations and the parent pay flow</name>
  <files>app/api/routes.py, app/templates/settings/payments.html, app/templates/parent/dashboard.html, app/templates/payments/pending.html, tests/test_company_fund.py, tests/smoke_audit.py</files>
  <behavior>
    - Test 11 (payment-options per fund): with `payments_zelle_enabled=1` and `company_payments_cashapp_enabled=1` + tag `LSODF`, GET `/api/payment-options` returns `payment_options.studio` containing a zelle option and no cashapp, and `payment_options.company` containing a cashapp option with cashtag `LSODF` and no zelle. A company Square link on a non-Square host is rejected by PUT `/api/settings/payments` with 400.
    - Test 12 (pending fund): parent POST `/api/payments/claim` with `fund: 'company'` -> pending row fund == company; without `fund` -> studio; `fund: 'x'` -> 400. Admin confirm of the company claim creates the payment row(s) with fund=company and the studio balance is unchanged.
    - Test 13 (receipt names the fund): monkeypatch `_send_email_async` in routes to capture, confirm a company claim with email configured (`app.config['MAIL_SERVER']` or the `is_configured` monkeypatched to True), assert the captured body contains "Company / Foundation".
  </behavior>
  <action>
    `app/api/routes.py`:
    - `PAYMENT_SETTINGS_KEYS`: add `company_payments_zelle_enabled`, `company_payments_zelle_name`, `company_payments_zelle_memo`, `company_payments_cashapp_enabled`, `company_payments_cashapp_tag`, `company_payments_square_enabled`, `company_payments_square_link` under a `# Company / Foundation destinations (link-only Square)` comment. `update_payment_settings`: validate BOTH link keys with `_valid_square_link` before writing (loop over `('payments_square_link', 'company_payments_square_link')`). `get_payment_settings`: add `has_company_zelle_qr` / `company_zelle_qr` from `company_payments_zelle_qr_data`; default memo for the company block: "Put your dancer's full name and 'Company' in the memo."
    - Zelle QR upload/delete: accept `?fund=company` on the existing `POST/DELETE /settings/payments/zelle-qr`; `_zelle_qr_key(fund)` returns `payments_zelle_qr_data` or `company_payments_zelle_qr_data`; invalid fund -> 400; audit detail names the fund. Same image whitelist, no duplication of the validation block (factor it into `_read_image_data_uri(file)` if needed).
    - `get_payment_options`: factor the current body into `_options_for_prefix(prefix)` (prefix `payments_` or `company_payments_`), reusing the cashtag scrub and link re-validation; return `{'payment_options': {'studio': _options_for_prefix('payments_'), 'company': _options_for_prefix('company_payments_')}}`. The company entry never reports `configured` Square API invoicing (link-only): omit the `configured` key for company or set it False with a comment.
    - `claim_payment`: read `fund = data.get('fund') or 'studio'`, validate with `is_fund` (400 otherwise), store on `PendingPayment(fund=fund)`; admin notification email names the fund label. (Server-side default per Claude's discretion: the portal always sends it, the default protects older clients.)
    - `confirm_pending_payment`: already uses `p.fund` (Task 2); pass `p.fund` to `_send_receipt`. `_send_receipt(parent_email, who, amount, method, fund)`: body says "for {who} ({FUND_LABELS[fund]} account)". Square webhook call passes `'studio'`.
    - `_pending_to_dict` already emits `fund` (Task 1).
    - Update `tests/smoke_audit.py` lines ~3745 and ~3755 to read `.get("payment_options", {}).get("studio", [])`.

    `app/templates/settings/payments.html`: add a "Company / Foundation payments" card after the Square card mirroring the studio Zelle / Cash App / Square-link controls with ids prefixed `co-` (`co-zelle-enabled`, `co-zelle-name`, `co-zelle-memo`, `co-qr-preview`, `co-qr-file`, `co-qr-remove`, `co-cashapp-enabled`, `co-cashapp-tag`, `co-square-enabled`, `co-square-link`); intro text: "Money for Company activities goes to the Foundation's own accounts. These are the destinations parents see for a Company balance." No Square token / location / webhook fields in this block. `loadSettings`/`saveSettings` read and write the `company_payments_*` keys; `uploadQR(fund)` / `removeQR(fund)` / `renderQR(fund, data)` take a fund argument and hit `?fund=company` for the company block.

    `app/templates/parent/dashboard.html`:
    - `PAY_OPTIONS` becomes the per-fund object; `loadPayOptions` stores `data.payment_options || {studio: [], company: []}`.
    - `openPay(type, id, name, amount, fund)` sets `payTarget.fund`; the modal title shows the fund label ("Pay Studio balance for Maya" / "Pay Company balance for Maya"); `renderMethods()` renders `PAY_OPTIONS[payTarget.fund]`; the Zelle card title uses `opt.name` (the Foundation's Zelle name for company); the empty-state text for company says "No Foundation payment methods are set up yet. Please contact the studio."
    - `submitClaim(method)` adds `fund: payTarget.fund` to the body.
    - Per child: one "Pay $X (Studio)" button when the studio balance > 0 and one "Pay $Y (Company)" when the company balance > 0; a family combined-pay banner per (family, fund) entry from Task 2. A family owing one fund sees only that fund's button and destinations (CONTEXT).
    - Pending claims list and payment history show a fund pill when `fund === 'company'`.

    `app/templates/payments/pending.html`: add a Fund column to the inbox table rendered from `p.fund` with the gold pill for company; `confirmPay` confirmation text says which balance it will reduce ("This will reduce the Company / Foundation balance.").

    Add Tests 11-13 to the harness.

    Commit: `feat(payments): give the Company fund its own payment destinations`.
  </action>
  <verify>
    <automated>cd /Users/thomasphillips/workspace/attendance-system && RFID_ENABLED=false python tests/test_company_fund.py && RFID_ENABLED=false python tests/smoke_audit.py && RFID_ENABLED=false python tests/test_parent_login.py && grep -c "company_payments_" app/api/routes.py | awk '{exit !($1>=8)}'</automated>
  </verify>
  <done>Admins configure Foundation Zelle / Cash App / Square-link destinations on /settings; `/api/payment-options` returns options per fund; parents see the right destination for each balance they owe and their "I sent a payment" claim carries the fund; the inbox shows the fund and confirming records into it; receipts name the fund; harnesses green.</done>
</task>

<task type="auto">
  <name>Task 5: Statements and exports per fund, final sweep</name>
  <files>app/main/routes.py, app/api/routes.py, app/templates/statements/student.html, app/templates/statements/family.html, tests/test_company_fund.py, README.md</files>
  <action>
    `app/main/routes.py _statement_rows(student_ids, year)`: return per-fund figures: `prior` becomes `{fund: float}`, `rows` keep `{'t': t, 'running': running[t.fund]}` with per-fund running balances seeded from the per-fund prior, and `tc` / `tp` become `{fund: float}`. `student_statement` / `family_statement` pass `prior_balance`, `total_charges`, `total_payments`, `ending_balance` as per-fund dicts plus `has_company` (True when any company row or a non-zero company prior). Update the docstring.

    `app/templates/statements/student.html` and `family.html`: add a Fund column (label from `FUND_LABELS`); the "Balance brought forward" row becomes one row per fund (Company row only when `has_company`); the running Balance column is labelled "Fund balance"; the totals table shows Total charged / Total paid / Ending balance per fund with a Studio subtotal block and, when `has_company`, a Company / Foundation subtotal block with a one-line note "Company amounts are payable to the LSODance Foundation." Never show a grand combined ending balance (CONTEXT).

    `app/api/routes.py export_transactions_csv`: add a `Fund` column (after Category) and, after the rows, append two subtotal rows per fund (`Subtotal charges - Studio`, `Subtotal payments - Studio`, same for Company) computed while streaming (the generator currently yields row by row; collect fund sums as you go and yield the subtotal rows at the end via a small wrapper generator). Optional `?fund=` filter using `is_fund`.

    Harness: Test 14 (statement per fund): render `/students/<id>/statement?year=<this year>` as admin for a student with a company charge and a studio charge; assert the response contains "Company / Foundation" and both fund subtotals and does not contain a grand "Ending balance" row without a fund label. Test 15 (CSV): `/api/reports/transactions.csv` header contains `Fund` and the body contains `Subtotal charges - Company`.

    Final sweep, in this task, before the commit:
    - `grep -rn $'\xe2\x80\x94' app/funds.py tests/test_company_fund.py` must be empty, and `git diff main --unified=0 | grep '^+' | grep -c $'\xe2\x80\x94'` must be 0 (no em dash added anywhere in this change set).
    - `grep -rn 'option value="tuition"' app/templates` must be empty.
    - `grep -rn "calc_balance\|calc_balance_bulk\|allocate_family_payment\|build_ledger" app tests` and confirm every call site reads a fund key.
    - Run every harness in `.github/workflows/tests.yml` locally.
    - README.md: add a short "Funds" paragraph under the billing section (Studio vs Company / Foundation, the transfer action, where to set Foundation destinations). No em dashes.

    Commit: `feat(reports): split statements and exports by fund`.
  </action>
  <verify>
    <automated>cd /Users/thomasphillips/workspace/attendance-system && for t in test_company_fund test_billing smoke_audit test_seasons test_parent_login test_parent_emails test_registration_fields test_message_recipients test_message_attachments test_attendance_card test_backup test_template_escaping; do RFID_ENABLED=false python tests/$t.py || exit 1; done && [ "$(git diff main --unified=0 | grep '^+' | grep -c $'\xe2\x80\x94')" = "0" ] && ! grep -rq 'option value="tuition"' app/templates</automated>
  </verify>
  <done>Year-end statements and the transactions CSV carry a fund column and per-fund subtotals with no combined ending balance; every harness in CI is green locally; no em dash was introduced by the change set; README documents the fund split.</done>
</task>

</tasks>

<threat_model>
## Trust Boundaries

| Boundary | Description |
|----------|-------------|
| parent -> /api/payments/claim | Untrusted `fund`, `amount`, `student_id`/`family_id` from a parent session |
| admin -> /api/settings/payments | Foundation Zelle name/memo, cashtag and Square link rendered into the parent portal (href / img src) |
| admin -> /api/students/<id>/fund-transfer | Creates money rows; must be admin-only, validated, audited |
| startup -> run_migrations | Runs SQL at boot on the prod volume |

## STRIDE Threat Register

| Threat ID | Category | Component | Disposition | Mitigation Plan |
|-----------|----------|-----------|-------------|-----------------|
| T-phq-01 | Tampering | claim_payment `fund` | mitigate | `is_fund` allowlist, 400 otherwise; existing owner checks on student/family unchanged |
| T-phq-02 | Elevation | fund-transfer endpoint | mitigate | `_admin_only()` first line; `_valid_amount` cap; from != to; parent -> 403 covered by Test 10 |
| T-phq-03 | Repudiation | fund-transfer, late fees per fund, claim confirm | mitigate | `AuditLog.record` with ref, amount, funds, actor on every write path; rows are never edited in place |
| T-phq-04 | Tampering | company_payments_square_link | mitigate | same `_valid_square_link` host allowlist on write AND re-validate on read in `_options_for_prefix` |
| T-phq-05 | Tampering | company_payments_cashapp_tag | mitigate | same `[^A-Za-z0-9_]` scrub and 20-char cap as the studio tag (shared helper, not a copy) |
| T-phq-06 | Info disclosure | /api/families, /api/balances funds keys | mitigate | money keys only when `current_user.is_admin`; smoke_audit leak check extended to `funds` |
| T-phq-07 | DoS | backfill migration at boot on 256MB machine | mitigate | single UPDATE with WHERE fund IS NULL OR '' and CREATE INDEX IF NOT EXISTS; no Python row loop |
| T-phq-08 | Tampering | Zelle QR upload `?fund=` | mitigate | fund allowlist; identical image-type whitelist; data URI stored under the fund-specific key only |
| T-phq-SC | Tampering | package installs | accept | no new dependencies in this change set |
</threat_model>

<verification>
After Task 5, from a clean checkout:
1. `RFID_ENABLED=false FLASK_PORT=5050 python3 run.py` boots against an existing dev DB without error (migration + backfill run, second boot logs nothing new).
2. As admin: /transactions shows Studio and Company columns; the Add Charge category dropdown has a Studio group and a Company / Foundation group; posting a Competition charge to a dancer shows only in their Company column.
3. Record a $20 Studio payment on that dancer's ledger: Company balance does not move.
4. Move $20 Studio -> Company from the ledger: two `transfer` rows appear, audit log has `fund.transfer`.
5. /settings: Company / Foundation block saves a Cash App tag; the parent of that dancer sees a "Pay Company balance" button with the Foundation cashtag and no studio Zelle.
6. Parent reports the payment; /pending-payments shows fund Company; confirm; receipt body names "Company / Foundation account"; studio balance unchanged.
7. /students/<id>/statement shows a Fund column and separate subtotals; /api/reports/transactions.csv has a Fund column and per-fund subtotal rows.
8. Every harness in .github/workflows/tests.yml passes locally.
</verification>

<success_criteria>
- Five commits on the branch, app boots and every listed harness passes after each one.
- `app/funds.py` is the only file that names company categories (grep for `company_dues` finds funds.py, tests, and templates that iterate `FUND_CATEGORIES`, nothing else hardcoded).
- No API response or template renders a combined studio+company balance.
- `tests/test_company_fund.py` covers: fund tagging, backfill idempotency, hard wall, per-fund allocation, per-fund late fee, company visibility, ledger shape, transfer, payment-options per fund, pending fund + confirm, receipt naming, statement + CSV per fund; it is a step in tests.yml.
- Zero em dashes added by the change set.
</success_criteria>

<output>
Create `.planning/quick/260919-phq-separate-company-money-from-studio-money/260919-phq-SUMMARY.md` when done, listing the five commits, the new settings keys, the new endpoint, and any deviation from this plan with the reason.
</output>
