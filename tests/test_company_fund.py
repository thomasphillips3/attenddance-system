"""Company fund harness for AttenDANCE.

The LSODance Company collects to the Foundation's accounts; the studio
collects tuition to its own. These tests prove the fund wall holds at every
layer: tagging on write, the idempotent backfill, the single category map,
and the dropdowns that render from it.

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
from app.migrations import run_migrations  # noqa: E402
from app.models import Family, Student, Transaction, User  # noqa: E402

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


def main():
    ids = seed()
    test_tagging(ids)
    test_backfill_idempotent(ids)
    test_map_is_single_source()
    test_dropdowns_render_from_map()
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
