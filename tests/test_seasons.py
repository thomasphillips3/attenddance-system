"""Seasons tests for AttenDANCE.

The invariants that make "build season n+1 while season n runs" safe:

  1. Boot migration seeds one active season and adopts existing classes.
  2. New classes default into the active season; explicit season_id wins.
  3. Draft-season classes are INVISIBLE to live views (class list default,
     public registration) but visible via ?season_id= (the Classes page picker).
  4. A recurring charge on a draft-season class never bills; the same charge
     bills normally once the season is activated.
  5. Activation archives the old season and winds its classes down (class
     inactive, auto-billing stopped, enrollments closed) without touching the
     new season's classes.
  6. registration_season_id points the public form at a draft season (fall
     sign-ups during summer).
  7. Draft-only, empty-only season deletion.

Run:  RFID_ENABLED=false python3 tests/test_seasons.py
Exit 0 = all green, 1 = failures.
"""
import os
import sys
import tempfile
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("RFID_ENABLED", "false")
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp.name}"

from app import create_app, db, _process_recurring_charges  # noqa: E402
from app.models import (  # noqa: E402
    ClassEnrollment, DanceClass, RecurringCharge, Season, Setting, Student,
    Transaction, User,
)

app = create_app("development")
app.config["TESTING"] = True
app.config["WTF_CSRF_ENABLED"] = False

results = []


def record(name, passed, detail=""):
    results.append((name, passed))
    print(f"[{'PASS' if passed else 'FAIL'}] {name}" + (f" - {detail}" if detail and not passed else ""))


def mk_class(name, season_id, **kw):
    from datetime import time
    c = DanceClass(name=name, day_of_week=0, start_time=time(17, 0), end_time=time(18, 0),
                   instructor_id=admin_id, season_id=season_id, **kw)
    db.session.add(c)
    db.session.commit()
    return c


with app.app_context():
    admin = User.query.filter_by(role="admin").first()
    if not admin:
        admin = User(username="admin", email="admin@example.test", role="admin",
                     first_name="Admin", last_name="User", is_admin=True)
        admin.set_password("pw12345")
        db.session.add(admin)
        db.session.commit()
    else:
        admin.set_password("pw12345")
        db.session.commit()
    admin_id = admin.id

    # ── 1. Boot seeded exactly one active season ─────────────────────
    actives = Season.query.filter_by(status="active").all()
    record("boot migration seeds exactly one active season", len(actives) == 1,
           f"got {len(actives)}")
    summer = actives[0]

    # ── 2. Class creation defaults into the active season ────────────
    live_cls = mk_class("Summer Jazz", None)
    # NULL season simulates a legacy row; also make a normal assigned one
    assigned = mk_class("Summer Ballet", summer.id)

client = app.test_client()
r = client.post("/auth/login", data={"username": "admin", "password": "pw12345"},
                follow_redirects=True)
record("admin login for API calls", r.status_code == 200, f"got {r.status_code}")

# API-created class with no season_id → active season
r = client.post("/api/classes", json={"name": "Summer Tap", "day_of_week": 1,
                                      "start_time": "16:00", "end_time": "17:00"})
with app.app_context():
    summer = Season.query.filter_by(status="active").first()
    api_cls = DanceClass.query.filter_by(name="Summer Tap").first()
    record("API class creation defaults to active season",
           r.status_code == 201 and api_cls.season_id == summer.id,
           f"status {r.status_code}, season {api_cls.season_id if api_cls else None}")

# ── 3. Draft season staging ─────────────────────────────────────────
r = client.post("/api/seasons", json={"name": "Fall 2026", "start_date": "2026-09-01"})
record("create draft season", r.status_code == 201 and r.get_json()["status"] == "draft",
       f"got {r.status_code} {r.get_json()}")
fall_id = r.get_json()["id"]

r = client.post("/api/classes", json={"name": "Fall Hip Hop", "day_of_week": 2,
                                      "start_time": "17:00", "end_time": "18:00",
                                      "season_id": fall_id})
record("create class in draft season", r.status_code == 201
       and r.get_json()["season_id"] == fall_id, f"got {r.get_json()}")
fall_cls_id = r.get_json()["id"]

# Default class list must NOT contain the draft class
names = [c["name"] for c in client.get("/api/classes").get_json()["classes"]]
record("draft-season class hidden from default class list",
       "Fall Hip Hop" not in names and "Summer Tap" in names, f"got {names}")

# Explicit season view shows it
names = [c["name"] for c in client.get(f"/api/classes?season_id={fall_id}").get_json()["classes"]]
record("?season_id= shows the draft season's classes",
       names == ["Fall Hip Hop"], f"got {names}")

# Public registration (default) must not list it
with app.app_context():
    Setting.set("registration_open", "1")
pub = client.get("/api/registration/open").get_json()
pub_names = [c["name"] for c in pub["classes"]]
record("public registration (default) hides draft classes",
       "Fall Hip Hop" not in pub_names and "Summer Tap" in pub_names, f"got {pub_names}")

# ── 6. Registration targeted at the draft season ────────────────────
with app.app_context():
    Setting.set("registration_season_id", str(fall_id))
pub_names = [c["name"] for c in client.get("/api/registration/open").get_json()["classes"]]
record("registration_season_id points public form at draft season",
       pub_names == ["Fall Hip Hop"], f"got {pub_names}")
with app.app_context():
    Setting.set("registration_season_id", "")

# ── 4. Draft-season recurring charge never bills ────────────────────
with app.app_context():
    student = Student.query.first()
    if not student:
        student = Student(first_name="Test", last_name="Dancer")
        db.session.add(student)
        db.session.commit()
    db.session.add(ClassEnrollment(student_id=student.id, class_id=fall_cls_id, is_active=True))
    rc = RecurringCharge(class_id=fall_cls_id, amount=100, category="tuition",
                         day_of_month=1, is_active=True,
                         created_at=datetime.utcnow() - timedelta(days=45))
    db.session.add(rc)
    db.session.commit()
    rc_id = rc.id

    _process_recurring_charges(today=date.today())
    n = Transaction.query.filter_by(recurring_charge_id=rc_id).count()
    record("recurring charge on DRAFT season class does not bill", n == 0, f"got {n} txns")

    # Enroll a student in a live class so activation has something to wind down
    db.session.add(ClassEnrollment(student_id=student.id,
                                   class_id=DanceClass.query.filter_by(name="Summer Tap").first().id,
                                   is_active=True))
    summer_rc = RecurringCharge(class_id=DanceClass.query.filter_by(name="Summer Tap").first().id,
                                amount=50, category="tuition", day_of_month=1, is_active=True)
    db.session.add(summer_rc)
    db.session.commit()

# ── 5. Activation flips the world ───────────────────────────────────
r = client.post(f"/api/seasons/{fall_id}/activate")
record("activate draft season", r.status_code == 200, f"got {r.status_code} {r.get_json()}")

with app.app_context():
    fall = db.session.get(Season, fall_id)
    old = Season.query.filter_by(name="Current Season").first()
    record("fall is now active, old season archived",
           fall.status == "active" and old.status == "archived",
           f"fall={fall.status} old={old.status}")

    summer_tap = DanceClass.query.filter_by(name="Summer Tap").first()
    fall_cls = db.session.get(DanceClass, fall_cls_id)
    record("old season's classes wound down; fall class untouched",
           not summer_tap.is_active and fall_cls.is_active,
           f"summer_tap.active={summer_tap.is_active} fall.active={fall_cls.is_active}")

    old_rc = RecurringCharge.query.filter_by(class_id=summer_tap.id).first()
    old_enr = ClassEnrollment.query.filter_by(class_id=summer_tap.id, is_active=True).count()
    record("old season auto-billing stopped and enrollments closed",
           not old_rc.is_active and old_enr == 0,
           f"rc.active={old_rc.is_active} open_enrollments={old_enr}")

    # The SAME draft charge now bills (season is active)
    _process_recurring_charges(today=date.today())
    n = Transaction.query.filter_by(recurring_charge_id=rc_id).count()
    record("recurring charge bills once its season becomes active", n == 1, f"got {n} txns")

# Default class list now shows fall, hides summer
names = [c["name"] for c in client.get("/api/classes").get_json()["classes"]]
record("after activation the default list is the new season",
       "Fall Hip Hop" in names and "Summer Tap" not in names, f"got {names}")

# ── 7. Season deletion guards ───────────────────────────────────────
r = client.delete(f"/api/seasons/{fall_id}")
record("cannot delete the active season", r.status_code == 400, f"got {r.status_code}")

r = client.post("/api/seasons", json={"name": "Scratch"})
scratch_id = r.get_json()["id"]
client.post("/api/classes", json={"name": "Scratch Class", "day_of_week": 3,
                                  "start_time": "10:00", "end_time": "11:00",
                                  "season_id": scratch_id})
r = client.delete(f"/api/seasons/{scratch_id}")
record("cannot delete a draft season that has classes", r.status_code == 400,
       f"got {r.status_code}")
with app.app_context():
    DanceClass.query.filter_by(name="Scratch Class").delete()
    db.session.commit()
r = client.delete(f"/api/seasons/{scratch_id}")
record("can delete an empty draft season", r.status_code == 200, f"got {r.status_code}")

passed = sum(1 for _, p in results if p)
total = len(results)
print("\n" + "=" * 56)
print(f"SUMMARY: {passed}/{total} passed, {total - passed} failed.")
sys.exit(0 if passed == total else 1)
