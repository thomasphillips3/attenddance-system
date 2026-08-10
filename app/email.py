"""Email sending for AttenDANCE — receipts, reminders, and message blasts.

Thin wrapper over smtplib so the rest of the app has one place to send mail.
All sends honor MAIL_REPLY_TO so parent replies go to the studio inbox.
"""

import logging
import smtplib
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from flask import current_app

logger = logging.getLogger(__name__)


def is_configured() -> bool:
    """True if an SMTP server is configured."""
    return bool(current_app.config.get("MAIL_SERVER"))


def _build_attachment_parts(attachments):
    """Base64-encode each attachment ONCE into a reusable MIME part.

    A blast sends one message per recipient (so parents never see each other's
    addresses), and encoding a 5MB flyer per recipient would burn CPU linearly
    in the recipient count. The parts are immutable once built, so the same
    objects get attached to every per-recipient message."""
    parts = []
    for att in attachments or []:
        raw = att.get("data")
        if not raw:
            continue
        filename = att.get("filename") or "attachment"
        ctype = att.get("content_type") or "application/octet-stream"
        maintype, _, subtype = ctype.partition("/")
        part = MIMEBase(maintype or "application", subtype or "octet-stream")
        part.set_payload(raw)
        encoders.encode_base64(part)
        # filename is sanitized at upload (secure_filename), so it carries no
        # quotes or newlines that could break out of the header.
        part.add_header("Content-Disposition", "attachment", filename=filename)
        parts.append(part)
    return parts


def send_email(to, subject: str, body: str, attachments=None) -> int:
    """Send a plaintext email to one or more recipients.

    Args:
        to: a single address (str) or an iterable of addresses.
        subject: email subject.
        body: plaintext body.
        attachments: optional list of dicts with `filename`, `content_type`,
            and `data` (bytes). Each is attached to every recipient's copy.

    Returns:
        Number of recipients the message was sent to.

    Raises:
        RuntimeError if SMTP is not configured.
        smtplib.SMTPException / OSError on send failure.
    """
    if isinstance(to, str):
        recipients = {to}
    else:
        recipients = {addr for addr in to if addr}
    if not recipients:
        return 0

    mail_server = current_app.config.get("MAIL_SERVER")
    if not mail_server:
        raise RuntimeError("SMTP not configured (MAIL_SERVER unset)")

    port = current_app.config.get("MAIL_PORT", 587)
    username = current_app.config.get("MAIL_USERNAME")
    password = current_app.config.get("MAIL_PASSWORD")
    reply_to = current_app.config.get("MAIL_REPLY_TO")
    sender = username or "noreply@attenddance.local"

    # Timeout is load-bearing: forgot-password sends inline in the request, so a
    # hung SMTP server would otherwise hold the worker for the full 120s gunicorn
    # timeout (and background send threads would hang forever).
    smtp = smtplib.SMTP(mail_server, port, timeout=20)
    try:
        if current_app.config.get("MAIL_USE_TLS", True):
            smtp.starttls()
        if username and password:
            smtp.login(username, password)
        # Strip CR/LF from header values: Python's email lib already raises on an
        # embedded newline (blocking header injection), but sanitizing means a
        # CRLF-bearing address (which can slip past registration's format check)
        # degrades cleanly instead of raising mid-send.
        def _hdr(v):
            return "".join(ch for ch in str(v) if ch not in "\r\n\x00") if v else v

        safe_subject = _hdr(subject)
        safe_sender = _hdr(sender)
        safe_reply = _hdr(reply_to) if reply_to else None
        attachment_parts = _build_attachment_parts(attachments)
        for addr in recipients:
            safe_addr = _hdr(addr)
            m = MIMEMultipart()
            m["From"] = safe_sender
            m["To"] = safe_addr
            m["Subject"] = safe_subject
            if safe_reply:
                m["Reply-To"] = safe_reply
            m.attach(MIMEText(body, "plain"))
            for part in attachment_parts:
                m.attach(part)
            smtp.sendmail(safe_sender, safe_addr, m.as_string())
    finally:
        try:
            smtp.quit()
        except Exception:
            pass
    return len(recipients)
