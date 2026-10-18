"""One tap in, one attendance change out.

process_tap() is the only place a tap becomes attendance. It takes no Flask
request and reads no current_user, so the tag URL page, the Android kiosk, the
USB reader and the offline replay batch all call it the same way, and tests
can pass any `occurred_at`.

The rules, in order:
  1. A replayed `event_key` returns the original result and writes nothing.
  2. Unknown, lost, retired, pending or unassigned tags are refused and logged.
  3. A tap within `tap_dedup_seconds` of the dancer's last tap is a duplicate:
     logged, no state change. This lives in the DB so it holds across threads
     and restarts.
  4. Direction comes from today's tap events. With a session open:
       - before `tap_dwell_minutes` the tap is "already in" (no change),
       - after it, the tap checks the dancer out of that class,
       - when that class has ended and a later class of today's has opened its
         window (rollover on, back-to-back), the first closes at its scheduled
         end, any class in between is marked for its scheduled hour, and the
         tap checks in to the last one, or is the departure from it if that
         class is over too.
  5. With no session open the tap checks in to the class whose window
     (start minus `tap_early_minutes` through end plus `tap_late_minutes`)
     contains the tap. Several matches resolve deterministically. No match
     asks staff to pick a class and writes no attendance.

Check-out updates the existing attendance row. It never inserts a second one:
the unique (student, class, day) index and the boot-time de-dupe in
app/migrations.py would reject or delete it.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from app import db
from app.models import (
    Attendance, ClassEnrollment, DanceClass, MakeupClass, Setting, Student, TapEvent,
)
from app.tap.credentials import find_tag

logger = logging.getLogger(__name__)

SETTING_DEFAULTS = {
    'tap_early_minutes': 30,
    'tap_late_minutes': 0,
    'tap_dedup_seconds': 10,
    'tap_dwell_minutes': 5,
    'tap_tardy_grace_minutes': 0,   # 0 = off; teachers own the T by default
}
SETTING_LIMITS = {
    'tap_early_minutes': 240,
    'tap_late_minutes': 240,
    'tap_dedup_seconds': 300,
    'tap_dwell_minutes': 180,
    'tap_tardy_grace_minutes': 120,
}

IN_OUTCOMES = ('checked_in', 'reopened')
OUT_OUTCOMES = ('checked_out', 'rolled_over')
STATEFUL = IN_OUTCOMES + OUT_OUTCOMES
# Outcomes a fresh tap is a duplicate of. Refused-tag outcomes are left out so
# a staff member can't be blocked by a stranger's card.
DEDUP_OF = STATEFUL + ('already_in', 'already_out', 'needs_class')
SOURCES = ('url', 'kiosk', 'usb', 'api')
# A tap this soon after a check-out reads as 'already out', not 'pick a class'.
ALREADY_OUT_WINDOW = timedelta(minutes=60)


class TapError(ValueError):
    """A tap request the service refuses (bad class pick, bad undo). With
    `retryable` set it was a transient conflict, and the same request (same
    event_key) can safely be sent again."""

    def __init__(self, message='', retryable=False):
        super().__init__(message)
        self.retryable = retryable


def setting_int(key: str) -> int:
    default = SETTING_DEFAULTS[key]
    try:
        val = int(Setting.get(key, str(default)) or default)
    except (TypeError, ValueError):
        return default
    return max(0, min(val, SETTING_LIMITS[key]))


def rollover_enabled() -> bool:
    return Setting.get_bool('tap_rollover_enabled', default=True)


@dataclass
class TapRequest:
    event_key: str
    token: str | None = None
    uid: str | None = None
    occurred_at: datetime | None = None
    source: str = 'api'
    actor_id: int | None = None
    class_id: int | None = None   # staff's pick after a needs_class result
    tag_id: int | None = None     # resolve by tag row (a pick made from a past result)


@dataclass
class TapResult:
    outcome: str
    event: TapEvent | None = None
    student: Student | None = None
    dance_class: DanceClass | None = None
    attendance: Attendance | None = None
    candidates: list = field(default_factory=list)
    rolled_from: DanceClass | None = None
    since: datetime | None = None
    previous_outcome: str | None = None
    replayed: bool = False
    tag_status: str | None = None

    def to_dict(self) -> dict:
        st, cls, att = self.student, self.dance_class, self.attendance
        return {
            'outcome': self.outcome,
            'previous_outcome': self.previous_outcome,
            'replayed': self.replayed,
            'event_id': self.event.id if self.event else None,
            'occurred_at': _iso(self.event.occurred_at) if self.event else None,
            'student': ({'id': st.id, 'first_name': st.first_name,
                         'last_name': st.last_name, 'family_id': st.family_id}
                        if st else None),
            'class': _class_dict(cls),
            'rolled_from': _class_dict(self.rolled_from),
            'attendance_id': att.id if att else None,
            'check_in_time': _iso(att.check_in_time) if att else None,
            'check_out_time': _iso(att.check_out_time) if att else None,
            'is_tardy': bool(att.is_tardy) if att else False,
            'since': _iso(self.since),
            'candidates': self.candidates,
            'tag_status': self.tag_status,
            'tag_label': (self.event.tag.label
                          if self.event and self.event.tag else None),
            'message': message_for(self),
            # A reloaded result page keeps its Undo; undo_tap() itself refuses
            # anything but the dancer's most recent tap.
            'can_undo': bool(self.event
                             and self.event.outcome in ('checked_in', 'checked_out', 'reopened')
                             and not self.event.undone),
            'can_reopen': self.outcome in ('checked_out', 'already_out')
                          or (self.outcome == 'duplicate'
                              and self.previous_outcome == 'checked_out'),
        }


def _iso(dt):
    return dt.isoformat() if dt else None


def _clock(dt) -> str:
    return dt.strftime('%I:%M %p').lstrip('0') if dt else ''


def _class_dict(cls):
    if not cls:
        return None
    return {'id': cls.id, 'name': cls.name,
            'start_time': cls.start_time.strftime('%H:%M'),
            'end_time': cls.end_time.strftime('%H:%M')}


def message_for(r: TapResult) -> str:
    cname = r.dance_class.name if r.dance_class else ''
    o = r.previous_outcome if r.outcome == 'duplicate' and r.previous_outcome else r.outcome
    if o == 'checked_in':
        return f'Checked in to {cname}' if cname else 'Checked in'
    if o in ('checked_out', 'rolled_over'):
        return f'Checked out of {cname}' if cname else 'Checked out'
    if o == 'reopened':
        return f'Back in {cname}' if cname else 'Back in class'
    if o == 'already_in':
        return f'Already checked in at {_clock(r.since)}'
    if o == 'already_out':
        return f'Already checked out at {_clock(r.since)}'
    if o == 'needs_class':
        return 'No class right now. Pick one.'
    if o == 'unknown_tag':
        return 'Tag not recognized'
    if o == 'inactive_tag':
        return {'lost': 'This tag was reported lost',
                'retired': 'This tag is retired',
                'pending': "This tag isn't finished being set up"}.get(r.tag_status,
                                                                    'This tag is not active')
    if o == 'unassigned_tag':
        return "This tag isn't assigned to a dancer"
    if o == 'inactive_student':
        return 'This dancer is not active'
    return ''


# ── Class resolution ────────────────────────────────────────────────

def _window(cls, day, early, late):
    start = datetime.combine(day, cls.start_time)
    end = datetime.combine(day, cls.end_time)
    return start - early, start, end, end + late


def todays_classes_for(student, now):
    """Live classes the dancer is enrolled in (or has a scheduled makeup in)
    that meet on `now`'s weekday."""
    from app.helpers import live_class_query
    day = now.date()
    enrolled = {cid for (cid,) in db.session.query(ClassEnrollment.class_id).filter(
        ClassEnrollment.student_id == student.id, ClassEnrollment.is_active.is_(True))}
    makeups = {cid for (cid,) in db.session.query(MakeupClass.makeup_class_id).filter(
        MakeupClass.student_id == student.id, MakeupClass.makeup_date == day,
        MakeupClass.status.in_(('scheduled', 'completed')),
        MakeupClass.makeup_class_id.isnot(None))}
    ids = enrolled | makeups
    if not ids:
        return []
    return (live_class_query()
            .filter(DanceClass.id.in_(ids), DanceClass.day_of_week == day.weekday())
            .order_by(DanceClass.start_time, DanceClass.id).all())


def candidate_classes(student, now):
    """Today's classes whose tap window contains `now`."""
    early = timedelta(minutes=setting_int('tap_early_minutes'))
    late = timedelta(minutes=setting_int('tap_late_minutes'))
    out = []
    for cls in todays_classes_for(student, now):
        w_open, _, _, w_close = _window(cls, now.date(), early, late)
        if w_open <= now <= w_close:
            out.append(cls)
    return out


def pick_class(classes, now):
    """One class from several, the same way every time: a class in session
    (latest start wins, so back-to-back picks the one just starting), else the
    next one to start, else the one that ended most recently."""
    day = now.date()

    def start(c):
        return datetime.combine(day, c.start_time)

    def end(c):
        return datetime.combine(day, c.end_time)

    in_session = [c for c in classes if start(c) <= now <= end(c)]
    if in_session:
        return max(in_session, key=lambda c: (start(c), -c.id))
    upcoming = [c for c in classes if start(c) > now]
    if upcoming:
        return min(upcoming, key=lambda c: (start(c), c.id))
    return max(classes, key=lambda c: (end(c), -c.id))


def picker_options(student, now):
    """Classes a staff member can pick for a needs_class tap: everything live
    today, the dancer's own classes first."""
    from app.helpers import live_class_query
    own = {c.id for c in todays_classes_for(student, now)}
    today = (live_class_query().filter(DanceClass.day_of_week == now.date().weekday())
             .order_by(DanceClass.start_time, DanceClass.id).all())
    today.sort(key=lambda c: (c.id not in own, c.start_time, c.id))
    return [{**_class_dict(c), 'enrolled': c.id in own} for c in today[:30]]


# ── Ledger helpers ──────────────────────────────────────────────────

def _todays_state_events(student_id, now):
    day = now.date()
    return (TapEvent.query.filter(
        TapEvent.student_id == student_id,
        TapEvent.outcome.in_(STATEFUL),
        TapEvent.undone.is_(False),
        func.date(TapEvent.occurred_at) == day,
        TapEvent.occurred_at <= now,
    ).order_by(TapEvent.occurred_at, TapEvent.id).all())


def _open_session(events):
    """(in_event, attendance) for the session still open, or (None, None). A
    teacher clearing the box or checking the dancer out by hand closes it."""
    if not events or events[-1].direction != 'in':
        return None, None
    last = events[-1]
    att = db.session.get(Attendance, last.attendance_id) if last.attendance_id else None
    if att is None or att.check_out_time is not None:
        return None, None
    return last, att


def _snapshot(att):
    if att is None:
        return {'created': True}
    return {
        'created': False,
        'is_present': att.is_present,
        'is_tardy': bool(att.is_tardy),
        'is_excused': bool(att.is_excused),
        'check_in_time': _iso(att.check_in_time),
        'check_in_method': att.check_in_method,
        'check_out_time': _iso(att.check_out_time),
    }


def _new_event(req, now, outcome, **kw):
    ev = TapEvent(event_key=req.event_key, occurred_at=now, source=req.source,
                  actor_id=req.actor_id, outcome=outcome, **kw)
    db.session.add(ev)
    return ev


def _notify(ev, kind):
    """Queue the parent notice in the same transaction as the tap."""
    from app.tap import outbox
    db.session.flush()
    outbox.enqueue(ev, kind)


def _commit_event(req, result):
    """Commit, turning an event_key race into the winner's result."""
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        winner = TapEvent.query.filter_by(event_key=req.event_key).first()
        if winner is None:
            raise
        return result_from_event(winner, replayed=True)
    return result


def _refuse(req, now, outcome, tag=None, student=None):
    ev = _new_event(req, now, outcome, tag_id=tag.id if tag else None,
                    student_id=student.id if student else None,
                    detail=(tag.status if tag and outcome == 'inactive_tag' else None))
    return _commit_event(req, TapResult(outcome=outcome, event=ev, student=student,
                                        tag_status=tag.status if tag else None))


def result_from_event(ev, replayed=False):
    """Rebuild the result screen for a past event (idempotent replay)."""
    prev = ev.detail if ev.outcome == 'duplicate' else None
    since = None
    if ev.outcome == 'already_in' and ev.attendance:
        since = ev.attendance.check_in_time
    elif ev.outcome == 'already_out' and ev.attendance:
        since = ev.attendance.check_out_time
    candidates = []
    if ev.outcome in ('needs_class', 'already_out') and ev.student:
        candidates = picker_options(ev.student, ev.occurred_at)
    return TapResult(outcome=ev.outcome, event=ev, student=ev.student,
                     dance_class=ev.dance_class, attendance=ev.attendance,
                     previous_outcome=prev, replayed=replayed, since=since,
                     candidates=candidates,
                     tag_status=ev.tag.status if ev.tag else None)


# ── The tap ─────────────────────────────────────────────────────────

def process_tap(req: TapRequest, _retried: bool = False) -> TapResult:
    if not req.event_key:
        raise TapError('event_key is required')
    existing = TapEvent.query.filter_by(event_key=req.event_key).first()
    if existing:
        return result_from_event(existing, replayed=True)

    now = (req.occurred_at or datetime.now()).replace(microsecond=0)
    if req.source not in SOURCES:
        req.source = 'api'

    if req.tag_id is not None:
        from app.models import NfcTag
        tag = db.session.get(NfcTag, req.tag_id)
    else:
        tag = find_tag(token=req.token, uid=req.uid)
    if tag is None:
        return _refuse(req, now, 'unknown_tag')
    student = tag.student
    if tag.status != 'active':
        return _refuse(req, now, 'inactive_tag', tag=tag, student=student)
    if student is None:
        return _refuse(req, now, 'unassigned_tag', tag=tag)
    if not student.is_active:
        return _refuse(req, now, 'inactive_student', tag=tag, student=student)

    picked = None
    if req.class_id is not None:
        from app.helpers import live_class_query
        picked = live_class_query().filter(DanceClass.id == req.class_id).first()
        if picked is None:
            raise TapError('That class is not running')

    # Duplicate: a second read of the same fob, a reload, a double tap. An
    # explicit class pick is a staff decision, not a re-read, so it skips this
    # (its own event_key already stops a double submit).
    if picked is None:
        window = timedelta(seconds=setting_int('tap_dedup_seconds'))
        recent = (TapEvent.query.filter(
            TapEvent.student_id == student.id,
            TapEvent.outcome.in_(DEDUP_OF),
            TapEvent.undone.is_(False),
            TapEvent.occurred_at > now - window,
            TapEvent.occurred_at <= now,
        ).order_by(TapEvent.occurred_at.desc(), TapEvent.id.desc()).first())
        if recent:
            ev = _new_event(req, now, 'duplicate', tag_id=tag.id, student_id=student.id,
                            class_id=recent.class_id, attendance_id=recent.attendance_id,
                            detail=recent.outcome)
            res = TapResult(outcome='duplicate', event=ev, student=student,
                            dance_class=recent.dance_class, attendance=recent.attendance,
                            previous_outcome=recent.outcome)
            if recent.outcome in ('needs_class', 'already_out'):
                res.candidates = picker_options(student, now)
            return _commit_event(req, res)

    events = _todays_state_events(student.id, now)
    in_event, open_att = _open_session(events)

    if in_event is not None:
        open_cls = in_event.dance_class
        day = now.date()
        if picked is not None and open_cls is not None and picked.id != open_cls.id:
            # Staff moved the dancer to another class: close this one now.
            return _roll(req, now, tag, student, in_event, open_att, [picked],
                         close_at=now, retried=_retried)
        if (picked is None and open_cls is not None and rollover_enabled()
                and now > datetime.combine(day, open_cls.end_time)):
            early = timedelta(minutes=setting_int('tap_early_minutes'))
            attended = {e.class_id for e in events if e.direction == 'in'}
            later = [c for c in todays_classes_for(student, now)
                     if c.id not in attended and c.start_time >= open_cls.end_time
                     and datetime.combine(day, c.start_time) - early <= now]
            if later:
                return _roll(req, now, tag, student, in_event, open_att, later,
                             close_at=datetime.combine(day, open_cls.end_time),
                             retried=_retried)

        if now - in_event.occurred_at < timedelta(minutes=setting_int('tap_dwell_minutes')):
            ev = _new_event(req, now, 'already_in', tag_id=tag.id, student_id=student.id,
                            class_id=in_event.class_id, attendance_id=open_att.id)
            return _commit_event(req, TapResult(
                outcome='already_in', event=ev, student=student, dance_class=open_cls,
                attendance=open_att, since=open_att.check_in_time))

        snap = _snapshot(open_att)
        open_att.check_out_time = now
        ev = _new_event(req, now, 'checked_out', direction='out', tag_id=tag.id,
                        student_id=student.id, class_id=in_event.class_id,
                        attendance_id=open_att.id, undo_data=_undo_blob(snap, open_att))
        _notify(ev, 'departure')
        return _commit_event(req, TapResult(outcome='checked_out', event=ev, student=student,
                                            dance_class=open_cls, attendance=open_att))

    # No session open.
    if picked is not None:
        return _check_in(req, now, tag, student, picked, retried=_retried)

    cands = candidate_classes(student, now)
    attended = {e.class_id for e in events if e.direction == 'in'}
    fresh = [c for c in cands if c.id not in attended]
    last_out = next((e for e in reversed(events) if e.direction == 'out'), None)
    recent_out = (last_out is not None and events[-1] is last_out
                  and now - last_out.occurred_at <= ALREADY_OUT_WINDOW)
    if not fresh and (cands or recent_out):
        # Tapped again after leaving: say so (staff can reopen), and offer the
        # picker in case this is really an arrival for another class.
        att = last_out.attendance if last_out else None
        ev = _new_event(req, now, 'already_out', tag_id=tag.id, student_id=student.id,
                        class_id=last_out.class_id if last_out else None,
                        attendance_id=att.id if att else None)
        return _commit_event(req, TapResult(
            outcome='already_out', event=ev, student=student,
            dance_class=last_out.dance_class if last_out else None, attendance=att,
            since=att.check_out_time if att else None,
            candidates=picker_options(student, now)))
    if not fresh:
        ev = _new_event(req, now, 'needs_class', tag_id=tag.id, student_id=student.id)
        return _commit_event(req, TapResult(outcome='needs_class', event=ev, student=student,
                                            candidates=picker_options(student, now)))
    return _check_in(req, now, tag, student, pick_class(fresh, now), retried=_retried)


def _retry_once(req, retried):
    """A unique-index race: re-run the tap once against the row the other
    writer just made. Never more than once, so a constraint that keeps failing
    is an error, not unbounded recursion."""
    db.session.rollback()
    if retried:
        raise TapError('Attendance changed at the same time. Tap again.', retryable=True)
    return process_tap(req, _retried=True)


def _upsert_present(student, cls, at, tardy_rule=True):
    """The dancer's attendance row for this class on `at`'s day, marked present
    at `at`. Returns (row, snapshot-before). Never inserts a second row."""
    day = at.date()
    att = Attendance.query.filter(
        Attendance.student_id == student.id,
        Attendance.class_id == cls.id,
        func.date(Attendance.check_in_time) == day,
    ).first()
    snap = _snapshot(att)
    grace = setting_int('tap_tardy_grace_minutes') if tardy_rule else 0
    late = grace > 0 and at > datetime.combine(day, cls.start_time) + timedelta(minutes=grace)
    if att is None:
        att = Attendance(student_id=student.id, class_id=cls.id, check_in_time=at,
                         check_in_method='tap', is_present=True, is_tardy=late)
        db.session.add(att)
    else:
        # Same as manual check-in over an A or E: the dancer showed up. A P the
        # teacher already marked keeps its T unless the tardy rule is on.
        was_present = att.is_present
        att.is_present = True
        att.is_excused = False
        att.check_in_time = at
        att.check_in_method = 'tap'
        att.check_out_time = None
        if grace > 0:
            att.is_tardy = late
        elif not was_present:
            att.is_tardy = False
    db.session.flush()
    return att, snap


def _check_in(req, now, tag, student, cls, rolled_from=None, rolled_ids=None, retried=False):
    try:
        att, snap = _upsert_present(student, cls, now, tardy_rule=rolled_from is None)
    except IntegrityError:
        # A manual mark raced us to the unique (student, class, day) index.
        return _retry_once(req, retried)
    if rolled_ids:
        snap['rolled_event_ids'] = rolled_ids
    ev = _new_event(req, now, 'checked_in', direction='in', tag_id=tag.id,
                    student_id=student.id, class_id=cls.id, attendance_id=att.id,
                    undo_data=_undo_blob(snap, att))
    if rolled_from is None:
        # A rolled-over check-in is the same dancer staying for the next
        # class: they never left, so nobody is told they arrived.
        _notify(ev, 'arrival')
    return _commit_event(req, TapResult(outcome='checked_in', event=ev, student=student,
                                        dance_class=cls, attendance=att,
                                        rolled_from=rolled_from))


def _roll(req, now, tag, student, in_event, open_att, later, close_at, retried=False):
    """Back-to-back classes. Close the open class at `close_at`, mark any class
    that started and ended since then present for its scheduled hour, then:
    if the last class is still running the tap checks the dancer in to it, and
    if it is over too the tap is their departure from it. Every row written is
    snapshotted so one undo reverses the whole chain."""
    day = now.date()
    open_cls = in_event.dance_class
    later = sorted(later, key=lambda c: (c.start_time, c.id))
    late = timedelta(minutes=setting_int('tap_late_minutes'))
    try:
        rolled_ids = []
        snap = _snapshot(open_att)
        open_att.check_out_time = close_at
        first = TapEvent(event_key=f'{req.event_key}:roll0', occurred_at=now,
                         source=req.source, actor_id=req.actor_id, outcome='rolled_over',
                         direction='out', tag_id=tag.id, student_id=student.id,
                         class_id=in_event.class_id, attendance_id=open_att.id,
                         undo_data=_undo_blob(snap, open_att))
        db.session.add(first)
        db.session.flush()
        rolled_ids.append(first.id)
        for i, cls in enumerate(later[:-1], start=1):
            att, csnap = _upsert_present(student, cls, datetime.combine(day, cls.start_time),
                                         tardy_rule=False)
            att.check_out_time = datetime.combine(day, cls.end_time)
            mid = TapEvent(event_key=f'{req.event_key}:roll{i}', occurred_at=now,
                           source=req.source, actor_id=req.actor_id, outcome='rolled_over',
                           direction='out', tag_id=tag.id, student_id=student.id,
                           class_id=cls.id, attendance_id=att.id, undo_data=_undo_blob(csnap, att))
            db.session.add(mid)
            db.session.flush()
            rolled_ids.append(mid.id)
        final = later[-1]
        if now <= datetime.combine(day, final.end_time) + late:
            return _check_in(req, now, tag, student, final, rolled_from=open_cls,
                             rolled_ids=rolled_ids, retried=retried)
        att, fsnap = _upsert_present(student, final, datetime.combine(day, final.start_time),
                                     tardy_rule=False)
        att.check_out_time = now
    except IntegrityError:
        return _retry_once(req, retried)
    fsnap['rolled_event_ids'] = rolled_ids
    ev = _new_event(req, now, 'checked_out', direction='out', tag_id=tag.id,
                    student_id=student.id, class_id=final.id, attendance_id=att.id,
                    undo_data=_undo_blob(fsnap, att))
    _notify(ev, 'departure')
    return _commit_event(req, TapResult(outcome='checked_out', event=ev, student=student,
                                        dance_class=final, attendance=att,
                                        rolled_from=open_cls))


# ── Staff corrections ───────────────────────────────────────────────

def _row_state(att):
    """The fields a tap or an undo touches, comparable as JSON."""
    return {
        'is_present': att.is_present,
        'is_tardy': bool(att.is_tardy),
        'is_excused': bool(att.is_excused),
        'check_in_time': _iso(att.check_in_time),
        'check_in_method': att.check_in_method,
        'check_out_time': _iso(att.check_out_time),
    }


def _undo_blob(before, att, **extra):
    """Undo data for one event: the row before the tap, plus what the tap left
    it as. Undo checks the row still looks like `after`, so it never rolls back
    a fix a teacher made on the card after the tap."""
    data = dict(before)
    data['after'] = _row_state(att) if att is not None else None
    data.update(extra)
    return json.dumps(data)


def _unchanged_since(att, snap):
    """True when the row is still exactly what this event left it as. Events
    from before 'after' was recorded can't be checked and are allowed."""
    if 'after' not in snap:
        return True
    if snap['after'] is None:
        return att is None
    return att is not None and _row_state(att) == snap['after']


STALE_MSG = ("This dancer's attendance was changed after the tap, so undo would erase "
             "that change. Fix it on the attendance card instead.")


def _restore(att, snap):
    att.is_present = snap['is_present']
    att.is_tardy = snap['is_tardy']
    att.is_excused = snap['is_excused']
    att.check_in_time = datetime.fromisoformat(snap['check_in_time'])
    att.check_in_method = snap['check_in_method']
    att.check_out_time = (datetime.fromisoformat(snap['check_out_time'])
                          if snap['check_out_time'] else None)


def undo_tap(event_id: int, actor_id=None) -> TapEvent:
    """Reverse the dancer's most recent check-in, check-out or reopen today."""
    ev = db.session.get(TapEvent, event_id)
    if ev is None or ev.outcome not in ('checked_in', 'checked_out', 'reopened'):
        raise TapError('Only a check-in or check-out can be undone')
    if ev.undone:
        raise TapError('That tap was already undone')
    latest = _todays_state_events(ev.student_id, datetime.combine(ev.occurred_at.date(),
                                                                  datetime.max.time()))
    if not latest or latest[-1].id != ev.id:
        raise TapError('Only the most recent tap can be undone')
    snap = json.loads(ev.undo_data or '{}')
    att = ev.attendance
    if not _unchanged_since(att, snap):
        raise TapError(STALE_MSG)
    for rolled_id in snap.get('rolled_event_ids') or []:
        rolled = db.session.get(TapEvent, rolled_id)
        if rolled is not None and not rolled.undone and not _unchanged_since(
                rolled.attendance, json.loads(rolled.undo_data or '{}')):
            raise TapError(STALE_MSG)
    if att is not None:
        if snap.get('created'):
            db.session.delete(att)
        elif snap:
            _restore(att, snap)
    for rolled_id in reversed(snap.get('rolled_event_ids') or []):
        rolled = db.session.get(TapEvent, rolled_id)
        if rolled is None or rolled.undone:
            continue
        rsnap = json.loads(rolled.undo_data or '{}')
        if rolled.attendance is not None:
            if rsnap.get('created'):
                db.session.delete(rolled.attendance)
            elif rsnap:
                _restore(rolled.attendance, rsnap)
        rolled.undone = True
    ev.undone = True
    from app.tap import outbox
    outbox.cancel_for_event(ev.id)
    db.session.add(TapEvent(event_key=f'undo:{ev.id}', occurred_at=datetime.now().replace(microsecond=0),
                            source='api', actor_id=actor_id, outcome='undone',
                            tag_id=ev.tag_id, student_id=ev.student_id, class_id=ev.class_id,
                            detail=f'undid event {ev.id} ({ev.outcome})'))
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        raise TapError('That tap was already undone')
    return ev


def reopen_session(event_id: int, actor_id=None, event_key=None) -> TapResult:
    """Put a checked-out dancer back in class (left by mistake, came back)."""
    ev = db.session.get(TapEvent, event_id)
    if ev is None or ev.student_id is None:
        raise TapError('Nothing to reopen')
    target = ev
    if ev.outcome == 'duplicate' or ev.outcome == 'already_out':
        target = next((e for e in reversed(_todays_state_events(
            ev.student_id, datetime.combine(ev.occurred_at.date(), datetime.max.time())))
            if e.direction == 'out'), None)
    if target is None or target.outcome != 'checked_out' or target.undone:
        raise TapError('Only a checked-out dancer can be put back in class')
    latest = _todays_state_events(target.student_id, datetime.combine(
        target.occurred_at.date(), datetime.max.time()))
    if not latest or latest[-1].id != target.id:
        raise TapError('The dancer is already back in a class')
    att = target.attendance
    if att is None:
        raise TapError('The attendance row is gone')
    if not _unchanged_since(att, json.loads(target.undo_data or '{}')):
        raise TapError("This dancer's attendance was changed after they checked out. "
                       'Fix it on the attendance card instead.')
    snap = _snapshot(att)
    att.check_out_time = None
    now = datetime.now().replace(microsecond=0)
    key = event_key or f'reopen:{target.id}'
    new = TapEvent(event_key=key, occurred_at=max(now, target.occurred_at), source='api',
                   actor_id=actor_id, outcome='reopened', direction='in',
                   tag_id=target.tag_id, student_id=target.student_id,
                   class_id=target.class_id, attendance_id=att.id,
                   undo_data=_undo_blob(snap, att))
    db.session.add(new)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        raise TapError('Already reopened')
    return TapResult(outcome='reopened', event=new, student=new.student,
                     dance_class=new.dance_class, attendance=att)
