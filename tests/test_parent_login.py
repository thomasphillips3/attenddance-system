"""Automatic parent logins on registration approval.

Approving an enrollment used to create the family and the dancers but no
account, so a parent had no way into the portal until an admin generated an
invite code per dancer and texted them the link by hand. In practice most
families never got one. Approval now sets the login up and emails it.

What has to hold:

  1. Approving a new family creates exactly ONE pending account for the
     household, linked to EVERY dancer in it - not one account per dancer.
  2. The emailed link actually works: redeeming it produces an active login
     carrying all of that family's dancers.
  3. A family that already logs in gets no second account and no invite email.
  4. A second registration before the first invite is redeemed reuses it rather
     than minting a rival account.
  5. The email is best-effort. It goes to the address on the registration, and
     neither an unconfigured SMTP server nor a failing one can break approval -
     the admin gets the link to send by hand instead.

Run:  RFID_ENABLED=false python3 tests/test_parent_login.py
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
from app.models import (  # noqa: E402
    ParentStudent, Registration, Setting, Student, User,
)

app = create_app("development")
app.config["TESTING"] = True
app.config["WTF_CSRF_ENABLED"] = False
app.config["SERVER_NAME"] = "attenddance.test"

results = []


def record(name, passed, detail=""):
    results.append((name, passed))
    print(f"[{'PASS' if passed else 'FAIL'}] {name}" + (f" - {detail}" if detail and not passed else ""))


def dancer(first, last="Newfam", **over):
    s = {"first_name": first, "last_name": last, "dob": "2015-04-02", "allergies": "None"}
    s.update(over)
    return s


def payload(email, **over):
    body = {
        "parent_name": "Dana Newfam", "parent_email": email,
        "parent_phone": "313-555-0100",
        "emergency_name": "Emergency Contact", "emergency_phone": "313-555-0911",
        "students": [dancer("Ava")],
    }
    body.update(over)
    return body


sent = []


class FakeSMTP:
    """Captures sends instead of talking to a server."""
    fail = False

    def __init__(self, *a, **kw):
        if FakeSMTP.fail:
            raise smtplib.SMTPConnectError(421, "nope")

    def starttls(self):
        pass

    def login(self, *a):
        pass

    def sendmail(self, sender, addr, msg):
        sent.append((addr, msg))

    def quit(self):
        pass


def wait_for_send(n=1, timeout=3.0):
    """The invite email is fire-and-forget on a thread; give it a moment."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and len(sent) < n:
        time.sleep(0.05)
    return len(sent)


def approve_latest(parent_email):
    with app.app_context():
        reg = (Registration.query.filter_by(parent_email=parent_email, status="pending")
               .order_by(Registration.id.desc()).first())
        rid = reg.id
    return staff.post(f"/api/registrations/{rid}/approve")


with app.app_context():
    admin = User.query.filter_by(role="admin").first()
    if not admin:
        admin = User(username="admin", email="admin@example.test", role="admin",
                     first_name="Admin", last_name="User", is_admin=True)
        db.session.add(admin)
    admin.set_password("pw12345")
    Setting.set("registration_open", "1")
    db.session.commit()

pub = app.test_client()
staff = app.test_client()
r = staff.post("/auth/login", data={"username": "admin", "password": "pw12345"},
               follow_redirects=True)
record("admin login for API calls", r.status_code == 200, f"got {r.status_code}")

real_smtp = smtplib.SMTP
smtplib.SMTP = FakeSMTP
app.config["MAIL_SERVER"] = "smtp.example.test"

try:
    # ── 1. One account per household, covering every dancer ─────────
    pub.post("/api/register", json=payload(
        "newfam@x.com",
        students=[dancer("Ava"), dancer("Zoe"), dancer("Kai")]))
    res = approve_latest("newfam@x.com").get_json() or {}
    record("approve succeeds", "portal_invite_url" in res, f"got {res}")
    invite_url = res.get("portal_invite_url")
    record("approval hands back a portal invite link", bool(invite_url), f"got {invite_url}")

    with app.app_context():
        kids = Student.query.filter_by(last_name="Newfam").all()
        kid_ids = {k.id for k in kids}
        record("all three dancers were created", len(kids) == 3, f"got {len(kids)}")
        invites = (User.query.filter_by(role="parent", is_active=False)
                   .filter(User.invite_code.isnot(None)).all())
        record("exactly ONE pending account for the household, not one per dancer",
               len(invites) == 1, f"got {len(invites)}")
        if invites:
            linked = {ps.student_id for ps in
                      ParentStudent.query.filter_by(parent_id=invites[0].id).all()}
            record("the pending account covers every dancer in the family",
                   linked == kid_ids, f"linked {len(linked)} of {len(kid_ids)}")

    # ── 5a. The email goes out, to the address on the registration ──
    wait_for_send(1)
    # The admin "new registration" notice lands in the same capture list, so
    # pick ours out by the code rather than trusting an index.
    code = invite_url.rsplit("=", 1)[-1]
    invite_mails = [(to, body) for to, body in sent if code in body]
    record("invite email was sent", len(invite_mails) == 1,
           f"captured {[t for t, _ in sent]}")
    if invite_mails:
        to, body = invite_mails[0]
        record("emailed to the registering parent", to == "newfam@x.com", f"got {to}")
        record("the email explains what the login is for",
               "balance" in body and "attendance" in body, "body is missing the what-you-get lines")
        record("approval message says the login went out",
               "emailed" in (res.get("message") or ""), f"got {res.get('message')}")
        record("response records that it was emailed",
               res.get("portal_invite_emailed") is True)

    # ── 2. The link redeems into a real login with every dancer ─────
    code = invite_url.rsplit("=", 1)[-1]
    page = pub.get(f"/auth/register?code={code}")
    record("the invite link opens the sign-up form with the code filled in",
           f'value="{code}"' in page.get_data(as_text=True), "code not prefilled")

    redeem = pub.post("/auth/register", data={
        "invite_code": code, "first_name": "Dana", "last_name": "Newfam",
        "email": "newfam@x.com", "password": "portal123"}, follow_redirects=True)
    record("redeeming the invite succeeds", redeem.status_code == 200,
           f"got {redeem.status_code}")
    with app.app_context():
        parent = User.query.filter_by(email="newfam@x.com").first()
        record("the parent now has an active login", parent is not None
               and parent.is_active and parent.is_parent,
               f"got {parent}")
        if parent:
            linked = {ps.student_id for ps in
                      ParentStudent.query.filter_by(parent_id=parent.id).all()}
            record("every dancer is on that one login", linked == kid_ids,
                   f"linked {len(linked)} of {len(kid_ids)}")
            record("the redeemed code is spent", parent.invite_code is None)

    # ── 3. A family that already logs in gets nothing new ───────────
    # `pub` is now signed in as that parent (redeeming logs you in), which is
    # exactly the state a returning parent is in when they open /register to add
    # a sibling. Submitting must work for them, not 403.
    sent.clear()
    sib = pub.post("/api/register", json=payload(
        "newfam@x.com", students=[dancer("Ava"), dancer("Newbie")]))
    record("a signed-in parent can still use the public enrollment form",
           sib.status_code == 201, f"got {sib.status_code} {sib.get_json()}")
    res2 = approve_latest("newfam@x.com").get_json() or {}
    record("re-registration by a family with a login creates no second invite",
           res2.get("portal_invite_url") is None, f"got {res2.get('portal_invite_url')}")
    record("and sends them no invite email", wait_for_send(1, 0.6) == 0,
           f"sent {len(sent)}")
    with app.app_context():
        record("still exactly one parent account for the household",
               User.query.filter_by(role="parent").count() == 1,
               f"got {User.query.filter_by(role='parent').count()}")
        newbie = Student.query.filter_by(first_name="Newbie").first()
        parent = User.query.filter_by(email="newfam@x.com").first()
        record("the new sibling is attached to the existing login",
               ParentStudent.query.filter_by(parent_id=parent.id,
                                             student_id=newbie.id).first() is not None)

    # ── 4. An unredeemed invite is reused, never duplicated ─────────
    sent.clear()
    pub.post("/api/register", json=payload("second@x.com", parent_name="Sam Second",
                                           students=[dancer("Bee", "Second")]))
    first = approve_latest("second@x.com").get_json() or {}
    pub.post("/api/register", json=payload("second@x.com", parent_name="Sam Second",
                                           students=[dancer("Bee", "Second"),
                                                     dancer("Cee", "Second")]))
    again = approve_latest("second@x.com").get_json() or {}
    record("a second approval reuses the unredeemed invite",
           first.get("portal_invite_url") == again.get("portal_invite_url"),
           f"{first.get('portal_invite_url')} vs {again.get('portal_invite_url')}")
    with app.app_context():
        pend = (User.query.filter_by(role="parent", is_active=False)
                .filter(User.invite_code.isnot(None)).all())
        record("no rival pending account was minted", len(pend) == 1, f"got {len(pend)}")
        sec_ids = {s.id for s in Student.query.filter_by(last_name="Second").all()}
        linked = {ps.student_id for ps in
                  ParentStudent.query.filter_by(parent_id=pend[0].id).all()} if pend else set()
        record("the later sibling was added to the same pending invite",
               linked == sec_ids, f"linked {len(linked)} of {len(sec_ids)}")

    # ── 5b. A failing SMTP server must not break approval ───────────
    sent.clear()
    FakeSMTP.fail = True
    pub.post("/api/register", json=payload("smtpdown@x.com", parent_name="Sara Down",
                                           students=[dancer("Dee", "Down")]))
    res3 = approve_latest("smtpdown@x.com")
    record("approval still succeeds when the mail server is down",
           res3.status_code == 200, f"got {res3.status_code}")
    with app.app_context():
        record("the dancer was still created", Student.query.filter_by(
            first_name="Dee", last_name="Down").count() == 1)
        record("the login was still created for the studio to send by hand",
               (res3.get_json() or {}).get("portal_invite_url") is not None)
    FakeSMTP.fail = False

finally:
    smtplib.SMTP = real_smtp

# ── 5c. No SMTP configured: hand the admin the link ─────────────────
app.config["MAIL_SERVER"] = None
pub.post("/api/register", json=payload("nosmtp@x.com", parent_name="Nate NoSmtp",
                                       students=[dancer("Eve", "Nosmtp")]))
res4 = approve_latest("nosmtp@x.com").get_json() or {}
record("approval succeeds with no mail server configured",
       res4.get("portal_invite_url") is not None, f"got {res4}")
record("the admin is told to send the link themselves",
       "send them this link yourself" in (res4.get("message") or ""),
       f"got {res4.get('message')}")
record("and it is not reported as emailed",
       res4.get("portal_invite_emailed") is False)

with app.app_context():
    Setting.set("registration_open", "0")
    db.session.commit()

passed = sum(1 for _, p in results if p)
total = len(results)
print("\n" + "=" * 56)
print(f"SUMMARY: {passed}/{total} passed, {total - passed} failed.")
sys.exit(0 if passed == total else 1)
