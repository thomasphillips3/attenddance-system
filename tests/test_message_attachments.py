"""Message attachment tests for AttenDANCE.

What has to hold for "attach a flyer to a message blast" to be trustworthy:

  1. The old JSON compose path still works (no attachments, no regression).
  2. A multipart compose stores the file and reports it back on the message.
  3. The stored bytes come back byte-identical from the download endpoint, as a
     download (never rendered inline), and only for staff.
  4. Rejected uploads (bad type, too big, too many, too much total) never leave
     a phantom Message row in the studio's history.
  5. The outgoing email actually carries the attachment, decodable and intact.
  6. Listing message history never pulls blob bytes into memory.

Run:  RFID_ENABLED=false python3 tests/test_message_attachments.py
Exit 0 = all green, 1 = failures.
"""
import base64
import io
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("RFID_ENABLED", "false")
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp.name}"

from app import create_app, db  # noqa: E402
from app.models import Message, MessageAttachment, Student, User  # noqa: E402

app = create_app("development")
app.config["TESTING"] = True
app.config["WTF_CSRF_ENABLED"] = False

results = []


def record(name, passed, detail=""):
    results.append((name, passed))
    print(f"[{'PASS' if passed else 'FAIL'}] {name}" + (f" - {detail}" if detail and not passed else ""))


PDF_BYTES = b"%PDF-1.4\n" + b"recital flyer " * 400 + b"\n%%EOF"


def upload(client, files, subject="Recital flyer", rtype="all"):
    data = {"subject": subject, "body": "See the attached flyer.", "recipient_type": rtype}
    data["attachments"] = files
    return client.post("/api/messages", data=data, content_type="multipart/form-data")


with app.app_context():
    admin = User.query.filter_by(role="admin").first()
    if not admin:
        admin = User(username="admin", email="admin@example.test", role="admin",
                     first_name="Admin", last_name="User", is_admin=True)
        db.session.add(admin)
    admin.set_password("pw12345")

    parent = User.query.filter_by(username="parent1").first()
    if not parent:
        parent = User(username="parent1", email="parent1@example.test", role="parent",
                      first_name="Pat", last_name="Parent")
        db.session.add(parent)
    parent.set_password("pw12345")

    if not Student.query.first():
        db.session.add(Student(first_name="Ava", last_name="Dancer",
                               parent_email="ava.parent@example.test"))
    db.session.commit()

client = app.test_client()
r = client.post("/auth/login", data={"username": "admin", "password": "pw12345"},
                follow_redirects=True)
record("admin login for API calls", r.status_code == 200, f"got {r.status_code}")

# ── 1. JSON compose (no attachments) still works ────────────────────
r = client.post("/api/messages", json={"subject": "No files", "body": "b",
                                       "recipient_type": "all"})
record("JSON compose with no attachments still succeeds",
       r.status_code == 201 and r.get_json().get("attachments") == [],
       f"got {r.status_code} {r.get_json()}")

# ── 2. Multipart compose stores the file ────────────────────────────
r = upload(client, [(io.BytesIO(PDF_BYTES), "Spring Recital.pdf")])
body = r.get_json()
record("multipart compose accepted", r.status_code == 201, f"got {r.status_code} {body}")
atts = body.get("attachments", [])
record("response reports the attachment", len(atts) == 1, f"got {atts}")
if atts:
    a = atts[0]
    record("filename is sanitized but recognizable", a["filename"] == "Spring_Recital.pdf",
           f"got {a['filename']}")
    record("content type derived from extension", a["content_type"] == "application/pdf",
           f"got {a['content_type']}")
    record("stored size matches the upload", a["size"] == len(PDF_BYTES),
           f"got {a['size']} want {len(PDF_BYTES)}")

msg_id = body.get("message_id")

# ── 3. Download round-trips the exact bytes, as a download ──────────
if atts:
    dl = client.get(atts[0]["url"])
    record("download returns the original bytes", dl.data == PDF_BYTES,
           f"got {len(dl.data)}b want {len(PDF_BYTES)}b")
    record("download forces attachment disposition",
           'attachment; filename="Spring_Recital.pdf"'
           in dl.headers.get("Content-Disposition", ""),
           f"got {dl.headers.get('Content-Disposition')}")
    record("download sets the stored content type",
           dl.headers.get("Content-Type", "").startswith("application/pdf"),
           f"got {dl.headers.get('Content-Type')}")

    # Attachment id must belong to the message id in the path.
    bad = client.get(f"/api/messages/{msg_id + 999}/attachments/{atts[0]['id']}")
    record("mismatched message id in the path 404s", bad.status_code == 404,
           f"got {bad.status_code}")

# ── History carries the metadata ────────────────────────────────────
hist = client.get("/api/messages").get_json()["messages"]
with_atts = [m for m in hist if m.get("attachments")]
record("history lists the attachment", len(with_atts) == 1
       and with_atts[0]["attachments"][0]["filename"] == "Spring_Recital.pdf",
       f"got {[m.get('attachments') for m in hist]}")

# ── 6. Listing never pulls the blob into memory ─────────────────────
with app.app_context():
    row = MessageAttachment.query.first()
    record("attachment blob is deferred until touched",
           "data" not in row.__dict__, f"loaded keys: {sorted(row.__dict__)}")
    record("touching .data loads the real bytes", row.data == PDF_BYTES,
           f"got {len(row.data or b'')}b")

# ── 4. Rejections, and no phantom Message rows ──────────────────────
with app.app_context():
    before = Message.query.count()

r = upload(client, [(io.BytesIO(b"MZ evil"), "payload.exe")], subject="Bad type")
record("disallowed extension rejected", r.status_code == 400, f"got {r.status_code}")

r = upload(client, [(io.BytesIO(b"<svg onload=alert(1)>"), "x.svg")], subject="Active content")
record("svg rejected (active content)", r.status_code == 400, f"got {r.status_code}")

r = upload(client, [(io.BytesIO(b"x" * (5 * 1024 * 1024 + 1)), "huge.pdf")], subject="Too big")
record("over-5MB file rejected", r.status_code == 400, f"got {r.status_code}")

r = upload(client, [(io.BytesIO(b"a"), f"f{i}.txt") for i in range(6)], subject="Too many")
record("more than 5 files rejected", r.status_code == 400, f"got {r.status_code}")

three_mb = b"y" * (3 * 1024 * 1024)
r = upload(client, [(io.BytesIO(three_mb), f"big{i}.pdf") for i in range(4)],
           subject="Too much total")
record("over-10MB total rejected", r.status_code == 400, f"got {r.status_code}")

r = upload(client, [(io.BytesIO(b""), "empty.pdf")], subject="Empty")
record("empty file rejected", r.status_code == 400, f"got {r.status_code}")

with app.app_context():
    after = Message.query.count()
    record("no phantom Message rows from rejected uploads", after == before,
           f"{before} -> {after}")

# ── 3b. Parents can't download studio attachments ───────────────────
if atts:
    client.get("/auth/logout")
    client.post("/auth/login", data={"username": "parent1", "password": "pw12345"},
                follow_redirects=True)
    r = client.get(atts[0]["url"])
    record("parent cannot download a message attachment", r.status_code == 403,
           f"got {r.status_code}")
    client.get("/auth/logout")
    client.post("/auth/login", data={"username": "admin", "password": "pw12345"},
                follow_redirects=True)

# ── 5. The outgoing email actually carries the file ─────────────────
sent_payloads = []


class FakeSMTP:
    def __init__(self, *a, **kw):
        pass

    def starttls(self):
        pass

    def login(self, *a):
        pass

    def sendmail(self, sender, addr, msg_string):
        sent_payloads.append(msg_string)

    def quit(self):
        pass


with app.app_context():
    import smtplib
    from email import message_from_string

    from app import email as email_service
    from app.api.routes import _send_message_blast

    app.config["MAIL_SERVER"] = "smtp.example.test"
    real_smtp = smtplib.SMTP
    smtplib.SMTP = FakeSMTP
    try:
        email_service.send_email(
            ["a@example.test", "b@example.test"], "Flyer", "See attached",
            attachments=[{"filename": "Spring_Recital.pdf",
                          "content_type": "application/pdf", "data": PDF_BYTES}])
        record("one message built per recipient", len(sent_payloads) == 2,
               f"got {len(sent_payloads)}")
        parts = list(message_from_string(sent_payloads[0]).walk())
        pdf_parts = [p for p in parts if p.get_filename() == "Spring_Recital.pdf"]
        record("MIME carries the attachment part", len(pdf_parts) == 1,
               f"filenames: {[p.get_filename() for p in parts]}")
        if pdf_parts:
            record("attachment decodes back to the original bytes",
                   base64.b64decode(pdf_parts[0].get_payload()) == PDF_BYTES)
            record("attachment part is marked as an attachment",
                   "attachment" in pdf_parts[0].get("Content-Disposition", ""),
                   f"got {pdf_parts[0].get('Content-Disposition')}")
        record("body text still present",
               any(p.get_content_type() == "text/plain" for p in parts))

        # The blast worker must pull the bytes back out of the DB itself.
        sent_payloads.clear()
        _send_message_blast(app, msg_id, ["parent@example.test"], "Flyer", "See attached")
        worker_parts = list(message_from_string(sent_payloads[0]).walk()) if sent_payloads else []
        record("blast worker attaches the stored file",
               any(p.get_filename() == "Spring_Recital.pdf" for p in worker_parts),
               f"filenames: {[p.get_filename() for p in worker_parts]}")
        record("blast worker marks the message sent",
               (Message.query.get(msg_id) or Message()).sent is True)
    finally:
        smtplib.SMTP = real_smtp
        app.config["MAIL_SERVER"] = None

passed = sum(1 for _, p in results if p)
total = len(results)
print("\n" + "=" * 56)
print(f"SUMMARY: {passed}/{total} passed, {total - passed} failed.")
sys.exit(0 if passed == total else 1)
