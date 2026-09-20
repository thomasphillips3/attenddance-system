# Quick Task 260919-phq: Separate Company money from studio money - Context

**Gathered:** 2026-09-20
**Status:** Ready for planning

<domain>
## Task Boundary

LaShelle's School of Dance runs a performance Company (LSODance Company, run by
Carollette) that competes locally and nationally. The Company has its own
501(c)(3) (LSODance Foundation, lsodf.org) and its own bank accounts. Today every
charge and payment lands in one ledger per student, so Company money and studio
money get mixed. This is a major pain point for the studio.

Goal: Company money is kept cleanly separate from general studio money at every
layer - charges, payments, balances, parent-facing pay instructions, reminders,
statements, and reports. Money for Company activities goes to a separate
(Foundation) account and never counts toward general studio balances.

Company-specific money (must NOT intermingle with studio money):
- Competition
- Convention
- Transportation
- Lodging
- Observer
- Costume Rental
- Company Dues
- Company Tickets

Existing `costumes` (recital costumes) stays a studio category. Existing
`Donation` (Foundation giving) is already separate and is NOT part of this task.

</domain>

<decisions>
## Implementation Decisions

### Fund model (user decision: "Hard wall + explicit transfer")
- Introduce a `fund` dimension with two values: `studio` and `company`.
- A payment tagged `company` only ever reduces the Company balance, even if the
  family has a studio credit. Same in reverse. No automatic cross-fund netting.
- Every balance is computed per fund. There is never a single combined number
  shown as "the balance" without both funds visible.
- Admin gets an audited "move $X between funds" action for the rare mistake
  (parent paid the wrong account). Implemented as two offsetting transactions
  (one per fund) linked by a shared reference, recorded in AuditLog. Not a
  silent edit of an existing row.

### Data model
- `Transaction.fund` column (String(10), not null, default `studio`), indexed.
  Derived from `category` on write for charges; explicitly chosen for payments
  (a payment has no meaningful category beyond the fund it settles).
- Backfill migration: existing rows get `fund` from their category using the
  category->fund map. All current categories are studio, so this is a no-op
  in prod today, but the migration must still exist and be idempotent (app
  auto-migration pattern in `app/__init__.py` / `app/migrations.py`).
- Category->fund map lives in ONE place (`app/helpers.py` or a small
  `app/funds.py`) and is the single source of truth for: charge forms, category
  dropdowns, balance calc, exports, statements.
- `RecurringCharge` and `PaymentPlan` (and installments) derive fund from
  category; no schema change unless the executor finds one is required.
- Late fees are applied per fund against that fund's overdue balance.

### Balance + ledger
- `calc_balance` / `calc_balance_bulk` / `build_ledger` return per-fund
  totals: `{'studio': {...}, 'company': {...}}` plus per-category breakdown.
  Existing callers that expect the old shape must be updated, not shimmed
  with a combined number.
- Student ledger page, transactions list, families list, dashboard tiles,
  parent portal: show two balances (Studio / Company). Company balance can be
  hidden when a student has no Company activity at all (no company memberships
  AND no company transactions) to avoid noise for the ~90% of families who are
  studio-only.
- `allocate_family_payment` allocates within a single fund only.

### Payment destinations (Foundation has its own accounts)
- New Setting keys under a `company_payments_*` prefix mirroring the studio
  ones: `company_payments_zelle_enabled/name/memo/qr_data`,
  `company_payments_cashapp_enabled/tag`, `company_payments_square_enabled/link`.
  Square API invoicing for the Company fund is OUT of scope for this task
  (link-only); studio Square invoicing is untouched.
- `/settings` payments page gets a second "Company / Foundation" block.
- `GET /api/payment-options` returns options per fund.
- Parent portal "how to pay" shows the correct destination for each balance
  the family owes. If they only owe one fund, show only that one.
- "I sent a payment" (PendingPayment) gains a `fund` field. Parent picks which
  balance they paid; default = the fund with a balance, or studio if both.
  Admin pending-payments inbox shows fund and confirming creates the
  Transaction in that fund.
- Square webhook: studio fund only (unchanged).

### Reminders, statements, exports
- Balance reminders and auto-reminders are per fund: separate line items in
  the email, separate thresholds not required (reuse `reminders_min_balance`
  against each fund independently).
- Year-end statements and any CSV/ledger export include fund as a column and
  subtotal per fund.
- Receipts name the fund that was paid.

### Access
- No new role. Company billing is done by existing admin/staff users.

### Claude's Discretion
- Exact naming of category values (snake_case as listed above is fine).
- Whether to hide the Company balance for studio-only students on every page
  or only on the parent portal.
- UI placement of the fund transfer action (ledger page is the natural spot).
- Whether `PendingPayment.fund` defaults are computed server-side or client-side.

</decisions>

<specifics>
## Specific Ideas

- Company program: https://lsodance.com/company-auditions-4
- Foundation: https://www.lsodf.org/
- Carollette runs Company + Foundation and is the primary user of the Company
  side of this. Wilkanda is the owner. Both are admins.
- Existing models already involved: `Transaction`, `RecurringCharge`,
  `PaymentPlan`, `PendingPayment`, `Setting`, `PerformanceGroup`,
  `CompanyMembership`, `AuditLog`.
- Category dropdowns are duplicated in `app/templates/transactions/list.html`
  (4x) and `ledger.html`. The executor should render them from the single map
  rather than adding 8 more hardcoded options to each.

</specifics>

<canonical_refs>
## Canonical References

- Memory: `~/.claude/projects/-Users-thomasphillips-workspace-attendance-system/memory/MEMORY.md`
  (project overview, people, deploy constraints, COPPA rule: adding PII = update
  privacy page + my-data export; `fund` is not PII).
- `~/.claude/rules/code-review-lessons.md` (comment/code sync, monotonic
  clocks, etc.)
- `~/.claude/about-me/voice.md`: NO em dashes anywhere, including code
  comments and commit messages. Hyphens only.

</canonical_refs>
