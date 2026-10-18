"""Tag tokens, chip UIDs, and the tag-to-dancer lookup."""

import hashlib
import re
import secrets

from flask import current_app

from app.models import NfcTag, Setting

# 16 bytes -> 22 url-safe chars, 128 bits. The full URL is ~55 bytes of NDEF,
# well inside NTAG213's 144.
TOKEN_BYTES = 16
_TOKEN_RE = re.compile(r'^[A-Za-z0-9_-]{16,64}$')
_HEX_RE = re.compile(r'^[0-9A-F]+$')

TAG_KINDS = ('fob', 'card', 'other')
TAG_STATUSES = ('pending', 'active', 'lost', 'retired')


def mint_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def valid_token_shape(token) -> bool:
    """Cheap shape check before any lookup, so junk never reaches the DB."""
    return isinstance(token, str) and bool(_TOKEN_RE.match(token))


def normalize_uid(raw):
    """Chip serial as uppercase hex with no separators, or None.

    Web NFC reports `04:a2:1b:...`; the USB reader is configured for
    `04A21B...`. Both normalize to the same string. Only 4, 7 and 10 byte UIDs
    exist on ISO 14443A chips, so anything else is a misconfigured reader and
    is rejected rather than stored as a wrong ID."""
    if raw is None:
        return None
    s = re.sub(r'[\s:\-]', '', str(raw)).upper()
    if not s or not _HEX_RE.match(s):
        return None
    if len(s) not in (8, 14, 20):
        return None
    return s


def legacy_uid(raw):
    """Convert a legacy `Student.rfid_uid` to a normalized UID.

    The old MFRC522 code stored SimpleMFRC522.read_id(): the 4 UID bytes plus
    the BCC check byte packed into one decimal integer. When the last byte is
    the XOR of the first four it is a BCC and is dropped. Values that are
    already hex pass through normalize_uid."""
    if raw is None:
        return None
    s = str(raw).strip()
    if s.isdigit():
        n = int(s)
        if n >= 1 << 40:
            return None
        h = f'{n:010X}'
        b = bytes.fromhex(h)
        if b[0] ^ b[1] ^ b[2] ^ b[3] == b[4]:
            return h[:8]
        if n < 1 << 32:
            return f'{n:08X}'
        return None
    return normalize_uid(s)


def tag_base_url() -> str:
    """Host baked into every tag. Once tags are locked this can never change,
    so it is a deliberate Setting (`tap_base_url`) that falls back to the
    portal address."""
    base = Setting.get('tap_base_url', '') or current_app.config.get('PORTAL_URL', '')
    return base.rstrip('/')


def tag_url(token: str) -> str:
    return f'{tag_base_url()}/t/{token}'


def find_tag(token=None, uid=None):
    """The NfcTag for a token or a chip UID, or None. Status is not checked
    here: callers decide what a lost or retired tag means."""
    if token:
        if not valid_token_shape(token):
            return None
        return NfcTag.query.filter_by(token_hash=hash_token(token)).first()
    norm = normalize_uid(uid)
    if norm:
        return NfcTag.query.filter_by(uid=norm).first()
    return None


def label_for(tag_id: int) -> str:
    """Non-identifying label staff can write on the tag box: T-0014. It is
    derived from the row id after flush, so two staff enrolling at once can't
    collide. Never the dancer's name."""
    return f'T-{tag_id:04d}'
