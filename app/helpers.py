"""Shared helpers for AttenDANCE — balance calculation, ledger building, serialization."""

from datetime import date

from sqlalchemy import func
from app import db
from app.models import Transaction


def build_aging(txns: list, as_of: date | None = None) -> dict:
    """Accounts-receivable aging for one entity from its transactions.

    Applies payments FIFO against the oldest charges (standard AR method), then
    buckets each charge's unpaid remainder by how old that charge is:
      current (0-30d), d31_60, d61_90, d90_plus.

    Expects txns for a single student/family. Order-independent (sorts here).
    Returns {'current','d31_60','d61_90','d90_plus','total'} as floats (rounded).
    """
    as_of = as_of or date.today()
    ordered = sorted(txns, key=lambda t: (t.transaction_date, t.created_at or t.transaction_date))
    charges = []  # oldest first: {'date', 'remaining'}
    payment_pool = 0.0
    for t in ordered:
        amt = float(t.amount)
        if t.type == 'charge':
            charges.append({'date': t.transaction_date, 'remaining': amt})
        else:
            payment_pool += amt

    # Pay down oldest charges first.
    for c in charges:
        if payment_pool <= 0:
            break
        pay = min(c['remaining'], payment_pool)
        c['remaining'] = round(c['remaining'] - pay, 2)
        payment_pool = round(payment_pool - pay, 2)

    buckets = {'current': 0.0, 'd31_60': 0.0, 'd61_90': 0.0, 'd90_plus': 0.0}
    for c in charges:
        rem = c['remaining']
        if rem <= 0:
            continue
        age = (as_of - c['date']).days
        if age <= 30:
            buckets['current'] += rem
        elif age <= 60:
            buckets['d31_60'] += rem
        elif age <= 90:
            buckets['d61_90'] += rem
        else:
            buckets['d90_plus'] += rem

    for k in buckets:
        buckets[k] = round(buckets[k], 2)
    buckets['total'] = round(sum(buckets.values()), 2)
    return buckets


def calc_balance(student_id: int) -> dict:
    """Calculate charges, payments, and balance for a student using SQL aggregation.

    Returns dict with keys: total_charges, total_payments, balance (all float).
    """
    rows = (
        db.session.query(
            Transaction.type,
            func.sum(Transaction.amount),
        )
        .filter_by(student_id=student_id)
        .group_by(Transaction.type)
        .all()
    )
    totals = {r[0]: float(r[1]) for r in rows}
    charges = totals.get('charge', 0.0)
    payments = totals.get('payment', 0.0)
    return {
        'total_charges': charges,
        'total_payments': payments,
        'balance': charges - payments,
    }


def calc_balance_bulk(student_ids: list[int]) -> dict[int, dict]:
    """Calculate balances for multiple students in one query.

    Returns {student_id: {total_charges, total_payments, balance}}.
    """
    if not student_ids:
        return {}
    rows = (
        db.session.query(
            Transaction.student_id,
            Transaction.type,
            func.sum(Transaction.amount),
        )
        .filter(Transaction.student_id.in_(student_ids))
        .group_by(Transaction.student_id, Transaction.type)
        .all()
    )
    result: dict[int, dict] = {}
    for sid, txn_type, total in rows:
        if sid not in result:
            result[sid] = {'total_charges': 0.0, 'total_payments': 0.0, 'balance': 0.0}
        if txn_type == 'charge':
            result[sid]['total_charges'] = float(total)
        else:
            result[sid]['total_payments'] = float(total)
    for sid in result:
        result[sid]['balance'] = result[sid]['total_charges'] - result[sid]['total_payments']
    # Fill in students with no transactions
    for sid in student_ids:
        if sid not in result:
            result[sid] = {'total_charges': 0.0, 'total_payments': 0.0, 'balance': 0.0}
    return result


def build_ledger(txns: list) -> dict:
    """Build a running-balance ledger from a list of Transaction objects.

    Expects txns already sorted by (transaction_date, created_at).
    Returns dict with keys: ledger (list), total_charges, total_payments, balance, by_category.
    """
    running = 0.0
    ledger = []
    total_charges = 0.0
    total_payments = 0.0
    cat_totals: dict[str, dict] = {}

    for t in txns:
        amt = float(t.amount)
        is_charge = t.type == 'charge'
        if is_charge:
            running += amt
            total_charges += amt
        else:
            running -= amt
            total_payments += amt

        # Per-category tracking
        cat = t.category
        if cat not in cat_totals:
            cat_totals[cat] = {'charges': 0.0, 'payments': 0.0}
        if is_charge:
            cat_totals[cat]['charges'] += amt
        else:
            cat_totals[cat]['payments'] += amt

        ledger.append({
            **transaction_to_dict(t),
            'running_balance': f'{running:.2f}',
        })

    by_category = {}
    for cat in sorted(cat_totals):
        c = cat_totals[cat]['charges']
        p = cat_totals[cat]['payments']
        by_category[cat] = {
            'charges': f'{c:.2f}',
            'payments': f'{p:.2f}',
            'balance': f'{c - p:.2f}',
        }

    return {
        'ledger': ledger,
        'total_charges': f'{total_charges:.2f}',
        'total_payments': f'{total_payments:.2f}',
        'balance': f'{total_charges - total_payments:.2f}',
        'by_category': by_category,
    }


def allocate_family_payment(student_ids: list[int], amount: float) -> list[tuple[int, float]]:
    """Split a lump family payment across children by outstanding balance.

    Children with the largest balances are paid down first; each is capped at
    its own balance. Any leftover (an overpayment) is appended to the first
    student as a credit so the full amount is always accounted for.

    Returns a list of (student_id, amount) tuples, amounts rounded to cents.
    """
    if not student_ids or amount <= 0:
        return []

    balances = calc_balance_bulk(student_ids)
    # Order by balance descending; only those who actually owe
    owing = sorted(
        ((sid, balances[sid]['balance']) for sid in student_ids if balances[sid]['balance'] > 0),
        key=lambda x: x[1], reverse=True,
    )

    remaining = round(amount, 2)
    allocations: list[tuple[int, float]] = []
    for sid, bal in owing:
        if remaining <= 0:
            break
        portion = round(min(bal, remaining), 2)
        if portion <= 0:
            continue
        allocations.append((sid, portion))
        remaining = round(remaining - portion, 2)

    # Leftover overpayment (or nobody owed) → credit the first student
    if remaining > 0:
        target = allocations[0][0] if allocations else student_ids[0]
        # Merge into existing allocation if present
        for i, (sid, amt) in enumerate(allocations):
            if sid == target:
                allocations[i] = (sid, round(amt + remaining, 2))
                break
        else:
            allocations.append((target, remaining))

    return allocations


# --- Serializers ---

def student_to_dict(student) -> dict:
    return {
        'id': student.id,
        'first_name': student.first_name,
        'last_name': student.last_name,
        'full_name': student.full_name,
        'email': student.email,
        'phone': student.phone,
        'date_of_birth': student.date_of_birth.isoformat() if student.date_of_birth else None,
        'age': student.age,
        'emergency_contact_name': student.emergency_contact_name,
        'emergency_contact_phone': student.emergency_contact_phone,
        'parent_email': student.parent_email,
        'parent_phone': student.parent_phone,
        'school': student.school,
        'grade': student.grade,
        'allergies': student.allergies,
        'special_needs': student.special_needs,
        'height': student.height,
        'weight': student.weight,
        'shoe_size': student.shoe_size,
        'shirt_size': student.shirt_size,
        'pants_size': student.pants_size,
        'leotard_size': student.leotard_size,
        'dress_size': student.dress_size,
        'waist': student.waist,
        'girth': student.girth,
        'inseam': student.inseam,
        'neck': student.neck,
        'tight_size': student.tight_size,
        'bust': student.bust,
        'hips': student.hips,
        'sleeve': student.sleeve,
        'chest': student.chest,
        'size_notes': student.size_notes,
        'family_id': student.family_id,
        'family_name': student.family.name if student.family else None,
        'rfid_uid': student.rfid_uid,
        'has_rfid': student.has_rfid(),
        'rfid_assigned_at': _utc_iso(student.rfid_assigned_at),  # UTC metadata -> 'Z' for correct local display
        'is_active': student.is_active,
        'enrollment_date': student.enrollment_date.isoformat(),
        'notes': student.notes,
        'medical_notes': student.medical_notes,
        'created_at': _utc_iso(student.created_at),
        'updated_at': _utc_iso(student.updated_at),
    }


def active_season():
    """The single active Season, or None (only possible before the seasons
    bootstrap migration has run)."""
    from app.models import Season
    return Season.query.filter_by(status='active').first()


def live_class_query():
    """DanceClass query restricted to the classes that are actually running:
    is_active AND in the active season.

    Every operational view (dashboards, check-in, dropdowns, public
    registration default) must use this instead of filter_by(is_active=True).
    A draft (future) season is a staging area — its classes are created
    is_active=True so they go live at activation, but they must not surface
    anywhere parents or teachers operate until then. season_id NULL rows
    predate the seasons feature and count as the active season."""
    from sqlalchemy import or_
    from app.models import DanceClass
    q = DanceClass.query.filter(DanceClass.is_active.is_(True))
    season = active_season()
    if season:
        return q.filter(or_(DanceClass.season_id.is_(None),
                            DanceClass.season_id == season.id))
    # No active season (pre-migration edge): only legacy NULL-season rows are
    # live — never leak draft/archived-season classes.
    return q.filter(DanceClass.season_id.is_(None))


def class_to_dict(dance_class) -> dict:
    return {
        'season_id': dance_class.season_id,
        'season_name': dance_class.season.name if dance_class.season else None,
        'id': dance_class.id,
        'name': dance_class.name,
        'description': dance_class.description,
        'location_id': dance_class.location_id,
        'location_name': dance_class.location.name if dance_class.location else None,
        'day_of_week': dance_class.day_of_week,
        'day_name': dance_class.day_name,
        'start_time': dance_class.start_time.strftime('%H:%M'),
        'end_time': dance_class.end_time.strftime('%H:%M'),
        'instructor_id': dance_class.instructor_id,
        'instructor_name': dance_class.instructor.full_name if dance_class.instructor else None,
        'max_students': dance_class.max_students,
        'enrolled_count': dance_class.enrolled_students_count,
        'level': dance_class.level,
        'age_group': dance_class.age_group,
        'is_active': dance_class.is_active,
        'created_at': _utc_iso(dance_class.created_at),
        'updated_at': _utc_iso(dance_class.updated_at),
    }


def _clean_str(value, maxlen=50_000):
    """Coerce a JSON value to a trimmed string; None/non-scalar -> ''. `maxlen`
    defaults to a generous 50 KB backstop against multi-MB storage abuse (SQLite
    ignores VARCHAR(n) limits); no legitimate field approaches it."""
    if value is None or isinstance(value, (list, dict)):
        return ''
    s = str(value).strip()
    return s[:maxlen] if maxlen else s


def _utc_iso(dt):
    """ISO-8601 for a naive UTC datetime, marked with 'Z' so the browser's
    new Date() converts it to local time instead of misreading it as local
    (which shifts a displayed time by the whole UTC offset)."""
    return (dt.isoformat() + 'Z') if dt else None


def _local_iso(dt):
    """ISO-8601 for a naive *studio-local* datetime (no 'Z'). Attendance
    check-in times are stored in the studio's timezone (the server runs there),
    so they must NOT be tagged 'Z' — that would make the browser treat the local
    wall-clock as UTC and shift it by the whole offset. Without an offset,
    new Date() parses it in the viewer's zone and shows the stored wall-clock
    time as-is (7:05 PM stays 7:05 PM)."""
    return dt.isoformat() if dt else None


def attendance_to_dict(attendance) -> dict:
    return {
        'id': attendance.id,
        'student_id': attendance.student_id,
        'student_name': attendance.student.full_name if attendance.student else None,
        'class_id': attendance.class_id,
        'class_name': attendance.dance_class.name if attendance.dance_class else None,
        # Attendance times are studio-local (see check-in handlers) — emit them
        # without a 'Z' so the log page renders the real wall-clock, not a shift.
        'check_in_time': _local_iso(attendance.check_in_time),
        'check_out_time': _local_iso(attendance.check_out_time),
        'check_in_method': attendance.check_in_method,
        'notes': attendance.notes,
        'is_present': attendance.is_present,
        'attendance_date': attendance.attendance_date.isoformat(),
        'duration': str(attendance.duration) if attendance.duration else None,
    }


def transaction_to_dict(t) -> dict:
    return {
        'id': t.id,
        'student_id': t.student_id,
        'student_name': t.student.full_name if t.student else None,
        'type': t.type,
        'amount': str(t.amount),
        'category': t.category,
        'payment_method': t.payment_method if t.payment_method != 'n/a' else None,
        'description': t.description,
        'transaction_date': t.transaction_date.isoformat(),
        'created_by': t.creator.full_name if t.creator else None,
        'created_at': _utc_iso(t.created_at),
    }


def recurring_to_dict(rc) -> dict:
    return {
        'id': rc.id,
        'class_id': rc.class_id,
        'class_name': rc.dance_class.name if rc.dance_class else None,
        'amount': str(rc.amount),
        'category': rc.category,
        'description': rc.description,
        'day_of_month': rc.day_of_month,
        'is_active': rc.is_active,
        'created_at': _utc_iso(rc.created_at),
    }


# Student fields that accept simple string-or-None values
STUDENT_STRING_FIELDS = [
    'first_name', 'last_name', 'email', 'phone',
    'emergency_contact_name', 'emergency_contact_phone', 'parent_email', 'parent_phone',
    'school', 'grade', 'allergies', 'special_needs',
    'height', 'weight', 'shoe_size', 'shirt_size', 'pants_size',
    'leotard_size', 'dress_size', 'waist', 'girth', 'inseam',
    'neck', 'tight_size', 'bust', 'hips', 'sleeve', 'chest',
    'size_notes', 'notes', 'medical_notes',
]


def apply_student_fields(student, data: dict) -> None:
    """Apply data dict fields to a Student instance. Coerces to trimmed strings;
    optional fields empty -> None, but the NOT NULL name fields are only updated to
    a non-empty value (never blanked), and a bad date_of_birth is ignored."""
    from datetime import datetime

    required = {'first_name', 'last_name'}  # NOT NULL columns
    for field in STUDENT_STRING_FIELDS:
        if field in data:
            val = _clean_str(data[field])
            if field in required:
                if val:
                    setattr(student, field, val)
            else:
                setattr(student, field, val or None)

    if 'date_of_birth' in data:
        dob = data['date_of_birth']
        try:
            student.date_of_birth = datetime.strptime(dob, '%Y-%m-%d').date() if dob else None
        except (TypeError, ValueError):
            pass  # keep the existing DOB on a malformed value

    if 'family_id' in data:
        from app.models import Family
        raw = data['family_id']
        if not raw:
            student.family_id = None  # explicit un-assign
        else:
            try:
                fid = int(raw)
            except (TypeError, ValueError):
                fid = None
            # Only re-assign to a family that actually exists — never 500 on a bad
            # value or orphan the student to a nonexistent family id.
            if fid and Family.query.get(fid):
                student.family_id = fid

    if 'is_active' in data:
        student.is_active = bool(data['is_active'])
