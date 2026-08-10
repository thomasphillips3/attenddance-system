"""
Database models for AttenDANCE system
"""

from datetime import datetime, date
from flask_login import UserMixin
from sqlalchemy.orm import deferred
from werkzeug.security import generate_password_hash, check_password_hash
from app import db

class User(UserMixin, db.Model):
    """Teacher/Admin user model"""
    __tablename__ = 'users'
    
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    email = db.Column(db.String(120), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(200), nullable=False)
    
    # Profile information
    first_name = db.Column(db.String(50), nullable=False)
    last_name = db.Column(db.String(50), nullable=False)
    phone = db.Column(db.String(20))
    
    # Permissions
    role = db.Column(db.String(20), default='teacher', nullable=False)  # admin, teacher, parent
    is_admin = db.Column(db.Boolean, default=False, nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    invite_code = db.Column(db.String(20), unique=True)
    
    # Timestamps
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    last_login = db.Column(db.DateTime)
    
    def set_password(self, password):
        """Set user password hash"""
        self.password_hash = generate_password_hash(password)
    
    def check_password(self, password):
        """Check user password"""
        return check_password_hash(self.password_hash, password)
    
    @property
    def full_name(self):
        """Get user's full name"""
        return f"{self.first_name} {self.last_name}"
    
    @property
    def is_parent(self):
        # An admin is never treated as a parent, so a stale role can't route an
        # admin into the read-only parent portal (defends the is_admin/role split
        # that let the seeded admin's role drift to 'teacher').
        return self.role == 'parent' and not self.is_admin

    @property
    def is_staff(self):
        # An admin is always staff regardless of role — the is_admin bool and the
        # role string can't diverge into an "admin locked out of staff pages" state.
        return self.is_admin or self.role in ('admin', 'teacher')

    def get_children(self):
        """Get students linked to this parent user."""
        if not self.is_parent:
            return []
        return (
            Student.query
            .join(ParentStudent, ParentStudent.student_id == Student.id)
            .filter(ParentStudent.parent_id == self.id)
            .all()
        )

    def __repr__(self):
        return f'<User {self.username}>'

class ParentStudent(db.Model):
    """Links parent users to their children (students)"""
    __tablename__ = 'parent_students'

    id = db.Column(db.Integer, primary_key=True)
    parent_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    student_id = db.Column(db.Integer, db.ForeignKey('students.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        db.UniqueConstraint('parent_id', 'student_id', name='unique_parent_student'),
    )

    parent = db.relationship('User', backref='parent_links')
    student = db.relationship('Student', backref='parent_links')

class Family(db.Model):
    """Family account — groups siblings, billing is per-family"""
    __tablename__ = 'families'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    primary_email = db.Column(db.String(120))
    primary_phone = db.Column(db.String(20))
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    students = db.relationship('Student', backref='family', lazy='dynamic')
    def __repr__(self):
        return f'<Family {self.name}>'


class Season(db.Model):
    """A session/term the studio runs classes in (e.g. "Fall 2026").

    Lifecycle: draft → active → archived. Exactly one season is active at a
    time; a draft season is a full staging area — its classes can be created,
    enrolled into, and given recurring charges, but none of that surfaces on
    live views or bills anyone until the season is activated. Activation
    archives the previously active season (cancelling its classes wholesale,
    same cascade as Cancel Class). Classes with season_id NULL predate this
    feature and are treated as belonging to the active season."""
    __tablename__ = 'seasons'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    start_date = db.Column(db.Date)
    end_date = db.Column(db.Date)
    status = db.Column(db.String(10), default='draft', nullable=False)  # draft/active/archived
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    classes = db.relationship('DanceClass', backref='season', lazy='dynamic')

    def __repr__(self):
        return f'<Season {self.name} ({self.status})>'


class Location(db.Model):
    """Studio location where classes are held"""
    __tablename__ = 'locations'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    address = db.Column(db.String(200))
    city = db.Column(db.String(100))
    state = db.Column(db.String(2))
    zip_code = db.Column(db.String(10))
    phone = db.Column(db.String(20))
    notes = db.Column(db.Text)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    classes = db.relationship('DanceClass', backref='location', lazy='dynamic')

    @property
    def full_address(self):
        parts = [self.address, self.city, self.state, self.zip_code]
        return ', '.join(p for p in parts if p)

    def __repr__(self):
        return f'<Location {self.name}>'


class Student(db.Model):
    """Student model"""
    __tablename__ = 'students'

    id = db.Column(db.Integer, primary_key=True)
    family_id = db.Column(db.Integer, db.ForeignKey('families.id'))

    # Basic information
    first_name = db.Column(db.String(50), nullable=False)
    last_name = db.Column(db.String(50), nullable=False)
    email = db.Column(db.String(120), unique=True, index=True)
    phone = db.Column(db.String(20))
    date_of_birth = db.Column(db.Date)
    
    # Contact information
    emergency_contact_name = db.Column(db.String(100))
    emergency_contact_phone = db.Column(db.String(20))
    parent_email = db.Column(db.String(120))
    parent_phone = db.Column(db.String(20))
    
    # RFID information
    rfid_uid = db.Column(db.String(50), unique=True, index=True)
    rfid_assigned_at = db.Column(db.DateTime)
    rfid_assigned_by = db.Column(db.Integer, db.ForeignKey('users.id'))
    
    # Status
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    enrollment_date = db.Column(db.Date, default=date.today, nullable=False)
    
    # School information
    school = db.Column(db.String(150))
    grade = db.Column(db.String(30))

    # Health / special needs
    allergies = db.Column(db.Text)
    special_needs = db.Column(db.Text)

    # Measurements (for costumes — matches Jackrabbit Sizes tab)
    height = db.Column(db.String(20))
    weight = db.Column(db.String(20))
    shoe_size = db.Column(db.String(20))
    shirt_size = db.Column(db.String(20))
    pants_size = db.Column(db.String(20))
    leotard_size = db.Column(db.String(20))
    dress_size = db.Column(db.String(20))
    waist = db.Column(db.String(20))
    girth = db.Column(db.String(20))
    inseam = db.Column(db.String(20))
    neck = db.Column(db.String(20))
    tight_size = db.Column(db.String(20))
    bust = db.Column(db.String(20))
    hips = db.Column(db.String(20))
    sleeve = db.Column(db.String(20))
    chest = db.Column(db.String(20))
    size_notes = db.Column(db.Text)

    # Notes
    notes = db.Column(db.Text)
    medical_notes = db.Column(db.Text)
    
    # Timestamps
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    assigned_by_user = db.relationship('User', backref='assigned_students')
    attendances = db.relationship('Attendance', backref='student', lazy='dynamic')
    class_enrollments = db.relationship('ClassEnrollment', backref='student', lazy='dynamic')
    transactions = db.relationship('Transaction', backref='student', lazy='dynamic')
    
    @property
    def full_name(self):
        """Get student's full name"""
        return f"{self.first_name} {self.last_name}"
    
    @property
    def age(self):
        """Calculate student's age"""
        if self.date_of_birth:
            today = date.today()
            return today.year - self.date_of_birth.year - (
                (today.month, today.day) < (self.date_of_birth.month, self.date_of_birth.day)
            )
        return None
    
    def has_rfid(self):
        """Check if student has RFID assigned"""
        return self.rfid_uid is not None
    
    def get_recent_attendance(self, days=30):
        """Get recent attendance records"""
        from datetime import timedelta
        # Local (datetime.now) to match check_in_time, which is stored studio-local.
        cutoff_date = datetime.now() - timedelta(days=days)
        return self.attendances.filter(Attendance.check_in_time >= cutoff_date).all()
    
    def __repr__(self):
        return f'<Student {self.full_name}>'

class DanceClass(db.Model):
    """Dance class model"""
    __tablename__ = 'classes'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text)
    location_id = db.Column(db.Integer, db.ForeignKey('locations.id'))
    season_id = db.Column(db.Integer, db.ForeignKey('seasons.id'))  # NULL = pre-seasons row, treated as active season

    # Schedule
    day_of_week = db.Column(db.Integer, nullable=False)  # 0=Monday, 6=Sunday
    start_time = db.Column(db.Time, nullable=False)
    end_time = db.Column(db.Time, nullable=False)
    
    # Instructor
    instructor_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    
    # Class details
    max_students = db.Column(db.Integer, default=20)
    level = db.Column(db.String(50))  # Beginner, Intermediate, Advanced
    age_group = db.Column(db.String(50))  # Kids, Teens, Adults
    
    # Status
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    
    # Timestamps
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    instructor = db.relationship('User', backref='taught_classes')
    enrollments = db.relationship('ClassEnrollment', backref='dance_class', lazy='dynamic')
    attendances = db.relationship('Attendance', backref='dance_class', lazy='dynamic')
    
    @property
    def enrolled_students_count(self):
        """Get count of enrolled students"""
        return self.enrollments.filter_by(is_active=True).count()
    
    @property
    def day_name(self):
        """Get day name"""
        days = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
        return days[self.day_of_week]
    
    def get_enrolled_students(self):
        """Get list of enrolled students"""
        return [enrollment.student for enrollment in 
                self.enrollments.filter_by(is_active=True).all()]
    
    def get_todays_attendance(self):
        """Get today's attendance for this class"""
        today = date.today()
        return self.attendances.filter(
            db.func.date(Attendance.check_in_time) == today
        ).all()
    
    def __repr__(self):
        return f'<DanceClass {self.name}>'

class ClassEnrollment(db.Model):
    """Student enrollment in classes"""
    __tablename__ = 'class_enrollments'
    
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('students.id'), nullable=False)
    class_id = db.Column(db.Integer, db.ForeignKey('classes.id'), nullable=False)
    
    # Enrollment details
    enrolled_date = db.Column(db.Date, default=date.today, nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    
    # Payment/billing (for future use)
    monthly_fee = db.Column(db.Numeric(10, 2))
    
    # Timestamps
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Unique constraint
    __table_args__ = (
        db.UniqueConstraint('student_id', 'class_id', name='unique_student_class'),
    )
    
    def __repr__(self):
        return f'<Enrollment {self.student.full_name} in {self.dance_class.name}>'

class Attendance(db.Model):
    """Attendance record model"""
    __tablename__ = 'attendance'
    
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('students.id'), nullable=False)
    class_id = db.Column(db.Integer, db.ForeignKey('classes.id'), nullable=False)
    
    # Attendance details. check_in_time is studio-local (datetime.now, not utcnow):
    # it's grouped/filtered by local date.today() and the unique-day index, so a
    # UTC value would land an evening check-in on the next day. All creation paths
    # set it explicitly; this default is the safety net for any future one.
    check_in_time = db.Column(db.DateTime, default=datetime.now, nullable=False)
    check_out_time = db.Column(db.DateTime)
    
    # How they checked in
    check_in_method = db.Column(db.String(20), default='rfid')  # rfid, manual, mobile
    
    # Optional notes
    notes = db.Column(db.Text)
    
    # Status
    is_present = db.Column(db.Boolean, default=True, nullable=False)
    
    # Lookup index for the per-(student, class, day) dedup query. NOTE: this is a
    # plain index, NOT a uniqueness guarantee — duplicate check-ins are prevented
    # at the APPLICATION level (manual_checkin returns 400 if already present;
    # toggle_attendance deletes-or-creates). Keep those checks; the DB won't.
    __table_args__ = (
        db.Index('idx_attendance_date_student', 'student_id',
                db.func.date('check_in_time'), 'class_id'),
    )
    
    @property
    def attendance_date(self):
        """Get attendance date"""
        return self.check_in_time.date()
    
    @property
    def duration(self):
        """Get attendance duration if checked out"""
        if self.check_out_time:
            return self.check_out_time - self.check_in_time
        return None
    
    def __repr__(self):
        return f'<Attendance {self.student.full_name} on {self.attendance_date}>'

class RFIDLog(db.Model):
    """RFID scan log for debugging and security"""
    __tablename__ = 'rfid_logs'
    
    id = db.Column(db.Integer, primary_key=True)
    rfid_uid = db.Column(db.String(50), nullable=False, index=True)
    student_id = db.Column(db.Integer, db.ForeignKey('students.id'))
    
    # Scan details. Studio-local (datetime.now) to match the check-in it records
    # and the dashboard's server-side strftime display of the scan time.
    scan_time = db.Column(db.DateTime, default=datetime.now, nullable=False)
    action_taken = db.Column(db.String(50))  # checkin, unknown_card, error
    
    # Results
    success = db.Column(db.Boolean, default=False, nullable=False)
    error_message = db.Column(db.String(200))
    
    # Relationships
    student = db.relationship('Student', backref='rfid_logs')
    
    def __repr__(self):
        return f'<RFIDLog {self.rfid_uid} at {self.scan_time}>'

class Transaction(db.Model):
    """Payment / transaction record"""
    __tablename__ = 'transactions'

    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('students.id'), nullable=False)

    type = db.Column(db.String(10), nullable=False, default='payment')  # charge or payment
    amount = db.Column(db.Numeric(10, 2), nullable=False)
    category = db.Column(db.String(50), nullable=False)  # tuition, costumes, shoes, other
    payment_method = db.Column(db.String(50))  # cash, zelle, venmo, cashapp, card, tap (null for charges)
    description = db.Column(db.Text)
    transaction_date = db.Column(db.Date, default=date.today, nullable=False)
    recurring_charge_id = db.Column(db.Integer, db.ForeignKey('recurring_charges.id'))

    created_by = db.Column(db.Integer, db.ForeignKey('users.id'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    creator = db.relationship('User', backref='created_transactions')

    recurring_charge = db.relationship('RecurringCharge', backref='transactions')

    def __repr__(self):
        return f'<Transaction ${self.amount} {self.category} for student {self.student_id}>'

class RecurringCharge(db.Model):
    """Automatic monthly charge rule"""
    __tablename__ = 'recurring_charges'

    id = db.Column(db.Integer, primary_key=True)
    class_id = db.Column(db.Integer, db.ForeignKey('classes.id'), nullable=False)
    amount = db.Column(db.Numeric(10, 2), nullable=False)
    category = db.Column(db.String(50), nullable=False, default='tuition')
    description = db.Column(db.Text)
    day_of_month = db.Column(db.Integer, nullable=False, default=1)  # 1-28
    is_active = db.Column(db.Boolean, default=True, nullable=False)

    created_by = db.Column(db.Integer, db.ForeignKey('users.id'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    dance_class = db.relationship('DanceClass', backref='recurring_charges')
    creator = db.relationship('User', backref='created_recurring_charges')

    def __repr__(self):
        return f'<RecurringCharge ${self.amount} {self.category} for class {self.class_id}>'

class Rule(db.Model):
    """Studio rule that parents must acknowledge"""
    __tablename__ = 'rules'

    id = db.Column(db.Integer, primary_key=True)
    text = db.Column(db.Text, nullable=False)
    display_order = db.Column(db.Integer, nullable=False, default=0)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    acknowledgments = db.relationship('RuleAcknowledgment', backref='rule', lazy='dynamic')

    def __repr__(self):
        return f'<Rule #{self.display_order}>'

class RuleAcknowledgment(db.Model):
    """Record of a parent initialing a specific rule for a student"""
    __tablename__ = 'rule_acknowledgments'

    id = db.Column(db.Integer, primary_key=True)
    rule_id = db.Column(db.Integer, db.ForeignKey('rules.id'), nullable=False)
    student_id = db.Column(db.Integer, db.ForeignKey('students.id'), nullable=False)
    parent_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    initials = db.Column(db.String(10), nullable=False)
    acknowledged_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        db.UniqueConstraint('rule_id', 'student_id', 'parent_id', name='unique_rule_ack'),
    )

    student = db.relationship('Student', backref='rule_acknowledgments')
    parent = db.relationship('User', backref='rule_acknowledgments')

    def __repr__(self):
        return f'<RuleAck rule={self.rule_id} student={self.student_id}>'

class Message(db.Model):
    """Email blast record"""
    __tablename__ = 'messages'

    id = db.Column(db.Integer, primary_key=True)
    subject = db.Column(db.String(200), nullable=False)
    body = db.Column(db.Text, nullable=False)
    recipient_type = db.Column(db.String(20), nullable=False)  # all, class, individual
    recipient_filter = db.Column(db.String(100))  # class_id or student_id
    recipient_count = db.Column(db.Integer, default=0)
    recipient_emails = db.Column(db.Text)  # comma-separated
    sent = db.Column(db.Boolean, default=False, nullable=False)
    sent_at = db.Column(db.DateTime)
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    creator = db.relationship('User', backref='sent_messages')

    def __repr__(self):
        return f'<Message "{self.subject}" to {self.recipient_count}>'


class MessageAttachment(db.Model):
    """A file attached to an email blast (flyer, permission slip, schedule PDF).

    Bytes live in the DB rather than on disk because Fly's filesystem is
    ephemeral and only the SQLite volume survives a restart - the same reason the
    Zelle QR is stored in Setting instead of uploads/.

    `data` is deferred so listing message history never drags megabytes of blob
    into memory on a 256MB machine; it loads only when something touches
    `.data` (the send worker and the download endpoint)."""
    __tablename__ = 'message_attachments'

    id = db.Column(db.Integer, primary_key=True)
    message_id = db.Column(db.Integer, db.ForeignKey('messages.id'),
                           nullable=False, index=True)
    filename = db.Column(db.String(255), nullable=False)
    content_type = db.Column(db.String(100), nullable=False)
    size = db.Column(db.Integer, default=0, nullable=False)
    data = deferred(db.Column(db.LargeBinary, nullable=False))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    message = db.relationship(
        'Message',
        backref=db.backref('attachments', lazy='dynamic',
                           cascade='all, delete-orphan'))

    def __repr__(self):
        return f'<MessageAttachment {self.filename} ({self.size}b)>'


class Setting(db.Model):
    """Key-value settings store for studio configuration"""
    __tablename__ = 'settings'

    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(100), unique=True, nullable=False, index=True)
    value = db.Column(db.Text)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    @staticmethod
    def get(key: str, default: str = '') -> str:
        row = Setting.query.filter_by(key=key).first()
        return row.value if row and row.value else default

    @staticmethod
    def get_bool(key: str, default: bool = False) -> bool:
        row = Setting.query.filter_by(key=key).first()
        if not row or row.value is None or row.value == '':
            return default
        return row.value == '1'

    @staticmethod
    def set(key: str, value: str):
        from app import db as _db
        row = Setting.query.filter_by(key=key).first()
        if row:
            row.value = value
        else:
            _db.session.add(Setting(key=key, value=value))
        _db.session.commit()

    def __repr__(self):
        return f'<Setting {self.key}>'


class PendingPayment(db.Model):
    """A payment a parent claims to have sent externally (Zelle/Cash App/etc),
    awaiting admin confirmation before it becomes a real Transaction."""
    __tablename__ = 'pending_payments'

    id = db.Column(db.Integer, primary_key=True)
    # Either a single student OR a whole family (one of these is set)
    student_id = db.Column(db.Integer, db.ForeignKey('students.id'))
    family_id = db.Column(db.Integer, db.ForeignKey('families.id'))
    parent_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)

    amount = db.Column(db.Numeric(10, 2), nullable=False)
    method = db.Column(db.String(20), nullable=False)  # zelle, cashapp, square, cash, other
    reference = db.Column(db.String(120))  # confirmation # / memo the parent entered
    note = db.Column(db.Text)  # optional parent note

    status = db.Column(db.String(20), default='pending', nullable=False)  # pending, confirmed, rejected
    admin_note = db.Column(db.Text)  # reason on reject / note on confirm
    transaction_id = db.Column(db.Integer, db.ForeignKey('transactions.id'))  # link once confirmed

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    reviewed_at = db.Column(db.DateTime)
    reviewed_by = db.Column(db.Integer, db.ForeignKey('users.id'))

    student = db.relationship('Student', backref='pending_payments')
    family = db.relationship('Family', backref='pending_payments')
    parent = db.relationship('User', foreign_keys=[parent_id], backref='claimed_payments')
    reviewer = db.relationship('User', foreign_keys=[reviewed_by])
    transaction = db.relationship('Transaction', backref='pending_payment')

    def __repr__(self):
        return f'<PendingPayment ${self.amount} {self.method} {self.status}>'


class SquareInvoice(db.Model):
    """Tracks Square invoices we've sent so the webhook can auto-record payment."""
    __tablename__ = 'square_invoices'

    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('students.id'), nullable=False)
    invoice_id = db.Column(db.String(120), unique=True, index=True, nullable=False)
    amount_cents = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(30), default='SENT', nullable=False)
    public_url = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    paid_at = db.Column(db.DateTime)

    student = db.relationship('Student', backref='square_invoices')

    def __repr__(self):
        return f'<SquareInvoice {self.invoice_id} {self.status}>'


class AuditLog(db.Model):
    """Audit trail for sensitive admin actions (settings changes, payment review)."""
    __tablename__ = 'audit_logs'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    action = db.Column(db.String(60), nullable=False)  # e.g. settings.update, payment.confirm
    detail = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)

    user = db.relationship('User', backref='audit_logs')

    @staticmethod
    def record(user_id, action: str, detail: str = ''):
        """Add an audit entry. Does NOT commit — caller commits with their txn."""
        from app import db as _db
        _db.session.add(AuditLog(user_id=user_id, action=action, detail=detail))

    def __repr__(self):
        return f'<AuditLog {self.action} by {self.user_id}>'


# ── Performance Company management ──────────────────────────────────

class PerformanceGroup(db.Model):
    """A performance/competition company or team (e.g. 'LSODance Company')."""
    __tablename__ = 'performance_groups'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    description = db.Column(db.Text)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    memberships = db.relationship('CompanyMembership', backref='group', lazy='dynamic')
    auditions = db.relationship('Audition', backref='group', lazy='dynamic')
    performances = db.relationship('Performance', backref='group', lazy='dynamic')

    def __repr__(self):
        return f'<PerformanceGroup {self.name}>'


class CompanyMembership(db.Model):
    """A student's membership in a performance group."""
    __tablename__ = 'company_memberships'

    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey('performance_groups.id'), nullable=False)
    student_id = db.Column(db.Integer, db.ForeignKey('students.id'), nullable=False)
    role = db.Column(db.String(40), default='Member', nullable=False)  # Member, Captain, etc.
    joined_date = db.Column(db.Date, default=date.today, nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    student = db.relationship('Student', backref='company_memberships')

    __table_args__ = (
        db.UniqueConstraint('group_id', 'student_id', name='unique_group_member'),
    )

    def __repr__(self):
        return f'<CompanyMembership s={self.student_id} g={self.group_id}>'


class Audition(db.Model):
    """An audition event for a performance group."""
    __tablename__ = 'auditions'

    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey('performance_groups.id'))
    title = db.Column(db.String(150), nullable=False)
    audition_date = db.Column(db.Date)
    location_text = db.Column(db.String(200))
    description = db.Column(db.Text)
    is_open = db.Column(db.Boolean, default=True, nullable=False)  # accepting signups
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    signups = db.relationship('AuditionSignup', backref='audition', lazy='dynamic')

    def __repr__(self):
        return f'<Audition {self.title}>'


class AuditionSignup(db.Model):
    """A student's signup for an audition."""
    __tablename__ = 'audition_signups'

    id = db.Column(db.Integer, primary_key=True)
    audition_id = db.Column(db.Integer, db.ForeignKey('auditions.id'), nullable=False)
    student_id = db.Column(db.Integer, db.ForeignKey('students.id'), nullable=False)
    parent_id = db.Column(db.Integer, db.ForeignKey('users.id'))  # who signed up (null if admin)
    status = db.Column(db.String(20), default='signed_up', nullable=False)  # signed_up, accepted, declined, waitlist
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    student = db.relationship('Student', backref='audition_signups')
    parent = db.relationship('User')

    __table_args__ = (
        db.UniqueConstraint('audition_id', 'student_id', name='unique_audition_signup'),
    )

    def __repr__(self):
        return f'<AuditionSignup s={self.student_id} a={self.audition_id} {self.status}>'


class Performance(db.Model):
    """A scheduled performance/show. group_id null = studio-wide event."""
    __tablename__ = 'performances'

    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey('performance_groups.id'))
    recital_id = db.Column(db.Integer, db.ForeignKey('recitals.id'))  # link a show date to a Recital hub
    title = db.Column(db.String(150), nullable=False)
    performance_date = db.Column(db.Date)
    call_time = db.Column(db.String(40))  # free text, e.g. "5:30 PM call, 7 PM show"
    venue = db.Column(db.String(200))
    description = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    assignments = db.relationship('PerformanceAssignment', backref='performance', lazy='dynamic')

    def __repr__(self):
        return f'<Performance {self.title}>'


class PerformanceAssignment(db.Model):
    """A student assigned to perform in a Performance."""
    __tablename__ = 'performance_assignments'

    id = db.Column(db.Integer, primary_key=True)
    performance_id = db.Column(db.Integer, db.ForeignKey('performances.id'), nullable=False)
    student_id = db.Column(db.Integer, db.ForeignKey('students.id'), nullable=False)
    notes = db.Column(db.String(200))  # e.g. role, number, costume note
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    student = db.relationship('Student', backref='performance_assignments')

    __table_args__ = (
        db.UniqueConstraint('performance_id', 'student_id', name='unique_performance_assignment'),
    )

    def __repr__(self):
        return f'<PerformanceAssignment s={self.student_id} p={self.performance_id}>'


# ── Waivers & forms ─────────────────────────────────────────────────

class WaiverTemplate(db.Model):
    """A form parents must sign per student (liability, photo release, medical auth)."""
    __tablename__ = 'waiver_templates'

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(150), nullable=False)
    body = db.Column(db.Text, nullable=False)
    # If True, a parent may sign while declining consent (e.g. opt OUT of photo release)
    allow_decline = db.Column(db.Boolean, default=False, nullable=False)
    display_order = db.Column(db.Integer, default=0, nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    signatures = db.relationship('WaiverSignature', backref='template', lazy='dynamic')

    def __repr__(self):
        return f'<WaiverTemplate {self.title}>'


class WaiverSignature(db.Model):
    """A parent's signature of a waiver for a specific student."""
    __tablename__ = 'waiver_signatures'

    id = db.Column(db.Integer, primary_key=True)
    template_id = db.Column(db.Integer, db.ForeignKey('waiver_templates.id'), nullable=False)
    student_id = db.Column(db.Integer, db.ForeignKey('students.id'), nullable=False)
    parent_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    signed_name = db.Column(db.String(120), nullable=False)  # typed signature
    consent = db.Column(db.Boolean, default=True, nullable=False)  # False = declined (opt-out forms)
    signed_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    student = db.relationship('Student', backref='waiver_signatures')
    parent = db.relationship('User', backref='waiver_signatures')

    __table_args__ = (
        db.UniqueConstraint('template_id', 'student_id', name='unique_waiver_signature'),
    )

    def __repr__(self):
        return f'<WaiverSignature t={self.template_id} s={self.student_id} consent={self.consent}>'


# ── Recital & costumes ──────────────────────────────────────────────

class Costume(db.Model):
    """A recital costume tied to a class or company, with a fee and sizing."""
    __tablename__ = 'costumes'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    class_id = db.Column(db.Integer, db.ForeignKey('classes.id'))
    group_id = db.Column(db.Integer, db.ForeignKey('performance_groups.id'))
    vendor = db.Column(db.String(120))
    fee = db.Column(db.Numeric(10, 2), default=0, nullable=False)
    notes = db.Column(db.Text)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    dance_class = db.relationship('DanceClass')
    group = db.relationship('PerformanceGroup')
    assignments = db.relationship('CostumeAssignment', backref='costume', lazy='dynamic')

    def __repr__(self):
        return f'<Costume {self.name}>'


class CostumeAssignment(db.Model):
    """A costume assigned to a student, with size and payment tracking."""
    __tablename__ = 'costume_assignments'

    id = db.Column(db.Integer, primary_key=True)
    costume_id = db.Column(db.Integer, db.ForeignKey('costumes.id'), nullable=False)
    student_id = db.Column(db.Integer, db.ForeignKey('students.id'), nullable=False)
    size = db.Column(db.String(40))
    notes = db.Column(db.String(200))
    charged = db.Column(db.Boolean, default=False, nullable=False)  # fee posted to ledger
    transaction_id = db.Column(db.Integer, db.ForeignKey('transactions.id'))
    paid = db.Column(db.Boolean, default=False, nullable=False)  # studio-tracked paid flag
    paid_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    student = db.relationship('Student', backref='costume_assignments')
    transaction = db.relationship('Transaction')

    __table_args__ = (
        db.UniqueConstraint('costume_id', 'student_id', name='unique_costume_assignment'),
    )

    def __repr__(self):
        return f'<CostumeAssignment c={self.costume_id} s={self.student_id}>'


class TicketType(db.Model):
    """A ticket tier for a performance (e.g. Adult $15, Child $10)."""
    __tablename__ = 'ticket_types'

    id = db.Column(db.Integer, primary_key=True)
    performance_id = db.Column(db.Integer, db.ForeignKey('performances.id'), nullable=False)
    name = db.Column(db.String(80), nullable=False)
    price = db.Column(db.Numeric(10, 2), default=0, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    performance = db.relationship('Performance', backref='ticket_types')
    orders = db.relationship('TicketOrder', backref='ticket_type', lazy='dynamic')

    def __repr__(self):
        return f'<TicketType {self.name} ${self.price}>'


class TicketOrder(db.Model):
    """An order/sale of tickets for a performance."""
    __tablename__ = 'ticket_orders'

    id = db.Column(db.Integer, primary_key=True)
    ticket_type_id = db.Column(db.Integer, db.ForeignKey('ticket_types.id'), nullable=False)
    parent_id = db.Column(db.Integer, db.ForeignKey('users.id'))  # who ordered (null if walk-up)
    student_id = db.Column(db.Integer, db.ForeignKey('students.id'))  # associated dancer (optional)
    quantity = db.Column(db.Integer, default=1, nullable=False)
    amount = db.Column(db.Numeric(10, 2), default=0, nullable=False)  # snapshot of qty * price
    paid = db.Column(db.Boolean, default=False, nullable=False)
    paid_at = db.Column(db.DateTime)
    note = db.Column(db.String(200))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    parent = db.relationship('User')
    student = db.relationship('Student')

    def __repr__(self):
        return f'<TicketOrder type={self.ticket_type_id} x{self.quantity}>'


# ── Payment plans (hardship installments) ───────────────────────────

class PaymentPlan(db.Model):
    """An agreement to pay a balance in scheduled installments."""
    __tablename__ = 'payment_plans'

    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('students.id'), nullable=False)
    installment_amount = db.Column(db.Numeric(10, 2), nullable=False)
    num_installments = db.Column(db.Integer, nullable=False)
    day_of_month = db.Column(db.Integer, default=1, nullable=False)
    note = db.Column(db.Text)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    student = db.relationship('Student', backref='payment_plans')
    installments = db.relationship('PaymentPlanInstallment', backref='plan', lazy='dynamic',
                                   cascade='all, delete-orphan')

    def __repr__(self):
        return f'<PaymentPlan s={self.student_id} ${self.installment_amount}x{self.num_installments}>'


class PaymentPlanInstallment(db.Model):
    """One scheduled installment of a payment plan."""
    __tablename__ = 'payment_plan_installments'

    id = db.Column(db.Integer, primary_key=True)
    plan_id = db.Column(db.Integer, db.ForeignKey('payment_plans.id'), nullable=False)
    seq = db.Column(db.Integer, nullable=False)
    due_date = db.Column(db.Date, nullable=False)
    amount = db.Column(db.Numeric(10, 2), nullable=False)
    paid = db.Column(db.Boolean, default=False, nullable=False)
    paid_at = db.Column(db.DateTime)

    def __repr__(self):
        return f'<Installment plan={self.plan_id} #{self.seq} due={self.due_date}>'


# ── Donations (501c3 Foundation) ────────────────────────────────────

class Donation(db.Model):
    """A donation to the studio's foundation, for giving statements."""
    __tablename__ = 'donations'

    id = db.Column(db.Integer, primary_key=True)
    donor_name = db.Column(db.String(120))
    donor_email = db.Column(db.String(120), index=True)
    amount = db.Column(db.Numeric(10, 2), nullable=False)
    method = db.Column(db.String(20))  # zelle, cashapp, cash, check, square, other
    note = db.Column(db.String(200))
    status = db.Column(db.String(20), default='recorded', nullable=False)  # recorded, pending, rejected
    parent_id = db.Column(db.Integer, db.ForeignKey('users.id'))  # if submitted by a logged-in parent
    donation_date = db.Column(db.Date, default=date.today, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    parent = db.relationship('User')

    def __repr__(self):
        return f'<Donation ${self.amount} {self.status}>'


# ── Self-registration & waitlists ───────────────────────────────────

class Registration(db.Model):
    """A public enrollment request awaiting admin approval."""
    __tablename__ = 'registrations'

    id = db.Column(db.Integer, primary_key=True)
    parent_name = db.Column(db.String(120), nullable=False)
    parent_email = db.Column(db.String(120), nullable=False)
    parent_phone = db.Column(db.String(20))
    students_json = db.Column(db.Text)  # JSON list of {first_name, last_name, dob, allergies}
    class_ids = db.Column(db.String(200))  # comma-separated requested class ids
    note = db.Column(db.Text)
    status = db.Column(db.String(20), default='pending', nullable=False)  # pending, approved, rejected
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    reviewed_at = db.Column(db.DateTime)
    reviewed_by = db.Column(db.Integer, db.ForeignKey('users.id'))

    reviewer = db.relationship('User')

    def __repr__(self):
        return f'<Registration {self.parent_name} {self.status}>'


class WaitlistEntry(db.Model):
    """A student waiting for a spot in a full class."""
    __tablename__ = 'waitlist_entries'

    id = db.Column(db.Integer, primary_key=True)
    class_id = db.Column(db.Integer, db.ForeignKey('classes.id'), nullable=False)
    student_id = db.Column(db.Integer, db.ForeignKey('students.id'), nullable=False)
    parent_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    status = db.Column(db.String(20), default='waiting', nullable=False)  # waiting, enrolled, removed
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    dance_class = db.relationship('DanceClass', backref='waitlist_entries')
    student = db.relationship('Student', backref='waitlist_entries')

    __table_args__ = (
        db.UniqueConstraint('class_id', 'student_id', name='unique_waitlist_entry'),
    )

    def __repr__(self):
        return f'<WaitlistEntry c={self.class_id} s={self.student_id} {self.status}>'


# ── Skills & progress ───────────────────────────────────────────────

class Skill(db.Model):
    """A skill or level a dancer can achieve (optionally scoped to a class)."""
    __tablename__ = 'skills'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    category = db.Column(db.String(60))  # e.g. Ballet, Level 1, Technique
    class_id = db.Column(db.Integer, db.ForeignKey('classes.id'))
    display_order = db.Column(db.Integer, default=0, nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    dance_class = db.relationship('DanceClass')
    achievements = db.relationship('StudentSkill', backref='skill', lazy='dynamic')

    def __repr__(self):
        return f'<Skill {self.name}>'


class StudentSkill(db.Model):
    """Record of a student achieving a skill."""
    __tablename__ = 'student_skills'

    id = db.Column(db.Integer, primary_key=True)
    skill_id = db.Column(db.Integer, db.ForeignKey('skills.id'), nullable=False)
    student_id = db.Column(db.Integer, db.ForeignKey('students.id'), nullable=False)
    achieved_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    note = db.Column(db.String(200))
    awarded_by = db.Column(db.Integer, db.ForeignKey('users.id'))

    student = db.relationship('Student', backref='skill_achievements')

    __table_args__ = (
        db.UniqueConstraint('skill_id', 'student_id', name='unique_student_skill'),
    )

    def __repr__(self):
        return f'<StudentSkill skill={self.skill_id} student={self.student_id}>'


# ── Makeup classes ──────────────────────────────────────────────────

class MakeupClass(db.Model):
    """A makeup session for a missed class."""
    __tablename__ = 'makeup_classes'

    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('students.id'), nullable=False)
    missed_class_id = db.Column(db.Integer, db.ForeignKey('classes.id'))
    missed_date = db.Column(db.Date)
    makeup_class_id = db.Column(db.Integer, db.ForeignKey('classes.id'))
    makeup_date = db.Column(db.Date)
    status = db.Column(db.String(20), default='requested', nullable=False)  # requested, scheduled, completed, cancelled
    note = db.Column(db.String(200))
    requested_by = db.Column(db.Integer, db.ForeignKey('users.id'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    student = db.relationship('Student', backref='makeups')
    missed_class = db.relationship('DanceClass', foreign_keys=[missed_class_id])
    makeup_class = db.relationship('DanceClass', foreign_keys=[makeup_class_id])

    def __repr__(self):
        return f'<MakeupClass s={self.student_id} {self.status}>'


# ── Leads / trial pipeline ──────────────────────────────────────────

class Lead(db.Model):
    """A prospective family / trial-class lead."""
    __tablename__ = 'leads'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(120))
    phone = db.Column(db.String(20))
    interest = db.Column(db.String(120))  # style/class/age they asked about
    source = db.Column(db.String(60))  # walk-in, referral, instagram, website...
    status = db.Column(db.String(20), default='new', nullable=False)  # new, contacted, trial_scheduled, converted, lost
    trial_date = db.Column(db.Date)
    note = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f'<Lead {self.name} {self.status}>'


# ── Staff time clock ────────────────────────────────────────────────

class TimeClockEntry(db.Model):
    """A staff clock-in/out record for payroll hours."""
    __tablename__ = 'time_clock_entries'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    # Studio-local (datetime.now, not utcnow): the payroll report filters punches
    # by local date, so an evening shift stored in UTC would land on the next day
    # and slip out of the report window. clock_out is set local to match.
    clock_in = db.Column(db.DateTime, default=datetime.now, nullable=False)
    clock_out = db.Column(db.DateTime)
    note = db.Column(db.String(200))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    user = db.relationship('User', backref='time_entries')

    @property
    def hours(self):
        if not self.clock_out:
            return None
        return round((self.clock_out - self.clock_in).total_seconds() / 3600, 2)

    def __repr__(self):
        return f'<TimeClockEntry u={self.user_id} in={self.clock_in}>'


# ── Recital hub (annual show organizer) ─────────────────────────────

class Recital(db.Model):
    """A single year's recital — the per-year container that ties the show
    order, music/choreography, awards, and booklet together in one place."""
    __tablename__ = 'recitals'

    id = db.Column(db.Integer, primary_key=True)
    year = db.Column(db.Integer, nullable=False, index=True)
    title = db.Column(db.String(150), nullable=False)
    theme = db.Column(db.String(150))
    recital_date = db.Column(db.Date)
    show_times = db.Column(db.String(120))  # free text, e.g. "Matinee 2 PM · Evening 6 PM"
    venue = db.Column(db.String(200))
    director_note = db.Column(db.Text)       # for the booklet
    acknowledgments = db.Column(db.Text)     # thank-yous / sponsors, for the booklet
    cover_image_data = db.Column(db.Text)    # optional booklet cover (data URI)
    ad_pricing_note = db.Column(db.Text)     # blurb describing ad sizes/prices
    is_active = db.Column(db.Boolean, default=False, nullable=False)  # the "current" recital
    is_locked = db.Column(db.Boolean, default=False, nullable=False)  # freeze the show order
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    numbers = db.relationship('RecitalNumber', backref='recital', lazy='dynamic',
                              cascade='all, delete-orphan')
    awards = db.relationship('RecitalAward', backref='recital', lazy='dynamic',
                             cascade='all, delete-orphan')
    ads = db.relationship('RecitalAd', backref='recital', lazy='dynamic',
                          cascade='all, delete-orphan')
    performances = db.relationship('Performance', backref='recital', lazy='dynamic')

    def __repr__(self):
        return f'<Recital {self.year} {self.title}>'


class RecitalNumber(db.Model):
    """One routine in the show order. Also holds the music + choreography
    details teachers organize for that number."""
    __tablename__ = 'recital_numbers'

    id = db.Column(db.Integer, primary_key=True)
    recital_id = db.Column(db.Integer, db.ForeignKey('recitals.id'), nullable=False)
    order_index = db.Column(db.Integer, default=0, nullable=False)  # position in the show
    title = db.Column(db.String(150), nullable=False)
    class_id = db.Column(db.Integer, db.ForeignKey('classes.id'))
    group_id = db.Column(db.Integer, db.ForeignKey('performance_groups.id'))
    style = db.Column(db.String(60))   # Ballet, Hip-Hop, Tap...
    act = db.Column(db.String(20))     # optional grouping, e.g. "Act 1"

    # Music
    song_title = db.Column(db.String(150))
    song_artist = db.Column(db.String(150))
    music_url = db.Column(db.Text)     # link to shared audio (Drive/Dropbox)
    music_notes = db.Column(db.Text)   # cue notes (fade in/out, edits)

    # Choreography
    choreographer = db.Column(db.String(120))
    choreo_notes = db.Column(db.Text)
    choreo_url = db.Column(db.Text)    # link to reference video/notes
    formation_notes = db.Column(db.Text)
    duration = db.Column(db.String(20))   # "2:45"
    props = db.Column(db.String(200))

    is_finale = db.Column(db.Boolean, default=False, nullable=False)
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    dance_class = db.relationship('DanceClass')
    group = db.relationship('PerformanceGroup')
    cast = db.relationship('RecitalCast', backref='number', lazy='dynamic',
                           cascade='all, delete-orphan')

    def __repr__(self):
        return f'<RecitalNumber #{self.order_index} {self.title}>'


class RecitalCast(db.Model):
    """A dancer performing in a recital number."""
    __tablename__ = 'recital_cast'

    id = db.Column(db.Integer, primary_key=True)
    number_id = db.Column(db.Integer, db.ForeignKey('recital_numbers.id'), nullable=False)
    student_id = db.Column(db.Integer, db.ForeignKey('students.id'), nullable=False)
    part = db.Column(db.String(80))  # e.g. "Soloist", "Lead" (optional)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    student = db.relationship('Student', backref='recital_cast')

    __table_args__ = (
        db.UniqueConstraint('number_id', 'student_id', name='unique_recital_cast'),
    )

    def __repr__(self):
        return f'<RecitalCast n={self.number_id} s={self.student_id}>'


class RecitalAward(db.Model):
    """A student award presented at the recital."""
    __tablename__ = 'recital_awards'

    id = db.Column(db.Integer, primary_key=True)
    recital_id = db.Column(db.Integer, db.ForeignKey('recitals.id'), nullable=False)
    title = db.Column(db.String(150), nullable=False)        # "Dancer of the Year"
    category = db.Column(db.String(60))                      # "Senior Award", "Studio Award"
    student_id = db.Column(db.Integer, db.ForeignKey('students.id'))  # roster recipient
    recipient_text = db.Column(db.String(150))              # freeform recipient (non-roster)
    description = db.Column(db.Text)
    order_index = db.Column(db.Integer, default=0, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    student = db.relationship('Student', backref='recital_awards')

    def __repr__(self):
        return f'<RecitalAward {self.title}>'


class RecitalAd(db.Model):
    """A custom ad / shout-out placed in the recital booklet."""
    __tablename__ = 'recital_ads'

    id = db.Column(db.Integer, primary_key=True)
    recital_id = db.Column(db.Integer, db.ForeignKey('recitals.id'), nullable=False)
    advertiser = db.Column(db.String(150), nullable=False)  # business or family name
    size = db.Column(db.String(40), default='shout_out', nullable=False)
    # full_page, half_page, quarter_page, business_card, shout_out
    price = db.Column(db.Numeric(10, 2), default=0, nullable=False)
    content = db.Column(db.Text)         # ad copy / shout-out message
    image_data = db.Column(db.Text)      # uploaded ad artwork (data URI)
    contact_name = db.Column(db.String(120))
    contact_email = db.Column(db.String(120))
    student_id = db.Column(db.Integer, db.ForeignKey('students.id'))  # "in honor of" dancer
    status = db.Column(db.String(20), default='submitted', nullable=False)  # submitted, approved, placed
    paid = db.Column(db.Boolean, default=False, nullable=False)
    paid_at = db.Column(db.DateTime)
    order_index = db.Column(db.Integer, default=0, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    student = db.relationship('Student', backref='recital_ads')

    def __repr__(self):
        return f'<RecitalAd {self.advertiser} {self.size}>'