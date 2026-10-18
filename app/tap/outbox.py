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
  - SMS: only with an explicit opt-in, only to the one number that agreed
    (never the dancer's phone or another guardian's), and a Twilio STOP
    (21610) turns the opt-in off for good
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


def sms_numbers(student):
    """Where a text notice may go: only the number that opted in. Consent is
    per number, so another guardian on file is never texted on someone
    else's say-so."""
    if not student.tap_sms_opt_in or not student.tap_sms_phone:
        return []
    return [student.tap_sms_phone]


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
        else:
            sent += _send_by_recipients(live, channel, now, flags, send_email, send_sms)
        db.session.commit()
    return sent


def _recipients(n, channel):
    """Who a notice about this dancer may go to.

    Email uses helpers.student_emails(): the dancer's own parent addresses
    plus the household's (Family.primary_email / secondary_email). The
    household addresses are, by the studio's contact model, guardians of
    every child in that household: balance reminders, receipts and blasts
    already go to them for each sibling. A guardian who is a contact for only
    one child belongs on that child's parent_email / parent_email_2, not on
    the family row. SMS goes only to the number that opted in for this
    dancer."""
    if channel == 'email':
        from app.helpers import student_emails
        return student_emails(n.student)
    return sms_numbers(n.student)


def _delivered(n):
    return {x.strip().lower() for x in (n.recipients or '').split(',') if x.strip()}


def _send_by_recipients(live, channel, now, flags, send_email, send_sms):
    """Send one household's due notices, one message per address, naming only
    the children that address is a contact for. A parent on both siblings
    gets one message about both; a guardian on only one dancer hears about
    that dancer alone, so nobody learns about a child they aren't a contact
    for.

    Delivery is tracked per notice per address in `recipients`: an address
    that already got a notice is never sent it again, a failed address is
    retried on its own, and a notice counts as sent only once every one of
    its addresses has it."""
    from app import email as email_service
    from app import sms as sms_service
    if channel == 'email' and not email_service.is_configured():
        _skip(live, 'email not configured')
        return 0
    if channel == 'sms' and not sms_service.is_configured():
        _skip(live, 'SMS not configured')
        return 0
    if flags['cap'] and _sent_today() >= flags['cap']:
        _skip(live, 'daily cap reached')
        return 0
    rows, wanted, done = [], {}, {}
    order = OrderedDict()   # address (lower) -> address as written
    for n in live:
        addrs = _recipients(n, channel)
        if not addrs:
            _skip([n], 'no parent email' if channel == 'email' else 'no opted-in number')
            continue
        rows.append(n)
        wanted[n.id] = {a.lower() for a in addrs}
        done[n.id] = _delivered(n) & wanted[n.id]
        for a in addrs:
            order.setdefault(a.lower(), a)
    failed, stopped, messages = set(), set(), 0
    for key, addr in order.items():
        about = [n for n in rows if key in wanted[n.id] and key not in done[n.id]]
        if not about:
            continue
        if channel == 'email':
            subject, body = compose_email(about)
            try:
                send_email(addr, subject, body)
                ok, code = True, None
            except Exception as e:  # noqa: BLE001 - SMTP raises many types
                logger.warning('Tap notice email to one address failed: %s', e)
                ok, code = False, None
        else:
            ok, code = send_sms(addr, compose_sms(about))
        if ok:
            messages += 1
            for n in about:
                done[n.id].add(key)
        elif channel == 'sms' and code in sms_service.OPTED_OUT_CODES:
            stopped.add(key)
            _record_stop(addr)
        else:
            failed.add(key)
    for n in rows:
        n.recipients = ', '.join(order[k] for k in order if k in done[n.id])
        missing = wanted[n.id] - done[n.id] - stopped
        if not missing:
            if done[n.id]:
                n.status, n.sent_at = 'sent', n.sent_at or now
            else:
                _skip([n], 'parent replied STOP')
        elif missing & failed:
            _retry([n], now, f'{channel} failed for {len(missing)} of {len(wanted[n.id])} address(es)')
    return messages


def _record_stop(phone):
    """Twilio says this number replied STOP. Turn SMS off for every dancer it
    belongs to, so we never retry it."""
    from app.models import AuditLog
    for s in Student.query.filter(Student.tap_sms_opt_in.is_(True),
                                  Student.tap_sms_phone == phone).all():
        s.tap_sms_opt_in = False
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
