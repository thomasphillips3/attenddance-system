"""Tap-In parent notices: queued with the tap, sent later, never wrong.

What has to hold:

  1. A tap only writes outbox rows; nothing is sent inside the request.
  2. Due rows go out as one email per household: siblings tapping together
     get one message. Wording is first name, class, time, "checked out".
  3. Household opt-out, an undone tap, a stale replay (20+ min late), the
     pause switch and the daily cap all stop a send.
  4. Adults get no notices; a rolled-over check-in sends no "arrival".
  5. SMS needs the studio switch AND the parent's opt-in, goes only to the
     one number that agreed (not the dancer's phone, not another guardian on
     file), and a Twilio STOP (21610) turns the opt-in off for good.
  6. A failing send retries with backoff, then gives up.
  7. Parents control their household from the portal; staff can switch
     notices off but never opt a parent in to texts.

Run:  RFID_ENABLED=false python3 tests/test_tap_notices.py
Exit 0 = all green, 1 = failures.
"""
import os
import sys
import tempfile
from datetime import date, datetime, time, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("RFID_ENABLED", "false")
os.environ["TAP_NOTIFY_WORKER"] = "0"
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp.name}"

from app import create_app, db  # noqa: E402
from app.models import (  # noqa: E402
    ClassEnrollment, DanceClass, Family, NfcTag, ParentStudent, Season, Setting, Student,
    TapNotification, User,
)
from app.tap import outbox  # noqa: E402
from app.tap.credentials import hash_token, label_for, mint_token  # noqa: E402
from app.tap.service import TapRequest, process_tap, undo_tap  # noqa: E402

app = create_app("development")
app.config["TESTING"] = True
app.config["MAIL_SERVER"] = "smtp.example.test"

results = []


def record(name, passed, detail=""):
    results.append((name, passed))
    print(f"[{'PASS' if passed else 'FAIL'}] {name}" + (f" - {detail}" if detail and not passed else ""))


emails, texts = [], []
sms_reply = {"code": None}


def fake_email(to, subject, body, attachments=None):
    if fake_email.fail or to in fake_email.fail_for:
        raise OSError("smtp down")
    emails.append((to, subject, body))
    return 1


fake_email.fail = False
fake_email.fail_for = set()


def fake_sms(to, body):
    texts.append((to, body))
    if sms_reply["code"]:
        return False, sms_reply["code"]
    return True, None


outbox._send_email = fake_email
outbox._send_sms = fake_sms

DAY = date(2026, 10, 14)          # a Wednesday
_n = [0]


def at(h, m, day=DAY):
    return datetime.combine(day, time(h, m))


def tap(token, when):
    _n[0] += 1
    return process_tap(TapRequest(event_key=f"notice-{_n[0]:05d}", token=token, occurred_at=when))


def pending():
    return TapNotification.query.filter_by(status="pending").all()


with app.app_context():
    record("the sender thread is off under the test harness", not outbox.worker_enabled(app))
    admin = User.query.filter_by(username="admin").first()
    admin.set_password("pw12345")
    mom = User(username="mom", email="mom@example.test", role="parent", first_name="Mo", last_name="M")
    mom.set_password("pw12345")
    teach = User(username="teach", email="t@example.test", role="teacher", first_name="T", last_name="T")
    teach.set_password("pw12345")
    db.session.add_all([mom, teach])
    db.session.flush()
    season = Season.query.filter_by(status="active").first()
    ballet = DanceClass(name="Ballet", day_of_week=DAY.weekday(), start_time=time(17, 0),
                        end_time=time(18, 0), instructor_id=admin.id, season_id=season.id)
    jazz = DanceClass(name="Jazz", day_of_week=DAY.weekday(), start_time=time(18, 0),
                      end_time=time(19, 0), instructor_id=admin.id, season_id=season.id)
    db.session.add_all([ballet, jazz])
    db.session.flush()
    fam = Family(name="Notice Family", primary_email="home@example.test", primary_phone="(248) 555-0100",
                 secondary_phone="313-555-0177")
    db.session.add(fam)
    db.session.flush()

    def kid(fn, family=None, **kw):
        s = Student(first_name=fn, last_name="Notice", family_id=family.id if family else None, **kw)
        db.session.add(s)
        db.session.flush()
        for c in (ballet, jazz):
            db.session.add(ClassEnrollment(student_id=s.id, class_id=c.id))
        tok = mint_token()
        t = NfcTag(student_id=s.id, token_hash=hash_token(tok), status="active")
        db.session.add(t)
        db.session.flush()
        t.label = label_for(t.id)
        return s, tok

    ava, t_ava = kid("Ava", fam, parent_email="mom@example.test")
    zoe, t_zoe = kid("Zoe", fam)
    sol, t_sol = kid("Sol", parent_email="sol.parent@example.test", phone="2485550199")
    adu, t_adu = kid("Adele", email="adele@example.test", date_of_birth=date(1990, 1, 1))
    db.session.add_all([ParentStudent(parent_id=mom.id, student_id=ava.id),
                        ParentStudent(parent_id=mom.id, student_id=zoe.id)])
    db.session.commit()
    ids = dict(ava=ava.id, zoe=zoe.id, sol=sol.id, adu=adu.id)

# ── A. Queued, not sent; siblings in one email ──────────────────────
with app.app_context():
    r1 = tap(t_ava, at(16, 55))
    r2 = tap(t_zoe, at(16, 55))
    record("a tap queues a notice and sends nothing inside the request",
           r1.outcome == "checked_in" and len(pending()) == 2 and emails == [])
    sa = {n.send_after for n in pending()}
    record("siblings who tap together share one send time", len(sa) == 1)
    for n in pending():           # the fixture taps are on a fixed past day;
        n.event.occurred_at = datetime.now()   # make them fresh for the send
    db.session.commit()
    outbox.drain_once(now=datetime.now())
    record("nothing goes out before the coalescing delay", emails == [])
    outbox.drain_once(now=datetime.now() + timedelta(minutes=1))
    by_to = {e[0]: e for e in emails}
    record("after it, each parent address gets ONE message",
           sorted(by_to) == ["home@example.test", "mom@example.test"] and len(emails) == 2,
           f"got {[e[0] for e in emails]}")
    subject, body = by_to["home@example.test"][1], by_to["home@example.test"][2]
    record("the household address hears about both siblings, first names only, with the time",
           subject.startswith("Ava and Zoe checked in at") and "Notice" not in subject, subject)
    record("body has the class and an opt-out line, no last names",
           "Ava checked in to Ballet at" in body and "Zoe checked in to Ballet at" in body
           and "turn these off" in body and "Notice" not in body)
    record("an address that's only Ava's contact hears only about Ava",
           by_to["mom@example.test"][1].startswith("Ava checked in at")
           and "Zoe" not in by_to["mom@example.test"][2])
    record("both rows are marked sent with exactly their own recipients",
           {n.student.first_name: (n.status, set(n.recipients.split(", ")))
            for n in TapNotification.query.all()}
           == {"Ava": ("sent", {"mom@example.test", "home@example.test"}),
               "Zoe": ("sent", {"home@example.test"})})

# ── B. Departure wording; adult; opt-out; undo; stale; pause; cap ───
with app.app_context():
    emails.clear()
    tap(t_ava, at(18, 0, DAY + timedelta(days=7)) - timedelta(minutes=60))  # 5:00 next week
    tap(t_ava, at(17, 50, DAY + timedelta(days=7)))
    deps = [n for n in pending() if n.kind == "departure"]
    record("a check-out queues a departure notice", len(deps) == 1)
    for n in pending():
        n.event.occurred_at = datetime.now()  # treat as fresh for the send test
    db.session.commit()
    outbox.drain_once(now=datetime.now() + timedelta(minutes=2))
    record("departure says 'checked out of', never 'picked up'",
           any("Ava checked out of Ballet" in b for _, _, b in emails)
           and not any("picked up" in b.lower() for _, _, b in emails))

    tap(t_adu, at(17, 0))
    record("an adult dancer gets no notice at all",
           TapNotification.query.filter_by(student_id=ids["adu"]).count() == 0)

    s = db.session.get(Student, ids["sol"])
    s.tap_email_opt_out = True
    db.session.commit()
    tap(t_sol, at(17, 0))
    record("an opted-out household gets no row queued",
           TapNotification.query.filter_by(student_id=ids["sol"]).count() == 0)
    s.tap_email_opt_out = False
    db.session.commit()

    D3 = DAY + timedelta(days=14)
    r = tap(t_sol, at(17, 0, D3))
    n = TapNotification.query.filter_by(student_id=ids["sol"], status="pending").one()
    n.event.occurred_at = datetime.now()
    db.session.commit()
    s.tap_email_opt_out = True
    db.session.commit()
    emails.clear()
    outbox.drain_once(now=datetime.now() + timedelta(minutes=1))
    db.session.refresh(n)
    record("an opt-out made after the tap still wins at send time",
           emails == [] and n.status == "skipped" and n.reason == "household opted out")
    s.tap_email_opt_out = False
    db.session.commit()

    D4 = DAY + timedelta(days=21)
    r = tap(t_sol, at(17, 0, D4))
    undo_tap(r.event.id)
    n = TapNotification.query.filter_by(tap_event_id=r.event.id).one()
    record("undoing a tap cancels its notice", n.status == "skipped" and n.reason == "undone")

    D5 = DAY + timedelta(days=28)
    r = tap(t_sol, at(17, 0, D5))
    n = TapNotification.query.filter_by(tap_event_id=r.event.id).one()
    outbox.drain_once(now=r.event.occurred_at + timedelta(minutes=25))
    db.session.refresh(n)
    record("a notice 25 minutes late is skipped, not sent", n.status == "skipped"
           and n.reason == "too late to be useful")

    def fresh_sol(day):
        r = tap(t_sol, at(17, 0, day))
        n = TapNotification.query.filter_by(tap_event_id=r.event.id).one()
        n.event.occurred_at = datetime.now()
        db.session.commit()
        return n

    n = fresh_sol(DAY + timedelta(days=35))
    Setting.set("tap_notify_paused", "1")
    outbox.drain_once(now=datetime.now() + timedelta(minutes=1))
    db.session.refresh(n)
    record("the pause switch stops sends", n.status == "skipped" and n.reason == "notices paused")
    Setting.set("tap_notify_paused", "0")

    n = fresh_sol(DAY + timedelta(days=42))
    Setting.set("tap_notify_daily_cap", "1")
    db.session.add(TapNotification(tap_event_id=n.tap_event_id, student_id=ids["sol"],
                                   household_key="x", kind="arrival", channel="sms",
                                   status="sent", sent_at=datetime.now(),
                                   send_after=datetime.now()))
    db.session.commit()
    outbox.drain_once(now=datetime.now() + timedelta(minutes=1))
    db.session.refresh(n)
    record("the daily cap stops sends", n.status == "skipped" and n.reason == "daily cap reached")
    Setting.set("tap_notify_daily_cap", "400")

    n = fresh_sol(DAY + timedelta(days=49))
    fake_email.fail = True
    t0 = datetime.now() + timedelta(minutes=1)
    outbox.drain_once(now=t0)
    db.session.refresh(n)
    record("a failed send is retried later, not dropped",
           n.status == "pending" and n.attempts == 1 and n.send_after > t0)
    outbox.drain_once(now=n.send_after)
    db.session.refresh(n)
    outbox.drain_once(now=n.send_after)
    db.session.refresh(n)
    record("after 3 tries it's marked failed", n.status == "failed" and n.attempts == 3)
    fake_email.fail = False

# ── C. Rollover: no arrival for the second class ────────────────────
with app.app_context():
    D6 = DAY + timedelta(days=56)
    tap(t_zoe, at(16, 55, D6))
    r = tap(t_zoe, at(18, 5, D6))
    record("a rolled-over check-in queues no arrival",
           r.outcome == "checked_in" and r.rolled_from is not None
           and TapNotification.query.filter_by(tap_event_id=r.event.id).count() == 0)

# ── D. SMS ──────────────────────────────────────────────────────────
with app.app_context():
    Setting.set("sms_enabled", "1")
    Setting.set("sms_twilio_sid", "AC_test")
    Setting.set("sms_twilio_token", "tok_test")
    Setting.set("sms_from_number", "+15555550000")
    Setting.set("tap_notify_email_enabled", "0")
    D7 = DAY + timedelta(days=63)
    tap(t_ava, at(17, 0, D7))
    record("no SMS row without the studio switch AND an opt-in",
           not any(n.channel == "sms" for n in pending()))
    Setting.set("tap_notify_sms_enabled", "1")
    for st in (ids["ava"], ids["zoe"]):
        db.session.get(Student, st).tap_sms_opt_in = True
        db.session.get(Student, st).tap_sms_phone = "+12485550100"
    db.session.commit()
    D8 = DAY + timedelta(days=70)
    r = tap(t_ava, at(17, 0, D8))
    n = TapNotification.query.filter_by(tap_event_id=r.event.id, channel="sms").one()
    n.event.occurred_at = datetime.now()
    db.session.commit()
    texts.clear()
    outbox.drain_once(now=datetime.now() + timedelta(minutes=1))
    record("an opted-in household gets a text at the number that agreed, in E.164",
           texts and texts[0][0] == "+12485550100" and "Ava checked in to Ballet" in texts[0][1]
           and "STOP" in texts[0][1], f"got {texts}")
    record("the second guardian on file is never texted on the first one's consent",
           all(t[0] != "+13135550177" for t in texts) and len(texts) == 1)

    r = tap(t_sol, at(17, 0, D8))
    db.session.get(Student, ids["sol"]).tap_sms_opt_in = True
    db.session.commit()
    texts.clear()
    D9 = DAY + timedelta(days=77)
    r = tap(t_sol, at(17, 0, D9))
    n = TapNotification.query.filter_by(tap_event_id=r.event.id, channel="sms").one()
    n.event.occurred_at = datetime.now()
    db.session.commit()
    outbox.drain_once(now=datetime.now() + timedelta(minutes=1))
    db.session.refresh(n)
    record("never texts the dancer's own phone: no agreed number means skip",
           texts == [] and n.status == "skipped" and n.reason == "no opted-in number")

    D10 = DAY + timedelta(days=84)
    r = tap(t_zoe, at(17, 0, D10))
    n = TapNotification.query.filter_by(tap_event_id=r.event.id, channel="sms").one()
    n.event.occurred_at = datetime.now()
    db.session.commit()
    sms_reply["code"] = 21610
    outbox.drain_once(now=datetime.now() + timedelta(minutes=1))
    db.session.refresh(n)
    record("a Twilio STOP (21610) skips the row and turns the household's SMS off",
           n.status == "skipped" and n.reason == "parent replied STOP"
           and not db.session.get(Student, ids["ava"]).tap_sms_opt_in
           and not db.session.get(Student, ids["zoe"]).tap_sms_opt_in)
    sms_reply["code"] = None
    Setting.set("tap_notify_email_enabled", "1")

# ── D2. Privacy and per-address delivery ────────────────────────────
with app.app_context():
    Setting.set("tap_notify_email_enabled", "1")
    Setting.set("tap_notify_sms_enabled", "0")
    fam2 = Family(name="Split Family")
    db.session.add(fam2)
    db.session.flush()

    def kid2(fn, email):
        s_ = Student(first_name=fn, last_name="Split", family_id=fam2.id, parent_email=email)
        db.session.add(s_)
        db.session.flush()
        db.session.add(ClassEnrollment(student_id=s_.id, class_id=db.session.query(DanceClass.id)
                                       .filter_by(name="Ballet").scalar()))
        tok = mint_token()
        tg = NfcTag(student_id=s_.id, token_hash=hash_token(tok), status="active")
        db.session.add(tg)
        db.session.flush()
        tg.label = label_for(tg.id)
        return s_, tok

    kim, t_kim = kid2("Kim", "kims.dad@example.test")
    lou, t_lou = kid2("Lou", "lous.mom@example.test")
    db.session.commit()
    DP = DAY + timedelta(days=91)
    tap(t_kim, at(16, 55, DP))
    tap(t_lou, at(16, 55, DP))
    for n in pending():
        n.event.occurred_at = datetime.now()
    db.session.commit()
    emails.clear()
    outbox.drain_once(now=datetime.now() + timedelta(minutes=1))
    by_to = {e[0]: e for e in emails}
    record("siblings with different contacts get separate notices",
           set(by_to) == {"kims.dad@example.test", "lous.mom@example.test"}, f"got {list(by_to)}")
    record("each contact only hears about their own dancer",
           "Kim" in by_to["kims.dad@example.test"][2] and "Lou" not in by_to["kims.dad@example.test"][2]
           and "Lou" in by_to["lous.mom@example.test"][2] and "Kim" not in by_to["lous.mom@example.test"][2])

    # With a household address on the family row too: the household hears
    # about both (it is every child's guardian by the contact model), while
    # each child-only address still hears only about its own child.
    fam2.primary_email = "split.home@example.test"
    db.session.commit()
    DP2 = DAY + timedelta(days=112)
    tap(t_kim, at(16, 55, DP2))
    tap(t_lou, at(16, 55, DP2))
    for n in pending():
        n.event.occurred_at = datetime.now()
    db.session.commit()
    emails.clear()
    outbox.drain_once(now=datetime.now() + timedelta(minutes=1))
    by_to = {e[0]: e for e in emails}
    record("mixed contacts: the household address gets one message about both",
           "Kim" in by_to.get("split.home@example.test", ("", "", ""))[2]
           and "Lou" in by_to["split.home@example.test"][2] and len(emails) == 3,
           f"got {[e[0] for e in emails]}")
    record("mixed contacts: child-only addresses still hear only about their own child",
           "Lou" not in by_to["kims.dad@example.test"][2]
           and "Kim" not in by_to["lous.mom@example.test"][2])

    # One address fails: it alone is retried, the other is never re-sent.
    DQ = DAY + timedelta(days=98)
    r = tap(t_ava, at(16, 55, DQ))
    n = TapNotification.query.filter_by(tap_event_id=r.event.id, channel="email").one()
    n.event.occurred_at = datetime.now()
    db.session.commit()
    emails.clear()
    fake_email.fail_for = {"home@example.test"}
    t1 = datetime.now() + timedelta(minutes=1)
    outbox.drain_once(now=t1)
    db.session.refresh(n)
    record("a partial failure keeps the notice pending, remembering who already got it",
           n.status == "pending" and n.attempts == 1 and n.recipients == "mom@example.test"
           and [e[0] for e in emails] == ["mom@example.test"], f"{n.status} {n.recipients} {emails}")
    fake_email.fail_for = set()
    emails.clear()
    outbox.drain_once(now=n.send_after)
    db.session.refresh(n)
    record("the retry goes only to the address that failed, then it's sent",
           [e[0] for e in emails] == ["home@example.test"] and n.status == "sent"
           and set(n.recipients.split(", ")) == {"mom@example.test", "home@example.test"},
           f"{emails} {n.status} {n.recipients}")

    # Texts: siblings opted in on different numbers each get their own text,
    # and one failing number doesn't mark the other child's notice failed.
    Setting.set("tap_notify_email_enabled", "0")
    Setting.set("tap_notify_sms_enabled", "1")
    a_, z_ = db.session.get(Student, ids["ava"]), db.session.get(Student, ids["zoe"])
    a_.tap_sms_opt_in, a_.tap_sms_phone = True, "+12485550100"
    z_.tap_sms_opt_in, z_.tap_sms_phone = True, "+15865550188"
    db.session.commit()
    DR = DAY + timedelta(days=105)
    ra, rz = tap(t_ava, at(16, 55, DR)), tap(t_zoe, at(16, 55, DR))
    na = TapNotification.query.filter_by(tap_event_id=ra.event.id, channel="sms").one()
    nz = TapNotification.query.filter_by(tap_event_id=rz.event.id, channel="sms").one()
    for x in (na, nz):
        x.event.occurred_at = datetime.now()
    db.session.commit()
    texts.clear()
    real_sms = outbox._send_sms
    outbox._send_sms = lambda to, body: (texts.append((to, body)) or ((False, 30003) if to == "+15865550188" else (True, None)))
    outbox.drain_once(now=datetime.now() + timedelta(minutes=1))
    outbox._send_sms = real_sms
    db.session.refresh(na)
    db.session.refresh(nz)
    record("each sibling's text goes only to their own opted-in number",
           sorted(t[0] for t in texts) == ["+12485550100", "+15865550188"]
           and all(("Ava" in b) != ("Zoe" in b) for _, b in texts))
    record("a failed number is retried; the delivered one is marked sent",
           na.status == "sent" and nz.status == "pending" and nz.attempts == 1,
           f"{na.status} {nz.status}")
    Setting.set("tap_notify_email_enabled", "1")   # section E expects texts still on

# ── E. Portal and staff controls ────────────────────────────────────
pc = app.test_client()
pc.post("/auth/login", data={"username": "mom", "password": "pw12345"})
r = pc.get("/api/my-notices")
d = r.get_json()
record("a parent sees their own dancers' settings and no other guardian's number",
       r.status_code == 200 and {c["first_name"] for c in d["children"]} == {"Ava", "Zoe"}
       and "0177" not in r.get_data(as_text=True))
r = pc.post("/api/my-notices", json={"email": False})
record("a parent turns email off for the household",
       r.status_code == 200 and all(not c["email_on"] for c in r.get_json()["children"]))
r = pc.post("/api/my-notices", json={"sms": True})
record("turning on texts without the consent box is refused", r.status_code == 400)
r = pc.post("/api/my-notices", json={"sms": True, "consent": True})
record("turning on texts without a number is refused", r.status_code == 400)
r = pc.post("/api/my-notices", json={"sms": True, "consent": True, "phone": "12345"})
record("a number that isn't a real phone is refused", r.status_code == 400)
r = pc.post("/api/my-notices", json={"sms": True, "consent": True, "phone": "(586) 555-0123"})
record("a number that hasn't been verified with a texted code is refused", r.status_code == 400)
import app.api.tap_routes as tap_routes  # noqa: E402
codes = []
tap_routes._code_sender = lambda to, body: (codes.append((to, body)) or (True, None))
r = pc.post("/api/my-notices/code", json={"phone": "(586) 555-0123"})
record("the portal texts a 6-digit code to the number", r.status_code == 200 and codes
       and codes[0][0] == "+15865550123", f"got {r.status_code} {r.get_json()}")
import re as _re  # noqa: E402
code = _re.search(r"\b(\d{6})\b", codes[0][1]).group(1)
wrong = "000000" if code != "000000" else "111111"
r = pc.post("/api/my-notices", json={"sms": True, "consent": True, "phone": "5865550123", "code": wrong})
record("a wrong code is refused", r.status_code == 400)
r = pc.post("/api/my-notices", json={"sms": True, "consent": True, "phone": "(586) 555-0199", "code": code})
record("a code only works for the number it was sent to", r.status_code == 400)
r = pc.post("/api/my-notices", json={"sms": True, "consent": True, "phone": "(586) 555-0123", "code": code})
record("with consent, a number and its code, texts turn on for the household", r.status_code == 200
       and all(c["sms_on"] and c["sms_numbers"] == ["...0123"] for c in r.get_json()["children"]),
       f"got {r.status_code} {r.get_json()}")
r = pc.post("/api/my-notices", json={"sms": True, "consent": True, "phone": "(586) 555-0123", "code": code})
record("a used code can't be replayed", r.status_code == 400)
for _ in range(3):
    pc.post("/api/my-notices/code", json={"phone": "5865550123"})
r = pc.post("/api/my-notices/code", json={"phone": "5865550123"})
record("codes are rate-limited per account", r.status_code == 429)

# One account can text any one number at most 3 times a day.
with app.app_context():
    from app.models import SmsVerification  # noqa: E402
    SmsVerification.query.delete()
    db.session.commit()
spam = app.test_client()
spam.post("/auth/login", data={"username": "mom", "password": "pw12345"})
import app.api.tap_routes as _tr  # noqa: E402
_saved = _tr.CODES_PER_USER_PER_HOUR
_tr.CODES_PER_USER_PER_HOUR = 100   # isolate the per-number cap
statuses = [spam.post("/api/my-notices/code", json={"phone": "7345550111"}).status_code for _ in range(4)]
_tr.CODES_PER_USER_PER_HOUR = _saved
record("one account can text the same number only 3 times a day",
       statuses == [200, 200, 200, 429], f"got {statuses}")

# One account spamming a number can't lock its owner out.
with app.app_context():
    for i in range(2):
        u = User(username=f"other{i}", email=f"other{i}@example.test", role="parent",
                 first_name="O", last_name=str(i))
        u.set_password("pw12345")
        db.session.add(u)
    db.session.commit()
codes.clear()
o0 = app.test_client()
o0.post("/auth/login", data={"username": "other0", "password": "pw12345"})
r = o0.post("/api/my-notices/code", json={"phone": "3135550123"})
record("another account can request a code for a fresh number", r.status_code == 200)
mom2 = app.test_client()
mom2.post("/auth/login", data={"username": "mom", "password": "pw12345"})
with app.app_context():
    from app.models import SmsVerification  # noqa: E402
    SmsVerification.query.filter_by(user_id=db.session.query(User.id).filter_by(username="mom").scalar()).delete()
    db.session.commit()
r = mom2.post("/api/my-notices/code", json={"phone": "3135550123"})
record("one other account's requests don't lock the owner out of their number", r.status_code == 200,
       f"got {r.status_code} {r.get_json()}")

# Parallel-guess race: attempts are claimed atomically, so a code allows at
# most CODE_MAX_ATTEMPTS guesses no matter how requests interleave.
with app.app_context():
    v = SmsVerification.query.filter_by(phone="+13135550123").order_by(SmsVerification.id.desc()).first()
    v.attempts = tap_routes.CODE_MAX_ATTEMPTS
    db.session.commit()
right = _re.search(r"\b(\d{6})\b", codes[-1][1]).group(1)
r = mom2.post("/api/my-notices", json={"sms": True, "consent": True, "phone": "3135550123", "code": right})
record("a code that used up its guesses is dead even with the right digits", r.status_code == 400)
r = mom2.get("/api/my-data")
record("the parent export lists the numbers they asked us to verify",
       any(x["phone"] == "+13135550123" for x in r.get_json().get("phone_verifications", [])))
with app.app_context():
    a = db.session.get(Student, ids["ava"])
    record("and who agreed, when, and which number is recorded",
           a.tap_sms_opt_in_by is not None and a.tap_sms_opt_in_at is not None
           and a.tap_sms_phone == "+15865550123")
    Setting.set("tap_notify_sms_enabled", "0")
pc.post("/api/my-notices", json={"sms": False})
r = pc.post("/api/my-notices", json={"sms": True, "consent": True})
record("texts can't be turned on while the studio has them off", r.status_code == 400)
r = pc.post("/api/my-notices", json={"email": True})
record("email back on", r.status_code == 200 and all(c["email_on"] for c in r.get_json()["children"]))

tc = app.test_client()
tc.post("/auth/login", data={"username": "teach", "password": "pw12345"})
r = tc.put(f"/api/students/{ids['ava']}/tap-notices", json={"sms": True})
record("staff can't opt a parent in to texts", r.status_code == 400)
r = tc.put(f"/api/students/{ids['ava']}/tap-notices", json={"email": False})
with app.app_context():
    record("staff can switch email off for the household (a parent asked)",
           r.status_code == 200 and db.session.get(Student, ids["zoe"]).tap_email_opt_out)
r = pc.put(f"/api/students/{ids['ava']}/tap-notices", json={"email": True})
record("a parent can't use the staff endpoint", r.status_code == 403)

ac = app.test_client()
ac.post("/auth/login", data={"username": "admin", "password": "pw12345"})
r = ac.put("/api/tap/settings", json={"tap_notify_daily_cap": 0})
record("a daily cap of 0 is refused", r.status_code == 400)
r = ac.put("/api/tap/settings", json={"tap_notify_paused": True, "tap_notify_daily_cap": 300})
record("an admin pauses notices and sets the cap",
       r.status_code == 200 and r.get_json()["tap_notify_paused"] is True
       and r.get_json()["tap_notify_daily_cap"] == 300)
r = pc.get("/api/my-data")
kid = next(k for k in r.get_json()["children"] if k["first_name"] == "Ava")
record("the parent export includes notice settings and every notice sent",
       "checkin_notice_settings" in kid and kid["checkin_notices_sent"])
html = pc.get("/parent").get_data(as_text=True)
record("the parent portal has the check-in notices card", "Check-in notices" in html)
html = app.test_client().get("/privacy").get_data(as_text=True)
record("the privacy page covers check-in notices and SMS consent",
       "Check-in notices:" in html and "check-in texts" in html)

passed = sum(1 for _, p in results if p)
total = len(results)
print("\n" + "=" * 56)
print(f"SUMMARY: {passed}/{total} passed, {total - passed} failed.")
try:
    os.unlink(_tmp.name)
except OSError:
    pass
sys.exit(0 if passed == total else 1)
