"""Arrival and departure notices for parents, sent off the request path.

enqueue() runs inside the tap's own transaction and only writes rows. One
daemon thread (start_worker / kick) drains them, so a slow SMTP server or a
15 second Twilio timeout never holds one of the single gunicorn worker's four
threads. Rows survive a restart: the worker drains whatever is pending at
boot, and anything older than STALE_AFTER is skipped instead of sent late.

Rules applied at send time (so a change made after the tap still wins):
  - studio pause switch, per-channel on/off, daily cap
  - the tap was undone -> skip
  - the dancer is 18 or older -> skip (adults don't need their own notice)
  - email: on unless the dancer's household opted out
  - SMS: only with an explicit opt-in, only to guardian numbers, and a
    Twilio STOP (21610) turns the opt-in off for good
  - siblings who tap within COALESCE seconds get one message
Wording is first name, class and time. It says "checked out", never
"picked up": this is attendance, not custody verification.
"""

import logging
import threading
from collections import OrderedDict
from datetime import date, datetime, timedelta

from flask import current_app

from app import db
from app.models import Setting, Student, TapEvent, TapNotification

logger = logging.getLogger(__name__)

COALESCE = timedelta(seconds=45)       # siblings tapping together -> one message
STALE_AFTER = timedelta(minutes=20)    # never send "checked in at 5:02" at 7:30
RETRY_LIMIT = 3
DEFAULT_DAILY_CAP = 400                # under Gmail's ~500/day for a consumer account
STUDIO_NAME = "LaShelle's School of Dance"


def setting_flags():
    try:
        cap = int(Setting.get('tap_notify_daily_cap', str(DEFAULT_DAILY_CAP)) or DEFAULT_DAILY_CAP)
    except (TypeError, ValueError):
        cap = DEFAULT_DAILY_CAP
    return {
        'email': Setting.get_bool('tap_notify_email_enabled', default=True),
        'sms': Setting.get_bool('tap_notify_sms_enabled', default=False),
        'paused': Setting.get_bool('tap_notify_paused', default=False),
        'cap': max(0, cap),
    }


def household_key(student) -> str:
    return f'f{student.family_id}' if student.family_id else f's{student.id}'


def is_adult(student) -> bool:
    age = student.age
    return age is not None and age >= 18


def guardian_phones(student):
    """Guardian numbers only. Never the dancer's own phone."""
    from app.sms import normalize_us_phone
    fam = student.family
    out = []
    for raw in (student.parent_phone,
                fam.primary_phone if fam else None,
                fam.secondary_phone if fam else None):
        p = normalize_us_phone(raw)
        if p and p not in out:
            out.append(p)
    return out


# ── Enqueue (inside the tap transaction) ────────────────────────────

def enqueue(event: TapEvent, kind: str):
    """Queue the notices one tap should send. Writes rows only; no network."""
    student = event.student
    if student is None or is_adult(student):
        return
    flags = setting_flags()
    now = datetime.now().replace(microsecond=0)
    key = household_key(student)
    channels = []
    if flags['email'] and not student.tap_email_opt_out:
        channels.append('email')
    if flags['sms'] and student.tap_sms_opt_in:
        channels.append('sms')
    for ch in channels:
        # A sibling already waiting to go out: ride along in the same message.
        waiting = (TapNotification.query.filter(
            TapNotification.household_key == key, TapNotification.kind == kind,
            TapNotification.channel == ch, TapNotification.status == 'pending',
            TapNotification.send_after > now).order_by(TapNotification.send_after).first())
        send_after = waiting.send_after if waiting else now + COALESCE
        db.session.add(TapNotification(tap_event_id=event.id, student_id=student.id,
                                       household_key=key, kind=kind, channel=ch,
                                       send_after=send_after))


def cancel_for_event(event_id: int, reason='undone'):
    """An undone tap must not tell anyone anything. Caller commits."""
    for n in TapNotification.query.filter_by(tap_event_id=event_id, status='pending').all():
        n.status = 'skipped'
        n.reason = reason


# ── Compose ─────────────────────────────────────────────────────────

def _names(students):
    names = [s.first_name for s in students]
    if len(names) <= 2:
        return ' and '.join(names)
    return ', '.join(names[:-1]) + ', and ' + names[-1]


def _clock(dt):
    return dt.strftime('%I:%M %p').lstrip('0')


def _lines(rows):
    out = []
    for n in rows:
        ev = n.event
        cname = ev.dance_class.name if ev.dance_class else 'class'
        verb = 'checked in to' if n.kind == 'arrival' else 'checked out of'
        out.append(f'{n.student.first_name} {verb} {cname} at {_clock(ev.occurred_at)}.')
    return out


def compose_email(rows):
    kids = list(OrderedDict((n.student_id, n.student) for n in rows).values())
    verb = 'checked in' if rows[0].kind == 'arrival' else 'checked out'
    subject = f'{_names(kids)} {verb} at {_clock(rows[0].event.occurred_at)}'
    portal = (current_app.config.get('PORTAL_URL') or '').rstrip('/')
    body = '\n'.join(_lines(rows)) + (
        f'\n\nThis is an automatic attendance notice from {STUDIO_NAME}. '
        f'To turn these off, sign in at {portal} and switch off check-in notices, '
        'or just reply to this email.'
    )
    return subject, body


def compose_sms(rows):
    return 'LSO Dance: ' + ' '.join(_lines(rows)) + ' Reply STOP to opt out.'


# ── Drain (background thread, or tests directly) ────────────────────

_send_email = None
_send_sms = None


def _senders():
    from app import email as email_service
    from app import sms as sms_service
    return (_send_email or email_service.send_email,
            _send_sms or sms_service.send_sms_result)


def _sent_today():
    start = datetime.combine(date.today(), datetime.min.time())
    return TapNotification.query.filter(TapNotification.status == 'sent',
                                        TapNotification.sent_at >= start).count()


def _skip(rows, reason):
    for n in rows:
        n.status = 'skipped'
        n.reason = reason


def _retry(rows, now, reason):
    for n in rows:
        n.attempts += 1
        if n.attempts >= RETRY_LIMIT:
            n.status = 'failed'
            n.reason = reason[:80]
        else:
            n.send_after = now + timedelta(minutes=2 ** n.attempts)
            n.reason = reason[:80]


def drain_once(now=None) -> int:
    """Send every due notice. Returns how many messages went out."""
    from app import email as email_service
    from app import sms as sms_service
    now = (now or datetime.now()).replace(microsecond=0)
    send_email, send_sms = _senders()
    flags = setting_flags()
    due = (TapNotification.query.filter(TapNotification.status == 'pending',
                                        TapNotification.send_after <= now)
           .order_by(TapNotification.id).limit(200).all())
    groups = OrderedDict()
    for n in due:
        groups.setdefault((n.household_key, n.kind, n.channel), []).append(n)
    sent = 0
    for (_, kind, channel), rows in groups.items():
        live = []
        for n in rows:
            ev = n.event
            if ev is None or ev.undone:
                _skip([n], 'undone')
            elif now - ev.occurred_at > STALE_AFTER:
                _skip([n], 'too late to be useful')
            elif is_adult(n.student):
                _skip([n], 'adult dancer')
            elif channel == 'email' and n.student.tap_email_opt_out:
                _skip([n], 'household opted out')
            elif channel == 'sms' and not n.student.tap_sms_opt_in:
                _skip([n], 'no SMS opt-in')
            else:
                live.append(n)
        if not live:
            db.session.commit()
            continue
        if flags['paused']:
            _skip(live, 'notices paused')
        elif not flags[channel]:
            _skip(live, f'{channel} notices off')
        elif flags['cap'] and _sent_today() >= flags['cap']:
            _skip(live, 'daily cap reached')
        elif channel == 'email':
            to = []
            for n in live:
                from app.helpers import student_emails
                for addr in student_emails(n.student):
                    if addr.lower() not in {a.lower() for a in to}:
                        to.append(addr)
            if not to:
                _skip(live, 'no parent email')
            elif not email_service.is_configured():
                _skip(live, 'email not configured')
            else:
                subject, body = compose_email(live)
                try:
                    send_email(to, subject, body)
                except Exception as e:  # noqa: BLE001 - SMTP raises many types
                    logger.warning('Tap notice email failed: %s', e)
                    _retry(live, now, f'email error: {e}')
                else:
                    for n in live:
                        n.status, n.sent_at, n.recipients = 'sent', now, ', '.join(to)
                    sent += 1
        else:
            phones = []
            for n in live:
                for p in guardian_phones(n.student):
                    if p not in phones:
                        phones.append(p)
            if not phones:
                _skip(live, 'no guardian phone')
            elif not sms_service.is_configured():
                _skip(live, 'SMS not configured')
            else:
                body = compose_sms(live)
                delivered, failed_codes = [], []
                for p in phones:
                    ok, code = send_sms(p, body)
                    if ok:
                        delivered.append(p)
                    else:
                        failed_codes.append((p, code))
                for p, code in failed_codes:
                    if code in sms_service.OPTED_OUT_CODES:
                        _record_stop(p)
                if delivered:
                    for n in live:
                        n.status, n.sent_at, n.recipients = 'sent', now, ', '.join(delivered)
                    sent += 1
                elif failed_codes and all(c in sms_service.OPTED_OUT_CODES for _, c in failed_codes):
                    _skip(live, 'parent replied STOP')
                else:
                    _retry(live, now, f'SMS error {failed_codes[0][1] if failed_codes else ""}')
        db.session.commit()
    return sent


def _record_stop(phone):
    """Twilio says this number replied STOP. Turn SMS off for every dancer it
    belongs to, so we never retry it."""
    for s in Student.query.filter(Student.tap_sms_opt_in.is_(True)).all():
        if phone in guardian_phones(s):
            s.tap_sms_opt_in = False
            from app.models import AuditLog
            AuditLog.record(None, 'tap.sms_stop', f'{s.full_name}: parent replied STOP')


# ── Worker thread ───────────────────────────────────────────────────

_wake = threading.Event()
_lock = threading.Lock()
_worker = None
POLL_SECONDS = 15


def worker_enabled(app) -> bool:
    """Production runs the sender; dev and the test harnesses don't, unless
    TAP_NOTIFY_WORKER=1 says so (tests drive drain_once() directly)."""
    import os
    env = os.environ.get('TAP_NOTIFY_WORKER')
    if env is not None:
        return env == '1'
    return bool(app.config.get('TAP_NOTIFY_WORKER')) and not app.testing


def start_worker(app):
    global _worker
    if not worker_enabled(app):
        return
    with _lock:
        if _worker is not None and _worker.is_alive():
            return
        _worker = threading.Thread(target=_loop, args=(app,), daemon=True, name='tap-notices')
        _worker.start()


def kick():
    """Wake the sender (after a tap commits). Starts it if it isn't running."""
    try:
        app = current_app._get_current_object()
    except RuntimeError:
        return
    start_worker(app)
    _wake.set()


def _loop(app):
    while True:
        _wake.wait(timeout=POLL_SECONDS)
        _wake.clear()
        with app.app_context():
            try:
                drain_once()
            except Exception:  # noqa: BLE001 - the sender must never die
                db.session.rollback()
                logger.exception('Tap notice drain failed')
            finally:
                db.session.remove()
