"""SMS sending via Twilio's REST API (no SDK dependency — uses requests).

Credentials live in Settings (Account SID + Auth Token encrypted, From number),
so the studio configures them in the UI. Gracefully no-ops when unconfigured.
"""

import logging

import requests
from flask import current_app

logger = logging.getLogger(__name__)

TWILIO_API = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"


def _creds():
    from app.crypto import decrypt
    from app.models import Setting
    sid = decrypt(Setting.get('sms_twilio_sid', ''))
    token = decrypt(Setting.get('sms_twilio_token', ''))
    from_number = Setting.get('sms_from_number', '')
    return sid, token, from_number


def is_configured() -> bool:
    from app.models import Setting
    if not Setting.get_bool('sms_enabled'):
        return False
    sid, token, from_number = _creds()
    return bool(sid and token and from_number)


# Twilio error codes that mean "never send to this number again".
# 21610: the recipient replied STOP. 21614: not a mobile number.
OPTED_OUT_CODES = {21610}


def send_sms_result(to: str, body: str):
    """Send a single SMS. Returns (ok, twilio_error_code_or_None). Never raises.
    Callers that must honor STOP (error 21610) read the code; everyone else
    uses send_sms()."""
    if not to:
        return False, None
    sid, token, from_number = _creds()
    if not (sid and token and from_number):
        return False, None
    try:
        resp = requests.post(
            TWILIO_API.format(sid=sid),
            data={'To': to, 'From': from_number, 'Body': body[:1500]},
            auth=(sid, token),
            timeout=15,
        )
        if resp.status_code in (200, 201):
            return True, None
        code = None
        try:
            code = int(resp.json().get('code'))
        except Exception:  # noqa: BLE001 - a non-JSON error body has no code
            code = None
        logger.error("Twilio send failed (%s, code %s): %s", resp.status_code, code, resp.text[:300])
        return False, code
    except Exception:
        logger.exception("Twilio request error sending to %s", to)
        return False, None


def send_sms(to: str, body: str) -> bool:
    """Send a single SMS. Returns True on success. Never raises."""
    return send_sms_result(to, body)[0]


def normalize_us_phone(raw):
    """E.164 for a US number typed any common way, or None. Twilio wants
    +1XXXXXXXXXX; registration forms hold "(248) 555-0100"."""
    if not raw:
        return None
    s = str(raw).strip()
    digits = ''.join(ch for ch in s if ch.isdigit())
    if s.startswith('+') and 8 <= len(digits) <= 15:
        return '+' + digits
    if len(digits) == 10:
        return '+1' + digits
    if len(digits) == 11 and digits[0] == '1':
        return '+' + digits
    return None


def test_connection():
    """Verify Twilio credentials by fetching the account. Returns (ok, message)."""
    sid, token, from_number = _creds()
    if not (sid and token):
        return False, "Account SID and Auth Token are required."
    if not from_number:
        return False, "A 'From' number is required."
    try:
        resp = requests.get(
            f"https://api.twilio.com/2010-04-01/Accounts/{sid}.json",
            auth=(sid, token), timeout=15,
        )
        if resp.status_code == 200:
            name = resp.json().get('friendly_name', sid)
            return True, f"Connected to Twilio account '{name}'."
        return False, f"Twilio rejected the credentials ({resp.status_code})."
    except Exception as e:  # noqa: BLE001
        return False, f"Connection failed: {e}"
