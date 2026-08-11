"""Public-registration field tests for AttenDANCE.

The studio expanded the enrollment form (Aug 2026): emergency contact, a second
guardian, home address, dancer email, and six fields that became mandatory.
What has to hold:

  1. The form's URL never changes - it's already handed out to parents.
  2. Required means required ON THE SERVER, for all six: parent phone, the
     emergency contact's name and phone, and per dancer a last name, a real date
     of birth, and the allergies/medical answer. (The emergency contact's
     RELATIONSHIP stays optional - you can't call a relationship.)
  3. Optional additions - second guardian, address, dancer email, emergency
     relationship - round-trip onto the Registration and are visible to the
     admin reviewing the queue.
  4. Approval isn't a black hole: address + second guardian land on the Family,
     emergency contact and dancer email land on the Student.
  5. Dancer email can't break approval. It's UNIQUE on students, so a family
     reusing one address across siblings, or an address another dancer already
     owns, must be skipped and reported - never a 500 mid-transaction.
  6. Re-registration refreshes the household but never clobbers what the office
     corrected by hand on a returning dancer.
  7. Registrations stored before these fields existed still approve.

Run:  RFID_ENABLED=false python3 tests/test_registration_fields.py
Exit 0 = all green, 1 = failures.
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("RFID_ENABLED", "false")
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp.name}"

from app import create_app, db  # noqa: E402
from app.models import Family, Registration, Setting, Student, User  # noqa: E402

app = create_app("development")
app.config["TESTING"] = True
app.config["WTF_CSRF_ENABLED"] = False

results = []


def record(name, passed, detail=""):
    results.append((name, passed))
    print(f"[{'PASS' if passed else 'FAIL'}] {name}" + (f" - {detail}" if detail and not passed else ""))


def dancer(first, last="Tester", **over):
    s = {"first_name": first, "last_name": last, "dob": "2015-04-02", "allergies": "None"}
    s.update(over)
    return s


def payload(email, **over):
    """A complete, valid submission. Parent phone and the emergency contact's
    name and phone are required, so they're baked in here; tests that are ABOUT
    those rules override them."""
    body = {
        "parent_name": "Dana Parent", "parent_email": email,
        "parent_phone": "313-555-0100",
        "emergency_name": "Emergency Contact", "emergency_phone": "313-555-0911",
        "students": [dancer("Ava")],
    }
    body.update(over)
    return body


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


# ── 1. The link parents already have must keep working ──────────────
r = pub.get("/register")
record("/register still serves the form (link unchanged)", r.status_code == 200,
       f"got {r.status_code}")
html = r.get_data(as_text=True)
record("form asks for the new sections", all(
    s in html for s in ["Additional Parent / Guardian", "Home Address",
                        "Emergency Contact", "Allergies / medical needs"]),
       "a section is missing from the rendered form")

login_html = pub.get("/auth/login").get_data(as_text=True)
record("login page offers new parents the registration link",
       "New Parent?" in login_html and 'href="/register"' in login_html,
       "link missing from login page")


# ── 2. Required fields, enforced server-side ────────────────────────
cases = [
    ("parent phone", payload("req1@x.com", parent_phone="")),
    ("dancer last name", payload("req2@x.com", students=[dancer("Ava", "")])),
    ("dancer dob", payload("req3@x.com", students=[dancer("Ava", dob="")])),
    ("dancer dob must parse", payload("req4@x.com", students=[dancer("Ava", dob="soon")])),
    ("dancer allergies", payload("req5@x.com", students=[dancer("Ava", allergies="")])),
    ("dancer email must be valid", payload("req6@x.com", students=[dancer("Ava", email="nope")])),
    ("emergency contact name", payload("req7@x.com", emergency_name="")),
    ("emergency contact phone", payload("req8@x.com", emergency_phone="")),
]
for label, body in cases:
    resp = pub.post("/api/register", json=body)
    record(f"rejects a submission missing {label}", resp.status_code == 400,
           f"got {resp.status_code} {resp.get_json()}")

with app.app_context():
    leaked = Registration.query.filter(
        Registration.parent_email.like("req%@x.com")).count()
    record("no partial rows stored from rejected submissions", leaked == 0,
           f"stored {leaked}")


# ── 3. Full submission round-trips every new field ──────────────────
FULL = payload(
    "full@x.com",
    parent2_name="Chris Parent", parent2_email="chris@x.com", parent2_phone="313-555-0199",
    emergency_name="Gloria Gran", emergency_phone="313-555-0177",
    emergency_relationship="Grandmother",
    address="123 Nine Mile Rd", city="Oak Park", state="MI", zip_code="48237",
    students=[dancer("Ava", "Parent", email="ava@x.com", allergies="Peanuts"),
              dancer("Zoe", "Parent")],
)
r = pub.post("/api/register", json=FULL)
record("full submission accepted", r.status_code == 201, f"got {r.status_code} {r.get_json()}")

with app.app_context():
    reg = Registration.query.filter_by(parent_email="full@x.com").first()
    record("second guardian stored", reg and reg.parent2_name == "Chris Parent"
           and reg.parent2_email == "chris@x.com" and reg.parent2_phone == "313-555-0199")
    record("emergency contact stored", reg and reg.emergency_name == "Gloria Gran"
           and reg.emergency_phone == "313-555-0177"
           and reg.emergency_relationship == "Grandmother")
    record("address stored", reg and reg.address == "123 Nine Mile Rd"
           and reg.city == "Oak Park" and reg.state == "MI" and reg.zip_code == "48237")
    stored = json.loads(reg.students_json)
    record("dancer email stored on the dancer", stored[0].get("email") == "ava@x.com",
           f"got {stored[0]}")
    full_rid = reg.id

queue = staff.get("/api/registrations?status=pending").get_json()["registrations"]
row = next((q for q in queue if q["parent_email"] == "full@x.com"), None)
record("admin queue exposes the new fields", row is not None
       and row["emergency_name"] == "Gloria Gran"
       and row["parent2_name"] == "Chris Parent"
       and row["city"] == "Oak Park",
       f"got {row}")


# ── 4. Approval carries it onto Family + Student ────────────────────
r = staff.post(f"/api/registrations/{full_rid}/approve")
record("approve succeeds", r.status_code == 200, f"got {r.status_code} {r.get_json()}")

with app.app_context():
    fam = Family.query.filter_by(primary_email="full@x.com").first()
    record("address landed on the family", fam and fam.address == "123 Nine Mile Rd"
           and fam.city == "Oak Park" and fam.zip_code == "48237")
    record("second guardian landed on the family", fam and fam.secondary_name == "Chris Parent"
           and fam.secondary_phone == "313-555-0199")
    record("family address_block reads as one line",
           fam and fam.address_block == "123 Nine Mile Rd, Oak Park, MI 48237",
           f"got {fam.address_block if fam else None}")

    ava = Student.query.filter_by(first_name="Ava", last_name="Parent").first()
    record("emergency contact landed on the dancer, with relationship",
           ava and ava.emergency_contact_name == "Gloria Gran (Grandmother)"
           and ava.emergency_contact_phone == "313-555-0177",
           f"got {ava.emergency_contact_name if ava else None}")
    record("dancer email landed", ava and ava.email == "ava@x.com",
           f"got {ava.email if ava else None}")
    record("allergies landed", ava and ava.allergies == "Peanuts",
           f"got {ava.allergies if ava else None}")
    zoe = Student.query.filter_by(first_name="Zoe", last_name="Parent").first()
    record("sibling with no email gets NULL, not an empty string",
           zoe is not None and zoe.email is None, f"got {zoe.email if zoe else 'missing'}")


# ── 5. Dancer email can never break approval ────────────────────────
# Same address on two siblings, plus one already owned by another dancer.
pub.post("/api/register", json=payload(
    "dupe@x.com", parent_name="Sam Dupe",
    students=[dancer("Kai", "Dupe", email="shared@x.com"),
              dancer("Rio", "Dupe", email="SHARED@x.com"),      # same, different case
              dancer("Sky", "Dupe", email="ava@x.com")]))       # already Ava's
with app.app_context():
    dupe_rid = Registration.query.filter_by(parent_email="dupe@x.com").first().id
r = staff.post(f"/api/registrations/{dupe_rid}/approve")
body = r.get_json() or {}
record("approve with duplicate dancer emails doesn't 500", r.status_code == 200,
       f"got {r.status_code} {body}")
record("the collisions are reported to the admin", len(body.get("skipped_emails", [])) == 2,
       f"got {body.get('skipped_emails')}")
with app.app_context():
    emails = {s.first_name: s.email for s in
              Student.query.filter_by(last_name="Dupe").all()}
    record("exactly one sibling claimed the shared address",
           sum(1 for v in emails.values() if v == "shared@x.com") == 1, f"got {emails}")
    record("the address already owned by another dancer was left off",
           emails.get("Sky") is None, f"got {emails}")
    record("Ava kept her email", (Student.query.filter_by(
        first_name="Ava", last_name="Parent").first() or Student()).email == "ava@x.com")


# ── 6. Re-registration: refresh the household, protect hand edits ───
with app.app_context():
    ava = Student.query.filter_by(first_name="Ava", last_name="Parent").first()
    ava.emergency_contact_name = "Office Corrected This"
    ava.emergency_contact_phone = "313-555-9999"
    db.session.commit()

# Note what this payload OMITS: every parent2_* field. A returning family
# retyping only what changed must not blank out the second guardian on file.
pub.post("/api/register", json=payload(
    "full@x.com", parent_name="Dana Parent",
    address="500 Coolidge Hwy", city="Oak Park", state="MI", zip_code="48237",
    emergency_name="New Contact", emergency_phone="313-555-0000",
    students=[dancer("Ava", "Parent")]))
with app.app_context():
    again_rid = (Registration.query.filter_by(parent_email="full@x.com", status="pending")
                 .first().id)
r = staff.post(f"/api/registrations/{again_rid}/approve")
record("re-registration approves", r.status_code == 200, f"got {r.status_code}")
with app.app_context():
    fam = Family.query.filter_by(primary_email="full@x.com").first()
    record("moving house updates the family address", fam.address == "500 Coolidge Hwy",
           f"got {fam.address}")
    record("a blank field never wipes the second guardian on file",
           fam.secondary_name == "Chris Parent" and fam.secondary_phone == "313-555-0199",
           f"got {fam.secondary_name} / {fam.secondary_phone}")
    ava = Student.query.filter_by(first_name="Ava", last_name="Parent").first()
    record("re-registration does NOT clobber an office-corrected emergency contact name",
           ava.emergency_contact_name == "Office Corrected This",
           f"got {ava.emergency_contact_name}")
    record("re-registration does NOT clobber an office-corrected emergency contact phone",
           ava.emergency_contact_phone == "313-555-9999",
           f"got {ava.emergency_contact_phone}")
    record("no duplicate dancer created on re-registration",
           Student.query.filter_by(first_name="Ava", last_name="Parent").count() == 1)


# ── 6b. "None" is an answer, not an allergy ─────────────────────────
# The form REQUIRES an allergies answer and tells parents to type "None". That
# literal must not reach the Student: every allergy display renders the field in
# the red medical callout, so storing "None" would flag every healthy dancer
# until staff stop reading it - and "None" is truthy, so it would also block the
# returning-dancer backfill from ever recording a real allergy later.
pub.post("/api/register", json=payload(
    "sentinel@x.com", parent_name="Sara Sentinel",
    students=[dancer("Ann", "Sentinel", allergies="None"),
              dancer("Ben", "Sentinel", allergies="n/a"),
              dancer("Cal", "Sentinel", allergies="No known allergies"),
              dancer("Dee", "Sentinel", allergies="Peanuts - carries an EpiPen")]))
with app.app_context():
    sent_rid = Registration.query.filter_by(parent_email="sentinel@x.com").first().id
staff.post(f"/api/registrations/{sent_rid}/approve")
with app.app_context():
    got = {s.first_name: s.allergies for s in
           Student.query.filter_by(last_name="Sentinel").all()}
    record('"None" is stored as no allergy, not the literal string',
           got.get("Ann") is None, f"got {got.get('Ann')!r}")
    record('"n/a" and "No known allergies" are treated the same way',
           got.get("Ben") is None and got.get("Cal") is None, f"got {got}")
    record("a real allergy is stored verbatim",
           got.get("Dee") == "Peanuts - carries an EpiPen", f"got {got.get('Dee')!r}")
    record("the parent's literal answer is still on the registration row",
           '"allergies": "None"' in (Registration.query.get(sent_rid).students_json or ""),
           "the audit trail of what the parent actually typed was lost")

# A later real allergy must reach the dancer - the "None" answer can't block it.
pub.post("/api/register", json=payload(
    "sentinel@x.com", parent_name="Sara Sentinel",
    students=[dancer("Ann", "Sentinel", allergies="Shellfish - severe")]))
with app.app_context():
    sent_rid2 = (Registration.query.filter_by(parent_email="sentinel@x.com", status="pending")
                 .first().id)
staff.post(f"/api/registrations/{sent_rid2}/approve")
with app.app_context():
    record("an allergy reported after a 'None' answer is recorded",
           Student.query.filter_by(first_name="Ann", last_name="Sentinel").first()
           .allergies == "Shellfish - severe")

# A CHANGED allergy on a dancer who already has one is never silently dropped:
# overwriting risks a skimmed "None" erasing an EpiPen note, so it's reported.
pub.post("/api/register", json=payload(
    "sentinel@x.com", parent_name="Sara Sentinel",
    students=[dancer("Dee", "Sentinel", allergies="Peanuts AND tree nuts")]))
with app.app_context():
    sent_rid3 = (Registration.query.filter_by(parent_email="sentinel@x.com", status="pending")
                 .first().id)
res = (staff.post(f"/api/registrations/{sent_rid3}/approve").get_json() or {})
record("a changed allergy answer is surfaced to the admin, not dropped",
       len(res.get("allergy_updates", [])) == 1
       and "Peanuts AND tree nuts" in res["allergy_updates"][0],
       f"got {res.get('allergy_updates')}")
record("the admin message says what to do about it",
       "CHECK:" in res.get("message", ""), f"got {res.get('message')}")
with app.app_context():
    record("the existing allergy is left intact until a human decides",
           Student.query.filter_by(first_name="Dee", last_name="Sentinel").first()
           .allergies == "Peanuts - carries an EpiPen")


# ── 6c. The emergency contact is one record, never two halves ───────
# Name and phone must always come from the same person. Backfilling only the
# empty half pairs the name on file with a different person's number, and staff
# call the wrong contact in the emergency the field exists for.
with app.app_context():
    fam2 = Family(name="Half Family", primary_email="half@x.com")
    db.session.add(fam2)
    db.session.flush()
    db.session.add(Student(first_name="Hal", last_name="Half", family_id=fam2.id,
                           parent_email="half@x.com",
                           emergency_contact_name="Uncle Bob (Uncle)",
                           emergency_contact_phone=None))
    db.session.commit()

pub.post("/api/register", json=payload(
    "half@x.com", parent_name="Hana Half",
    emergency_name="Gloria Gran", emergency_relationship="Grandmother",
    emergency_phone="313-555-0177",
    students=[dancer("Hal", "Half")]))
with app.app_context():
    half_rid = Registration.query.filter_by(parent_email="half@x.com").first().id
staff.post(f"/api/registrations/{half_rid}/approve")
with app.app_context():
    hal = Student.query.filter_by(first_name="Hal", last_name="Half").first()
    record("a half-filled emergency contact is never completed with another person's number",
           hal.emergency_contact_phone is None,
           f"name={hal.emergency_contact_name!r} phone={hal.emergency_contact_phone!r}")
    record("the contact name on file is left alone",
           hal.emergency_contact_name == "Uncle Bob (Uncle)",
           f"got {hal.emergency_contact_name!r}")

# With NOTHING on file, the submitted contact is adopted whole.
with app.app_context():
    fam3 = Family(name="Empty Family", primary_email="empty@x.com")
    db.session.add(fam3)
    db.session.flush()
    db.session.add(Student(first_name="Eve", last_name="Empty", family_id=fam3.id,
                           parent_email="empty@x.com"))
    db.session.commit()
pub.post("/api/register", json=payload(
    "empty@x.com", parent_name="Ed Empty",
    emergency_name="Gloria Gran", emergency_relationship="Grandmother",
    emergency_phone="313-555-0177",
    students=[dancer("Eve", "Empty")]))
with app.app_context():
    empty_rid = Registration.query.filter_by(parent_email="empty@x.com").first().id
staff.post(f"/api/registrations/{empty_rid}/approve")
with app.app_context():
    eve = Student.query.filter_by(first_name="Eve", last_name="Empty").first()
    record("a dancer with no contact on file gets the whole submitted contact",
           eve.emergency_contact_name == "Gloria Gran (Grandmother)"
           and eve.emergency_contact_phone == "313-555-0177",
           f"name={eve.emergency_contact_name!r} phone={eve.emergency_contact_phone!r}")


# ── 6d. The parent data export covers the new household PII ────────
# Home address and the second guardian sit on the Family, so they'd fall
# straight through an export that only walks the account and its children -
# and the privacy policy promises parents can download everything held.
with app.app_context():
    from app.models import ParentStudent
    ava = Student.query.filter_by(first_name="Ava", last_name="Parent").first()
    parent = User.query.filter_by(username="exportparent").first()
    if not parent:
        parent = User(username="exportparent", email="exportparent@x.com", role="parent",
                      first_name="Dana", last_name="Parent")
        db.session.add(parent)
    parent.set_password("pw12345")
    db.session.flush()
    if not ParentStudent.query.filter_by(parent_id=parent.id, student_id=ava.id).first():
        db.session.add(ParentStudent(parent_id=parent.id, student_id=ava.id))
    db.session.commit()

pc = app.test_client()
pc.post("/auth/login", data={"username": "exportparent", "password": "pw12345"},
        follow_redirects=True)
exp = pc.get("/api/my-data").get_json() or {}
houses = exp.get("households") or []
record("data export includes the household record", len(houses) == 1, f"got {houses}")
if houses:
    h = houses[0]
    record("export carries the home address",
           h.get("address") == "500 Coolidge Hwy" and h.get("zip_code") == "48237", f"got {h}")
    record("export carries the second guardian",
           h.get("second_guardian_name") == "Chris Parent"
           and h.get("second_guardian_email") == "chris@x.com", f"got {h}")
kids = exp.get("children") or exp.get("students") or []
blob = json.dumps(exp)
record("export still carries the dancer-level additions",
       "ava@x.com" in blob and "Office Corrected This" in blob,
       "dancer email or emergency contact missing from the export")


# ── 7. Rows written before these fields existed still approve ───────
with app.app_context():
    legacy = Registration(
        parent_name="Old Row", parent_email="legacy@x.com", parent_phone=None,
        students_json=json.dumps([{"first_name": "Pat", "last_name": "Legacy",
                                   "dob": "", "allergies": ""}]))
    db.session.add(legacy)
    db.session.commit()
    legacy_rid = legacy.id
r = staff.post(f"/api/registrations/{legacy_rid}/approve")
record("a pre-existing registration (no new fields) still approves",
       r.status_code == 200, f"got {r.status_code} {r.get_json()}")
with app.app_context():
    record("legacy dancer created", Student.query.filter_by(
        first_name="Pat", last_name="Legacy").count() == 1)


# ── Migration adds the columns to an ALREADY-CREATED table ──────────
# Production's families/registrations tables predate these columns, and
# db.create_all() does NOT alter an existing table - only run_migrations() adds
# them. Inspecting this test DB proves nothing: create_all() built it from the
# current models, so the columns are there whether or not the migration works.
# Reproduce prod's shape instead: drop the new columns back off, then migrate.
FAM_NEW = ["secondary_name", "secondary_email", "secondary_phone",
           "address", "city", "state", "zip_code"]
REG_NEW = ["parent2_name", "parent2_email", "parent2_phone", "emergency_name",
           "emergency_phone", "emergency_relationship",
           "address", "city", "state", "zip_code"]

with app.app_context():
    import sqlalchemy

    from app.migrations import run_migrations

    with db.engine.begin() as conn:
        for table, cols in (("families", FAM_NEW), ("registrations", REG_NEW)):
            for col in cols:
                conn.execute(sqlalchemy.text(f"ALTER TABLE {table} DROP COLUMN {col}"))

    insp = sqlalchemy.inspect(db.engine)
    stripped_ok = (not set(FAM_NEW) & {c["name"] for c in insp.get_columns("families")}
                   and not set(REG_NEW) & {c["name"] for c in insp.get_columns("registrations")})
    record("fixture reproduces the pre-migration (production) schema", stripped_ok,
           "columns survived the drop, so the check below would be meaningless")

    run_migrations(db)

    insp = sqlalchemy.inspect(db.engine)
    fam_cols = {c["name"] for c in insp.get_columns("families")}
    reg_cols = {c["name"] for c in insp.get_columns("registrations")}
    record("migration adds the new columns to an existing families table",
           set(FAM_NEW) <= fam_cols, f"still missing {sorted(set(FAM_NEW) - fam_cols)}")
    record("migration adds the new columns to an existing registrations table",
           set(REG_NEW) <= reg_cols, f"still missing {sorted(set(REG_NEW) - reg_cols)}")

    # Boot runs migrations on every wake, so a second pass must be a no-op.
    try:
        run_migrations(db)
        rerun_ok = True
    except Exception as exc:  # noqa: BLE001 - the failure IS the assertion
        rerun_ok = False
        print(f"    second run_migrations raised: {exc}")
    record("running the migration twice is a no-op (Fly wakes the app all day)", rerun_ok)

    # The whole point: the public endpoint has to work against the migrated DB.
    r = pub.post("/api/register", json=payload("postmigrate@x.com"))
    record("public registration works after migrating a pre-existing schema",
           r.status_code == 201, f"got {r.status_code} {r.get_json()}")

with app.app_context():
    Setting.set("registration_open", "0")
    db.session.commit()

passed = sum(1 for _, p in results if p)
total = len(results)
print("\n" + "=" * 56)
print(f"SUMMARY: {passed}/{total} passed, {total - passed} failed.")
sys.exit(0 if passed == total else 1)
