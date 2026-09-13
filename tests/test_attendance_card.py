"""Full-season attendance card.

The Take Attendance page used to show a trailing 8-week window - 7 prior weeks
plus the current one, each column labelled by its Monday. The owner read the
last column ("09/07") as attendance stopping on the 7th. A physical attendance
card shows the whole season, so now the grid does too.

What has to hold:

  1. With NO season dates (production's seeded season), the page is the old
     page: 8 columns, only the current week tappable. The deploy is invisible
     until the studio sets dates.
  2. With dates, the grid spans start..end Monday-aligned and ALWAYS includes
     the current week, even when today is outside the season.
  3. Past weeks are tappable when the class actually met inside the season
     that week; they post the class's meeting date, not today. Future weeks
     are inert. A tap on a past week is a backfill: stamped at class time,
     method 'backfill', audited.
  4. The toggle is week-scoped when the card asks for it: any row Mon-Sun lights
     the box, and un-marking clears every row in the week - including the
     legacy rows dated on the wrong day that the old exact-date toggle could
     never remove. Without `week_start` the old exact-date contract stands.
  5. The season API refuses an inverted or year-plus range, because the grid
     is built from those dates and would render empty or enormous.

Run:  RFID_ENABLED=false python3 tests/test_attendance_card.py
Exit 0 = all green, 1 = failures.
"""
import os
import re
import sys
import tempfile
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("RFID_ENABLED", "false")
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp.name}"

from sqlalchemy import func  # noqa: E402

from app import create_app, db  # noqa: E402
from app.helpers import MAX_CARD_WEEKS, attendance_card_weeks  # noqa: E402
from app.models import (  # noqa: E402
    Attendance, AuditLog, ClassEnrollment, DanceClass, Season, Student, User,
)

app = create_app("development")
app.config["TESTING"] = True
app.config["WTF_CSRF_ENABLED"] = False

results = []


def record(name, passed, detail=""):
    results.append((name, passed))
    print(f"[{'PASS' if passed else 'FAIL'}] {name}" + (f" - {detail}" if detail and not passed else ""))


T = date.today()
CM = T - timedelta(days=T.weekday())          # this week's Monday
W = timedelta(weeks=1)


def boxes(html, sid):
    """{monday_iso: (classes, attrs)} for one student's row."""
    out = {}
    for m in re.finditer(rf'<div id="box-{sid}-(\d{{4}}-\d{{2}}-\d{{2}})"\s+class="([^"]*)"([^>]*)>', html):
        out[m.group(1)] = (m.group(2), m.group(3))
    return out


# ── A. The helper, on its own ───────────────────────────────────────
def stub(start, end):
    return SimpleNamespace(start_date=start, end_date=end)


r = attendance_card_weeks(None, today=date(2026, 9, 13))
record("no season -> trailing 8 weeks ending at the current Monday",
       r["mode"] == "trailing" and len(r["weeks"]) == 8
       and r["weeks"][-1] == date(2026, 9, 7) and r["weeks"][0] == date(2026, 7, 20)
       and r["latest"] == date(2026, 9, 13), f"got {r}")
for label, s in (("start only", stub(date(2026, 9, 11), None)),
                 ("end only", stub(None, date(2027, 6, 16))),
                 ("inverted", stub(date(2027, 6, 16), date(2026, 9, 11)))):
    r = attendance_card_weeks(s, today=date(2026, 9, 13))
    record(f"{label} dates -> trailing fallback", r["mode"] == "trailing" and len(r["weeks"]) == 8
           and not r["capped"], f"got {r['mode']} {len(r['weeks'])}")

r = attendance_card_weeks(stub(date(2026, 9, 11), date(2027, 6, 16)), today=date(2026, 9, 13))
record("Fri 09/11 .. Wed 06/16 -> Mondays 09/07 .. 06/14, 41 weeks",
       r["mode"] == "season" and r["weeks"][0] == date(2026, 9, 7)
       and r["weeks"][-1] == date(2027, 6, 14) and len(r["weeks"]) == 41
       and r["latest"] == date(2027, 6, 20) and r["current_monday"] == date(2026, 9, 7)
       and not r["capped"], f"got {r['weeks'][0]}..{r['weeks'][-1]} n={len(r['weeks'])}")
r = attendance_card_weeks(stub(date(2026, 9, 11), date(2027, 6, 16)), today=date(2026, 8, 1))
record("today before the season -> current week is prepended",
       r["weeks"][0] == date(2026, 7, 27) and r["weeks"][-1] == date(2027, 6, 14), f"got {r['weeks'][0]}")
r = attendance_card_weeks(stub(date(2026, 9, 11), date(2027, 6, 16)), today=date(2027, 7, 10))
record("today after the season -> current week is appended",
       r["weeks"][-1] == date(2027, 7, 5) and r["weeks"][0] == date(2026, 9, 7), f"got {r['weeks'][-1]}")
r = attendance_card_weeks(stub(date(2026, 9, 11), date(2036, 6, 16)), today=date(2026, 9, 13))
record(f"a span over {MAX_CARD_WEEKS} weeks falls back to trailing and says so",
       r["mode"] == "trailing" and r["capped"] and len(r["weeks"]) == 8
       and r["weeks"][-1] == date(2026, 9, 7), f"got {r['mode']} capped={r['capped']}")


# ── Fixture ─────────────────────────────────────────────────────────
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
    season = Season.query.filter_by(status="active").first()
    season_id = season.id
    # The class meets on TODAY's weekday so the current-week box is unambiguous.
    cls = DanceClass(name="Card Class", day_of_week=T.weekday(), start_time=time(17, 0),
                     end_time=time(18, 0), instructor_id=admin.id, season_id=season_id)
    db.session.add(cls)
    db.session.flush()
    # A second class that meets on MONDAY, so that with a mid-week season start
    # the first column's meeting date is provably before the season began.
    mon_cls = DanceClass(name="Monday Class", day_of_week=0, start_time=time(10, 0),
                         end_time=time(11, 0), instructor_id=admin.id, season_id=season_id)
    db.session.add(mon_cls)
    db.session.flush()
    kids = []
    for fn in ("Ava", "Zoe"):
        st = Student(first_name=fn, last_name="Card")
        db.session.add(st)
        db.session.flush()
        db.session.add(ClassEnrollment(student_id=st.id, class_id=cls.id))
        db.session.add(ClassEnrollment(student_id=st.id, class_id=mon_cls.id))
        kids.append(st.id)
    db.session.commit()
    cid, mon_cid, sid, sid2 = cls.id, mon_cls.id, kids[0], kids[1]
    DAY = T.weekday()

staff = app.test_client()
staff.post("/auth/login", data={"username": "admin", "password": "pw12345"}, follow_redirects=True)
teach = app.test_client()
teach.post("/auth/login", data={"username": "teach", "password": "pw12345"}, follow_redirects=True)


# ── B1. NULL dates == the old page ──────────────────────────────────
with app.app_context():
    s = Season.query.get(season_id)
    record("fixture: the seeded season has no dates (production's shape)",
           s.start_date is None and s.end_date is None)

html = staff.get(f"/take-attendance/{cid}").get_data(as_text=True)
bx = boxes(html, sid)
record("no dates: 8 columns per student", len(bx) == 8, f"got {len(bx)}")
record("no dates: columns are the 7 prior Mondays plus this one",
       sorted(bx) == [(CM - i * W).isoformat() for i in range(7, -1, -1)], f"got {sorted(bx)}")
cur = bx.get(CM.isoformat(), ("", ""))
record("no dates: the current week is tappable and posts today",
       "current-week" in cur[0] and "tappable" in cur[0] and f'data-date="{T.isoformat()}"' in cur[1],
       f"got {cur}")
record("no dates: no past week is tappable",
       not any("past-week" in c and "tappable" in c for c, _ in bx.values()))
record("no dates: the backfill instruction isn't shown", "fill in a past week" not in html)


# ── B5 (part). Season API validation ────────────────────────────────
r = staff.put(f"/api/seasons/{season_id}", json={"start_date": T.isoformat(),
                                                 "end_date": (T - timedelta(days=1)).isoformat()})
record("end before start is refused", r.status_code == 400, f"got {r.status_code} {r.get_json()}")
r = staff.put(f"/api/seasons/{season_id}", json={"start_date": T.isoformat(),
                                                 "end_date": (T + timedelta(days=500)).isoformat()})
record("a span over a year is refused", r.status_code == 400, f"got {r.status_code} {r.get_json()}")
with app.app_context():
    s = Season.query.get(season_id)
    record("refused updates leave the dates untouched", s.start_date is None and s.end_date is None)
r = teach.put(f"/api/seasons/{season_id}", json={"start_date": T.isoformat()})
record("a teacher cannot edit season dates", r.status_code == 403, f"got {r.status_code}")
r = staff.post("/api/seasons", json={"name": "Backwards", "start_date": "2027-06-16",
                                     "end_date": "2026-09-11"})
record("creating an inverted season is refused", r.status_code == 400, f"got {r.status_code}")

# A season around today: starts on the WEDNESDAY three weeks back, ends three
# weeks ahead. The Wednesday start means the Monday class's first-column
# meeting (that Monday) is before the season began.
START = CM - 3 * W + timedelta(days=2)
END = CM + 3 * W + timedelta(days=6)
r = staff.put(f"/api/seasons/{season_id}", json={"start_date": START.isoformat(),
                                                 "end_date": END.isoformat()})
record("admin sets the season dates", r.status_code == 200
       and r.get_json().get("start_date") == START.isoformat()
       and r.get_json().get("end_date") == END.isoformat(), f"got {r.status_code} {r.get_json()}")
with app.app_context():
    record("the update is audited",
           AuditLog.query.filter_by(action="season.update").count() >= 1)


# ── B2/B3. The season grid ──────────────────────────────────────────
html = staff.get(f"/take-attendance/{cid}").get_data(as_text=True)
bx = boxes(html, sid)
first_monday = min(START - timedelta(days=START.weekday()), CM)
last_monday = max(END - timedelta(days=END.weekday()), CM)
expected = []
m = first_monday
while m <= last_monday:
    expected.append(m.isoformat())
    m += W
record("season grid spans the Monday-aligned start..end",
       sorted(bx) == expected, f"got {sorted(bx)[:2]}..{sorted(bx)[-2:]} want {expected[:2]}..{expected[-2:]}")
record("exactly one current week", sum("current-week" in c for c, _ in bx.values()) == 1)
record("the backfill instruction is shown", "fill in a past week" in html)

past_iso = (CM - W).isoformat()
past_meeting = CM - W + timedelta(days=DAY)
pc, pa = bx[past_iso]
record("a past week is a dashed tappable box",
       "past-week" in pc and "tappable" in pc and 'onclick="toggleMark(this)"' in pa, f"got {pc} | {pa}")
record("a past week posts the day the class met, not today",
       f'data-date="{past_meeting.isoformat()}"' in pa and past_meeting < T, f"got {pa}")
record("a past week carries its week_start",
       f'data-week-start="{past_iso}"' in pa, f"got {pa}")
record("the column is labelled with the class's meeting date",
       f'<div class="week-label mb-1">{past_meeting.strftime("%m/%d")}</div>' in html)

fc, fa = bx[(CM + W).isoformat()]
record("a future week is inert", "future-week" in fc and "tappable" not in fc
       and "data-date" not in fa and "onclick" not in fa, f"got {fc} | {fa}")

# First column of the MONDAY class: it "met" on the Monday before the
# Wednesday the season started, so that box must be inert.
mon_html = staff.get(f"/take-attendance/{mon_cid}").get_data(as_text=True)
mbx = boxes(mon_html, sid)
ffc, ffa = mbx[first_monday.isoformat()]
record("a past week the class met before the season started is inert",
       "past-week" in ffc and "tappable" not in ffc and "onclick" not in ffa,
       f"first Monday {first_monday} < start {START}: {ffc}")
sc, sa = mbx[(first_monday + W).isoformat()]
record("the next week of that class, inside the season, is tappable",
       "past-week" in sc and "tappable" in sc, f"got {sc}")


# ── B2. Backfill round-trip through the API ─────────────────────────
r = staff.post("/api/attendance/toggle", json={"student_id": sid, "class_id": cid,
                                               "date": past_meeting.isoformat(),
                                               "week_start": past_iso})
body = r.get_json() or {}
record("backfill tap marks present", r.status_code == 201 and body.get("present") is True
       and body.get("date") == past_meeting.isoformat(), f"got {r.status_code} {body}")
with app.app_context():
    rows = Attendance.query.filter(Attendance.student_id == sid, Attendance.class_id == cid,
                                   func.date(Attendance.check_in_time) == past_meeting).all()
    record("one row, on the class date", len(rows) == 1, f"got {len(rows)}")
    if rows:
        record("stamped at the class's start time", rows[0].check_in_time.time() == time(17, 0),
               f"got {rows[0].check_in_time}")
        record("marked as a backfill in the log", rows[0].check_in_method == "backfill",
               f"got {rows[0].check_in_method}")
    record("the backfill is audited",
           AuditLog.query.filter_by(action="attendance.backfill").count() == 1)
html = staff.get(f"/take-attendance/{cid}").get_data(as_text=True)
record("the past week now shows marked", "marked" in boxes(html, sid)[past_iso][0])

r = staff.post("/api/attendance/toggle", json={"student_id": sid, "class_id": cid,
                                               "date": past_meeting.isoformat(),
                                               "week_start": past_iso})
record("tapping again un-marks", r.status_code == 200 and (r.get_json() or {}).get("present") is False,
       f"got {r.status_code} {r.get_json()}")
with app.app_context():
    record("and the row is gone", Attendance.query.filter(
        Attendance.student_id == sid, Attendance.class_id == cid,
        func.date(Attendance.check_in_time) == past_meeting).count() == 0)

# ── B2. The legacy trap: a row on the WRONG day of the week ─────────
wrong_day = CM - 2 * W + timedelta(days=(DAY + 2) % 7)   # not the class day
with app.app_context():
    db.session.add(Attendance(student_id=sid, class_id=cid,
                              check_in_time=datetime.combine(wrong_day, time(9, 0)),
                              check_in_method="manual", is_present=True))
    db.session.commit()
html = staff.get(f"/take-attendance/{cid}").get_data(as_text=True)
record("a row on any day of the week lights that week's box",
       "marked" in boxes(html, sid)[(CM - 2 * W).isoformat()][0])
r = staff.post("/api/attendance/toggle", json={"student_id": sid, "class_id": cid,
                                               "date": (CM - 2 * W + timedelta(days=DAY)).isoformat(),
                                               "week_start": (CM - 2 * W).isoformat()})
with app.app_context():
    left = Attendance.query.filter(
        Attendance.student_id == sid, Attendance.class_id == cid,
        func.date(Attendance.check_in_time) >= CM - 2 * W,
        func.date(Attendance.check_in_time) <= CM - 2 * W + timedelta(days=6)).count()
record("un-marking a week clears a legacy row dated on the wrong day (the old trap)",
       (r.get_json() or {}).get("present") is False and left == 0,
       f"got present={(r.get_json() or {}).get('present')} rows_left={left}")

# ── B2. The old exact-date contract without week_start ──────────────
with app.app_context():
    db.session.add(Attendance(student_id=sid2, class_id=cid,
                              check_in_time=datetime.combine(wrong_day, time(9, 0)),
                              check_in_method="manual", is_present=True))
    db.session.commit()
r = staff.post("/api/attendance/toggle", json={"student_id": sid2, "class_id": cid,
                                               "date": (CM - 2 * W + timedelta(days=DAY)).isoformat()})
with app.app_context():
    n = Attendance.query.filter(
        Attendance.student_id == sid2, Attendance.class_id == cid,
        func.date(Attendance.check_in_time) >= CM - 2 * W,
        func.date(Attendance.check_in_time) <= CM - 2 * W + timedelta(days=6)).count()
record("without week_start the toggle is still exact-date (smoke contract)",
       (r.get_json() or {}).get("present") is True and n == 2, f"got present={(r.get_json() or {}).get('present')} rows={n}")

# ── B2. Guards ──────────────────────────────────────────────────────
r = staff.post("/api/attendance/toggle", json={"student_id": sid, "class_id": cid,
                                               "date": (T + timedelta(days=1)).isoformat()})
record("a future date is refused", r.status_code == 400, f"got {r.status_code}")
r = staff.post("/api/attendance/toggle", json={"student_id": sid, "class_id": cid,
                                               "date": (START - timedelta(days=30)).isoformat()})
record("a past date outside the season is refused", r.status_code == 400, f"got {r.status_code}")
with app.app_context():
    record("refused toggles created nothing",
           Attendance.query.filter(Attendance.student_id == sid, Attendance.class_id == cid,
                                   func.date(Attendance.check_in_time) < START).count() == 0)

# Today outside the season: the current week must still be there and tappable.
r = staff.put(f"/api/seasons/{season_id}", json={"start_date": (CM - 6 * W).isoformat(),
                                                 "end_date": (CM - 2 * W).isoformat()})
html = staff.get(f"/take-attendance/{cid}").get_data(as_text=True)
bx = boxes(html, sid)
cur = bx.get(CM.isoformat(), ("", ""))
record("today outside the season: this week is still on the card and tappable",
       "current-week" in cur[0] and "tappable" in cur[0], f"got {cur[0]}")
record("today outside the season: the banner says so", "outside this season's dates" in html)
r = staff.post("/api/attendance/toggle", json={"student_id": sid, "class_id": cid,
                                               "date": T.isoformat(), "week_start": CM.isoformat()})
record("today outside the season: today is still markable", r.status_code == 201, f"got {r.status_code}")
staff.post("/api/attendance/toggle", json={"student_id": sid, "class_id": cid,
                                           "date": T.isoformat(), "week_start": CM.isoformat()})

# ── B1. The defensive cap ───────────────────────────────────────────
with app.app_context():
    s = Season.query.get(season_id)
    s.start_date = CM - 10 * W
    s.end_date = CM + 60 * W       # past the API's guard, via the DB
    db.session.commit()
html = staff.get(f"/take-attendance/{cid}").get_data(as_text=True)
bx = boxes(html, sid)
record("an absurd span falls back to 8 weeks", len(bx) == 8 and CM.isoformat() in bx, f"got {len(bx)}")
record("and tells the admin why", f"more than {MAX_CARD_WEEKS} weeks" in html)

# ── Part A. Registration fee category ───────────────────────────────
tx = staff.get("/transactions").get_data(as_text=True)
record("Registration Fee is in all four category selects on the payments page",
       tx.count('<option value="registration">Registration Fee</option>') == 4,
       f"got {tx.count('value=\"registration\"')}")
ledger = staff.get(f"/students/{sid}/ledger").get_data(as_text=True)
record("and in the ledger's record-payment select",
       'value="registration"' in ledger)

passed = sum(1 for _, p in results if p)
total = len(results)
print("\n" + "=" * 56)
print(f"SUMMARY: {passed}/{total} passed, {total - passed} failed.")
sys.exit(0 if passed == total else 1)
