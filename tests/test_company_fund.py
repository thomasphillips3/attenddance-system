"""Company fund harness for AttenDANCE.

The LSODance Company collects to the Foundation's accounts; the studio
collects tuition to its own. These tests prove the fund wall holds at every
layer: tagging on write, the idempotent backfill, the single category map,
the dropdowns that render from it, per-fund balances and allocation, per-fund
late fees, and the Company-visibility helper.

Run:  RFID_ENABLED=false python3 tests/test_company_fund.py
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

import sqlalchemy  # noqa: E402

from app import create_app, db  # noqa: E402
from app.funds import (  # noqa: E402
    CATEGORY_FUND, COMPANY_CATEGORIES, fund_for_category,
)
from app.helpers import (  # noqa: E402
    allocate_family_payment, calc_balance, calc_balance_bulk, has_company_activity_bulk,
)
from app.migrations import run_migrations  # noqa: E402
from app.models import (  # noqa: E402
    CompanyMembership, Family, PerformanceGroup, Setting, Student, Transaction, User,
)

app = create_app("development")
app.config["TESTING"] = True

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
results = []


def record(name, passed, detail=""):
    results.append((name, passed))
    print(f"[{'PASS' if passed else 'FAIL'}] {name}" + (f" - {detail}" if detail and not passed else ""))


def seed():
    with app.app_context():
        fam = Family(name="Fund Family")
        db.session.add(fam)
        db.session.flush()
        a = Student(first_name="Ava", last_name="Fund", family_id=fam.id)
        b = Student(first_name="Ben", last_name="Fund", family_id=fam.id)
        db.session.add_all([a, b])
        db.session.flush()
        admin = User(username="fundadmin", email="fa@x.com", first_name="Fund", last_name="Admin",
                     role="admin", is_admin=True, is_active=True)
        admin.set_password("pw")
        db.session.add(admin)
        db.session.commit()
        return {"a": a.id, "b": b.id, "fam": fam.id}


def login_admin(c):
    return c.post("/auth/login", data={"username": "fundadmin", "password": "pw"},
                  follow_redirects=True)


def new_student(first, family_id=None):
    """A fresh dancer with no money history, so a test starts from zero."""
    with app.app_context():
        s = Student(first_name=first, last_name="Wall", family_id=family_id)
        db.session.add(s)
        db.session.commit()
        return s.id


def post_txn(sid, type_, amount, category="tuition", fund=None):
    """Write one row straight to the DB; `fund` follows the category unless given."""
    with app.app_context():
        db.session.add(Transaction(
            student_id=sid, type=type_, amount=amount, category=category,
            fund=fund_for_category(category, fund),
            payment_method="cash" if type_ == "payment" else "n/a", description="t"))
        db.session.commit()


def test_tagging(ids):
    """Test 1: a charge's fund follows its category; a payment's explicit fund
    wins; a bogus fund is rejected."""
    with app.test_client() as c:
        login_admin(c)
        r = c.post("/api/transactions", json={
            "type": "charge", "student_id": ids["a"], "amount": 40,
            "category": "competition"})
        record("competition charge is tagged company",
               r.status_code == 201 and r.get_json().get("fund") == "company", str(r.get_json()))
        r = c.post("/api/transactions", json={
            "type": "charge", "student_id": ids["a"], "amount": 40,
            "category": "tuition"})
        record("tuition charge is tagged studio",
               r.status_code == 201 and r.get_json().get("fund") == "studio", str(r.get_json()))
        r = c.post("/api/transactions", json={
            "type": "payment", "student_id": ids["a"], "amount": 10,
            "category": "tuition", "payment_method": "cash", "fund": "company"})
        record("explicit fund=company on a payment wins over the category",
               r.status_code == 201 and r.get_json().get("fund") == "company", str(r.get_json()))
        r = c.post("/api/transactions", json={
            "type": "payment", "student_id": ids["a"], "amount": 10,
            "category": "tuition", "payment_method": "cash", "fund": "bogus"})
        record("fund=bogus is a 400", r.status_code == 400, str(r.get_json()))
        with app.app_context():
            stored = {t.fund for t in Transaction.query.filter_by(student_id=ids["a"]).all()}
            record("stored rows carry the funds the API reported",
                   stored == {"studio", "company"}, str(stored))


def test_backfill_idempotent(ids):
    """Test 2: rows with an empty fund heal from their category on the next
    boot, a second boot touches nothing, and the fund index exists."""
    with app.app_context():
        rows = [
            Transaction(student_id=ids["b"], type="charge", amount=10, category="tuition",
                        fund="", payment_method="n/a", description="legacy"),
            Transaction(student_id=ids["b"], type="charge", amount=10, category="competition",
                        fund="", payment_method="n/a", description="legacy"),
            Transaction(student_id=ids["b"], type="charge", amount=10, category="late fee",
                        fund="", payment_method="n/a", description="legacy"),
        ]
        db.session.add_all(rows)
        db.session.commit()
        legacy_ids = [t.id for t in rows]
        db.session.expire_all()

        run_migrations(db)
        db.session.expire_all()
        got = [db.session.get(Transaction, i).fund for i in legacy_ids]
        record("backfill: tuition -> studio, competition -> company, late fee -> studio",
               got == ["studio", "company", "studio"], str(got))

        def snapshot():
            return [(t.id, t.fund, t.category, str(t.amount))
                    for t in Transaction.query.order_by(Transaction.id).all()]
        before = snapshot()
        try:
            run_migrations(db)
            ok = True
        except Exception as e:  # noqa: BLE001
            ok = False
            record("backfill: second run raises", False, str(e))
        db.session.expire_all()
        after = snapshot()
        record("backfill: second run is a no-op (identical rows, no error)",
               ok and before == after, f"{before} != {after}")

        names = {ix["name"] for ix in sqlalchemy.inspect(db.engine).get_indexes("transactions")}
        record("ix_transactions_fund index exists", "ix_transactions_fund" in names, str(names))


def test_map_is_single_source():
    """Test 3: the map covers every legacy category plus the Company ones, and
    fund_for_category resolves explicit / unknown values the documented way."""
    legacy = {"tuition", "costumes", "shoes", "registration", "other"}
    record("CATEGORY_FUND covers the legacy studio categories and every company category",
           set(CATEGORY_FUND) >= legacy | set(COMPANY_CATEGORIES), str(set(CATEGORY_FUND)))
    record("every legacy category maps to studio",
           all(CATEGORY_FUND[c] == "studio" for c in legacy), str({c: CATEGORY_FUND[c] for c in legacy}))
    record("fund_for_category('late fee', 'company') == 'company'",
           fund_for_category("late fee", "company") == "company")
    record("fund_for_category('unknown') == 'studio'",
           fund_for_category("unknown") == "studio")
    record("fund_for_category('competition') == 'company'",
           fund_for_category("competition") == "company")


def test_dropdowns_render_from_map():
    """Test 4: no template carries its own category list; both money pages
    render from the FUND_CATEGORIES jinja global."""
    for rel in ("app/templates/transactions/list.html", "app/templates/transactions/ledger.html"):
        with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
            text = fh.read()
        record(f"{rel}: no hardcoded option value=\"tuition\"", 'option value="tuition"' not in text)
        record(f"{rel}: references FUND_CATEGORIES", "FUND_CATEGORIES" in text)
    with app.test_client() as c:
        login_admin(c)
        html = c.get("/transactions").get_data(as_text=True)
        record("/transactions renders a Company / Foundation optgroup with company_dues",
               'label="Company / Foundation"' in html and 'value="company_dues"' in html)


def test_hard_wall():
    """Test 5: a Company credit never reduces the studio balance and vice
    versa; the bulk helper agrees with the single one."""
    sid = new_student("Wall")
    post_txn(sid, "charge", 100, "tuition")
    post_txn(sid, "charge", 50, "competition")
    post_txn(sid, "payment", 80, "tuition", fund="company")
    with app.app_context():
        bal = calc_balance(sid)
        record("studio balance stays 100.00 after an 80 Company payment",
               bal["studio"]["balance"] == 100.0, str(bal))
        record("company balance is -30.00 (50 charged, 80 paid)",
               bal["company"]["balance"] == -30.0, str(bal))
    post_txn(sid, "payment", 100, "tuition", fund="studio")
    with app.app_context():
        bal = calc_balance(sid)
        record("a 100 studio payment zeroes studio and leaves company at -30.00",
               bal["studio"]["balance"] == 0.0 and bal["company"]["balance"] == -30.0, str(bal))
        record("calc_balance_bulk matches calc_balance",
               calc_balance_bulk([sid])[sid] == bal, str(calc_balance_bulk([sid])))
        empty = calc_balance_bulk([sid, 999999])[999999]
        record("bulk fills an id with no rows with both funds at zero",
               empty["studio"]["balance"] == 0.0 and empty["company"]["balance"] == 0.0, str(empty))


def test_per_fund_allocation():
    """Test 6: a family payment is split within ONE fund; the other fund's
    balances are invisible to it, and the sum always equals the input."""
    with app.app_context():
        fam = Family(name="Alloc Family")
        db.session.add(fam)
        db.session.commit()
        fid = fam.id
    a = new_student("AllocA", fid)
    b = new_student("AllocB", fid)
    post_txn(a, "charge", 200, "tuition")
    post_txn(a, "charge", 30, "company_dues")
    post_txn(b, "charge", 50, "tuition")
    with app.app_context():
        allocs = allocate_family_payment([a, b], 100.0, "company")
        record("company allocation goes only to A (30 owed + 70 credit stays in company)",
               allocs == [(a, 100.0)], str(allocs))
        allocs = allocate_family_payment([a, b], 100.0, "studio")
        record("studio allocation pays A first (largest studio balance), B absent",
               allocs == [(a, 100.0)], str(allocs))
        allocs = allocate_family_payment([a, b], 240.0, "studio")
        record("studio $240 splits A=200, B=40 and sums to the input",
               dict(allocs) == {a: 200.0, b: 40.0} and round(sum(x for _, x in allocs), 2) == 240.0,
               str(allocs))
        try:
            allocate_family_payment([a, b], 10.0, "bogus")
            record("allocate_family_payment rejects an unknown fund", False, "no ValueError")
        except ValueError:
            record("allocate_family_payment rejects an unknown fund", True)


def test_late_fee_per_fund():
    """Test 7: a student owing Company only gets exactly one Company late fee,
    none in studio, and a second run in the same month adds nothing."""
    sid = new_student("LateCo")
    post_txn(sid, "charge", 75, "convention")
    with app.app_context():
        Setting.set("late_fee_min_balance", "0")
        Setting.set("late_fee_amount", "10")
        db.session.commit()

    def fees():
        with app.app_context():
            rows = Transaction.query.filter_by(student_id=sid, category="late fee").all()
            return sorted((t.fund, float(t.amount)) for t in rows)

    with app.test_client() as c:
        login_admin(c)
        r = c.post("/api/balances/apply-late-fees", json={})
        record("apply-late-fees succeeds", r.status_code == 200, str(r.get_json()))
        got = fees()
        record("company-only debtor gets exactly one company late fee and no studio fee",
               got == [("company", 10.0)], str(got))
        r = c.post("/api/balances/apply-late-fees", json={})
        record("second run in the same month adds no late fee",
               r.status_code == 200 and fees() == [("company", 10.0)], str(fees()))
        with app.app_context():
            bal = calc_balance(sid)
            record("the late fee landed in the company balance (85.00), studio still 0",
                   bal["company"]["balance"] == 85.0 and bal["studio"]["balance"] == 0.0, str(bal))


def test_company_visibility():
    """Test 8: has_company_activity_bulk flags a company row or an active
    membership, and not the studio-only sibling."""
    with_row = new_student("HasRow")
    member = new_student("Member")
    plain = new_student("Plain")
    inactive = new_student("ExMember")
    post_txn(with_row, "charge", 10, "observer")
    post_txn(plain, "charge", 10, "tuition")
    with app.app_context():
        grp = PerformanceGroup(name="LSODance Company")
        db.session.add(grp)
        db.session.flush()
        db.session.add(CompanyMembership(group_id=grp.id, student_id=member, is_active=True))
        db.session.add(CompanyMembership(group_id=grp.id, student_id=inactive, is_active=False))
        db.session.commit()
        got = has_company_activity_bulk([with_row, member, plain, inactive])
        record("has_company_activity_bulk: company row and active member only",
               got == {with_row, member}, str(got))
        record("has_company_activity_bulk([]) is an empty set",
               has_company_activity_bulk([]) == set())
    with app.test_client() as c:
        login_admin(c)
        rows = {b["student_id"]: b for b in c.get("/api/balances").get_json()["balances"]}
        record("/api/balances rows carry per-fund blocks and has_company, no flat balance",
               "funds" in rows[plain] and "balance" not in rows[plain]
               and rows[with_row]["has_company"] is True and rows[plain]["has_company"] is False,
               str(rows.get(plain)))
        def account_card(student_id):
            html = c.get(f"/students/{student_id}/detail").get_data(as_text=True)
            start = html.find(">Account</h2>")
            end = html.find(">Portal Access</h2>")
            return html[start:end] if start >= 0 and end > start else ""
        record("studio-only student detail shows Studio and hides the Company block",
               "Studio" in account_card(plain) and "Company / Foundation" not in account_card(plain),
               account_card(plain)[:200])
        record("student with Company activity shows the Company block",
               "Company / Foundation" in account_card(with_row), account_card(with_row)[:200])

def main():
    ids = seed()
    test_tagging(ids)
    test_backfill_idempotent(ids)
    test_map_is_single_source()
    test_dropdowns_render_from_map()
    test_hard_wall()
    test_per_fund_allocation()
    test_late_fee_per_fund()
    test_company_visibility()
    fails = [r for r in results if not r[1]]
    print("\n" + "=" * 56)
    print(f"SUMMARY: {len(results) - len(fails)}/{len(results)} passed, {len(fails)} failed.")
    try:
        os.unlink(_tmp.name)
    except OSError:
        pass
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
