"""Two parent emails per account.

The studio's request (Aug 2026): "Mommy's like Daddy's to get the bill as well
I'm only able to email one parent at a time." Everything the app sends about a
dancer - the bill, receipts, balance reminders, studio blasts - went to exactly
one address, so the other parent never saw it.

What has to hold:

  1. One resolver decides recipients, and EVERY parent-facing send uses it.
     Two copies would drift and quietly drop a parent again.
  2. Sources are the dancer's two parent addresses plus the household's two.
     A family that registered through the public form already has the second
     guardian on file, so most households get both parents automatically.
  3. The dancer's own address stays a FALLBACK - used only when no parent
     address exists, never alongside one. Bills don't go to the child.
  4. Duplicates collapse case-insensitively: one parent, one copy.
  5. It survives the pilot-era shape - dancers with no family row at all.

Run:  RFID_ENABLED=false python3 tests/test_parent_emails.py
Exit 0 = all green, 1 = failures.
"""
import os
import smtplib
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("RFID_ENABLED", "false")
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp.name}"

from app import create_app, db  # noqa: E402
from app.helpers import family_emails, student_emails  # noqa: E402
from app.models import (  # noqa: E402
    ClassEnrollment, DanceClass, Family, Student, Transaction, User,
)

app = create_app("development")
app.config["TESTING"] = True
app.config["WTF_CSRF_ENABLED"] = False

results = []


def record(name, passed, detail=""):
    results.append((name, passed))
    print(f"[{'PASS' if passed else 'FAIL'}] {name}" + (f" - {detail}" if detail and not passed else ""))


sent = []


class FakeSMTP:
    def __init__(self, *a, **kw):
        pass

    def starttls(self):
        pass

    def login(self, *a):
        pass

    def sendmail(self, sender, addr, msg):
        sent.append(addr)

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
    admin.set_password("pw12345")
    db.session.commit()
    admin_id = admin.id

staff = app.test_client()
r = staff.post("/auth/login", data={"username": "admin", "password": "pw12345"},
               follow_redirects=True)
record("admin login for API calls", r.status_code == 200, f"got {r.status_code}")


# ── 1. The resolver itself ──────────────────────────────────────────
with app.app_context():
    fam = Family(name="Both Family", primary_email="mom@x.com",
                 secondary_email="dad@x.com")
    db.session.add(fam)
    db.session.flush()

    kid = Student(first_name="Nia", last_name="Both", family_id=fam.id,
                  parent_email="mom@x.com", parent_email_2="grandma@x.com",
                  email="nia@x.com")
    db.session.add(kid)

    # No family at all - the pilot-era shape the studio still has.
    solo = Student(first_name="Solo", last_name="Nofam",
                   parent_email="solomom@x.com", parent_email_2="solodad@x.com")
    db.session.add(solo)

    # Only the dancer's own address exists.
    orphan = Student(first_name="Only", last_name="Kidmail", email="kidonly@x.com")
    db.session.add(orphan)

    # Same address typed twice in different cases, plus a blank.
    dupe = Student(first_name="Dupe", last_name="Case",
                   parent_email="Same@X.com", parent_email_2="  same@x.com  ",
                   email="dupe@x.com")
    db.session.add(dupe)
    db.session.commit()

    got = student_emails(kid)
    record("dancer's two parent emails and the household's are all included",
           set(got) == {"mom@x.com", "dad@x.com", "grandma@x.com"}, f"got {got}")
    record("the dancer's own address is NOT added when a parent address exists",
           "nia@x.com" not in got, f"got {got}")
    record("the primary parent email comes first", got[0] == "mom@x.com", f"got {got}")

    got_solo = student_emails(solo)
    record("a dancer with no family still gets both parents",
           set(got_solo) == {"solomom@x.com", "solodad@x.com"}, f"got {got_solo}")

    got_orphan = student_emails(orphan)
    record("the dancer's own address IS used when there is no parent address",
           got_orphan == ["kidonly@x.com"], f"got {got_orphan}")

    got_dupe = student_emails(dupe)
    record("the same address in different cases collapses to one copy",
           got_dupe == ["Same@X.com"], f"got {got_dupe}")

    record("blank and malformed entries are dropped",
           student_emails(Student(first_name="X", last_name="Y",
                                  parent_email="  ", parent_email_2="notanemail")) == [],
           "a blank or malformed address survived")

    fam_got = family_emails(fam)
    record("family resolver spans the household and its dancers",
           set(fam_got) == {"mom@x.com", "dad@x.com", "grandma@x.com"}, f"got {fam_got}")
    kid_id, fam_id, solo_id = kid.id, fam.id, solo.id


# ── 2. The student API round-trips the second address ───────────────
r = staff.put(f"/api/students/{solo_id}", json={"parent_email_2": "newdad@x.com"})
record("saving a second parent email works", r.status_code == 200,
       f"got {r.status_code} {r.get_json()}")
got = staff.get(f"/api/students/{solo_id}").get_json() or {}
record("and it reads back on the dancer", got.get("parent_email_2") == "newdad@x.com",
       f"got {got.get('parent_email_2')}")
with app.app_context():
    record("the new address is in the send list",
           "newdad@x.com" in student_emails(Student.query.get(solo_id)),
           f"got {student_emails(Student.query.get(solo_id))}")


# ── 3. Every send path actually uses it ─────────────────────────────
real_smtp = smtplib.SMTP
smtplib.SMTP = FakeSMTP
app.config["MAIL_SERVER"] = "smtp.example.test"
try:
    # A studio blast to the whole class.
    with app.app_context():
        from datetime import time as _t
        cls = DanceClass(name="Both Class", day_of_week=0, start_time=_t(17, 0),
                         end_time=_t(18, 0), instructor_id=admin_id)
        db.session.add(cls)
        db.session.flush()
        db.session.add(ClassEnrollment(student_id=kid_id, class_id=cls.id))
        db.session.commit()
        cls_id = cls.id

    sent.clear()
    r = staff.post("/api/messages", json={
        "subject": "Recital", "body": "Details inside.",
        "recipient_type": "class", "recipient_filter": cls_id})
    record("class blast accepted", r.status_code == 201, f"got {r.status_code}")
    wait_for(3)
    record("a class blast reaches BOTH parents and the household",
           {"mom@x.com", "dad@x.com", "grandma@x.com"} <= set(sent), f"got {sorted(set(sent))}")
    record("the dancer's own address is not blasted",
           "nia@x.com" not in sent, f"got {sorted(set(sent))}")

    # A balance reminder.
    with app.app_context():
        db.session.add(Transaction(student_id=kid_id, type="charge", amount=50,
                                   category="tuition", payment_method="n/a",
                                   description="Tuition", created_by=admin_id))
        db.session.commit()
    sent.clear()
    r = staff.post("/api/balances/send-reminders", json={})
    record("reminder run accepted", r.status_code == 200, f"got {r.status_code} {r.get_json()}")
    wait_for(3)
    record("a balance reminder reaches both parents",
           {"mom@x.com", "dad@x.com"} <= set(sent), f"got {sorted(set(sent))}")

    # A payment receipt. Receipts fire when the studio CONFIRMS a parent-reported
    # payment (and on the Square webhook) - recording a transaction by hand has
    # never sent one, so confirm is the path to exercise.
    with app.app_context():
        from app.models import PendingPayment
        pp = PendingPayment(student_id=kid_id, amount=25, method="zelle",
                            reference="TEST-1", parent_id=admin_id)
        db.session.add(pp)
        db.session.commit()
        pp_id = pp.id
    sent.clear()
    r = staff.post(f"/api/pending-payments/{pp_id}/confirm", json={"category": "tuition"})
    record("payment confirmed", r.status_code == 200, f"got {r.status_code} {r.get_json()}")
    wait_for(2)
    record("a payment receipt reaches both parents",
           {"mom@x.com", "dad@x.com"} <= set(sent), f"got {sorted(set(sent))}")
finally:
    smtplib.SMTP = real_smtp
    app.config["MAIL_SERVER"] = None


# ── 4. The new PII field is covered by the policy and the export ────
priv = app.test_client().get("/privacy").get_data(as_text=True)
record("the privacy policy lists the second parent email",
       "two parent/guardian email addresses" in priv,
       "policy still describes a single parent email")
record("the data export carries it",
       "parent_email_2" in str(staff.get(f"/api/students/{kid_id}").get_json()),
       "parent_email_2 missing from the student serializer the export uses")


# ── 5. Migration adds the column to an EXISTING students table ──────
# Production's students table predates this column and db.create_all() will not
# alter it; only run_migrations() adds it. Reproduce that shape.
with app.app_context():
    import sqlalchemy

    from app.migrations import run_migrations

    with db.engine.begin() as conn:
        conn.execute(sqlalchemy.text("ALTER TABLE students DROP COLUMN parent_email_2"))

    # Drop the pool: these columns were dropped on a different connection, and a
    # pooled one can still hold SQLite's old schema, making the ALTER below fail
    # with "duplicate column name" against a table that demonstrably lacks it.
    db.engine.dispose()
    insp = sqlalchemy.inspect(db.engine)
    record("fixture reproduces the pre-migration schema",
           "parent_email_2" not in {c["name"] for c in insp.get_columns("students")},
           "the column survived the drop, so the check below proves nothing")
    run_migrations(db)
    insp = sqlalchemy.inspect(db.engine)
    record("migration adds it to an existing students table",
           "parent_email_2" in {c["name"] for c in insp.get_columns("students")},
           "column missing after migrate")
    db.engine.dispose()
    run_migrations(db)  # Fly wakes the app all day; a second pass must be a no-op
    record("running the migration twice is a no-op", True)

passed = sum(1 for _, p in results if p)
total = len(results)
print("\n" + "=" * 56)
print(f"SUMMARY: {passed}/{total} passed, {total - passed} failed.")
sys.exit(0 if passed == total else 1)
