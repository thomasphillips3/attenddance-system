"""Main web interface routes for AttenDANCE system."""

from datetime import date, timedelta
from functools import wraps

from flask import redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import desc, func

from app import db
from app.helpers import calc_balance, live_class_query
from app.main import bp
from app.models import (
    Attendance,
    ClassEnrollment,
    DanceClass,
    Donation,
    Family,
    Setting,
    Student,
    Transaction,
)


def staff_required(f):
    """Decorator: only admin/teacher can access."""
    @wraps(f)
    @login_required
    def decorated(*args, **kwargs):
        if current_user.is_parent:
            return redirect(url_for('main.parent_dashboard'))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    """Decorator: only admin can access."""
    @wraps(f)
    @login_required
    def decorated(*args, **kwargs):
        if not current_user.is_admin:
            return redirect(url_for('main.dashboard'))
        return f(*args, **kwargs)
    return decorated


@bp.route('/')
def index():
    if current_user.is_authenticated:
        return redirect(url_for('main.dashboard'))
    return redirect(url_for('auth.login'))


@bp.route('/privacy')
def privacy_policy():
    """Public privacy notice — no login required, per COPPA/state notice rules."""
    return render_template('privacy.html')


@bp.route('/dashboard')
@staff_required
def dashboard():
    today = date.today()
    current_weekday = today.weekday()

    total_students = Student.query.filter_by(is_active=True).count()
    total_classes = live_class_query().count()
    todays_attendance = Attendance.query.filter(
        func.date(Attendance.check_in_time) == today
    ).count()
    students_without_rfid = Student.query.filter_by(
        is_active=True, rfid_uid=None
    ).count()

    todays_classes = live_class_query().filter_by(
        day_of_week=current_weekday
    ).order_by(DanceClass.start_time).all()

    from app.models import RFIDLog
    recent_attendance = Attendance.query.order_by(
        desc(Attendance.check_in_time)
    ).limit(10).all()
    recent_rfid_logs = RFIDLog.query.order_by(
        desc(RFIDLog.scan_time)
    ).limit(10).all()

    return render_template('dashboard.html',
        today=today,
        total_students=total_students,
        total_classes=total_classes,
        todays_attendance=todays_attendance,
        students_without_rfid=students_without_rfid,
        todays_classes=todays_classes,
        recent_attendance=recent_attendance,
        recent_rfid_logs=recent_rfid_logs,
    )


@bp.route('/students')
@staff_required
def students():
    return render_template('students/list.html')


@bp.route('/classes')
@staff_required
def classes():
    return render_template('classes/list.html')


@bp.route('/attendance')
@staff_required
def attendance():
    return render_template('attendance/list.html')


@bp.route('/parent')
@login_required
def parent_dashboard():
    if not current_user.is_parent:
        return redirect(url_for('main.dashboard'))
    children = current_user.get_children()
    child_data = []
    family_groups = {}  # family_id -> {name, balance, member_ids, member_names}
    for child in children:
        bal = calc_balance(child.id)
        recent_att = Attendance.query.filter_by(student_id=child.id).order_by(
            desc(Attendance.check_in_time)).limit(10).all()
        # The child's current class schedule — active enrollments in still-active
        # classes (a cancelled class's enrollment is deactivated, so it won't show).
        enr = ClassEnrollment.query.filter_by(student_id=child.id, is_active=True).all()
        # Order by weekday then start time so it reads like a weekly schedule,
        # not DB insertion order.
        active_enr = sorted(
            (e for e in enr if e.dance_class and e.dance_class.is_active),
            key=lambda e: (e.dance_class.day_of_week, e.dance_class.start_time),
        )
        child_classes = [{
            'name': e.dance_class.name,
            'day_name': e.dance_class.day_name,
            'start_time': e.dance_class.start_time.strftime('%-I:%M %p'),
            'end_time': e.dance_class.end_time.strftime('%-I:%M %p'),
            'location': e.dance_class.location.name if e.dance_class.location else None,
        } for e in active_enr]
        child_data.append({
            'student': child,
            'balance': bal['balance'],
            'total_charges': bal['total_charges'],
            'total_payments': bal['total_payments'],
            'recent_attendance': recent_att,
            'classes': child_classes,
        })
        if child.family_id:
            g = family_groups.setdefault(child.family_id, {
                'family_id': child.family_id,
                'name': child.family.name if child.family else 'Family',
                'balance': 0.0,
                'members': [],
            })
            g['balance'] += bal['balance']
            g['members'].append(child.full_name)

    # Only offer combined pay when a family has 2+ of this parent's children and owes money
    families = [g for g in family_groups.values() if len(g['members']) > 1 and g['balance'] > 0]
    return render_template('parent/dashboard.html', children=child_data, families=families)


@bp.route('/transactions')
@admin_required
def transactions():
    students = Student.query.filter_by(is_active=True).order_by(Student.last_name, Student.first_name).all()
    # Billing config includes DRAFT-season classes on purpose: fall tuition
    # rules get set up while summer runs. The season gate in
    # _process_recurring_charges keeps them from billing until activation.
    from app.models import Season
    classes = live_class_query().order_by(DanceClass.name).all()
    classes += (DanceClass.query.join(Season, DanceClass.season_id == Season.id)
                .filter(DanceClass.is_active.is_(True), Season.status == 'draft')
                .order_by(DanceClass.name).all())
    return render_template('transactions/list.html', students=students, classes=classes)


@bp.route('/students/<int:student_id>/detail')
@staff_required
def student_detail(student_id):
    from app.models import ParentStudent, User
    student = Student.query.get_or_404(student_id)
    enrollments = ClassEnrollment.query.filter_by(student_id=student_id, is_active=True).all()
    classes = [e.dance_class for e in enrollments if e.dance_class]
    bal = calc_balance(student_id)
    recent_att = Attendance.query.filter_by(student_id=student_id).order_by(
        desc(Attendance.check_in_time)).limit(10).all()
    # Parent portal accounts linked to this student (admin card: invite + reset).
    portal_parents = (User.query
                      .join(ParentStudent, ParentStudent.parent_id == User.id)
                      .filter(ParentStudent.student_id == student_id)
                      .order_by(User.first_name).all())
    return render_template('students/detail.html', student=student, classes=classes,
        balance=bal['balance'], total_charges=bal['total_charges'],
        total_payments=bal['total_payments'], recent_attendance=recent_att,
        portal_parents=portal_parents)


@bp.route('/students/<int:student_id>/ledger')
@admin_required
def student_ledger(student_id):
    student = Student.query.get_or_404(student_id)
    return render_template('transactions/ledger.html', student=student)


@bp.route('/families')
@staff_required
def families_page():
    return render_template('families/list.html')


@bp.route('/families/<int:family_id>/ledger')
@admin_required
def family_ledger(family_id):
    family = Family.query.get_or_404(family_id)
    return render_template('families/ledger.html', family=family)


@bp.route('/messages')
@staff_required
def messages_page():
    students = Student.query.filter_by(is_active=True).order_by(Student.last_name).all()
    classes = live_class_query().order_by(DanceClass.name).all()
    return render_template('messages/list.html', students=students, classes=classes)


@bp.route('/rules')
@staff_required
def rules_admin():
    students = Student.query.filter_by(is_active=True).order_by(Student.last_name).all()
    return render_template('rules/admin.html', students=students)


@bp.route('/rules/acknowledge/<int:student_id>')
@login_required
def acknowledge_rules(student_id):
    student = Student.query.get_or_404(student_id)
    if not _parent_owns(student):  # a parent may only act on their own child
        return redirect(url_for('main.parent_dashboard'))
    return render_template('rules/acknowledge.html', student=student)


@bp.route('/take-attendance')
@staff_required
def take_attendance():
    today = date.today()
    current_weekday = today.weekday()
    todays_classes = live_class_query().filter_by(
        day_of_week=current_weekday
    ).order_by(DanceClass.start_time).all()
    all_classes = live_class_query().order_by(
        DanceClass.day_of_week, DanceClass.start_time
    ).all()
    return render_template('attendance/take_pick.html',
        today=today, todays_classes=todays_classes, all_classes=all_classes)


@bp.route('/take-attendance/<int:class_id>')
@staff_required
def take_attendance_class(class_id):
    """Card-based attendance for a class — prefetches all data in bulk."""
    dance_class = DanceClass.query.get_or_404(class_id)
    today = date.today()

    # Get 8 weeks of dates (current week + 7 prior)
    current_monday = today - timedelta(days=today.weekday())
    weeks = [(current_monday - timedelta(weeks=i)) for i in range(7, -1, -1)]
    earliest = weeks[0]
    latest = weeks[-1] + timedelta(days=6)

    # Bulk-load enrolled students
    enrollments = (
        ClassEnrollment.query
        .filter_by(class_id=class_id, is_active=True)
        .all()
    )
    student_ids = [e.student_id for e in enrollments]
    if not student_ids:
        return render_template('attendance/take.html',
            dance_class=dance_class, students=[], weeks=weeks,
            today=today, current_monday=current_monday, today_checked={})

    enrolled_students = Student.query.filter(Student.id.in_(student_ids)).all()
    student_map = {s.id: s for s in enrolled_students}

    # Prefetch ALL attendance for these students in this class across the 8-week window
    all_attendance = Attendance.query.filter(
        Attendance.student_id.in_(student_ids),
        Attendance.class_id == class_id,
        func.date(Attendance.check_in_time) >= earliest,
        func.date(Attendance.check_in_time) <= latest,
    ).all()

    # Build lookup: (student_id, week_start_iso) -> bool
    att_lookup = {}
    today_checked = {}
    for att in all_attendance:
        att_date = att.check_in_time.date()
        att_monday = att_date - timedelta(days=att_date.weekday())
        key = (att.student_id, att_monday.isoformat())
        att_lookup[key] = True
        if att_date == today:
            today_checked[att.student_id] = True

    students = []
    for sid in student_ids:
        s = student_map.get(sid)
        if not s:
            continue
        week_marks = {}
        for week_start in weeks:
            week_marks[week_start.isoformat()] = (sid, week_start.isoformat()) in att_lookup
        students.append({
            'id': s.id,
            'full_name': s.full_name,
            'first_name': s.first_name,
            'weeks': week_marks,
        })
    students.sort(key=lambda x: x['full_name'])

    # Fill in False for students not in today_checked
    for s in students:
        if s['id'] not in today_checked:
            today_checked[s['id']] = False

    return render_template('attendance/take.html',
        dance_class=dance_class, students=students,
        weeks=weeks, today=today, current_monday=current_monday,
        today_checked=today_checked)


@bp.route('/staff')
@admin_required
def staff_page():
    """Staff/teacher management page (admin only)."""
    return render_template('staff/list.html')


@bp.route('/locations')
@admin_required
def locations_page():
    return render_template('locations/list.html')


@bp.route('/settings')
@admin_required
def settings_page():
    return render_template('settings/payments.html')


@bp.route('/pending-payments')
@admin_required
def pending_payments_page():
    return render_template('payments/pending.html')


@bp.route('/company')
@admin_required
def company_page():
    students = Student.query.filter_by(is_active=True).order_by(Student.last_name, Student.first_name).all()
    return render_template('company/manage.html', students=students)


@bp.route('/recital')
@admin_required
def recital_page():
    students = Student.query.filter_by(is_active=True).order_by(Student.last_name, Student.first_name).all()
    classes = live_class_query().order_by(DanceClass.name).all()
    return render_template('recital/manage.html', students=students, classes=classes)


@bp.route('/recital-hub')
@staff_required
def recital_hub_page():
    """The per-year recital command center — show order, music/choreo, awards, booklet."""
    from app.models import PerformanceGroup
    students = Student.query.filter_by(is_active=True).order_by(Student.last_name, Student.first_name).all()
    classes = live_class_query().order_by(DanceClass.name).all()
    groups = PerformanceGroup.query.filter_by(is_active=True).order_by(PerformanceGroup.name).all()
    return render_template('recital/hub.html', students=students, classes=classes, groups=groups)


@bp.route('/recital/<int:recital_id>/booklet')
@staff_required
def recital_booklet(recital_id):
    """Print-ready recital booklet: cover, program, awards, ads, acknowledgments."""
    from app.models import Recital, RecitalNumber, RecitalAward, RecitalAd
    recital = Recital.query.get_or_404(recital_id)
    numbers = recital.numbers.order_by(RecitalNumber.order_index).all()
    program = []
    for n in numbers:
        cast = sorted(n.cast.all(), key=lambda c: (c.part or '￿', c.student.full_name))
        program.append({'n': n, 'cast': cast})
    awards = recital.awards.order_by(RecitalAward.order_index, RecitalAward.id).all()
    ads = recital.ads.order_by(RecitalAd.order_index, RecitalAd.id).all()
    return render_template('recital/booklet.html', recital=recital, program=program,
                           awards=awards, ads=ads)


@bp.route('/donations')
@admin_required
def donations_page():
    return render_template('donations/admin.html')


@bp.route('/register')
def public_register():
    """Public self-registration page (no login)."""
    return render_template('registration/public.html')


@bp.route('/registrations')
@admin_required
def registrations_page():
    return render_template('registration/admin.html')


@bp.route('/calendar')
@staff_required
def calendar_page():
    return render_template('calendar/view.html')


@bp.route('/skills')
@admin_required
def skills_page():
    students = Student.query.filter_by(is_active=True).order_by(Student.last_name, Student.first_name).all()
    classes = live_class_query().order_by(DanceClass.name).all()
    return render_template('skills/manage.html', students=students, classes=classes)


@bp.route('/students/<int:student_id>/certificate')
@login_required
def student_certificate(student_id):
    student = Student.query.get_or_404(student_id)
    if current_user.is_parent and student.id not in {s.id for s in current_user.get_children()}:
        return redirect(url_for('main.parent_dashboard'))
    from app.models import Skill, StudentSkill
    achieved_ids = {a.skill_id for a in StudentSkill.query.filter_by(student_id=student_id).all()}
    skills = Skill.query.filter(Skill.id.in_(achieved_ids)).order_by(Skill.category, Skill.display_order).all() if achieved_ids else []
    return render_template('skills/certificate.html', student=student, skills=skills)


@bp.route('/makeups')
@admin_required
def makeups_page():
    students = Student.query.filter_by(is_active=True).order_by(Student.last_name, Student.first_name).all()
    classes = live_class_query().order_by(DanceClass.name).all()
    return render_template('makeups/manage.html', students=students, classes=classes)


@bp.route('/leads')
@admin_required
def leads_page():
    return render_template('leads/manage.html')


@bp.route('/timeclock')
@staff_required
def timeclock_page():
    return render_template('timeclock/view.html')


@bp.route('/analytics')
@admin_required
def analytics_page():
    return render_template('analytics/dashboard.html')


@bp.route('/reports/aging')
@admin_required
def aging_report_page():
    return render_template('reports/aging.html')


@bp.route('/reports/revenue')
@admin_required
def revenue_report_page():
    return render_template('reports/revenue.html')


def _parent_owns(student):
    return (not current_user.is_parent) or (student.id in {s.id for s in current_user.get_children()})


def _year_arg():
    from datetime import date as _date
    try:
        return int(request.args.get('year', _date.today().year))
    except (TypeError, ValueError):
        return _date.today().year


def _statement_rows(student_ids, year):
    """Return (prior_balance, rows, total_charges, total_payments) for the year."""
    from datetime import date as _date
    start = _date(year, 1, 1)
    end = _date(year, 12, 31)
    prior = Transaction.query.filter(Transaction.student_id.in_(student_ids),
                                     Transaction.transaction_date < start).all()
    prior_balance = sum(float(t.amount) if t.type == 'charge' else -float(t.amount) for t in prior)
    txns = (Transaction.query
            .filter(Transaction.student_id.in_(student_ids),
                    Transaction.transaction_date >= start, Transaction.transaction_date <= end)
            .order_by(Transaction.transaction_date, Transaction.created_at).all())
    running = prior_balance
    rows, tc, tp = [], 0.0, 0.0
    for t in txns:
        amt = float(t.amount)
        if t.type == 'charge':
            running += amt
            tc += amt
        else:
            running -= amt
            tp += amt
        rows.append({'t': t, 'running': running})
    return prior_balance, rows, tc, tp


@bp.route('/students/<int:student_id>/statement')
@login_required
def student_statement(student_id):
    student = Student.query.get_or_404(student_id)
    if not _parent_owns(student):
        return redirect(url_for('main.parent_dashboard'))
    year = _year_arg()
    prior, rows, tc, tp = _statement_rows([student.id], year)
    return render_template('statements/student.html', student=student, year=year,
                           prior_balance=prior, rows=rows, total_charges=tc, total_payments=tp,
                           ending_balance=prior + tc - tp)


@bp.route('/families/<int:family_id>/statement')
@staff_required
def family_statement(family_id):
    family = Family.query.get_or_404(family_id)
    students = family.students.all()
    year = _year_arg()
    ids = [s.id for s in students] or [-1]
    prior, rows, tc, tp = _statement_rows(ids, year)
    return render_template('statements/family.html', family=family, students=students, year=year,
                           prior_balance=prior, rows=rows, total_charges=tc, total_payments=tp,
                           ending_balance=prior + tc - tp)


@bp.route('/giving-statement')
@login_required
def giving_statement():
    from datetime import date as _date
    year = _year_arg()
    email = (request.args.get('email') or '').strip()
    if current_user.is_parent:
        email = current_user.email  # parents only see their own
    if not email:
        return redirect(url_for('main.dashboard'))
    start, end = _date(year, 1, 1), _date(year, 12, 31)
    donations = (Donation.query
                 .filter(Donation.donor_email == email, Donation.status == 'recorded',
                         Donation.donation_date >= start, Donation.donation_date <= end)
                 .order_by(Donation.donation_date).all())
    total = sum(float(d.amount) for d in donations)
    return render_template('statements/giving.html', email=email, year=year, donations=donations,
                           total=total, org_name=Setting.get('donations_org_name', '') or 'LSODance Foundation',
                           ein=Setting.get('donations_ein', ''))


@bp.route('/waivers')
@admin_required
def waivers_page():
    return render_template('waivers/admin.html')


@bp.route('/students/<int:student_id>/sign-waivers')
@login_required
def sign_waivers_page(student_id):
    student = Student.query.get_or_404(student_id)
    if not _parent_owns(student):  # a parent may only act on their own child
        return redirect(url_for('main.parent_dashboard'))
    return render_template('waivers/sign.html', student=student)
