"""Database schema migrations for AttenDANCE.
Adds columns to existing tables on startup (SQLite ALTER TABLE).
"""

import sqlalchemy

from app.funds import COMPANY_CATEGORIES


STUDENT_COLUMNS = [
    ('school', 'VARCHAR(150)'), ('grade', 'VARCHAR(30)'),
    ('allergies', 'TEXT'), ('special_needs', 'TEXT'),
    ('family_id', 'INTEGER'), ('height', 'VARCHAR(20)'),
    ('weight', 'VARCHAR(20)'), ('shoe_size', 'VARCHAR(20)'),
    ('shirt_size', 'VARCHAR(20)'), ('pants_size', 'VARCHAR(20)'),
    ('leotard_size', 'VARCHAR(20)'), ('dress_size', 'VARCHAR(20)'),
    ('waist', 'VARCHAR(20)'), ('girth', 'VARCHAR(20)'),
    ('inseam', 'VARCHAR(20)'), ('neck', 'VARCHAR(20)'),
    ('tight_size', 'VARCHAR(20)'), ('bust', 'VARCHAR(20)'),
    ('hips', 'VARCHAR(20)'), ('sleeve', 'VARCHAR(20)'),
    ('chest', 'VARCHAR(20)'), ('size_notes', 'TEXT'),
    ('parent_phone', 'VARCHAR(20)'),
    ('parent_email_2', 'VARCHAR(120)'),
]

USER_COLUMNS = [
    ('role', "VARCHAR(20) DEFAULT 'teacher'"),
    ('invite_code', 'VARCHAR(20)'),
]

TRANSACTION_COLUMNS = [
    ('type', "VARCHAR(10) DEFAULT 'payment'"),
    ('recurring_charge_id', 'INTEGER'),
    ('fund', "VARCHAR(10) DEFAULT 'studio'"),
]

PENDING_PAYMENT_COLUMNS = [
    ('fund', "VARCHAR(10) DEFAULT 'studio'"),
]

CLASS_COLUMNS = [
    ('location_id', 'INTEGER'),
    ('season_id', 'INTEGER'),
]

PERFORMANCE_COLUMNS = [
    ('recital_id', 'INTEGER'),
]

# Second guardian + home address, collected on the public registration form and
# carried onto the household at approval.
FAMILY_COLUMNS = [
    ('secondary_name', 'VARCHAR(120)'),
    ('secondary_email', 'VARCHAR(120)'),
    ('secondary_phone', 'VARCHAR(20)'),
    ('address', 'VARCHAR(200)'),
    ('city', 'VARCHAR(80)'),
    ('state', 'VARCHAR(40)'),
    ('zip_code', 'VARCHAR(20)'),
]

# Tardy is a flavor of present: is_present stays True, is_tardy marks the T.
ATTENDANCE_COLUMNS = [
    ('is_tardy', 'BOOLEAN DEFAULT 0 NOT NULL'),
]

REGISTRATION_COLUMNS = [
    ('parent2_name', 'VARCHAR(120)'),
    ('parent2_email', 'VARCHAR(120)'),
    ('parent2_phone', 'VARCHAR(20)'),
    ('emergency_name', 'VARCHAR(120)'),
    ('emergency_phone', 'VARCHAR(20)'),
    ('emergency_relationship', 'VARCHAR(60)'),
    ('address', 'VARCHAR(200)'),
    ('city', 'VARCHAR(80)'),
    ('state', 'VARCHAR(40)'),
    ('zip_code', 'VARCHAR(20)'),
]


def _add_missing_columns(conn, inspector, table, columns):
    existing = [c['name'] for c in inspector.get_columns(table)]
    for col, coltype in columns:
        if col not in existing:
            conn.execute(sqlalchemy.text(f'ALTER TABLE {table} ADD COLUMN {col} {coltype}'))


def _backfill_transaction_fund(conn):
    """Give every transaction a fund derived from its category, in one UPDATE.

    Rows written before the fund column existed have fund NULL (ALTER TABLE
    default applies only to new rows on some SQLite versions) or '' (an ORM
    write that bypassed the model default). Today every prod row is a studio
    category, so this is a no-op there, but it must exist so a restored
    pre-fund backup heals on boot. The WHERE clause makes a second run touch
    zero rows, and the single statement keeps the 256MB machine's boot cheap:
    no per-row Python loop. The company list is bound from app/funds.py, never
    typed here, so the map stays the only source of truth."""
    placeholders = ', '.join(f':c{i}' for i in range(len(COMPANY_CATEGORIES)))
    params = {f'c{i}': cat for i, cat in enumerate(COMPANY_CATEGORIES)}
    conn.execute(sqlalchemy.text(
        'UPDATE transactions SET fund = CASE '
        f"WHEN category IN ({placeholders}) THEN 'company' ELSE 'studio' END "
        "WHERE fund IS NULL OR fund = ''"), params)
    conn.execute(sqlalchemy.text(
        'CREATE INDEX IF NOT EXISTS ix_transactions_fund ON transactions(fund)'))


def _reconcile_admin_role(conn):
    """The default admin was seeded with is_admin=1 but role defaulted to
    'teacher', so `filter_by(role='admin')` missed it — meaning admin email
    notifications (new registration requests, parent-reported payments) silently
    went to nobody on the studio's primary account. Reconcile role='admin' for
    any is_admin user whose role disagrees. Idempotent."""
    conn.execute(sqlalchemy.text(
        "UPDATE users SET role='admin' "
        "WHERE is_admin=1 AND (role IS NULL OR role != 'admin')"))


def _enforce_attendance_uniqueness(conn):
    """One attendance row per (student, class, day). The Attendance model has no
    UniqueConstraint, so a concurrent double-tap could create duplicate 'present'
    rows (inflating counts + breaking the toggle). De-dupe any existing dupes
    (keep the earliest row) then add a functional unique index so the DB rejects
    duplicates. Idempotent: the DELETE is a no-op on clean data and the index is
    IF NOT EXISTS."""
    conn.execute(sqlalchemy.text(
        'DELETE FROM attendance WHERE id NOT IN ('
        ' SELECT MIN(id) FROM attendance'
        ' GROUP BY student_id, class_id, date(check_in_time))'))
    conn.execute(sqlalchemy.text(
        'CREATE UNIQUE INDEX IF NOT EXISTS ix_attendance_unique_day'
        ' ON attendance(student_id, class_id, date(check_in_time))'))


def _seed_default_season(conn, inspector):
    """One-time seasons bootstrap: when the seasons table is empty, create an
    active 'Current Season' and adopt every existing class into it. Keeps the
    invariant that exactly one season is active and every class has a season,
    without changing anything user-visible (the studio can rename it from the
    Classes page). Idempotent: a non-empty seasons table means this already ran
    (or the studio made its own), so it never touches data again."""
    if 'seasons' not in inspector.get_table_names():
        return
    count = conn.execute(sqlalchemy.text('SELECT COUNT(*) FROM seasons')).scalar()
    if count:
        return
    conn.execute(sqlalchemy.text(
        "INSERT INTO seasons (name, status, created_at) "
        "VALUES ('Current Season', 'active', CURRENT_TIMESTAMP)"))
    season_id = conn.execute(sqlalchemy.text(
        "SELECT id FROM seasons WHERE status='active' ORDER BY id LIMIT 1")).scalar()
    conn.execute(sqlalchemy.text(
        'UPDATE classes SET season_id = :sid WHERE season_id IS NULL'), {'sid': season_id})


def run_migrations(db):
    with db.engine.connect() as conn:
        inspector = sqlalchemy.inspect(db.engine)
        _add_missing_columns(conn, inspector, 'students', STUDENT_COLUMNS)
        _add_missing_columns(conn, inspector, 'users', USER_COLUMNS)
        if 'users' in inspector.get_table_names():
            _reconcile_admin_role(conn)
        if 'transactions' in inspector.get_table_names():
            _add_missing_columns(conn, inspector, 'transactions', TRANSACTION_COLUMNS)
            _backfill_transaction_fund(conn)
        if 'pending_payments' in inspector.get_table_names():
            _add_missing_columns(conn, inspector, 'pending_payments', PENDING_PAYMENT_COLUMNS)
        if 'classes' in inspector.get_table_names():
            _add_missing_columns(conn, inspector, 'classes', CLASS_COLUMNS)
        if 'performances' in inspector.get_table_names():
            _add_missing_columns(conn, inspector, 'performances', PERFORMANCE_COLUMNS)
        if 'families' in inspector.get_table_names():
            _add_missing_columns(conn, inspector, 'families', FAMILY_COLUMNS)
        if 'registrations' in inspector.get_table_names():
            _add_missing_columns(conn, inspector, 'registrations', REGISTRATION_COLUMNS)
        if 'attendance' in inspector.get_table_names():
            _add_missing_columns(conn, inspector, 'attendance', ATTENDANCE_COLUMNS)
            _enforce_attendance_uniqueness(conn)
        _seed_default_season(conn, inspector)
        conn.commit()
