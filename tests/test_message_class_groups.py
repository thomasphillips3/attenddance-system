"""Emailing several classes at once (and by day / location).

Carollette: "Can you make it an option under the email to select more than one
class to email at a time? ... by the day the class meets ... by location like
the Oak Park classes versus the Lathrup Village classes?"

Day and location are picker shortcuts that tick class boxes, so the server only
has to take a SET of classes. What has to hold:

  1. recipient_filter takes one id or a comma list; the audience is the union
     of the rosters, and a family in two picked classes is mailed once.
  2. Inactive enrollments are left out.
  3. Bad input ("3,abc", "", ",") is a 400, never a 500 or an empty blast.
  4. Preview and send resolve the same audience, and the stored filter is the
     normalized id list, not the client's raw text.
  5. History says "2 classes" (names listed); old single-class rows still read
     as that class's name.
  6. Teachers can send to classes but still not to everyone.
  7. The Messages page renders when a class name has an apostrophe.

Run:  RFID_ENABLED=false python3 tests/test_message_class_groups.py
Exit 0 = all green, 1 = failures.
"""
import os
import sys
import tempfile
from datetime import time as dtime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("RFID_ENABLED", "false")
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp.name}"

from app import create_app, db  # noqa: E402
from app.models import ClassEnrollment, DanceClass, Location, Message, Student, User  # noqa: E402

app = create_app("development")
app.config["TESTING"] = True
app.config["WTF_CSRF_ENABLED"] = False
app.config["MAIL_SERVER"] = None   # no email: send saves the message and returns the list

results = []


def record(name, passed, detail=""):
    results.append((name, passed))
    print(f"[{'PASS' if passed else 'FAIL'}] {name}" + (f" - {detail}" if detail and not passed else ""))


with app.app_context():
    admin = User.query.filter_by(role="admin").first()
    if not admin:
        admin = User(username="admin", email="admin@example.test", role="admin",
                     first_name="Admin", last_name="User", is_admin=True)
        db.session.add(admin)
    admin.set_password("pw12345")
    teacher = User(username="teach", email="teach@example.test", role="teacher",
                   first_name="Tee", last_name="Cher")
    teacher.set_password("pw12345")
    db.session.add(teacher)
    db.session.flush()
    op = Location(name="Oak Park")
    lv = Location(name="Lathrup Village")
    db.session.add_all([op, lv])
    db.session.flush()

    def mk(name, day, loc):
        c = DanceClass(name=name, day_of_week=day, start_time=dtime(17, 0), end_time=dtime(18, 0),
                       instructor_id=admin.id, location_id=loc.id)
        db.session.add(c)
        db.session.flush()
        return c.id

    mon_op = mk("Mon Ballet", 0, op)
    mon_lv = mk("Mon Tap's", 0, lv)       # apostrophe on purpose
    tue_op = mk("Tue Jazz", 1, op)

    def kid(fn, mail):
        s = Student(first_name=fn, last_name="Grp", parent_email=mail)
        db.session.add(s)
        db.session.flush()
        return s.id

    ava, ben, cal, dee = (kid("Ava", "ava@x.com"), kid("Ben", "ben@x.com"),
                          kid("Cal", "cal@x.com"), kid("Dee", "dee@x.com"))
    enr = [(ava, mon_op), (ava, mon_lv),   # Ava is in both Monday classes
           (ben, mon_lv), (cal, tue_op)]
    for sid, cid in enr:
        db.session.add(ClassEnrollment(student_id=sid, class_id=cid))
    db.session.add(ClassEnrollment(student_id=dee, class_id=mon_op, is_active=False))
    db.session.commit()

staff = app.test_client()
staff.post("/auth/login", data={"username": "admin", "password": "pw12345"}, follow_redirects=True)
teach = app.test_client()
teach.post("/auth/login", data={"username": "teach", "password": "pw12345"}, follow_redirects=True)


def preview(client, flt):
    r = client.get("/api/messages/recipients", query_string={"recipient_type": "class",
                                                              "recipient_filter": flt})
    return r, (r.get_json() or {})


# 1. union + dedupe
r, d = preview(staff, f"{mon_op},{mon_lv}")
record("two classes give the union of both rosters, family mailed once",
       r.status_code == 200 and set(d.get("emails", [])) == {"ava@x.com", "ben@x.com"}
       and d.get("count") == 2, f"got {r.status_code} {d}")
r, d = preview(staff, f"{mon_op}, {mon_lv} ,{tue_op}")
record("whitespace in the list is tolerated and three classes union",
       set(d.get("emails", [])) == {"ava@x.com", "ben@x.com", "cal@x.com"}, f"got {d}")
r, d = preview(staff, f"{mon_op},{mon_op}")
record("a repeated id is not a second copy", d.get("emails") == ["ava@x.com"], f"got {d}")
r, d = preview(staff, str(tue_op))
record("a single id still works", d.get("emails") == ["cal@x.com"], f"got {d}")

# 2. inactive enrollment
record("an inactive enrollment is left out", "dee@x.com" not in
       preview(staff, str(mon_op))[1].get("emails", []))

# 3. bad input
for bad in (f"{mon_op},abc", "", ",", " , "):
    r, d = preview(staff, bad)
    record(f"bad filter {bad!r} is a 400", r.status_code == 400, f"got {r.status_code} {d}")
r, d = preview(staff, ",".join(str(i) for i in range(1, 202)))
record("more than 200 classes is a 400", r.status_code == 400, f"got {r.status_code}")
r, d = preview(staff, "99999")
record("an unknown class id is an empty audience, not an error",
       r.status_code == 200 and d.get("count") == 0, f"got {r.status_code} {d}")

for bad in ("99999999999999999999", "1_0", "+3", "٣"):
    r, d = preview(staff, bad)
    record(f"unsafe id {bad!r} is a 400, not a 500", r.status_code == 400, f"got {r.status_code}")

# teacher cannot reach a non-live class by posting its id
with app.app_context():
    adm = User.query.filter_by(username="admin").first()
    dead = DanceClass(name="Retired", day_of_week=3, start_time=dtime(17, 0), end_time=dtime(18, 0),
                      instructor_id=adm.id, is_active=False)
    db.session.add(dead)
    db.session.flush()
    db.session.add(ClassEnrollment(student_id=ava, class_id=dead.id))
    db.session.commit()
    dead_id = dead.id
r, d = preview(teach, f"{mon_op},{dead_id}")
record("a teacher cannot include a retired class", r.status_code == 400, f"got {r.status_code} {d}")
r, d = preview(teach, str(mon_op))
record("a teacher can still preview a live class", r.status_code == 200, f"got {r.status_code}")

# 4. send resolves the same audience, stores a normalized filter
_, pv = preview(staff, f"{mon_lv},{mon_op}")
r = staff.post("/api/messages", data={"subject": "Mondays", "body": "Hi",
                                      "recipient_type": "class",
                                      "recipient_filter": f" {mon_lv} , {mon_op},{mon_op}"},
               content_type="multipart/form-data")
d = r.get_json() or {}
record("send reaches exactly what the preview showed",
       r.status_code == 201 and d.get("recipient_count") == pv.get("count")
       and sorted(d.get("recipient_emails", [])) == pv.get("emails"), f"got {r.status_code} {d}")
with app.app_context():
    m = Message.query.filter_by(subject="Mondays").first()
    record("stored filter is the sorted, de-duplicated id list",
           m.recipient_filter == ",".join(str(i) for i in sorted({mon_op, mon_lv})),
           f"got {m.recipient_filter!r}")
r = staff.post("/api/messages", data={"subject": "x", "body": "y", "recipient_type": "class",
                                      "recipient_filter": ""},
               content_type="multipart/form-data")
record("sending to no classes is a 400 and stores nothing",
       r.status_code == 400, f"got {r.status_code}")
with app.app_context():
    record("no phantom message after the rejected send", Message.query.filter_by(subject="x").count() == 0)

# 5. history labels
staff.post("/api/messages", data={"subject": "One class", "body": "Hi", "recipient_type": "class",
                                  "recipient_filter": str(tue_op)},
           content_type="multipart/form-data")
hist = {m["subject"]: m for m in (staff.get("/api/messages").get_json() or {}).get("messages", [])}
record("history lists every class name on a multi-class message",
       sorted(hist.get("Mondays", {}).get("recipient_classes", [])) == sorted(["Mon Ballet", "Mon Tap's"]),
       f"got {hist.get('Mondays')}")
record("a one-class message lists that class",
       hist.get("One class", {}).get("recipient_classes") == ["Tue Jazz"], f"got {hist.get('One class')}")
with app.app_context():
    old = Message(subject="Legacy", body="b", recipient_type="class", recipient_filter="not-an-id",
                  recipient_count=0, recipient_emails="", created_by=1, sent=True)
    db.session.add(old)
    db.session.commit()
with app.app_context():
    db.session.add(Message(subject="Huge", body="b", recipient_type="class",
                           recipient_filter="99999999999999999999", recipient_count=0,
                           recipient_emails="", created_by=1, sent=True))
    db.session.commit()
r = staff.get("/api/messages")
record("a legacy row with a junk filter does not break the history list", r.status_code == 200,
       f"got {r.status_code}")

# 6. teacher scope
r, d = preview(teach, f"{mon_op},{tue_op}")
record("a teacher can preview several classes", r.status_code == 200 and d.get("count") == 2,
       f"got {r.status_code} {d}")
r = teach.post("/api/messages", data={"subject": "T", "body": "t", "recipient_type": "all"},
               content_type="multipart/form-data")
record("a teacher still cannot send to everyone", r.status_code == 403, f"got {r.status_code}")

# 7. page
r = staff.get("/messages")
html = r.get_data(as_text=True)
record("the Messages page renders", r.status_code == 200, f"got {r.status_code}")
record("a class name with an apostrophe is JSON-encoded, not pasted into a JS string",
       "Mon Tap\\u0027s" in html and "Mon Tap's" not in html)
record("the picker offers day and location shortcuts",
       'id="day-chips"' in html and 'id="loc-chips"' in html and "Lathrup Village" in html)

passed = sum(1 for _, p in results if p)
total = len(results)
print("\n" + "=" * 56)
print(f"SUMMARY: {passed}/{total} passed, {total - passed} failed.")
sys.exit(0 if passed == total else 1)
