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
    ('tap_email_opt_out', 'BOOLEAN DEFAULT 0 NOT NULL'),
    ('tap_sms_opt_in', 'BOOLEAN DEFAULT 0 NOT NULL'),
    ('tap_sms_opt_in_at', 'DATETIME'),
    ('tap_sms_opt_in_by', 'INTEGER'),
    ('tap_sms_phone', 'VARCHAR(20)'),
    ('photo_data', 'TEXT'),
    ('photo_updated_at', 'DATETIME'),
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
    ('is_excused', 'BOOLEAN DEFAULT 0 NOT NULL'),
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
    """One attendance row per (student, class, day), enforced by a functional
    UNIQUE index. Once the index exists the DB rejects duplicates, so this does
    nothing on every later boot.

    The one time the index is created, any duplicate rows already present are
    resolved first: the earliest row per (student, class, day) is kept and
    every other row is COPIED to attendance_dupe_archive before it is deleted,
    so a later staff correction on a duplicate is never lost for good. The
    archive is plain rows, recoverable by hand."""
    has_index = conn.execute(sqlalchemy.text(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='ix_attendance_unique_day'")).first()
    if has_index:
        return
    extra = ('SELECT id FROM attendance WHERE id NOT IN ('
             ' SELECT MIN(id) FROM attendance'
             ' GROUP BY student_id, class_id, date(check_in_time))')
    dupes = conn.execute(sqlalchemy.text(f'SELECT COUNT(*) FROM ({extra})')).scalar()
    if dupes:
        conn.execute(sqlalchemy.text(
            'CREATE TABLE IF NOT EXISTS attendance_dupe_archive AS '
            'SELECT *, CURRENT_TIMESTAMP AS archived_at FROM attendance WHERE 0'))
        conn.execute(sqlalchemy.text(
            'INSERT INTO attendance_dupe_archive '
            f'SELECT *, CURRENT_TIMESTAMP FROM attendance WHERE id IN ({extra})'))
        conn.execute(sqlalchemy.text(f'DELETE FROM attendance WHERE id IN ({extra})'))
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


def _backfill_nfc_tags(conn):
    """Carry every legacy `students.rfid_uid` badge into `nfc_tags` as an
    active, token-less tag, so a dancer who already had a card keeps working
    on the USB reader. The legacy column is left untouched as history.
    Idempotent: a badge whose UID is already in nfc_tags is skipped."""
    from app.tap.credentials import legacy_uid
    rows = conn.execute(sqlalchemy.text(
        'SELECT id, rfid_uid, rfid_assigned_at, rfid_assigned_by FROM students '
        "WHERE rfid_uid IS NOT NULL AND rfid_uid != ''")).fetchall()
    for sid, raw, assigned_at, assigned_by in rows:
        uid = legacy_uid(raw)
        if not uid:
            continue
        taken = conn.execute(sqlalchemy.text('SELECT 1 FROM nfc_tags WHERE uid = :u'),
                             {'u': uid}).first()
        if taken:
            continue
        conn.execute(sqlalchemy.text(
            "INSERT INTO nfc_tags (student_id, uid, kind, status, locked, source, "
            "created_at, created_by, activated_at) VALUES (:s, :u, 'card', 'active', 0, "
            "'legacy', COALESCE(:at, CURRENT_TIMESTAMP), :by, COALESCE(:at, CURRENT_TIMESTAMP))"),
            {'s': sid, 'u': uid, 'at': assigned_at, 'by': assigned_by})
        conn.execute(sqlalchemy.text(
            "UPDATE nfc_tags SET label = printf('T-%04d', id) WHERE uid = :u AND label IS NULL"),
            {'u': uid})


def _move_applied_charges_to_allocations(conn, inspector):
    """Carry the old one-payment-one-charge links into payment_allocations.

    Before allocations, a payment named a single charge in
    transactions.applied_charge_id and paid it in full (the API refused an
    amount over the charge's remainder). Each link becomes one allocation for
    the payment's whole amount, then the old column is cleared so this never
    runs twice for the same payment and nothing reads two sources of truth.
    The column stays in the table (SQLite can't drop it cheaply); fresh
    databases never get it, hence the column check."""
    columns = {c['name'] for c in inspector.get_columns('transactions')}
    if 'applied_charge_id' not in columns:
        return
    conn.execute(sqlalchemy.text(
        "INSERT INTO payment_allocations (payment_id, charge_id, amount, created_at) "
        "SELECT p.id, p.applied_charge_id, p.amount, CURRENT_TIMESTAMP "
        "FROM transactions p JOIN transactions c ON c.id = p.applied_charge_id "
        "WHERE p.type = 'payment' AND c.type = 'charge' AND c.student_id = p.student_id "
        "AND NOT EXISTS (SELECT 1 FROM payment_allocations a WHERE a.payment_id = p.id)"))
    conn.execute(sqlalchemy.text(
        'UPDATE transactions SET applied_charge_id = NULL WHERE applied_charge_id IS NOT NULL'))


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
            if 'payment_allocations' in inspector.get_table_names():
                _move_applied_charges_to_allocations(conn, inspector)
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
        if 'nfc_tags' in inspector.get_table_names():
            _backfill_nfc_tags(conn)
        conn.commit()
