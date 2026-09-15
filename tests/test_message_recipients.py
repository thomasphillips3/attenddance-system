"""Seeing who a message goes to, and proofing it first.

Carollette: "I can see that 53 ppl got an email, but I also want to see the
list with all the emails. We need to be able to see the emails we send. It
would be nice to send ourselves a test and to get the email everyone else
gets. OR a way to export a list with everyone's emails."

The addresses and the body were already stored on every Message; nothing
showed them. What has to hold now:

  1. A recipient preview resolves the audience with the SAME function the send
     uses, so the list shown can never disagree with the blast. `all` is
     admin-only, same as sending. `?format=csv` downloads it - and with
     recipient_type=all that is the "export everyone's email" report.
  2. A test send delivers the exact email (subject, body, attachments) to the
     sender only. Nothing is stored, nobody else is mailed, the subject is
     prefixed [TEST], and it refuses cleanly when email isn't configured.
  3. "Send a copy to me" adds the sender's address to the real blast and it
     shows up in the stored recipient list like any other.
  4. A sent message's list is downloadable, and the history API still carries
     the body and the addresses the detail panel renders.
  5. The roster export carries the second parent email too.

Run:  RFID_ENABLED=false python3 tests/test_message_recipients.py
Exit 0 = all green, 1 = failures.
"""
import io
import os
import smtplib
import sys
import tempfile
import time
from datetime import time as dtime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("RFID_ENABLED", "false")
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp.name}"

from app import create_app, db  # noqa: E402
from app.models import ClassEnrollment, DanceClass, Message, Student, User  # noqa: E402

app = create_app("development")
app.config["TESTING"] = True
app.config["WTF_CSRF_ENABLED"] = False

results = []


def record(name, passed, detail=""):
    results.append((name, passed))
    print(f"[{'PASS' if passed else 'FAIL'}] {name}" + (f" - {detail}" if detail and not passed else ""))


sent = []   # (to, subject, raw)


class FakeSMTP:
    def __init__(self, *a, **kw):
        pass

    def starttls(self):
        pass

    def login(self, *a):
        pass

    def sendmail(self, sender, addr, msg):
        subj = ""
        for line in msg.splitlines():
            if line.lower().startswith("subject:"):
                subj = line.split(":", 1)[1].strip()
                break
        sent.append((addr, subj, msg))

    def quit(self):
        pass


def wait_for(n, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end and len(sent) < n:
        time.sleep(0.05)
    return len(sent)


with app.app_context():
    admin = User.query.filter_by(role="admin").first()
    if not admin:
        admin = User(username="admin", email="admin@example.test", role="admin",
                     first_name="Admin", last_name="User", is_admin=True)
        db.session.add(admin)
    admin.email = "wilkanda@example.test"
    admin.set_password("pw12345")
    teacher = User(username="teach", email="teach@example.test", role="teacher",
                   first_name="Tee", last_name="Cher")
    teacher.set_password("pw12345")
    db.session.add(teacher)
    db.session.flush()
    cls = DanceClass(name="Recip Class", day_of_week=0, start_time=dtime(17, 0),
                     end_time=dtime(18, 0), instructor_id=admin.id)
    db.session.add(cls)
    db.session.flush()
    fam = []
    for fn, mail, mail2 in (("Ava", "ava.mom@x.com", "ava.dad@x.com"),
                            ("Ben", "ben.mom@x.com", None),
                            ("Cal", "cal.mom@x.com", None)):
        st = Student(first_name=fn, last_name="Recip", parent_email=mail, parent_email_2=mail2)
        db.session.add(st)
        db.session.flush()
        fam.append(st.id)
    db.session.add(ClassEnrollment(student_id=fam[0], class_id=cls.id))
    db.session.add(ClassEnrollment(student_id=fam[1], class_id=cls.id))
    db.session.commit()
    cid, ava_id = cls.id, fam[0]

staff = app.test_client()
staff.post("/auth/login", data={"username": "admin", "password": "pw12345"}, follow_redirects=True)
teach = app.test_client()
teach.post("/auth/login", data={"username": "teach", "password": "pw12345"}, follow_redirects=True)


# ── 1. Recipient preview ────────────────────────────────────────────
r = staff.get(f"/api/messages/recipients?recipient_type=class&recipient_filter={cid}")
d = r.get_json() or {}
record("class preview lists both parents of every enrolled dancer",
       r.status_code == 200 and set(d.get("emails", [])) == {"ava.mom@x.com", "ava.dad@x.com", "ben.mom@x.com"}
       and d.get("count") == 3, f"got {r.status_code} {d}")
r = staff.get("/api/messages/recipients?recipient_type=all")
d = r.get_json() or {}
record("all-parents preview covers every active dancer",
       {"ava.mom@x.com", "ava.dad@x.com", "ben.mom@x.com", "cal.mom@x.com"} <= set(d.get("emails", [])),
       f"got {d}")
record("preview is sorted, case-insensitively",
       d.get("emails") == sorted(d.get("emails", []), key=str.lower))
r = staff.get(f"/api/messages/recipients?recipient_type=individual&recipient_filter={ava_id}")
record("individual preview is that family only",
       set((r.get_json() or {}).get("emails", [])) == {"ava.mom@x.com", "ava.dad@x.com"}, f"got {r.get_json()}")
r = teach.get("/api/messages/recipients?recipient_type=all")
record("a teacher cannot preview the whole studio (same rule as sending)",
       r.status_code == 403, f"got {r.status_code}")
r = teach.get(f"/api/messages/recipients?recipient_type=class&recipient_filter={cid}")
record("a teacher can preview a class", r.status_code == 200, f"got {r.status_code}")
r = staff.get("/api/messages/recipients")
record("missing recipient_type is a 400, not a 500", r.status_code == 400, f"got {r.status_code}")

r = staff.get("/api/messages/recipients?recipient_type=all&format=csv")
body = r.get_data(as_text=True)
record("the export downloads as a CSV",
       r.status_code == 200 and "text/csv" in r.headers.get("Content-Type", "")
       and "attachment" in r.headers.get("Content-Disposition", "")
       and "emails-all-parents-" in r.headers.get("Content-Disposition", ""),
       f"got {r.status_code} {r.headers.get('Content-Type')} {r.headers.get('Content-Disposition')}")
record("one address per line under an Email header",
       body.splitlines()[0].strip() == "Email" and "ava.dad@x.com" in body and "cal.mom@x.com" in body,
       f"got {body[:120]!r}")


# ── 2 + 3. Test send, and copy-me ──────────────────────────────────
real_smtp = smtplib.SMTP
smtplib.SMTP = FakeSMTP
try:
    # Test send refuses when email isn't configured.
    app.config["MAIL_SERVER"] = None
    r = staff.post("/api/messages", data={"subject": "Recital", "body": "Details",
                                          "recipient_type": "all", "test": "1"},
                   content_type="multipart/form-data")
    record("a test send says so when email isn't configured",
           r.status_code == 400 and "not configured" in ((r.get_json() or {}).get("error") or ""),
           f"got {r.status_code} {r.get_json()}")

    app.config["MAIL_SERVER"] = "smtp.example.test"
    with app.app_context():
        before = Message.query.count()
    sent.clear()
    r = staff.post("/api/messages", data={
        "subject": "Recital", "body": "Details inside.", "recipient_type": "all", "test": "1",
        "attachments": [(io.BytesIO(b"%PDF-1.4 flyer"), "flyer.pdf")]},
        content_type="multipart/form-data")
    d = r.get_json() or {}
    record("test send accepted", r.status_code == 200 and d.get("test") is True
           and d.get("sent_to") == "wilkanda@example.test", f"got {r.status_code} {d}")
    wait_for(1)
    record("the test goes to the sender and NOBODY else",
           [a for a, _, _ in sent] == ["wilkanda@example.test"], f"got {[a for a, _, _ in sent]}")
    record("the test subject is marked", any(s.startswith("[TEST] Recital") for _, s, _ in sent),
           f"got {[s for _, s, _ in sent]}")
    record("the test carries the attachment", any('filename="flyer.pdf"' in raw for _, _, raw in sent))
    with app.app_context():
        record("a test send stores nothing in history", Message.query.count() == before,
               f"{before} -> {Message.query.count()}")

    # Real send with copy_me.
    sent.clear()
    r = staff.post("/api/messages", data={"subject": "Real one", "body": "Hi all",
                                          "recipient_type": "class", "recipient_filter": str(cid),
                                          "copy_me": "1"},
                   content_type="multipart/form-data")
    d = r.get_json() or {}
    record("real send with copy-me accepted", r.status_code == 201 and d.get("copied_to") == "wilkanda@example.test",
           f"got {r.status_code} {d}")
    record("the response says the copy is included", "copy to wilkanda@example.test" in (d.get("message") or ""),
           f"got {d.get('message')}")
    wait_for(4)
    got = {a for a, _, _ in sent}
    record("the sender receives the same email the parents get",
           "wilkanda@example.test" in got and {"ava.mom@x.com", "ava.dad@x.com", "ben.mom@x.com"} <= got,
           f"got {sorted(got)}")
    record("the sender's copy is not marked as a test",
           all(not s.startswith("[TEST]") for _, s, _ in sent), f"got {[s for _, s, _ in sent]}")
    mid = d.get("message_id")
    with app.app_context():
        m = Message.query.get(mid)
        record("the copy is stored with the recipients",
               m is not None and "wilkanda@example.test" in (m.recipient_emails or "")
               and m.recipient_count == 4, f"got count={m.recipient_count if m else None}")

    # Without copy_me, the sender is left out.
    sent.clear()
    r = staff.post("/api/messages", data={"subject": "No copy", "body": "Hi",
                                          "recipient_type": "class", "recipient_filter": str(cid)},
                   content_type="multipart/form-data")
    wait_for(3)
    record("without copy-me the sender is not mailed",
           r.status_code == 201 and "wilkanda@example.test" not in {a for a, _, _ in sent},
           f"got {sorted({a for a, _, _ in sent})}")
finally:
    smtplib.SMTP = real_smtp
    app.config["MAIL_SERVER"] = None


# ── 4. History carries the body + list; per-message download ────────
hist = staff.get("/api/messages").get_json() or {}
row = next((x for x in hist.get("messages", []) if x["id"] == mid), None)
record("history returns the full body", row is not None and row.get("body") == "Hi all", f"got {row}")
record("history returns the address list",
       row is not None and "ava.dad@x.com" in (row.get("recipient_emails") or ""), f"got {row}")
r = staff.get(f"/api/messages/{mid}/recipients.csv")
body = r.get_data(as_text=True)
record("a sent message's recipient list downloads as CSV",
       r.status_code == 200 and "text/csv" in r.headers.get("Content-Type", "")
       and "wilkanda@example.test" in body and "ava.mom@x.com" in body, f"got {r.status_code} {body[:80]!r}")
r = staff.get("/api/messages/999999/recipients.csv")
record("unknown message id is a 404", r.status_code == 404, f"got {r.status_code}")


# ── 5. Roster export has the second parent email ────────────────────
r = staff.get("/api/reports/students.csv")
body = r.get_data(as_text=True)
record("roster export has a Second parent email column",
       "Second parent email" in body.splitlines()[0], f"got {body.splitlines()[0]}")
record("and Ava's second parent is in it", "ava.dad@x.com" in body)

passed = sum(1 for _, p in results if p)
total = len(results)
print("\n" + "=" * 56)
print(f"SUMMARY: {passed}/{total} passed, {total - passed} failed.")
sys.exit(0 if passed == total else 1)
