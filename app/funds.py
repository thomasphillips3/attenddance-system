"""The two money funds and the category-to-fund map.

LaShelle's School of Dance collects tuition and studio fees to its own
accounts. The LSODance Company (a performance company with its own 501(c)(3),
the LSODance Foundation) collects Company money to the Foundation's accounts.
The two must never mix: a Company payment only ever settles Company charges,
and a studio credit never hides a Company debt.

This module is the ONLY place that names which charge categories belong to
which fund. Every dropdown renders from CATEGORIES, every writer derives a
charge's fund with fund_for_category, and the startup backfill migration
binds COMPANY_CATEGORIES. Adding a category here is the whole change; a
second copy of the list anywhere else would drift and mis-file money.

Pure module: no app or db imports, so migrations and tests can import it
without an application context.
"""

STUDIO = 'studio'
COMPANY = 'company'
FUNDS = (STUDIO, COMPANY)
FUND_LABELS = {STUDIO: 'Studio', COMPANY: 'Company / Foundation'}

# (value, label, fund). Order is dropdown order. These are the user-pickable
# charge categories; a charge's fund is derived from its category.
CATEGORIES = [
    ('tuition', 'Tuition', STUDIO),
    ('costumes', 'Costumes', STUDIO),
    ('shoes', 'Shoes', STUDIO),
    ('registration', 'Registration Fee', STUDIO),
    ('other', 'Other', STUDIO),
    ('competition', 'Competition', COMPANY),
    ('convention', 'Convention', COMPANY),
    ('transportation', 'Transportation', COMPANY),
    ('lodging', 'Lodging', COMPANY),
    ('observer', 'Observer', COMPANY),
    ('costume_rental', 'Costume Rental', COMPANY),
    ('company_dues', 'Company Dues', COMPANY),
    ('company_tickets', 'Company Tickets', COMPANY),
]

# Posted by the app, never picked from a form, and they exist in BOTH funds
# (a late fee or a transfer leg belongs to whichever fund it was assessed in),
# so the writer always passes the fund explicitly for these.
SYSTEM_CATEGORIES = ('late fee', 'transfer')

CATEGORY_FUND = {value: fund for value, _, fund in CATEGORIES}
CATEGORY_LABELS = {value: label for value, label, _ in CATEGORIES}
COMPANY_CATEGORIES = tuple(v for v, _, f in CATEGORIES if f == COMPANY)


def is_fund(value) -> bool:
    """True when `value` is exactly one of the two fund names."""
    return value in FUNDS


def fund_for_category(category, explicit=None) -> str:
    """Which fund a row belongs to.

    An explicit fund (already validated with is_fund) always wins: payments
    have no meaningful category beyond the balance they settle, and the
    system categories exist in both funds. Otherwise the category decides,
    and anything unmapped (legacy or unknown) is studio money.
    """
    if explicit in FUNDS:
        return explicit
    return CATEGORY_FUND.get(category, STUDIO)


def categories_for_fund(fund) -> list:
    """The (value, label, fund) tuples for one fund, in dropdown order."""
    return [c for c in CATEGORIES if c[2] == fund]


def empty_totals() -> dict:
    """The per-fund totals shape with nothing posted yet."""
    return {'total_charges': 0.0, 'total_payments': 0.0, 'balance': 0.0}


def empty_fund_totals() -> dict:
    """{fund: empty_totals()} for both funds, so callers can index either
    fund without a KeyError even when a student has no rows in it."""
    return {fund: empty_totals() for fund in FUNDS}
