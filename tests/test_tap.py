"""Tap-In core: one tap in, one attendance change out.

What has to hold:

  1. A tap during a class window checks the dancer in with method 'tap'. A
     tap outside every window asks staff to pick a class and writes nothing.
  2. A repeat tap within the dedup window is a duplicate. A tap after the
     dwell checks out by setting check_out_time on the SAME row.
  3. Direction comes from the tap ledger: a teacher's P on the card does not
     turn the arrival tap into a departure.
  4. A replayed event_key writes nothing new. Every tap is logged.
  5. Back-to-back classes roll over; tardy auto-mark is off unless set.
  6. Unknown, lost, pending and unassigned tags are refused. Undo reverses.
  7. Legacy rfid_uid badges are backfilled as tags; tokens are stored hashed.
  8. The API refuses parents and answers an expired session with JSON 401.

Run:  RFID_ENABLED=false python3 tests/test_tap.py
Exit 0 = all green, 1 = failures.
"""
import os
import sys
import tempfile
from datetime import date, datetime, time, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("RFID_ENABLED", "false")
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp.name}"

from app import create_app, db  # noqa: E402
from app.migrations import run_migrations  # noqa: E402
from app.models import (  # noqa: E402
    Attendance, ClassEnrollment, DanceClass, Family, MakeupClass, NfcTag, ParentStudent,
    Season, Setting, Student, TapEvent, Transaction, User, WaiverTemplate,
)
from app.tap.credentials import (  # noqa: E402
    hash_token, label_for, legacy_uid, mint_token, normalize_uid, tag_url,
)
from app.tap.service import (  # noqa: E402
    TapError, TapRequest, pick_class, process_tap, reopen_session, undo_tap,
)

app = create_app("development")
app.config["TESTING"] = True

results = []


def record(name, passed, detail=""):
    results.append((name, passed))
    print(f"[{'PASS' if passed else 'FAIL'}] {name}" + (f" - {detail}" if detail and not passed else ""))


# A fixed Wednesday so class windows don't depend on when the test runs.
DAY = date(2026, 10, 14)
WD = DAY.weekday()


def at(h, m, s=0, day=DAY):
    return datetime.combine(day, time(h, m, s))


_n = [0]
_calls = [0]


def key():
    _n[0] += 1
    return f"test-event-{_n[0]:05d}"


def tap(token=None, uid=None, when=None, class_id=None, event_key=None, source="api"):
    r = process_tap(TapRequest(event_key=event_key or key(), token=token, uid=uid,
                               occurred_at=when, source=source, class_id=class_id))
    if not r.replayed:
        _calls[0] += 1
    return r


def rows(sid, cid=None):
    q = Attendance.query.filter_by(student_id=sid)
    if cid:
        q = q.filter_by(class_id=cid)
    return q.order_by(Attendance.id).all()


def make_tag(sid, status="active", uid=None, kind="fob"):
    token = mint_token()
    t = NfcTag(student_id=sid, token_hash=hash_token(token), uid=normalize_uid(uid) if uid else None,
               kind=kind, status=status)
    db.session.add(t)
    db.session.flush()
    t.label = label_for(t.id)
    db.session.commit()
    return token, t.id


# ── A. Credentials, on their own ────────────────────────────────────
record("normalize_uid: Web NFC colons and USB hex agree",
       normalize_uid("04:a2:1b:3c:4d:5e:80") == normalize_uid("04A21B3C4D5E80") == "04A21B3C4D5E80")
record("normalize_uid: odd lengths (a 4-byte read of a 7-byte chip, decimal) are refused",
       normalize_uid("04A21B3C4D") is None and normalize_uid("1234567890") is None
       and normalize_uid("zz:11:22:33") is None and normalize_uid("") is None)
record("legacy_uid: MFRC522 read_id (4 bytes + BCC, decimal) drops the BCC",
       legacy_uid(str(int("DEADBEEF" + f"{0xDE ^ 0xAD ^ 0xBE ^ 0xEF:02X}", 16))) == "DEADBEEF")
record("legacy_uid: plain hex passes through", legacy_uid("04a21b3c") == "04A21B3C")
tok = mint_token()
record("tokens are 22 url-safe chars and hash to 64 hex",
       len(tok) == 22 and len(hash_token(tok)) == 64 and hash_token(tok) != tok)
with app.app_context():
    record("tag URL uses the portal host by default", tag_url("abc").endswith("/t/abc"))
    Setting.set("tap_base_url", "https://tags.example.test/")
    record("tag URL uses tap_base_url once decided",
           tag_url("abc") == "https://tags.example.test/t/abc")
    Setting.set("tap_base_url", "")

# ── Fixture ─────────────────────────────────────────────────────────
with app.app_context():
    admin = User.query.filter_by(username="admin").first()
    admin.set_password("pw12345")
    teacher = User(username="teach", email="teach@example.test", role="teacher",
                   first_name="Tee", last_name="Cher")
    teacher.set_password("pw12345")
    parent = User(username="mom", email="mom@example.test", role="parent",
                  first_name="Mo", last_name="M")
    parent.set_password("pw12345")
    db.session.add_all([teacher, parent])
    db.session.flush()
    season = Season.query.filter_by(status="active").first()

    def mkcls(name, sh, sm, eh, em, wd=WD):
        c = DanceClass(name=name, day_of_week=wd, start_time=time(sh, sm), end_time=time(eh, em),
                       instructor_id=admin.id, season_id=season.id)
        db.session.add(c)
        db.session.flush()
        return c

    ballet = mkcls("Ballet", 17, 0, 18, 0)
    jazz = mkcls("Jazz", 18, 0, 19, 0)          # back-to-back after ballet
    tap_cls = mkcls("Tap", 19, 30, 20, 30)      # later, with a gap
    hiphop = mkcls("Hip Hop", 17, 0, 18, 0)     # same slot as ballet, other kid
    monday = mkcls("Monday Class", 17, 0, 18, 0, wd=(WD + 5) % 7)

    fam = Family(name="Tester Family")
    db.session.add(fam)
    db.session.flush()

    def kid(fn, classes, family=None):
        s = Student(first_name=fn, last_name="Tester", family_id=family.id if family else None)
        db.session.add(s)
        db.session.flush()
        for c in classes:
            db.session.add(ClassEnrollment(student_id=s.id, class_id=c.id))
        return s

    ava = kid("Ava", [ballet, jazz, tap_cls], fam)
    zoe = kid("Zoe", [ballet], fam)
    max_ = kid("Max", [hiphop])
    lia = kid("Lia", [ballet])
    ned = kid("Ned", [ballet])
    old = kid("Old", [ballet])
    db.session.add(ParentStudent(parent_id=parent.id, student_id=ava.id))
    db.session.commit()
    ids = dict(ava=ava.id, zoe=zoe.id, max=max_.id, lia=lia.id, ned=ned.id, old=old.id,
               ballet=ballet.id, jazz=jazz.id, tap=tap_cls.id, hiphop=hiphop.id,
               monday=monday.id, parent=parent.id, teacher=teacher.id)

    t_ava, ava_tag = make_tag(ids["ava"], uid="04:A2:1B:3C:4D:5E:80")
    t_zoe, _ = make_tag(ids["zoe"])
    t_max, _ = make_tag(ids["max"])
    t_lia, _ = make_tag(ids["lia"])
    t_ned, _ = make_tag(ids["ned"])
    t_lost, _ = make_tag(ids["old"], status="lost")
    t_pending, _ = make_tag(ids["old"], status="pending")
    t_free, _ = make_tag(None)

# ── B. Check-in ─────────────────────────────────────────────────────
with app.app_context():
    r = tap(t_ava, when=at(16, 40))
    record("a tap 20 min before ballet checks Ava in", r.outcome == "checked_in"
           and r.dance_class.id == ids["ballet"], f"got {r.outcome}")
    a = rows(ids["ava"], ids["ballet"])
    record("one present row, method 'tap', stamped at the tap",
           len(a) == 1 and a[0].is_present and a[0].check_in_method == "tap"
           and a[0].check_in_time == at(16, 40) and a[0].check_out_time is None)
    record("the tag token is never stored in the clear",
           NfcTag.query.filter_by(token_hash=t_ava).first() is None
           and NfcTag.query.filter_by(token_hash=hash_token(t_ava)).first() is not None)

    r = tap(t_ava, when=at(16, 40, 6))
    record("a second read 6 s later is a duplicate", r.outcome == "duplicate"
           and r.previous_outcome == "checked_in", f"got {r.outcome}")
    r = tap(t_ava, when=at(16, 42))
    record("a tap 2 min in (under the dwell) is 'already in'", r.outcome == "already_in"
           and r.since == at(16, 40), f"got {r.outcome}")
    record("neither wrote a second attendance row", len(rows(ids["ava"])) == 1)

    r = tap(t_lia, when=at(16, 15))
    record("a tap 45 min early is outside the window -> pick a class",
           r.outcome == "needs_class" and rows(ids["lia"]) == []
           and any(c["id"] == ids["ballet"] and c["enrolled"] for c in r.candidates),
           f"got {r.outcome} {r.candidates}")
    r = tap(t_lia, when=at(16, 15, 30), class_id=ids["ballet"])
    record("staff's class pick checks her in", r.outcome == "checked_in"
           and r.dance_class.id == ids["ballet"] and len(rows(ids["lia"])) == 1)
    try:
        tap(t_lia, when=at(16, 16), class_id=ids["monday"] + 999)
        record("a pick of a class that isn't running is refused", False)
    except TapError:
        record("a pick of a class that isn't running is refused", True)

    r = tap(t_max, when=at(17, 5), source="usb")
    record("a USB tap by token also resolves (Max -> Hip Hop)", r.outcome == "checked_in"
           and r.dance_class.id == ids["hiphop"])
    r = tap(uid="04a21b3c4d5e80", when=at(17, 10))
    record("a USB reader tap by chip UID finds Ava's tag (already in, dwell)",
           r.outcome in ("already_in", "checked_out") and r.student.id == ids["ava"],
           f"got {r.outcome}")

# ── C. Teacher pre-marked P, then the arrival tap ───────────────────
with app.app_context():
    db.session.add(Attendance(student_id=ids["zoe"], class_id=ids["ballet"],
                              check_in_time=at(16, 50), check_in_method="manual",
                              is_present=True, is_tardy=True))
    db.session.commit()
    r = tap(t_zoe, when=at(16, 55))
    z = rows(ids["zoe"])
    record("a teacher's P doesn't turn Zoe's arrival tap into a departure",
           r.outcome == "checked_in" and len(z) == 1 and z[0].check_out_time is None
           and z[0].check_in_method == "tap", f"got {r.outcome}")
    record("and the teacher's T survives (tardy rule is off)", z[0].is_tardy)

    db.session.add(Attendance(student_id=ids["ned"], class_id=ids["ballet"],
                              check_in_time=at(9, 0), check_in_method="manual",
                              is_present=False, is_excused=True))
    db.session.commit()
    r = tap(t_ned, when=at(17, 1))
    n = rows(ids["ned"])
    record("a tap flips an excused row to present", r.outcome == "checked_in" and len(n) == 1
           and n[0].is_present and not n[0].is_excused and not n[0].is_tardy)

# ── D. Check-out and idempotency ────────────────────────────────────
with app.app_context():
    r = tap(t_zoe, when=at(18, 2))
    z = rows(ids["zoe"])
    record("after the dwell, Zoe's tap checks her out of the same row",
           r.outcome == "checked_out" and len(z) == 1 and z[0].check_out_time == at(18, 2))
    r2 = tap(t_zoe, when=at(18, 2, 4))
    record("a re-read right after check-out is a duplicate of the check-out",
           r2.outcome == "duplicate" and r2.previous_outcome == "checked_out")
    r3 = tap(t_zoe, when=at(18, 4))
    record("a later tap with ballet closed is 'already out', with the picker offered",
           r3.outcome == "already_out" and r3.since == at(18, 2) and r3.candidates
           and r3.to_dict()["can_reopen"], f"got {r3.outcome}")

    k = key()
    first = tap(t_max, when=at(18, 3), event_key=k)
    n_events = TapEvent.query.count()
    again = tap(t_max, when=at(18, 3), event_key=k)
    record("replaying an event_key returns the same result and writes nothing",
           again.replayed and again.outcome == first.outcome
           and again.event.id == first.event.id and TapEvent.query.count() == n_events)

    all_events = TapEvent.query.count()
    record("every tap above is in the ledger, one row each (accepted and not)",
           all_events == _calls[0], f"got {all_events} events for {_calls[0]} taps")

# ── E. Refused tags ─────────────────────────────────────────────────
with app.app_context():
    before = Attendance.query.count()
    for label, token, want in (("unknown token", mint_token(), "unknown_tag"),
                               ("lost tag", t_lost, "inactive_tag"),
                               ("pending tag", t_pending, "inactive_tag"),
                               ("unassigned tag", t_free, "unassigned_tag")):
        r = tap(token, when=at(17, 0))
        record(f"{label} is refused as {want}", r.outcome == want, f"got {r.outcome}")
    r = tap("short", when=at(17, 0))
    record("a malformed token never reaches the DB and is unknown", r.outcome == "unknown_tag")
    record("no refused tap wrote attendance", Attendance.query.count() == before)
    lost = tap(t_lost, when=at(17, 1))
    record("a lost tag's message says so", lost.to_dict()["message"] == "This tag was reported lost")

# ── F. Back-to-back rollover, undo ──────────────────────────────────
D2 = DAY + timedelta(days=7)    # next Wednesday, clean slate for Ava
with app.app_context():
    r = tap(t_ava, when=at(16, 55, day=D2))
    record("next week: Ava in to ballet", r.outcome == "checked_in" and r.dance_class.id == ids["ballet"])
    r = tap(t_ava, when=at(20, 40, day=D2))
    on = lambda cid: [x for x in rows(ids["ava"], cid) if x.check_in_time.date() == D2]  # noqa: E731
    b, j, tp = on(ids["ballet"]), on(ids["jazz"]), on(ids["tap"])
    record("leaving after the last class: ballet closes 6:00, jazz 6-7, tap is the departure",
           r.outcome == "checked_out" and r.dance_class.id == ids["tap"]
           and r.rolled_from.id == ids["ballet"]
           and b[0].check_out_time == at(18, 0, day=D2)
           and len(j) == 1 and j[0].check_in_time == at(18, 0, day=D2)
           and j[0].check_out_time == at(19, 0, day=D2)
           and len(tp) == 1 and tp[0].check_in_time == at(19, 30, day=D2)
           and tp[0].check_out_time == at(20, 40, day=D2), f"got {r.outcome}")
    record("rollover never marks the rolled classes tardy", not j[0].is_tardy and not tp[0].is_tardy)

    undo_tap(r.event.id)
    b, j, tp = on(ids["ballet"]), on(ids["jazz"]), on(ids["tap"])
    record("one undo reverses the whole chain: ballet open again, no jazz or tap rows",
           b[0].check_out_time is None and j == [] and tp == [])
    try:
        undo_tap(r.event.id)
        record("undoing twice is refused", False)
    except TapError:
        record("undoing twice is refused", True)

    r = tap(t_ava, when=at(18, 5, day=D2))
    record("tapping just after jazz starts rolls into jazz as a check-in",
           r.outcome == "checked_in" and r.dance_class.id == ids["jazz"]
           and r.rolled_from.id == ids["ballet"], f"got {r.outcome}")
    r = tap(t_ava, when=at(19, 5, day=D2))
    record("tapping between jazz and tap rolls into tap as a check-in",
           r.outcome == "checked_in" and r.dance_class.id == ids["tap"]
           and r.rolled_from.id == ids["jazz"]
           and on(ids["jazz"])[0].check_out_time == at(19, 0, day=D2), f"got {r.outcome}")

    Setting.set("tap_rollover_enabled", "0")
    D3 = D2 + timedelta(days=7)
    tap(t_ava, when=at(16, 55, day=D3))
    r = tap(t_ava, when=at(19, 5, day=D3))
    record("with rollover off, the late tap checks out of ballet",
           r.outcome == "checked_out" and r.dance_class.id == ids["ballet"])
    Setting.set("tap_rollover_enabled", "1")

    D4 = D3 + timedelta(days=7)
    tap(t_ava, when=at(17, 0, day=D4))
    co = tap(t_ava, when=at(17, 30, day=D4))
    re_ = reopen_session(co.event.id)
    a4 = [x for x in rows(ids["ava"], ids["ballet"]) if x.check_in_time.date() == D4]
    record("reopen puts a checked-out dancer back in class",
           re_.outcome == "reopened" and a4[0].check_out_time is None)
    r = tap(t_ava, when=at(17, 31, day=D4))
    record("a tap right after reopening is 'already in' (dwell restarts)",
           r.outcome == "already_in", f"got {r.outcome}")

    D5 = D4 + timedelta(days=7)
    r = tap(t_ava, when=at(17, 0, day=D5))
    undo_tap(r.event.id)
    record("undoing a fresh check-in deletes the row it created",
           [x for x in rows(ids["ava"]) if x.check_in_time.date() == D5] == [])

# ── F2. Undo never erases a later fix on the card ───────────────────
with app.app_context():
    D7 = DAY + timedelta(days=63)
    r = tap(t_ava, when=at(17, 0, day=D7))
    row = [x for x in rows(ids["ava"]) if x.check_in_time.date() == D7][0]
    row.is_tardy = True                 # a teacher marks T after the tap
    db.session.commit()
    try:
        undo_tap(r.event.id)
        record("undo refuses once a teacher changed the row after the tap", False)
    except TapError:
        row = [x for x in rows(ids["ava"]) if x.check_in_time.date() == D7]
        record("undo refuses once a teacher changed the row after the tap",
               len(row) == 1 and row[0].is_tardy and row[0].is_present)
    co = tap(t_ava, when=at(17, 40, day=D7))
    row = [x for x in rows(ids["ava"]) if x.check_in_time.date() == D7][0]
    row.check_out_time = at(17, 45, day=D7)   # a teacher corrects the time
    db.session.commit()
    try:
        undo_tap(co.event.id)
        record("undoing a check-out refuses after a manual correction", False)
    except TapError:
        record("undoing a check-out refuses after a manual correction", True)
    try:
        reopen_session(co.event.id)
        record("put back in class refuses after a manual correction", False)
    except TapError:
        row = [x for x in rows(ids["ava"]) if x.check_in_time.date() == D7][0]
        record("put back in class refuses after a manual correction",
               row.check_out_time == at(17, 45, day=D7))
    D8 = DAY + timedelta(days=70)
    r = tap(t_ava, when=at(17, 0, day=D8))
    db.session.delete([x for x in rows(ids["ava"]) if x.check_in_time.date() == D8][0])
    db.session.commit()                       # teacher cleared the box
    try:
        undo_tap(r.event.id)
        record("undo refuses when the teacher already cleared the box", False)
    except TapError:
        record("undo refuses when the teacher already cleared the box", True)
    D9 = DAY + timedelta(days=77)
    tap(t_ava, when=at(16, 55, day=D9))
    r = tap(t_ava, when=at(20, 40, day=D9))   # rolls ballet -> jazz -> tap
    jz = [x for x in rows(ids["ava"], ids["jazz"]) if x.check_in_time.date() == D9][0]
    jz.is_tardy = True                        # a teacher touches a rolled class
    db.session.commit()
    try:
        undo_tap(r.event.id)
        record("undo of a rollover refuses if any rolled row was changed", False)
    except TapError:
        record("undo of a rollover refuses if any rolled row was changed",
               [x for x in rows(ids["ava"], ids["tap"]) if x.check_in_time.date() == D9] != [])

# ── G. Tardy rule, makeups, deterministic pick ──────────────────────
with app.app_context():
    Setting.set("tap_tardy_grace_minutes", "5")
    D6 = DAY + timedelta(days=42)
    tap(t_lia, when=at(17, 12, day=D6))
    l6 = [x for x in rows(ids["lia"]) if x.check_in_time.date() == D6]
    record("with a 5 min grace, a 5:12 tap marks T", l6 and l6[0].is_tardy)
    tap(t_ned, when=at(17, 3, day=D6))
    n6 = [x for x in rows(ids["ned"]) if x.check_in_time.date() == D6]
    record("and a 5:03 tap does not", n6 and not n6[0].is_tardy)
    Setting.set("tap_tardy_grace_minutes", "0")

    MD = DAY + timedelta(days=5)   # the Monday class's weekday
    db.session.add(MakeupClass(student_id=ids["max"], makeup_class_id=ids["monday"],
                               makeup_date=MD, status="scheduled"))
    db.session.commit()
    r = tap(t_max, when=at(16, 50, day=MD))
    record("a scheduled makeup class is a candidate", r.outcome == "checked_in"
           and r.dance_class.id == ids["monday"], f"got {r.outcome}")

    b = db.session.get(DanceClass, ids["ballet"])
    j = db.session.get(DanceClass, ids["jazz"])
    record("pick_class: in session beats upcoming", pick_class([b, j], at(17, 30)).id == b.id)
    record("pick_class: at the 6:00 boundary the class just starting wins",
           pick_class([b, j], at(18, 0)).id == j.id)
    record("pick_class: before both, the next to start wins",
           pick_class([j, b], at(16, 45)).id == b.id)

# ── G2. A unique-index race retries once, never forever ─────────────
import app.tap.service as tap_service  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402
with app.app_context():
    calls = {"n": 0}
    real = tap_service._upsert_present

    def always_conflict(*a, **kw):
        calls["n"] += 1
        raise IntegrityError("INSERT", {}, Exception("UNIQUE constraint failed"))

    tap_service._upsert_present = always_conflict
    try:
        tap(t_ned, when=at(17, 2, day=DAY + timedelta(days=49)))
        record("a constraint that keeps failing raises instead of recursing", False)
    except TapError:
        record("a constraint that keeps failing raises instead of recursing", calls["n"] == 2,
               f"tried {calls['n']} times")
    finally:
        tap_service._upsert_present = real

# ── H. Flags ────────────────────────────────────────────────────────
from app.tap.flags import flags_for  # noqa: E402
with app.app_context():
    ava = db.session.get(Student, ids["ava"])
    record("no waivers, no balance -> no flags", flags_for(ava, include_money=True) == [])
    db.session.add(WaiverTemplate(title="Liability", body="..."))
    db.session.add(Transaction(student_id=ids["zoe"], type="charge", amount=50,
                               category="tuition", fund="studio", payment_method="n/a",
                               transaction_date=DAY))
    db.session.commit()
    codes = [f["code"] for f in flags_for(ava, include_money=True)]
    record("a sibling's charge flags the household; the unsigned form flags too",
           codes == ["check_account", "forms_needed"], f"got {codes}")
    record("teachers never get the account badge",
           [f["code"] for f in flags_for(ava, include_money=False)] == ["forms_needed"])
    db.session.add(Transaction(student_id=ids["ava"], type="payment", amount=50,
                               category="tuition", fund="studio", payment_method="cash",
                               transaction_date=DAY))
    db.session.commit()
    record("a sibling's payment nets the household to zero",
           "check_account" not in [f["code"] for f in flags_for(ava, include_money=True)])
    labels = " ".join(f["label"] for f in flags_for(ava, include_money=True))
    record("flags never carry a dollar amount", "$" not in labels)

# ── I. Legacy backfill ──────────────────────────────────────────────
with app.app_context():
    legacy_dec = str(int("CAFEF00D" + f"{0xCA ^ 0xFE ^ 0xF0 ^ 0x0D:02X}", 16))
    oldkid = db.session.get(Student, ids["old"])
    oldkid.rfid_uid = legacy_dec
    db.session.commit()
    run_migrations(db)
    run_migrations(db)
    tags = NfcTag.query.filter_by(uid="CAFEF00D").all()
    record("a legacy rfid_uid badge becomes one active tag (idempotent)",
           len(tags) == 1 and tags[0].status == "active" and tags[0].source == "legacy"
           and tags[0].student_id == ids["old"] and tags[0].token_hash is None
           and tags[0].label == label_for(tags[0].id))
    record("the legacy column is untouched", db.session.get(Student, ids["old"]).rfid_uid == legacy_dec)

# ── J. API ──────────────────────────────────────────────────────────
anon = app.test_client()
r = anon.post("/api/tap", json={"event_key": "anon-key-0001", "token": t_zoe})
record("an expired session gets JSON 401, not a login redirect",
       r.status_code == 401 and r.is_json and "login_url" in r.get_json())

mom = app.test_client()
mom.post("/auth/login", data={"username": "mom", "password": "pw12345"})
r = mom.post("/api/tap", json={"event_key": "mom-key-00001", "token": t_ava})
record("a parent can't record a tap", r.status_code == 403)

teach = app.test_client()
teach.post("/auth/login", data={"username": "teach", "password": "pw12345"})
r = teach.post("/api/tap", json={"token": t_ava})
record("event_key is required", r.status_code == 400)
r = teach.post("/api/tap", json={"event_key": "teach-key-0001", "token": t_ava})
body = r.get_json()
record("a teacher records a tap and gets the result screen data",
       r.status_code == 201 and body["outcome"] in ("checked_in", "needs_class", "already_in",
                                                    "checked_out", "already_out")
       and body["student"]["first_name"] == "Ava" and "message" in body
       and isinstance(body["flags"], list), f"got {r.status_code} {body}")
record("the teacher's result has no account badge",
       all(f["code"] != "check_account" for f in body["flags"]))
r = teach.post("/api/tap", json={"event_key": "teach-key-0001", "token": t_ava})
record("replaying the same event_key over the API returns 200 and the same event",
       r.status_code == 200 and r.get_json()["event_id"] == body["event_id"]
       and r.get_json()["replayed"])

r = mom.get("/api/my-data")
kids = r.get_json().get("children", [])
ava_block = next((k for k in kids if k["first_name"] == "Ava"), {})
record("the parent's data export lists Ava's tag and tap log, never a token",
       r.status_code == 200 and ava_block.get("check_in_tags")
       and ava_block["check_in_tags"][0]["label"].startswith("T-")
       and ava_block.get("tap_log") and "token" not in r.get_data(as_text=True).lower(),
       f"got {r.status_code} {list(ava_block)}")
html = app.test_client().get("/privacy").get_data(as_text=True)
record("the privacy page describes check-in tags and check-out times",
       "Check-in tags" in html and "checked out" in html)

adm = app.test_client()
adm.post("/auth/login", data={"username": "admin", "password": "pw12345"})
r = adm.post("/api/tap", json={"event_key": "adm-key-00001", "token": mint_token()})
record("an unknown tag over the API is a 201 with outcome unknown_tag and no student",
       r.status_code == 201 and r.get_json()["outcome"] == "unknown_tag"
       and r.get_json()["student"] is None)
r = adm.post("/api/tap/999999/undo")
record("undo of a missing event is a clean 400", r.status_code == 400)

passed = sum(1 for _, p in results if p)
total = len(results)
print("\n" + "=" * 56)
print(f"SUMMARY: {passed}/{total} passed, {total - passed} failed.")
try:
    os.unlink(_tmp.name)
except OSError:
    pass
sys.exit(0 if passed == total else 1)
