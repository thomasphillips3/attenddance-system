"""REST API routes for AttenDANCE system."""

import logging
import os
import secrets
import tempfile
import threading
import time
from datetime import date, datetime, timedelta

from flask import current_app, jsonify, request, send_file, url_for
from flask_login import current_user, login_required
from sqlalchemy import desc, func, or_
from sqlalchemy.exc import IntegrityError
from werkzeug.utils import secure_filename

from app import db, square_service
from app.api import bp
from app.helpers import (
    active_season,
    allocate_family_payment,
    apply_student_fields,
    attendance_to_dict,
    build_aging,
    build_ledger,
    calc_balance,
    calc_balance_bulk,
    class_to_dict,
    live_class_query,
    recurring_to_dict,
    student_to_dict,
    transaction_to_dict,
)
from app.models import (
    Attendance,
    Audition,
    AuditionSignup,
    AuditLog,
    ClassEnrollment,
    CompanyMembership,
    Costume,
    CostumeAssignment,
    DanceClass,
    Donation,
    Family,
    Lead,
    Location,
    MakeupClass,
    Message,
    MessageAttachment,
    ParentStudent,
    PaymentPlan,
    PaymentPlanInstallment,
    PendingPayment,
    Performance,
    PerformanceAssignment,
    PerformanceGroup,
    Recital,
    RecitalAd,
    RecitalAward,
    RecitalCast,
    RecitalNumber,
    RecurringCharge,
    Registration,
    Rule,
    RuleAcknowledgment,
    Season,
    Setting,
    Skill,
    SquareInvoice,
    Student,
    StudentSkill,
    TicketOrder,
    TicketType,
    TimeClockEntry,
    Transaction,
    User,
    WaitlistEntry,
    WaiverSignature,
    WaiverTemplate,
)

try:
    from rfid.service import get_rfid_service
except ImportError:
    get_rfid_service = None

logger = logging.getLogger(__name__)


# ── Authorization helpers ───────────────────────────────────────────
# Staff = admin/teacher (User.is_staff). Parents may only touch students
# and families they are linked to via ParentStudent. These return a
# (json, status) error tuple to `return` on denial, else None.

def _staff_only():
    """Deny non-staff (parents). Return error tuple or None."""
    if not current_user.is_staff:
        return jsonify({'error': 'Staff access required'}), 403
    return None


def _child_ids(user) -> set:
    """Student ids a parent is linked to (empty for staff — staff use _staff_only)."""
    return {s.id for s in user.get_children()} if getattr(user, 'is_parent', False) else set()


def _require_student_access(student_id):
    """Allow staff, or a parent linked to this student. Error tuple or None.
    A non-numeric id can't belong to any parent, so treat it as not-authorized
    rather than letting int() raise a 500."""
    if current_user.is_staff:
        return None
    try:
        sid = int(student_id)
    except (TypeError, ValueError):
        return jsonify({'error': 'Not authorized for this student'}), 403
    if current_user.is_parent and sid in _child_ids(current_user):
        return None
    return jsonify({'error': 'Not authorized for this student'}), 403


def _require_family_access(family_id):
    """Allow staff, or a parent with at least one child in this family. A
    non-numeric id can't belong to any parent, so treat it as not-authorized."""
    if current_user.is_staff:
        return None
    try:
        fid = int(family_id)
    except (TypeError, ValueError):
        return jsonify({'error': 'Not authorized for this family'}), 403
    if current_user.is_parent:
        fam_ids = {s.family_id for s in current_user.get_children() if s.family_id}
        if fid in fam_ids:
            return None
    return jsonify({'error': 'Not authorized for this family'}), 403


def _require_student_money_access(student_id):
    """Billing variant of _require_student_access: ADMIN or a linked parent.
    Teachers are staff but must not read financial data (least privilege —
    Thomas, 2026-07-06)."""
    if current_user.is_admin:
        return None
    if current_user.is_staff:
        return jsonify({'error': 'Admin access required'}), 403
    return _require_student_access(student_id)


def _require_family_money_access(family_id):
    """Billing variant of _require_family_access: ADMIN or a linked parent."""
    if current_user.is_admin:
        return None
    if current_user.is_staff:
        return jsonify({'error': 'Admin access required'}), 403
    return _require_family_access(family_id)


def _valid_amount(raw):
    """Coerce a money amount. Returns (value, None) or (None, (json, status)).
    Rejects non-numeric, non-positive, and absurdly large values so a typo or
    bad payload can't silently corrupt balances (a negative charge is a credit)."""
    try:
        amt = round(float(raw), 2)
    except (TypeError, ValueError):
        return None, (jsonify({'error': 'A valid amount is required'}), 400)
    if amt <= 0:
        return None, (jsonify({'error': 'Amount must be greater than zero'}), 400)
    if amt > 1_000_000:
        return None, (jsonify({'error': 'Amount is unreasonably large'}), 400)
    return amt, None


def _utc_iso(dt):
    """ISO-8601 for a naive UTC datetime (datetime.utcnow), marked with a 'Z' so
    the browser's `new Date()` converts it to local time. Without the suffix, JS
    reads a naive datetime string as LOCAL, so a UTC time renders shifted by the
    whole UTC offset (e.g. a 5pm clock-in shown as 9pm)."""
    return (dt.isoformat() + 'Z') if dt else None


def _local_iso(dt):
    """ISO-8601 for a naive *studio-local* datetime (no 'Z') — for timestamps
    stored in the studio's timezone (time-clock punches, attendance). Emitting a
    'Z' would tag the local wall-clock as UTC and shift it by the whole offset."""
    return dt.isoformat() if dt else None


def _opt_int(raw):
    """Coerce an optional FK id to a positive int, or None if absent/un-parseable.
    For links whose render path already null-guards the relationship (group_id,
    class_id on costumes/recital numbers), so a garbage id drops the link instead
    of 500-ing the create request."""
    if not raw:
        return None
    v, err = _valid_id(raw)
    return v if not err else None


def _opt_num(raw, default, is_float=False):
    """Parse an optional numeric UPDATE field, keeping `default` (usually the
    current value) if it's garbage — so a non-numeric body can't 500 a
    get_or_404-then-int() update endpoint (the update-fuzz uses a nonexistent id
    and 404s before reaching the parse, so these were unguarded)."""
    try:
        return round(float(raw), 2) if is_float else int(raw)
    except (TypeError, ValueError):
        return default


def _resolve_student_id(raw, required=True):
    """Validate that a student id is a positive int referring to a real student.
    Returns (id_or_None, None) on success or (None, (json, status)) on error.
    Prevents the recurring bug where a create endpoint stores an unchecked
    student_id and orphans a row that then 500s its roster page. Pass
    required=False for endpoints where the student link is optional."""
    if raw in (None, '', 0):
        if required:
            return None, (jsonify({'error': 'student_id is required'}), 400)
        return None, None
    sid, err = _valid_id(raw)
    if err:
        return None, err
    if Student.query.get(sid) is None:
        return None, (jsonify({'error': 'student not found'}), 404)
    return sid, None


def _clean_str(value, maxlen=50_000):
    """Coerce a JSON value to a trimmed string. Scalars (str/int/float) stringify;
    None and non-scalars (list/dict) become '' so a `not name` guard still fires.
    Guards every `.strip()` on client input against a non-string blowing up (a
    JSON `name: 123` would otherwise raise AttributeError -> 500).

    `maxlen` truncates the result. It DEFAULTS to a generous 50 KB backstop —
    SQLite ignores VARCHAR(n) limits, so without a cap a client (esp. the public
    registration) could stuff a multi-MB string into any field and bloat the DB
    volume; no legitimate field (even a message-blast body) approaches 50 KB.
    High-risk fields pass a much tighter cap; pass maxlen=None to opt out."""
    if value is None or isinstance(value, (list, dict)):
        return ''
    s = str(value).strip()
    return s[:maxlen] if maxlen else s


def _valid_id(raw):
    """Coerce a JSON id to a positive int. Returns (int, None) or (None, (json, status)).
    Rejects non-numeric ('xyz'), non-positive (-1, 0), and null so a bad id can't
    500 the endpoint or create an orphan row that then breaks its list page."""
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return None, (jsonify({'error': 'A valid id is required'}), 400)
    if val <= 0:
        return None, (jsonify({'error': 'A valid id is required'}), 400)
    return val, None


def _parse_txn_date(raw):
    """Parse an optional YYYY-MM-DD date. Returns (date, None) or (None, (json, status))."""
    if not raw:
        return date.today(), None
    try:
        return datetime.strptime(raw, '%Y-%m-%d').date(), None
    except (TypeError, ValueError):
        return None, (jsonify({'error': 'Invalid date — use YYYY-MM-DD'}), 400)


# Endpoints a parent may invoke with a mutating method. EVERY other write is
# staff-only. This is default-deny / fail-closed: a newly added write endpoint
# is automatically parent-forbidden until explicitly allowlisted here. The
# allowlisted endpoints still perform their own per-student ownership checks.
# Public-registration volume caps (circuit breakers on the one
# unauthenticated write surface). Module-level so tests can patch them.
_MAX_PENDING_PER_EMAIL = 3
_MAX_PENDING_REGISTRATIONS = 500

_PARENT_WRITE_ALLOWED = {
    'api.api_logout',
    'api.claim_payment',
    'api.signup_for_audition',
    'api.sign_waiver',
    'api.create_ticket_order',
    'api.create_makeup',
    'api.create_donation',
    'api.acknowledge_rule',
    'api.request_data_deletion',
}


@bp.before_request
def _restrict_parent_writes():
    """Block parents from any mutating API call not on the allowlist."""
    if request.method in ('POST', 'PUT', 'DELETE', 'PATCH'):
        if current_user.is_authenticated and getattr(current_user, 'is_parent', False):
            if request.endpoint not in _PARENT_WRITE_ALLOWED:
                return jsonify({'error': 'Staff access required'}), 403
    return None


# ── Auth endpoints ──────────────────────────────────────────────────

@bp.route('/auth/login', methods=['POST'])
def api_login():
    return jsonify({'message': 'Use /auth/login endpoint'}), 400


@bp.route('/auth/logout', methods=['POST'])
@login_required
def api_logout():
    return jsonify({'message': 'Use /auth/logout endpoint'}), 400


@bp.route('/auth/me')
@login_required
def api_current_user():
    return jsonify({
        'id': current_user.id,
        'username': current_user.username,
        'email': current_user.email,
        'full_name': current_user.full_name,
        'first_name': current_user.first_name,
        'last_name': current_user.last_name,
        'is_admin': current_user.is_admin,
        'last_login': _utc_iso(current_user.last_login),
    })


# ── Student endpoints ───────────────────────────────────────────────

@bp.route('/students', methods=['GET'])
@login_required
def get_students():
    err = _staff_only()
    if err:
        return err
    page = request.args.get('page', 1, type=int)
    per_page = min(request.args.get('per_page', 20, type=int), 100)
    search = request.args.get('search', '').strip()
    active_only = request.args.get('active', 'true').lower() == 'true'

    query = Student.query
    if active_only:
        query = query.filter_by(is_active=True)
    if search:
        query = query.filter(
            Student.first_name.contains(search)
            | Student.last_name.contains(search)
            | Student.email.contains(search)
        )
    query = query.order_by(Student.last_name, Student.first_name)
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)

    return jsonify({
        'students': [student_to_dict(s) for s in pagination.items],
        'pagination': {
            'page': page,
            'pages': pagination.pages,
            'per_page': per_page,
            'total': pagination.total,
            'has_next': pagination.has_next,
            'has_prev': pagination.has_prev,
        },
    })


@bp.route('/students/<int:student_id>', methods=['GET'])
@login_required
def get_student(student_id):
    err = _require_student_access(student_id)
    if err:
        return err
    return jsonify(student_to_dict(Student.query.get_or_404(student_id)))


@bp.route('/students', methods=['POST'])
@login_required
def create_student():
    err = _admin_only()
    if err:
        return err
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400
    for field in ('first_name', 'last_name'):
        if not data.get(field):
            return jsonify({'error': f'{field} is required'}), 400

    if data.get('email'):
        if Student.query.filter_by(email=data['email']).first():
            return jsonify({'error': 'Email already exists'}), 400

    try:
        student = Student()
        apply_student_fields(student, data)
        db.session.add(student)
        db.session.commit()
        return jsonify(student_to_dict(student)), 201
    except Exception:
        db.session.rollback()
        logger.exception("Failed to create student")
        return jsonify({'error': 'An internal error occurred'}), 500


@bp.route('/students/<int:student_id>', methods=['PUT'])
@login_required
def update_student(student_id):
    err = _admin_only()
    if err:
        return err
    student = Student.query.get_or_404(student_id)
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    # Check email uniqueness if changing
    if 'email' in data:
        email = _clean_str(data['email']) or None
        if email and email != student.email:
            if Student.query.filter_by(email=email).first():
                return jsonify({'error': 'Email already exists'}), 400

    try:
        apply_student_fields(student, data)
        student.updated_at = datetime.utcnow()
        db.session.commit()
        return jsonify(student_to_dict(student))
    except Exception:
        db.session.rollback()
        logger.exception("Failed to update student %d", student_id)
        return jsonify({'error': 'An internal error occurred'}), 500


@bp.route('/students/<int:student_id>', methods=['DELETE'])
@login_required
def delete_student(student_id):
    err = _admin_only()
    if err:
        return err
    from app.models import WaitlistEntry
    student = Student.query.get_or_404(student_id)
    student.is_active = False
    student.updated_at = datetime.utcnow()
    # Withdrawing a student cascades to their class participation: deactivate
    # enrollments (else they linger on rosters AND keep occupying a capacity spot
    # a replacement can't take) and clear their waitlist entries. Financial records
    # (their balance, ledger, pending payments) are LEFT INTACT — a withdrawn
    # student may still owe, and that must stay collectible (see the A/R report).
    ClassEnrollment.query.filter_by(student_id=student_id, is_active=True).update(
        {'is_active': False}, synchronize_session=False)
    WaitlistEntry.query.filter_by(student_id=student_id, status='waiting').update(
        {'status': 'removed'}, synchronize_session=False)
    db.session.commit()
    return jsonify({'message': 'Student deactivated successfully'})


@bp.route('/students/<int:student_id>/assign-rfid', methods=['POST'])
@login_required
def assign_rfid(student_id):
    err = _staff_only()
    if err:
        return err
    student = Student.query.get_or_404(student_id)
    data = request.get_json()
    rfid_uid = _clean_str(data.get('rfid_uid')) if data else ''
    if not rfid_uid:
        return jsonify({'error': 'RFID UID is required'}), 400

    existing = Student.query.filter_by(rfid_uid=rfid_uid).first()
    if existing and existing.id != student_id:
        return jsonify({'error': 'RFID card is already assigned to another student'}), 400

    student.rfid_uid = rfid_uid
    student.rfid_assigned_at = datetime.utcnow()
    student.rfid_assigned_by = current_user.id
    student.updated_at = datetime.utcnow()
    db.session.commit()

    return jsonify({'message': 'RFID card assigned successfully', 'student': student_to_dict(student)})


@bp.route('/students/<int:student_id>/remove-rfid', methods=['POST'])
@login_required
def remove_rfid(student_id):
    err = _staff_only()
    if err:
        return err
    student = Student.query.get_or_404(student_id)
    student.rfid_uid = None
    student.rfid_assigned_at = None
    student.rfid_assigned_by = None
    student.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'message': 'RFID card removed successfully', 'student': student_to_dict(student)})


# ── Season endpoints ────────────────────────────────────────────────

def _season_to_dict(s):
    return {
        'id': s.id,
        'name': s.name,
        'status': s.status,
        'start_date': s.start_date.isoformat() if s.start_date else None,
        'end_date': s.end_date.isoformat() if s.end_date else None,
        'class_count': s.classes.filter_by(is_active=True).count(),
    }


def _parse_season_date(value, field):
    """(date-or-None, error-response-or-None) from a YYYY-MM-DD string. Empty
    clears the date."""
    val = _clean_str(value)
    if not val:
        return None, None
    try:
        return datetime.strptime(val, '%Y-%m-%d').date(), None
    except ValueError:
        return None, (jsonify({'error': f'invalid {field} (use YYYY-MM-DD)'}), 400)


@bp.route('/seasons', methods=['GET'])
@login_required
def list_seasons():
    err = _staff_only()
    if err:
        return err
    seasons = Season.query.order_by(Season.created_at).all()
    reg_sid = Setting.get('registration_season_id', '')
    return jsonify({
        'seasons': [_season_to_dict(s) for s in seasons],
        'registration_season_id': int(reg_sid) if reg_sid.isdigit() else None,
    })


@bp.route('/seasons', methods=['POST'])
@login_required
def create_season():
    """Create a DRAFT season — a staging area where the next session's schedule
    is built (classes, enrollments, billing config) while the current season
    keeps running untouched."""
    err = _admin_only()
    if err:
        return err
    data = request.get_json() or {}
    name = _clean_str(data.get('name'))
    if not name:
        return jsonify({'error': 'name is required'}), 400
    start_date, derr = _parse_season_date(data.get('start_date'), 'start_date')
    if derr:
        return derr
    end_date, derr = _parse_season_date(data.get('end_date'), 'end_date')
    if derr:
        return derr
    season = Season(name=name[:100], start_date=start_date, end_date=end_date, status='draft')
    db.session.add(season)
    AuditLog.record(current_user.id, 'season.create', f'Created draft season "{season.name}"')
    db.session.commit()
    return jsonify(_season_to_dict(season)), 201


@bp.route('/seasons/<int:season_id>', methods=['PUT', 'PATCH'])
@login_required
def update_season(season_id):
    err = _admin_only()
    if err:
        return err
    season = Season.query.get_or_404(season_id)
    data = request.get_json() or {}
    if data.get('name') is not None:
        name = _clean_str(data['name'])
        if not name:
            return jsonify({'error': 'name cannot be empty'}), 400
        season.name = name[:100]
    for fld in ('start_date', 'end_date'):
        if fld in data:
            d, derr = _parse_season_date(data.get(fld), fld)
            if derr:
                return derr
            setattr(season, fld, d)
    db.session.commit()
    return jsonify(_season_to_dict(season))


@bp.route('/seasons/<int:season_id>/activate', methods=['POST'])
@login_required
def activate_season(season_id):
    """Make a season THE live season. The previously active season is archived
    and its classes are wound down with the same cascade as Cancel Class
    (deactivate class + recurring charges + enrollments, clear waitlists) so
    nothing from the old season keeps billing or showing up. History
    (attendance, ledger) is untouched. NULL-season legacy classes count as the
    outgoing season and are wound down too."""
    err = _admin_only()
    if err:
        return err
    from app.models import WaitlistEntry
    season = Season.query.get_or_404(season_id)
    if season.status == 'active':
        return jsonify({'message': f'{season.name} is already the active season'})

    outgoing = Season.query.filter_by(status='active').all()
    outgoing_ids = [s.id for s in outgoing]
    # Classes being retired: the outgoing season's live classes, plus NULL-season
    # legacy rows. The incoming season can't be among outgoing_ids (we returned
    # above if it was already active), so its classes are never touched.
    season_clause = (or_(DanceClass.season_id.is_(None), DanceClass.season_id.in_(outgoing_ids))
                     if outgoing_ids else DanceClass.season_id.is_(None))
    retiring = DanceClass.query.filter(DanceClass.is_active.is_(True), season_clause).all()
    retiring_ids = [c.id for c in retiring]
    if retiring_ids:
        RecurringCharge.query.filter(
            RecurringCharge.class_id.in_(retiring_ids),
            RecurringCharge.is_active.is_(True),
        ).update({'is_active': False}, synchronize_session=False)
        ClassEnrollment.query.filter(
            ClassEnrollment.class_id.in_(retiring_ids),
            ClassEnrollment.is_active.is_(True),
        ).update({'is_active': False}, synchronize_session=False)
        WaitlistEntry.query.filter(
            WaitlistEntry.class_id.in_(retiring_ids),
            WaitlistEntry.status == 'waiting',
        ).update({'status': 'removed'}, synchronize_session=False)
        for c in retiring:
            c.is_active = False

    for s in outgoing:
        s.status = 'archived'
    season.status = 'active'
    # If public registration was aimed at this (draft) season, it's now just
    # the default; clear the override.
    if Setting.get('registration_season_id', '') == str(season.id):
        Setting.set('registration_season_id', '')
    AuditLog.record(current_user.id, 'season.activate',
                    f'Activated "{season.name}"; archived {len(outgoing)} season(s), '
                    f'wound down {len(retiring_ids)} class(es)')
    db.session.commit()
    return jsonify({'message': f'{season.name} is now the active season',
                    'classes_wound_down': len(retiring_ids)})


@bp.route('/seasons/<int:season_id>', methods=['DELETE'])
@login_required
def delete_season(season_id):
    """Delete a season — drafts only, and only while empty (mis-clicks). A
    season with classes is history and can only be archived via activation."""
    err = _admin_only()
    if err:
        return err
    season = Season.query.get_or_404(season_id)
    if season.status != 'draft':
        return jsonify({'error': 'Only draft seasons can be deleted'}), 400
    if season.classes.count():
        return jsonify({'error': 'Season has classes — move or cancel them first'}), 400
    if Setting.get('registration_season_id', '') == str(season.id):
        Setting.set('registration_season_id', '')
    db.session.delete(season)
    AuditLog.record(current_user.id, 'season.delete', f'Deleted empty draft season "{season.name}"')
    db.session.commit()
    return jsonify({'message': f'{season.name} deleted'})


# ── Class endpoints ─────────────────────────────────────────────────

@bp.route('/classes', methods=['GET'])
@login_required
def get_classes():
    err = _staff_only()  # full class list (instructors, rosters) is staff-only
    if err:
        return err
    active_only = request.args.get('active', 'true').lower() == 'true'
    season_param = request.args.get('season_id')
    if season_param:
        # Explicit season view (the Classes page's season picker) — shows that
        # season's classes whether it's draft, active, or archived.
        sid, serr = _valid_id(season_param)
        if serr:
            return serr
        Season.query.get_or_404(sid)
        query = DanceClass.query.filter_by(season_id=sid)
        if active_only:
            query = query.filter_by(is_active=True)
    elif active_only:
        # Default: only classes actually running (active season). Draft-season
        # classes must not leak into check-in dropdowns and rosters.
        query = live_class_query()
    else:
        query = DanceClass.query
    query = query.order_by(DanceClass.day_of_week, DanceClass.start_time)
    return jsonify({'classes': [class_to_dict(cls) for cls in query.all()]})


@bp.route('/instructors', methods=['GET'])
@login_required
def list_instructors():
    """Active teachers/admins (id + name only) for the class instructor picker.
    Staff-accessible — instructor names already appear on class cards, so this
    exposes nothing new (unlike /api/staff, which carries staff PII + is admin-only)."""
    err = _staff_only()
    if err:
        return err
    users = (User.query
             .filter(User.role.in_(['admin', 'teacher']), User.is_active.is_(True))
             .order_by(User.last_name, User.first_name).all())
    return jsonify({'instructors': [{'id': u.id, 'name': u.full_name} for u in users]})


@bp.route('/classes/<int:class_id>', methods=['GET'])
@login_required
def get_class(class_id):
    err = _staff_only()  # instructor name + class internals are staff-only (matches get_classes)
    if err:
        return err
    return jsonify(class_to_dict(DanceClass.query.get_or_404(class_id)))


@bp.route('/classes', methods=['POST'])
@login_required
def create_class():
    err = _admin_only()
    if err:
        return err
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400
    for field in ('name', 'day_of_week', 'start_time', 'end_time'):
        if field not in data:
            return jsonify({'error': f'{field} is required'}), 400

    # Validate the optional location + instructor references (default instructor
    # = the current user) so a bad id can't 500 or create a class whose bad
    # instructor_id then dead-pages the whole class list.
    location_id = None
    if data.get('location_id'):
        location_id, lerr = _valid_id(data.get('location_id'))
        if lerr:
            return lerr
        if Location.query.get(location_id) is None:
            return jsonify({'error': 'location not found'}), 404
    instructor_id, ierr = _valid_id(data.get('instructor_id', current_user.id))
    if ierr:
        return ierr
    if User.query.get(instructor_id) is None:
        return jsonify({'error': 'instructor not found'}), 404

    # Season: explicit id (the Classes page passes the season being viewed, so
    # "Add Class" while looking at a draft fall season lands there), defaulting
    # to the active season.
    if data.get('season_id'):
        season_id, snerr = _valid_id(data.get('season_id'))
        if snerr:
            return snerr
        if Season.query.get(season_id) is None:
            return jsonify({'error': 'season not found'}), 404
    else:
        season = active_season()
        season_id = season.id if season else None

    try:
        dance_class = DanceClass(
            name=_clean_str(data['name']),
            description=_clean_str(data.get('description')) or None,
            location_id=location_id,
            season_id=season_id,
            day_of_week=int(data['day_of_week']),
            start_time=datetime.strptime(data['start_time'], '%H:%M').time(),
            end_time=datetime.strptime(data['end_time'], '%H:%M').time(),
            instructor_id=instructor_id,
            max_students=data.get('max_students', 20),
            level=_clean_str(data.get('level')) or None,
            age_group=_clean_str(data.get('age_group')) or None,
        )
        db.session.add(dance_class)
        db.session.commit()
        return jsonify(class_to_dict(dance_class)), 201
    except Exception:
        db.session.rollback()
        logger.exception("Failed to create class")
        return jsonify({'error': 'An internal error occurred'}), 500


@bp.route('/classes/<int:class_id>', methods=['PUT', 'PATCH'])
@login_required
def update_class(class_id):
    """Edit a class — the studio adjusts schedules/instructors/capacity mid-season."""
    err = _admin_only()
    if err:
        return err
    dc = DanceClass.query.get_or_404(class_id)
    data = request.get_json() or {}
    if data.get('name') is not None:
        name = _clean_str(data['name'])
        if not name:
            return jsonify({'error': 'name cannot be empty'}), 400
        dc.name = name
    if 'description' in data:
        dc.description = _clean_str(data.get('description')) or None
    if data.get('location_id'):
        loc_id, lerr = _valid_id(data.get('location_id'))
        if lerr:
            return lerr
        if Location.query.get(loc_id) is None:
            return jsonify({'error': 'location not found'}), 404
        dc.location_id = loc_id
    if data.get('instructor_id'):
        inst_id, ierr = _valid_id(data.get('instructor_id'))
        if ierr:
            return ierr
        if User.query.get(inst_id) is None:
            return jsonify({'error': 'instructor not found'}), 404
        dc.instructor_id = inst_id
    if data.get('day_of_week') is not None:
        try:
            dc.day_of_week = int(data['day_of_week'])
        except (TypeError, ValueError):
            return jsonify({'error': 'invalid day_of_week'}), 400
    for fld in ('start_time', 'end_time'):
        if data.get(fld):
            try:
                setattr(dc, fld, datetime.strptime(data[fld], '%H:%M').time())
            except (TypeError, ValueError):
                return jsonify({'error': f'invalid {fld} (use HH:MM)'}), 400
    if data.get('max_students') is not None:
        try:
            dc.max_students = int(data['max_students'])
        except (TypeError, ValueError):
            return jsonify({'error': 'invalid max_students'}), 400
    if 'level' in data:
        dc.level = _clean_str(data.get('level')) or None
    if 'age_group' in data:
        dc.age_group = _clean_str(data.get('age_group')) or None
    if data.get('season_id'):
        # Move a class between seasons (e.g. created in summer by mistake,
        # belongs to the fall draft).
        sn_id, snerr = _valid_id(data.get('season_id'))
        if snerr:
            return snerr
        if Season.query.get(sn_id) is None:
            return jsonify({'error': 'season not found'}), 404
        dc.season_id = sn_id
    try:
        db.session.commit()
        return jsonify(class_to_dict(dc))
    except Exception:
        db.session.rollback()
        logger.exception("Failed to update class %d", class_id)
        return jsonify({'error': 'An internal error occurred'}), 500


@bp.route('/classes/<int:class_id>', methods=['DELETE'])
@login_required
def deactivate_class(class_id):
    """Cancel a class (soft-delete). Cascades so the cancellation is complete:
    - deactivate its recurring charges (else auto-billing keeps charging families
      for a class that no longer runs);
    - deactivate its enrollments (else the cancelled class lingers in every
      student's enrolled-classes list, which filters by active enrollment);
    - clear its waitlist (else those families are stuck waiting for a dead class).
    Past attendance + ledger charges are left intact (history)."""
    err = _admin_only()
    if err:
        return err
    from app.models import RecurringCharge, WaitlistEntry
    dc = DanceClass.query.get_or_404(class_id)
    dc.is_active = False
    stopped = RecurringCharge.query.filter_by(class_id=class_id, is_active=True).update(
        {'is_active': False}, synchronize_session=False)
    ClassEnrollment.query.filter_by(class_id=class_id, is_active=True).update(
        {'is_active': False}, synchronize_session=False)
    WaitlistEntry.query.filter_by(class_id=class_id, status='waiting').update(
        {'status': 'removed'}, synchronize_session=False)
    AuditLog.record(current_user.id, 'class.deactivate',
                    f'Cancelled class "{dc.name}" ({stopped} recurring charge(s) stopped)')
    db.session.commit()
    return jsonify({'message': f'{dc.name} cancelled', 'recurring_charges_stopped': stopped})


# ── Enrollment endpoints ────────────────────────────────────────────

@bp.route('/classes/<int:class_id>/enrollments', methods=['GET'])
@login_required
def get_class_enrollments(class_id):
    err = _staff_only()
    if err:
        return err
    dance_class = DanceClass.query.get_or_404(class_id)
    enrollments = (
        ClassEnrollment.query
        .filter_by(class_id=class_id, is_active=True)
        .all()
    )
    students = []
    for e in enrollments:
        s = e.student
        if s:
            students.append({
                'enrollment_id': e.id,
                'student_id': s.id,
                'full_name': s.full_name,
                'email': s.email,
                'has_rfid': s.has_rfid(),
                'enrolled_date': e.enrolled_date.isoformat(),
            })
    return jsonify({'enrollments': students, 'class_name': dance_class.name})


@bp.route('/classes/<int:class_id>/enroll', methods=['POST'])
@login_required
def enroll_student(class_id):
    err = _admin_only()
    if err:
        return err
    dance_class = DanceClass.query.get_or_404(class_id)
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    student_ids = data.get('student_ids')
    if not student_ids and data.get('student_id') is not None:
        student_ids = [data.get('student_id')]
    if not student_ids:
        return jsonify({'error': 'student_id or student_ids is required'}), 400
    if not isinstance(student_ids, list):  # tolerate a scalar; a non-list would TypeError the loop
        student_ids = [student_ids]

    # Enforce class capacity so a class can't be silently overbooked (the waitlist
    # exists for exactly this). Enroll up to the cap and report who couldn't fit;
    # the admin can then raise max_students (classes are editable) or waitlist them.
    # max_students of 0/None means no limit.
    capacity = dance_class.max_students or 0
    active_count = ClassEnrollment.query.filter_by(class_id=class_id, is_active=True).count()

    enrolled = []
    skipped = []
    full = []
    for raw in student_ids:
        sid_val, id_err = _valid_id(raw)
        if id_err:
            continue  # skip un-parseable ids rather than 500 the whole batch
        student = Student.query.get(sid_val)
        if not student:
            continue
        existing = ClassEnrollment.query.filter_by(
            student_id=student.id, class_id=class_id
        ).first()
        if existing and existing.is_active:
            skipped.append(student.full_name)
            continue
        if capacity and active_count >= capacity:
            full.append(student.full_name)
            continue
        if existing:
            existing.is_active = True
            existing.enrolled_date = date.today()
        else:
            db.session.add(ClassEnrollment(student_id=student.id, class_id=class_id))
        active_count += 1
        enrolled.append(student.full_name)

    db.session.commit()
    msg = f'{len(enrolled)} student(s) enrolled in {dance_class.name}'
    if skipped:
        msg += f' ({len(skipped)} already enrolled)'
    if full:
        msg += f' — {len(full)} not enrolled: class is full ({capacity} max). Raise the capacity or use the waitlist.'
    return jsonify({'message': msg, 'enrolled': enrolled, 'skipped': skipped, 'full': full}), 201


@bp.route('/enrollments/<int:enrollment_id>', methods=['DELETE'])
@login_required
def unenroll_student(enrollment_id):
    err = _admin_only()
    if err:
        return err
    enrollment = ClassEnrollment.query.get_or_404(enrollment_id)
    enrollment.is_active = False
    db.session.commit()
    return jsonify({'message': 'Student unenrolled successfully'})


# ── Global search (staff topbar) ────────────────────────────────────

@bp.route('/search', methods=['GET'])
@login_required
def global_search():
    """Staff topbar search across active students, families, and classes.

    Names are matched case-insensitively; each result links to that entity's
    page. Staff-only — a parent must not be able to enumerate the roster.
    """
    if not current_user.is_staff:
        return jsonify({'error': 'Staff access required'}), 403
    q = _clean_str(request.args.get('q'))
    if len(q) < 2:
        return jsonify({'students': [], 'families': [], 'classes': []})
    like = f'%{q}%'
    students = Student.query.filter(
        Student.is_active.is_(True),
        or_(
            Student.first_name.ilike(like),
            Student.last_name.ilike(like),
            (Student.first_name + ' ' + Student.last_name).ilike(like),
        ),
    ).order_by(Student.first_name, Student.last_name).limit(8).all()
    families = Family.query.filter(Family.name.ilike(like)).order_by(Family.name).limit(8).all()
    classes = live_class_query().filter(
        DanceClass.name.ilike(like)
    ).order_by(DanceClass.name).limit(8).all()
    return jsonify({
        'students': [{'id': s.id, 'name': s.full_name, 'url': f'/students/{s.id}/detail'}
                     for s in students],
        # Family results link to the ledger for admins; the ledger page is
        # admin-only, so teachers land on the families card list instead.
        'families': [{'id': f.id, 'name': f.name,
                      'url': f'/families/{f.id}/ledger' if current_user.is_admin else '/families'}
                     for f in families],
        'classes': [{'id': c.id, 'name': c.name, 'url': '/classes'} for c in classes],
    })


# ── Attendance endpoints ────────────────────────────────────────────

@bp.route('/attendance/toggle', methods=['POST'])
@login_required
def toggle_attendance():
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400
    student_id, serr = _valid_id(data.get('student_id'))
    if serr:
        return serr
    class_id, cerr = _valid_id(data.get('class_id'))
    if cerr:
        return cerr
    # Validate the student and class exist (manual_checkin does this too) so a
    # bad id can't create an orphan attendance row.
    Student.query.get_or_404(student_id)
    DanceClass.query.get_or_404(class_id)

    target_date = _parse_date(data.get('date')) or date.today()

    existing = Attendance.query.filter(
        Attendance.student_id == student_id,
        Attendance.class_id == class_id,
        func.date(Attendance.check_in_time) == target_date,
    ).first()

    if existing:
        db.session.delete(existing)
        db.session.commit()
        return jsonify({'present': False, 'message': 'Attendance removed'})

    att = Attendance(
        student_id=student_id,
        class_id=class_id,
        check_in_time=datetime.combine(target_date, datetime.now().time()),
        check_in_method='manual',
        is_present=True,
    )
    db.session.add(att)
    try:
        db.session.commit()
    except IntegrityError:
        # A concurrent double-tap raced us to the unique (student, class, day)
        # index. The other request already marked them present — treat this as a
        # no-op success rather than a 500 or a duplicate row.
        db.session.rollback()
        return jsonify({'present': True, 'message': 'Already marked present'})
    return jsonify({'present': True, 'message': 'Marked present'}), 201


@bp.route('/attendance', methods=['GET'])
@login_required
def get_attendance():
    err = _staff_only()
    if err:
        return err
    page = request.args.get('page', 1, type=int)
    per_page = min(request.args.get('per_page', 50, type=int), 100)
    # _parse_date returns None on a garbage/absent value, so a bad ?date_from=abc
    # skips the filter instead of 500-ing on an uncaught strptime ValueError.
    date_from = _parse_date(request.args.get('date_from'))
    date_to = _parse_date(request.args.get('date_to'))
    class_id = request.args.get('class_id', type=int)
    student_id = request.args.get('student_id', type=int)

    query = Attendance.query
    if date_from:
        query = query.filter(func.date(Attendance.check_in_time) >= date_from)
    if date_to:
        query = query.filter(func.date(Attendance.check_in_time) <= date_to)
    if class_id:
        query = query.filter_by(class_id=class_id)
    if student_id:
        query = query.filter_by(student_id=student_id)
    query = query.order_by(desc(Attendance.check_in_time))
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)

    return jsonify({
        'attendance': [attendance_to_dict(att) for att in pagination.items],
        'pagination': {
            'page': page,
            'pages': pagination.pages,
            'per_page': per_page,
            'total': pagination.total,
            'has_next': pagination.has_next,
            'has_prev': pagination.has_prev,
        },
    })


@bp.route('/attendance/today', methods=['GET'])
@login_required
def get_todays_attendance():
    err = _staff_only()
    if err:
        return err
    today = date.today()
    class_id = request.args.get('class_id', type=int)
    query = Attendance.query.filter(func.date(Attendance.check_in_time) == today)
    if class_id:
        query = query.filter_by(class_id=class_id)
    records = query.order_by(desc(Attendance.check_in_time)).all()
    return jsonify({
        'date': today.isoformat(),
        'attendance': [attendance_to_dict(att) for att in records],
        'count': len(records),
    })


@bp.route('/attendance/checkin', methods=['POST'])
@login_required
def manual_checkin():
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    student_id = data.get('student_id')
    class_id = data.get('class_id')
    if not student_id or not class_id:
        return jsonify({'error': 'student_id and class_id are required'}), 400

    student = Student.query.get_or_404(student_id)
    DanceClass.query.get_or_404(class_id)

    today = date.today()
    existing = Attendance.query.filter(
        Attendance.student_id == student_id,
        Attendance.class_id == class_id,
        func.date(Attendance.check_in_time) == today,
    ).first()
    if existing:
        return jsonify({'error': 'Student already checked in today'}), 400

    try:
        att = Attendance(
            student_id=student_id,
            class_id=class_id,
            # Local time (the server runs in the studio's timezone). Must match the
            # `date.today()` used above and everywhere else the app groups by day —
            # `datetime.utcnow()` would date an evening check-in on the next UTC day,
            # hiding it from today's roster and tripping the unique-day index. Same
            # basis as toggle_attendance.
            check_in_time=datetime.now(),
            check_in_method='manual',
            notes=_clean_str(data.get('notes')) or None,
            is_present=True,
        )
        db.session.add(att)
        db.session.commit()
        return jsonify({
            'message': f'{student.full_name} checked in successfully',
            'attendance': attendance_to_dict(att),
        }), 201
    except IntegrityError:
        # A concurrent double-tap raced past the pre-check to the unique
        # (student, class, day) index. They're already checked in — graceful
        # no-op, not a 500 (mirrors toggle_attendance).
        db.session.rollback()
        return jsonify({'error': 'Student already checked in today'}), 400
    except Exception:
        db.session.rollback()
        logger.exception("Failed to check in student %d", student_id)
        return jsonify({'error': 'An internal error occurred'}), 500


# ── RFID endpoints ──────────────────────────────────────────────────

@bp.route('/rfid/status', methods=['GET'])
@login_required
def rfid_status():
    err = _staff_only()
    if err:
        return err
    if not get_rfid_service:
        return jsonify({'service_running': False, 'message': 'RFID not available'})
    stats = get_rfid_service().get_stats()
    return jsonify({
        'service_running': stats['running'],
        'total_scans': stats['total_scans'],
        'successful_checkins': stats['successful_checkins'],
        'failed_scans': stats['failed_scans'],
        'last_scan_time': _utc_iso(stats['last_scan_time']),  # UTC -> 'Z' for correct local display
        'last_scan_uid': stats['last_scan_uid'],
    })


@bp.route('/rfid/simulate', methods=['POST'])
@login_required
def simulate_rfid_scan():
    if not current_user.is_admin:
        return jsonify({'error': 'Admin access required'}), 403
    data = request.get_json()
    uid = data.get('uid') if data else None
    if not uid:
        return jsonify({'error': 'UID is required'}), 400
    if not get_rfid_service:
        return jsonify({'error': 'RFID not available'}), 400
    success = get_rfid_service().simulate_scan(uid)
    return jsonify({'success': success, 'message': f'Simulated scan for UID: {uid}'})


@bp.route('/rfid/logs', methods=['GET'])
@login_required
def get_rfid_logs():
    err = _staff_only()
    if err:
        return err
    from app.models import RFIDLog

    page = request.args.get('page', 1, type=int)
    per_page = min(request.args.get('per_page', 50, type=int), 100)
    query = RFIDLog.query.order_by(desc(RFIDLog.scan_time))
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)

    logs = [{
        'id': log.id,
        'rfid_uid': log.rfid_uid,
        'student_id': log.student_id,
        'student_name': log.student.full_name if log.student else None,
        'scan_time': log.scan_time.isoformat(),
        'action_taken': log.action_taken,
        'success': log.success,
        'error_message': log.error_message,
    } for log in pagination.items]

    return jsonify({
        'logs': logs,
        'pagination': {
            'page': page,
            'pages': pagination.pages,
            'per_page': per_page,
            'total': pagination.total,
            'has_next': pagination.has_next,
            'has_prev': pagination.has_prev,
        },
    })


# ── Dashboard stats ─────────────────────────────────────────────────

@bp.route('/dashboard/stats', methods=['GET'])
@login_required
def dashboard_stats():
    err = _staff_only()
    if err:
        return err
    from app.models import RFIDLog

    today = date.today()
    total_students = Student.query.filter_by(is_active=True).count()
    total_classes = live_class_query().count()
    todays_attendance = Attendance.query.filter(
        func.date(Attendance.check_in_time) == today
    ).count()
    week_start = today - timedelta(days=today.weekday())
    week_attendance = Attendance.query.filter(
        func.date(Attendance.check_in_time) >= week_start
    ).count()
    recent_rfid_logs = RFIDLog.query.filter(
        # Local: scan_time is stored studio-local, so the cutoff must be too.
        RFIDLog.scan_time >= datetime.now() - timedelta(days=1)
    ).count()

    return jsonify({
        'total_students': total_students,
        'total_classes': total_classes,
        'todays_attendance': todays_attendance,
        'week_attendance': week_attendance,
        'recent_rfid_activity': recent_rfid_logs,
        'date': today.isoformat(),
    })


# ── Transaction endpoints ───────────────────────────────────────────
# Billing is ADMIN-only (Thomas, 2026-07-06): teachers take attendance and see
# rosters, but must not read the studio ledger or post charges/payments. Parents
# keep their own-child financial views (ledger, plans, claims) via the
# owner-checks inside those endpoints.

@bp.route('/transactions', methods=['GET'])
@login_required
def get_transactions():
    err = _admin_only()
    if err:
        return err
    page = request.args.get('page', 1, type=int)
    per_page = min(request.args.get('per_page', 50, type=int), 100)
    student_id = request.args.get('student_id', type=int)
    category = request.args.get('category', '').strip()

    query = Transaction.query
    if student_id:
        query = query.filter_by(student_id=student_id)
    if category:
        query = query.filter_by(category=category)
    query = query.order_by(desc(Transaction.transaction_date), desc(Transaction.created_at))
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)

    return jsonify({
        'transactions': [transaction_to_dict(t) for t in pagination.items],
        'pagination': {
            'page': page,
            'pages': pagination.pages,
            'per_page': per_page,
            'total': pagination.total,
        },
    })


@bp.route('/transactions', methods=['POST'])
@login_required
def create_transaction():
    err = _admin_only()
    if err:
        return err
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400
    txn_type = data.get('type', 'payment')
    if txn_type not in ('charge', 'payment'):
        return jsonify({'error': "type must be 'charge' or 'payment'"}), 400
    for field in ('student_id', 'category'):
        if not data.get(field):
            return jsonify({'error': f'{field} is required'}), 400
    if txn_type == 'payment' and not data.get('payment_method'):
        return jsonify({'error': 'payment_method is required for payments'}), 400

    amount, err = _valid_amount(data.get('amount'))
    if err:
        return err
    txn_date, err = _parse_txn_date(data.get('transaction_date'))
    if err:
        return err

    student = Student.query.get(data['student_id'])
    if not student:
        return jsonify({'error': 'Student not found'}), 404

    try:
        t = Transaction(
            student_id=student.id,
            type=txn_type,
            amount=amount,
            category=data['category'],
            payment_method=data.get('payment_method') or 'n/a',
            description=_clean_str(data.get('description')) or None,
            transaction_date=txn_date,
            created_by=current_user.id,
        )
        db.session.add(t)
        # Log money creation to the audit trail, same as deletion/confirmation —
        # otherwise the trail shows who *removed* a charge but not who *posted* one
        # (matters since staff, not only admins, can post charges).
        AuditLog.record(current_user.id, 'transaction.create',
                        f'{txn_type} ${float(amount):.2f} {data["category"]} for {student.full_name}')
        db.session.commit()
        return jsonify(transaction_to_dict(t)), 201
    except Exception:
        db.session.rollback()
        logger.exception("Failed to create transaction")
        return jsonify({'error': 'An internal error occurred'}), 500


@bp.route('/balances', methods=['GET'])
@login_required
def get_balances():
    """Balance summary for all active students — single SQL aggregate."""
    err = _admin_only()
    if err:
        return err
    students = Student.query.filter_by(is_active=True).order_by(Student.last_name, Student.first_name).all()
    # Include WITHDRAWN students who still owe so their debt stays visible in the
    # main money view — a deactivated student's balance shouldn't disappear until
    # it's settled. Only those with a positive balance (long-settled withdrawals
    # don't clutter the roster).
    inactive = Student.query.filter_by(is_active=False).order_by(Student.last_name, Student.first_name).all()
    inactive_owing = []
    if inactive:
        inactive_bals = calc_balance_bulk([s.id for s in inactive])
        inactive_owing = [s for s in inactive
                          if inactive_bals.get(s.id, {}).get('balance', 0) > 0]
    students = students + inactive_owing
    student_ids = [s.id for s in students]
    balances_map = calc_balance_bulk(student_ids)

    balances = []
    for s in students:
        bal = balances_map[s.id]
        balances.append({
            'student_id': s.id,
            'student_name': s.full_name,
            'withdrawn': not s.is_active,
            'total_charges': f'{bal["total_charges"]:.2f}',
            'total_payments': f'{bal["total_payments"]:.2f}',
            'balance': f'{bal["balance"]:.2f}',
        })
    return jsonify({'balances': balances})


def _compute_aging():
    """Shared A/R aging computation for both the JSON report and the CSV export
    so the two can never drift. Returns (rows, totals, as_of): per-student unpaid
    balance bucketed by how overdue each charge is (0-30 / 31-60 / 61-90 / 90+),
    owe-most first, amounts as 2dp strings."""
    students = Student.query.filter_by(is_active=True).all()
    # Include WITHDRAWN students who still owe: a deactivated student's unpaid
    # balance must stay visible so the studio can collect it, not silently vanish
    # from A/R. Only pull inactive students who actually have a balance (one bulk
    # query) so long-settled withdrawals don't bloat the report.
    inactive = Student.query.filter_by(is_active=False).all()
    if inactive:
        inactive_bals = calc_balance_bulk([s.id for s in inactive])
        students += [s for s in inactive
                     if inactive_bals.get(s.id, {}).get('balance', 0) > 0]
    sid_ids = [s.id for s in students]
    # One query for all transactions, grouped in memory (hundreds of rows).
    txns_by_student: dict[int, list] = {sid: [] for sid in sid_ids}
    if sid_ids:
        for t in Transaction.query.filter(Transaction.student_id.in_(sid_ids)).all():
            txns_by_student[t.student_id].append(t)

    rows = []
    totals = {'current': 0.0, 'd31_60': 0.0, 'd61_90': 0.0, 'd90_plus': 0.0, 'total': 0.0}
    for s in students:
        ag = build_aging(txns_by_student[s.id])
        if ag['total'] <= 0:
            continue  # only show entities that actually owe
        for k in totals:
            totals[k] = round(totals[k] + ag[k], 2)
        rows.append({
            'student_id': s.id,
            'student_name': s.full_name,
            'family_name': s.family.name if s.family else None,
            'withdrawn': not s.is_active,
            **{k: f'{ag[k]:.2f}' for k in ('current', 'd31_60', 'd61_90', 'd90_plus', 'total')},
        })
    # Owe-most first.
    rows.sort(key=lambda r: float(r['total']), reverse=True)
    return rows, totals, date.today().isoformat()


@bp.route('/reports/aging', methods=['GET'])
@login_required
def aging_report():
    """Accounts-receivable aging (JSON). Admin-only — it exposes every family's
    debt, and the nav gates it behind is_admin."""
    err = _admin_only()
    if err:
        return err
    rows, totals, as_of = _compute_aging()
    return jsonify({
        'rows': rows,
        'totals': {k: f'{totals[k]:.2f}' for k in totals},
        'as_of': as_of,
        'count': len(rows),
    })


def _csv_safe(value):
    """Neutralize CSV/formula injection. Excel/Sheets execute a cell that starts
    with = + @ (or a control char) as a formula — and some of this data (student
    names) comes from public registration — so prefix those with a single quote to
    force text. A leading '-' is only escaped when the cell isn't a plain number,
    so legitimate negative amounts (-50.00) still render as numbers."""
    import re as _re
    s = '' if value is None else str(value)
    if not s:
        return s
    if s[0] in ('=', '+', '@', '\t', '\r') or (s[0] == '-' and not _re.fullmatch(r'-?\d+(\.\d+)?', s)):
        return "'" + s
    return s


def _csv_response(filename, header, rows):
    """Build a downloadable CSV Response from a header + iterable of row lists."""
    import csv
    import io

    from flask import Response
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    for r in rows:
        writer.writerow([_csv_safe(cell) for cell in r])
    return Response(
        buf.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'},
    )


@bp.route('/reports/students.csv', methods=['GET'])
@login_required
def export_students_csv():
    """Roster export — for the accountant, mail-merge, or an owner-held backup.
    The Balance column is admin-only (billing); a teacher's export is the plain
    roster (class lists, allergy sheets)."""
    err = _staff_only()
    if err:
        return err
    students = Student.query.filter_by(is_active=True).order_by(
        Student.last_name, Student.first_name).all()
    header = ['Last name', 'First name', 'Family', 'Date of birth', 'Age',
              'Parent email', 'Parent phone', 'Emergency contact', 'Emergency phone',
              'Allergies', 'Special needs']
    bals = None
    if current_user.is_admin:
        header = header + ['Balance']
        bals = calc_balance_bulk([s.id for s in students])
    rows = ([
        s.last_name, s.first_name, s.family.name if s.family else '',
        s.date_of_birth.isoformat() if s.date_of_birth else '', s.age if s.age is not None else '',
        s.parent_email or '', s.parent_phone or '',
        s.emergency_contact_name or '', s.emergency_contact_phone or '',
        s.allergies or '', s.special_needs or '',
    ] + ([f"{bals[s.id]['balance']:.2f}"] if bals is not None else [])
        for s in students)
    return _csv_response(f'students-{date.today().isoformat()}.csv', header, rows)


@bp.route('/reports/transactions.csv', methods=['GET'])
@login_required
def export_transactions_csv():
    """Transaction ledger export for bookkeeping/taxes. Optional ?start=&end= (YYYY-MM-DD)."""
    err = _admin_only()
    if err:
        return err
    q = Transaction.query
    for param, op in (('start', '>='), ('end', '<=')):
        val = request.args.get(param)
        if val:
            try:
                d = datetime.strptime(val, '%Y-%m-%d').date()
                q = q.filter(Transaction.transaction_date >= d if op == '>='
                             else Transaction.transaction_date <= d)
            except ValueError:
                pass
    txns = q.order_by(Transaction.transaction_date, Transaction.created_at).all()
    header = ['Date', 'Student', 'Type', 'Category', 'Amount', 'Method', 'Description']
    rows = ([
        t.transaction_date.isoformat(), t.student.full_name if t.student else '',
        t.type, t.category, f"{float(t.amount):.2f}",
        t.payment_method if t.payment_method and t.payment_method != 'n/a' else '',
        t.description or '',
    ] for t in txns)
    return _csv_response(f'transactions-{date.today().isoformat()}.csv', header, rows)


@bp.route('/reports/aging.csv', methods=['GET'])
@login_required
def export_aging_csv():
    """A/R aging export so the owner can work accounts offline or hand it to the
    accountant. Admin-only — same debt exposure as the on-screen aging report."""
    err = _admin_only()
    if err:
        return err
    rows, totals, as_of = _compute_aging()
    header = ['Student', 'Family', 'Status', 'Current (0-30)', '31-60', '61-90', '90+', 'Total']
    body = [[
        r['student_name'], r['family_name'] or '',
        'Withdrawn' if r['withdrawn'] else 'Active',
        r['current'], r['d31_60'], r['d61_90'], r['d90_plus'], r['total'],
    ] for r in rows]
    # Footer totals row so the export reconciles without re-summing by hand.
    body.append(['TOTAL', '', '',
                 f"{totals['current']:.2f}", f"{totals['d31_60']:.2f}",
                 f"{totals['d61_90']:.2f}", f"{totals['d90_plus']:.2f}",
                 f"{totals['total']:.2f}"])
    return _csv_response(f'aging-{as_of}.csv', header, body)


def _sqlite_db_path():
    """On-disk path of the file-backed SQLite DB, or None if the configured DB
    isn't a usable file (`:memory:` or a non-sqlite URL) and so can't be
    snapshotted."""
    from sqlalchemy.engine import make_url
    uri = current_app.config.get('SQLALCHEMY_DATABASE_URI', '')
    if not uri.startswith('sqlite'):
        return None
    db_path = make_url(uri).database
    if not db_path or db_path == ':memory:':
        return None
    return db_path


def _sqlite_snapshot_bytes(db_path):
    """A consistent point-in-time copy of the SQLite DB as a BytesIO, via
    SQLite's online backup API — a raw file copy could be torn mid-write. The
    studio DB is a few MB, so reading it fully into memory is fine; the temp
    file is always cleaned up."""
    import sqlite3
    from io import BytesIO
    tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp.close()
    src = dst = None
    try:
        src = sqlite3.connect(db_path)
        dst = sqlite3.connect(tmp.name)
        with dst:
            src.backup(dst)  # consistent point-in-time snapshot
        dst.close()
        dst = None
        with open(tmp.name, 'rb') as fh:
            return BytesIO(fh.read())
    finally:
        if src is not None:
            src.close()
        if dst is not None:
            dst.close()
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


def _backup_response(actor_user_id, source_label):
    """Shared body for the two backup routes: snapshot the DB, record a
    best-effort audit entry, and stream the file. `actor_user_id` is None for
    the unattended cron path (audit_logs.user_id is nullable)."""
    db_path = _sqlite_db_path()
    if not db_path:
        return jsonify({'error': 'Backup is only supported for a file-backed SQLite database'}), 400
    if not os.path.exists(db_path):
        return jsonify({'error': 'Database file not found'}), 404

    payload = _sqlite_snapshot_bytes(db_path)

    try:
        AuditLog.record(actor_user_id, 'backup_downloaded',
                        f'{source_label}: {payload.getbuffer().nbytes} bytes')
        db.session.commit()
    except Exception:  # audit is best-effort; never block the backup
        db.session.rollback()

    payload.seek(0)
    return send_file(payload, mimetype='application/x-sqlite3', as_attachment=True,
                     download_name=f'attendance-backup-{date.today().isoformat()}.db')


@bp.route('/admin/backup', methods=['GET'])
@login_required
def download_backup():
    """Download a complete, consistent snapshot of the database (admin only).

    The whole studio lives in one SQLite file on a Fly volume; this is the
    studio's disaster-recovery + data-portability safety net — an owner can pull
    a full backup anytime and store it off-Fly, or take their data if they ever
    leave. The nightly automated backup uses the token-protected sibling below."""
    err = _admin_only()
    if err:
        return err
    return _backup_response(current_user.id, 'admin')


def _month_buckets(n):
    """(label, start, end) for the last n calendar months, oldest first."""
    month_start = date.today().replace(day=1)
    out = []
    for i in range(n - 1, -1, -1):
        year = month_start.year + (month_start.month - 1 - i) // 12
        month = (month_start.month - 1 - i) % 12 + 1
        ms = date(year, month, 1)
        me = date(year + (month // 12), (month % 12) + 1, 1)
        out.append((ms.strftime('%b %y'), ms, me))
    return out


@bp.route('/reports/revenue', methods=['GET'])
@login_required
def revenue_report():
    """Money report for the owner: charged vs collected by month, collected by
    category this year, and headline totals. Admin-only — aggregate studio
    financials; the nav gates it behind is_admin."""
    err = _admin_only()
    if err:
        return err

    def _sum(type_, start=None, end=None):
        q = db.session.query(func.coalesce(func.sum(Transaction.amount), 0)).filter(
            Transaction.type == type_)
        if start is not None:
            q = q.filter(Transaction.transaction_date >= start)
        if end is not None:
            q = q.filter(Transaction.transaction_date < end)
        return float(q.scalar() or 0)

    monthly = [{
        'month': label,
        'charged': round(_sum('charge', ms, me), 2),
        'collected': round(_sum('payment', ms, me), 2),
    } for label, ms, me in _month_buckets(12)]

    year_start = date.today().replace(month=1, day=1)
    cat_rows = (db.session.query(Transaction.category, func.sum(Transaction.amount))
                .filter(Transaction.type == 'payment', Transaction.transaction_date >= year_start)
                .group_by(Transaction.category).all())
    by_category = sorted(
        [{'category': c or 'uncategorized', 'amount': round(float(a or 0), 2)} for c, a in cat_rows],
        key=lambda x: x['amount'], reverse=True)

    students = Student.query.filter_by(is_active=True).all()
    bals = calc_balance_bulk([s.id for s in students])
    outstanding = round(sum(b['balance'] for b in bals.values() if b['balance'] > 0), 2)
    month_start = date.today().replace(day=1)

    return jsonify({
        'monthly': monthly,
        'by_category': by_category,
        'totals': {
            'collected_this_month': round(_sum('payment', month_start), 2),
            'collected_this_year': round(_sum('payment', year_start), 2),
            'collected_all_time': round(_sum('payment'), 2),
            'outstanding': outstanding,
        },
        'active_students': len(students),
    })


@bp.route('/students/<int:student_id>/ledger', methods=['GET'])
@login_required
def get_student_ledger(student_id):
    """Full ledger with running balance — single pass."""
    err = _require_student_money_access(student_id)
    if err:
        return err
    student = Student.query.get_or_404(student_id)
    txns = Transaction.query.filter_by(student_id=student_id).order_by(
        Transaction.transaction_date, Transaction.created_at
    ).all()
    result = build_ledger(txns)
    return jsonify({
        'student_id': student.id,
        'student_name': student.full_name,
        **result,
    })


@bp.route('/transactions/bulk-charge', methods=['POST'])
@login_required
def bulk_charge():
    err = _admin_only()
    if err:
        return err
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400
    for field in ('class_id', 'category'):
        if not data.get(field):
            return jsonify({'error': f'{field} is required'}), 400

    amount, err = _valid_amount(data.get('amount'))
    if err:
        return err
    txn_date, err = _parse_txn_date(data.get('transaction_date'))
    if err:
        return err

    dance_class = DanceClass.query.get(data['class_id'])
    if not dance_class:
        return jsonify({'error': 'Class not found'}), 404

    enrollments = ClassEnrollment.query.filter_by(class_id=dance_class.id, is_active=True).all()
    if not enrollments:
        return jsonify({'error': 'No students enrolled in this class'}), 400

    charged = []
    for e in enrollments:
        t = Transaction(
            student_id=e.student_id,
            type='charge',
            amount=amount,
            category=data['category'],
            payment_method='n/a',
            description=_clean_str(data.get('description')) or f'{dance_class.name} - {data["category"]}',
            transaction_date=txn_date,
            created_by=current_user.id,
        )
        db.session.add(t)
        charged.append(e.student_id)
    AuditLog.record(current_user.id, 'bulk_charge',
                    f'${float(amount):.2f} {data["category"]} to {len(charged)} students in {dance_class.name}')
    db.session.commit()
    return jsonify({'message': f'Charged {len(charged)} students', 'count': len(charged)}), 201


@bp.route('/transactions/<int:tid>', methods=['DELETE'])
@login_required
def delete_transaction(tid):
    """Delete a posted charge or payment (admin only) — the studio needs to fix
    a fat-fingered amount, wrong student, or duplicate entry. Hard delete, but
    audit-logged (the accountability trail) and any back-references from a
    confirmed pending payment / Square invoice are cleared first so nothing is
    left pointing at a deleted row."""
    err = _admin_only()
    if err:
        return err
    t = Transaction.query.get_or_404(tid)
    detail = (f'{t.type} ${float(t.amount):.2f} {t.category} for '
              f'{t.student.full_name if t.student else t.student_id} on {t.transaction_date}')
    # Clear the two back-references that FK to transactions so nothing dangles.
    PendingPayment.query.filter_by(transaction_id=t.id).update(
        {'transaction_id': None}, synchronize_session=False)
    CostumeAssignment.query.filter_by(transaction_id=t.id).update(
        {'transaction_id': None}, synchronize_session=False)
    db.session.delete(t)
    AuditLog.record(current_user.id, 'transaction.delete', detail)
    db.session.commit()
    return jsonify({'message': 'Transaction deleted'})


# ── Recurring charge endpoints ──────────────────────────────────────

@bp.route('/recurring-charges', methods=['GET'])
@login_required
def get_recurring_charges():
    err = _admin_only()
    if err:
        return err
    charges = RecurringCharge.query.order_by(RecurringCharge.created_at).all()
    return jsonify({'recurring_charges': [recurring_to_dict(rc) for rc in charges]})


@bp.route('/recurring-charges', methods=['POST'])
@login_required
def create_recurring_charge():
    err = _admin_only()
    if err:
        return err
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400
    for field in ('class_id', 'amount', 'category'):
        if not data.get(field):
            return jsonify({'error': f'{field} is required'}), 400

    class_id, cerr = _valid_id(data.get('class_id'))
    if cerr:
        return cerr
    dance_class = DanceClass.query.get(class_id)
    if not dance_class:
        return jsonify({'error': 'Class not found'}), 404

    # Validate the amount the SAME way one-off charges are — this fires every
    # month automatically, so a negative (silent monthly credit), non-numeric, or
    # absurd value is worse here than anywhere else.
    amount, aerr = _valid_amount(data.get('amount'))
    if aerr:
        return aerr

    try:
        day = int(data.get('day_of_month', 1))
    except (TypeError, ValueError):
        return jsonify({'error': 'day_of_month must be 1-28'}), 400
    if day < 1 or day > 28:
        return jsonify({'error': 'day_of_month must be 1-28'}), 400

    rc = RecurringCharge(
        class_id=dance_class.id,
        amount=amount,
        category=_clean_str(data['category']),
        description=_clean_str(data.get('description')) or None,
        day_of_month=day,
        created_by=current_user.id,
    )
    db.session.add(rc)
    db.session.commit()
    return jsonify(recurring_to_dict(rc)), 201


@bp.route('/recurring-charges/<int:rc_id>', methods=['PUT', 'PATCH'])
@login_required
def update_recurring_charge(rc_id):
    err = _admin_only()
    if err:
        return err
    """Edit an auto-billing rule — the studio raises tuition or shifts the billing
    day mid-year without having to delete and recreate (which risks a gap)."""
    rc = RecurringCharge.query.get_or_404(rc_id)
    data = request.get_json() or {}
    if data.get('amount') is not None:
        # Same validation as one-off charges — this fires monthly, so a negative
        # (silent monthly credit) / non-numeric / absurd value is worst here.
        amount, aerr = _valid_amount(data.get('amount'))
        if aerr:
            return aerr
        rc.amount = amount
    if data.get('category') is not None:
        cat = _clean_str(data['category'])
        if not cat:
            return jsonify({'error': 'category cannot be empty'}), 400
        rc.category = cat
    if 'description' in data:
        rc.description = _clean_str(data.get('description')) or None
    if data.get('day_of_month') is not None:
        try:
            day = int(data['day_of_month'])
        except (TypeError, ValueError):
            return jsonify({'error': 'day_of_month must be 1-28'}), 400
        if day < 1 or day > 28:
            return jsonify({'error': 'day_of_month must be 1-28'}), 400
        rc.day_of_month = day
    db.session.commit()
    return jsonify({'message': 'Auto-billing rule updated', 'id': rc.id,
                    'amount': f'{float(rc.amount):.2f}', 'day_of_month': rc.day_of_month,
                    'category': rc.category})


@bp.route('/recurring-charges/<int:rc_id>', methods=['DELETE'])
@login_required
def delete_recurring_charge(rc_id):
    err = _admin_only()
    if err:
        return err
    rc = RecurringCharge.query.get_or_404(rc_id)
    rc.is_active = False
    db.session.commit()
    return jsonify({'message': 'Recurring charge deactivated'})


@bp.route('/recurring-charges/process', methods=['POST'])
@login_required
def process_recurring_charges():
    err = _admin_only()
    if err:
        return err
    from app import _process_recurring_charges
    _process_recurring_charges()
    return jsonify({'message': 'Recurring charges processed'})


# ── Square payment endpoints ────────────────────────────────────────

@bp.route('/square/status', methods=['GET'])
@login_required
def square_status():
    err = _staff_only()
    if err:
        return err
    return jsonify({'configured': square_service.is_configured()})


@bp.route('/students/<int:student_id>/send-invoice', methods=['POST'])
@login_required
def send_student_invoice(student_id):
    err = _admin_only()
    if err:
        return err
    if not square_service.is_configured():
        return jsonify({'error': 'Square is not configured. Set SQUARE_ACCESS_TOKEN and SQUARE_LOCATION_ID in environment.'}), 400

    student = Student.query.get_or_404(student_id)
    bal = calc_balance(student_id)

    if bal['balance'] <= 0:
        return jsonify({'error': 'No outstanding balance to invoice'}), 400

    # Square derives the order total from the SUM of line items (amount_cents is
    # ignored by the SDK), so the line items must sum to the OUTSTANDING balance,
    # not to gross charges — otherwise a family that has paid down their balance
    # gets billed the full original amount. Invoice the net balance as one line.
    amount_cents = int(round(bal['balance'] * 100))
    line_items = [{
        'name': 'Outstanding balance',
        'amount_cents': amount_cents,
    }]

    due = date.today() + timedelta(days=14)
    try:
        result = square_service.send_invoice(
            student=student,
            amount_cents=amount_cents,
            line_items=line_items,
            due_date=due,
        )
        # Persist so the webhook can auto-record the payment when paid
        inv = SquareInvoice(
            student_id=student.id,
            invoice_id=result['invoice_id'],
            amount_cents=amount_cents,
            status=result.get('status', 'SENT'),
            public_url=result.get('invoice_url', ''),
        )
        db.session.add(inv)
        AuditLog.record(current_user.id, 'invoice.send',
                        f'Square invoice ${bal["balance"]:.2f} to {student.full_name} '
                        f'({student.parent_email or student.email})')
        db.session.commit()
        return jsonify({
            'message': f'Invoice sent to {student.parent_email or student.email}',
            'invoice_url': result['invoice_url'],
            'invoice_id': result['invoice_id'],
        })
    except Exception as e:
        db.session.rollback()
        # Log the full failure server-side so a "Square invoices aren't sending"
        # report is diagnosable; return the studio's usual clean Square detail
        # (matches square_service.test_connection) rather than a raw traceback.
        logger.exception("Square invoice failed for student %d", student_id)
        return jsonify({'error': f'Could not send the invoice: {square_service._error_detail(e)}'}), 500


# ── Parent invite endpoints ─────────────────────────────────────────

@bp.route('/students/<int:student_id>/invite-parent', methods=['POST'])
@login_required
def invite_parent(student_id):
    # Parent-account onboarding is an admin operation (least privilege).
    err = _admin_only()
    if err:
        return err

    student = Student.query.get_or_404(student_id)

    # Check if parent already linked
    existing_parent = (
        User.query
        .join(ParentStudent, ParentStudent.parent_id == User.id)
        .filter(ParentStudent.student_id == student_id, User.is_active == True)  # noqa: E712
        .first()
    )
    if existing_parent:
        return jsonify({
            'error': f'Parent already linked: {existing_parent.full_name} ({existing_parent.email})'
        }), 400

    code = secrets.token_hex(4).upper()
    parent_user = User(
        username=f'parent-{code}',
        email=f'invite-{code}@pending.local',
        first_name='Pending',
        last_name='Parent',
        password_hash='not-set',
        role='parent',
        is_active=False,
        invite_code=code,
    )
    db.session.add(parent_user)
    db.session.flush()

    link = ParentStudent(parent_id=parent_user.id, student_id=student_id)
    db.session.add(link)
    db.session.commit()

    return jsonify({
        'invite_code': code,
        'message': f'Invite code generated for {student.full_name}. Share this with the parent: {code}',
        'register_url': f'/auth/register?code={code}',
    }), 201


@bp.route('/seed-demo-parent', methods=['POST'])
@login_required
def seed_demo_parent():
    if not current_user.is_admin:
        return jsonify({'error': 'Admin access required'}), 403
    # Dev-only: this links a publicly-known password to a real student's records.
    # Production boot also disables any previously-seeded demo account.
    if not (current_app.debug or current_app.testing):
        return jsonify({'error': 'Demo accounts are disabled in production'}), 403

    student = Student.query.first()
    if not student:
        return jsonify({'error': 'No students found'}), 400

    existing = User.query.filter_by(username='parent-demo').first()
    if existing:
        ParentStudent.query.filter_by(parent_id=existing.id).delete()
        db.session.delete(existing)
        db.session.commit()

    p = User(
        username='parent-demo', email='parent@demo.local',
        first_name='Demo', last_name='Parent', role='parent', is_active=True,
    )
    p.set_password('parent123')
    db.session.add(p)
    db.session.flush()
    db.session.add(ParentStudent(parent_id=p.id, student_id=student.id))
    db.session.commit()
    return jsonify({
        'message': f'Parent account created: parent-demo / parent123, linked to {student.full_name}'
    })


# ── Rules & Regulations endpoints ──────────────────────────────────

@bp.route('/rules', methods=['GET'])
@login_required
def get_rules():
    rules = Rule.query.filter_by(is_active=True).order_by(Rule.display_order).all()
    return jsonify({'rules': [
        {'id': r.id, 'text': r.text, 'display_order': r.display_order}
        for r in rules
    ]})


@bp.route('/rules', methods=['POST'])
@login_required
def create_rule():
    # Admin-only (least privilege, Thomas 2026-07-06): rules are studio policy —
    # teachers can view them (the nav/page stay staff) but not change them.
    err = _admin_only()
    if err:
        return err
    data = request.get_json()
    if not data or not data.get('text'):
        return jsonify({'error': 'text is required'}), 400
    max_order = db.session.query(func.max(Rule.display_order)).scalar() or 0
    r = Rule(text=_clean_str(data['text']), display_order=max_order + 1)
    db.session.add(r)
    db.session.commit()
    return jsonify({'id': r.id, 'text': r.text, 'display_order': r.display_order}), 201


@bp.route('/rules/<int:rule_id>', methods=['PUT'])
@login_required
def update_rule(rule_id):
    err = _admin_only()
    if err:
        return err
    r = Rule.query.get_or_404(rule_id)
    data = request.get_json(silent=True) or {}
    if data.get('text'):
        r.text = _clean_str(data['text'])
    if 'display_order' in data:
        try:
            r.display_order = int(data['display_order'])
        except (TypeError, ValueError):
            return jsonify({'error': 'display_order must be a number'}), 400
    db.session.commit()
    return jsonify({'id': r.id, 'text': r.text, 'display_order': r.display_order})


@bp.route('/rules/<int:rule_id>', methods=['DELETE'])
@login_required
def delete_rule(rule_id):
    err = _admin_only()
    if err:
        return err
    r = Rule.query.get_or_404(rule_id)
    r.is_active = False
    db.session.commit()
    return jsonify({'message': 'Rule removed'})


@bp.route('/students/<int:student_id>/rules-status', methods=['GET'])
@login_required
def get_student_rules_status(student_id):
    err = _require_student_access(student_id)
    if err:
        return err
    student = Student.query.get_or_404(student_id)
    rules = Rule.query.filter_by(is_active=True).order_by(Rule.display_order).all()
    acks = RuleAcknowledgment.query.filter_by(student_id=student_id).all()
    ack_map = {a.rule_id: a for a in acks}

    result = []
    for r in rules:
        ack = ack_map.get(r.id)
        result.append({
            'rule_id': r.id, 'text': r.text, 'display_order': r.display_order,
            'acknowledged': ack is not None,
            'initials': ack.initials if ack else None,
            'acknowledged_at': _utc_iso(ack.acknowledged_at) if ack else None,
        })

    active_rule_ids = {r.id for r in rules}
    done = sum(1 for rid in ack_map if rid in active_rule_ids)
    total = len(rules)

    return jsonify({
        'student_name': student.full_name,
        'rules': result,
        'total': total, 'acknowledged': done,
        'complete': done == total and total > 0,
    })


@bp.route('/rules/<int:rule_id>/acknowledge', methods=['POST'])
@login_required
def acknowledge_rule(rule_id):
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400
    student_id = data.get('student_id')
    initials = _clean_str(data.get('initials'))
    if not student_id or not initials:
        return jsonify({'error': 'student_id and initials are required'}), 400

    err = _require_student_access(student_id)
    if err:
        return err

    Rule.query.get_or_404(rule_id)
    Student.query.get_or_404(student_id)

    existing = RuleAcknowledgment.query.filter_by(
        rule_id=rule_id, student_id=student_id, parent_id=current_user.id
    ).first()
    if existing:
        return jsonify({'message': 'Already acknowledged'})

    ack = RuleAcknowledgment(
        rule_id=rule_id, student_id=student_id,
        parent_id=current_user.id, initials=initials.upper(),
    )
    db.session.add(ack)
    db.session.commit()
    return jsonify({'message': 'Rule acknowledged', 'initials': ack.initials}), 201


# ── Message / Email blast endpoints ─────────────────────────────────

# Attachment limits. Per-file 5MB / per-message 10MB keeps a whole-studio blast
# under Gmail's 25MB ceiling once base64 inflates it ~33%, and keeps the peak
# working set survivable on the 256MB Fly machine (the send worker holds one
# serialized copy at a time). MAX_CONTENT_LENGTH (16MB) rejects anything past
# the total before Flask reads the body.
_ATTACH_MAX_FILE_BYTES = 5 * 1024 * 1024
_ATTACH_MAX_TOTAL_BYTES = 10 * 1024 * 1024
_ATTACH_MAX_COUNT = 5

# Extension -> content type. The stored type comes from THIS map, never from the
# client's multipart Content-Type header (same reasoning as the image uploads:
# a header-supplied type can carry an injection payload). An unlisted extension
# is rejected outright, which keeps executables and active content (.svg, .html,
# .js) out of both the DB and parents' inboxes.
_ATTACH_TYPES = {
    'pdf': 'application/pdf',
    'png': 'image/png',
    'jpg': 'image/jpeg',
    'jpeg': 'image/jpeg',
    'gif': 'image/gif',
    'webp': 'image/webp',
    'heic': 'image/heic',
    'txt': 'text/plain',
    'csv': 'text/csv',
    'doc': 'application/msword',
    'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'xls': 'application/vnd.ms-excel',
    'xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    'ppt': 'application/vnd.ms-powerpoint',
    'pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
}

_ATTACH_ALLOWED_LABEL = 'PDF, image, Word, Excel, PowerPoint, TXT, or CSV'


def _attachment_to_dict(a, message_id):
    return {
        'id': a.id, 'message_id': message_id,
        'filename': a.filename, 'content_type': a.content_type, 'size': a.size,
        'url': url_for('api.download_message_attachment',
                       mid=message_id, aid=a.id),
    }


def _attachments_for_messages(message_ids):
    """Bulk-load attachment metadata for a page of messages, blobs excluded.

    Queries explicit columns so the LargeBinary never enters the result set -
    a 50-row history page with a 5MB flyer on each would otherwise pull 250MB
    through a 256MB machine. `MessageAttachment.data` is deferred as a second
    line of defense."""
    if not message_ids:
        return {}
    rows = (db.session.query(
                MessageAttachment.id, MessageAttachment.message_id,
                MessageAttachment.filename, MessageAttachment.content_type,
                MessageAttachment.size)
            .filter(MessageAttachment.message_id.in_(message_ids))
            .order_by(MessageAttachment.id)
            .all())
    out: dict[int, list] = {}
    for r in rows:
        out.setdefault(r.message_id, []).append({
            'id': r.id, 'message_id': r.message_id,
            'filename': r.filename, 'content_type': r.content_type, 'size': r.size,
            'url': url_for('api.download_message_attachment',
                           mid=r.message_id, aid=r.id),
        })
    return out


def _collect_message_attachments():
    """Read, validate, and normalize uploaded files off a multipart message post.

    Returns a list of dicts ready for MessageAttachment, or an (json, status)
    error tuple. An empty list means the admin attached nothing, which is the
    normal case and not an error."""
    files = [f for f in request.files.getlist('attachments') if f and f.filename]
    if not files:
        return []
    if len(files) > _ATTACH_MAX_COUNT:
        return jsonify({'error': f'Too many files (max {_ATTACH_MAX_COUNT})'}), 400

    out = []
    total = 0
    for f in files:
        ext = f.filename.rsplit('.', 1)[-1].lower() if '.' in f.filename else ''
        content_type = _ATTACH_TYPES.get(ext)
        if not content_type:
            return jsonify({
                'error': f'"{f.filename}" is not an allowed file type '
                         f'({_ATTACH_ALLOWED_LABEL})'
            }), 400
        raw = f.read()
        if not raw:
            return jsonify({'error': f'"{f.filename}" is empty'}), 400
        if len(raw) > _ATTACH_MAX_FILE_BYTES:
            mb = _ATTACH_MAX_FILE_BYTES // (1024 * 1024)
            return jsonify({'error': f'"{f.filename}" is too large (max {mb}MB per file)'}), 400
        total += len(raw)
        if total > _ATTACH_MAX_TOTAL_BYTES:
            mb = _ATTACH_MAX_TOTAL_BYTES // (1024 * 1024)
            return jsonify({'error': f'Attachments total more than {mb}MB'}), 400
        # secure_filename strips path separators, quotes, and newlines, so the
        # stored name is safe to drop straight into a Content-Disposition header
        # (both on download here and on the outgoing MIME part).
        safe = secure_filename(f.filename) or f'attachment.{ext}'
        out.append({'filename': safe[:255], 'content_type': content_type,
                    'size': len(raw), 'data': raw})
    return out


@bp.route('/messages', methods=['GET'])
@login_required
def get_messages():
    err = _staff_only()
    if err:
        return err
    page = request.args.get('page', 1, type=int)
    per_page = min(request.args.get('per_page', 50, type=int), 100)
    pagination = (Message.query.order_by(desc(Message.created_at))
                  .paginate(page=page, per_page=per_page, error_out=False))
    attachments = _attachments_for_messages([m.id for m in pagination.items])
    return jsonify({
        'messages': [{
            'id': m.id, 'subject': m.subject, 'body': m.body,
            'recipient_type': m.recipient_type, 'recipient_filter': m.recipient_filter,
            'recipient_count': m.recipient_count, 'recipient_emails': m.recipient_emails,
            'sent': m.sent, 'sent_at': _utc_iso(m.sent_at),
            'created_by': m.creator.full_name if m.creator else None,
            'created_at': _utc_iso(m.created_at),
            'attachments': attachments.get(m.id, []),
        } for m in pagination.items],
        'pagination': {
            'page': page, 'pages': pagination.pages,
            'per_page': per_page, 'total': pagination.total,
        },
    })


@bp.route('/messages/<int:mid>/attachments/<int:aid>', methods=['GET'])
@login_required
def download_message_attachment(mid, aid):
    """Serve an attachment back to staff - so the studio can re-download what
    went out, and can send it by hand when SMTP isn't configured."""
    from flask import Response
    err = _staff_only()
    if err:
        return err
    a = MessageAttachment.query.get_or_404(aid)
    if a.message_id != mid:
        return jsonify({'error': 'Attachment not found'}), 404
    return Response(a.data, mimetype=a.content_type, headers={
        # Always download, never render in-origin: even with a whitelist, an
        # inline render of user-supplied bytes is a needless XSS surface.
        'Content-Disposition': f'attachment; filename="{a.filename}"',
    })


def _send_email_async(emails, subject, body):
    """Fire-and-forget a best-effort email off the request thread. Admin
    notifications (a new pending payment, a new registration) are best-effort —
    the user doesn't need them to land before their response, and the admins see
    every item in the UI regardless. Sending inline couples the user's latency
    (and, at worst, the 120s worker timeout) to Gmail's responsiveness, which is
    wrong for a notification the user never sees. Resolve recipients before
    calling this (a fast DB query); only the SMTP send is backgrounded."""
    app = current_app._get_current_object()
    recipients = list(emails)
    if not recipients:
        return

    def _worker():
        with app.app_context():
            from app import email as email_service
            try:
                email_service.send_email(recipients, subject, body)
            except Exception:
                logger.exception("Async email send failed: %s", subject)

    threading.Thread(target=_worker, daemon=True).start()


def _send_message_blast(app, message_id, emails, subject, body):
    """Background worker for a message blast. Runs off the request thread: a
    whole-studio blast ('all' recipients) is one SMTP round-trip per family and
    would exceed the 120s gunicorn worker timeout if sent inline (the same reason
    balance reminders are threaded). The Message row is saved as unsent before
    this starts and flipped to sent here on success.

    Attachment bytes are re-read from the DB here rather than handed in from the
    request, so the uploaded buffers are free the moment the response returns."""
    with app.app_context():
        from app import email as email_service
        try:
            attachments = [
                {'filename': a.filename, 'content_type': a.content_type, 'data': a.data}
                for a in MessageAttachment.query.filter_by(message_id=message_id)
                                                .order_by(MessageAttachment.id).all()
            ]
            email_service.send_email(emails, subject, body, attachments=attachments)
            m = Message.query.get(message_id)
            if m:
                m.sent = True
                m.sent_at = datetime.utcnow()
                db.session.commit()
            logger.info("Message blast %s sent to %d recipient(s)", message_id, len(emails))
        except Exception:
            # Left as sent=False so the messages history shows it didn't go out;
            # the saved recipient list stays on the row for a manual retry.
            logger.exception("Message blast %s background send failed", message_id)


@bp.route('/messages', methods=['POST'])
@login_required
def send_message():
    # Two content types land here: multipart/form-data when the compose form
    # carries attachments (files can't ride in JSON without a 33% base64 tax on
    # a 256MB machine), and JSON for every other caller.
    if request.files or request.mimetype == 'multipart/form-data':
        data = request.form.to_dict()
    else:
        data = request.get_json(silent=True)
    if not data:
        return jsonify({'error': 'No data provided'}), 400
    subject = _clean_str(data.get('subject'))
    body = _clean_str(data.get('body'))
    rtype = _clean_str(data.get('recipient_type'))
    if not subject or not body or not rtype:
        return jsonify({'error': 'subject, body, and recipient_type are required'}), 400
    if rtype == 'all' and not current_user.is_admin:
        # Whole-studio blasts are admin-only (least privilege); teachers can
        # still message a class or an individual family.
        return jsonify({'error': 'Only an admin can message all parents'}), 403

    # Validate attachments BEFORE the Message row exists, so a rejected file
    # doesn't leave a phantom entry in the studio's message history.
    attachments = _collect_message_attachments()
    if isinstance(attachments, tuple):
        return attachments  # error response

    emails = _resolve_recipient_emails(rtype, data.get('recipient_filter'))
    if isinstance(emails, tuple):
        return emails  # error response

    if not emails:
        return jsonify({'error': 'No email addresses found for selected recipients'}), 400

    msg = Message(
        subject=subject,
        body=body,
        recipient_type=rtype,
        recipient_filter=str(data.get('recipient_filter', '')),
        recipient_count=len(emails),
        recipient_emails=', '.join(sorted(emails)),
        created_by=current_user.id,
        sent=False,
    )
    db.session.add(msg)
    db.session.flush()  # need msg.id to hang attachments off
    saved_attachments = []
    for att in attachments:
        row = MessageAttachment(message_id=msg.id, **att)
        db.session.add(row)
        saved_attachments.append(row)
    db.session.commit()
    attachment_meta = [_attachment_to_dict(a, msg.id) for a in saved_attachments]

    from app import email as email_service
    if email_service.is_configured():
        # Send in a background thread and return immediately. Sending inline would
        # blow the 120s gunicorn timeout on a whole-studio blast and 502 the
        # request mid-send (partial delivery, sent-state lost). The row is saved
        # as unsent above and marked sent by the worker.
        app = current_app._get_current_object()
        threading.Thread(target=_send_message_blast,
                         args=(app, msg.id, sorted(emails), subject, body),
                         daemon=True).start()
        note = ''
        if attachment_meta:
            note = f' with {len(attachment_meta)} attachment(s)'
        return jsonify({
            'message': f'Sending to {len(emails)} recipient(s){note}…',
            'message_id': msg.id,
            'queued': len(emails),
            'attachments': attachment_meta,
        }), 201

    # SMTP not configured — hand the admin the addresses to send manually.
    reply_to = current_app.config.get('MAIL_REPLY_TO', '')
    return jsonify({
        'message': 'Message saved (SMTP not configured — copy emails below to send manually)',
        'message_id': msg.id,
        'recipient_emails': sorted(emails),
        'recipient_count': len(emails),
        'reply_to': reply_to,
        'attachments': attachment_meta,
    }), 201


def _resolve_recipient_emails(rtype: str, recipient_filter) -> set | tuple:
    """Resolve email addresses for a message. Returns a set of emails or an error tuple."""
    emails: set[str] = set()
    if rtype == 'all':
        for s in Student.query.filter_by(is_active=True).all():
            if s.parent_email:
                emails.add(s.parent_email)
            elif s.email:
                emails.add(s.email)
    elif rtype == 'class':
        if not recipient_filter:
            return jsonify({'error': 'recipient_filter (class_id) required for class type'}), 400
        try:
            cid = int(recipient_filter)
        except (TypeError, ValueError):
            return jsonify({'error': 'Invalid class selection'}), 400
        # Use join to avoid N+1
        rows = (
            db.session.query(Student.parent_email, Student.email)
            .join(ClassEnrollment, ClassEnrollment.student_id == Student.id)
            .filter(ClassEnrollment.class_id == cid, ClassEnrollment.is_active == True)  # noqa: E712
            .all()
        )
        for parent_email, student_email in rows:
            if parent_email:
                emails.add(parent_email)
            elif student_email:
                emails.add(student_email)
    elif rtype == 'individual':
        if not recipient_filter:
            return jsonify({'error': 'recipient_filter (student_id) required for individual type'}), 400
        try:
            sid = int(recipient_filter)
        except (TypeError, ValueError):
            return jsonify({'error': 'Invalid student selection'}), 400
        s = Student.query.get(sid)
        if s and (s.parent_email or s.email):
            emails.add(s.parent_email or s.email)
    return emails


# ── Family endpoints ────────────────────────────────────────────────

@bp.route('/families', methods=['GET'])
@login_required
def get_families():
    """Get all families with balances — bulk query."""
    err = _staff_only()
    if err:
        return err
    families = Family.query.filter_by(is_active=True).order_by(Family.name).all()

    # Collect all student IDs across all families
    family_students: dict[int, list] = {}
    all_student_ids: list[int] = []
    for f in families:
        students = f.students.filter_by(is_active=True).all()
        family_students[f.id] = students
        all_student_ids.extend(s.id for s in students)

    # Single bulk balance query
    balances_map = calc_balance_bulk(all_student_ids)

    result = []
    for f in families:
        students = family_students[f.id]
        row = {
            'id': f.id, 'name': f.name,
            'primary_email': f.primary_email, 'primary_phone': f.primary_phone,
            'student_count': len(students),
            'students': [{'id': s.id, 'full_name': s.full_name} for s in students],
        }
        # Billing is admin-only: teachers get the family list (for forms and
        # sibling context) but not the money columns.
        if current_user.is_admin:
            total_charges = sum(balances_map[s.id]['total_charges'] for s in students)
            total_payments = sum(balances_map[s.id]['total_payments'] for s in students)
            row.update({
                'total_charges': f'{total_charges:.2f}',
                'total_payments': f'{total_payments:.2f}',
                'balance': f'{total_charges - total_payments:.2f}',
            })
        result.append(row)
    return jsonify({'families': result})


@bp.route('/families', methods=['POST'])
@login_required
def create_family():
    err = _admin_only()
    if err:
        return err
    data = request.get_json() or {}
    name = _clean_str(data.get('name'))
    if not name:
        return jsonify({'error': 'name is required'}), 400
    f = Family(
        name=name,
        primary_email=_clean_str(data.get('primary_email')) or None,
        primary_phone=_clean_str(data.get('primary_phone')) or None,
    )
    db.session.add(f)
    db.session.commit()
    return jsonify({'id': f.id, 'name': f.name}), 201


@bp.route('/families/<int:family_id>/ledger', methods=['GET'])
@login_required
def get_family_ledger(family_id):
    """Combined ledger for all students in a family — single pass."""
    err = _require_family_money_access(family_id)
    if err:
        return err
    family = Family.query.get_or_404(family_id)
    students = family.students.filter_by(is_active=True).all()
    student_ids = [s.id for s in students]

    all_txns = (
        Transaction.query
        .filter(Transaction.student_id.in_(student_ids))
        .order_by(Transaction.transaction_date, Transaction.created_at)
        .all()
    ) if student_ids else []

    result = build_ledger(all_txns)
    return jsonify({
        'family_id': family.id, 'family_name': family.name,
        'students': [{'id': s.id, 'full_name': s.full_name} for s in students],
        **result,
    })


# ── Staff / Teacher endpoints (admin only) ──────────────────────────

def _staff_to_dict(u):
    return {
        'id': u.id,
        'username': u.username,
        'email': u.email,
        'first_name': u.first_name,
        'last_name': u.last_name,
        'full_name': u.full_name,
        'phone': u.phone,
        'role': u.role,
        'is_admin': u.is_admin,
        'is_active': u.is_active,
        'last_login': _utc_iso(u.last_login),
        'created_at': _utc_iso(u.created_at),
    }


@bp.route('/staff', methods=['GET'])
@login_required
def get_staff():
    """Get all staff (admin + teacher) users."""
    if not current_user.is_admin:
        return jsonify({'error': 'Admin access required'}), 403
    users = User.query.filter(User.role.in_(['admin', 'teacher'])).order_by(User.last_name, User.first_name).all()
    return jsonify({'staff': [_staff_to_dict(u) for u in users]})


@bp.route('/staff', methods=['POST'])
@login_required
def create_staff():
    """Create a new teacher or admin user."""
    if not current_user.is_admin:
        return jsonify({'error': 'Admin access required'}), 403
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400
    for field in ('first_name', 'last_name', 'email', 'username', 'password'):
        if not data.get(field):
            return jsonify({'error': f'{field} is required'}), 400

    if User.query.filter_by(username=_clean_str(data['username'])).first():
        return jsonify({'error': 'Username already taken'}), 400
    if User.query.filter_by(email=_clean_str(data['email'])).first():
        return jsonify({'error': 'Email already in use'}), 400

    role = data.get('role', 'teacher')
    if role not in ('admin', 'teacher'):
        return jsonify({'error': 'Role must be admin or teacher'}), 400

    u = User(
        username=_clean_str(data['username']),
        email=_clean_str(data['email']),
        first_name=_clean_str(data['first_name']),
        last_name=_clean_str(data['last_name']),
        phone=_clean_str(data.get('phone')) or None,
        role=role,
        is_admin=(role == 'admin'),
        is_active=True,
    )
    u.set_password(data['password'])
    db.session.add(u)
    db.session.commit()
    return jsonify(_staff_to_dict(u)), 201


@bp.route('/staff/<int:user_id>', methods=['PUT'])
@login_required
def update_staff(user_id):
    """Update a staff user."""
    if not current_user.is_admin:
        return jsonify({'error': 'Admin access required'}), 403
    u = User.query.get_or_404(user_id)
    if u.role not in ('admin', 'teacher'):
        return jsonify({'error': 'Not a staff user'}), 400
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    if 'first_name' in data:
        u.first_name = _clean_str(data['first_name'])
    if 'last_name' in data:
        u.last_name = _clean_str(data['last_name'])
    if 'email' in data:
        email = _clean_str(data['email'])
        if email != u.email and User.query.filter_by(email=email).first():
            return jsonify({'error': 'Email already in use'}), 400
        u.email = email
    if 'phone' in data:
        u.phone = _clean_str(data['phone']) or None
    if 'role' in data:
        role = data['role']
        if role not in ('admin', 'teacher'):
            return jsonify({'error': 'Role must be admin or teacher'}), 400
        u.role = role
        u.is_admin = (role == 'admin')
    if data.get('password'):
        u.set_password(data['password'])

    db.session.commit()
    return jsonify(_staff_to_dict(u))


@bp.route('/parents/<int:user_id>/reset-link', methods=['POST'])
@login_required
def parent_reset_link(user_id):
    """Admin generates a single-use password-reset link for a PARENT account.
    This is the no-SMTP recovery path: a parent who forgot their password calls
    the studio, and the admin texts/reads them this link. Reuses the
    forgot-password token — 1-hour expiry, invalidated by any password change,
    rejected for inactive accounts."""
    err = _admin_only()
    if err:
        return err
    u = User.query.get_or_404(user_id)
    if u.role != 'parent':
        return jsonify({'error': 'Reset links are for parent accounts'}), 400
    if not u.is_active:
        return jsonify({'error': 'That account is deactivated'}), 400
    from app.auth.routes import _reset_serializer
    token = _reset_serializer().dumps({'uid': u.id, 'pw': (u.password_hash or '')[-16:]})
    link = url_for('auth.reset_password', token=token, _external=True)
    AuditLog.record(current_user.id, 'parent.reset_link',
                    f'Password-reset link generated for {u.username}')
    db.session.commit()
    return jsonify({'reset_url': link, 'expires_in_minutes': 60})


@bp.route('/staff/<int:user_id>', methods=['DELETE'])
@login_required
def deactivate_staff(user_id):
    """Deactivate a staff user (soft delete)."""
    if not current_user.is_admin:
        return jsonify({'error': 'Admin access required'}), 403
    u = User.query.get_or_404(user_id)
    if u.id == current_user.id:
        return jsonify({'error': 'Cannot deactivate your own account'}), 400
    u.is_active = False
    db.session.commit()
    return jsonify({'message': f'{u.full_name} deactivated'})


# ── Location endpoints ──────────────────────────────────────────────

def _location_to_dict(loc):
    return {
        'id': loc.id,
        'name': loc.name,
        'address': loc.address,
        'city': loc.city,
        'state': loc.state,
        'zip_code': loc.zip_code,
        'full_address': loc.full_address,
        'phone': loc.phone,
        'notes': loc.notes,
        'is_active': loc.is_active,
        'class_count': loc.classes.filter_by(is_active=True).count(),
        'created_at': _utc_iso(loc.created_at),
    }


@bp.route('/locations', methods=['GET'])
@login_required
def get_locations():
    """Get all locations (staff only — carries internal notes/phone)."""
    err = _staff_only()
    if err:
        return err
    locations = Location.query.filter_by(is_active=True).order_by(Location.name).all()
    return jsonify({'locations': [_location_to_dict(loc) for loc in locations]})


@bp.route('/locations', methods=['POST'])
@login_required
def create_location():
    """Create a new location (admin only)."""
    if not current_user.is_admin:
        return jsonify({'error': 'Admin access required'}), 403
    data = request.get_json() or {}
    name = _clean_str(data.get('name'))
    if not name:
        return jsonify({'error': 'name is required'}), 400

    loc = Location(
        name=name,
        address=_clean_str(data.get('address')) or None,
        city=_clean_str(data.get('city')) or None,
        state=_clean_str(data.get('state')) or None,
        zip_code=_clean_str(data.get('zip_code')) or None,
        phone=_clean_str(data.get('phone')) or None,
        notes=_clean_str(data.get('notes')) or None,
    )
    db.session.add(loc)
    db.session.commit()
    return jsonify(_location_to_dict(loc)), 201


@bp.route('/locations/<int:location_id>', methods=['PUT'])
@login_required
def update_location(location_id):
    """Update a location."""
    if not current_user.is_admin:
        return jsonify({'error': 'Admin access required'}), 403
    loc = Location.query.get_or_404(location_id)
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    for field in ('name', 'address', 'city', 'state', 'zip_code', 'phone', 'notes'):
        if field in data:
            setattr(loc, field, _clean_str(data[field]) or None)

    db.session.commit()
    return jsonify(_location_to_dict(loc))


@bp.route('/locations/<int:location_id>', methods=['DELETE'])
@login_required
def deactivate_location(location_id):
    """Deactivate a location."""
    if not current_user.is_admin:
        return jsonify({'error': 'Admin access required'}), 403
    loc = Location.query.get_or_404(location_id)
    loc.is_active = False
    db.session.commit()
    return jsonify({'message': f'{loc.name} deactivated'})


# ── Settings endpoints (admin only) ─────────────────────────────────

# Settings editable via the payments PUT endpoint
PAYMENT_SETTINGS_KEYS = [
    'payments_zelle_enabled', 'payments_zelle_name', 'payments_zelle_memo',
    'payments_cashapp_enabled', 'payments_cashapp_tag',
    'payments_square_enabled', 'payments_square_access_token',
    'payments_square_location_id', 'payments_square_environment',
    'payments_square_webhook_signature_key', 'payments_square_link',
    # SMS / Twilio
    'sms_enabled', 'sms_twilio_sid', 'sms_twilio_token', 'sms_from_number',
    # Automated reminders
    'reminders_auto_enabled', 'reminders_day_of_month', 'reminders_min_balance', 'reminders_send_sms',
    # Late fees
    'late_fee_amount', 'late_fee_min_balance',
    # Donations / Foundation
    'donations_enabled', 'donations_org_name', 'donations_ein',
    # Self-registration
    'registration_open', 'registration_message', 'registration_season_id',
]

# Secret settings: encrypted at rest, masked on read
SECRET_SETTINGS_KEYS = {
    'payments_square_access_token',
    'payments_square_webhook_signature_key',
    'sms_twilio_token',
}

DEFAULT_ZELLE_MEMO = "Put your dancer's full name in the memo so we can match your payment."


def _admin_only():
    """Return an error response tuple if the current user isn't admin, else None."""
    if not current_user.is_admin:
        return jsonify({'error': 'Admin access required'}), 403
    return None


def _mask_secret(plaintext: str) -> str:
    if not plaintext:
        return ''
    if len(plaintext) > 12:
        return plaintext[:4] + '••••' + plaintext[-4:]
    return '••••'


# Hosts a Square-hosted checkout link may live on. The saved link renders into an
# href on the parent portal, so it is host-allowlisted rather than merely
# "starts with https" — an open redirect or a look-alike payment page here would
# send parents' card details somewhere else (defense-in-depth against an
# admin-account compromise, same reasoning as the Cash App cashtag scrub).
SQUARE_LINK_HOSTS = {'square.link', 'checkout.square.site', 'squareup.com'}
SQUARE_LINK_SUFFIXES = ('.square.site', '.squareup.com')


def _valid_square_link(value):
    """('normalized url', None) on success, (None, error message) on failure.
    Empty input is valid and clears the setting."""
    from urllib.parse import urlparse
    val = (value or '').strip()
    if not val:
        return '', None
    if len(val) > 500:
        return None, 'Square link is too long'
    parsed = urlparse(val)
    if parsed.scheme != 'https':
        return None, 'Square link must start with https://'
    host = (parsed.hostname or '').lower()
    if host not in SQUARE_LINK_HOSTS and not host.endswith(SQUARE_LINK_SUFFIXES):
        return None, ('Square link must be a Square checkout URL '
                      '(square.link, checkout.square.site, or squareup.com)')
    return val, None


@bp.route('/settings/payments', methods=['GET'])
@login_required
def get_payment_settings():
    """Get payment configuration. Secrets are decrypted then masked for display."""
    err = _admin_only()
    if err:
        return err
    from app.crypto import decrypt
    settings = {}
    for key in PAYMENT_SETTINGS_KEYS:
        raw = Setting.get(key, '')
        if key in SECRET_SETTINGS_KEYS:
            settings[key] = _mask_secret(decrypt(raw))
        else:
            settings[key] = raw
    if not settings.get('payments_zelle_memo'):
        settings['payments_zelle_memo'] = DEFAULT_ZELLE_MEMO
    settings['has_zelle_qr'] = bool(Setting.get('payments_zelle_qr_data') or Setting.get('payments_zelle_qr_path'))
    settings['zelle_qr'] = Setting.get('payments_zelle_qr_data') or Setting.get('payments_zelle_qr_path', '')
    return jsonify({'settings': settings})


@bp.route('/settings/payments', methods=['PUT'])
@login_required
def update_payment_settings():
    """Update payment configuration. Secrets are encrypted before storage."""
    err = _admin_only()
    if err:
        return err
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    from app.crypto import encrypt

    # Validate BEFORE writing anything: Setting.set commits per key, so bailing
    # out mid-loop would leave the rest of the form half-saved.
    square_link = None
    if 'payments_square_link' in data:
        square_link, lerr = _valid_square_link(data['payments_square_link'])
        if lerr:
            return jsonify({'error': lerr}), 400

    changed = []
    for key in PAYMENT_SETTINGS_KEYS:
        if key not in data:
            continue
        val = (data[key] or '').strip()
        if key == 'payments_square_link':
            val = square_link
        if key in SECRET_SETTINGS_KEYS:
            # Skip masked placeholders — means "leave unchanged"
            if not val or '••••' in val:
                continue
            Setting.set(key, encrypt(val))
            changed.append(key)
        else:
            Setting.set(key, val)
            changed.append(key)

    if changed:
        AuditLog.record(current_user.id, 'settings.update',
                        'Updated payment settings: ' + ', '.join(changed))
        db.session.commit()
    return jsonify({'message': 'Payment settings updated', 'changed': changed})


@bp.route('/settings/payments/zelle-qr', methods=['POST'])
@login_required
def upload_zelle_qr():
    """Upload Zelle QR code image — stored as a data URI in the DB so it
    survives redeploys (the filesystem on Fly is ephemeral)."""
    import base64
    err = _admin_only()
    if err:
        return err
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400
    f = request.files['file']
    if not f.filename:
        return jsonify({'error': 'No file selected'}), 400

    raw = f.read()
    if len(raw) > 2 * 1024 * 1024:
        return jsonify({'error': 'Image too large (max 2MB)'}), 400

    # Whitelist the content type to EXACT values — never trust the raw mimetype.
    # It comes from the client's multipart Content-Type header, so a prefix check
    # (startswith 'image/') would let `image/png"><script>` through and into the
    # data URI, which then renders in an unescaped src= (parent-portal XSS).
    VALID_IMAGE_TYPES = {'image/png', 'image/jpeg', 'image/gif', 'image/webp'}
    ext_map = {'png': 'image/png', 'jpg': 'image/jpeg', 'jpeg': 'image/jpeg',
               'gif': 'image/gif', 'webp': 'image/webp'}
    content_type = (f.mimetype or '').lower()
    if content_type not in VALID_IMAGE_TYPES:
        ext = f.filename.rsplit('.', 1)[-1].lower() if '.' in f.filename else ''
        content_type = ext_map.get(ext, '')
    if content_type not in VALID_IMAGE_TYPES:
        return jsonify({'error': 'File must be an image (PNG, JPG, GIF, or WebP)'}), 400

    data_uri = f'data:{content_type};base64,' + base64.b64encode(raw).decode('ascii')
    Setting.set('payments_zelle_qr_data', data_uri)
    AuditLog.record(current_user.id, 'settings.zelle_qr', 'Uploaded Zelle QR code')
    db.session.commit()
    return jsonify({'message': 'QR code uploaded', 'path': data_uri})


@bp.route('/settings/payments/zelle-qr', methods=['DELETE'])
@login_required
def delete_zelle_qr():
    """Remove the stored Zelle QR code."""
    err = _admin_only()
    if err:
        return err
    Setting.set('payments_zelle_qr_data', '')
    Setting.set('payments_zelle_qr_path', '')
    AuditLog.record(current_user.id, 'settings.zelle_qr', 'Removed Zelle QR code')
    db.session.commit()
    return jsonify({'message': 'QR code removed'})


@bp.route('/settings/payments/square-token', methods=['DELETE'])
@login_required
def clear_square_token():
    """Clear the stored Square access token."""
    err = _admin_only()
    if err:
        return err
    Setting.set('payments_square_access_token', '')
    AuditLog.record(current_user.id, 'settings.update', 'Cleared Square access token')
    db.session.commit()
    return jsonify({'message': 'Square access token cleared'})


@bp.route('/settings/payments/test-square', methods=['POST'])
@login_required
def test_square_connection():
    """Test the configured Square credentials against the Square API."""
    err = _admin_only()
    if err:
        return err
    ok, message = square_service.test_connection()
    return jsonify({'ok': ok, 'message': message}), (200 if ok else 400)


@bp.route('/payment-options', methods=['GET'])
@login_required
def get_payment_options():
    """Get enabled payment options for the parent portal (no sensitive data)."""
    options = []
    if Setting.get_bool('payments_zelle_enabled'):
        options.append({
            'type': 'zelle',
            'name': Setting.get('payments_zelle_name', 'Zelle'),
            'qr': Setting.get('payments_zelle_qr_data') or Setting.get('payments_zelle_qr_path', ''),
            'memo': Setting.get('payments_zelle_memo') or DEFAULT_ZELLE_MEMO,
        })
    if Setting.get_bool('payments_cashapp_enabled'):
        # A Cash App cashtag is alphanumeric/underscore; strip anything else so an
        # admin-set value can't inject into the unescaped href/URL it renders into
        # on the parent portal (defense-in-depth against an admin-account compromise).
        import re as _re
        tag = _re.sub(r'[^A-Za-z0-9_]', '', Setting.get('payments_cashapp_tag', '').lstrip('$'))[:20]
        options.append({
            'type': 'cashapp',
            'tag': f'${tag}' if tag else '',
            'cashtag': tag,
            'url': f'https://cash.app/${tag}' if tag else '',
        })
    if Setting.get_bool('payments_square_enabled'):
        # Re-validate on the way out, not just on write: this URL goes straight
        # into an href on the parent portal, and a value that bypassed the PUT
        # (direct DB edit, restored backup) must not reach parents.
        link, lerr = _valid_square_link(Setting.get('payments_square_link', ''))
        options.append({
            'type': 'square',
            'configured': square_service.is_configured(),
            'link': '' if lerr else link,
        })
    return jsonify({'payment_options': options})


@bp.route('/audit-log', methods=['GET'])
@login_required
def get_audit_log():
    """Recent audit entries (admin only)."""
    err = _admin_only()
    if err:
        return err
    limit = min(request.args.get('limit', 50, type=int), 200)
    rows = AuditLog.query.order_by(desc(AuditLog.created_at)).limit(limit).all()
    return jsonify({'entries': [{
        'id': r.id,
        'action': r.action,
        'detail': r.detail,
        'user': r.user.full_name if r.user else 'System',
        'created_at': _utc_iso(r.created_at),
    } for r in rows]})


# ── Pending payment (reconciliation) endpoints ──────────────────────

VALID_PAYMENT_METHODS = {'zelle', 'cashapp', 'square', 'cash', 'venmo', 'other'}
STUDIO_NAME = "LaShelle's School of Dance"


def _parent_student_ids(user) -> set:
    """Set of student ids a parent is linked to."""
    return {s.id for s in user.get_children()} if user.is_parent else set()


def _pending_to_dict(p) -> dict:
    if p.student:
        who = p.student.full_name
    elif p.family:
        who = f'{p.family.name} (family)'
    else:
        who = 'Unknown'
    return {
        'id': p.id,
        'student_id': p.student_id,
        'family_id': p.family_id,
        'who': who,
        'parent_name': p.parent.full_name if p.parent else None,
        'amount': f'{float(p.amount):.2f}',
        'method': p.method,
        'reference': p.reference,
        'note': p.note,
        'status': p.status,
        'admin_note': p.admin_note,
        'created_at': _utc_iso(p.created_at),
        'reviewed_at': _utc_iso(p.reviewed_at),
        'reviewed_by': p.reviewer.full_name if p.reviewer else None,
    }


def _send_receipt(parent_email, who, amount, method):
    """Best-effort payment receipt email. Never raises."""
    if not parent_email:
        return
    from app import email as email_service
    if not email_service.is_configured():
        return
    method_label = {'zelle': 'Zelle', 'cashapp': 'Cash App', 'square': 'Square',
                    'cash': 'cash', 'venmo': 'Venmo'}.get(method, method)
    body = (
        f"Hi,\n\n"
        f"This confirms we've received and recorded your payment of ${amount:.2f} "
        f"for {who} via {method_label}.\n\n"
        f"You can view your balance any time in the parent portal.\n\n"
        f"Thank you,\n{STUDIO_NAME}"
    )
    # Fire-and-forget: the receipt is best-effort and must not make the admin's
    # payment-confirm (or the Square webhook) wait on a slow SMTP send. Same
    # pattern as the admin-notify emails. The payment is already committed by
    # the time this runs, so a failed/slow receipt never affects the balance.
    _send_email_async([parent_email], f'Payment received — {STUDIO_NAME}', body)


@bp.route('/payments/claim', methods=['POST'])
@login_required
def claim_payment():
    """A parent reports a payment they've sent externally (awaits admin confirm)."""
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    method = _clean_str(data.get('method')).lower()
    if method not in VALID_PAYMENT_METHODS:
        return jsonify({'error': f'Invalid payment method. One of: {", ".join(sorted(VALID_PAYMENT_METHODS))}'}), 400

    # Same amount validation as admin charges — including the upper cap, so a
    # parent can't report an absurd (e.g. $999,999,999) payment into the inbox.
    amount, aerr = _valid_amount(data.get('amount'))
    if aerr:
        return aerr

    student_id = data.get('student_id')
    family_id = data.get('family_id')
    if not student_id and not family_id:
        return jsonify({'error': 'student_id or family_id is required'}), 400
    if student_id:
        student_id, serr = _valid_id(student_id)
        if serr:
            return serr
    if family_id:
        family_id, ferr = _valid_id(family_id)
        if ferr:
            return ferr

    # Authorization: parents may only claim for their own children/families
    if current_user.is_parent:
        my_students = _parent_student_ids(current_user)
        if student_id and student_id not in my_students:
            return jsonify({'error': 'Not authorized for this student'}), 403
        if family_id:
            my_families = {Student.query.get(sid).family_id for sid in my_students}
            if family_id not in my_families:
                return jsonify({'error': 'Not authorized for this family'}), 403
    elif not current_user.is_admin:
        # Billing is admin-only: teachers can't record payment claims on a
        # family's behalf (least privilege).
        return jsonify({'error': 'Not authorized'}), 403

    p = PendingPayment(
        student_id=student_id if student_id else None,
        family_id=family_id if family_id else None,
        parent_id=current_user.id,
        amount=amount,
        method=method,
        reference=_clean_str(data.get('reference'), 100) or None,  # parent input — cap length
        note=_clean_str(data.get('note'), 1000) or None,
    )
    db.session.add(p)
    db.session.commit()

    # Notify admins by email (best-effort)
    from app import email as email_service
    if email_service.is_configured():
        who = p.student.full_name if p.student else (p.family.name + ' (family)' if p.family else 'a family')
        admin_emails = {u.email for u in User.query.filter_by(role='admin', is_active=True).all() if u.email and '@' in u.email}
        if admin_emails:
            _send_email_async(
                admin_emails,
                f'New payment to confirm — {who}',
                f'{current_user.full_name} reported a {method} payment of ${amount:.2f} for {who}.\n\n'
                f'Reference: {p.reference or "(none)"}\n\nConfirm it in the Pending Payments page.',
            )

    return jsonify({'message': 'Payment reported — the studio will confirm it shortly.',
                    'pending_payment': _pending_to_dict(p)}), 201


@bp.route('/pending-payments', methods=['GET'])
@login_required
def list_pending_payments():
    err = _admin_only()
    if err:
        return err
    status = request.args.get('status', 'pending').strip()
    query = PendingPayment.query
    if status and status != 'all':
        query = query.filter_by(status=status)
    rows = query.order_by(desc(PendingPayment.created_at)).limit(200).all()
    return jsonify({'pending_payments': [_pending_to_dict(p) for p in rows]})


@bp.route('/pending-payments/count', methods=['GET'])
@login_required
def pending_payments_count():
    # Billing badge is admin-only; the count leaks payment activity volume.
    if not current_user.is_admin:
        return jsonify({'count': 0})
    return jsonify({'count': PendingPayment.query.filter_by(status='pending').count()})


@bp.route('/pending-payments/<int:pid>/confirm', methods=['POST'])
@login_required
def confirm_pending_payment(pid):
    err = _admin_only()
    if err:
        return err
    p = PendingPayment.query.get_or_404(pid)
    if p.status != 'pending':
        return jsonify({'error': f'Already {p.status}'}), 400

    data = request.get_json(silent=True) or {}
    category = (data.get('category') or 'tuition').strip()
    amount = float(p.amount)

    # Determine per-student allocation
    if p.student_id:
        allocations = [(p.student_id, amount)]
        receipt_email = p.student.parent_email or p.student.email
        who = p.student.full_name
    else:
        students = p.family.students.filter_by(is_active=True).all()
        allocations = allocate_family_payment([s.id for s in students], amount)
        if not allocations:
            # Nobody owed and no students — record against first student if any
            if students:
                allocations = [(students[0].id, amount)]
            else:
                return jsonify({'error': 'Family has no active students to credit'}), 400
        receipt_email = p.family.primary_email or (students[0].parent_email if students else None)
        who = f'{p.family.name} (family)'

    method_label = p.method
    desc_ref = f' (ref: {p.reference})' if p.reference else ''
    first_txn = None
    for sid, portion in allocations:
        t = Transaction(
            student_id=sid,
            type='payment',
            amount=portion,
            category=category,
            payment_method=method_label,
            description=f'Online payment via {method_label}{desc_ref}',
            transaction_date=date.today(),
            created_by=current_user.id,
        )
        db.session.add(t)
        if first_txn is None:
            db.session.flush()
            first_txn = t

    # Atomically claim this pending payment. Two concurrent confirms (an admin
    # double-click, or two open tabs) both pass the status check above before
    # either commits; without this guard both create payment transactions and
    # the family is credited twice. The conditional UPDATE re-checks status in
    # the DB at write time, so only the first confirm wins — the second matches
    # 0 rows and rolls back (its just-inserted transactions included) instead of
    # double-crediting.
    note = _clean_str(data['admin_note']) if data.get('admin_note') else p.admin_note
    claimed = db.session.query(PendingPayment).filter_by(
        id=pid, status='pending').update({
            'status': 'confirmed',
            'reviewed_at': datetime.utcnow(),
            'reviewed_by': current_user.id,
            'transaction_id': first_txn.id if first_txn else None,
            'admin_note': note,
        }, synchronize_session=False)
    if not claimed:
        db.session.rollback()
        return jsonify({'error': 'Already processed'}), 400

    AuditLog.record(current_user.id, 'payment.confirm',
                    f'Confirmed ${amount:.2f} {method_label} for {who}')
    db.session.commit()

    _send_receipt(receipt_email, who, amount, p.method)
    return jsonify({'message': f'Payment of ${amount:.2f} confirmed', 'pending_payment': _pending_to_dict(p)})


@bp.route('/pending-payments/<int:pid>/reject', methods=['POST'])
@login_required
def reject_pending_payment(pid):
    err = _admin_only()
    if err:
        return err
    p = PendingPayment.query.get_or_404(pid)
    if p.status != 'pending':
        return jsonify({'error': f'Already {p.status}'}), 400
    data = request.get_json(silent=True) or {}
    p.status = 'rejected'
    p.admin_note = _clean_str(data.get('admin_note')) or None
    p.reviewed_at = datetime.utcnow()
    p.reviewed_by = current_user.id
    who = p.student.full_name if p.student else (p.family.name + ' (family)' if p.family else 'unknown')
    AuditLog.record(current_user.id, 'payment.reject',
                    f'Rejected ${float(p.amount):.2f} {p.method} for {who}')
    db.session.commit()
    return jsonify({'message': 'Payment claim rejected', 'pending_payment': _pending_to_dict(p)})


@bp.route('/my-payments', methods=['GET'])
@login_required
def my_payments():
    """A parent's own pending claims + recent confirmed payments across children."""
    if not current_user.is_parent:
        return jsonify({'pending': [], 'history': []})

    student_ids = list(_parent_student_ids(current_user))
    pending = (PendingPayment.query
               .filter_by(parent_id=current_user.id)
               .order_by(desc(PendingPayment.created_at)).limit(50).all())

    history = []
    plans = []
    if student_ids:
        txns = (Transaction.query
                .filter(Transaction.student_id.in_(student_ids), Transaction.type == 'payment')
                .order_by(desc(Transaction.transaction_date), desc(Transaction.created_at))
                .limit(50).all())
        history = [transaction_to_dict(t) for t in txns]
        plan_rows = (PaymentPlan.query
                     .filter(PaymentPlan.student_id.in_(student_ids), PaymentPlan.is_active == True)  # noqa: E712
                     .all())
        plans = [_plan_to_dict(p) for p in plan_rows]

    return jsonify({
        'pending': [_pending_to_dict(p) for p in pending],
        'history': history,
        'plans': plans,
    })


@bp.route('/donation-info', methods=['GET'])
@login_required
def donation_info():
    """Whether the Foundation accepts donations via the portal (for parents)."""
    return jsonify({
        'enabled': Setting.get_bool('donations_enabled'),
        'org_name': Setting.get('donations_org_name', '') or 'LSODance Foundation',
    })


# ── Balance reminder emails ─────────────────────────────────────────

def _reminder_body(name, balance):
    return (
        f"Hi,\n\n"
        f"This is a friendly reminder that {name} has an outstanding balance of "
        f"${balance:.2f} with {STUDIO_NAME}.\n\n"
        f"You can pay any time through the parent portal. Thank you!\n\n"
        f"{STUDIO_NAME}"
    )


def _student_phone(s):
    return s.parent_phone or (s.family.primary_phone if s.family else None) or s.phone


def _notify_student_balance(s, balance, email_ok, sms_ok):
    """Send a balance reminder to one student's parent via the available channels.
    Returns a set of channels actually used."""
    from app import email as email_service
    from app import sms as sms_service
    used = set()
    body = _reminder_body(s.full_name, balance)
    if email_ok:
        to = s.parent_email or s.email
        if to:
            try:
                email_service.send_email(to, f'Balance reminder — {STUDIO_NAME}', body)
                used.add('email')
            except Exception:
                logger.exception("Reminder email failed for student #%s", s.id)
    if sms_ok:
        phone = _student_phone(s)
        if phone and sms_service.send_sms(phone, body):
            used.add('sms')
    return used


def _send_reminders_to(app, student_ids, email_ok, sms_ok):
    """Background worker for the manual 'remind everyone who owes' action. Runs
    off the request thread because a large studio × (email + up to a 15s SMS)
    would exceed the 120s gunicorn worker timeout if sent inline."""
    with app.app_context():
        try:
            students = Student.query.filter(Student.id.in_(student_ids)).all()
            balances = calc_balance_bulk([s.id for s in students])
            notified = 0
            for s in students:
                bal = balances[s.id]['balance']
                if bal <= 0:
                    continue
                if _notify_student_balance(s, bal, email_ok, sms_ok):
                    notified += 1
            logger.info("Manual balance reminders sent: %d", notified)
        except Exception:
            logger.exception("Manual reminder background send failed")


@bp.route('/balances/send-reminders', methods=['POST'])
@login_required
def send_balance_reminders():
    """Queue a balance reminder to every student who owes, via email and/or SMS.
    The actual sending runs in a background thread — for a large studio, sending
    inline (an email plus a 15s-timeout SMS per family) would blow the 120s
    gunicorn worker timeout and 502 the request."""
    err = _admin_only()
    if err:
        return err
    from app import email as email_service
    from app import sms as sms_service
    email_ok = email_service.is_configured()
    sms_ok = sms_service.is_configured()
    if not email_ok and not sms_ok:
        return jsonify({'error': 'Configure email (SMTP) or SMS (Twilio) first'}), 400

    students = Student.query.filter_by(is_active=True).all()
    balances = calc_balance_bulk([s.id for s in students])
    owing = [s.id for s in students if balances[s.id]['balance'] > 0]

    AuditLog.record(current_user.id, 'reminders.send', f'Queued {len(owing)} balance reminders')
    db.session.commit()

    app = current_app._get_current_object()
    threading.Thread(target=_send_reminders_to, args=(app, owing, email_ok, sms_ok),
                     daemon=True).start()
    return jsonify({'message': f'Sending reminders to {len(owing)} family(ies)…', 'queued': len(owing)})


@bp.route('/students/<int:student_id>/send-reminder', methods=['POST'])
@login_required
def send_student_reminder(student_id):
    err = _admin_only()
    if err:
        return err
    from app import email as email_service
    from app import sms as sms_service
    email_ok = email_service.is_configured()
    sms_ok = sms_service.is_configured()
    if not email_ok and not sms_ok:
        return jsonify({'error': 'Configure email (SMTP) or SMS (Twilio) first'}), 400
    student = Student.query.get_or_404(student_id)
    bal = calc_balance(student_id)['balance']
    if bal <= 0:
        return jsonify({'error': 'No outstanding balance'}), 400
    used = _notify_student_balance(student, bal, email_ok, sms_ok)
    if not used:
        return jsonify({'error': 'No email or phone on file for this student'}), 400
    AuditLog.record(current_user.id, 'reminders.send', f'Sent reminder to {student.full_name} via {", ".join(used)}')
    db.session.commit()
    return jsonify({'message': f'Reminder sent via {", ".join(used)}'})


# ── Square webhook (auto-reconcile online invoice payments) ──────────

@bp.route('/webhooks/square', methods=['POST'])
def square_webhook():
    """Receive Square invoice payment events and auto-record the payment.

    No login (Square calls this). Verified via HMAC signature when a signature
    key is configured.
    """
    import base64
    import hashlib
    import hmac
    import json

    raw_body = request.get_data()

    # Fail closed: never auto-record a payment from an UNVERIFIED webhook. This
    # endpoint reduces a family's balance, so without a configured signature key
    # we cannot trust the caller — an attacker who learned a real invoice id could
    # otherwise forge a "PAID" event and credit an account. Return 200 so Square
    # doesn't retry; the studio must set the signature key to enable auto-reconcile
    # (until then, Square payments are reconciled manually via /pending-payments).
    from app.crypto import decrypt
    sig_key = decrypt(Setting.get('payments_square_webhook_signature_key', ''))
    if not sig_key:
        logger.warning("Square webhook received but no signature key configured — not auto-recording")
        return jsonify({'status': 'unverified_ignored'}), 200
    provided = request.headers.get('x-square-hmacsha256-signature', '')
    mac = hmac.new(sig_key.encode('utf-8'), (request.url + raw_body.decode('utf-8')).encode('utf-8'),
                   hashlib.sha256)
    expected = base64.b64encode(mac.digest()).decode('ascii')
    if not hmac.compare_digest(expected, provided):
        logger.warning("Square webhook signature mismatch")
        return jsonify({'error': 'Invalid signature'}), 403

    try:
        event = json.loads(raw_body or b'{}')
    except ValueError:
        return jsonify({'error': 'Invalid JSON'}), 400

    event_type = event.get('type', '')
    invoice = (event.get('data', {}).get('object', {}) or {}).get('invoice', {})
    invoice_id = invoice.get('id')
    status = invoice.get('status', '')

    # Only auto-record a FULLY paid invoice. A PARTIALLY_PAID event must be
    # ignored — recording it here would book the whole invoice amount (not the
    # partial payment) AND set paid_at, which would then swallow the eventual
    # PAID event. Partial payments are handled by the later PAID event (once the
    # invoice is fully settled) or by manual reconciliation.
    if not invoice_id or status != 'PAID':
        return jsonify({'status': 'ignored'}), 200

    rec = SquareInvoice.query.filter_by(invoice_id=invoice_id).first()
    if not rec:
        logger.info("Square webhook for unknown invoice %s", invoice_id)
        return jsonify({'status': 'unknown_invoice'}), 200
    if rec.paid_at:
        return jsonify({'status': 'already_recorded'}), 200

    student = Student.query.get(rec.student_id)
    if not student:
        return jsonify({'status': 'unknown_student'}), 200

    amount = rec.amount_cents / 100.0
    t = Transaction(
        student_id=student.id,
        type='payment',
        amount=amount,
        category='tuition',
        payment_method='square',
        description=f'Square invoice {invoice_id} ({event_type})',
        transaction_date=date.today(),
        created_by=None,
    )
    db.session.add(t)
    # Atomically claim the invoice so a duplicate PAID delivery can't double-credit
    # the family. Square delivers webhooks at-least-once, so two identical PAID
    # events can be processed concurrently by different worker threads — both would
    # pass the paid_at pre-check above and each insert a payment. The conditional
    # UPDATE re-checks paid_at IS NULL at write time: only the first wins; the loser
    # matches 0 rows and rolls back its just-inserted payment (autoflushed before
    # the UPDATE runs). Same pattern as the pending-payment confirm race.
    claimed = db.session.query(SquareInvoice).filter(
        SquareInvoice.id == rec.id, SquareInvoice.paid_at.is_(None)).update(
        {'status': status, 'paid_at': datetime.utcnow()}, synchronize_session=False)
    if not claimed:
        db.session.rollback()
        return jsonify({'status': 'already_recorded'}), 200
    AuditLog.record(None, 'payment.square_webhook',
                    f'Auto-recorded ${amount:.2f} for {student.full_name} (invoice {invoice_id})')
    db.session.commit()

    _send_receipt(student.parent_email or student.email, student.full_name, amount, 'square')
    return jsonify({'status': 'recorded'}), 200


# ── Performance Company endpoints ───────────────────────────────────

def _group_to_dict(g):
    return {
        'id': g.id,
        'name': g.name,
        'description': g.description,
        'is_active': g.is_active,
        'member_count': g.memberships.filter_by(is_active=True).count(),
    }


def _performance_to_dict(p):
    return {
        'id': p.id,
        'group_id': p.group_id,
        'group_name': p.group.name if p.group else 'Studio-wide',
        'title': p.title,
        'performance_date': p.performance_date.isoformat() if p.performance_date else None,
        'call_time': p.call_time,
        'venue': p.venue,
        'description': p.description,
        'assignment_count': p.assignments.count(),
        'ticket_types': [{
            'id': t.id, 'name': t.name, 'price': f'{float(t.price):.2f}',
        } for t in p.ticket_types],
    }


@bp.route('/performance/groups', methods=['GET'])
@login_required
def list_groups():
    if not current_user.is_staff:
        return jsonify({'error': 'Staff access required'}), 403
    groups = PerformanceGroup.query.filter_by(is_active=True).order_by(PerformanceGroup.name).all()
    return jsonify({'groups': [_group_to_dict(g) for g in groups]})


@bp.route('/performance/groups', methods=['POST'])
@login_required
def create_group():
    err = _admin_only()
    if err:
        return err
    data = request.get_json() or {}
    name = _clean_str(data.get('name'))
    if not name:
        return jsonify({'error': 'name is required'}), 400
    g = PerformanceGroup(name=name, description=_clean_str(data.get('description')) or None)
    db.session.add(g)
    db.session.commit()
    return jsonify(_group_to_dict(g)), 201


@bp.route('/performance/groups/<int:gid>', methods=['PUT'])
@login_required
def update_group(gid):
    err = _admin_only()
    if err:
        return err
    g = PerformanceGroup.query.get_or_404(gid)
    data = request.get_json() or {}
    if 'name' in data and _clean_str(data['name']):
        g.name = _clean_str(data['name'])
    if 'description' in data:
        g.description = _clean_str(data['description']) or None
    db.session.commit()
    return jsonify(_group_to_dict(g))


@bp.route('/performance/groups/<int:gid>', methods=['DELETE'])
@login_required
def delete_group(gid):
    err = _admin_only()
    if err:
        return err
    g = PerformanceGroup.query.get_or_404(gid)
    g.is_active = False
    db.session.commit()
    return jsonify({'message': f'{g.name} archived'})


@bp.route('/performance/groups/<int:gid>/members', methods=['GET'])
@login_required
def list_group_members(gid):
    if not current_user.is_staff:
        return jsonify({'error': 'Staff access required'}), 403
    g = PerformanceGroup.query.get_or_404(gid)
    members = g.memberships.filter_by(is_active=True).all()
    return jsonify({'members': [{
        'id': m.id,
        'student_id': m.student_id,
        'student_name': m.student.full_name if m.student else None,
        'role': m.role,
        'joined_date': m.joined_date.isoformat() if m.joined_date else None,
    } for m in members]})


@bp.route('/performance/groups/<int:gid>/members', methods=['POST'])
@login_required
def add_group_member(gid):
    err = _admin_only()
    if err:
        return err
    g = PerformanceGroup.query.get_or_404(gid)
    data = request.get_json() or {}
    student_id, serr = _valid_id(data.get('student_id'))
    if serr:
        return serr
    if Student.query.get(student_id) is None:
        return jsonify({'error': 'student not found'}), 404
    existing = CompanyMembership.query.filter_by(group_id=gid, student_id=student_id).first()
    if existing:
        existing.is_active = True
        existing.role = (data.get('role') or existing.role).strip()
        db.session.commit()
        return jsonify({'message': 'Member reactivated'}), 200
    m = CompanyMembership(group_id=gid, student_id=student_id,
                          role=(data.get('role') or 'Member').strip())
    db.session.add(m)
    db.session.commit()
    return jsonify({'message': 'Member added', 'id': m.id}), 201


@bp.route('/performance/members/<int:mid>', methods=['DELETE'])
@login_required
def remove_group_member(mid):
    err = _admin_only()
    if err:
        return err
    m = CompanyMembership.query.get_or_404(mid)
    m.is_active = False
    db.session.commit()
    return jsonify({'message': 'Member removed'})


@bp.route('/performance/auditions', methods=['GET'])
@login_required
def list_auditions():
    if not current_user.is_staff:
        return jsonify({'error': 'Staff access required'}), 403
    rows = Audition.query.order_by(desc(Audition.created_at)).all()
    return jsonify({'auditions': [{
        'id': a.id,
        'group_id': a.group_id,
        'group_name': a.group.name if a.group else None,
        'title': a.title,
        'audition_date': a.audition_date.isoformat() if a.audition_date else None,
        'location_text': a.location_text,
        'description': a.description,
        'is_open': a.is_open,
        'signup_count': a.signups.count(),
    } for a in rows]})


@bp.route('/performance/auditions', methods=['POST'])
@login_required
def create_audition():
    err = _admin_only()
    if err:
        return err
    data = request.get_json() or {}
    if not data.get('title'):
        return jsonify({'error': 'title is required'}), 400
    a = Audition(
        group_id=_opt_int(data.get('group_id')),
        title=_clean_str(data['title']),
        audition_date=_parse_date(data.get('audition_date')),
        location_text=_clean_str(data.get('location_text')) or None,
        description=_clean_str(data.get('description')) or None,
        is_open=bool(data.get('is_open', True)),
    )
    db.session.add(a)
    db.session.commit()
    return jsonify({'message': 'Audition created', 'id': a.id}), 201


@bp.route('/performance/auditions/<int:aid>', methods=['PUT'])
@login_required
def update_audition(aid):
    err = _admin_only()
    if err:
        return err
    a = Audition.query.get_or_404(aid)
    data = request.get_json() or {}
    if 'title' in data and _clean_str(data['title']):
        a.title = _clean_str(data['title'])
    if 'group_id' in data:
        a.group_id = _opt_int(data.get('group_id'))
    if 'audition_date' in data:
        a.audition_date = _parse_date(data.get('audition_date'))
    if 'location_text' in data:
        a.location_text = _clean_str(data['location_text']) or None
    if 'description' in data:
        a.description = _clean_str(data['description']) or None
    if 'is_open' in data:
        a.is_open = bool(data['is_open'])
    db.session.commit()
    return jsonify({'message': 'Audition updated'})


@bp.route('/performance/auditions/<int:aid>', methods=['DELETE'])
@login_required
def delete_audition(aid):
    err = _admin_only()
    if err:
        return err
    a = Audition.query.get_or_404(aid)
    AuditionSignup.query.filter_by(audition_id=aid).delete()
    db.session.delete(a)
    db.session.commit()
    return jsonify({'message': 'Audition deleted'})


@bp.route('/performance/auditions/<int:aid>/signups', methods=['GET'])
@login_required
def list_audition_signups(aid):
    if not current_user.is_staff:
        return jsonify({'error': 'Staff access required'}), 403
    a = Audition.query.get_or_404(aid)
    return jsonify({'signups': [{
        'id': s.id,
        'student_id': s.student_id,
        'student_name': s.student.full_name if s.student else None,
        'status': s.status,
        'notes': s.notes,
        'created_at': _utc_iso(s.created_at),
    } for s in a.signups.order_by(AuditionSignup.created_at).all()]})


@bp.route('/performance/auditions/<int:aid>/signup', methods=['POST'])
@login_required
def signup_for_audition(aid):
    """A parent signs their child up for an audition."""
    a = Audition.query.get_or_404(aid)
    if not a.is_open:
        return jsonify({'error': 'This audition is closed'}), 400
    data = request.get_json() or {}
    student_id, err = _valid_id(data.get('student_id'))
    if err:
        return err
    if Student.query.get(student_id) is None:
        return jsonify({'error': 'student not found'}), 404
    if current_user.is_parent and student_id not in _parent_student_ids(current_user):
        return jsonify({'error': 'Not authorized for this student'}), 403
    if AuditionSignup.query.filter_by(audition_id=aid, student_id=student_id).first():
        return jsonify({'error': 'Already signed up'}), 400
    s = AuditionSignup(audition_id=aid, student_id=student_id, parent_id=current_user.id,
                       notes=_clean_str(data.get('notes')) or None)
    db.session.add(s)
    db.session.commit()
    return jsonify({'message': 'Signed up for audition'}), 201


@bp.route('/performance/signups/<int:sid>/status', methods=['POST'])
@login_required
def set_signup_status(sid):
    err = _admin_only()
    if err:
        return err
    s = AuditionSignup.query.get_or_404(sid)
    data = request.get_json() or {}
    status = _clean_str(data.get('status'))
    if status not in ('signed_up', 'accepted', 'declined', 'waitlist'):
        return jsonify({'error': 'Invalid status'}), 400
    s.status = status
    # Auto-add accepted students to the audition's group as members
    if status == 'accepted' and s.audition.group_id:
        existing = CompanyMembership.query.filter_by(group_id=s.audition.group_id, student_id=s.student_id).first()
        if existing:
            existing.is_active = True
        else:
            db.session.add(CompanyMembership(group_id=s.audition.group_id, student_id=s.student_id))
    db.session.commit()
    return jsonify({'message': f'Marked {status}'})


@bp.route('/performance/performances', methods=['GET'])
@login_required
def list_performances():
    if not current_user.is_staff:
        return jsonify({'error': 'Staff access required'}), 403
    rows = Performance.query.order_by(desc(Performance.performance_date)).all()
    return jsonify({'performances': [_performance_to_dict(p) for p in rows]})


@bp.route('/performance/performances', methods=['POST'])
@login_required
def create_performance():
    err = _admin_only()
    if err:
        return err
    data = request.get_json() or {}
    if not data.get('title'):
        return jsonify({'error': 'title is required'}), 400
    p = Performance(
        group_id=_opt_int(data.get('group_id')),
        title=_clean_str(data['title']),
        performance_date=_parse_date(data.get('performance_date')),
        call_time=_clean_str(data.get('call_time')) or None,
        venue=_clean_str(data.get('venue')) or None,
        description=_clean_str(data.get('description')) or None,
    )
    db.session.add(p)
    db.session.commit()
    return jsonify(_performance_to_dict(p)), 201


@bp.route('/performance/performances/<int:pid>', methods=['PUT'])
@login_required
def update_performance(pid):
    err = _admin_only()
    if err:
        return err
    p = Performance.query.get_or_404(pid)
    data = request.get_json() or {}
    if 'title' in data and _clean_str(data['title']):
        p.title = _clean_str(data['title'])
    if 'group_id' in data:
        p.group_id = _opt_int(data.get('group_id'))
    if 'performance_date' in data:
        p.performance_date = _parse_date(data.get('performance_date'))
    if 'call_time' in data:
        p.call_time = _clean_str(data['call_time']) or None
    if 'venue' in data:
        p.venue = _clean_str(data['venue']) or None
    if 'description' in data:
        p.description = _clean_str(data['description']) or None
    db.session.commit()
    return jsonify(_performance_to_dict(p))


@bp.route('/performance/performances/<int:pid>', methods=['DELETE'])
@login_required
def delete_performance(pid):
    err = _admin_only()
    if err:
        return err
    p = Performance.query.get_or_404(pid)
    # Delete children whose FK to the performance (or its ticket types) is NOT
    # NULL — otherwise SQLAlchemy tries to null them on delete and hits the
    # constraint (a performance with ticket types couldn't be deleted at all).
    PerformanceAssignment.query.filter_by(performance_id=pid).delete(synchronize_session=False)
    tt_ids = [t.id for t in TicketType.query.filter_by(performance_id=pid).all()]
    if tt_ids:
        TicketOrder.query.filter(TicketOrder.ticket_type_id.in_(tt_ids)).delete(synchronize_session=False)
        TicketType.query.filter_by(performance_id=pid).delete(synchronize_session=False)
    db.session.delete(p)
    db.session.commit()
    return jsonify({'message': 'Performance deleted'})


@bp.route('/performance/performances/<int:pid>/assignments', methods=['GET'])
@login_required
def list_assignments(pid):
    if not current_user.is_staff:
        return jsonify({'error': 'Staff access required'}), 403
    p = Performance.query.get_or_404(pid)
    return jsonify({'assignments': [{
        'id': a.id,
        'student_id': a.student_id,
        'student_name': a.student.full_name if a.student else None,
        'notes': a.notes,
    } for a in p.assignments.all()]})


@bp.route('/performance/performances/<int:pid>/assignments', methods=['POST'])
@login_required
def add_assignment(pid):
    err = _admin_only()
    if err:
        return err
    p = Performance.query.get_or_404(pid)
    data = request.get_json() or {}
    # Bulk: assign all active members of the performance's group
    if data.get('assign_group') and p.group_id:
        added = 0
        for m in p.group.memberships.filter_by(is_active=True).all():
            if not PerformanceAssignment.query.filter_by(performance_id=pid, student_id=m.student_id).first():
                db.session.add(PerformanceAssignment(performance_id=pid, student_id=m.student_id))
                added += 1
        db.session.commit()
        return jsonify({'message': f'Assigned {added} group members'}), 201
    student_id, serr = _valid_id(data.get('student_id'))
    if serr:
        return serr
    if Student.query.get(student_id) is None:
        return jsonify({'error': 'student not found'}), 404
    if PerformanceAssignment.query.filter_by(performance_id=pid, student_id=student_id).first():
        return jsonify({'error': 'Already assigned'}), 400
    a = PerformanceAssignment(performance_id=pid, student_id=student_id,
                              notes=_clean_str(data.get('notes')) or None)
    db.session.add(a)
    db.session.commit()
    return jsonify({'message': 'Assigned', 'id': a.id}), 201


@bp.route('/performance/assignments/<int:aid>', methods=['DELETE'])
@login_required
def remove_assignment(aid):
    err = _admin_only()
    if err:
        return err
    a = PerformanceAssignment.query.get_or_404(aid)
    db.session.delete(a)
    db.session.commit()
    return jsonify({'message': 'Removed'})


@bp.route('/my-company', methods=['GET'])
@login_required
def my_company():
    """A parent's children's company memberships, upcoming performances, open auditions."""
    if not current_user.is_parent:
        return jsonify({'memberships': [], 'performances': [], 'open_auditions': []})
    student_ids = _parent_student_ids(current_user)
    if not student_ids:
        return jsonify({'memberships': [], 'performances': [], 'open_auditions': []})

    memberships = (CompanyMembership.query
                   .filter(CompanyMembership.student_id.in_(student_ids), CompanyMembership.is_active == True)  # noqa: E712
                   .all())
    group_ids = {m.group_id for m in memberships}
    members_out = [{
        'student_id': m.student_id,
        'student_name': m.student.full_name if m.student else None,
        'group_name': m.group.name,
        'role': m.role,
    } for m in memberships]

    today = date.today()
    # Performances for their groups, studio-wide events, or where their child is individually assigned
    assigned_perf_ids = {a.performance_id for a in PerformanceAssignment.query
                         .filter(PerformanceAssignment.student_id.in_(student_ids)).all()}
    perfs = Performance.query.filter(
        db.or_(
            Performance.group_id.in_(group_ids) if group_ids else db.false(),
            Performance.group_id.is_(None),
            Performance.id.in_(assigned_perf_ids) if assigned_perf_ids else db.false(),
        )
    ).all()
    perfs = [p for p in perfs if (p.performance_date is None or p.performance_date >= today)]
    perfs.sort(key=lambda p: (p.performance_date or date.max))

    open_auditions = [{
        'id': a.id,
        'title': a.title,
        'group_name': a.group.name if a.group else None,
        'audition_date': a.audition_date.isoformat() if a.audition_date else None,
        'location_text': a.location_text,
        'description': a.description,
        'children': [{
            'student_id': sid,
            'student_name': Student.query.get(sid).full_name,
            'signed_up': bool(AuditionSignup.query.filter_by(audition_id=a.id, student_id=sid).first()),
        } for sid in student_ids],
    } for a in Audition.query.filter_by(is_open=True).order_by(Audition.audition_date).all()]

    return jsonify({
        'memberships': members_out,
        'performances': [_performance_to_dict(p) for p in perfs],
        'open_auditions': open_auditions,
    })


# ── Waivers & forms endpoints ───────────────────────────────────────

def _waiver_template_to_dict(t):
    return {
        'id': t.id,
        'title': t.title,
        'body': t.body,
        'allow_decline': t.allow_decline,
        'display_order': t.display_order,
        'is_active': t.is_active,
        'signed_count': t.signatures.count(),
    }


@bp.route('/waivers/templates', methods=['GET'])
@login_required
def list_waiver_templates():
    if not current_user.is_staff:
        return jsonify({'error': 'Staff access required'}), 403
    include_inactive = request.args.get('all') == '1'
    q = WaiverTemplate.query
    if not include_inactive:
        q = q.filter_by(is_active=True)
    rows = q.order_by(WaiverTemplate.display_order, WaiverTemplate.id).all()
    return jsonify({'templates': [_waiver_template_to_dict(t) for t in rows]})


@bp.route('/waivers/templates', methods=['POST'])
@login_required
def create_waiver_template():
    err = _admin_only()
    if err:
        return err
    data = request.get_json() or {}
    if not data.get('title') or not data.get('body'):
        return jsonify({'error': 'title and body are required'}), 400
    t = WaiverTemplate(
        title=_clean_str(data['title']),
        body=_clean_str(data['body']),
        allow_decline=bool(data.get('allow_decline', False)),
        display_order=int(data.get('display_order', 0)),
    )
    db.session.add(t)
    db.session.commit()
    return jsonify(_waiver_template_to_dict(t)), 201


@bp.route('/waivers/templates/<int:tid>', methods=['PUT'])
@login_required
def update_waiver_template(tid):
    err = _admin_only()
    if err:
        return err
    t = WaiverTemplate.query.get_or_404(tid)
    data = request.get_json() or {}
    if 'title' in data and _clean_str(data['title']):
        t.title = _clean_str(data['title'])
    if 'body' in data and _clean_str(data['body']):
        t.body = _clean_str(data['body'])
    if 'allow_decline' in data:
        t.allow_decline = bool(data['allow_decline'])
    if 'display_order' in data:
        t.display_order = _opt_num(data['display_order'], t.display_order)
    if 'is_active' in data:
        t.is_active = bool(data['is_active'])
    db.session.commit()
    return jsonify(_waiver_template_to_dict(t))


@bp.route('/waivers/templates/<int:tid>', methods=['DELETE'])
@login_required
def delete_waiver_template(tid):
    err = _admin_only()
    if err:
        return err
    t = WaiverTemplate.query.get_or_404(tid)
    t.is_active = False
    db.session.commit()
    return jsonify({'message': f'{t.title} archived'})


@bp.route('/waivers/compliance', methods=['GET'])
@login_required
def waiver_compliance():
    """Per-template: how many active students have signed, and who hasn't."""
    if not current_user.is_staff:
        return jsonify({'error': 'Staff access required'}), 403
    students = Student.query.filter_by(is_active=True).all()
    total = len(students)
    templates = WaiverTemplate.query.filter_by(is_active=True).order_by(WaiverTemplate.display_order).all()
    out = []
    for t in templates:
        signed = {s.student_id: s for s in t.signatures.all()}
        unsigned = [{'student_id': s.id, 'student_name': s.full_name} for s in students if s.id not in signed]
        # Guard the student deref: a signature whose student was removed would
        # otherwise 500 this staff page (same orphan-safety as the rosters).
        declined = []
        for sid, sig in signed.items():
            if sig.consent:
                continue
            st = sig.student if sig.student is not None else Student.query.get(sid)
            declined.append({'student_id': sid,
                             'student_name': st.full_name if st else '(removed student)'})
        out.append({
            'id': t.id,
            'title': t.title,
            'allow_decline': t.allow_decline,
            'signed_count': len([1 for sid in signed if sid in {s.id for s in students}]),
            'total': total,
            'unsigned': unsigned,
            'declined': declined,
        })
    return jsonify({'compliance': out})


@bp.route('/students/<int:student_id>/waivers', methods=['GET'])
@login_required
def get_student_waivers(student_id):
    """List active waiver templates and this student's signature status."""
    if current_user.is_parent and student_id not in _parent_student_ids(current_user):
        return jsonify({'error': 'Not authorized'}), 403
    student = Student.query.get_or_404(student_id)
    templates = WaiverTemplate.query.filter_by(is_active=True).order_by(WaiverTemplate.display_order).all()
    out = []
    for t in templates:
        sig = WaiverSignature.query.filter_by(template_id=t.id, student_id=student_id).first()
        out.append({
            'id': t.id,
            'title': t.title,
            'body': t.body,
            'allow_decline': t.allow_decline,
            'signed': sig is not None,
            'consent': sig.consent if sig else None,
            'signed_name': sig.signed_name if sig else None,
            'signed_at': _utc_iso(sig.signed_at) if sig else None,
        })
    return jsonify({'student_id': student.id, 'student_name': student.full_name, 'waivers': out})


@bp.route('/students/<int:student_id>/waivers/<int:template_id>/sign', methods=['POST'])
@login_required
def sign_waiver(student_id, template_id):
    if current_user.is_parent and student_id not in _parent_student_ids(current_user):
        return jsonify({'error': 'Not authorized'}), 403
    Student.query.get_or_404(student_id)
    t = WaiverTemplate.query.get_or_404(template_id)
    data = request.get_json() or {}
    signed_name = _clean_str(data.get('signed_name'))
    if not signed_name:
        return jsonify({'error': 'A typed signature (your name) is required'}), 400
    consent = bool(data.get('consent', True))
    if not consent and not t.allow_decline:
        return jsonify({'error': 'This form requires agreement to participate'}), 400

    sig = WaiverSignature.query.filter_by(template_id=template_id, student_id=student_id).first()
    if sig:
        sig.signed_name = signed_name
        sig.consent = consent
        sig.parent_id = current_user.id
        sig.signed_at = datetime.utcnow()
    else:
        sig = WaiverSignature(template_id=template_id, student_id=student_id, parent_id=current_user.id,
                              signed_name=signed_name, consent=consent)
        db.session.add(sig)
    db.session.commit()
    return jsonify({'message': 'Signed', 'consent': consent})


# ── Recital: costumes ───────────────────────────────────────────────

COSTUME_SIZE_FIELDS = [
    'leotard_size', 'dress_size', 'shirt_size', 'pants_size', 'shoe_size',
    'height', 'weight', 'girth', 'waist', 'hips', 'inseam',
]


def _costume_to_dict(c):
    assigns = c.assignments.all()
    return {
        'id': c.id,
        'name': c.name,
        'class_id': c.class_id,
        'class_name': c.dance_class.name if c.dance_class else None,
        'group_id': c.group_id,
        'group_name': c.group.name if c.group else None,
        'vendor': c.vendor,
        'fee': f'{float(c.fee):.2f}',
        'notes': c.notes,
        'assigned_count': len(assigns),
        'charged_count': sum(1 for a in assigns if a.charged),
        'paid_count': sum(1 for a in assigns if a.paid),
    }


@bp.route('/costumes', methods=['GET'])
@login_required
def list_costumes():
    if not current_user.is_staff:
        return jsonify({'error': 'Staff access required'}), 403
    rows = Costume.query.filter_by(is_active=True).order_by(Costume.created_at.desc()).all()
    return jsonify({'costumes': [_costume_to_dict(c) for c in rows]})


@bp.route('/costumes', methods=['POST'])
@login_required
def create_costume():
    err = _admin_only()
    if err:
        return err
    data = request.get_json() or {}
    if not data.get('name'):
        return jsonify({'error': 'name is required'}), 400
    c = Costume(
        name=_clean_str(data['name']),
        class_id=_opt_int(data.get('class_id')),
        group_id=_opt_int(data.get('group_id')),
        vendor=_clean_str(data.get('vendor')) or None,
        fee=data.get('fee') or 0,
        notes=_clean_str(data.get('notes')) or None,
    )
    db.session.add(c)
    db.session.commit()
    return jsonify(_costume_to_dict(c)), 201


@bp.route('/costumes/<int:cid>', methods=['PUT'])
@login_required
def update_costume(cid):
    err = _admin_only()
    if err:
        return err
    c = Costume.query.get_or_404(cid)
    data = request.get_json() or {}
    if 'name' in data and _clean_str(data['name']):
        c.name = _clean_str(data['name'])
    if 'vendor' in data:
        c.vendor = _clean_str(data['vendor']) or None
    if 'fee' in data:
        try:  # 0 is valid (free costume); garbage -> 400 via the existing guard
            c.fee = round(float(data['fee'] or 0), 2)
        except (TypeError, ValueError):
            pass
    if 'class_id' in data:
        c.class_id = _opt_int(data['class_id'])
    if 'group_id' in data:
        c.group_id = _opt_int(data['group_id'])
    if 'notes' in data:
        c.notes = _clean_str(data['notes']) or None
    db.session.commit()
    return jsonify(_costume_to_dict(c))


@bp.route('/costumes/<int:cid>', methods=['DELETE'])
@login_required
def delete_costume(cid):
    err = _admin_only()
    if err:
        return err
    c = Costume.query.get_or_404(cid)
    c.is_active = False
    db.session.commit()
    return jsonify({'message': f'{c.name} archived'})


@bp.route('/costumes/<int:cid>/assignments', methods=['GET'])
@login_required
def list_costume_assignments(cid):
    if not current_user.is_staff:
        return jsonify({'error': 'Staff access required'}), 403
    c = Costume.query.get_or_404(cid)
    out = []
    for a in c.assignments.all():
        s = a.student
        out.append({
            'id': a.id,
            'student_id': a.student_id,
            'student_name': s.full_name,
            'size': a.size,
            'charged': a.charged,
            'paid': a.paid,
            'measurements': {f: getattr(s, f) for f in COSTUME_SIZE_FIELDS if getattr(s, f)},
        })
    out.sort(key=lambda x: x['student_name'])
    return jsonify({'assignments': out})


@bp.route('/costumes/<int:cid>/assignments', methods=['POST'])
@login_required
def add_costume_assignment(cid):
    err = _admin_only()
    if err:
        return err
    c = Costume.query.get_or_404(cid)
    data = request.get_json() or {}

    # Bulk: assign everyone in the linked class or group
    if data.get('assign_all'):
        student_ids = []
        if c.class_id:
            student_ids = [e.student_id for e in ClassEnrollment.query.filter_by(class_id=c.class_id, is_active=True).all()]
        elif c.group_id:
            student_ids = [m.student_id for m in CompanyMembership.query.filter_by(group_id=c.group_id, is_active=True).all()]
        if not student_ids:
            return jsonify({'error': 'No class/company linked, or it has no members'}), 400
        added = 0
        for sid in student_ids:
            if not CostumeAssignment.query.filter_by(costume_id=cid, student_id=sid).first():
                db.session.add(CostumeAssignment(costume_id=cid, student_id=sid))
                added += 1
        db.session.commit()
        return jsonify({'message': f'Assigned {added} dancers'}), 201

    student_id, serr = _valid_id(data.get('student_id'))
    if serr:
        return serr
    if Student.query.get(student_id) is None:
        return jsonify({'error': 'student not found'}), 404
    if CostumeAssignment.query.filter_by(costume_id=cid, student_id=student_id).first():
        return jsonify({'error': 'Already assigned'}), 400
    a = CostumeAssignment(costume_id=cid, student_id=student_id,
                          size=_clean_str(data.get('size')) or None)
    db.session.add(a)
    db.session.commit()
    return jsonify({'message': 'Assigned', 'id': a.id}), 201


@bp.route('/costume-assignments/<int:aid>', methods=['PUT'])
@login_required
def update_costume_assignment(aid):
    err = _admin_only()
    if err:
        return err
    a = CostumeAssignment.query.get_or_404(aid)
    data = request.get_json() or {}
    if 'size' in data:
        a.size = _clean_str(data['size']) or None
    if 'paid' in data:
        a.paid = bool(data['paid'])
        a.paid_at = datetime.utcnow() if a.paid else None
    db.session.commit()
    return jsonify({'message': 'Updated'})


@bp.route('/costume-assignments/<int:aid>', methods=['DELETE'])
@login_required
def delete_costume_assignment(aid):
    err = _admin_only()
    if err:
        return err
    a = CostumeAssignment.query.get_or_404(aid)
    db.session.delete(a)
    db.session.commit()
    return jsonify({'message': 'Removed'})


@bp.route('/costumes/<int:cid>/charge', methods=['POST'])
@login_required
def charge_costume(cid):
    """Post the costume fee as a 'costumes' charge to each assigned, not-yet-charged dancer."""
    err = _admin_only()
    if err:
        return err
    c = Costume.query.get_or_404(cid)
    fee = float(c.fee)
    if fee <= 0:
        return jsonify({'error': 'Set a costume fee greater than $0 first'}), 400
    charged = 0
    # Serialize concurrent charge runs so the not-yet-charged read below can't be
    # seen by two requests at once and double-charge (same guard as late fees).
    with _costume_charge_lock:
        for a in c.assignments.filter_by(charged=False).all():
            t = Transaction(
                student_id=a.student_id,
                type='charge',
                amount=fee,
                category='costumes',
                payment_method='n/a',
                description=f'Costume: {c.name}',
                transaction_date=date.today(),
                created_by=current_user.id,
            )
            db.session.add(t)
            db.session.flush()
            a.charged = True
            a.transaction_id = t.id
            charged += 1
        AuditLog.record(current_user.id, 'costume.charge', f'Charged ${fee:.2f} for "{c.name}" to {charged} dancers')
        db.session.commit()
    return jsonify({'message': f'Charged {charged} dancers ${fee:.2f} each', 'count': charged})


@bp.route('/my-costumes', methods=['GET'])
@login_required
def my_costumes():
    """A parent's children's costume assignments."""
    if not current_user.is_parent:
        return jsonify({'costumes': []})
    student_ids = _parent_student_ids(current_user)
    if not student_ids:
        return jsonify({'costumes': []})
    rows = (CostumeAssignment.query
            .filter(CostumeAssignment.student_id.in_(student_ids)).all())
    out = [{
        'student_name': a.student.full_name if a.student else None,
        'costume_name': a.costume.name,
        'size': a.size,
        'fee': f'{float(a.costume.fee):.2f}',
        'charged': a.charged,
        'paid': a.paid,
    } for a in rows if a.costume.is_active]
    return jsonify({'costumes': out})


# ── Recital: tickets ────────────────────────────────────────────────

@bp.route('/performances/<int:pid>/ticket-types', methods=['GET'])
@login_required
def list_ticket_types(pid):
    Performance.query.get_or_404(pid)
    types = TicketType.query.filter_by(performance_id=pid).all()
    return jsonify({'ticket_types': [{
        'id': t.id, 'name': t.name, 'price': f'{float(t.price):.2f}',
    } for t in types]})


@bp.route('/performances/<int:pid>/ticket-types', methods=['POST'])
@login_required
def create_ticket_type(pid):
    err = _admin_only()
    if err:
        return err
    Performance.query.get_or_404(pid)
    data = request.get_json() or {}
    if not data.get('name'):
        return jsonify({'error': 'name is required'}), 400
    t = TicketType(performance_id=pid, name=_clean_str(data['name']),
                   price=_opt_num(data.get('price'), 0, is_float=True))  # garbage -> 0, not a DB StatementError
    db.session.add(t)
    db.session.commit()
    return jsonify({'id': t.id, 'name': t.name, 'price': f'{float(t.price):.2f}'}), 201


@bp.route('/ticket-types/<int:tid>', methods=['DELETE'])
@login_required
def delete_ticket_type(tid):
    err = _admin_only()
    if err:
        return err
    t = TicketType.query.get_or_404(tid)
    TicketOrder.query.filter_by(ticket_type_id=tid).delete()
    db.session.delete(t)
    db.session.commit()
    return jsonify({'message': 'Ticket type removed'})


@bp.route('/performances/<int:pid>/ticket-orders', methods=['GET'])
@login_required
def list_ticket_orders(pid):
    if not current_user.is_admin:
        return jsonify({'error': 'Staff access required'}), 403
    Performance.query.get_or_404(pid)
    type_ids = [t.id for t in TicketType.query.filter_by(performance_id=pid).all()]
    orders = (TicketOrder.query.filter(TicketOrder.ticket_type_id.in_(type_ids)).order_by(desc(TicketOrder.created_at)).all()
              if type_ids else [])
    total_qty = sum(o.quantity for o in orders)
    revenue = sum(float(o.amount) for o in orders if o.paid)
    pending = sum(float(o.amount) for o in orders if not o.paid)
    return jsonify({
        'orders': [{
            'id': o.id,
            'type_name': o.ticket_type.name,
            'buyer': (o.parent.full_name if o.parent else None) or (o.student.full_name if o.student else 'Walk-up'),
            'quantity': o.quantity,
            'amount': f'{float(o.amount):.2f}',
            'paid': o.paid,
            'note': o.note,
            'created_at': _utc_iso(o.created_at),
        } for o in orders],
        'summary': {'total_tickets': total_qty, 'revenue': f'{revenue:.2f}', 'pending': f'{pending:.2f}'},
    })


@bp.route('/performances/<int:pid>/ticket-orders', methods=['POST'])
@login_required
def create_ticket_order(pid):
    """Record a ticket order. Admin can mark paid; parents create an unpaid request."""
    Performance.query.get_or_404(pid)
    data = request.get_json() or {}
    tt = TicketType.query.filter_by(id=data.get('ticket_type_id'), performance_id=pid).first()
    if not tt:
        return jsonify({'error': 'Invalid ticket type'}), 400
    try:
        qty = max(1, int(data.get('quantity', 1)))
    except (TypeError, ValueError):
        return jsonify({'error': 'Invalid quantity'}), 400

    student_id = data.get('student_id')
    if student_id:
        student_id, serr = _valid_id(student_id)
        if serr:
            return serr
    if current_user.is_parent:
        if student_id and student_id not in _parent_student_ids(current_user):
            return jsonify({'error': 'Not authorized for this student'}), 403
        paid = False  # parent requests are unpaid until the studio confirms
    elif not current_user.is_admin:
        # Ticket sales are money — admin-only on the staff side (least privilege).
        return jsonify({'error': 'Admin access required'}), 403
    else:
        paid = bool(data.get('paid', False))

    o = TicketOrder(
        ticket_type_id=tt.id,
        parent_id=current_user.id if current_user.is_parent else None,
        student_id=student_id if student_id else None,
        quantity=qty,
        amount=round(float(tt.price) * qty, 2),
        paid=paid,
        paid_at=datetime.utcnow() if paid else None,
        note=_clean_str(data.get('note')) or None,
    )
    db.session.add(o)
    db.session.commit()
    return jsonify({'message': f'{qty} × {tt.name} recorded', 'id': o.id}), 201


@bp.route('/ticket-orders/<int:oid>/toggle-paid', methods=['POST'])
@login_required
def toggle_ticket_paid(oid):
    err = _admin_only()
    if err:
        return err
    o = TicketOrder.query.get_or_404(oid)
    o.paid = not o.paid
    o.paid_at = datetime.utcnow() if o.paid else None
    db.session.commit()
    return jsonify({'message': 'Paid' if o.paid else 'Marked unpaid', 'paid': o.paid})


@bp.route('/ticket-orders/<int:oid>', methods=['DELETE'])
@login_required
def delete_ticket_order(oid):
    err = _admin_only()
    if err:
        return err
    o = TicketOrder.query.get_or_404(oid)
    db.session.delete(o)
    db.session.commit()
    return jsonify({'message': 'Order removed'})


# ── SMS test + cron + late fees ─────────────────────────────────────

@bp.route('/settings/sms/test', methods=['POST'])
@login_required
def test_sms_connection():
    err = _admin_only()
    if err:
        return err
    from app import sms as sms_service
    ok, message = sms_service.test_connection()
    return jsonify({'ok': ok, 'message': message}), (200 if ok else 400)


def _cron_authorized():
    """True if the request carries the valid cron token (Setting 'cron_token',
    config CRON_TOKEN, or env CRON_TOKEN). Constant-time compare to avoid
    leaking the token via response timing; an unset token always rejects rather
    than accepting an empty one."""
    token = Setting.get('cron_token', '') or current_app.config.get('CRON_TOKEN') or os.environ.get('CRON_TOKEN', '')
    provided = request.args.get('token') or request.headers.get('X-Cron-Token', '')
    return bool(token) and secrets.compare_digest(str(provided), str(token))


@bp.route('/cron/run', methods=['POST'])
def cron_run():
    """Token-protected endpoint for external schedulers to run recurring charges
    and auto-reminders. Token from Setting 'cron_token' or env CRON_TOKEN."""
    if not _cron_authorized():
        return jsonify({'error': 'Invalid or missing cron token'}), 403
    from app import _process_auto_reminders, _process_recurring_charges
    ran = []
    for name, fn in (('recurring_charges', _process_recurring_charges),
                     ('auto_reminders', _process_auto_reminders)):
        try:
            fn()
            ran.append(name)
        except Exception:
            db.session.rollback()
            logger.exception("Cron: %s failed", name)
    return jsonify({'status': 'ok', 'ran': ran})


@bp.route('/cron/backup', methods=['GET'])
def cron_backup():
    """Token-protected DB snapshot for an unattended scheduler (the nightly S3
    backup runs off-box because the 256MB Fly machine is OOM-sensitive and
    auto-sleeps — this HTTPS hit wakes it, GitHub Actions streams the file and
    uploads it). Same consistent snapshot as /admin/backup, but authenticated
    with the cron token instead of an admin session."""
    if not _cron_authorized():
        return jsonify({'error': 'Invalid or missing cron token'}), 403
    return _backup_response(None, 'cron')


# Serializes concurrent late-fee runs. The per-student "already charged this
# month?" check below is idempotent for a *sequential* re-run, but two
# *concurrent* runs (a double-click) both read the pre-charge state and each
# charge every over-threshold family — double late fees. Holding this lock across
# the check+commit makes the second run see the first's committed charges and
# skip them. Valid because the app runs a single gunicorn worker (see the deploy
# note); it would need a DB-level guard if workers were ever scaled up.
_late_fee_lock = threading.Lock()
# Same rationale for the other admin bulk-money op: two concurrent "charge costume
# fee" calls (two tabs) could both read the same not-yet-charged assignments before
# either commits and double-charge the dancers. Serialize them (single worker).
_costume_charge_lock = threading.Lock()


@bp.route('/balances/apply-late-fees', methods=['POST'])
@login_required
def apply_late_fees():
    """Apply a late fee charge to every student over a balance threshold."""
    err = _admin_only()
    if err:
        return err
    data = request.get_json() or {}
    try:
        amount = round(float(data.get('amount') or Setting.get('late_fee_amount', '0') or 0), 2)
        min_balance = round(float(data.get('min_balance') if data.get('min_balance') is not None
                                  else (Setting.get('late_fee_min_balance', '0') or 0)), 2)
    except (TypeError, ValueError):
        return jsonify({'error': 'Invalid amount or threshold'}), 400
    if amount <= 0:
        return jsonify({'error': 'Set a late fee amount greater than $0'}), 400

    with _late_fee_lock:
        students = Student.query.filter_by(is_active=True).all()
        balances = calc_balance_bulk([s.id for s in students])
        month_start = date.today().replace(day=1)
        applied = 0
        skipped = 0
        for s in students:
            if balances[s.id]['balance'] <= min_balance:
                continue
            # Idempotency: never stack a second late fee on a student in the same
            # calendar month. Without this, a double-click or a refresh that
            # re-POSTs charges every over-threshold family twice.
            already = Transaction.query.filter_by(
                student_id=s.id, type='charge', category='late fee',
            ).filter(Transaction.transaction_date >= month_start).first()
            if already:
                skipped += 1
                continue
            db.session.add(Transaction(
                student_id=s.id, type='charge', amount=amount, category='late fee',
                payment_method='n/a', description='Late fee', transaction_date=date.today(),
                created_by=current_user.id,
            ))
            applied += 1
        AuditLog.record(current_user.id, 'late_fee.apply',
                        f'Applied ${amount:.2f} late fee to {applied} students '
                        f'({skipped} already charged this month; balance > ${min_balance:.2f})')
        db.session.commit()
    msg = f'Applied ${amount:.2f} late fee to {applied} students'
    if skipped:
        msg += f' ({skipped} already had one this month — skipped)'
    return jsonify({'message': msg, 'count': applied, 'skipped': skipped})


# ── Payment plans ───────────────────────────────────────────────────

def _add_months(d, n):
    """Return date d shifted by n months, clamping the day to month length."""
    import calendar
    month = d.month - 1 + n
    year = d.year + month // 12
    month = month % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _plan_to_dict(p):
    insts = p.installments.order_by(PaymentPlanInstallment.seq).all()
    return {
        'id': p.id,
        'student_id': p.student_id,
        'student_name': p.student.full_name if p.student else None,
        'installment_amount': f'{float(p.installment_amount):.2f}',
        'num_installments': p.num_installments,
        'day_of_month': p.day_of_month,
        'note': p.note,
        'is_active': p.is_active,
        'total': f'{float(p.installment_amount) * p.num_installments:.2f}',
        'paid_count': sum(1 for i in insts if i.paid),
        'installments': [{
            'id': i.id, 'seq': i.seq, 'due_date': i.due_date.isoformat(),
            'amount': f'{float(i.amount):.2f}', 'paid': i.paid,
        } for i in insts],
    }


@bp.route('/payment-plans', methods=['GET'])
@login_required
def list_payment_plans():
    err = _admin_only()
    if err:
        return err
    plans = PaymentPlan.query.filter_by(is_active=True).order_by(desc(PaymentPlan.created_at)).all()
    return jsonify({'plans': [_plan_to_dict(p) for p in plans]})


@bp.route('/students/<int:student_id>/payment-plan', methods=['GET'])
@login_required
def get_payment_plan(student_id):
    err = _require_student_money_access(student_id)
    if err:
        return err
    plan = PaymentPlan.query.filter_by(student_id=student_id, is_active=True).order_by(desc(PaymentPlan.created_at)).first()
    return jsonify({'plan': _plan_to_dict(plan) if plan else None})


@bp.route('/students/<int:student_id>/payment-plan', methods=['POST'])
@login_required
def create_payment_plan(student_id):
    err = _admin_only()
    if err:
        return err
    Student.query.get_or_404(student_id)
    data = request.get_json() or {}
    try:
        amount = round(float(data.get('installment_amount')), 2)
        num = int(data.get('num_installments'))
        day = int(data.get('day_of_month', 1))
    except (TypeError, ValueError):
        return jsonify({'error': 'installment_amount, num_installments, day_of_month are required'}), 400
    if amount <= 0 or num <= 0 or not (1 <= day <= 28):
        return jsonify({'error': 'Amounts must be positive; day must be 1–28'}), 400

    # Deactivate any prior active plan
    PaymentPlan.query.filter_by(student_id=student_id, is_active=True).update({'is_active': False})

    plan = PaymentPlan(student_id=student_id, installment_amount=amount, num_installments=num,
                       day_of_month=day, note=_clean_str(data.get('note')) or None,
                       created_by=current_user.id)
    db.session.add(plan)
    db.session.flush()

    today = date.today()
    first = today.replace(day=day) if today.day <= day else _add_months(today.replace(day=day), 1)
    for i in range(num):
        db.session.add(PaymentPlanInstallment(plan_id=plan.id, seq=i + 1,
                                              due_date=_add_months(first, i), amount=amount))
    AuditLog.record(current_user.id, 'payment_plan.create',
                    f'Plan for {plan.student.full_name}: ${amount:.2f} × {num}')
    db.session.commit()
    return jsonify(_plan_to_dict(plan)), 201


@bp.route('/payment-plan-installments/<int:iid>/toggle-paid', methods=['POST'])
@login_required
def toggle_installment_paid(iid):
    err = _admin_only()
    if err:
        return err
    i = PaymentPlanInstallment.query.get_or_404(iid)
    i.paid = not i.paid
    i.paid_at = datetime.utcnow() if i.paid else None
    db.session.commit()
    return jsonify({'message': 'Updated', 'paid': i.paid})


@bp.route('/payment-plans/<int:pid>', methods=['DELETE'])
@login_required
def delete_payment_plan(pid):
    err = _admin_only()
    if err:
        return err
    p = PaymentPlan.query.get_or_404(pid)
    p.is_active = False
    db.session.commit()
    return jsonify({'message': 'Payment plan ended'})


# ── Donations (Foundation) ──────────────────────────────────────────

def _donation_to_dict(d):
    return {
        'id': d.id,
        'donor_name': d.donor_name,
        'donor_email': d.donor_email,
        'amount': f'{float(d.amount):.2f}',
        'method': d.method,
        'note': d.note,
        'status': d.status,
        'donation_date': d.donation_date.isoformat(),
    }


@bp.route('/donations', methods=['GET'])
@login_required
def list_donations():
    err = _admin_only()
    if err:
        return err
    status = request.args.get('status', '').strip()
    q = Donation.query
    if status:
        q = q.filter_by(status=status)
    rows = q.order_by(desc(Donation.donation_date), desc(Donation.created_at)).limit(500).all()
    total = sum(float(d.amount) for d in rows if d.status == 'recorded')
    return jsonify({'donations': [_donation_to_dict(d) for d in rows], 'recorded_total': f'{total:.2f}'})


@bp.route('/donations', methods=['POST'])
@login_required
def create_donation():
    """Admin records a donation (status recorded); a parent submits one (pending)."""
    data = request.get_json() or {}
    try:
        amount = round(float(data.get('amount')), 2)
    except (TypeError, ValueError):
        return jsonify({'error': 'A valid amount is required'}), 400
    if amount <= 0:
        return jsonify({'error': 'Amount must be greater than zero'}), 400

    is_parent = current_user.is_parent
    d = Donation(
        donor_name=(data.get('donor_name') or (current_user.full_name if is_parent else '')).strip() or None,
        donor_email=(data.get('donor_email') or (current_user.email if is_parent else '')).strip() or None,
        amount=amount,
        method=_clean_str(data.get('method')).lower() or None,
        note=_clean_str(data.get('note')) or None,
        status='pending' if is_parent else 'recorded',
        parent_id=current_user.id if is_parent else None,
        donation_date=date.today(),
    )
    db.session.add(d)
    db.session.commit()
    msg = ('Thank you! Your donation was submitted — the Foundation will confirm it.'
           if is_parent else 'Donation recorded')
    return jsonify({'message': msg, 'donation': _donation_to_dict(d)}), 201


@bp.route('/donations/<int:did>/confirm', methods=['POST'])
@login_required
def confirm_donation(did):
    err = _admin_only()
    if err:
        return err
    d = Donation.query.get_or_404(did)
    d.status = 'recorded'
    db.session.commit()
    return jsonify({'message': 'Donation confirmed'})


@bp.route('/donations/<int:did>', methods=['DELETE'])
@login_required
def delete_donation(did):
    err = _admin_only()
    if err:
        return err
    d = Donation.query.get_or_404(did)
    db.session.delete(d)
    db.session.commit()
    return jsonify({'message': 'Donation removed'})


# ── Self-registration (public) ──────────────────────────────────────

@bp.route('/registration/open', methods=['GET'])
def registration_open_info():
    """Public: is enrollment open, plus the classes a family can request."""
    is_open = Setting.get_bool('registration_open')
    classes = []
    if is_open:
        # Registration can target a specific season (e.g. open FALL sign-ups
        # while summer is still the active season). When the setting points at
        # a real season, list that season's classes — draft included, that's
        # the point. Otherwise: the live (active-season) schedule.
        reg_sid = Setting.get('registration_season_id', '')
        target = Season.query.get(int(reg_sid)) if reg_sid.isdigit() else None
        if target:
            reg_classes_query = DanceClass.query.filter_by(season_id=target.id, is_active=True)
        else:
            reg_classes_query = live_class_query()
        for c in reg_classes_query.order_by(DanceClass.name).all():
            enrolled = c.enrolled_students_count
            classes.append({
                'id': c.id, 'name': c.name, 'day_name': c.day_name,
                'start_time': c.start_time.strftime('%I:%M %p').lstrip('0'),
                'level': c.level, 'age_group': c.age_group,
                'full': c.max_students is not None and enrolled >= c.max_students,
            })
    return jsonify({
        'open': is_open,
        'message': Setting.get('registration_message', '') or "Welcome! Tell us about your dancer(s) and we'll be in touch.",
        'classes': classes,
    })


# Throttle admin "new registration" notifications. The public submit is
# unauthenticated and emailed the admins on EVERY submission, so a scripted flood
# (possible while registration is open) could email-bomb the studio — getting the
# account rate-limited by Gmail and burying real alerts. Admins see every request
# on the (paginated) Registrations page regardless; this is only the nudge, so at
# most one every _REG_NOTIFY_WINDOW seconds is plenty. Process-local (single worker).
_REG_NOTIFY_WINDOW = 120
_last_reg_notify = [0.0]


def _notify_admins_new_registration():
    now = time.time()
    if now - _last_reg_notify[0] < _REG_NOTIFY_WINDOW:
        return
    from app import email as email_service
    if not email_service.is_configured():
        return
    admins = {u.email for u in User.query.filter_by(role='admin', is_active=True).all()
              if u.email and '@' in u.email}
    if not admins:
        return
    _last_reg_notify[0] = now  # mark before sending so a burst doesn't all fire
    _send_email_async(
        admins, 'New enrollment request(s)',
        'One or more new registration requests were received. '
        'Review them on the Registrations page.')


@bp.route('/register', methods=['POST'])
def submit_registration():
    """Public: submit an enrollment request for admin review."""
    import json
    if not Setting.get_bool('registration_open'):
        return jsonify({'error': 'Registration is currently closed'}), 403
    # Public + unauthenticated: never trust the shape of the payload. Coerce every
    # field so a non-string name or a non-list `students` can't 500 the endpoint.
    data = request.get_json(silent=True) or {}
    # Unauthenticated input — cap every field length (SQLite ignores VARCHAR(n),
    # so uncapped a scripted submit could store multi-MB values and bloat the DB).
    parent_name = _clean_str(data.get('parent_name'), 120)
    parent_email = _clean_str(data.get('parent_email'), 200)
    if not parent_name or not parent_email:
        return jsonify({'error': 'Parent name and email are required'}), 400
    if '@' not in parent_email or '.' not in parent_email.split('@')[-1]:
        return jsonify({'error': 'Please enter a valid email address'}), 400
    # Volume circuit breakers for the one unauthenticated write surface (the
    # remote IP isn't usable behind the proxy, so throttle on what we have):
    # a parent re-submitting is fine a couple of times, but unbounded submits
    # would bury the admin queue; the global cap stops a bot from making the
    # queue unusable / bloating the DB. Both are friendly-fail — the studio's
    # contact info is the fallback path.
    if Registration.query.filter(Registration.status == 'pending',
                                 func.lower(Registration.parent_email) == parent_email.lower()
                                 ).count() >= _MAX_PENDING_PER_EMAIL:
        return jsonify({'error': 'You already have a registration waiting for review — '
                                 'the studio will reach out soon.'}), 429
    if Registration.query.filter_by(status='pending').count() >= _MAX_PENDING_REGISTRATIONS:
        logger.warning("Registration circuit breaker tripped: pending queue at cap")
        return jsonify({'error': 'Registration is temporarily unavailable — '
                                 'please contact the studio directly.'}), 429
    raw_students = data.get('students')
    if not isinstance(raw_students, list):
        raw_students = []
    # Store only cleaned, expected fields (not the raw dicts) so a non-string
    # last_name/dob can't 500 the admin approve flow later, and cap the count so
    # a scripted submit can't stuff thousands of rows into one registration.
    students = []
    for s in raw_students:
        if not isinstance(s, dict):
            continue
        fn = _clean_str(s.get('first_name'), 80)
        if not fn:
            continue
        students.append({
            'first_name': fn,
            'last_name': _clean_str(s.get('last_name'), 80),
            'dob': _clean_str(s.get('dob'), 20),
            'allergies': _clean_str(s.get('allergies'), 500),
        })
        if len(students) >= 30:
            break
    if not students:
        return jsonify({'error': 'Add at least one dancer'}), 400
    raw_class_ids = data.get('class_ids')
    if not isinstance(raw_class_ids, list):
        raw_class_ids = []

    reg = Registration(
        parent_name=parent_name,
        parent_email=parent_email,
        parent_phone=_clean_str(data.get('parent_phone'), 40) or None,
        students_json=json.dumps(students),
        class_ids=','.join(str(int(c)) for c in raw_class_ids[:50] if str(c).isdigit()),
        note=_clean_str(data.get('note'), 2000) or None,
    )
    db.session.add(reg)
    db.session.commit()
    _notify_admins_new_registration()
    return jsonify({'message': "Thanks! Your registration was received — the studio will reach out soon."}), 201


@bp.route('/registrations', methods=['GET'])
@login_required
def list_registrations():
    err = _admin_only()
    if err:
        return err
    import json
    status = request.args.get('status', 'pending').strip()
    page = request.args.get('page', 1, type=int)
    per_page = min(request.args.get('per_page', 50, type=int), 100)
    q = Registration.query
    if status and status != 'all':
        q = q.filter_by(status=status)
    pagination = (q.order_by(desc(Registration.created_at))
                  .paginate(page=page, per_page=per_page, error_out=False))
    out = []
    for r in pagination.items:
        try:
            students = json.loads(r.students_json or '[]')
        except ValueError:
            students = []
        class_names = []
        if r.class_ids:
            ids = [int(x) for x in r.class_ids.split(',') if x.isdigit()]
            class_names = [c.name for c in DanceClass.query.filter(DanceClass.id.in_(ids)).all()]
        # Returning-family hint for the admin queue: same matcher approval uses,
        # so the badge and the approve behavior can't disagree.
        m_fam, m_students = _match_registration_family(r.parent_email)
        out.append({
            'id': r.id, 'parent_name': r.parent_name, 'parent_email': r.parent_email,
            'parent_phone': r.parent_phone, 'students': students, 'class_names': class_names,
            'note': r.note, 'status': r.status, 'created_at': _utc_iso(r.created_at),
            'returning': m_fam is not None or bool(m_students),
            'matched_family': m_fam.name if m_fam else None,
        })
    return jsonify({
        'registrations': out,
        'pagination': {
            'page': page, 'pages': pagination.pages,
            'per_page': per_page, 'total': pagination.total,
        },
    })


def _match_registration_family(parent_email):
    """Find the existing family a registration belongs to: by the family's
    primary_email, else via a student whose parent_email matches (the pilot-era
    data holds the email on the student, and most students have no family).
    Returns (family_or_None, email_matched_students). Shared by the pending
    list (the 'returning' badge) and approval (the actual dedupe) so the hint
    and the behavior can never disagree."""
    if not parent_email:
        return None, []
    email_l = parent_email.lower()
    fam = (Family.query
           .filter(Family.is_active.is_(True),
                   func.lower(Family.primary_email) == email_l)
           .first())
    email_students = (Student.query
                      .filter(func.lower(Student.parent_email) == email_l)
                      .all())
    if not fam:
        for st in email_students:
            if st.family and st.family.is_active:
                fam = st.family
                break
    return fam, email_students


@bp.route('/registrations/count', methods=['GET'])
@login_required
def registrations_count():
    if not current_user.is_staff:
        return jsonify({'count': 0})
    return jsonify({'count': Registration.query.filter_by(status='pending').count()})


@bp.route('/registrations/<int:rid>/approve', methods=['POST'])
@login_required
def approve_registration(rid):
    err = _admin_only()
    if err:
        return err
    import json
    from datetime import datetime as _dt
    reg = Registration.query.get_or_404(rid)
    if reg.status != 'pending':
        return jsonify({'error': f'Already {reg.status}'}), 400
    try:
        students = json.loads(reg.students_json or '[]')
    except ValueError:
        students = []
    if not students:
        return jsonify({'error': 'No dancers on this registration'}), 400

    # Reuse an existing family with this parent email instead of creating a
    # duplicate. Families register twice (June and again in August, or a
    # double-submit days apart); approving both used to create two families
    # with the same children duplicated and the ledger split between them.
    # The matcher also handles the pilot-era shape (email on the student, no
    # family) — see _match_registration_family.
    fam, email_students = _match_registration_family(reg.parent_email)
    matched_existing = fam is not None
    if not fam:
        fam = Family(name=f'{reg.parent_name} Family', primary_email=reg.parent_email,
                     primary_phone=reg.parent_phone)
        db.session.add(fam)
        db.session.flush()
    elif not fam.primary_email:
        # Backfill so the NEXT re-registration matches on the family fast path.
        fam.primary_email = reg.parent_email

    class_ids = [int(x) for x in (reg.class_ids or '').split(',') if x.isdigit()]
    # Only enroll in classes that still exist — a class deleted between submit and
    # approval would otherwise leave a dangling enrollment that 500s when the
    # roster later tries to read its class name. Capture each class's capacity and
    # current headcount so approval respects capacity like the manual-enroll path
    # (otherwise approving registrations silently overbooks popular fall classes).
    caps = {}
    if class_ids:
        for c in DanceClass.query.filter(DanceClass.id.in_(class_ids)).all():
            caps[c.id] = {
                'capacity': c.max_students or 0,
                'count': ClassEnrollment.query.filter_by(class_id=c.id, is_active=True).count(),
                'name': c.name,
            }
        class_ids = [cid for cid in class_ids if cid in caps]
    created = []
    created_objs = []
    full_skips = []
    returning = []
    # Existing dancers who could be returning: those already in the (possibly
    # reused) family, plus any student whose parent_email matches — the pilot
    # data has students with a matching email but NO family. Don't recreate a
    # returning dancer (that duplicates them + splits the ledger).
    existing_by_name = {}
    if matched_existing:
        for st in Student.query.filter_by(family_id=fam.id).all():
            existing_by_name[(st.first_name.strip().lower(),
                              (st.last_name or '').strip().lower())] = st
    for st in email_students:
        existing_by_name.setdefault(
            (st.first_name.strip().lower(), (st.last_name or '').strip().lower()), st)

    def _enroll(student):
        """Enroll in the requested classes, capacity-checked, skipping ones the
        dancer is already actively enrolled in (re-registration lists them)."""
        for cid in class_ids:
            if ClassEnrollment.query.filter_by(student_id=student.id, class_id=cid,
                                               is_active=True).first():
                continue
            cap = caps.get(cid)
            if cap and cap['capacity'] and cap['count'] >= cap['capacity']:
                # Class is full — skip and report so the admin can raise the cap or
                # waitlist this dancer, rather than silently overbooking.
                full_skips.append(f"{student.full_name} → {cap['name']}")
                continue
            db.session.add(ClassEnrollment(student_id=student.id, class_id=cid))
            if cap:
                cap['count'] += 1

    for s in students:
        fn = (s.get('first_name') or '').strip()
        if not fn:
            continue
        ln = (s.get('last_name') or reg.parent_name.split()[-1]).strip()
        existing = existing_by_name.get((fn.lower(), ln.lower()))
        if existing:
            # Returning dancer: reactivate if withdrawn (last year's soft delete)
            # and enroll them in the newly-requested classes — this is the fall
            # re-enrollment path for existing families. A pilot-era dancer with
            # no family (or one matched via email) is adopted into this family
            # so siblings and family billing group correctly from now on.
            if not existing.is_active:
                existing.is_active = True
            if existing.family_id != fam.id:
                existing.family_id = fam.id
            _enroll(existing)
            returning.append(f'{fn} {ln}')
            continue
        dob = None
        if s.get('dob'):
            try:
                dob = _dt.strptime(s['dob'], '%Y-%m-%d').date()
            except ValueError:
                dob = None
        student = Student(
            first_name=fn, last_name=ln,
            family_id=fam.id, parent_email=reg.parent_email, parent_phone=reg.parent_phone,
            date_of_birth=dob, allergies=(s.get('allergies') or '').strip() or None,
        )
        db.session.add(student)
        db.session.flush()
        _enroll(student)
        created.append(student.full_name)
        created_objs.append(student)
        existing_by_name[(fn.lower(), ln.lower())] = student  # dedupe within one payload too

    # Link newly-created dancers to the family's existing portal accounts.
    # Without this, a returning family's new sibling never appears on the
    # parent's portal — approval created the dancer with no ParentStudent link,
    # and register-with-code can't be reused by an existing account. The
    # registering parent listed the child themselves, so linkage is intended.
    # (Returning dancers keep their existing links untouched.)
    portal_linked = 0
    if created_objs:
        sibling_ids = [st.id for st in Student.query.filter_by(family_id=fam.id).all()
                       if st.id not in {c.id for c in created_objs}]
        parent_ids = {ps.parent_id for ps in ParentStudent.query.filter(
            ParentStudent.student_id.in_(sibling_ids)).all()} if sibling_ids else set()
        for pid in parent_ids:
            for st in created_objs:
                if not ParentStudent.query.filter_by(parent_id=pid, student_id=st.id).first():
                    db.session.add(ParentStudent(parent_id=pid, student_id=st.id))
                    portal_linked += 1

    # Atomically claim the registration — two concurrent approvals (double-click)
    # both pass the status check above and would otherwise each create a Family
    # with its own students + enrollments (duplicate family). The conditional
    # UPDATE re-checks status in the DB; the loser matches 0 rows and rolls back
    # the family/students it just inserted.
    claimed = db.session.query(Registration).filter_by(
        id=rid, status='pending').update({
            'status': 'approved',
            'reviewed_at': _dt.utcnow(),
            'reviewed_by': current_user.id,
        }, synchronize_session=False)
    if not claimed:
        db.session.rollback()
        return jsonify({'error': 'Already processed'}), 400
    AuditLog.record(current_user.id, 'registration.approve',
                    f'Approved {reg.parent_name}: {", ".join(created)}')
    db.session.commit()
    msg = f'Created {len(created)} dancer(s) under {fam.name}'
    if matched_existing:
        msg += ' (matched the existing family by parent email — no duplicate family created)'
    if returning:
        msg += (f' — {len(returning)} returning dancer(s) re-enrolled (not duplicated): '
                f'{", ".join(returning)}')
    if portal_linked:
        msg += " — new dancer(s) linked to the family's portal account"
    if full_skips:
        msg += (f' — {len(full_skips)} enrollment(s) skipped (class full): '
                f'{"; ".join(full_skips)}. Raise the capacity or use the waitlist.')
    return jsonify({'message': msg, 'students': created, 'full_skipped': full_skips,
                    'returning_dancers': returning, 'matched_existing_family': matched_existing})


@bp.route('/registrations/<int:rid>/reject', methods=['POST'])
@login_required
def reject_registration(rid):
    err = _admin_only()
    if err:
        return err
    reg = Registration.query.get_or_404(rid)
    reg.status = 'rejected'
    reg.reviewed_at = datetime.utcnow()
    reg.reviewed_by = current_user.id
    db.session.commit()
    return jsonify({'message': 'Registration rejected'})


# ── Waitlists ───────────────────────────────────────────────────────

@bp.route('/classes/<int:class_id>/waitlist', methods=['GET'])
@login_required
def get_waitlist(class_id):
    if not current_user.is_staff:
        return jsonify({'error': 'Staff access required'}), 403
    DanceClass.query.get_or_404(class_id)
    rows = (WaitlistEntry.query.filter_by(class_id=class_id, status='waiting')
            .order_by(WaitlistEntry.created_at).all())
    return jsonify({'waitlist': [{
        'id': w.id, 'student_id': w.student_id, 'student_name': w.student.full_name if w.student else None,
        'position': i + 1, 'created_at': _utc_iso(w.created_at),
    } for i, w in enumerate(rows)]})


@bp.route('/classes/<int:class_id>/waitlist', methods=['POST'])
@login_required
def add_to_waitlist(class_id):
    err = _admin_only()
    if err:
        return err
    DanceClass.query.get_or_404(class_id)
    data = request.get_json() or {}
    student_id, serr = _valid_id(data.get('student_id'))
    if serr:
        return serr
    if Student.query.get(student_id) is None:
        return jsonify({'error': 'student not found'}), 404
    if current_user.is_parent and student_id not in _parent_student_ids(current_user):
        return jsonify({'error': 'Not authorized for this student'}), 403
    if ClassEnrollment.query.filter_by(class_id=class_id, student_id=student_id, is_active=True).first():
        return jsonify({'error': 'Already enrolled in this class'}), 400
    existing = WaitlistEntry.query.filter_by(class_id=class_id, student_id=student_id).first()
    if existing and existing.status == 'waiting':
        return jsonify({'error': 'Already on the waitlist'}), 400
    if existing:
        existing.status = 'waiting'
        existing.created_at = datetime.utcnow()
    else:
        db.session.add(WaitlistEntry(class_id=class_id, student_id=student_id,
                                     parent_id=current_user.id if current_user.is_parent else None))
    db.session.commit()
    return jsonify({'message': 'Added to the waitlist'}), 201


@bp.route('/waitlist/<int:wid>/promote', methods=['POST'])
@login_required
def promote_waitlist(wid):
    err = _admin_only()
    if err:
        return err
    w = WaitlistEntry.query.get_or_404(wid)
    already = ClassEnrollment.query.filter_by(
        class_id=w.class_id, student_id=w.student_id, is_active=True).first()
    if not already:
        # Respect capacity — promoting into a still-full class would overbook and
        # defeat the point of the waitlist. Free a spot (unenroll) or raise the cap first.
        cls = w.dance_class
        capacity = (cls.max_students or 0) if cls else 0
        if capacity and ClassEnrollment.query.filter_by(
                class_id=w.class_id, is_active=True).count() >= capacity:
            return jsonify({'error': f'{cls.name} is full ({capacity} max). '
                                     'Free a spot or raise the capacity before promoting.'}), 400
        db.session.add(ClassEnrollment(student_id=w.student_id, class_id=w.class_id))
    w.status = 'enrolled'
    AuditLog.record(current_user.id, 'waitlist.promote',
                    f'Enrolled {w.student.full_name} from waitlist into {w.dance_class.name}')
    db.session.commit()
    return jsonify({'message': f'{w.student.full_name} enrolled in {w.dance_class.name}'})


@bp.route('/waitlist/<int:wid>', methods=['DELETE'])
@login_required
def remove_waitlist(wid):
    err = _admin_only()
    if err:
        return err
    w = WaitlistEntry.query.get_or_404(wid)
    if current_user.is_parent and w.parent_id != current_user.id:
        return jsonify({'error': 'Not authorized'}), 403
    w.status = 'removed'
    db.session.commit()
    return jsonify({'message': 'Removed from waitlist'})


# ── Skills & progress ───────────────────────────────────────────────

@bp.route('/skills', methods=['GET'])
@login_required
def list_skills():
    if not current_user.is_staff:
        return jsonify({'error': 'Staff access required'}), 403
    q = Skill.query.filter_by(is_active=True)
    class_id = request.args.get('class_id', type=int)
    if class_id:
        q = q.filter_by(class_id=class_id)
    rows = q.order_by(Skill.display_order, Skill.id).all()
    return jsonify({'skills': [{
        'id': s.id, 'name': s.name, 'category': s.category,
        'class_id': s.class_id, 'class_name': s.dance_class.name if s.dance_class else None,
        'achieved_count': s.achievements.count(),
    } for s in rows]})


@bp.route('/skills', methods=['POST'])
@login_required
def create_skill():
    err = _admin_only()
    if err:
        return err
    data = request.get_json() or {}
    name = _clean_str(data.get('name'))
    if not name:
        return jsonify({'error': 'name is required'}), 400
    class_id = None
    if data.get('class_id'):
        class_id, cerr = _valid_id(data.get('class_id'))
        if cerr:
            return cerr
    try:
        display_order = int(data.get('display_order') or 0)
    except (TypeError, ValueError):
        display_order = 0
    s = Skill(name=name, category=_clean_str(data.get('category')) or None,
              class_id=class_id, display_order=display_order)
    db.session.add(s)
    db.session.commit()
    return jsonify({'id': s.id, 'name': s.name}), 201


@bp.route('/skills/<int:sid>', methods=['DELETE'])
@login_required
def delete_skill(sid):
    err = _admin_only()
    if err:
        return err
    s = Skill.query.get_or_404(sid)
    s.is_active = False
    db.session.commit()
    return jsonify({'message': f'{s.name} archived'})


@bp.route('/students/<int:student_id>/skills', methods=['GET'])
@login_required
def get_student_skills(student_id):
    if current_user.is_parent and student_id not in _parent_student_ids(current_user):
        return jsonify({'error': 'Not authorized'}), 403
    Student.query.get_or_404(student_id)
    achieved = {a.skill_id: a for a in StudentSkill.query.filter_by(student_id=student_id).all()}
    skills = Skill.query.filter_by(is_active=True).order_by(Skill.category, Skill.display_order, Skill.id).all()
    return jsonify({'skills': [{
        'id': s.id, 'name': s.name, 'category': s.category,
        'achieved': s.id in achieved,
        'achieved_at': _utc_iso(achieved[s.id].achieved_at) if s.id in achieved else None,
    } for s in skills]})


@bp.route('/students/<int:student_id>/skills/<int:skill_id>/toggle', methods=['POST'])
@login_required
def toggle_student_skill(student_id, skill_id):
    err = _admin_only()
    if err:
        return err
    Student.query.get_or_404(student_id)
    Skill.query.get_or_404(skill_id)
    existing = StudentSkill.query.filter_by(student_id=student_id, skill_id=skill_id).first()
    if existing:
        db.session.delete(existing)
        achieved = False
    else:
        db.session.add(StudentSkill(student_id=student_id, skill_id=skill_id, awarded_by=current_user.id))
        achieved = True
    db.session.commit()
    return jsonify({'achieved': achieved})


# ── Makeup classes ──────────────────────────────────────────────────

def _makeup_to_dict(m):
    return {
        'id': m.id,
        'student_id': m.student_id,
        'student_name': m.student.full_name if m.student else None,
        'missed_class': m.missed_class.name if m.missed_class else None,
        'missed_date': m.missed_date.isoformat() if m.missed_date else None,
        'makeup_class': m.makeup_class.name if m.makeup_class else None,
        'makeup_date': m.makeup_date.isoformat() if m.makeup_date else None,
        'status': m.status,
        'note': m.note,
        'created_at': _utc_iso(m.created_at),
    }


@bp.route('/makeups', methods=['GET'])
@login_required
def list_makeups():
    if not current_user.is_staff:
        return jsonify({'error': 'Staff access required'}), 403
    q = MakeupClass.query
    status = request.args.get('status', '').strip()
    if status:
        q = q.filter_by(status=status)
    rows = q.order_by(desc(MakeupClass.created_at)).limit(200).all()
    return jsonify({'makeups': [_makeup_to_dict(m) for m in rows]})


def _parse_date(v):
    if not v:
        return None
    try:
        return datetime.strptime(v, '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return None


def _parse_time(v):
    """Parse an HH:MM time string, or None on garbage/absent — so a bad
    start_time/end_time in a request body can't 500 an unguarded strptime."""
    if not v:
        return None
    try:
        return datetime.strptime(v, '%H:%M').time()
    except (ValueError, TypeError):
        return None


@bp.route('/makeups', methods=['POST'])
@login_required
def create_makeup():
    data = request.get_json() or {}
    student_id, err = _valid_id(data.get('student_id'))
    if err:
        return err
    if Student.query.get(student_id) is None:
        return jsonify({'error': 'student not found'}), 404
    if current_user.is_parent and student_id not in _parent_student_ids(current_user):
        return jsonify({'error': 'Not authorized for this student'}), 403

    def _opt_id(raw):  # optional class id: blank -> None, garbage -> None (don't 500)
        if not raw:
            return None
        val, e = _valid_id(raw)
        return val if not e else None

    m = MakeupClass(
        student_id=student_id,
        missed_class_id=_opt_id(data.get('missed_class_id')),
        missed_date=_parse_date(data.get('missed_date')),
        makeup_class_id=_opt_id(data.get('makeup_class_id')),
        makeup_date=_parse_date(data.get('makeup_date')),
        status='requested' if current_user.is_parent else (data.get('status') or 'scheduled'),
        note=_clean_str(data.get('note')) or None,
        requested_by=current_user.id,
    )
    db.session.add(m)
    db.session.commit()
    return jsonify(_makeup_to_dict(m)), 201


@bp.route('/makeups/<int:mid>', methods=['PUT'])
@login_required
def update_makeup(mid):
    err = _admin_only()
    if err:
        return err
    m = MakeupClass.query.get_or_404(mid)
    data = request.get_json() or {}
    if 'status' in data and data['status'] in ('requested', 'scheduled', 'completed', 'cancelled'):
        m.status = data['status']
    if 'makeup_class_id' in data:
        m.makeup_class_id = _opt_int(data.get('makeup_class_id'))  # garbage -> None, not 500
    if 'makeup_date' in data:
        m.makeup_date = _parse_date(data['makeup_date'])
    if 'note' in data:
        m.note = _clean_str(data['note']) or None
    db.session.commit()
    return jsonify(_makeup_to_dict(m))


@bp.route('/makeups/<int:mid>', methods=['DELETE'])
@login_required
def delete_makeup(mid):
    err = _admin_only()
    if err:
        return err
    m = MakeupClass.query.get_or_404(mid)
    db.session.delete(m)
    db.session.commit()
    return jsonify({'message': 'Removed'})


@bp.route('/my-makeups', methods=['GET'])
@login_required
def my_makeups():
    if not current_user.is_parent:
        return jsonify({'makeups': []})
    ids = _parent_student_ids(current_user)
    if not ids:
        return jsonify({'makeups': []})
    rows = MakeupClass.query.filter(MakeupClass.student_id.in_(ids)).order_by(desc(MakeupClass.created_at)).all()
    return jsonify({'makeups': [_makeup_to_dict(m) for m in rows]})


# ── Leads / trial pipeline ──────────────────────────────────────────

def _lead_to_dict(l):
    return {
        'id': l.id, 'name': l.name, 'email': l.email, 'phone': l.phone,
        'interest': l.interest, 'source': l.source, 'status': l.status,
        'trial_date': l.trial_date.isoformat() if l.trial_date else None,
        'note': l.note, 'created_at': _utc_iso(l.created_at),
    }


@bp.route('/leads', methods=['GET'])
@login_required
def list_leads():
    err = _admin_only()
    if err:
        return err
    q = Lead.query
    status = request.args.get('status', '').strip()
    if status and status != 'all':
        q = q.filter_by(status=status)
    rows = q.order_by(desc(Lead.created_at)).limit(300).all()
    return jsonify({'leads': [_lead_to_dict(l) for l in rows]})


@bp.route('/leads', methods=['POST'])
@login_required
def create_lead():
    err = _admin_only()
    if err:
        return err
    data = request.get_json() or {}
    name = _clean_str(data.get('name'))
    if not name:
        return jsonify({'error': 'name is required'}), 400
    l = Lead(name=name, email=_clean_str(data.get('email')) or None,
             phone=_clean_str(data.get('phone')) or None,
             interest=_clean_str(data.get('interest')) or None,
             source=_clean_str(data.get('source')) or None,
             trial_date=_parse_date(data.get('trial_date')),
             note=_clean_str(data.get('note')) or None)
    db.session.add(l)
    db.session.commit()
    return jsonify(_lead_to_dict(l)), 201


@bp.route('/leads/<int:lid>', methods=['PUT'])
@login_required
def update_lead(lid):
    err = _admin_only()
    if err:
        return err
    l = Lead.query.get_or_404(lid)
    data = request.get_json() or {}
    for f in ('name', 'email', 'phone', 'interest', 'source', 'note'):
        if f in data:
            val = _clean_str(data[f]) or None
            if f == 'name' and not val:
                return jsonify({'error': 'name cannot be empty'}), 400  # NOT NULL column
            setattr(l, f, val)
    if 'status' in data and data['status'] in ('new', 'contacted', 'trial_scheduled', 'converted', 'lost'):
        l.status = data['status']
    if 'trial_date' in data:
        l.trial_date = _parse_date(data['trial_date'])
    db.session.commit()
    return jsonify(_lead_to_dict(l))


@bp.route('/leads/<int:lid>', methods=['DELETE'])
@login_required
def delete_lead(lid):
    err = _admin_only()
    if err:
        return err
    db.session.delete(Lead.query.get_or_404(lid))
    db.session.commit()
    return jsonify({'message': 'Lead removed'})


@bp.route('/leads/<int:lid>/convert', methods=['POST'])
@login_required
def convert_lead(lid):
    """Create a Family + Student from a lead and mark it converted."""
    err = _admin_only()
    if err:
        return err
    l = Lead.query.get_or_404(lid)
    if l.status == 'converted':
        # Idempotent: a double-click (or converting an already-converted lead)
        # must not create a second duplicate family + student.
        return jsonify({'error': 'This lead was already converted'}), 400
    parts = (_clean_str(l.name) or 'New Dancer').split(' ', 1)
    fam = Family(name=f'{l.name} Family', primary_email=l.email, primary_phone=l.phone)
    db.session.add(fam)
    db.session.flush()
    student = Student(first_name=parts[0], last_name=parts[1] if len(parts) > 1 else '',
                      family_id=fam.id, parent_email=l.email, parent_phone=l.phone)
    db.session.add(student)
    l.status = 'converted'
    AuditLog.record(current_user.id, 'lead.convert', f'Converted lead {l.name} to a student')
    db.session.commit()
    return jsonify({'message': f'Created {student.full_name} from lead'})


# ── Staff time clock ────────────────────────────────────────────────

@bp.route('/timeclock/me', methods=['GET'])
@login_required
def timeclock_me():
    if not current_user.is_staff:
        return jsonify({'error': 'Staff access required'}), 403
    open_entry = TimeClockEntry.query.filter_by(user_id=current_user.id, clock_out=None).first()
    recent = (TimeClockEntry.query.filter_by(user_id=current_user.id)
              .order_by(desc(TimeClockEntry.clock_in)).limit(20).all())
    return jsonify({
        'open': bool(open_entry),
        # Clock punches are studio-local — emit without 'Z' (see _local_iso).
        'open_since': _local_iso(open_entry.clock_in) if open_entry else None,
        'entries': [{
            'id': e.id, 'clock_in': _local_iso(e.clock_in),
            'clock_out': _local_iso(e.clock_out), 'hours': e.hours,
        } for e in recent],
    })


@bp.route('/timeclock/clock-in', methods=['POST'])
@login_required
def clock_in():
    if not current_user.is_staff:
        return jsonify({'error': 'Staff access required'}), 403
    if TimeClockEntry.query.filter_by(user_id=current_user.id, clock_out=None).first():
        return jsonify({'error': "You're already clocked in"}), 400
    e = TimeClockEntry(user_id=current_user.id)
    db.session.add(e)
    db.session.commit()
    return jsonify({'message': 'Clocked in', 'clock_in': _local_iso(e.clock_in)}), 201


@bp.route('/timeclock/clock-out', methods=['POST'])
@login_required
def clock_out():
    if not current_user.is_staff:
        return jsonify({'error': 'Staff access required'}), 403
    e = TimeClockEntry.query.filter_by(user_id=current_user.id, clock_out=None).first()
    if not e:
        return jsonify({'error': "You're not clocked in"}), 400
    # Local time (studio timezone) so it matches clock_in and the local-date
    # bounds the payroll report filters on — datetime.utcnow() would push an
    # evening shift's timestamps onto the next UTC day and slip it out of the
    # report window. Duration (hours) is unaffected (both ends move together).
    e.clock_out = datetime.now()
    db.session.commit()
    return jsonify({'message': f'Clocked out — {e.hours} hrs', 'hours': e.hours})


@bp.route('/timeclock/report', methods=['GET'])
@login_required
def timeclock_report():
    err = _admin_only()
    if err:
        return err
    start = _parse_date(request.args.get('start')) or (date.today() - timedelta(days=30))
    end = _parse_date(request.args.get('end')) or date.today()
    entries = (TimeClockEntry.query
               .filter(TimeClockEntry.clock_in >= datetime.combine(start, datetime.min.time()),
                       TimeClockEntry.clock_in <= datetime.combine(end, datetime.max.time()),
                       TimeClockEntry.clock_out.isnot(None))
               .all())
    by_user = {}
    for e in entries:
        by_user.setdefault(e.user_id, {
            'name': e.user.full_name if e.user else '(removed user)', 'hours': 0.0, 'shifts': 0})
        by_user[e.user_id]['hours'] += e.hours or 0
        by_user[e.user_id]['shifts'] += 1
    report = sorted(by_user.values(), key=lambda x: -x['hours'])
    for r in report:
        r['hours'] = round(r['hours'], 2)
    return jsonify({'start': start.isoformat(), 'end': end.isoformat(), 'report': report,
                    'total_hours': round(sum(r['hours'] for r in report), 2)})


# ── Retention / analytics dashboard ─────────────────────────────────

@bp.route('/analytics/retention', methods=['GET'])
@login_required
def analytics_retention():
    err = _admin_only()
    if err:
        return err
    today = date.today()
    month_start = today.replace(day=1)
    # Local: compared against Attendance.check_in_time, which is stored studio-local.
    cutoff_30 = datetime.now() - timedelta(days=30)

    active = Student.query.filter_by(is_active=True).count()
    inactive = Student.query.filter_by(is_active=False).count()
    new_this_month = Student.query.filter(Student.enrollment_date >= month_start).count()

    # At-risk: active students with no attendance in the last 30 days
    active_students = Student.query.filter_by(is_active=True).all()
    recent_att_ids = {row[0] for row in db.session.query(Attendance.student_id)
                      .filter(Attendance.check_in_time >= cutoff_30).distinct().all()}
    at_risk = [s.full_name for s in active_students if s.id not in recent_att_ids]

    # Enrollment by month (last 12 months) using enrollment_date
    enroll_by_month = []
    for i in range(11, -1, -1):
        m = month_start
        # step back i months
        year = m.year + (m.month - 1 - i) // 12
        month = (m.month - 1 - i) % 12 + 1
        ms = date(year, month, 1)
        me = date(year + (month // 12), (month % 12) + 1, 1)
        count = Student.query.filter(Student.enrollment_date >= ms, Student.enrollment_date < me).count()
        enroll_by_month.append({'month': ms.strftime('%b %y'), 'count': count})

    # Attendance by month (last 6 months)
    att_by_month = []
    for i in range(5, -1, -1):
        year = month_start.year + (month_start.month - 1 - i) // 12
        month = (month_start.month - 1 - i) % 12 + 1
        ms = date(year, month, 1)
        me = date(year + (month // 12), (month % 12) + 1, 1)
        count = Attendance.query.filter(func.date(Attendance.check_in_time) >= ms,
                                        func.date(Attendance.check_in_time) < me).count()
        att_by_month.append({'month': ms.strftime('%b %y'), 'count': count})

    # Students per class (top 10 active classes by enrollment)
    per_class = []
    for cls in live_class_query().all():
        per_class.append({'name': cls.name, 'count': cls.enrolled_students_count})
    per_class = sorted(per_class, key=lambda x: -x['count'])[:10]

    return jsonify({
        'active': active, 'inactive': inactive, 'new_this_month': new_this_month,
        'at_risk_count': len(at_risk), 'at_risk': at_risk[:50],
        'enroll_by_month': enroll_by_month,
        'attendance_by_month': att_by_month,
        'students_per_class': per_class,
    })


# ── Recital hub (annual show organizer) ─────────────────────────────

def _staff_guard():
    """403 unless the current user is staff (admin or teacher), else None."""
    if not current_user.is_staff:
        return jsonify({'error': 'Staff access required'}), 403
    return None


def _recital_to_dict(r, detail=False):
    d = {
        'id': r.id, 'year': r.year, 'title': r.title, 'theme': r.theme,
        'recital_date': r.recital_date.isoformat() if r.recital_date else None,
        'show_times': r.show_times, 'venue': r.venue,
        'is_active': r.is_active, 'is_locked': r.is_locked,
        'number_count': r.numbers.count(),
        'award_count': r.awards.count(),
        'ad_count': r.ads.count(),
    }
    if detail:
        d.update({
            'director_note': r.director_note,
            'acknowledgments': r.acknowledgments,
            'ad_pricing_note': r.ad_pricing_note,
            'has_cover': bool(r.cover_image_data),
        })
    return d


def _number_to_dict(n, with_cast=True):
    cast = n.cast.all()
    d = {
        'id': n.id, 'recital_id': n.recital_id, 'order_index': n.order_index,
        'title': n.title,
        'class_id': n.class_id, 'class_name': n.dance_class.name if n.dance_class else None,
        'group_id': n.group_id, 'group_name': n.group.name if n.group else None,
        'style': n.style, 'act': n.act,
        'song_title': n.song_title, 'song_artist': n.song_artist,
        'music_url': n.music_url, 'music_notes': n.music_notes,
        'choreographer': n.choreographer, 'choreo_notes': n.choreo_notes,
        'choreo_url': n.choreo_url, 'formation_notes': n.formation_notes,
        'duration': n.duration, 'props': n.props,
        'is_finale': n.is_finale, 'notes': n.notes,
        'cast_count': len(cast),
        'has_music': bool(n.music_url or n.song_title),
        'has_choreo': bool(n.choreo_notes or n.choreo_url or n.formation_notes),
    }
    if with_cast:
        rows = [{
            'id': c.id, 'student_id': c.student_id,
            'student_name': c.student.full_name if c.student else None, 'part': c.part,
        } for c in cast]
        rows.sort(key=lambda x: (x['part'] or '￿', x['student_name']))
        d['cast'] = rows
    return d


def _award_to_dict(a):
    return {
        'id': a.id, 'recital_id': a.recital_id, 'title': a.title,
        'category': a.category, 'student_id': a.student_id,
        'student_name': a.student.full_name if a.student else None,
        'recipient_text': a.recipient_text, 'description': a.description,
        'order_index': a.order_index,
    }


def _ad_to_dict(a, with_image=False):
    d = {
        'id': a.id, 'recital_id': a.recital_id, 'advertiser': a.advertiser,
        'size': a.size, 'price': f'{float(a.price):.2f}', 'content': a.content,
        'contact_name': a.contact_name, 'contact_email': a.contact_email,
        'student_id': a.student_id,
        'student_name': a.student.full_name if a.student else None,
        'status': a.status, 'paid': a.paid, 'order_index': a.order_index,
        'has_image': bool(a.image_data),
    }
    if with_image:
        d['image_data'] = a.image_data
    return d


# Recitals ----------------------------------------------------------

@bp.route('/recitals', methods=['GET'])
@login_required
def list_recitals():
    err = _staff_guard()
    if err:
        return err
    rows = Recital.query.order_by(Recital.year.desc(), Recital.created_at.desc()).all()
    return jsonify({'recitals': [_recital_to_dict(r) for r in rows]})


@bp.route('/recitals/<int:rid>', methods=['GET'])
@login_required
def get_recital(rid):
    err = _staff_guard()
    if err:
        return err
    r = Recital.query.get_or_404(rid)
    numbers = r.numbers.order_by(RecitalNumber.order_index).all()
    awards = r.awards.order_by(RecitalAward.order_index, RecitalAward.id).all()
    ads = r.ads.order_by(RecitalAd.order_index, RecitalAd.id).all()
    perfs = r.performances.order_by(Performance.performance_date).all()
    return jsonify({
        'recital': _recital_to_dict(r, detail=True),
        'numbers': [_number_to_dict(n) for n in numbers],
        'awards': [_award_to_dict(a) for a in awards],
        'ads': [_ad_to_dict(a) for a in ads],
        'performances': [_performance_to_dict(p) for p in perfs],
    })


@bp.route('/recitals', methods=['POST'])
@login_required
def create_recital():
    err = _admin_only()
    if err:
        return err
    data = request.get_json() or {}
    if not data.get('title'):
        return jsonify({'error': 'title is required'}), 400
    try:
        year = int(data.get('year') or date.today().year)
    except (TypeError, ValueError):
        return jsonify({'error': 'year must be a number'}), 400
    r = Recital(
        year=year,
        title=_clean_str(data['title']),
        theme=_clean_str(data.get('theme')) or None,
        recital_date=_parse_date(data.get('recital_date')),
        show_times=_clean_str(data.get('show_times')) or None,
        venue=_clean_str(data.get('venue')) or None,
    )
    # First recital created becomes active automatically
    if Recital.query.count() == 0:
        r.is_active = True
    db.session.add(r)
    AuditLog.record(current_user.id, 'recital.create', f'Created recital {r.year}: {r.title}')
    db.session.commit()
    return jsonify(_recital_to_dict(r, detail=True)), 201


@bp.route('/recitals/<int:rid>', methods=['PUT'])
@login_required
def update_recital(rid):
    err = _admin_only()
    if err:
        return err
    r = Recital.query.get_or_404(rid)
    data = request.get_json() or {}
    if 'title' in data and _clean_str(data['title']):
        r.title = _clean_str(data['title'])
    if 'year' in data and data['year']:
        try:
            r.year = int(data['year'])
        except (TypeError, ValueError):
            return jsonify({'error': 'year must be a number'}), 400
    if 'theme' in data:
        r.theme = _clean_str(data['theme']) or None
    if 'recital_date' in data:
        r.recital_date = _parse_date(data.get('recital_date'))
    if 'show_times' in data:
        r.show_times = _clean_str(data['show_times']) or None
    if 'venue' in data:
        r.venue = _clean_str(data['venue']) or None
    if 'director_note' in data:
        r.director_note = _clean_str(data['director_note']) or None
    if 'acknowledgments' in data:
        r.acknowledgments = _clean_str(data['acknowledgments']) or None
    if 'ad_pricing_note' in data:
        r.ad_pricing_note = _clean_str(data['ad_pricing_note']) or None
    if 'is_locked' in data:
        r.is_locked = bool(data['is_locked'])
    db.session.commit()
    return jsonify(_recital_to_dict(r, detail=True))


@bp.route('/recitals/<int:rid>/activate', methods=['POST'])
@login_required
def activate_recital(rid):
    err = _admin_only()
    if err:
        return err
    r = Recital.query.get_or_404(rid)
    Recital.query.update({Recital.is_active: False})
    r.is_active = True
    AuditLog.record(current_user.id, 'recital.activate', f'Set active recital: {r.year} {r.title}')
    db.session.commit()
    return jsonify(_recital_to_dict(r, detail=True))


@bp.route('/recitals/<int:rid>', methods=['DELETE'])
@login_required
def delete_recital(rid):
    err = _admin_only()
    if err:
        return err
    r = Recital.query.get_or_404(rid)
    # Detach any linked performances so their FK doesn't dangle
    for p in r.performances.all():
        p.recital_id = None
    AuditLog.record(current_user.id, 'recital.delete', f'Deleted recital {r.year}: {r.title}')
    db.session.delete(r)
    db.session.commit()
    return jsonify({'message': 'Recital deleted'})


@bp.route('/recitals/<int:rid>/cover', methods=['GET'])
@login_required
def get_recital_cover(rid):
    err = _staff_guard()
    if err:
        return err
    r = Recital.query.get_or_404(rid)
    return jsonify({'image_data': r.cover_image_data or ''})


@bp.route('/recitals/<int:rid>/cover', methods=['POST'])
@login_required
def upload_recital_cover(rid):
    import base64
    err = _admin_only()
    if err:
        return err
    r = Recital.query.get_or_404(rid)
    data_uri = _image_data_uri_from_request()
    if isinstance(data_uri, tuple):
        return data_uri  # error response
    r.cover_image_data = data_uri
    AuditLog.record(current_user.id, 'recital.cover', f'Updated cover for recital {r.id}')
    db.session.commit()
    return jsonify({'message': 'Cover updated', 'has_cover': True})


def _image_data_uri_from_request(max_mb=2):
    """Shared multipart image -> data-URI helper (mirrors the Zelle QR upload).

    The content type is whitelisted to EXACT values, never prefix-checked: the
    mimetype comes from the client's multipart Content-Type header, so a prefix
    check (startswith 'image/') would let `image/png"><script>` through and into
    the data URI. These URIs render into an <img src=> in the booklet (Jinja
    autoescapes there today) — the exact whitelist keeps them structurally
    injection-proof regardless of the render context, matching the QR endpoint."""
    import base64
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400
    f = request.files['file']
    if not f.filename:
        return jsonify({'error': 'No file selected'}), 400
    raw = f.read()
    if len(raw) > max_mb * 1024 * 1024:
        return jsonify({'error': f'Image too large (max {max_mb}MB)'}), 400
    VALID_IMAGE_TYPES = {'image/png', 'image/jpeg', 'image/gif', 'image/webp'}
    ext_map = {'png': 'image/png', 'jpg': 'image/jpeg', 'jpeg': 'image/jpeg',
               'gif': 'image/gif', 'webp': 'image/webp'}
    content_type = (f.mimetype or '').lower()
    if content_type not in VALID_IMAGE_TYPES:
        ext = f.filename.rsplit('.', 1)[-1].lower() if '.' in f.filename else ''
        content_type = ext_map.get(ext, '')
    if content_type not in VALID_IMAGE_TYPES:
        return jsonify({'error': 'File must be an image (PNG, JPG, GIF, or WebP)'}), 400
    return f'data:{content_type};base64,' + base64.b64encode(raw).decode('ascii')


# Recital numbers (show order + music/choreography) — any staff -------

@bp.route('/recitals/<int:rid>/numbers', methods=['GET'])
@login_required
def list_recital_numbers(rid):
    err = _staff_guard()
    if err:
        return err
    r = Recital.query.get_or_404(rid)
    numbers = r.numbers.order_by(RecitalNumber.order_index).all()
    return jsonify({'numbers': [_number_to_dict(n) for n in numbers]})


@bp.route('/recitals/<int:rid>/numbers', methods=['POST'])
@login_required
def create_recital_number(rid):
    err = _staff_guard()
    if err:
        return err
    r = Recital.query.get_or_404(rid)
    data = request.get_json() or {}
    if not data.get('title'):
        return jsonify({'error': 'title is required'}), 400
    last = r.numbers.order_by(RecitalNumber.order_index.desc()).first()
    n = RecitalNumber(
        recital_id=r.id,
        order_index=(last.order_index + 1) if last else 1,
        title=_clean_str(data['title']),
        class_id=_opt_int(data.get('class_id')),
        group_id=_opt_int(data.get('group_id')),
        style=_clean_str(data.get('style')) or None,
        act=_clean_str(data.get('act')) or None,
    )
    db.session.add(n)
    db.session.commit()
    return jsonify(_number_to_dict(n)), 201


NUMBER_TEXT_FIELDS = [
    'title', 'style', 'act', 'song_title', 'song_artist', 'music_url',
    'music_notes', 'choreographer', 'choreo_notes', 'choreo_url',
    'formation_notes', 'duration', 'props', 'notes',
]


@bp.route('/recital-numbers/<int:nid>', methods=['PUT'])
@login_required
def update_recital_number(nid):
    err = _staff_guard()
    if err:
        return err
    n = RecitalNumber.query.get_or_404(nid)
    data = request.get_json() or {}
    for field in NUMBER_TEXT_FIELDS:
        if field in data:
            val = _clean_str(data[field])
            if field == 'title' and not val:
                continue  # never blank the title
            setattr(n, field, val or None)
    if 'class_id' in data:
        n.class_id = _opt_int(data['class_id'])
    if 'group_id' in data:
        n.group_id = _opt_int(data['group_id'])
    if 'is_finale' in data:
        n.is_finale = bool(data['is_finale'])
    db.session.commit()
    return jsonify(_number_to_dict(n))


@bp.route('/recital-numbers/<int:nid>', methods=['DELETE'])
@login_required
def delete_recital_number(nid):
    err = _staff_guard()
    if err:
        return err
    n = RecitalNumber.query.get_or_404(nid)
    db.session.delete(n)
    db.session.commit()
    return jsonify({'message': 'Number removed'})


@bp.route('/recitals/<int:rid>/numbers/reorder', methods=['POST'])
@login_required
def reorder_recital_numbers(rid):
    err = _staff_guard()
    if err:
        return err
    r = Recital.query.get_or_404(rid)
    data = request.get_json() or {}
    order = data.get('order') or []
    by_id = {n.id: n for n in r.numbers.all()}
    pos = 0
    for nid in order:
        n = by_id.get(int(nid))
        if n:
            pos += 1
            n.order_index = pos
    db.session.commit()
    return jsonify({'message': 'Reordered', 'count': pos})


# Cast --------------------------------------------------------------

@bp.route('/recital-numbers/<int:nid>/cast', methods=['GET'])
@login_required
def list_recital_cast(nid):
    err = _staff_guard()
    if err:
        return err
    n = RecitalNumber.query.get_or_404(nid)
    return jsonify(_number_to_dict(n))


@bp.route('/recital-numbers/<int:nid>/cast', methods=['POST'])
@login_required
def add_recital_cast(nid):
    err = _staff_guard()
    if err:
        return err
    n = RecitalNumber.query.get_or_404(nid)
    data = request.get_json() or {}

    def _add(sid, part=None):
        # Only cast students that actually exist (skip bad ids) so we don't
        # orphan a RecitalCast row that then 500s the number's cast display.
        if Student.query.get(sid) is None:
            return 0
        if not RecitalCast.query.filter_by(number_id=nid, student_id=sid).first():
            db.session.add(RecitalCast(number_id=nid, student_id=sid, part=part))
            return 1
        return 0

    # Fill cast from the number's linked class/group
    if data.get('fill_from_class'):
        sids = []
        if n.class_id:
            sids = [e.student_id for e in ClassEnrollment.query.filter_by(class_id=n.class_id, is_active=True).all()]
        elif n.group_id:
            sids = [m.student_id for m in CompanyMembership.query.filter_by(group_id=n.group_id, is_active=True).all()]
        if not sids:
            return jsonify({'error': 'Link a class or group to this number first'}), 400
        added = sum(_add(s) for s in sids)
        db.session.commit()
        return jsonify({'message': f'Added {added} dancers', 'number': _number_to_dict(n)}), 201

    if data.get('student_ids'):
        raw_ids = data['student_ids']
        if not isinstance(raw_ids, list):  # a non-list (e.g. int) would TypeError the loop
            return jsonify({'error': 'student_ids must be a list'}), 400
        ids = []
        for s in raw_ids:
            v, _e = _valid_id(s)
            if not _e:
                ids.append(v)
        added = sum(_add(s) for s in ids)
        db.session.commit()
        return jsonify({'message': f'Added {added} dancers', 'number': _number_to_dict(n)}), 201

    student_id, serr = _resolve_student_id(data.get('student_id'))
    if serr:
        return serr
    added = _add(student_id, _clean_str(data.get('part')) or None)
    if not added:
        return jsonify({'error': 'Dancer already in this number'}), 400
    db.session.commit()
    return jsonify({'message': 'Added', 'number': _number_to_dict(n)}), 201


@bp.route('/recital-cast/<int:cid>', methods=['PUT'])
@login_required
def update_recital_cast(cid):
    err = _staff_guard()
    if err:
        return err
    c = RecitalCast.query.get_or_404(cid)
    data = request.get_json() or {}
    if 'part' in data:
        c.part = _clean_str(data['part']) or None
    db.session.commit()
    return jsonify({'message': 'Updated'})


@bp.route('/recital-cast/<int:cid>', methods=['DELETE'])
@login_required
def delete_recital_cast(cid):
    err = _staff_guard()
    if err:
        return err
    c = RecitalCast.query.get_or_404(cid)
    db.session.delete(c)
    db.session.commit()
    return jsonify({'message': 'Removed'})


# Awards (admin) ----------------------------------------------------

@bp.route('/recitals/<int:rid>/awards', methods=['GET'])
@login_required
def list_recital_awards(rid):
    err = _staff_guard()
    if err:
        return err
    r = Recital.query.get_or_404(rid)
    rows = r.awards.order_by(RecitalAward.order_index, RecitalAward.id).all()
    return jsonify({'awards': [_award_to_dict(a) for a in rows]})


@bp.route('/recitals/<int:rid>/awards', methods=['POST'])
@login_required
def create_recital_award(rid):
    err = _admin_only()
    if err:
        return err
    r = Recital.query.get_or_404(rid)
    data = request.get_json() or {}
    if not data.get('title'):
        return jsonify({'error': 'title is required'}), 400
    award_student_id, serr = _resolve_student_id(data.get('student_id'), required=False)
    if serr:
        return serr
    last = r.awards.order_by(RecitalAward.order_index.desc()).first()
    a = RecitalAward(
        recital_id=r.id,
        title=_clean_str(data['title']),
        category=_clean_str(data.get('category')) or None,
        student_id=award_student_id,
        recipient_text=_clean_str(data.get('recipient_text')) or None,
        description=_clean_str(data.get('description')) or None,
        order_index=(last.order_index + 1) if last else 1,
    )
    db.session.add(a)
    db.session.commit()
    return jsonify(_award_to_dict(a)), 201


@bp.route('/recital-awards/<int:aid>', methods=['PUT'])
@login_required
def update_recital_award(aid):
    err = _admin_only()
    if err:
        return err
    a = RecitalAward.query.get_or_404(aid)
    data = request.get_json() or {}
    if 'title' in data and _clean_str(data['title']):
        a.title = _clean_str(data['title'])
    if 'category' in data:
        a.category = _clean_str(data['category']) or None
    if 'student_id' in data:
        a.student_id = _opt_int(data.get('student_id'))
    if 'recipient_text' in data:
        a.recipient_text = _clean_str(data['recipient_text']) or None
    if 'description' in data:
        a.description = _clean_str(data['description']) or None
    if 'order_index' in data:
        try:
            a.order_index = int(data['order_index'])
        except (TypeError, ValueError):
            pass
    db.session.commit()
    return jsonify(_award_to_dict(a))


@bp.route('/recital-awards/<int:aid>', methods=['DELETE'])
@login_required
def delete_recital_award(aid):
    err = _admin_only()
    if err:
        return err
    a = RecitalAward.query.get_or_404(aid)
    db.session.delete(a)
    db.session.commit()
    return jsonify({'message': 'Award removed'})


# Booklet ads (admin) -----------------------------------------------

AD_SIZES = {'full_page', 'half_page', 'quarter_page', 'business_card', 'shout_out'}


@bp.route('/recitals/<int:rid>/ads', methods=['GET'])
@login_required
def list_recital_ads(rid):
    err = _staff_guard()
    if err:
        return err
    r = Recital.query.get_or_404(rid)
    rows = r.ads.order_by(RecitalAd.order_index, RecitalAd.id).all()
    return jsonify({'ads': [_ad_to_dict(a) for a in rows]})


@bp.route('/recitals/<int:rid>/ads', methods=['POST'])
@login_required
def create_recital_ad(rid):
    err = _admin_only()
    if err:
        return err
    r = Recital.query.get_or_404(rid)
    data = request.get_json() or {}
    if not data.get('advertiser'):
        return jsonify({'error': 'advertiser is required'}), 400
    ad_student_id, serr = _resolve_student_id(data.get('student_id'), required=False)
    if serr:
        return serr
    size = (data.get('size') or 'shout_out').strip()
    if size not in AD_SIZES:
        size = 'shout_out'
    last = r.ads.order_by(RecitalAd.order_index.desc()).first()
    a = RecitalAd(
        recital_id=r.id,
        advertiser=_clean_str(data['advertiser']),
        size=size,
        price=data.get('price') or 0,
        content=_clean_str(data.get('content')) or None,
        contact_name=_clean_str(data.get('contact_name')) or None,
        contact_email=_clean_str(data.get('contact_email')) or None,
        student_id=ad_student_id,
        status=(data.get('status') or 'submitted').strip(),
        paid=bool(data.get('paid')),
        order_index=(last.order_index + 1) if last else 1,
    )
    if a.paid:
        a.paid_at = datetime.utcnow()
    db.session.add(a)
    db.session.commit()
    return jsonify(_ad_to_dict(a)), 201


@bp.route('/recital-ads/<int:aid>', methods=['PUT'])
@login_required
def update_recital_ad(aid):
    err = _admin_only()
    if err:
        return err
    a = RecitalAd.query.get_or_404(aid)
    data = request.get_json() or {}
    if 'advertiser' in data and _clean_str(data['advertiser']):
        a.advertiser = _clean_str(data['advertiser'])
    if 'size' in data and data['size'] in AD_SIZES:
        a.size = data['size']
    if 'price' in data:
        try:
            a.price = round(float(data['price'] or 0), 2)
        except (TypeError, ValueError):
            pass  # bad price keeps the old value (never let it reach the Numeric column)
    if 'content' in data:
        a.content = _clean_str(data['content']) or None
    if 'contact_name' in data:
        a.contact_name = _clean_str(data['contact_name']) or None
    if 'contact_email' in data:
        a.contact_email = _clean_str(data['contact_email']) or None
    if 'student_id' in data:
        a.student_id = _opt_int(data['student_id'])
    if 'status' in data:
        a.status = _clean_str(data['status']) or 'submitted'
    if 'order_index' in data:
        try:
            a.order_index = int(data['order_index'])
        except (TypeError, ValueError):
            pass
    if 'paid' in data:
        a.paid = bool(data['paid'])
        a.paid_at = datetime.utcnow() if a.paid else None
    db.session.commit()
    return jsonify(_ad_to_dict(a))


@bp.route('/recital-ads/<int:aid>/image', methods=['GET'])
@login_required
def get_recital_ad_image(aid):
    """Serve a booklet ad's uploaded image as raw bytes (for <img src>)."""
    import base64
    from flask import Response
    err = _staff_guard()
    if err:
        return err
    a = RecitalAd.query.get_or_404(aid)
    if not a.image_data or ',' not in a.image_data:
        return jsonify({'error': 'No image'}), 404
    header, b64 = a.image_data.split(',', 1)
    mime = 'image/png'
    if header.startswith('data:') and ';' in header:
        mime = header[5:header.index(';')]
    try:
        raw = base64.b64decode(b64)
    except (ValueError, TypeError):
        return jsonify({'error': 'Bad image data'}), 500
    return Response(raw, mimetype=mime)


@bp.route('/recital-ads/<int:aid>/image', methods=['POST'])
@login_required
def upload_recital_ad_image(aid):
    err = _admin_only()
    if err:
        return err
    a = RecitalAd.query.get_or_404(aid)
    data_uri = _image_data_uri_from_request()
    if isinstance(data_uri, tuple):
        return data_uri  # error response
    a.image_data = data_uri
    AuditLog.record(current_user.id, 'recital.ad_image', f'Uploaded ad image for "{a.advertiser}"')
    db.session.commit()
    return jsonify({'message': 'Ad image uploaded', 'has_image': True})


@bp.route('/recital-ads/<int:aid>/image', methods=['DELETE'])
@login_required
def delete_recital_ad_image(aid):
    err = _admin_only()
    if err:
        return err
    a = RecitalAd.query.get_or_404(aid)
    a.image_data = None
    db.session.commit()
    return jsonify({'message': 'Ad image removed'})


@bp.route('/recital-ads/<int:aid>', methods=['DELETE'])
@login_required
def delete_recital_ad(aid):
    err = _admin_only()
    if err:
        return err
    a = RecitalAd.query.get_or_404(aid)
    db.session.delete(a)
    db.session.commit()
    return jsonify({'message': 'Ad removed'})


# Link existing show dates (Performances) to the recital ------------

@bp.route('/recitals/<int:rid>/link-performance', methods=['POST'])
@login_required
def link_performance(rid):
    err = _admin_only()
    if err:
        return err
    Recital.query.get_or_404(rid)
    data = request.get_json() or {}
    pid = data.get('performance_id')
    if not pid:
        return jsonify({'error': 'performance_id is required'}), 400
    p = Performance.query.get_or_404(int(pid))
    p.recital_id = None if data.get('unlink') else rid
    db.session.commit()
    return jsonify({'message': 'Unlinked' if data.get('unlink') else 'Linked'})


# Parent-facing read-only recital view ------------------------------

@bp.route('/my-recital', methods=['GET'])
@login_required
def my_recital():
    """The active recital's program, with the parent's own dancers highlighted."""
    if not current_user.is_parent:
        return jsonify({'recital': None})
    r = Recital.query.filter_by(is_active=True).first()
    if not r:
        return jsonify({'recital': None})
    student_ids = _parent_student_ids(current_user)
    numbers = r.numbers.order_by(RecitalNumber.order_index).all()
    program, my_count = [], 0
    for n in numbers:
        mine = [{'student_name': c.student.full_name if c.student else None, 'part': c.part}
                for c in n.cast.all() if c.student_id in student_ids]
        if mine:
            my_count += 1
        program.append({
            'order_index': n.order_index, 'title': n.title, 'style': n.style,
            'act': n.act, 'is_finale': n.is_finale, 'my_dancers': mine,
        })
    return jsonify({
        'recital': {
            'title': r.title, 'theme': r.theme,
            'recital_date': r.recital_date.isoformat() if r.recital_date else None,
            'show_times': r.show_times, 'venue': r.venue,
        },
        'program': program,
        'my_number_count': my_count,
    })


# ── Personal data export / deletion request ─────────────────────────
# "Download My Data" (COPPA + state privacy access/portability rights). A
# parent's own account plus every record tied to their linked children; staff
# get their own account plus their own time-clock punches. No target-id param
# (always "your own"), so this needs no IDOR guard beyond @login_required.

@bp.route('/my-data', methods=['GET'])
@login_required
def my_data_export():
    from app.models import RFIDLog

    export = {
        'export_generated_at': _utc_iso(datetime.utcnow()),
        'studio': STUDIO_NAME,
        'account': {
            'id': current_user.id,
            'username': current_user.username,
            'email': current_user.email,
            'first_name': current_user.first_name,
            'last_name': current_user.last_name,
            'phone': current_user.phone,
            'role': current_user.role,
            'created_at': _utc_iso(current_user.created_at),
            'last_login': _utc_iso(current_user.last_login),
        },
    }

    if current_user.is_parent:
        child_blocks = []
        for child in current_user.get_children():
            rule_acks = RuleAcknowledgment.query.filter_by(
                student_id=child.id, parent_id=current_user.id).all()
            rfid_logs = (RFIDLog.query.filter_by(student_id=child.id)
                         .order_by(RFIDLog.scan_time).all())
            child_blocks.append({
                **student_to_dict(child),
                'attendance': [attendance_to_dict(a) for a in
                               child.attendances.order_by(Attendance.check_in_time).all()],
                'class_enrollments': [{
                    'class_name': e.dance_class.name if e.dance_class else None,
                    'enrolled_date': e.enrolled_date.isoformat(),
                    'is_active': e.is_active,
                } for e in child.class_enrollments.all()],
                'transactions': [transaction_to_dict(t) for t in
                                  child.transactions.order_by(Transaction.transaction_date).all()],
                'payment_plans': [_plan_to_dict(p) for p in
                                   PaymentPlan.query.filter_by(student_id=child.id).all()],
                'rule_acknowledgments': [{
                    'rule_text': a.rule.text if a.rule else None,
                    'initials': a.initials,
                    'acknowledged_at': _utc_iso(a.acknowledged_at),
                } for a in rule_acks],
                'waiver_signatures': [{
                    'template_title': w.template.title if w.template else None,
                    'consent': w.consent,
                    'signed_name': w.signed_name,
                    'signed_at': _utc_iso(w.signed_at),
                } for w in WaiverSignature.query.filter_by(student_id=child.id).all()],
                'company_memberships': [{
                    'group_name': m.group.name if m.group else None,
                    'role': m.role,
                    'joined_date': m.joined_date.isoformat(),
                    'is_active': m.is_active,
                } for m in CompanyMembership.query.filter_by(student_id=child.id).all()],
                'audition_signups': [{
                    'audition_title': a.audition.title if a.audition else None,
                    'status': a.status,
                    'notes': a.notes,
                    'created_at': _utc_iso(a.created_at),
                } for a in AuditionSignup.query.filter_by(student_id=child.id).all()],
                'performance_assignments': [{
                    'performance_title': p.performance.title if p.performance else None,
                    'performance_date': (p.performance.performance_date.isoformat()
                                          if p.performance and p.performance.performance_date else None),
                    'notes': p.notes,
                } for p in PerformanceAssignment.query.filter_by(student_id=child.id).all()],
                'costume_assignments': [{
                    'costume_name': c.costume.name if c.costume else None,
                    'size': c.size,
                    'paid': c.paid,
                    'notes': c.notes,
                } for c in CostumeAssignment.query.filter_by(student_id=child.id).all()],
                'skill_achievements': [{
                    'skill_name': s.skill.name if s.skill else None,
                    'achieved_at': _utc_iso(s.achieved_at),
                    'note': s.note,
                } for s in StudentSkill.query.filter_by(student_id=child.id).all()],
                'makeup_requests': [_makeup_to_dict(m) for m in
                                     MakeupClass.query.filter_by(student_id=child.id).all()],
                'waitlist_entries': [{
                    'class_name': w.dance_class.name if w.dance_class else None,
                    'status': w.status,
                    'created_at': _utc_iso(w.created_at),
                } for w in WaitlistEntry.query.filter_by(student_id=child.id).all()],
                'recital_cast': [{
                    'recital_year': c.number.recital.year if c.number and c.number.recital else None,
                    'recital_title': c.number.recital.title if c.number and c.number.recital else None,
                    'number_title': c.number.title if c.number else None,
                    'part': c.part,
                } for c in RecitalCast.query.filter_by(student_id=child.id).all()],
                'recital_awards': [{
                    'recital_year': a.recital.year if a.recital else None,
                    'title': a.title,
                    'category': a.category,
                    'description': a.description,
                } for a in RecitalAward.query.filter_by(student_id=child.id).all()],
                'rfid_scan_log': [{
                    'scan_time': _utc_iso(r.scan_time),
                    'action_taken': r.action_taken,
                    'success': r.success,
                } for r in rfid_logs],
            })
        export['children'] = child_blocks
        export['pending_payments'] = [_pending_to_dict(p) for p in
            PendingPayment.query.filter_by(parent_id=current_user.id).all()]
        export['donations'] = [_donation_to_dict(d) for d in
            Donation.query.filter_by(parent_id=current_user.id).all()]
        export['ticket_orders'] = [{
            'ticket_type': t.ticket_type.name if t.ticket_type else None,
            'performance_title': (t.ticket_type.performance.title
                                   if t.ticket_type and t.ticket_type.performance else None),
            'quantity': t.quantity,
            'amount': f'{t.amount:.2f}' if t.amount is not None else None,
            'paid': t.paid,
            'created_at': _utc_iso(t.created_at),
        } for t in TicketOrder.query.filter_by(parent_id=current_user.id).all()]
    else:
        export['time_clock_entries'] = [{
            'clock_in': _utc_iso(e.clock_in),
            'clock_out': _utc_iso(e.clock_out),
            'hours': e.hours,
            'note': e.note,
        } for e in TimeClockEntry.query.filter_by(user_id=current_user.id)
                    .order_by(TimeClockEntry.clock_in).all()]

    try:
        AuditLog.record(current_user.id, 'data_export_downloaded',
                        f'{"parent" if current_user.is_parent else "staff"} downloaded personal data export')
        db.session.commit()
    except Exception:  # audit is best-effort; never block the export
        db.session.rollback()

    resp = jsonify(export)
    # Sanitize the username before putting it in a header: strip anything that
    # isn't alphanumeric/-/_ so a crafted username can't inject a quote or CRLF
    # into Content-Disposition (header splitting).
    safe_username = ''.join(c for c in current_user.username if c.isalnum() or c in '-_') or 'account'
    resp.headers['Content-Disposition'] = (
        f'attachment; filename="my-data-{safe_username}-{date.today().isoformat()}.json"'
    )
    # This is a full PII bundle — never let a proxy or browser cache it.
    resp.headers['Cache-Control'] = 'no-store'
    return resp


@bp.route('/my-data/delete-request', methods=['POST'])
@login_required
def request_data_deletion():
    """A parent asks the studio to delete their/their dancer's data. We don't
    auto-erase: some records (billing, tax/accounting) must be kept regardless
    of the request, and deciding what's safe to remove needs a human — so this
    notifies the studio and logs the request rather than deleting anything."""
    if not current_user.is_parent:
        return jsonify({'error': 'Parent access required'}), 403

    from app import email as email_service

    data = request.get_json(silent=True) or {}
    note = _clean_str(data.get('note'), maxlen=2000)
    children = ', '.join(c.full_name for c in current_user.get_children()) or '(no linked dancers)'

    detail = f'Requested by {current_user.full_name} <{current_user.email}> for: {children}'
    if note:
        detail += f' — note: {note}'
    AuditLog.record(current_user.id, 'data_deletion_requested', detail)
    db.session.commit()

    emailed = False
    if email_service.is_configured():
        reply_to = current_app.config.get('MAIL_REPLY_TO')
        if reply_to:
            body = (f"{current_user.full_name} ({current_user.email}) requested deletion of their "
                    f"account/data for: {children}.\n\n"
                    + (f"Their note: {note}\n\n" if note else '')
                    + "Please follow up directly to confirm what can be removed (financial/legal "
                      "records may need to be kept) and complete the request.")
            try:
                email_service.send_email(reply_to, "Data deletion request — LaShelle's School of Dance", body)
                emailed = True
            except Exception:
                logger.exception("Deletion-request notification email failed")

    return jsonify({
        'message': ("Your request was sent to the studio — they'll follow up with you." if emailed
                    else "Your request was recorded. The studio will follow up with you directly."),
    })
