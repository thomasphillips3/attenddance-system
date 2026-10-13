"""Family credit harness for AttenDANCE.

A parent paid two siblings' tuition in one payment. The credit sat on one
child's ledger and the other still owed, so the studio's balance reminder
told the family a child owed $49 when the household owed nothing. These tests
prove the two fixes:

  1. Reminders are household-level: sibling balances net per fund before
     anyone is told they owe, one message goes to the household, and a
     Company credit never hides a studio debt.
  2. An admin can move credit between siblings (append-only, audited,
     same-fund, capped at the credit the giver actually holds), and the
     revenue report does not count the offsetting rows as money collected.

Run:  RFID_ENABLED=false python3 tests/test_family_credit.py
Exit 0 = all green, 1 = failures.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("RFID_ENABLED", "false")
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp.name}"

from app import create_app, db  # noqa: E402
from app import email as email_service  # noqa: E402
from app import sms as sms_service  # noqa: E402
from app.api import routes as api_routes  # noqa: E402
from app.funds import fund_for_category  # noqa: E402
from app.helpers import calc_balance, family_reminder_groups, calc_balance_bulk  # noqa: E402
from app.models import AuditLog, Family, Student, Transaction, User  # noqa: E402

app = create_app("development")
app.config["TESTING"] = True

results = []


def record(name, passed, detail=""):
    results.append((name, passed))
    print(f"[{'PASS' if passed else 'FAIL'}] {name}" + (f" - {detail}" if detail and not passed else ""))


def post_txn(sid, type_, amount, category="tuition", fund=None):
    with app.app_context():
        db.session.add(Transaction(
            student_id=sid, type=type_, amount=amount, category=category,
            fund=fund_for_category(category, fund),
            payment_method="cash" if type_ == "payment" else "n/a", description="t"))
        db.session.commit()


def new_family(name, kids, email=None):
    """A household with `kids` dancers, no money history. Returns (family_id, [ids])."""
    with app.app_context():
        fam = Family(name=name, primary_email=email)
        db.session.add(fam)
        db.session.flush()
        ids = []
        for k in kids:
            s = Student(first_name=k, last_name=name, family_id=fam.id)
            db.session.add(s)
            db.session.flush()
            ids.append(s.id)
        db.session.commit()
        return fam.id, ids


def login_admin(c):
    return c.post("/auth/login", data={"username": "creditadmin", "password": "pw"},
                  follow_redirects=True)


class Capture:
    """Swap the mail and SMS senders for recorders; restore on exit."""
    def __enter__(self):
        self.sent, self.texts = [], []
        self._real = (email_service.send_email, email_service.is_configured,
                      sms_service.send_sms, sms_service.is_configured)
        email_service.send_email = lambda to, subject, body, attachments=None: self.sent.append((to, subject, body)) or 1
        email_service.is_configured = lambda: True
        sms_service.send_sms = lambda phone, body: self.texts.append((phone, body)) or True
        sms_service.is_configured = lambda: False
        return self

    def __exit__(self, *a):
        (email_service.send_email, email_service.is_configured,
         sms_service.send_sms, sms_service.is_configured) = self._real


def groups_for(ids):
    with app.app_context():
        students = Student.query.filter(Student.id.in_(ids)).order_by(Student.id).all()
        return family_reminder_groups(students, calc_balance_bulk(ids))


def test_grouping():
    """The household nets per fund; funds never net against each other."""
    fam, (a, b) = new_family("Net", ["Ann", "Bo"])
    post_txn(a, "charge", 49)            # Ann owes 49
    post_txn(b, "payment", 49)           # Bo holds a 49 credit (the lump payment)
    (g,) = groups_for([a, b])
    record("siblings group into one household", len(g["students"]) == 2)
    record("a credit on one sibling cancels a charge on the other",
           g["funds"]["studio"]["balance"] == 0.0, str(g["funds"]))
    # Company debt must not be hidden by a studio credit.
    fam2, (c, d) = new_family("Wall", ["Cy", "Di"])
    post_txn(c, "payment", 100, "tuition")                    # studio credit
    post_txn(d, "charge", 40, "competition")                  # company debt
    (g2,) = groups_for([c, d])
    record("studio credit does not hide a company debt",
           g2["funds"]["studio"]["balance"] == -100.0 and g2["funds"]["company"]["balance"] == 40.0, str(g2["funds"]))
    # A dancer with no family stands alone.
    with app.app_context():
        s = Student(first_name="Solo", last_name="Kid")
        db.session.add(s)
        db.session.commit()
        sid = s.id
    post_txn(sid, "charge", 10)
    (g3,) = groups_for([sid])
    record("a dancer with no family is their own group",
           len(g3["students"]) == 1 and g3["funds"]["studio"]["balance"] == 10.0)


def test_manual_reminders():
    """Send Reminders: a netted household is skipped, a real balance gets ONE email."""
    famA, (a1, a2) = new_family("Netted", ["Ann", "Bo"], email="netted@x.com")
    post_txn(a1, "charge", 49)
    post_txn(a2, "payment", 49)
    famB, (b1, b2) = new_family("Owes", ["Cy", "Di"], email="owes@x.com")
    post_txn(b1, "charge", 30)
    post_txn(b2, "charge", 20)
    with Capture() as cap, app.test_client() as c:
        login_admin(c)
        # Run the worker inline: the route only starts a thread around it.
        api_routes._send_reminders_to(app, True, False)
    mine = [m for m in cap.sent if any(x in ("netted@x.com", "owes@x.com") for x in m[0])]
    to_netted = [m for m in mine if "netted@x.com" in m[0]]
    to_owes = [m for m in mine if "owes@x.com" in m[0]]
    record("a family that nets to zero gets no reminder", to_netted == [], str(to_netted))
    record("a family that owes gets exactly one email, not one per child", len(to_owes) == 1, str(len(to_owes)))
    if to_owes:
        body = to_owes[0][2]
        record("the email lists the household total (30 + 20)", "$50.00" in body, body)
        record("the email names the family, not one child", "Owes family" in body or "the Owes family" in body, body)
        record("subject has no em dash", "—" not in to_owes[0][1], to_owes[0][1])


def test_single_reminder_button():
    fam, (a, b) = new_family("Single", ["Ann", "Bo"], email="single@x.com")
    post_txn(a, "charge", 49)
    post_txn(b, "payment", 49)
    with Capture() as cap, app.test_client() as c:
        login_admin(c)
        r = c.post(f"/api/students/{a}/send-reminder")
        record("the per-student reminder refuses when the household nets to zero",
               r.status_code == 400 and not cap.sent, f"{r.status_code} {r.get_json()}")
        post_txn(a, "charge", 10)
        r = c.post(f"/api/students/{a}/send-reminder")
        record("and sends once the household really owes", r.status_code == 200 and len(cap.sent) == 1,
               f"{r.status_code} {r.get_json()}")
        record("the amount is the household's $10, not Ann's $59",
               cap.sent and "$10.00" in cap.sent[0][2] and "$59" not in cap.sent[0][2], cap.sent[0][2] if cap.sent else "")


def test_auto_reminders():
    fam, (a, b) = new_family("Auto", ["Ann", "Bo"], email="auto@x.com")
    post_txn(a, "charge", 49)
    post_txn(b, "payment", 49)
    from app import _send_balance_reminders
    with Capture() as cap:
        _send_balance_reminders(app)
    record("auto-reminders skip a household that nets to zero",
           not [m for m in cap.sent if "auto@x.com" in m[0]], str(cap.sent))
    post_txn(b, "charge", 15)
    with Capture() as cap:
        _send_balance_reminders(app)
    got = [m for m in cap.sent if "auto@x.com" in m[0]]
    record("and email once, on the net, when the household owes", len(got) == 1 and "$15.00" in got[0][2], str(got))


def test_credit_transfer():
    fam, (a, b) = new_family("Move", ["Ann", "Bo"])
    post_txn(a, "charge", 49)
    post_txn(b, "payment", 49)
    other_fam, (z,) = new_family("Other", ["Zed"])
    with app.test_client() as c:
        login_admin(c)
        url = f"/api/families/{fam}/credit-transfer"
        r = c.post(url, json={"from_student_id": b, "to_student_id": a, "amount": 49, "note": "Oct for both"})
        body = r.get_json() or {}
        record("credit-transfer -> 201 with a reference", r.status_code == 201 and body.get("reference", "").startswith("sib-"), str(body))
        ref = body.get("reference", "")
        with app.app_context():
            ba, bb = calc_balance(a)["studio"]["balance"], calc_balance(b)["studio"]["balance"]
            record("both children land at zero", ba == 0.0 and bb == 0.0, f"{ba} {bb}")
            rows = Transaction.query.filter_by(category="transfer").filter(Transaction.student_id.in_([a, b])).all()
            kinds = sorted((t.student_id == b and "giver" or "taker", t.type) for t in rows)
            record("a charge on the giver and a payment on the taker, nothing else",
                   kinds == [("giver", "charge"), ("taker", "payment")], str(kinds))
            record("both rows carry the reference and the note",
                   all(ref in (t.description or "") and "Oct for both" in t.description for t in rows))
            audit = AuditLog.query.filter_by(action="family.credit_transfer").order_by(AuditLog.id.desc()).first()
            record("an audit entry carries the reference", audit is not None and ref in (audit.detail or ""))
            original = Transaction.query.filter_by(student_id=b, category="tuition", type="payment").one()
            record("the original payment row is untouched", float(original.amount) == 49.0)
        r = c.post(url, json={"from_student_id": b, "to_student_id": a, "amount": 1})
        record("moving credit the giver no longer has -> 400", r.status_code == 400, str(r.get_json()))
        r = c.post(url, json={"from_student_id": a, "to_student_id": a, "amount": 1})
        record("same student -> 400", r.status_code == 400)
        r = c.post(url, json={"from_student_id": b, "to_student_id": z, "amount": 1})
        record("a student from another family -> 400", r.status_code == 400, str(r.get_json()))
        r = c.post(url, json={"from_student_id": b, "to_student_id": a, "amount": 0})
        record("amount <= 0 -> 400", r.status_code == 400)
        r = c.post(url, json={"from_student_id": b, "to_student_id": a, "amount": 5, "fund": "bogus"})
        record("unknown fund -> 400", r.status_code == 400)
        # Fund wall: studio credit cannot be moved as Company credit.
        post_txn(b, "payment", 20)   # fresh 20 studio credit on Bo
        r = c.post(url, json={"from_student_id": b, "to_student_id": a, "amount": 20, "fund": "company"})
        record("cannot move studio credit as company credit", r.status_code == 400, str(r.get_json()))
        r = c.post(url, json={"from_student_id": b, "to_student_id": a, "amount": 20})
        record("the same credit moves fine in its own fund", r.status_code == 201)
        # Family ledger exposes per-student balances for the modal.
        d = c.get(f"/api/families/{fam}/ledger").get_json()
        record("family ledger returns each student's per-fund balances",
               all("balances" in s and set(s["balances"]) == {"studio", "company"} for s in d["students"]), str(d["students"]))
        page = c.get(f"/families/{fam}/ledger").get_data(as_text=True)
        record("family ledger page renders the credit-move modal", 'id="credit-modal"' in page and "credit-transfer" in page)
        # Revenue ignores the offsetting rows.
        rev = c.get("/api/reports/revenue").get_json()
        collected = rev["totals"]["collected_all_time"]
        with app.app_context():
            real = sum(float(t.amount) for t in Transaction.query.filter_by(type="payment") if t.category != "transfer")
        record("revenue does not count transfer rows as collected", abs(collected - real) < 0.005, f"{collected} vs {real}")
        record("or list transfer in collected-by-category",
               "transfer" not in [r_["category"] for r_ in rev["by_category"]], str(rev["by_category"]))
    # Non-admins cannot call it.
    with app.app_context():
        t = User(username="creditteacher", email="ct@x.com", first_name="T", last_name="T",
                 role="teacher", is_admin=False, is_active=True)
        t.set_password("pw")
        db.session.add(t)
        db.session.commit()
    with app.test_client() as c2:
        c2.post("/auth/login", data={"username": "creditteacher", "password": "pw"}, follow_redirects=True)
        r = c2.post(f"/api/families/{fam}/credit-transfer", json={"from_student_id": b, "to_student_id": a, "amount": 1})
        record("a teacher cannot move credit", r.status_code in (401, 403), str(r.status_code))


def main():
    with app.app_context():
        db.create_all()
        admin = User(username="creditadmin", email="ca@x.com", first_name="Credit", last_name="Admin",
                     role="admin", is_admin=True, is_active=True)
        admin.set_password("pw")
        db.session.add(admin)
        db.session.commit()
    for t in (test_grouping, test_manual_reminders, test_single_reminder_button,
              test_auto_reminders, test_credit_transfer):
        try:
            t()
        except Exception as e:  # a crash is a failure, not a skipped test
            import traceback
            traceback.print_exc()
            record(f"{t.__name__} ran without crashing", False, repr(e))
    failed = [n for n, ok in results if not ok]
    print("\n" + "=" * 56)
    print(f"SUMMARY: {len(results) - len(failed)}/{len(results)} passed, {len(failed)} failed.")
    for n in failed:
        print(f"  FAILED: {n}")
    sys.exit(1 if failed else 0)


main()
