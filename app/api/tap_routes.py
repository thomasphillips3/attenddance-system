"""Tap-In API: record a tap, undo it, put a dancer back in class.

Registered on the existing `api` blueprint on purpose: the default-deny parent
write guard (_restrict_parent_writes) runs before these, so a parent session
can never record a tap. These endpoints answer an expired session with JSON
401 instead of Flask-Login's redirect, so the kiosk's fetch() sees a real
error rather than a login page it would mistake for success.
"""

import logging
import re

from flask import jsonify, request, url_for
from flask_login import current_user

from app.api import bp
from app.tap.service import (
    SOURCES, TapError, TapRequest, process_tap, reopen_session, result_from_event, undo_tap,
)

logger = logging.getLogger(__name__)

_EVENT_KEY_RE = re.compile(r'^[A-Za-z0-9:_-]{8,64}$')


def _auth_error():
    if not current_user.is_authenticated:
        return jsonify({'error': 'Your sign-in expired. Sign in again.',
                        'login_url': url_for('auth.login')}), 401
    if not current_user.is_staff:
        return jsonify({'error': 'Staff access required'}), 403
    return None


def tap_response(result, status=200):
    from app.tap import outbox
    from app.tap.flags import flags_for
    outbox.kick()
    body = result.to_dict()
    body['flags'] = flags_for(result.student, include_money=current_user.is_admin)
    st = result.student
    body['photo_url'] = (f'/api/students/{st.id}/photo?v={int(st.photo_updated_at.timestamp())}'
                         if st is not None and st.photo_updated_at else None)
    return jsonify(body), status


@bp.route('/tap', methods=['POST'])
def record_tap():
    err = _auth_error()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    event_key = data.get('event_key')
    if not isinstance(event_key, str) or not _EVENT_KEY_RE.match(event_key):
        return jsonify({'error': 'event_key is required'}), 400
    token = data.get('token') if isinstance(data.get('token'), str) else None
    uid = data.get('uid') if isinstance(data.get('uid'), (str, int)) else None
    if not token and uid is None:
        return jsonify({'error': 'token or uid is required'}), 400
    class_id = data.get('class_id')
    if class_id is not None:
        try:
            class_id = int(class_id)
        except (TypeError, ValueError):
            return jsonify({'error': 'class_id must be a number'}), 400
    source = data.get('source') if data.get('source') in SOURCES else 'api'
    req = TapRequest(event_key=event_key, token=token,
                     uid=str(uid) if uid is not None else None,
                     source=source, actor_id=current_user.id, class_id=class_id)
    try:
        result = process_tap(req)
    except TapError as e:
        return jsonify({'error': str(e)}), 400
    return tap_response(result, 200 if result.replayed else 201)


@bp.route('/tap/<int:event_id>/undo', methods=['POST'])
def undo_tap_event(event_id):
    err = _auth_error()
    if err:
        return err
    try:
        ev = undo_tap(event_id, actor_id=current_user.id)
    except TapError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify({'message': 'Undone', 'event_id': ev.id})


@bp.route('/tap/<int:event_id>/reopen', methods=['POST'])
def reopen_tap_session(event_id):
    err = _auth_error()
    if err:
        return err
    try:
        result = reopen_session(event_id, actor_id=current_user.id)
    except TapError as e:
        return jsonify({'error': str(e)}), 400
    return tap_response(result)


@bp.route('/tap/<int:event_id>/pick', methods=['POST'])
def pick_class_for_tap(event_id):
    """Staff chose a class on a 'pick a class' (or 'already out') result. The
    tag comes from the original event, so the result page never needs the
    token again."""
    err = _auth_error()
    if err:
        return err
    from app import db
    from app.models import TapEvent
    ev = db.session.get(TapEvent, event_id)
    if ev is None or ev.tag is None:
        return jsonify({'error': 'Not found'}), 404
    data = request.get_json(silent=True) or {}
    event_key = data.get('event_key')
    if not isinstance(event_key, str) or not _EVENT_KEY_RE.match(event_key):
        return jsonify({'error': 'event_key is required'}), 400
    try:
        class_id = int(data.get('class_id'))
    except (TypeError, ValueError):
        return jsonify({'error': 'class_id is required'}), 400
    req = TapRequest(event_key=event_key, uid=None, source=ev.source,
                     actor_id=current_user.id, class_id=class_id)
    req.tag_id = ev.tag_id
    try:
        result = process_tap(req)
    except TapError as e:
        return jsonify({'error': str(e)}), 400
    return tap_response(result, 200 if result.replayed else 201)


@bp.route('/tap/<int:event_id>', methods=['GET'])
def get_tap_event(event_id):
    err = _auth_error()
    if err:
        return err
    from app import db
    from app.models import TapEvent
    ev = db.session.get(TapEvent, event_id)
    if ev is None:
        return jsonify({'error': 'Not found'}), 404
    return tap_response(result_from_event(ev, replayed=True))


# ── Tag management (enroll, verify, lose, replace, lock) ────────────
#
# Enrollment on an Android phone: read the blank tag (serial + any URL already
# on it) -> /tags/check -> mint a pending tag -> write its URL -> read it back
# -> /tags/<id>/activate with the serial and the URL read back. On an iPhone
# the URL is written with a tag-writer app and the tag is activated by tapping
# it: the /t/<token> page offers "finish setup" for a pending tag.

def _tag_dict(tag):
    return {
        'id': tag.id,
        'label': tag.label,
        'kind': tag.kind,
        'status': tag.status,
        'locked': tag.locked,
        'source': tag.source,
        'has_token': tag.token_hash is not None,
        'uid': tag.uid,
        'student_id': tag.student_id,
        'student_name': tag.student.full_name if tag.student else None,
        'created_at': tag.created_at.isoformat() + 'Z' if tag.created_at else None,
        'activated_at': tag.activated_at.isoformat() + 'Z' if tag.activated_at else None,
        'deactivated_at': tag.deactivated_at.isoformat() + 'Z' if tag.deactivated_at else None,
    }


def _student_or_404(student_id):
    from app import db
    from app.models import Student
    return db.session.get(Student, student_id)


def _uid_conflict(uid, student_id, ignore_tag_id=None):
    """Error message if this chip is already somebody else's active tag."""
    from app.models import NfcTag
    other = NfcTag.query.filter_by(uid=uid).first()
    if other is None or other.id == ignore_tag_id:
        return None
    if other.status == 'active' and other.student_id != student_id:
        who = other.student.first_name if other.student else 'another dancer'
        return f'This tag is already set up for {who} ({other.label}). Mark it lost or retired there first.'
    if other.status == 'active' and other.student_id == student_id:
        return f'This tag is already set up for this dancer ({other.label}).'
    return None


def _release_uid(uid, keep_tag_id):
    """A lost/retired/pending row still holding this chip serial gives it up,
    so a found or recycled tag can be enrolled again. The old row also loses
    its token and is retired: the chip may still carry the old URL, and that
    row must never be reactivated to check its old dancer in by phone."""
    from app.models import NfcTag
    for other in NfcTag.query.filter(NfcTag.uid == uid, NfcTag.id != keep_tag_id).all():
        if other.status != 'active':
            other.uid = None
            other.token_hash = None
            if other.status != 'lost':
                other.status = 'retired'
            other.deactivated_at = other.deactivated_at or _utcnow()


def _audit(action, detail):
    from app.models import AuditLog
    AuditLog.record(current_user.id, action, detail)


@bp.route('/students/<int:student_id>/tags', methods=['GET'])
def list_student_tags(student_id):
    err = _auth_error()
    if err:
        return err
    from app.models import NfcTag
    student = _student_or_404(student_id)
    if student is None:
        return jsonify({'error': 'Student not found'}), 404
    from app.models import Setting
    tags = (NfcTag.query.filter_by(student_id=student_id)
            .order_by(NfcTag.status != 'active', NfcTag.id.desc()).all())
    return jsonify({'tags': [_tag_dict(t) for t in tags],
                    'lock_ready': bool(Setting.get('tap_base_url', ''))})


@bp.route('/tags/check', methods=['POST'])
def check_tag():
    """Before writing: is this physical tag free to enroll for this dancer?"""
    err = _auth_error()
    if err:
        return err
    from app.tap.credentials import find_tag, normalize_uid
    data = request.get_json(silent=True) or {}
    try:
        student_id = int(data.get('student_id'))
    except (TypeError, ValueError):
        return jsonify({'error': 'student_id is required'}), 400
    uid = normalize_uid(data.get('uid')) if data.get('uid') else None
    if data.get('uid') and uid is None:
        return jsonify({'ok': False, 'error': "This tag's serial number can't be read. "
                                              'Use an NTAG213 tag.'}), 200
    if uid:
        msg = _uid_conflict(uid, student_id)
        if msg:
            return jsonify({'ok': False, 'error': msg}), 200
    url = data.get('url') if isinstance(data.get('url'), str) else ''
    token = url.rstrip('/').rsplit('/t/', 1)[-1] if '/t/' in url else None
    if token:
        existing = find_tag(token=token)
        if existing and existing.status == 'active' and existing.student_id != student_id:
            who = existing.student.first_name if existing.student else 'another dancer'
            return jsonify({'ok': False,
                            'error': f'This tag is already set up for {who} ({existing.label}).'}), 200
    return jsonify({'ok': True, 'uid': uid})


@bp.route('/students/<int:student_id>/tags', methods=['POST'])
def mint_student_tag(student_id):
    """Create a pending tag and return its URL to write. The token is shown
    this once; only its hash is kept."""
    err = _auth_error()
    if err:
        return err
    from app import db
    from app.models import NfcTag
    from app.tap.credentials import (
        TAG_KINDS, hash_token, label_for, mint_token, normalize_uid, tag_url,
    )
    student = _student_or_404(student_id)
    if student is None:
        return jsonify({'error': 'Student not found'}), 404
    if not student.is_active:
        return jsonify({'error': 'This dancer is inactive'}), 400
    data = request.get_json(silent=True) or {}
    kind = data.get('kind') if data.get('kind') in TAG_KINDS else 'fob'
    uid = None
    if data.get('uid'):
        uid = normalize_uid(data.get('uid'))
        if uid is None:
            return jsonify({'error': "That serial number isn't valid"}), 400
        msg = _uid_conflict(uid, student_id)
        if msg:
            return jsonify({'error': msg}), 409
    token = mint_token()
    tag = NfcTag(student_id=student_id, token_hash=hash_token(token), kind=kind,
                 status='pending', created_by=current_user.id)
    db.session.add(tag)
    db.session.flush()
    tag.label = label_for(tag.id)
    if uid:
        _release_uid(uid, tag.id)
        db.session.flush()
        tag.uid = uid
    _audit('tag.mint', f'{tag.label} ({kind}) for {student.full_name}')
    db.session.commit()
    return jsonify({**_tag_dict(tag), 'url': tag_url(token)}), 201


@bp.route('/students/<int:student_id>/tags/uid', methods=['POST'])
def enroll_uid_tag(student_id):
    """Enroll a tag by chip serial only (USB reader enrollment). It works on
    the USB station; it has no URL, so phones can't tap it."""
    err = _auth_error()
    if err:
        return err
    from app import db
    from app.models import NfcTag
    from app.tap.credentials import TAG_KINDS, label_for, normalize_uid
    student = _student_or_404(student_id)
    if student is None:
        return jsonify({'error': 'Student not found'}), 404
    data = request.get_json(silent=True) or {}
    uid = normalize_uid(data.get('uid'))
    if uid is None:
        return jsonify({'error': 'Scan the tag on the USB reader. The serial must be 8 or 14 '
                                 'hex characters.'}), 400
    msg = _uid_conflict(uid, student_id)
    if msg:
        return jsonify({'error': msg}), 409
    kind = data.get('kind') if data.get('kind') in TAG_KINDS else 'card'
    tag = NfcTag(student_id=student_id, kind=kind, status='active',
                 created_by=current_user.id, activated_at=_utcnow())
    db.session.add(tag)
    db.session.flush()
    tag.label = label_for(tag.id)
    _release_uid(uid, tag.id)
    db.session.flush()
    tag.uid = uid
    _audit('tag.enroll_uid', f'{tag.label} for {student.full_name}')
    db.session.commit()
    return jsonify(_tag_dict(tag)), 201


def _utcnow():
    from datetime import datetime
    return datetime.utcnow()


def _get_tag(tag_id):
    from app import db
    from app.models import NfcTag
    return db.session.get(NfcTag, tag_id)


@bp.route('/tags/<int:tag_id>/activate', methods=['POST'])
def activate_tag(tag_id):
    """Finish enrollment. With `url` (read back from the chip after writing)
    the token must match this tag, which proves the write landed. Without it
    the caller is the /t/<token> page, which already proved the token."""
    err = _auth_error()
    if err:
        return err
    from app import db
    from app.tap.credentials import hash_token, normalize_uid
    tag = _get_tag(tag_id)
    if tag is None:
        return jsonify({'error': 'Tag not found'}), 404
    if tag.status not in ('pending', 'active'):
        return jsonify({'error': f'This tag is {tag.status}. Set up a new tag instead.'}), 400
    if tag.student is None or not tag.student.is_active:
        return jsonify({'error': 'This tag has no active dancer'}), 400
    data = request.get_json(silent=True) or {}
    url = data.get('url') if isinstance(data.get('url'), str) else ''
    token = data.get('token') if isinstance(data.get('token'), str) else None
    if url:
        token = url.rstrip('/').rsplit('/t/', 1)[-1] if '/t/' in url else ''
    if not token or tag.token_hash != hash_token(token):
        return jsonify({'error': "The tag didn't read back correctly. Hold it still and try again."}), 400
    if data.get('uid'):
        uid = normalize_uid(data.get('uid'))
        if uid is None:
            return jsonify({'error': "This tag's serial number can't be read"}), 400
        msg = _uid_conflict(uid, tag.student_id, ignore_tag_id=tag.id)
        if msg:
            return jsonify({'error': msg}), 409
        _release_uid(uid, tag.id)
        db.session.flush()
        tag.uid = uid
    was = tag.status
    tag.status = 'active'
    tag.activated_at = tag.activated_at or _utcnow()
    tag.deactivated_at = None
    if was != 'active':
        _audit('tag.activate', f'{tag.label} for {tag.student.full_name}')
    db.session.commit()
    return jsonify(_tag_dict(tag))


@bp.route('/tags/<int:tag_id>/status', methods=['POST'])
def set_tag_status(tag_id):
    """Mark a tag lost or retired, or reactivate a found one."""
    err = _auth_error()
    if err:
        return err
    from app import db
    tag = _get_tag(tag_id)
    if tag is None:
        return jsonify({'error': 'Tag not found'}), 404
    data = request.get_json(silent=True) or {}
    status = data.get('status')
    if status not in ('lost', 'retired', 'active'):
        return jsonify({'error': "status must be 'lost', 'retired' or 'active'"}), 400
    if status == 'active':
        if tag.status not in ('lost', 'retired'):
            return jsonify({'error': 'Only a lost or retired tag can be reactivated'}), 400
        if tag.student is None or not tag.student.is_active:
            return jsonify({'error': 'This tag has no active dancer'}), 400
        if tag.token_hash is None and tag.uid is None:
            return jsonify({'error': 'This tag has nothing left to identify it'}), 400
        if tag.uid:
            msg = _uid_conflict(tag.uid, tag.student_id, ignore_tag_id=tag.id)
            if msg:
                return jsonify({'error': msg}), 409
        tag.deactivated_at = None
    else:
        tag.deactivated_at = _utcnow()
    _audit(f'tag.{status}', f'{tag.label} ({tag.student.full_name if tag.student else "no dancer"})')
    tag.status = status
    db.session.commit()
    return jsonify(_tag_dict(tag))


@bp.route('/tags/<int:tag_id>/replace', methods=['POST'])
def replace_tag(tag_id):
    """Lost tag -> mark it lost and mint the replacement in one step."""
    err = _auth_error()
    if err:
        return err
    from app import db
    from app.models import NfcTag
    from app.tap.credentials import hash_token, label_for, mint_token, tag_url
    old = _get_tag(tag_id)
    if old is None or old.student is None:
        return jsonify({'error': 'Tag not found'}), 404
    if not old.student.is_active:
        return jsonify({'error': 'This dancer is inactive'}), 400
    if old.status in ('active', 'pending'):
        old.status = 'lost'
        old.deactivated_at = _utcnow()
    data = request.get_json(silent=True) or {}
    kind = data.get('kind') if data.get('kind') in ('fob', 'card', 'other') else old.kind
    token = mint_token()
    new = NfcTag(student_id=old.student_id, token_hash=hash_token(token), kind=kind,
                 status='pending', created_by=current_user.id)
    db.session.add(new)
    db.session.flush()
    new.label = label_for(new.id)
    _audit('tag.replace', f'{old.label} -> {new.label} for {old.student.full_name}')
    db.session.commit()
    return jsonify({'old': _tag_dict(old), 'new': {**_tag_dict(new), 'url': tag_url(token)}}), 201


@bp.route('/tags/<int:tag_id>', methods=['DELETE'])
def delete_pending_tag(tag_id):
    """Throw away an abandoned setup. Only a pending tag that never checked
    anyone in can be deleted; anything else is marked retired instead."""
    err = _auth_error()
    if err:
        return err
    from app import db
    from app.models import TapEvent
    tag = _get_tag(tag_id)
    if tag is None:
        return jsonify({'error': 'Tag not found'}), 404
    if tag.status != 'pending' or TapEvent.query.filter_by(tag_id=tag.id).first():
        return jsonify({'error': 'Only an unfinished setup can be deleted. Retire the tag instead.'}), 400
    db.session.delete(tag)
    db.session.commit()
    return jsonify({'message': 'Deleted'})


@bp.route('/tags/<int:tag_id>/lock', methods=['POST'])
def lock_tag(tag_id):
    """Record that the chip was made read-only (the phone does the locking).
    Locking is permanent and bakes the hostname into the tag for good, so it
    is refused until an admin has saved the permanent tag address."""
    err = _auth_error()
    if err:
        return err
    from app import db
    from app.models import Setting
    tag = _get_tag(tag_id)
    if tag is None:
        return jsonify({'error': 'Tag not found'}), 404
    if not Setting.get('tap_base_url', ''):
        return jsonify({'error': 'Save the permanent tag address in Tap-In settings before '
                                 'locking any tag. Locked tags can never be rewritten.'}), 400
    if tag.status != 'active' or tag.token_hash is None:
        return jsonify({'error': 'Only an active tag with a link can be locked'}), 400
    tag.locked = True
    _audit('tag.lock', f'{tag.label}')
    db.session.commit()
    return jsonify(_tag_dict(tag))


@bp.route('/tags/missing', methods=['GET'])
def students_without_tags():
    """Active dancers with no active tag: who still needs one."""
    err = _auth_error()
    if err:
        return err
    from app import db
    from app.models import NfcTag, Student
    has = {sid for (sid,) in db.session.query(NfcTag.student_id).filter(
        NfcTag.status == 'active', NfcTag.student_id.isnot(None))}
    pending = {sid for (sid,) in db.session.query(NfcTag.student_id).filter(
        NfcTag.status == 'pending', NfcTag.student_id.isnot(None))}
    students = (Student.query.filter_by(is_active=True)
                .order_by(Student.last_name, Student.first_name).all())
    missing = [{'id': s.id, 'name': s.full_name, 'has_pending': s.id in pending}
               for s in students if s.id not in has]
    return jsonify({'students': missing, 'count': len(missing),
                    'total_active': len(students)})


# ── Tap-In settings ─────────────────────────────────────────────────

_INT_SETTINGS = ('tap_early_minutes', 'tap_late_minutes', 'tap_dedup_seconds',
                 'tap_dwell_minutes', 'tap_tardy_grace_minutes')


def _tap_settings():
    from app.models import NfcTag, Setting
    from app.tap.credentials import tag_base_url
    from app.tap.service import rollover_enabled, setting_int
    from app.tap.outbox import setting_flags
    flags = setting_flags()
    return {
        **{k: setting_int(k) for k in _INT_SETTINGS},
        'tap_rollover_enabled': rollover_enabled(),
        'tap_notify_email_enabled': flags['email'],
        'tap_notify_sms_enabled': flags['sms'],
        'tap_notify_paused': flags['paused'],
        'tap_notify_daily_cap': flags['cap'],
        'tap_base_url': Setting.get('tap_base_url', ''),
        'effective_base_url': tag_base_url(),
        'locked_tags': NfcTag.query.filter_by(locked=True).count(),
    }


def _valid_base_url(raw, allow_http):
    from urllib.parse import urlparse
    raw = (raw or '').strip().rstrip('/')
    if not raw:
        return '', None
    u = urlparse(raw)
    if u.scheme not in (('https', 'http') if allow_http else ('https',)) or not u.netloc:
        return None, 'The tag address must start with https:// and name a host'
    if u.path or u.query or u.fragment or u.params:
        return None, 'Use just the address, like https://attenddance.fly.dev (no path)'
    return f'{u.scheme}://{u.netloc.lower()}', None


@bp.route('/tap/settings', methods=['GET'])
def get_tap_settings():
    err = _auth_error()
    if err:
        return err
    return jsonify(_tap_settings())


@bp.route('/tap/settings', methods=['PUT'])
def update_tap_settings():
    err = _auth_error()
    if err:
        return err
    if not current_user.is_admin:
        return jsonify({'error': 'Admin access required'}), 403
    from flask import current_app
    from app import db
    from app.models import AuditLog, NfcTag, Setting
    from app.tap.service import SETTING_LIMITS
    data = request.get_json(silent=True) or {}
    changes = {}
    for key in _INT_SETTINGS:
        if key in data:
            try:
                val = int(data[key])
            except (TypeError, ValueError):
                return jsonify({'error': f'{key} must be a whole number'}), 400
            if not 0 <= val <= SETTING_LIMITS[key]:
                return jsonify({'error': f'{key} must be between 0 and {SETTING_LIMITS[key]}'}), 400
            if key == 'tap_dedup_seconds' and val < 3:
                return jsonify({'error': 'Keep the double-tap window at 3 seconds or more'}), 400
            changes[key] = str(val)
    for flag in ('tap_rollover_enabled', 'tap_notify_email_enabled', 'tap_notify_sms_enabled',
                 'tap_notify_paused'):
        if flag in data:
            changes[flag] = '1' if data[flag] in (True, 1, '1', 'true', 'on') else '0'
    if 'tap_notify_daily_cap' in data:
        try:
            cap = int(data['tap_notify_daily_cap'])
        except (TypeError, ValueError):
            return jsonify({'error': 'The daily cap must be a whole number'}), 400
        if not 1 <= cap <= 2000:
            return jsonify({'error': 'The daily cap must be between 1 and 2000'}), 400
        changes['tap_notify_daily_cap'] = str(cap)
    if 'tap_base_url' in data:
        url, verr = _valid_base_url(data.get('tap_base_url'),
                                    allow_http=current_app.debug or current_app.testing)
        if verr:
            return jsonify({'error': verr}), 400
        current = Setting.get('tap_base_url', '')
        if url != current and NfcTag.query.filter_by(locked=True).count() \
                and not data.get('confirm_break_locked'):
            return jsonify({'error': 'Locked tags still point at the current address. Changing it '
                                     'breaks every locked tag unless that address keeps redirecting '
                                     'here.', 'needs_confirm': True}), 409
        changes['tap_base_url'] = url
    for key, val in changes.items():
        row = Setting.query.filter_by(key=key).first()
        if row:
            row.value = val
        else:
            db.session.add(Setting(key=key, value=val))
    if changes:
        AuditLog.record(current_user.id, 'tap.settings',
                        ', '.join(f'{k}={v}' for k, v in sorted(changes.items())))
    db.session.commit()
    return jsonify(_tap_settings())


# ── Tap station: liveness and offline replay ────────────────────────

MAX_BATCH = 200
# How far back a queued tap may still be replayed. Older than a day, the card
# has almost certainly been fixed by hand; staff mark it there instead.
REPLAY_MAX_AGE_HOURS = 24
REPLAY_FUTURE_SLACK_SECONDS = 120


@bp.route('/tap/ping', methods=['GET'])
def tap_ping():
    """The station's "am I online and still signed in" check."""
    err = _auth_error()
    if err:
        return err
    from datetime import datetime
    return jsonify({'ok': True, 'user': current_user.first_name,
                    'server_time': datetime.now().replace(microsecond=0).isoformat()})


def replay_time(raw):
    """Studio-local naive datetime for a queued tap's epoch-ms timestamp, or
    (None, reason). The station's clock is trusted only within bounds: a
    phone with a wrong clock must not stamp attendance on another day."""
    from datetime import datetime, timedelta
    try:
        ms = float(raw)
    except (TypeError, ValueError):
        return None, 'missing time'
    try:
        when = datetime.fromtimestamp(ms / 1000.0).replace(microsecond=0)
    except (OverflowError, OSError, ValueError):
        return None, 'bad time'
    now = datetime.now().replace(microsecond=0)
    if when > now + timedelta(seconds=REPLAY_FUTURE_SLACK_SECONDS):
        return now, None
    if when < now - timedelta(hours=REPLAY_MAX_AGE_HOURS):
        return None, 'older than a day; mark it on the attendance card'
    return min(when, now), None


@bp.route('/tap/batch', methods=['POST'])
def record_tap_batch():
    """Replay taps a station queued while offline, in the order they happened.
    Each is idempotent on its event_key, so a retried batch never doubles."""
    err = _auth_error()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    taps = data.get('taps')
    if not isinstance(taps, list):
        return jsonify({'error': 'taps must be a list'}), 400
    if len(taps) > MAX_BATCH:
        return jsonify({'error': f'Send at most {MAX_BATCH} taps at a time'}), 400
    items = [t for t in taps if isinstance(t, dict)]
    items.sort(key=lambda t: (t.get('occurred_at') if isinstance(t.get('occurred_at'), (int, float)) else 0))
    out = []
    for t in items:
        key = t.get('event_key')
        if not isinstance(key, str) or not _EVENT_KEY_RE.match(key):
            out.append({'event_key': key if isinstance(key, str) else None, 'ok': False,
                        'error': 'bad event_key'})
            continue
        token = t.get('token') if isinstance(t.get('token'), str) else None
        uid = t.get('uid') if isinstance(t.get('uid'), (str, int)) else None
        if not token and uid is None:
            out.append({'event_key': key, 'ok': False, 'error': 'token or uid is required'})
            continue
        when, why = replay_time(t.get('occurred_at'))
        if when is None:
            out.append({'event_key': key, 'ok': False, 'error': why})
            continue
        source = t.get('source') if t.get('source') in SOURCES else 'kiosk'
        try:
            result = process_tap(TapRequest(event_key=key, token=token,
                                            uid=str(uid) if uid is not None else None,
                                            occurred_at=when, source=source,
                                            actor_id=current_user.id))
        except TapError as e:
            out.append({'event_key': key, 'ok': False, 'error': str(e)})
            continue
        body = result.to_dict()
        out.append({'event_key': key, 'ok': True, 'outcome': body['outcome'],
                    'message': body['message'],
                    'first_name': body['student']['first_name'] if body['student'] else None})
    from app.tap import outbox
    outbox.kick()
    return jsonify({'results': out})


# ── Check-in notice preferences ─────────────────────────────────────

SMS_CONSENT_TEXT = ("Text me when my dancer checks in and out of class. Message and data rates "
                    "may apply. About 2 texts per class. Reply STOP to cancel.")


def _notice_dict(student):
    from app.tap.outbox import is_adult, sms_numbers
    return {
        'student_id': student.id,
        'first_name': student.first_name,
        'email_on': not student.tap_email_opt_out,
        'sms_on': bool(student.tap_sms_opt_in),
        'adult': is_adult(student),
        # Last 4 only: enough for a parent to recognize their number.
        'sms_numbers': ['...' + p[-4:] for p in sms_numbers(student)],
    }


@bp.route('/my-notices', methods=['GET'])
def get_my_notices():
    if not current_user.is_authenticated:
        return jsonify({'error': 'Sign in again'}), 401
    if not current_user.is_parent:
        return jsonify({'error': 'Parents only'}), 403
    from app.tap.outbox import setting_flags
    flags = setting_flags()
    from app.sms import normalize_us_phone
    suggested = normalize_us_phone(current_user.phone)
    return jsonify({
        'children': [_notice_dict(c) for c in current_user.get_children()],
        'sms_available': flags['sms'],
        'sms_consent_text': SMS_CONSENT_TEXT,
        # Prefill with the parent's own account phone only (theirs to agree for).
        'suggested_phone': suggested,
    })


CODE_TTL_MINUTES = 10
CODE_MAX_ATTEMPTS = 5
CODES_PER_USER_PER_HOUR = 3
CODES_PER_USER_PER_DAY = 10
# One account can text any single number at most this often, so a parent
# account can't be used to bomb a stranger's phone.
CODES_PER_USER_PER_PHONE_PER_DAY = 3
# A number is only shut off for everyone when several DIFFERENT accounts aim
# codes at it in a day. One account can't lock a parent out of their own
# number by spamming it (its own per-number limit stops that account), and a
# number can't be bombed from many accounts either.
OTHER_ACCOUNTS_PER_PHONE_PER_DAY = 3
CODE_RETENTION_DAYS = 30
_code_sender = None   # tests swap this; default is sms.send_sms_result


def _code_hash(user_id, phone, code):
    import hashlib
    from flask import current_app
    raw = f"{current_app.config['SECRET_KEY']}:{user_id}:{phone}:{code}"
    return hashlib.sha256(raw.encode()).hexdigest()


@bp.route('/my-notices/code', methods=['POST'])
def send_my_notice_code():
    """Text a 6-digit code to the number a parent wants texts at. Texts only
    turn on once that code comes back, so nobody can sign a stranger up.
    Rate-limited per account and per number."""
    if not current_user.is_authenticated:
        return jsonify({'error': 'Sign in again'}), 401
    if not current_user.is_parent:
        return jsonify({'error': 'Parents only'}), 403
    import secrets
    from datetime import datetime, timedelta
    from app import db
    from app import sms as sms_service
    from app.models import SmsVerification
    from app.tap.outbox import setting_flags
    if not setting_flags()['sms'] or not sms_service.is_configured():
        return jsonify({'error': "The studio hasn't turned on text notices yet"}), 400
    data = request.get_json(silent=True) or {}
    phone = sms_service.normalize_us_phone(data.get('phone'))
    if phone is None:
        return jsonify({'error': 'Enter the mobile number to text, like 248-555-0100'}), 400
    now = datetime.utcnow()
    # Old codes are only an abuse-control record. Drop them after a month.
    SmsVerification.query.filter(
        SmsVerification.created_at < now - timedelta(days=CODE_RETENTION_DAYS)).delete(
        synchronize_session=False)
    mine = SmsVerification.query.filter(SmsVerification.user_id == current_user.id)
    last_hour = mine.filter(SmsVerification.created_at > now - timedelta(hours=1)).count()
    last_day = mine.filter(SmsVerification.created_at > now - timedelta(days=1)).count()
    this_phone = mine.filter(SmsVerification.phone == phone,
                             SmsVerification.created_at > now - timedelta(days=1)).count()
    other_accounts = (db.session.query(SmsVerification.user_id).filter(
        SmsVerification.phone == phone,
        SmsVerification.user_id != current_user.id,
        SmsVerification.created_at > now - timedelta(days=1)).distinct().count())
    if (last_hour >= CODES_PER_USER_PER_HOUR or last_day >= CODES_PER_USER_PER_DAY
            or this_phone >= CODES_PER_USER_PER_PHONE_PER_DAY
            or other_accounts >= OTHER_ACCOUNTS_PER_PHONE_PER_DAY):
        db.session.commit()
        return jsonify({'error': 'Too many codes. Try again later.'}), 429
    code = f'{secrets.randbelow(10 ** 6):06d}'
    db.session.add(SmsVerification(user_id=current_user.id, phone=phone,
                                   code_hash=_code_hash(current_user.id, phone, code),
                                   expires_at=now + timedelta(minutes=CODE_TTL_MINUTES)))
    db.session.commit()
    sender = _code_sender or sms_service.send_sms_result
    ok, _ = sender(phone, f'LSO Dance code: {code}. Enter it in the parent portal to get '
                          'check-in texts. Reply STOP to opt out.')
    if not ok:
        return jsonify({'error': "Couldn't text that number. Check it and try again."}), 400
    return jsonify({'sent': True, 'phone_last4': phone[-4:]})


def _check_code(phone, code):
    """True when `code` is the live code texted to `phone` for this parent.

    The guess is counted with one conditional UPDATE and committed BEFORE the
    comparison, so parallel requests can't each read attempts=0 and get more
    than CODE_MAX_ATTEMPTS guesses. Marking the code used is conditional too,
    so it can only be spent once."""
    import hmac
    from datetime import datetime
    from app import db
    from app.models import SmsVerification
    if not isinstance(code, str) or not code.strip().isdigit():
        return False
    v = (SmsVerification.query.filter_by(user_id=current_user.id, phone=phone, used=False)
         .order_by(SmsVerification.id.desc()).first())
    if v is None or v.expires_at < datetime.utcnow():
        return False
    claimed = (SmsVerification.query
               .filter(SmsVerification.id == v.id, SmsVerification.used.is_(False),
                       SmsVerification.attempts < CODE_MAX_ATTEMPTS)
               .update({SmsVerification.attempts: SmsVerification.attempts + 1},
                       synchronize_session=False))
    db.session.commit()
    if not claimed:
        return False
    if not hmac.compare_digest(v.code_hash, _code_hash(current_user.id, phone, code.strip())):
        return False
    spent = (SmsVerification.query
             .filter(SmsVerification.id == v.id, SmsVerification.used.is_(False))
             .update({SmsVerification.used: True}, synchronize_session=False))
    return bool(spent)


@bp.route('/my-notices', methods=['POST'])
def update_my_notices():
    """A parent turns check-in notices on or off for their whole household.
    SMS needs the explicit consent box; who agreed and when is recorded."""
    if not current_user.is_authenticated:
        return jsonify({'error': 'Sign in again'}), 401
    if not current_user.is_parent:
        return jsonify({'error': 'Parents only'}), 403
    from datetime import datetime
    from app import db
    from app.models import AuditLog
    from app.sms import normalize_us_phone
    from app.tap.outbox import setting_flags
    data = request.get_json(silent=True) or {}
    children = current_user.get_children()
    if 'email' in data:
        for c in children:
            c.tap_email_opt_out = not bool(data['email'])
    if 'sms' in data:
        want = bool(data['sms'])
        phone = None
        if want:
            if not setting_flags()['sms']:
                return jsonify({'error': "The studio hasn't turned on text notices yet"}), 400
            if not data.get('consent'):
                return jsonify({'error': 'Check the box to agree to receive texts'}), 400
            # The parent types the number that agrees. Texts go there and
            # nowhere else: another guardian's number needs their own consent.
            phone = normalize_us_phone(data.get('phone'))
            if phone is None:
                return jsonify({'error': 'Enter the mobile number to text, like 248-555-0100'}), 400
            if not _check_code(phone, data.get('code')):
                db.session.commit()   # keep the attempt count
                return jsonify({'error': "That code didn't match. Check the text or send a new code."}), 400
        for c in children:
            if want:
                c.tap_sms_opt_in_at = datetime.utcnow()
                c.tap_sms_opt_in_by = current_user.id
                c.tap_sms_phone = phone
            c.tap_sms_opt_in = want
    AuditLog.record(current_user.id, 'tap.notices',
                    f"email={data.get('email', '-')} sms={data.get('sms', '-')}")
    db.session.commit()
    return jsonify({'children': [_notice_dict(c) for c in children]})


@bp.route('/students/<int:student_id>/tap-notices', methods=['PUT'])
def staff_update_notices(student_id):
    """Staff can switch a household's notices off (a parent asked by phone or
    replied to an email) or email back on. Staff can turn SMS off but never on:
    texting needs the parent's own consent."""
    err = _auth_error()
    if err:
        return err
    from app import db
    from app.models import AuditLog, Student
    student = db.session.get(Student, student_id)
    if student is None:
        return jsonify({'error': 'Student not found'}), 404
    data = request.get_json(silent=True) or {}
    if data.get('sms') is True:
        return jsonify({'error': 'Only a parent can turn on text notices, from the parent portal'}), 400
    members = ([s for s in Student.query.filter_by(family_id=student.family_id).all()]
               if student.family_id else [student])
    for s in members:
        if 'email' in data:
            s.tap_email_opt_out = not bool(data['email'])
        if data.get('sms') is False:
            s.tap_sms_opt_in = False
    AuditLog.record(current_user.id, 'tap.notices.staff',
                    f'{student.full_name} household: {data}')
    db.session.commit()
    return jsonify(_notice_dict(student))


@bp.route('/students/<int:student_id>/tap-notices', methods=['GET'])
def staff_get_notices(student_id):
    err = _auth_error()
    if err:
        return err
    from app import db
    from app.models import Student
    student = db.session.get(Student, student_id)
    if student is None:
        return jsonify({'error': 'Student not found'}), 404
    return jsonify(_notice_dict(student))


# ── Staff-only dancer photo ─────────────────────────────────────────

PHOTO_MAX_BYTES = 300 * 1024
_PHOTO_PREFIXES = {'data:image/jpeg;base64,': 'image/jpeg', 'data:image/png;base64,': 'image/png',
                   'data:image/webp;base64,': 'image/webp'}
_PHOTO_MAGIC = {'image/jpeg': (b'\xff\xd8\xff',), 'image/png': (b'\x89PNG\r\n\x1a\n',),
                'image/webp': (b'RIFF',)}


def _photo_bytes(data_uri):
    """(mimetype, bytes) for a stored data URI, or (None, None)."""
    import base64
    if not data_uri:
        return None, None
    for prefix, mime in _PHOTO_PREFIXES.items():
        if data_uri.startswith(prefix):
            try:
                return mime, base64.b64decode(data_uri[len(prefix):], validate=True)
            except Exception:  # noqa: BLE001 - corrupt stored value
                return None, None
    return None, None


@bp.route('/students/<int:student_id>/photo', methods=['POST'])
def upload_student_photo(student_id):
    """Save the dancer's staff-only photo. The browser sends a small JPEG data
    URI; the bytes are checked against the image type's signature so a
    renamed file can't be stored as a "photo"."""
    err = _auth_error()
    if err:
        return err
    from datetime import datetime
    from app import db
    from app.models import AuditLog, Student
    student = db.session.get(Student, student_id)
    if student is None:
        return jsonify({'error': 'Student not found'}), 404
    data = request.get_json(silent=True) or {}
    uri = data.get('photo') if isinstance(data.get('photo'), str) else ''
    mime, raw = _photo_bytes(uri)
    if mime is None:
        return jsonify({'error': 'Send a JPEG, PNG or WebP photo'}), 400
    if len(raw) > PHOTO_MAX_BYTES:
        return jsonify({'error': 'That photo is too large (300 KB max)'}), 400
    if not any(raw.startswith(m) for m in _PHOTO_MAGIC[mime]):
        return jsonify({'error': "That file isn't a real image"}), 400
    student.photo_data = uri
    student.photo_updated_at = datetime.utcnow()
    AuditLog.record(current_user.id, 'student.photo', f'{student.full_name}: photo saved')
    db.session.commit()
    return jsonify({'message': 'Saved', 'photo_url': f'/api/students/{student_id}/photo'})


@bp.route('/students/<int:student_id>/photo', methods=['DELETE'])
def delete_student_photo(student_id):
    err = _auth_error()
    if err:
        return err
    from app import db
    from app.models import AuditLog, Student
    student = db.session.get(Student, student_id)
    if student is None:
        return jsonify({'error': 'Student not found'}), 404
    student.photo_data = None
    student.photo_updated_at = None
    AuditLog.record(current_user.id, 'student.photo', f'{student.full_name}: photo removed')
    db.session.commit()
    return jsonify({'message': 'Removed'})


@bp.route('/students/<int:student_id>/photo', methods=['GET'])
def get_student_photo(student_id):
    """Staff only, never cached: a tap station must not keep dancers' faces."""
    err = _auth_error()
    if err:
        return err
    from flask import Response
    from app import db
    from app.models import Student
    student = db.session.get(Student, student_id)
    mime, raw = _photo_bytes(student.photo_data if student else None)
    if mime is None:
        return jsonify({'error': 'No photo'}), 404
    resp = Response(raw, mimetype=mime)
    resp.headers['Cache-Control'] = 'private, no-store'
    return resp
