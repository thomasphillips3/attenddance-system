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
    from app.tap.flags import flags_for
    body = result.to_dict()
    body['flags'] = flags_for(result.student, include_money=current_user.is_admin)
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
