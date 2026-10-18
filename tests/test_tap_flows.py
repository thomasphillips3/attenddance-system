"""Tap-In flows around the core: tag setup, the tag link page, the station.

What has to hold:

  1. Tag setup: mint a pending tag, refuse a chip that is someone else's
     active tag, activate only when the URL read back matches, enroll by
     serial for the USB reader, lose / replace / reactivate / delete, and
     lock only once the permanent tag address is saved.
  2. /t/<token>: for anyone who isn't staff the page is byte-identical for a
     valid, unknown and revoked token, and the token is never looked up. For
     staff, GET records nothing; the page's POST does. Responses are
     no-store and never leak the token in a Referer.
  3. Pick-a-class works from a past result without the token.
  4. Offline replay: in order, at the time the tap happened, idempotent,
     bounded (a day old at most, future clamped, 200 per batch).
  5. Settings are admin-only and validated; changing the tag address with
     locked tags needs an explicit confirm.
  6. The new pages render and the service worker is served from the root.

Run:  RFID_ENABLED=false python3 tests/test_tap_flows.py
Exit 0 = all green, 1 = failures.
"""
import os
import re
import sys
import tempfile
import time as _time
from datetime import date, datetime, time, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("RFID_ENABLED", "false")
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp.name}"

from app import create_app, db  # noqa: E402
from app.models import (  # noqa: E402
    Attendance, ClassEnrollment, DanceClass, NfcTag, ParentStudent, Season, Setting, Student,
    TapEvent, User,
)
from app.tap.credentials import hash_token, label_for, mint_token  # noqa: E402

app = create_app("development")
app.config["TESTING"] = True

results = []


def record(name, passed, detail=""):
    results.append((name, passed))
    print(f"[{'PASS' if passed else 'FAIL'}] {name}" + (f" - {detail}" if detail and not passed else ""))


_n = [0]


def key(prefix="flow"):
    _n[0] += 1
    return f"{prefix}-{_n[0]:06d}-{int(_time.time())}"


# ── Fixture: a class running right now, so live taps check in ───────
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
    now = datetime.now()
    start = (now - timedelta(minutes=10)).time().replace(second=0, microsecond=0)
    end = (now + timedelta(minutes=50)).time().replace(second=0, microsecond=0)
    if end <= start:  # don't straddle midnight
        start, end = time(0, 0), time(23, 59)
    cls = DanceClass(name="Now Class", day_of_week=date.today().weekday(), start_time=start,
                     end_time=end, instructor_id=admin.id, season_id=season.id)
    db.session.add(cls)
    db.session.flush()
    kids = {}
    for fn in ("Ava", "Ben", "Cam", "Dee", "Eve"):
        s = Student(first_name=fn, last_name="Flow")
        db.session.add(s)
        db.session.flush()
        db.session.add(ClassEnrollment(student_id=s.id, class_id=cls.id))
        kids[fn] = s.id
    db.session.add(ParentStudent(parent_id=parent.id, student_id=kids["Ava"]))
    db.session.commit()
    cid, admin_id, season_id = cls.id, admin.id, season.id


def make_tag(sid, status="active", uid=None):
    with app.app_context():
        tok = mint_token()
        t = NfcTag(student_id=sid, token_hash=hash_token(tok), status=status, uid=uid)
        db.session.add(t)
        db.session.flush()
        t.label = label_for(t.id)
        db.session.commit()
        return tok, t.id


def login(user):
    c = app.test_client()
    c.post("/auth/login", data={"username": user, "password": "pw12345"})
    return c


adm, teach, mom, anon = login("admin"), login("teach"), login("mom"), app.test_client()

# ── A. Tag setup ────────────────────────────────────────────────────
r = teach.post(f"/api/students/{kids['Ava']}/tags", json={"kind": "card"})
d = r.get_json()
record("a teacher mints a pending tag and gets its URL once",
       r.status_code == 201 and d["status"] == "pending" and d["kind"] == "card"
       and "/t/" in d["url"] and d["label"] == label_for(d["id"]), f"got {r.status_code} {d}")
ava_pending_id, ava_url = d["id"], d["url"]
ava_token = ava_url.rsplit("/t/", 1)[1]
with app.app_context():
    t = db.session.get(NfcTag, ava_pending_id)
    record("only the token's hash is stored", t.token_hash == hash_token(ava_token)
           and ava_token not in (t.token_hash or ""))
r = teach.get(f"/api/students/{kids['Ava']}/tags")
record("listing tags never returns a token or URL",
       r.status_code == 200 and "url" not in r.get_json()["tags"][0]
       and ava_token not in r.get_data(as_text=True))

r = teach.post(f"/api/tags/{ava_pending_id}/activate",
               json={"uid": "04:11:22:33:44:55:66", "url": "https://x.test/t/" + mint_token()})
record("activation refuses a URL that doesn't match the tag (write didn't land)",
       r.status_code == 400)
r = teach.post(f"/api/tags/{ava_pending_id}/activate",
               json={"uid": "04:11:22:33:44:55:66", "url": ava_url})
record("activation with the URL read back makes it active and stores the serial",
       r.status_code == 200 and r.get_json()["status"] == "active"
       and r.get_json()["uid"] == "04112233445566", f"got {r.status_code} {r.get_json()}")

r = teach.post("/api/tags/check", json={"student_id": kids["Ben"], "uid": "04112233445566"})
record("check refuses Ava's active chip for Ben, and names the owner by first name only",
       r.status_code == 200 and r.get_json()["ok"] is False and "Ava" in r.get_json()["error"]
       and "Flow" not in r.get_json()["error"])
r = teach.post("/api/tags/check", json={"student_id": kids["Ben"], "url": ava_url})
record("check also refuses Ava's tag by the URL already on it", r.get_json()["ok"] is False)
r = teach.post("/api/tags/check", json={"student_id": kids["Ben"], "uid": "04AA"})
record("check refuses a serial that isn't a real UID length", r.get_json()["ok"] is False)
r = teach.post("/api/tags/check", json={"student_id": kids["Ben"], "uid": "04:99:88:77:66:55:44"})
record("check passes a blank tag", r.get_json()["ok"] is True
       and r.get_json()["uid"] == "04998877665544")
r = teach.post(f"/api/students/{kids['Ben']}/tags", json={"uid": "04112233445566"})
record("minting with someone else's active chip is a 409", r.status_code == 409)
r = teach.post(f"/api/students/{kids['Ben']}/tags/uid", json={"uid": "04112233445566"})
record("serial-only enrollment of Ava's chip for Ben is refused too", r.status_code == 409)

r = teach.post(f"/api/students/{kids['Ben']}/tags/uid", json={"uid": "0a:b1:c2:d3", "kind": "card"})
ben_uid_tag = r.get_json()
record("serial-only enrollment (USB reader) makes an active, token-less tag",
       r.status_code == 201 and ben_uid_tag["status"] == "active" and not ben_uid_tag["has_token"]
       and ben_uid_tag["uid"] == "0AB1C2D3")
r = teach.post(f"/api/students/{kids['Ben']}/tags/uid", json={"uid": "123"})
record("a junk serial is refused", r.status_code == 400)

r = teach.post(f"/api/tags/{ava_pending_id}/status", json={"status": "lost"})
record("marking Ava's tag lost", r.status_code == 200 and r.get_json()["status"] == "lost")
r = teach.post(f"/api/students/{kids['Cam']}/tags/uid", json={"uid": "04112233445566"})
record("a lost tag's chip can be re-enrolled for someone else (found and recycled)",
       r.status_code == 201, f"got {r.status_code} {r.get_json()}")
with app.app_context():
    old = db.session.get(NfcTag, ava_pending_id)
    record("and the lost row gives up the serial and its token but keeps its history",
           old.uid is None and old.token_hash is None and old.status == "lost"
           and old.student_id == kids["Ava"])

r = teach.post(f"/api/tags/{ava_pending_id}/replace", json={})
rep = r.get_json()
record("replace mints a pending replacement for the same dancer",
       r.status_code == 201 and rep["new"]["status"] == "pending"
       and rep["new"]["student_id"] == kids["Ava"] and "/t/" in rep["new"]["url"]
       and rep["old"]["status"] == "lost")
r = teach.delete(f"/api/tags/{rep['new']['id']}")
record("an unfinished setup can be deleted", r.status_code == 200)
r = teach.delete(f"/api/tags/{ben_uid_tag['id']}")
record("an active tag can't be deleted (retire it instead)", r.status_code == 400)
r = teach.post(f"/api/tags/{ava_pending_id}/status", json={"status": "active"})
record("a lost tag with nothing left to identify it can't be reactivated",
       r.status_code == 400, f"got {r.status_code} {r.get_json()}")

tok_ava, ava_tag = make_tag(kids["Ava"])
r = adm.post(f"/api/tags/{ava_tag}/lock")
record("locking is refused until the permanent tag address is saved",
       r.status_code == 400 and "address" in r.get_json()["error"])
r = teach.put("/api/tap/settings", json={"tap_base_url": "https://tags.example.test"})
record("a teacher can't change Tap-In settings", r.status_code == 403)
for bad in ("ftp://x.test", "https://x.test/path", "notaurl"):
    r = adm.put("/api/tap/settings", json={"tap_base_url": bad})
    record(f"tag address {bad!r} is refused", r.status_code == 400)
r = adm.put("/api/tap/settings", json={"tap_base_url": "https://Tags.Example.test/"})
record("an admin saves the permanent tag address (normalized)",
       r.status_code == 200 and r.get_json()["tap_base_url"] == "https://tags.example.test")
r = adm.post(f"/api/tags/{ava_tag}/lock")
record("now an active tag can be marked locked", r.status_code == 200 and r.get_json()["locked"])
r = adm.put("/api/tap/settings", json={"tap_base_url": "https://other.example.test"})
record("changing the address with locked tags needs an explicit confirm",
       r.status_code == 409 and r.get_json().get("needs_confirm"))
r = adm.put("/api/tap/settings", json={"tap_base_url": "https://other.example.test",
                                       "confirm_break_locked": True})
record("and goes through when confirmed", r.status_code == 200)
adm.put("/api/tap/settings", json={"tap_base_url": "https://tags.example.test",
                                   "confirm_break_locked": True})
r = adm.put("/api/tap/settings", json={"tap_dedup_seconds": 1})
record("a 1 second double-tap window is refused", r.status_code == 400)
r = adm.put("/api/tap/settings", json={"tap_dwell_minutes": "abc"})
record("a non-number setting is refused", r.status_code == 400)
r = adm.put("/api/tap/settings", json={"tap_tardy_grace_minutes": 7, "tap_rollover_enabled": False})
record("settings save", r.status_code == 200 and r.get_json()["tap_tardy_grace_minutes"] == 7
       and r.get_json()["tap_rollover_enabled"] is False)
adm.put("/api/tap/settings", json={"tap_tardy_grace_minutes": 0, "tap_rollover_enabled": True})

r = teach.get("/api/tags/missing")
m = r.get_json()
names = [s["name"] for s in m["students"]]
record("the missing-tag roster lists dancers with no active tag",
       r.status_code == 200 and "Dee Flow" in names and "Eve Flow" in names
       and "Ava Flow" not in names and "Ben Flow" not in names, f"got {names}")

# ── B. /t/<token> ───────────────────────────────────────────────────
tok_rev, rev_id = make_tag(kids["Dee"], status="retired")
pages = {}
for label, tok in (("valid", tok_ava), ("unknown", mint_token()), ("revoked", tok_rev)):
    for who, client in (("anon", anon), ("parent", mom)):
        r = client.get(f"/t/{tok}")
        pages[(label, who)] = (r.status_code, r.get_data())
bodies = {v for v in pages.values()}
record("non-staff get byte-identical pages for valid, unknown and revoked tokens",
       len(bodies) == 1 and next(iter(bodies))[0] == 200, f"{len(bodies)} distinct")
record("the generic page names no dancer and holds no token",
       b"Ava" not in pages[("valid", "anon")][1] and tok_ava.encode() not in pages[("valid", "anon")][1])
r = anon.get(f"/t/{tok_ava}")
record("tag pages are no-store, noindex, and send no Referer",
       "no-store" in r.headers.get("Cache-Control", "")
       and r.headers.get("Referrer-Policy") == "no-referrer"
       and "noindex" in r.headers.get("X-Robots-Tag", ""))

with app.app_context():
    before = TapEvent.query.count()
r = teach.get(f"/t/{tok_ava}")
with app.app_context():
    after = TapEvent.query.count()
record("a staff GET of the tag page records nothing (prefetch-safe)",
       r.status_code == 200 and after == before and b"/api/tap" in r.get_data())
record("the staff page doesn't put the dancer's name in the HTML either",
       b"Ava" not in r.get_data())

tok_pend, pend_id = make_tag(kids["Eve"], status="pending")
r = teach.get(f"/t/{tok_pend}")
record("a pending tag's page offers Finish setup for the dancer", b"Finish setup" in r.get_data()
       and b"Eve" in r.get_data())
r = teach.post(f"/api/tags/{pend_id}/activate", json={"token": mint_token()})
record("finish setup refuses the wrong token", r.status_code == 400)
r = teach.post(f"/api/tags/{pend_id}/activate", json={"token": tok_pend})
record("finish setup with the tag's own token activates it",
       r.status_code == 200 and r.get_json()["status"] == "active")

r = teach.post("/api/tap", json={"event_key": key(), "token": tok_ava, "source": "url"})
first = r.get_json()
record("the page's POST checks Ava in", r.status_code == 201 and first["outcome"] == "checked_in"
       and first["class"]["id"] == cid, f"got {r.status_code} {first}")
r = teach.get(f"/tap-in/result/{first['event_id']}")
record("the result URL renders for staff without recording", r.status_code == 200)
r = mom.get(f"/tap-in/result/{first['event_id']}")
record("a parent can't open a result URL", r.status_code in (302, 403))
r = mom.get(f"/api/tap/{first['event_id']}")
record("or read a tap over the API", r.status_code == 403)

# ── C. Pick a class from a past result ──────────────────────────────
with app.app_context():
    other = DanceClass(name="Other Class", day_of_week=date.today().weekday(),
                       start_time=time(0, 0), end_time=time(0, 30), instructor_id=admin_id,
                       season_id=season_id)
    db.session.add(other)
    db.session.commit()
    other_id = other.id
tok_eve = tok_pend
with app.app_context():
    Setting.set("tap_early_minutes", "0")
    c = db.session.get(DanceClass, cid)
    c.start_time = (datetime.now() + timedelta(minutes=90)).time().replace(second=0, microsecond=0) \
        if datetime.now().hour < 21 else time(23, 58)
    c.end_time = time(23, 59)
    db.session.commit()
r = teach.post("/api/tap", json={"event_key": key(), "token": tok_eve})
nc = r.get_json()
record("with no class in its window, Eve's tap asks staff to pick",
       nc["outcome"] == "needs_class" and any(x["id"] == cid for x in nc["candidates"]),
       f"got {nc['outcome']}")
r = teach.post(f"/api/tap/{nc['event_id']}/pick", json={"class_id": cid})
record("pick needs an event_key", r.status_code == 400)
k = key("pick")
r = teach.post(f"/api/tap/{nc['event_id']}/pick", json={"event_key": k, "class_id": cid})
pk = r.get_json()
record("picking from the result checks her in, no token needed",
       r.status_code == 201 and pk["outcome"] == "checked_in" and pk["class"]["id"] == cid)
r = teach.post(f"/api/tap/{nc['event_id']}/pick", json={"event_key": k, "class_id": cid})
record("a double-submitted pick is idempotent", r.status_code == 200 and r.get_json()["replayed"])
with app.app_context():
    Setting.set("tap_early_minutes", "30")
    c = db.session.get(DanceClass, cid)
    c.start_time, c.end_time = start, end
    db.session.commit()

# ── D. Offline replay ───────────────────────────────────────────────
tok_ben, _ = make_tag(kids["Ben"])
ms = lambda dt: int(dt.timestamp() * 1000)  # noqa: E731
t_in = datetime.now() - timedelta(minutes=8)
t_out = datetime.now() - timedelta(minutes=1)
k_in, k_out = key("kiosk"), key("kiosk")
batch = {"taps": [
    {"event_key": k_out, "token": tok_ben, "occurred_at": ms(t_out), "source": "kiosk"},  # out of order
    {"event_key": k_in, "token": tok_ben, "occurred_at": ms(t_in), "source": "kiosk"},
    {"event_key": key("kiosk"), "uid": "0AB1C2D3", "occurred_at": ms(datetime.now() - timedelta(days=2))},
    {"event_key": key("kiosk"), "token": tok_ben, "occurred_at": "soon"},
    {"event_key": "bad key!", "token": tok_ben, "occurred_at": ms(t_in)},
]}
r = teach.post("/api/tap/batch", json=batch)
res = {x["event_key"]: x for x in r.get_json()["results"]}
record("replay runs in the order taps happened: in, then out",
       res[k_in]["outcome"] == "checked_in" and res[k_out]["outcome"] == "checked_out",
       f"got {res.get(k_in)} {res.get(k_out)}")
with app.app_context():
    a = Attendance.query.filter_by(student_id=kids["Ben"]).first()
    record("and stamps attendance with when they happened, not when they arrived",
           a.check_in_time == t_in.replace(microsecond=0)
           and a.check_out_time == t_out.replace(microsecond=0))
bad = [x for x in r.get_json()["results"] if not x["ok"]]
record("a tap older than a day, a missing time and a bad key are refused, not recorded",
       len(bad) == 3 and any("older than a day" in (x.get("error") or "") for x in bad))
record("refused replays are settled (retry false) so the station can drop them",
       all(x.get("retry") is False for x in bad))
with app.app_context():
    old_ev = TapEvent.query.filter(TapEvent.outcome == "replay_refused",
                                   TapEvent.detail.like("older than a day%")).first()
    record("a too-old replay is logged for staff at the time it happened, with the dancer",
           old_ev is not None and old_ev.student_id == kids["Ben"]
           and old_ev.occurred_at.date() < date.today(),
           f"got {old_ev and (old_ev.student_id, old_ev.occurred_at, old_ev.tag_id)} ben={kids['Ben']}")
html = teach.get("/tap-in/today").get_data(as_text=True)
record("and it shows under Taps that need a look today", "too old to record" in html)
with app.app_context():
    for i in range(60):
        db.session.add(TapEvent(event_key=f"bulk-refused-{i:03d}", outcome="replay_refused",
                                occurred_at=datetime.now() - timedelta(days=3),
                                source="kiosk", detail="older than a day"))
    db.session.commit()
    total_refused = TapEvent.query.filter_by(outcome="replay_refused").count()
html = teach.get("/tap-in/today").get_data(as_text=True)
record("every refused replay is listed, with a count (no 50-row cutoff)",
       html.count("too old to record") == total_refused
       and re.search(r"Taps that need a look \((\d+)\)", html)
       and int(re.search(r"Taps that need a look \((\d+)\)", html).group(1)) >= total_refused,
       f"listed {html.count('too old to record')} of {total_refused}")
import app.api.tap_routes as _tr  # noqa: E402
from app.tap.service import TapError as _TE  # noqa: E402
_real = _tr.process_tap


def _flaky(req):
    raise _TE("Attendance changed at the same time. Tap again.", retryable=True)


_tr.process_tap = _flaky
kf = key("kiosk")
r = teach.post("/api/tap/batch", json={"taps": [{"event_key": kf, "token": tok_ben,
                                                 "occurred_at": ms(datetime.now())}]})
_tr.process_tap = _real
x = r.get_json()["results"][0]
record("a transient failure tells the station to keep the tap and retry",
       x["ok"] is False and x["retry"] is True)
_tr.process_tap = lambda req: (_ for _ in ()).throw(_TE("Attendance changed at the same time. Tap again.", retryable=True))
r = teach.post("/api/tap", json={"event_key": key("live"), "token": tok_ben})
_tr.process_tap = _real
record("a live tap that hits a conflict is a 409 with retry, not a dead end",
       r.status_code == 409 and r.get_json()["retry"] is True)
r = teach.post("/api/tap", json={"event_key": key("live"), "token": tok_ben, "class_id": 999999})
record("a tap refused for good (class not running) is a 400 with retry false",
       r.status_code == 400 and r.get_json()["retry"] is False)
js = open("app/static/js/tap-station.js").read()
record("the live station queues a tap on a server error or a retryable answer",
       "out.res.status >= 500 || out.data.retry === true" in js)
record("the station only drops taps that were recorded or settled",
       "x.ok || x.retry === false" in js and "chunk.map(c => c.event_key)" not in js)
n_before = None
with app.app_context():
    n_before = TapEvent.query.count()
r = teach.post("/api/tap/batch", json=batch)
with app.app_context():
    record("re-sending the same batch records nothing new", TapEvent.query.count() == n_before)
r = teach.post("/api/tap/batch", json={"taps": [{"event_key": key("kiosk"), "token": tok_ben,
                                                 "occurred_at": ms(datetime.now() + timedelta(hours=3))}]})
with app.app_context():
    ev = TapEvent.query.order_by(TapEvent.id.desc()).first()
    record("a station clock in the future is clamped to server time",
           abs((ev.occurred_at - datetime.now()).total_seconds()) < 60)
r = teach.post("/api/tap/batch", json={"taps": [{}] * 201})
record("batches over 200 are refused", r.status_code == 400)
r = teach.post("/api/tap/batch", json={"taps": "nope"})
record("a non-list batch is refused", r.status_code == 400)
r = anon.post("/api/tap/batch", json={"taps": []})
record("an expired session replaying gets JSON 401 (the queue keeps the taps)",
       r.status_code == 401 and r.is_json)
r = mom.post("/api/tap/batch", json={"taps": []})
record("a parent can't replay taps", r.status_code == 403)
r = teach.get("/api/tap/ping")
record("ping answers a signed-in station", r.status_code == 200 and r.get_json()["ok"])
r = anon.get("/api/tap/ping")
record("ping tells a signed-out station with 401", r.status_code == 401)

# ── D2. Staff-only photo ────────────────────────────────────────────
import base64  # noqa: E402
JPEG = "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xff\xe0" + b"0" * 200).decode()
r = teach.post(f"/api/students/{kids['Ava']}/photo", json={"photo": JPEG})
record("staff save a dancer's check-in photo", r.status_code == 200)
r = teach.post(f"/api/students/{kids['Ava']}/photo",
               json={"photo": "data:image/jpeg;base64," + base64.b64encode(b"<svg>").decode()})
record("a file that isn't really a JPEG is refused", r.status_code == 400)
r = teach.post(f"/api/students/{kids['Ava']}/photo",
               json={"photo": "data:image/svg+xml;base64,PHN2Zz4="})
record("SVG (scriptable) is refused", r.status_code == 400)
big = "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xff" + b"0" * 400_000).decode()
r = teach.post(f"/api/students/{kids['Ava']}/photo", json={"photo": big})
record("a photo over 300 KB is refused", r.status_code == 400)
r = teach.get(f"/api/students/{kids['Ava']}/photo")
record("staff can load it, never cached", r.status_code == 200 and r.mimetype == "image/jpeg"
       and "no-store" in r.headers.get("Cache-Control", ""))
r = mom.get(f"/api/students/{kids['Ava']}/photo")
record("a parent can't load the staff photo endpoint", r.status_code == 403)
r = teach.post("/api/tap", json={"event_key": key(), "token": tok_ava})
record("the tap result carries the photo URL", r.get_json().get("photo_url", "").startswith(
       f"/api/students/{kids['Ava']}/photo"))
r = mom.get("/api/my-data")
ava_block = next(k for k in r.get_json()["children"] if k["first_name"] == "Ava")
record("the parent's data export includes the photo", ava_block.get("checkin_photo") == JPEG)
r = teach.delete(f"/api/students/{kids['Ava']}/photo")
record("staff can remove it", r.status_code == 200
       and teach.get(f"/api/students/{kids['Ava']}/photo").status_code == 404)

# ── E. Pages ────────────────────────────────────────────────────────
for url in ("/tap-in", "/tap-in/station", "/tap-in/today",
            f"/tap-in/today?date={date.today().isoformat()}", "/tap-in/today?date=garbage",
            f"/students/{kids['Ava']}/detail"):
    r = teach.get(url)
    record(f"{url} renders for staff", r.status_code == 200, f"got {r.status_code}")
for url in ("/tap-in", "/tap-in/station", "/tap-in/today"):
    r = mom.get(url)
    record(f"{url} bounces a parent", r.status_code == 302)
html = teach.get(f"/students/{kids['Ava']}/detail").get_data(as_text=True)
record("the student page carries the tag card", 'id="tags"' in html and "tagCard(" in html)
html = teach.get("/tap-in/today").get_data(as_text=True)
record("today's page lists Ben's tap session", "Ben Flow" in html)
r = anon.get("/sw.js")
record("the service worker is served from the root with root scope allowed",
       r.status_code == 200 and r.headers.get("Service-Worker-Allowed") == "/"
       and b"NOT RECORDED" in r.get_data())

passed = sum(1 for _, p in results if p)
total = len(results)
print("\n" + "=" * 56)
print(f"SUMMARY: {passed}/{total} passed, {total - passed} failed.")
try:
    os.unlink(_tmp.name)
except OSError:
    pass
sys.exit(0 if passed == total else 1)
