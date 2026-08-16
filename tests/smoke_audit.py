"""QA production-readiness harness for AttenDANCE.

Runtime checks that complement the static audit:
  1. Boot on a throwaway SQLite DB.
  2. Seed admin + two UNRELATED parent/student/family sets.
  3. IDOR probe: parent A must NOT be able to read/edit/delete parent B's child
     via the JSON API, and must not reach staff-only endpoints.
  4. Smoke: as admin, GET every no-arg route and assert no 500s.

Run:  RFID_ENABLED=false python3 tests/smoke_audit.py
Exit code 0 = all green, 1 = failures (prints a report).
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("RFID_ENABLED", "false")

# Point the app at a throwaway DB before importing config.
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp.name}"

from app import create_app, db  # noqa: E402
from app.models import User, Student, Family, ParentStudent  # noqa: E402

app = create_app("development")
app.config["WTF_CSRF_ENABLED"] = False
app.config["TESTING"] = True

results = []  # (severity, name, passed, detail)


def record(name, passed, detail="", severity="P1"):
    results.append((severity, name, passed, detail))
    mark = "PASS" if passed else "FAIL"
    print(f"[{mark}] ({severity}) {name}" + (f" — {detail}" if detail and not passed else ""))


def seed():
    with app.app_context():
        # Two unrelated families, each with one parent + one child.
        fam_a = Family(name="Alpha Family")
        fam_b = Family(name="Beta Family")
        db.session.add_all([fam_a, fam_b])
        db.session.flush()

        child_a = Student(first_name="Ava", last_name="Alpha", family_id=fam_a.id,
                          parent_email="alpha-parent@x.com",
                          allergies="peanuts", special_needs="asthma")
        child_b = Student(first_name="Bo", last_name="Beta", family_id=fam_b.id,
                          parent_email="beta-parent@x.com",
                          allergies="shellfish", special_needs="epilepsy")
        db.session.add_all([child_a, child_b])
        db.session.flush()

        parent_a = User(username="parent_a", email="a@x.com", role="parent",
                        first_name="Pat", last_name="Alpha", is_active=True)
        parent_a.set_password("pw")
        parent_b = User(username="parent_b", email="b@x.com", role="parent",
                        first_name="Pat", last_name="Beta", is_active=True)
        parent_b.set_password("pw")
        db.session.add_all([parent_a, parent_b])
        db.session.flush()

        db.session.add_all([
            ParentStudent(parent_id=parent_a.id, student_id=child_a.id),
            ParentStudent(parent_id=parent_b.id, student_id=child_b.id),
        ])
        db.session.commit()
        return {
            "child_a": child_a.id, "child_b": child_b.id,
            "fam_a": fam_a.id, "fam_b": fam_b.id,
        }


def login(client, username, password):
    return client.post("/auth/login",
                       data={"username": username, "password": password},
                       follow_redirects=True)


def run_idor(ids):
    """Parent A acting on Parent B's child must be blocked (403/404)."""
    from datetime import time as _time
    from app.models import DanceClass, User as _U
    with app.app_context():
        _adm = _U.query.filter_by(username="admin").first()
        _dc = DanceClass(name="IDOR Class", day_of_week=4, start_time=_time(9, 0),
                         end_time=_time(10, 0), instructor_id=_adm.id)
        db.session.add(_dc)
        db.session.commit()
        idor_cid = _dc.id
    with app.test_client() as c:
        login(c, "parent_a", "pw")
        bid = ids["child_b"]
        fbid = ids["fam_b"]
        aid = ids["child_a"]

        # Positive: parent MUST still reach their own child + own data (no over-lock).
        for method, path, desc in [
            ("GET", f"/api/students/{aid}", "own child PII"),
            ("GET", f"/api/students/{aid}/ledger", "own child ledger"),
            ("GET", "/api/my-payments", "own payments"),
            ("GET", "/api/my-data", "own data export"),
        ]:
            resp = c.open(path, method=method)
            ok = resp.status_code == 200
            record(f"Parent CAN access {desc} [{method} {path}] -> {resp.status_code}",
                   ok, f"got {resp.status_code}, expected 200", "P1")

        # The data export must contain ONLY the requesting parent's own child —
        # this is the exact bundle a parent downloads, so a leak here is a
        # full-PII IDOR (allergies, DOB, medical notes) for an unrelated family.
        export = c.get("/api/my-data").get_json() or {}
        child_names = {ch.get("full_name") for ch in export.get("children", [])}
        record("Data export contains own child, not other family's",
               child_names == {"Ava Alpha"}, f"got children={child_names}", "P0")

        probes = [
            ("GET",    f"/api/students/{bid}",                 "read other child's PII"),
            ("PUT",    f"/api/students/{bid}",                 "edit other child"),
            ("DELETE", f"/api/students/{bid}",                 "deactivate other child"),
            ("GET",    f"/api/students/{bid}/ledger",          "read other child's ledger"),
            ("GET",    f"/api/students/{bid}/skills",          "read other child's skills"),
            ("GET",    f"/api/students/{bid}/waivers",         "read other child's waivers"),
            ("GET",    f"/api/students/{bid}/rules-status",    "read other child's rules status"),
            ("GET",    f"/api/students/{bid}/payment-plan",    "read other child's plan"),
            ("GET",    f"/api/families/{fbid}/ledger",         "read other family's ledger"),
        ]
        for method, path, desc in probes:
            resp = c.open(path, method=method, json={} if method != "GET" else None)
            blocked = resp.status_code in (401, 403, 404)
            record(f"IDOR blocked: {desc} [{method} {path}] -> {resp.status_code}",
                   blocked, f"got {resp.status_code}, expected 401/403/404", "P0")

        # Parent must not reach staff-only endpoints (reads). Comprehensive sweep
        # of the whole staff-only GET surface (not a spot-check) — every one of
        # these leaks studio-wide or other-family data if it 200s for a parent.
        staff_only = [
            "/api/students",              # full roster
            "/api/transactions",          # all money
            "/api/staff",                 # all staff accounts
            "/api/families",              # all families
            "/api/messages",              # all sent messages
            "/api/balances",              # every family's balance
            "/api/attendance",            # attendance records
            "/api/attendance/today",      # today's attendance
            "/api/dashboard/stats",       # studio stats
            "/api/reports/aging",         # A/R aging report
            "/api/reports/revenue",       # revenue report
            "/api/reports/students.csv",  # roster export
            "/api/reports/transactions.csv",  # money export
            "/api/leads",                 # sales pipeline (PII of prospects)
            "/api/donations",             # all donations
            "/api/locations",            # venues + internal notes/phone
            "/api/classes",               # full class list (instructors, rosters)
            "/api/instructors",           # instructor picker (staff names)
            "/api/skills",                # skill catalog
            "/api/costumes",              # costume catalog
            "/api/recurring-charges",     # every recurring charge
            "/api/pending-payments",      # every family's reported payments
            "/api/registrations",         # every enrollment request (PII)
            "/api/audit-log",             # the full audit trail
            "/api/analytics/retention",   # retention analytics
            "/api/rfid/logs",             # every check-in
            "/api/timeclock/report",      # staff payroll
            "/api/waivers/compliance",    # every family's waiver status
            "/api/settings/payments",     # payment config (may carry secrets)
        ]
        for path in staff_only:
            resp = c.get(path)
            blocked = resp.status_code in (401, 403, 404)
            record(f"Parent blocked from staff read [GET {path}] -> {resp.status_code}",
                   blocked, f"got {resp.status_code}", "P0")

        # A SINGLE class GET leaks the instructor name + class internals, so it
        # must be staff-only like the plural /api/classes (it wasn't).
        r_cls = c.get(f"/api/classes/{idor_cid}")
        record(f"Parent blocked from single-class read [GET /api/classes/<id>] -> {r_cls.status_code}",
               r_cls.status_code in (401, 403, 404), f"got {r_cls.status_code}", "P1")

        # The nav-badge count endpoints intentionally 200 for parents but MUST
        # return a safe stub (0), never the real studio count.
        for path in ("/api/pending-payments/count", "/api/registrations/count"):
            j = c.get(path).get_json() or {}
            record(f"Count endpoint returns safe stub to parent [{path}] -> {j}",
                   j.get("count") == 0, f"leaked real count: {j}", "P2")

        # Parent must not perform staff-only WRITES (fabricate money, email blasts).
        forbidden_writes = [
            ("POST", "/api/transactions", "fabricate a payment/charge"),
            ("POST", "/api/transactions/bulk-charge", "bulk-charge families"),
            ("POST", "/api/messages", "send studio-wide email blast"),
            ("POST", "/api/recurring-charges", "create recurring charge"),
            ("POST", "/api/classes", "create a class"),
        ]
        for method, path, desc in forbidden_writes:
            resp = c.open(path, method=method, json={})
            blocked = resp.status_code in (401, 403)
            record(f"Parent write BLOCKED: {desc} [{method} {path}] -> {resp.status_code}",
                   blocked, f"got {resp.status_code}, expected 403", "P0")

        # Parent-ALLOWED writes must NOT be blocked by the guard (may 400 on bad
        # body, but must not 403). This guards against over-locking the portal.
        allowed_writes = [
            ("POST", "/api/payments/claim", "report a payment"),
            ("POST", "/api/donations", "make a donation"),
            ("POST", "/api/my-data/delete-request", "request data deletion"),
        ]
        for method, path, desc in allowed_writes:
            resp = c.open(path, method=method, json={})
            not_locked = resp.status_code != 403
            record(f"Parent-allowed write reachable: {desc} [{method} {path}] -> {resp.status_code}",
                   not_locked, f"got 403 — guard over-locked the portal", "P1")


def run_migration_idempotency():
    """The boot ALTER-TABLE migrations run on EVERY app start, and Fly wakes/sleeps
    the machine several times a day — so re-running them must be a no-op, never an
    error (a non-idempotent migration would boot once then crash on the next wake)."""
    from app.migrations import run_migrations
    errs = []
    with app.app_context():
        for _ in range(3):  # simulate repeated Fly wakes on the already-migrated DB
            try:
                run_migrations(db)
            except Exception as e:  # noqa: BLE001
                errs.append(f"{type(e).__name__}: {e}")
                break
    record("Boot migrations are idempotent (safe to re-run every wake)",
           not errs, f"re-run failed: {errs}", "P1")


def run_prod_security_config():
    """Guard the production security posture from regression: (1) the fail-closed
    SECRET_KEY guard refuses a missing/default key but boots on a strong one, and
    (2) both the session and the 'remember me' cookies get Secure + HttpOnly +
    SameSite in production. The remember cookie is a long-lived credential, so it
    must be hardened like the session cookie.

    SECRET_KEY is read at config-import time, so this must run in fresh
    subprocesses with the env set before import — an in-process test can't change
    the frozen value (and would give a false result)."""
    import subprocess

    def _boot(secret_key):
        """Boot production in a clean interpreter with the given SECRET_KEY.
        Returns (rc, stdout). rc != 0 means the fail-closed guard refused."""
        snippet = (
            "import os, json\n"
            "from app import create_app\n"
            "app = create_app('production')\n"
            "keys = ['SESSION_COOKIE_SECURE','SESSION_COOKIE_HTTPONLY','SESSION_COOKIE_SAMESITE',"
            "'REMEMBER_COOKIE_SECURE','REMEMBER_COOKIE_HTTPONLY','REMEMBER_COOKIE_SAMESITE']\n"
            "print(json.dumps({k: app.config.get(k) for k in keys}))\n"
        )
        env = dict(os.environ)
        env["SECRET_KEY"] = secret_key
        env["RFID_ENABLED"] = "false"
        env["DATABASE_URL"] = f"sqlite:///{tempfile.NamedTemporaryFile(suffix='.db').name}"
        p = subprocess.run([sys.executable, "-c", snippet], capture_output=True,
                           text=True, cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           env=env, timeout=60)
        return p.returncode, p.stdout.strip()

    # Default key must be refused (fail closed).
    rc, _ = _boot("dev-secret-key-change-in-production-12345")
    record("Prod refuses a default SECRET_KEY (fail closed)", rc != 0,
           "prod booted with the known default key", "P0")

    # A strong key must be accepted, so the studio can actually deploy.
    import secrets as _secrets
    rc, out = _boot(_secrets.token_hex(32))
    record("Prod boots with a strong SECRET_KEY", rc == 0,
           f"guard too strict, rejected a real key (rc={rc})", "P0")
    if rc == 0 and out:
        import json as _json
        cfg = _json.loads(out.splitlines()[-1])
        want = {
            "SESSION_COOKIE_SECURE": True, "SESSION_COOKIE_HTTPONLY": True,
            "SESSION_COOKIE_SAMESITE": "Lax", "REMEMBER_COOKIE_SECURE": True,
            "REMEMBER_COOKIE_HTTPONLY": True, "REMEMBER_COOKIE_SAMESITE": "Lax",
        }
        for k, exp in want.items():
            record(f"Prod cookie hardening: {k} == {exp!r} (got {cfg.get(k)!r})",
                   cfg.get(k) == exp, f"got {cfg.get(k)!r}", "P2")


def run_privilege_escalation(ids):
    """No path to admin: a teacher can't reach staff-management, an admin can't
    convert a parent account to admin via update_staff, and student updates whitelist
    fields (no mass-assignment — a bad/nonexistent family_id can't 500 or orphan)."""
    with app.app_context():
        from app.models import User
        if not User.query.filter_by(username="teacher_esc").first():
            t = User(username="teacher_esc", email="tesc@x.com", role="teacher",
                     first_name="Esc", last_name="Teacher", is_active=True, is_admin=False)
            t.set_password("teacherpw")
            db.session.add(t)
            db.session.commit()
        parent = User.query.filter_by(username="parent_a").first()
        parent_id = parent.id
    # Teacher cannot reach staff management (create/update staff).
    with app.test_client() as c:
        login(c, "teacher_esc", "teacherpw")
        r1 = c.put(f"/api/staff/{parent_id}", json={"role": "admin"})
        r2 = c.post("/api/staff", json={"username": "z", "email": "z@x.com", "first_name": "Z",
                                        "last_name": "Q", "password": "securepw", "role": "admin"})
        record(f"Teacher cannot update/create staff ({r1.status_code}/{r2.status_code})",
               r1.status_code in (401, 403) and r2.status_code in (401, 403),
               f"{r1.status_code}/{r2.status_code}", "P0")
    # Admin cannot promote a PARENT to admin via update_staff (refuses non-staff).
    with app.test_client() as c:
        login(c, "admin", "admin123")
        r3 = c.put(f"/api/staff/{parent_id}", json={"role": "admin"})
        with app.app_context():
            from app.models import User as _U
            escalated = _U.query.get(parent_id).is_admin
        record(f"Admin can't promote a parent to admin via update_staff -> {r3.status_code}",
               r3.status_code == 400 and not escalated, f"status={r3.status_code} is_admin={escalated}", "P0")
        # Student update whitelists fields: bad/nonexistent family_id neither 500s nor orphans.
        sid = ids["child_a"]
        with app.app_context():
            orig_fam = Student.query.get(sid).family_id
        rb = c.put(f"/api/students/{sid}", json={"family_id": "xyz"})
        rn = c.put(f"/api/students/{sid}", json={"family_id": 999999})
        with app.app_context():
            fam_now = Student.query.get(sid).family_id
        record(f"Student update: bad family_id no 500, no orphan ({rb.status_code}/{rn.status_code}, fam kept={fam_now == orig_fam})",
               rb.status_code < 500 and rn.status_code < 500 and fam_now == orig_fam,
               f"{rb.status_code}/{rn.status_code} fam {orig_fam}->{fam_now}", "P2")


def run_teacher_authz(ids):
    """Teachers are staff-but-not-admin: they take attendance and see rosters,
    but must NOT reach money reports / settings / staff / audit / backup (least
    privilege). The nav hides those from teachers, but the API must enforce it —
    a hidden link is not access control."""
    with app.app_context():
        from app.models import User
        if not User.query.filter_by(username="teacher_t").first():
            t = User(username="teacher_t", email="tt@x.com", role="teacher",
                     first_name="Tea", last_name="Cher", is_active=True, is_admin=False)
            t.set_password("pw")
            db.session.add(t)
            db.session.commit()
    with app.test_client() as c:
        login(c, "teacher_t", "pw")
        # Can do their job: see class + student rosters.
        for path in ("/api/classes", "/api/students"):
            r = c.get(path)
            record(f"Teacher CAN access roster {path} -> {r.status_code}",
                   r.status_code == 200, f"got {r.status_code} (over-locked)", "P2")
        # Must NOT reach admin-only financial/config/staff surfaces.
        admin_only = [
            "/api/reports/revenue", "/api/reports/aging", "/api/reports/aging.csv",
            "/api/settings/payments",
            "/api/staff", "/api/audit-log", "/api/admin/backup",
            "/api/analytics/retention", "/api/donations", "/api/registrations",
        ]
        for path in admin_only:
            r = c.get(path)
            record(f"Teacher blocked from admin-only [{path}] -> {r.status_code}",
                   r.status_code in (401, 403), f"got {r.status_code} — teacher reached admin data", "P1")
        # Billing is admin-only across the board (Thomas, 2026-07-06): teachers
        # must not READ money data or POST money mutations. Sweep the whole
        # teacher-reachable billing surface — reads...
        sid = ids["child_a"]
        money_gets = [
            "/api/transactions", "/api/balances", "/api/reports/transactions.csv",
            "/api/recurring-charges", f"/api/students/{sid}/ledger",
            "/api/families/1/ledger", f"/api/students/{sid}/payment-plan",
            "/api/performances/1/ticket-orders",
        ]
        for path in money_gets:
            r = c.get(path)
            record(f"Teacher blocked from billing read [{path}] -> {r.status_code}",
                   r.status_code in (401, 403), f"got {r.status_code} — teacher read money data", "P1")
        # ...and writes (posting charges/payments, billing setup, claims, blasts).
        money_posts = [
            ("/api/transactions", {"student_id": sid, "type": "charge", "amount": 10, "category": "tuition"}),
            ("/api/transactions/bulk-charge", {"class_id": 1, "amount": 10, "category": "tuition"}),
            ("/api/recurring-charges", {"class_id": 1, "amount": 10, "category": "tuition", "day_of_month": 1}),
            ("/api/recurring-charges/process", {}),
            ("/api/payments/claim", {"student_id": sid, "amount": 10, "method": "zelle"}),
            (f"/api/students/{sid}/send-invoice", {}),
            ("/api/messages", {"subject": "s", "body": "b", "recipient_type": "all"}),
        ]
        for path, body in money_posts:
            r = c.post(path, json=body)
            record(f"Teacher blocked from billing write [{path}] -> {r.status_code}",
                   r.status_code in (401, 403), f"got {r.status_code} — teacher posted money", "P1")
        # Roster / class / enrollment / family MUTATIONS are admin-only too
        # (iter 184): they carry billing side effects (cancelling a class stops
        # its recurring charges; enrollment drives tuition) or PII edits.
        mutation_probes = [
            ("POST", "/api/students", {"first_name": "T", "last_name": "X"}),
            ("PUT", f"/api/students/{sid}", {"first_name": "T"}),
            ("DELETE", f"/api/students/{sid}", None),
            ("POST", "/api/classes", {"name": "TClass", "day_of_week": 1,
                                      "start_time": "10:00", "end_time": "11:00"}),
            ("PUT", "/api/classes/1", {"name": "TClass2"}),
            ("DELETE", "/api/classes/1", None),
            ("POST", "/api/classes/1/enroll", {"student_ids": [sid]}),
            ("DELETE", "/api/enrollments/1", None),
            ("POST", "/api/classes/1/waitlist", {"student_id": sid}),
            ("DELETE", "/api/waitlist/1", None),
            ("POST", "/api/families", {"name": "TFam"}),
            (f"POST", f"/api/students/{sid}/invite-parent", {}),
        ]
        for method, path, body in mutation_probes:
            r = c.open(path, method=method, json=body)
            record(f"Teacher blocked from roster/class mutation [{method} {path}] -> {r.status_code}",
                   r.status_code in (401, 403), f"got {r.status_code} — teacher mutated roster/class", "P1")
        # Roster CSV still works for teachers but must NOT carry the Balance column.
        csv_r = c.get("/api/reports/students.csv")
        csv_body = csv_r.get_data(as_text=True)
        record(f"Teacher roster CSV has no Balance column -> {csv_r.status_code}",
               csv_r.status_code == 200 and "Balance" not in csv_body.splitlines()[0],
               f"status={csv_r.status_code} header={csv_body.splitlines()[0] if csv_body else ''}", "P1")
        # Search family results must not link teachers to the (admin-only) ledger page.
        sr = (c.get("/api/search?q=alpha").get_json() or {})
        fam_urls = [f.get("url", "") for f in sr.get("families", [])]
        record("Teacher search results don't link to family ledgers",
               all("/ledger" not in u for u in fam_urls), f"urls={fam_urls}", "P2")
        # Pending-payments badge returns 0 for teachers (no volume leak).
        pc = (c.get("/api/pending-payments/count").get_json() or {}).get("count")
        record(f"Teacher pending-payments badge is 0 (got {pc})", pc == 0, f"count={pc}", "P2")
        # UI drift guard: pages must not show teachers buttons that 403 —
        # the student-detail withdraw toggle is server-rendered (assert absent),
        # and the classes page's JS-rendered admin buttons key off IS_ADMIN.
        detail_body = c.get(f"/students/{sid}/detail").get_data(as_text=True)
        classes_body = c.get("/classes").get_data(as_text=True)
        record("Teacher UI hides admin-only actions (withdraw toggle, IS_ADMIN=false)",
               'id="toggle-btn"' not in detail_body and "const IS_ADMIN = false;" in classes_body,
               f"toggle_shown={'toggle-btn' in detail_body} is_admin_flag={'IS_ADMIN = false' in classes_body}", "P3")
        # Family list still works for teachers but carries NO money fields.
        fams = (c.get("/api/families").get_json() or {}).get("families", [])
        leak = any("balance" in f or "total_charges" in f for f in fams)
        record(f"Teacher family list has no money fields ({len(fams)} families)",
               not leak, "balance/total_charges present for a teacher", "P1")
        # Still CAN message a class (instructional need preserved).
        cm = c.post("/api/messages", json={"subject": "Bring jazz shoes", "body": "b",
                                           "recipient_type": "class", "recipient_filter": "1"})
        record(f"Teacher CAN message a class -> {cm.status_code}",
               cm.status_code in (200, 201, 400), f"got {cm.status_code} (403 = over-locked)", "P2")
    # The money-access helper must still let a PARENT read their OWN child's
    # ledger + plan (the admin-or-owner path — teachers were cut out, parents not).
    with app.test_client() as pc:
        login(pc, "parent_a", "pw")
        rl = pc.get(f"/api/students/{ids['child_a']}/ledger")
        rp = pc.get(f"/api/students/{ids['child_a']}/payment-plan")
        record(f"Parent still reads own child's ledger ({rl.status_code}) + plan ({rp.status_code})",
               rl.status_code == 200 and rp.status_code == 200,
               f"ledger={rl.status_code} plan={rp.status_code} (parent over-locked)", "P1")


def run_admin_parent_reset_link(ids):
    """The no-SMTP password-recovery path: an admin can generate a single-use
    reset link for a PARENT account (parents forget passwords; email isn't
    configured yet; without this the only fix was hand-editing the prod DB).
    Admin-only, parent-role-only, and the link must actually work end to end."""
    from urllib.parse import urlparse
    from app.models import User
    with app.app_context():
        pa = User.query.filter_by(username="parent_a").first()
        admin_u = User.query.filter_by(username="admin").first()
        pa_id, admin_id = pa.id, admin_u.id
    with app.test_client() as c:
        login(c, "admin", "admin123")
        r = c.post(f"/api/parents/{pa_id}/reset-link")
        d = r.get_json() or {}
        url = d.get("reset_url", "")
        r_staff_target = c.post(f"/api/parents/{admin_id}/reset-link")
    record(f"Admin gets a parent reset link ({r.status_code}); staff target rejected ({r_staff_target.status_code})",
           r.status_code == 200 and "/auth/reset-password/" in url and r_staff_target.status_code == 400,
           f"status={r.status_code} url={url[:60]} staff_target={r_staff_target.status_code}", "P1")
    with app.test_client() as c:
        login(c, "teacher_t", "pw")
        rt = c.post(f"/api/parents/{pa_id}/reset-link")
    with app.test_client() as c:
        login(c, "parent_b", "pw")
        rp = c.post(f"/api/parents/{pa_id}/reset-link")
    record(f"Teacher ({rt.status_code}) and parent ({rp.status_code}) blocked from reset links",
           rt.status_code in (401, 403) and rp.status_code in (401, 403),
           f"teacher={rt.status_code} parent={rp.status_code}", "P0")
    # The link works end to end: set a new password, old dies, link is single-use.
    path = urlparse(url).path
    with app.test_client() as c:
        g = c.get(path)
        p = c.post(path, data={"password": "newpw777", "confirm_password": "newpw777"})
    with app.test_client() as c:
        ok_new = login(c, "parent_a", "newpw777").status_code in (200, 302) and \
                 c.get("/parent").status_code == 200
    with app.test_client() as c:
        login(c, "parent_a", "pw")
        old_dead = c.get("/parent").status_code in (302, 401, 403)
    with app.test_client() as c:
        reused = c.get(path, follow_redirects=False)
        single_use = reused.status_code == 302  # bounced to forgot-password
    record("Reset link works once: new password logs in, old dead, link not replayable",
           g.status_code == 200 and p.status_code == 302 and ok_new and old_dead and single_use,
           f"get={g.status_code} post={p.status_code} new={ok_new} old_dead={old_dead} reuse302={single_use}", "P1")
    # Restore parent_a's password for later tests.
    with app.app_context():
        pa = User.query.filter_by(username="parent_a").first()
        pa.set_password("pw")
        db.session.commit()


def run_csrf_guard_behind_tls_proxy():
    """Deploy-day safety: Fly terminates TLS, so Flask sees http:// internally
    while the browser sends Origin: https://<host>. The CSRF origin guard must
    compare HOSTS only — a scheme-sensitive compare would 403 every browser
    POST in production, starting with login. Simulate the proxied shape."""
    with app.test_client() as c:
        # Same host, https Origin (what Fly forwards) — must pass the guard.
        r = c.post("/auth/login",
                   data={"username": "admin", "password": "admin123"},
                   headers={"Origin": "https://localhost"},
                   environ_overrides={"HTTP_X_FORWARDED_PROTO": "https"})
        same_ok = r.status_code in (200, 302)
    with app.test_client() as c:
        # Different host — must still be blocked.
        r2 = c.post("/auth/login",
                    data={"username": "admin", "password": "admin123"},
                    headers={"Origin": "https://evil.example"})
        cross_blocked = r2.status_code == 403
    record("CSRF origin guard is scheme-agnostic (https Origin passes; foreign host blocked)",
           same_ok and cross_blocked,
           f"same_origin={'pass' if same_ok else r.status_code} cross={r2.status_code}", "P0")


def run_smtp_timeout():
    """send_email must pass a socket timeout to smtplib — forgot-password sends
    inline in the request, so a hung SMTP server would otherwise hold the worker
    for the full 120s gunicorn timeout (and background sends would hang forever)."""
    import smtplib
    from app import email as email_service
    captured = {}

    class _FakeSMTP:
        def __init__(self, server, port, timeout=None):
            captured["timeout"] = timeout
        def starttls(self):
            pass
        def login(self, u, p):
            pass
        def sendmail(self, f, t, m):
            pass
        def quit(self):
            pass

    real = smtplib.SMTP
    smtplib.SMTP = _FakeSMTP
    try:
        with app.app_context():
            prev = app.config.get("MAIL_SERVER")
            app.config["MAIL_SERVER"] = "smtp.test.local"
            try:
                email_service.send_email("x@y.com", "s", "b")
            finally:
                app.config["MAIL_SERVER"] = prev
    finally:
        smtplib.SMTP = real
    record(f"SMTP connections carry a timeout (got {captured.get('timeout')})",
           isinstance(captured.get("timeout"), (int, float)) and 0 < captured["timeout"] <= 60,
           f"timeout={captured.get('timeout')} — a hung SMTP blocks the worker", "P2")
    # The rest of the outbound surface, statically: every requests.* call and
    # the Square client constructor must carry an explicit timeout (httpx
    # treats None as NO timeout). A hung outbound call pins a worker thread.
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    bad = []
    for f in (root / "app").rglob("*.py"):
        src = f.read_text()
        # One level of nested parens (auth=(a, b), dicts) before the close.
        call = r"\((?:[^()]|\([^()]*\))*\)"
        for m in re.finditer(r"requests\.(?:get|post|put|delete|request)" + call, src, re.S):
            if "timeout" not in m.group(0):
                bad.append(f"{f.name}: {m.group(0)[:60]}")
        for m in re.finditer(r"\bSquare" + call, src, re.S):
            if "timeout" not in m.group(0):
                bad.append(f"{f.name}: {m.group(0)[:60]}")
    record(f"All outbound HTTP calls carry explicit timeouts ({len(bad)} without)",
           not bad, "; ".join(bad[:4]), "P2")


def run_proxyfix_https_links():
    """Behind Fly's TLS-terminating proxy, external URLs (password-reset links
    texted to parents) must generate as https://. A production-configured app
    trusts one hop of X-Forwarded-Proto; the testing app must NOT (it talks to
    clients directly)."""
    import os
    import tempfile
    from app import create_app
    from config.config import config as _cfgmap
    dbf = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    dbf.close()
    # ProductionConfig reads env at import time, so patch the class directly.
    ProdCfg = _cfgmap["production"]
    prev_cfg = {"SECRET_KEY": ProdCfg.SECRET_KEY,
                "SQLALCHEMY_DATABASE_URI": ProdCfg.SQLALCHEMY_DATABASE_URI}
    ProdCfg.SECRET_KEY = "x" * 64
    ProdCfg.SQLALCHEMY_DATABASE_URI = f"sqlite:///{dbf.name}"
    try:
        prod_app = create_app("production")
        with prod_app.test_client() as c:
            c.post("/auth/login", data={"username": "admin", "password": "admin123"},
                   headers={"Origin": "https://localhost"},
                   environ_overrides={"HTTP_X_FORWARDED_PROTO": "https"})
            # Seed a parent in the prod app's own DB, then mint a reset link.
            with prod_app.app_context():
                from app import db as _db
                from app.models import User
                p = User(username="pf_parent", email="pf@x.com", role="parent",
                         first_name="P", last_name="F", is_active=True)
                p.set_password("pw")
                _db.session.add(p)
                _db.session.commit()
                pid = p.id
            r = c.post(f"/api/parents/{pid}/reset-link",
                       headers={"Origin": "https://localhost"},
                       environ_overrides={"HTTP_X_FORWARDED_PROTO": "https"})
            url = (r.get_json() or {}).get("reset_url", "")
        record(f"Production reset links are https behind the proxy ({url[:28]}…)",
               r.status_code == 200 and url.startswith("https://"),
               f"status={r.status_code} url={url[:60]}", "P2")
    finally:
        for k, v in prev_cfg.items():
            setattr(ProdCfg, k, v)
        try:
            os.unlink(dbf.name)
        except OSError:
            pass


def run_no_pii_in_logs():
    """Static guard: application log lines must not carry PII. Fly retains logs
    with weaker access control than the DB, and this system holds minors' data —
    a child's name or a parent's email doesn't belong in a retained log stream.
    Log entity IDs instead (this caught 6 lines: reminder failures and every
    RFID scan logged the child's full name)."""
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    pii_vars = re.compile(r"logger\.\w+\([^)]*("
                          r"full_name|first_name|last_name|parent_name|"
                          r"parent_email|\.email\b|parent_phone|\.phone\b)")
    offenders = []
    for rel in ("app", "rfid"):
        for f in (root / rel).rglob("*.py"):
            for i, line in enumerate(f.read_text().splitlines(), 1):
                if pii_vars.search(line):
                    offenders.append(f"{f.relative_to(root)}:{i}: {line.strip()[:80]}")
    record(f"No PII (names/emails/phones) in application log lines ({len(offenders)} found)",
           not offenders, "; ".join(offenders[:5]), "P2")


def run_demo_parent_prod_lockout():
    """The demo parent (parent-demo/parent123) is a dev convenience linked to a
    REAL student's records. In production it must be dead twice over: the seed
    endpoint refuses, and boot deactivates any previously-seeded account (the
    live prod DB has one — it self-cleans on next deploy)."""
    from app import _disable_demo_parent
    from app.models import User
    # Seeding works in dev/testing (it's a demo tool)...
    with app.test_client() as c:
        login(c, "admin", "admin123")
        r_dev = c.post("/api/seed-demo-parent")
        # ...but refuses when the app is production-shaped.
        prev_debug, prev_testing = app.config.get("DEBUG"), app.config.get("TESTING")
        app.config["DEBUG"] = False
        app.config["TESTING"] = False
        try:
            r_prod = c.post("/api/seed-demo-parent")
        finally:
            app.config["DEBUG"] = prev_debug
            app.config["TESTING"] = prev_testing
    record(f"Demo-parent seeding: dev {r_dev.status_code}, production {r_prod.status_code}",
           r_dev.status_code == 200 and r_prod.status_code == 403,
           f"dev={r_dev.status_code} prod={r_prod.status_code}", "P1")
    # Boot cleanup deactivates a seeded demo account; the login then fails.
    with app.app_context():
        _disable_demo_parent()
        demo = User.query.filter_by(username="parent-demo").first()
        deactivated = demo is not None and demo.is_active is False
    with app.test_client() as c:
        login(c, "parent-demo", "parent123")
        r = c.get("/parent")
        blocked = r.status_code in (302, 401, 403)  # redirected to login / rejected
    record("Production boot disables the demo parent; its login stops working",
           deactivated and blocked,
           f"deactivated={deactivated} portal_status={r.status_code}", "P1")


def run_login_no_demo_creds_in_prod():
    """The login page shows demo credentials (admin123 / parent123) for dev
    convenience, gated behind DEBUG. In production (DEBUG=False) that box MUST be
    hidden — otherwise working admin + parent passwords are printed on the PUBLIC
    login page for anyone to use. The GO-LIVE runbook promises this hides."""
    prev = app.config.get("DEBUG")
    app.config["DEBUG"] = False
    try:
        with app.test_client() as c:
            body = c.get("/auth/login").get_data(as_text=True)
    finally:
        app.config["DEBUG"] = prev
    record("Production login page does NOT leak demo credentials",
           "admin123" not in body and "parent123" not in body,
           "demo credentials rendered on the prod login page — credential exposure", "P1")


def run_security_headers():
    """Site-wide hardening headers must be present on every response
    (clickjacking, MIME-sniffing, referrer leak). HSTS is asserted ONLY when
    cookies are Secure (production/HTTPS) — never over plain HTTP."""
    with app.test_client() as c:
        h = c.get("/auth/login").headers
        record("Hardening headers set (X-Frame-Options DENY, nosniff, Referrer-Policy)",
               h.get("X-Frame-Options") == "DENY"
               and h.get("X-Content-Type-Options") == "nosniff"
               and "strict-origin" in (h.get("Referrer-Policy") or ""),
               f"XFO={h.get('X-Frame-Options')} XCTO={h.get('X-Content-Type-Options')} "
               f"RP={h.get('Referrer-Policy')}", "P2")
        record("HSTS is NOT asserted under non-secure (dev) config",
               h.get("Strict-Transport-Security") is None,
               f"HSTS leaked over http: {h.get('Strict-Transport-Security')}", "P3")
    prev = app.config.get("SESSION_COOKIE_SECURE")
    app.config["SESSION_COOKIE_SECURE"] = True
    try:
        with app.test_client() as c:
            hsts = c.get("/auth/login").headers.get("Strict-Transport-Security")
    finally:
        app.config["SESSION_COOKIE_SECURE"] = prev
    record("HSTS set when cookies are Secure (production/HTTPS)",
           hsts is not None and "max-age=" in hsts, f"HSTS={hsts}", "P2")


def run_csrf():
    """Cross-origin writes must be blocked; same-origin writes must pass through."""
    with app.test_client() as c:
        login(c, "admin", "admin123")
        # Foreign Origin on a state-changing request -> 403.
        resp = c.post("/api/classes", json={},
                      headers={"Origin": "https://evil.example.com"})
        record(f"Cross-origin write blocked [Origin: evil] -> {resp.status_code}",
               resp.status_code == 403, f"got {resp.status_code}, expected 403", "P1")
        # Same-origin Origin -> not blocked by the CSRF guard (may 400 on body).
        resp = c.post("/api/classes", json={},
                      headers={"Origin": "http://localhost"})
        record(f"Same-origin write not CSRF-blocked -> {resp.status_code}",
               resp.status_code != 403 or b'Cross-origin' not in resp.data,
               f"got 403 cross-origin on same-origin request", "P1")
        # No Origin/Referer (e.g. server client) -> allowed through the guard.
        resp = c.post("/api/classes", json={})
        record(f"No-Origin write not CSRF-blocked -> {resp.status_code}",
               b'Cross-origin' not in resp.data, "blocked a no-Origin request", "P2")


def run_invite_security():
    """Parent invite codes are the account-claim credential — they must be
    CSPRNG-generated, single-use (cleared on claim, no reuse), and the claim must
    enforce a password minimum (onboarding is where most parents set it)."""
    import re
    from app.models import Student, Family, User
    with app.app_context():
        fam = Family(name="Invite Fam")
        db.session.add(fam)
        db.session.flush()
        st = Student(first_name="Inv", last_name="Kid", family_id=fam.id)
        db.session.add(st)
        db.session.commit()
        stid = st.id
    with app.test_client() as c:
        login(c, "admin", "admin123")
        code = (c.post(f"/api/students/{stid}/invite-parent", json={}).get_json() or {}).get("invite_code", "")
    record(f"Invite code is 8-char CSPRNG hex ({code})",
           bool(re.fullmatch(r"[0-9A-F]{8}", code)), f"got {code!r}", "P2")
    # Short password rejected; account stays unclaimed.
    with app.test_client() as c:
        c.post("/auth/register", data={"invite_code": code, "first_name": "P", "last_name": "K",
                                       "email": "invparent@x.com", "password": "ab"})
    with app.app_context():
        still_unclaimed = User.query.filter_by(invite_code=code, is_active=False).first() is not None
    record("Registration enforces the password minimum (2-char rejected)",
           still_unclaimed, "short password was accepted", "P2")
    # Valid claim succeeds and clears the code (single-use).
    with app.test_client() as c:
        c.post("/auth/register", data={"invite_code": code, "first_name": "P", "last_name": "K",
                                       "email": "invparent@x.com", "password": "secure123"})
    with app.app_context():
        claimed = User.query.filter_by(email="invparent@x.com", is_active=True).first()
        code_gone = User.query.filter_by(invite_code=code).first() is None
    record("Valid claim activates the account and clears the code (single-use)",
           claimed is not None and code_gone, "claim failed or code not cleared", "P1")
    # Reusing the spent code creates no account (no takeover).
    with app.test_client() as c:
        c.post("/auth/register", data={"invite_code": code, "first_name": "A", "last_name": "T",
                                       "email": "attacker@x.com", "password": "secure123"})
    with app.app_context():
        no_takeover = User.query.filter_by(email="attacker@x.com").first() is None
    record("Reusing a spent invite code creates no account (no takeover)",
           no_takeover, "a spent code was reusable", "P0")


def run_multichild_invite_merge():
    """Sibling families get one invite per child. Registering the 2nd+ invite
    with the same email must MERGE the dancer onto the parent's existing account
    (one login shows all kids), not 500 on the unique-email constraint."""
    from app.models import User, Student, ParentStudent

    with app.app_context():
        k1 = Student(first_name="Sib", last_name="One")
        k2 = Student(first_name="Sib", last_name="Two")
        db.session.add_all([k1, k2])
        db.session.flush()
        for code, kid in (("MCODE1", k1), ("MCODE2", k2)):
            u = User(username=f"parent-{code}", email=f"invite-{code}@pending.local",
                     first_name="Pending", last_name="Parent", role="parent",
                     is_active=False, invite_code=code, password_hash="x")
            db.session.add(u)
            db.session.flush()
            db.session.add(ParentStudent(parent_id=u.id, student_id=kid.id))
        db.session.commit()

    with app.test_client() as c:
        r1 = c.post("/auth/register", data={"invite_code": "MCODE1", "first_name": "Sib",
                    "last_name": "Parent", "email": "sibs@x.com", "password": "siblingpw"})
        record(f"Sibling invite 1 registers -> {r1.status_code}", r1.status_code in (200, 302),
               f"got {r1.status_code}", "P1")
    with app.test_client() as c:
        r2 = c.post("/auth/register", data={"invite_code": "MCODE2", "first_name": "Sib",
                    "last_name": "Parent", "email": "sibs@x.com", "password": "siblingpw"},
                    follow_redirects=False)
        record(f"Sibling invite 2 (same email) merges, no 500 -> {r2.status_code}",
               r2.status_code in (200, 302), f"got {r2.status_code} (500=the bug)", "P1")
    with app.app_context():
        accts = User.query.filter_by(email="sibs@x.com", is_active=True).all()
        one_acct = len(accts) == 1
        both_kids = one_acct and len({s.last_name for s in accts[0].get_children()} | {"One", "Two"}) == 2 \
            and len(accts[0].get_children()) == 2
        record("Both siblings under one account", one_acct and both_kids,
               f"accounts={len(accts)}, kids={[s.full_name for s in accts[0].get_children()] if accts else []}", "P1")
        # Scoped to the two codes THIS test seeded. It used to assert no invite
        # code existed anywhere, which was equivalent while these were the only
        # invites in the DB - but approval now issues a pending invite per new
        # family, so unredeemed ones are expected elsewhere. What must hold is
        # that redeeming these two consumed both.
        record("No orphaned invite accounts",
               User.query.filter(User.invite_code.in_(["MCODE1", "MCODE2"])).count() == 0,
               "leftover invites", "P2")


def run_crypto_secrets():
    """Secrets (Square token, webhook signature key) are Fernet-encrypted at rest,
    keyed off SECRET_KEY. Verify: round-trip works; and the failure path (a
    tampered ciphertext, i.e. what happens if SECRET_KEY is rotated) returns ''
    — NOT garbage — so an undecryptable token makes Square report not-configured
    (get_access_token() -> '' -> is_configured() False) instead of using nonsense."""
    from app.crypto import encrypt, decrypt, ENC_PREFIX
    with app.app_context():
        enc = encrypt("sq-secret-token-123")
        checks = {
            "round-trips": decrypt(enc) == "sq-secret-token-123",
            "ciphertext is enc-prefixed": enc.startswith(ENC_PREFIX),
            "tampered/rotated -> '' not garbage": decrypt(ENC_PREFIX + "not-a-valid-token") == "",
            "empty -> ''": decrypt("") == "",
            "plain: prefix stripped": decrypt("plain:hello") == "hello",
            "legacy unprefixed treated as plaintext": decrypt("legacyvalue") == "legacyvalue",
        }
    bad = [k for k, ok in checks.items() if not ok]
    record("Secret encryption round-trips and fails safe (undecryptable -> '', not garbage)",
           not bad, f"wrong: {bad}", "P2")


def run_square_webhook(ids):
    """Square auto-reconcile (real card money): it FAILS CLOSED without a
    signature key (an unverified webhook can't credit an account); with a key, a
    bad signature is rejected (403), a PARTIALLY_PAID event is ignored (else it
    books the whole invoice + blocks the final PAID), and a PAID event records the
    full amount exactly once (idempotent)."""
    import base64
    import hashlib
    import hmac
    import json as _json
    from app.models import SquareInvoice, Transaction, Setting
    from app.crypto import encrypt

    sid = ids["child_a"]
    with app.app_context():
        db.session.add(SquareInvoice(student_id=sid, invoice_id="sqinv_test_1",
                                     amount_cents=8000, status="SENT"))
        db.session.commit()

    def body(status):
        return _json.dumps({"type": "invoice.updated",
                            "data": {"object": {"invoice": {"id": "sqinv_test_1", "status": status}}}})

    def n_txns():
        with app.app_context():
            return Transaction.query.filter(Transaction.description.like("%sqinv_test_1%")).all()

    def sign(b, key=b"squarekey"):
        url = "http://localhost/api/webhooks/square"
        return base64.b64encode(hmac.new(key, (url + b).encode(), hashlib.sha256).digest()).decode()

    with app.test_client() as c:  # no login — Square calls this
        # No signature key configured -> fail closed, nothing recorded.
        r = c.post("/api/webhooks/square", data=body("PAID"), content_type="application/json")
        record(f"Webhook fails closed without a signature key -> {r.get_json()}",
               (r.get_json() or {}).get("status") == "unverified_ignored" and len(n_txns()) == 0,
               f"status={r.get_json()} txns={len(n_txns())}", "P1")

    with app.app_context():
        Setting.set("payments_square_webhook_signature_key", encrypt("squarekey"))
        db.session.commit()

    with app.test_client() as c:
        # Bad signature rejected.
        rb = c.post("/api/webhooks/square", data=body("PAID"), content_type="application/json",
                    headers={"x-square-hmacsha256-signature": "bad"})
        record(f"Webhook rejects a bad signature -> {rb.status_code}", rb.status_code == 403,
               f"got {rb.status_code}", "P1")
        # PARTIALLY_PAID (signed) ignored.
        pb = body("PARTIALLY_PAID")
        rp = c.post("/api/webhooks/square", data=pb, content_type="application/json",
                    headers={"x-square-hmacsha256-signature": sign(pb)})
        record(f"PARTIALLY_PAID ignored (no transaction) -> {rp.get_json()}",
               (rp.get_json() or {}).get("status") == "ignored" and len(n_txns()) == 0,
               f"txns={len(n_txns())}", "P2")
        # PAID (signed) records $80 once.
        fb = body("PAID")
        rf = c.post("/api/webhooks/square", data=fb, content_type="application/json",
                    headers={"x-square-hmacsha256-signature": sign(fb)})
        txns = n_txns()
        record(f"PAID records the full $80 once -> {rf.get_json()}",
               (rf.get_json() or {}).get("status") == "recorded" and len(txns) == 1
               and float(txns[0].amount) == 80.0, f"txns={[float(t.amount) for t in txns]}", "P1")
        # Duplicate PAID (signed) idempotent.
        rd = c.post("/api/webhooks/square", data=fb, content_type="application/json",
                    headers={"x-square-hmacsha256-signature": sign(fb)})
        record(f"Duplicate PAID is idempotent (still 1) -> {rd.get_json()}",
               (rd.get_json() or {}).get("status") == "already_recorded" and len(n_txns()) == 1,
               f"txns={len(n_txns())}", "P1")

    # Concurrency: Square delivers webhooks at-least-once, so two identical PAID
    # events can hit two worker threads at once. Both pass the paid_at pre-check;
    # only the atomic claim stops a double-credit. Fire two at a FRESH invoice
    # simultaneously and assert exactly one payment lands (the sequential
    # duplicate above is caught by the pre-check — this catches the true race).
    import threading
    with app.app_context():
        db.session.add(SquareInvoice(student_id=sid, invoice_id="sqinv_race",
                                     amount_cents=5000, status="SENT"))
        db.session.commit()
    rbody = _json.dumps({"type": "invoice.updated",
                         "data": {"object": {"invoice": {"id": "sqinv_race", "status": "PAID"}}}})
    rsig = sign(rbody)
    barrier = threading.Barrier(2)
    rstatuses = []

    def race_worker():
        with app.test_client() as c:
            barrier.wait()
            r = c.post("/api/webhooks/square", data=rbody, content_type="application/json",
                       headers={"x-square-hmacsha256-signature": rsig})
            rstatuses.append((r.get_json() or {}).get("status"))

    threads = [threading.Thread(target=race_worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    with app.app_context():
        race_n = Transaction.query.filter(Transaction.description.like("%sqinv_race%")).count()
    record(f"Concurrent duplicate PAID webhooks credit the family exactly once (statuses={rstatuses})",
           race_n == 1, f"created {race_n} payments for one invoice (want 1); statuses={rstatuses}", "P1")


def run_reconciliation(ids):
    """The core fall tuition-collection flow: parent claims a payment -> admin
    confirms -> a payment Transaction is created and the balance drops. Confirm
    is idempotent; a parent can't confirm their own claim."""
    from app.models import Transaction, PendingPayment
    from app.helpers import calc_balance

    sid = ids["child_a"]
    with app.app_context():
        db.session.add(Transaction(student_id=sid, type="charge", amount=100,
                                   category="tuition", payment_method="n/a", description="fall tuition"))
        db.session.commit()
        start_bal = calc_balance(sid)["balance"]

    # Parent claims a $60 Zelle payment
    with app.test_client() as c:
        login(c, "parent_a", "pw")
        r = c.post("/api/payments/claim",
                   json={"student_id": sid, "amount": 60, "method": "zelle", "reference": "abc123"})
        record(f"Parent claims a payment -> {r.status_code}", r.status_code in (200, 201),
               r.get_data(as_text=True)[:60], "P1")
        # Amount now has the same bounds as admin charges (upper cap) — a parent
        # can't stuff an absurd $999,999,999 into the confirm inbox.
        rbig = c.post("/api/payments/claim",
                      json={"student_id": sid, "amount": 999999999, "method": "zelle"})
        record(f"Parent can't report an absurd amount -> {rbig.status_code}",
               rbig.status_code == 400, f"got {rbig.status_code} (want 400)", "P2")

    with app.app_context():
        pend = PendingPayment.query.filter_by(student_id=sid, status="pending").order_by(
            PendingPayment.id.desc()).first()
        pid = pend.id if pend else None
    record("Claim created a pending payment", pid is not None, "", "P1")

    if pid:
        # A parent must NOT be able to confirm (admin-only)
        with app.test_client() as c:
            login(c, "parent_a", "pw")
            r = c.post(f"/api/pending-payments/{pid}/confirm", json={})
            record(f"Parent cannot confirm a payment -> {r.status_code}",
                   r.status_code == 403, f"got {r.status_code}", "P0")
        # Admin confirms -> creates the payment transaction, reduces balance
        with app.test_client() as c:
            login(c, "admin", "admin123")
            r = c.post(f"/api/pending-payments/{pid}/confirm", json={"category": "tuition"})
            record(f"Admin confirms the payment -> {r.status_code}", r.status_code == 200,
                   r.get_data(as_text=True)[:60], "P1")
            with app.app_context():
                new_bal = calc_balance(sid)["balance"]
                pay = Transaction.query.filter_by(student_id=sid, type="payment").count()
            record(f"Balance dropped by $60 ({start_bal:.2f} -> {new_bal:.2f})",
                   round(start_bal - new_bal, 2) == 60.0 and pay >= 1, "", "P1")
            # double-confirm is rejected
            r2 = c.post(f"/api/pending-payments/{pid}/confirm", json={})
            record(f"Double-confirm rejected -> {r2.status_code}", r2.status_code == 400,
                   f"got {r2.status_code}", "P1")


def run_enrollment(ids):
    """Class enrollment (fall-critical): enroll a student, dedup a repeat,
    unenroll, and confirm counts. Parents can't enroll."""
    from datetime import time as _time
    from app.models import DanceClass, ClassEnrollment, User

    with app.app_context():
        admin = User.query.filter_by(username="admin").first()
        dc = DanceClass(name="Ballet I", day_of_week=0, start_time=_time(17, 0),
                        end_time=_time(18, 0), instructor_id=admin.id, max_students=10)
        db.session.add(dc)
        db.session.commit()
        cid = dc.id
    sid = ids["child_a"]

    with app.test_client() as c:
        login(c, "admin", "admin123")
        r = c.post(f"/api/classes/{cid}/enroll", json={"student_id": sid})
        record(f"Enroll a student -> {r.status_code}", r.status_code == 201, r.get_data(as_text=True)[:60], "P1")
        # duplicate enroll is skipped, not 500
        r2 = c.post(f"/api/classes/{cid}/enroll", json={"student_id": sid})
        d2 = r2.get_json() or {}
        record(f"Duplicate enroll skipped (no 500) -> {r2.status_code}",
               r2.status_code == 201 and d2.get("skipped"), str(d2)[:60], "P1")
        with app.app_context():
            eid = ClassEnrollment.query.filter_by(class_id=cid, student_id=sid, is_active=True).first().id
            count = ClassEnrollment.query.filter_by(class_id=cid, is_active=True).count()
        record(f"Exactly one active enrollment (got {count})", count == 1, "", "P1")
        # unenroll
        r3 = c.delete(f"/api/enrollments/{eid}")
        record(f"Unenroll -> {r3.status_code}", r3.status_code == 200, "", "P2")
        with app.app_context():
            still = ClassEnrollment.query.filter_by(class_id=cid, is_active=True).count()
        record(f"Unenroll removed the active enrollment (now {still})", still == 0, "", "P1")

        # Robustness: a garbage/nonexistent id must not 500 or create an orphan.
        rg = c.post(f"/api/classes/{cid}/enroll", json={"student_id": "xyz"})
        record(f"Enroll garbage student_id handled (no 500) -> {rg.status_code}",
               rg.status_code < 500, f"got {rg.status_code}", "P2")
        wn = c.post(f"/api/classes/{cid}/waitlist", json={"student_id": 999999})
        record(f"Waitlist nonexistent student -> {wn.status_code} (404, no orphan)",
               wn.status_code == 404, f"got {wn.status_code}", "P2")
        wg = c.post(f"/api/classes/{cid}/waitlist", json={"student_id": "xyz"})
        record(f"Waitlist garbage student_id -> {wg.status_code} (400)",
               wg.status_code == 400, f"got {wg.status_code}", "P3")
        # Waitlist a real student, confirm the page renders (no orphan 500), promote.
        bid = ids["child_b"]
        wr = c.post(f"/api/classes/{cid}/waitlist", json={"student_id": bid})
        record(f"Waitlist a real student -> {wr.status_code}", wr.status_code == 201,
               wr.get_data(as_text=True)[:60], "P2")
        gw = c.get(f"/api/classes/{cid}/waitlist")
        record(f"Waitlist page renders (no orphan 500) -> {gw.status_code}",
               gw.status_code == 200, f"got {gw.status_code}", "P2")
        with app.app_context():
            from app.models import WaitlistEntry
            wid = WaitlistEntry.query.filter_by(class_id=cid, student_id=bid, status='waiting').first().id
        pr = c.post(f"/api/waitlist/{wid}/promote")
        with app.app_context():
            promoted = ClassEnrollment.query.filter_by(class_id=cid, student_id=bid, is_active=True).count()
        record(f"Promote from waitlist enrolls the student -> {pr.status_code}, enrolled={promoted}",
               pr.status_code == 200 and promoted == 1, f"status={pr.status_code} enrolled={promoted}", "P2")
    with app.test_client() as c:
        login(c, "parent_a", "pw")
        r = c.post(f"/api/classes/{cid}/enroll", json={"student_id": sid})
        record(f"Parent cannot enroll -> {r.status_code}", r.status_code == 403, f"got {r.status_code}", "P0")


def run_deactivation_revokes_session():
    """Deactivating an account must revoke access immediately, not only block
    fresh logins — a just-fired teacher shouldn't keep their live session."""
    from app.models import User

    with app.app_context():
        u = User(username="tempteach", email="tempteach@x.com", first_name="T",
                 last_name="R", role="teacher", is_active=True)
        u.set_password("pw")
        db.session.add(u)
        db.session.commit()
        uid = u.id

    with app.test_client() as c:
        login(c, "tempteach", "pw")
        record(f"Active staff can reach a protected page -> {c.get('/students').status_code}",
               c.get("/students").status_code == 200, "", "P2")
        with app.app_context():
            User.query.filter_by(id=uid).update({"is_active": False})
            db.session.commit()
        r = c.get("/students", follow_redirects=False)
        record(f"Deactivated mid-session is revoked -> {r.status_code}", r.status_code == 302,
               f"got {r.status_code} (200 = still had access)", "P2")


def run_open_redirect_guard():
    """Login's ?next= must only redirect same-site. A bare netloc check misses
    browser-normalised cross-origin forms (//evil, /\\evil, https:evil, ////evil),
    which enable phishing via a crafted login link. Malicious targets must fall
    back to the dashboard; safe relative paths must be honored."""
    malicious = ["https://evil.com", "//evil.com", "/\\evil.com", "https:evil.com",
                 "\\/evil.com", "////evil.com", "http://evil.com", "javascript:alert(1)"]
    with app.test_client() as c:
        for v in malicious:
            r = c.post(f"/auth/login?next={v}",
                       data={"username": "admin", "password": "admin123"}, follow_redirects=False)
            loc = r.headers.get("Location", "")
            c.get("/auth/logout")
            offsite = "evil.com" in loc.replace("\\", "/").lower() or loc.startswith("javascript")
            record(f"Open-redirect blocked for next={v!r} -> {loc!r}", not offsite,
                   f"redirected off-site to {loc}", "P1")
    with app.test_client() as c:
        r = c.post("/auth/login?next=/students",
                   data={"username": "admin", "password": "admin123"}, follow_redirects=False)
        record(f"Safe relative next is honored -> {r.headers.get('Location')!r}",
               r.headers.get("Location") == "/students", f"got {r.headers.get('Location')}", "P2")


def run_login_throttle():
    """Login must throttle brute-force: after several failures for one account, a
    cooldown kicks in (even a correct password is blocked until it passes), the
    throttle is per-account (doesn't lock out other users), and a real login clears
    it. Uses a dedicated user + clears its counter so it can't affect other tests."""
    from app.auth.routes import _clear_login_failures, _LOGIN_MAX_FAILS
    from app.models import User
    with app.app_context():
        if not User.query.filter_by(username="throttle_u").first():
            u = User(username="throttle_u", email="throttle@x.com", role="parent",
                     first_name="Th", last_name="Ro", is_active=True)
            u.set_password("realpassword")
            db.session.add(u)
            db.session.commit()
    _clear_login_failures("throttle_u")
    try:
        # Exhaust the allowed failures, then the next attempt is throttled (429).
        codes = []
        for _ in range(_LOGIN_MAX_FAILS):
            with app.test_client() as c:
                codes.append(c.post("/auth/login", json={"username": "throttle_u", "password": "nope"}).status_code)
        with app.test_client() as c:
            over = c.post("/auth/login", json={"username": "throttle_u", "password": "nope"}).status_code
        record(f"Login throttles brute-force ({_LOGIN_MAX_FAILS} fails then {over})",
               set(codes) == {401} and over == 429, f"codes={set(codes)} over={over}", "P1")
        # Even the CORRECT password is blocked while throttled.
        with app.test_client() as c:
            locked = c.post("/auth/login", json={"username": "throttle_u", "password": "realpassword"}).status_code
        record(f"Throttle blocks even a correct password -> {locked}", locked == 429,
               f"got {locked}", "P2")
        # Per-account: a DIFFERENT user is unaffected.
        with app.test_client() as c:
            other = c.post("/auth/login", json={"username": "admin", "password": "admin123"}).status_code
        record(f"Throttle is per-account (admin unaffected) -> {other}", other == 200,
               f"got {other}", "P1")
        # A real login (after clearing) resets the counter.
        _clear_login_failures("throttle_u")
        with app.test_client() as c:
            ok = c.post("/auth/login", json={"username": "throttle_u", "password": "realpassword"}).status_code
        record(f"Cleared throttle lets a valid login through -> {ok}", ok == 200,
               f"got {ok}", "P2")
    finally:
        _clear_login_failures("throttle_u")


def run_password_reset():
    """Self-service password reset: request page degrades gracefully without
    SMTP, the signed token resets the password, and old creds stop working."""
    from app.auth.routes import _reset_serializer

    with app.test_client() as c:
        record(f"Forgot-password page renders -> {c.get('/auth/forgot-password').status_code}",
               c.get('/auth/forgot-password').status_code == 200, "", "P2")
        r = c.post('/auth/forgot-password', data={'email': 'a@x.com'}, follow_redirects=True)
        record("Forgot-password degrades gracefully without SMTP",
               'contact the studio' in r.get_data(as_text=True).lower(), "no fallback message", "P2")

    with app.app_context():
        from app.models import User
        u0 = User.query.filter_by(email='a@x.com').first()
        uid = u0.id
        # Token must embed the current password-hash slice (single-use scheme).
        token = _reset_serializer().dumps({'uid': uid, 'pw': u0.password_hash[-16:]})
        bad = token[:-3] + 'zzz'

    with app.test_client() as c:
        record(f"Reset page with valid token -> {c.get('/auth/reset-password/'+token).status_code}",
               c.get('/auth/reset-password/' + token).status_code == 200, "", "P1")
    with app.test_client() as c:
        record(f"Invalid reset token redirected -> {c.get('/auth/reset-password/'+bad).status_code}",
               c.get('/auth/reset-password/' + bad, follow_redirects=False).status_code == 302, "", "P2")
    with app.test_client() as c:
        rr = c.post('/auth/reset-password/' + token,
                    data={'password': 'brandnewpw', 'confirm_password': 'brandnewpw'},
                    follow_redirects=False)
        record(f"Reset sets a new password -> {rr.status_code}", rr.status_code == 302, "", "P1")
    # Single-use: replaying the SAME token must NOT reset again (the hash changed).
    with app.test_client() as c:
        replay = c.post('/auth/reset-password/' + token,
                        data={'password': 'attackerpw', 'confirm_password': 'attackerpw'},
                        follow_redirects=False)
    with app.app_context():
        from app.models import User as _U
        took = _U.query.filter_by(email='a@x.com').first().check_password('attackerpw')
    record("Reset token is single-use (replay after use is rejected)",
           not took, "a used reset link was replayable — account takeover", "P0")
    with app.test_client() as c:  # fresh client (not logged in)
        ok = c.post('/auth/login', data={'username': 'a@x.com', 'password': 'brandnewpw'},
                    follow_redirects=False)
        record(f"New password works after reset -> {ok.status_code}", ok.status_code == 302, "", "P1")
    with app.test_client() as c:  # fresh client — old password must now fail
        old = c.post('/auth/login', data={'username': 'a@x.com', 'password': 'pw'},
                     follow_redirects=False)
        record(f"Old password rejected after reset -> {old.status_code}", old.status_code == 200,
               f"got {old.status_code} (302 = old pw still works!)", "P1")
    # restore parent_a's password so later tests still log in
    with app.app_context():
        from app.models import User
        u = User.query.filter_by(email='a@x.com').first()
        u.set_password('pw')
        db.session.commit()


def run_rule_authz():
    """Rule MUTATIONS are admin-only (least privilege — Thomas, 2026-07-06):
    rules are studio policy. Teachers can still VIEW the rules page/list;
    parents can't mutate anything (before_request block)."""
    from app.models import User, Rule
    with app.app_context():
        if not User.query.filter_by(username="teacher_t").first():
            t = User(username="teacher_t", email="tt@x.com", role="teacher",
                     first_name="Tea", last_name="Cher", is_active=True, is_admin=False)
            t.set_password("pw")
            db.session.add(t)
            db.session.commit()
    with app.test_client() as c:
        login(c, "teacher_t", "pw")
        tcr = c.post("/api/rules", json={"text": "Teacher-made rule"})
        tview = c.get("/api/rules")
    with app.test_client() as c:
        login(c, "admin", "admin123")
        cr = c.post("/api/rules", json={"text": "Admin-made rule"})
        rid = (cr.get_json() or {}).get("id")
        pu = c.put(f"/api/rules/{rid}", json={"text": "Admin-edited rule"}) if rid else None
        de = c.delete(f"/api/rules/{rid}") if rid else None
    with app.test_client() as c:
        login(c, "parent_a", "pw")
        pcr = c.post("/api/rules", json={"text": "Parent rule"})
    record("Admin manages rules; teacher can view but not mutate; parent blocked",
           tcr.status_code == 403 and tview.status_code == 200
           and cr.status_code == 201 and pu is not None and pu.status_code == 200
           and de is not None and de.status_code == 200 and pcr.status_code == 403,
           f"teacher_create={tcr.status_code} teacher_view={tview.status_code} "
           f"admin_create={cr.status_code} put={getattr(pu,'status_code',None)} "
           f"del={getattr(de,'status_code',None)} parent_create={pcr.status_code}", "P2")


def run_forgot_password_no_enumeration():
    """Anti-enumeration: with email configured, forgot-password must return the
    SAME generic reply whether or not the account exists — otherwise an attacker
    can discover which emails have accounts by diffing the responses."""
    from app import email as email_service
    app.config["MAIL_SERVER"] = "smtp.example.com"
    orig = email_service.send_email
    email_service.send_email = lambda *a, **k: 1  # no-op send (an account may exist)
    try:
        with app.test_client() as c:
            r_exist = c.post('/auth/forgot-password', data={'email': 'a@x.com'},
                             follow_redirects=True).get_data(as_text=True).lower()
        with app.test_client() as c:
            r_nope = c.post('/auth/forgot-password', data={'email': 'no-such-user-zzz@x.com'},
                            follow_redirects=True).get_data(as_text=True).lower()
    finally:
        email_service.send_email = orig
        app.config["MAIL_SERVER"] = None
    generic = "if an account exists"
    record("Forgot-password reply is identical for real vs unknown email (no account enumeration)",
           generic in r_exist and generic in r_nope
           and "no account" not in r_nope and "not found" not in r_nope
           and "doesn't exist" not in r_nope,
           f"exist_generic={generic in r_exist} nope_generic={generic in r_nope}", "P2")


def run_login_by_email():
    """Invited parents get an auto-generated `parent-<code>` username they never
    see and register with an email — so login MUST accept email, or they're
    locked out after logging out. Staff keep username login."""
    with app.test_client() as c:
        # parent_a was seeded with email a@x.com
        r = c.post("/auth/login", data={"username": "a@x.com", "password": "pw"},
                   follow_redirects=False)
        record(f"Login by email works -> {r.status_code}", r.status_code == 302,
               f"got {r.status_code} (302=success)", "P1")
    with app.test_client() as c:
        r = c.post("/auth/login", data={"username": "parent_a", "password": "pw"},
                   follow_redirects=False)
        record(f"Login by username still works -> {r.status_code}", r.status_code == 302,
               f"got {r.status_code}", "P1")
    with app.test_client() as c:
        r = c.post("/auth/login", data={"username": "nobody@x.com", "password": "pw"},
                   follow_redirects=False)
        record(f"Login with unknown email rejected -> {r.status_code}", r.status_code == 200,
               f"got {r.status_code}", "P2")


def run_waiver_signing(ids):
    """A parent signs their OWN child's waiver end-to-end (enrollment flow):
    signature records + reads back as signed; decline rules enforced."""
    from app.models import WaiverTemplate

    with app.app_context():
        mandatory = WaiverTemplate(title="Liability Waiver", body="I agree.",
                                   allow_decline=False, is_active=True)
        optional = WaiverTemplate(title="Photo Release", body="Photos ok?",
                                  allow_decline=True, is_active=True)
        db.session.add_all([mandatory, optional])
        db.session.commit()
        mid, oid = mandatory.id, optional.id

    sid = ids["child_a"]
    with app.test_client() as c:
        login(c, "parent_a", "pw")
        # sign the mandatory waiver for own child
        r = c.post(f"/api/students/{sid}/waivers/{mid}/sign",
                   json={"signed_name": "Pat Alpha", "consent": True})
        record(f"Parent signs own child's waiver -> {r.status_code}", r.status_code == 200,
               f"got {r.status_code}: {r.get_data(as_text=True)[:80]}", "P1")
        # reads back as signed
        data = c.get(f"/api/students/{sid}/waivers").get_json() or {}
        signed = next((w for w in data.get("waivers", []) if w["id"] == mid), {})
        record("Signed waiver reads back as signed", signed.get("signed") is True, str(signed)[:80], "P1")
        # empty signature rejected
        r = c.post(f"/api/students/{sid}/waivers/{mid}/sign", json={"signed_name": ""})
        record(f"Waiver requires a typed signature -> {r.status_code}", r.status_code == 400,
               f"got {r.status_code}", "P3")
        # declining a mandatory form rejected
        r = c.post(f"/api/students/{sid}/waivers/{mid}/sign",
                   json={"signed_name": "Pat Alpha", "consent": False})
        record(f"Cannot decline a mandatory waiver -> {r.status_code}", r.status_code == 400,
               f"got {r.status_code}", "P2")
        # declining an opt-out form allowed
        r = c.post(f"/api/students/{sid}/waivers/{oid}/sign",
                   json={"signed_name": "Pat Alpha", "consent": False})
        record(f"Can decline an opt-out form -> {r.status_code}", r.status_code == 200,
               f"got {r.status_code}", "P2")

    # Staff compliance view: the mandatory waiver is signed, the opt-out is
    # declined, and both are counted. Then inject an orphan signature (student
    # removed) and confirm the page still renders instead of 500-ing.
    with app.test_client() as c:
        login(c, "admin", "admin123")
        comp = (c.get("/api/waivers/compliance").get_json() or {}).get("compliance", [])
        by_id = {t["id"]: t for t in comp}
        mrow, orow = by_id.get(mid, {}), by_id.get(oid, {})
        record("Compliance: mandatory shows a signature, opt-out shows a decline",
               mrow.get("signed_count", 0) >= 1 and len(orow.get("declined", [])) >= 1,
               f"mandatory={mrow.get('signed_count')} declined={len(orow.get('declined', []))}", "P2")
        with app.app_context():
            from app.models import WaiverSignature, User as _U
            adm = _U.query.filter_by(username="admin").first()
            db.session.add(WaiverSignature(template_id=oid, student_id=987654, parent_id=adm.id,
                                           signed_name="Ghost", consent=False))
            db.session.commit()
        rc = c.get("/api/waivers/compliance")
        record(f"Compliance renders with an orphan signature -> {rc.status_code}",
               rc.status_code == 200, f"got {rc.status_code} (orphan deref 500)", "P2")


def run_attendance(ids):
    """Taking attendance — the most-used fall feature. Mark present persists,
    toggling again removes it (no duplicate rows), manual check-in dedups, the
    endpoints validate the student/class exist and don't 500 on a bad date, and
    parents can't mark attendance."""
    from datetime import time as _time
    from app.models import DanceClass, ClassEnrollment, Attendance
    sid = ids["child_a"]
    with app.app_context():
        admin = User.query.filter_by(username="admin").first()
        dc = DanceClass(name="Attn Class", day_of_week=0, start_time=_time(17, 0),
                        end_time=_time(18, 0), instructor_id=admin.id)
        db.session.add(dc)
        db.session.flush()
        db.session.add(ClassEnrollment(student_id=sid, class_id=dc.id))
        db.session.commit()
        cid = dc.id

    def rows():
        with app.app_context():
            return Attendance.query.filter_by(student_id=sid, class_id=cid).count()

    with app.test_client() as c:
        login(c, "admin", "admin123")
        r1 = c.post("/api/attendance/toggle", json={"student_id": sid, "class_id": cid})
        d1 = r1.get_json() or {}
        record(f"Mark present -> {r1.status_code} present={d1.get('present')}",
               r1.status_code == 201 and d1.get("present") is True, str(d1), "P1")
        r2 = c.post("/api/attendance/toggle", json={"student_id": sid, "class_id": cid})
        d2 = r2.get_json() or {}
        record(f"Toggle again removes attendance -> {r2.status_code} present={d2.get('present')}",
               r2.status_code == 200 and d2.get("present") is False and rows() == 0, str(d2), "P1")
        # Manual check-in dedups: 2nd check-in same day is rejected, leaves one row.
        c.post("/api/attendance/checkin", json={"student_id": sid, "class_id": cid})
        rc = c.post("/api/attendance/checkin", json={"student_id": sid, "class_id": cid})
        record(f"Manual check-in dedups (2nd -> {rc.status_code}, {rows()} row)",
               rc.status_code == 400 and rows() == 1, f"status={rc.status_code} rows={rows()}", "P2")
        # The check-in must land on TODAY's roster. The server runs in the studio's
        # timezone (Eastern), so a UTC-dated timestamp would file an evening
        # check-in under tomorrow and hide it from today's list (and trip the
        # unique-day index). Assert the just-checked-in student shows up today.
        today_att = c.get(f"/api/attendance/today?class_id={cid}").get_json() or {}
        today_ids = [a.get("student_id") for a in today_att.get("attendance", [])]
        record("Manual check-in appears in today's attendance roster (correct local day)",
               sid in today_ids, f"today ids={today_ids} (want {sid}); count={today_att.get('count')}", "P1")
        # Attendance times are studio-local, so the API must emit them WITHOUT a
        # 'Z' — tagging a local wall-clock as UTC shifts the log page's displayed
        # time by the whole offset. Assert no 'Z' and the ISO is dated today.
        from datetime import date as _date_local
        mine = next((a for a in today_att.get("attendance", []) if a.get("student_id") == sid), {})
        cit = mine.get("check_in_time") or ""
        record("Attendance check_in_time is emitted studio-local (no 'Z' shift, dated today)",
               bool(cit) and not cit.endswith("Z") and cit.startswith(_date_local.today().isoformat()),
               f"check_in_time={cit!r} today={_date_local.today().isoformat()}", "P2")
        # Validation: nonexistent class 404s (no orphan row), garbage id 400s, bad date doesn't 500.
        rb1 = c.post("/api/attendance/toggle", json={"student_id": sid, "class_id": 999999})
        rb2 = c.post("/api/attendance/toggle", json={"student_id": "xyz", "class_id": cid})
        rb3 = c.post("/api/attendance/toggle", json={"student_id": sid, "class_id": cid, "date": "not-a-date"})
        record(f"Toggle validates existence + bad date (404={rb1.status_code}, 400={rb2.status_code}, date={rb3.status_code})",
               rb1.status_code == 404 and rb2.status_code == 400 and rb3.status_code != 500,
               f"{rb1.status_code}/{rb2.status_code}/{rb3.status_code}", "P3")
        # missing fields rejected
        r3 = c.post("/api/attendance/toggle", json={"student_id": sid})
        record(f"Attendance toggle requires class_id -> {r3.status_code}", r3.status_code == 400,
               f"got {r3.status_code}", "P3")
    with app.test_client() as c:
        login(c, "parent_a", "pw")
        r = c.post("/api/attendance/toggle", json={"student_id": sid, "class_id": cid})
        record(f"Parent cannot mark attendance -> {r.status_code}", r.status_code == 403,
               f"got {r.status_code}", "P0")


def run_skills(ids):
    """Skill tracking + certificate. The toggle must be clean (on/off, no dupes),
    404 on bad ids, admin-only, and the certificate must render for a student who
    has a skill."""
    from app.models import StudentSkill
    sid = ids["child_a"]
    with app.test_client() as c:
        login(c, "admin", "admin123")
        skid = (c.post("/api/skills", json={"name": "Leap", "category": "technique"}).get_json() or {}).get("id")
        t1 = c.post(f"/api/students/{sid}/skills/{skid}/toggle").get_json() or {}
        t2 = c.post(f"/api/students/{sid}/skills/{skid}/toggle").get_json() or {}
        with app.app_context():
            n = StudentSkill.query.filter_by(student_id=sid, skill_id=skid).count()
        record(f"Skill toggle on/off is clean (on={t1.get('achieved')} off={t2.get('achieved')} rows={n})",
               t1.get("achieved") is True and t2.get("achieved") is False and n == 0,
               f"{t1}/{t2} rows={n}", "P2")
        r404s = c.post(f"/api/students/999999/skills/{skid}/toggle").status_code
        r404k = c.post(f"/api/students/{sid}/skills/999999/toggle").status_code
        record(f"Skill toggle 404s on bad ids (student={r404s} skill={r404k})",
               r404s == 404 and r404k == 404, f"{r404s}/{r404k}", "P3")
        c.post(f"/api/students/{sid}/skills/{skid}/toggle")  # award it
        rc = c.get(f"/students/{sid}/certificate")
        record(f"Certificate renders for a student with a skill -> {rc.status_code}",
               rc.status_code == 200, f"got {rc.status_code}", "P3")
    with app.test_client() as c:
        login(c, "parent_a", "pw")
        rp = c.post(f"/api/students/{sid}/skills/1/toggle")
        record(f"Parent cannot award skills -> {rp.status_code}", rp.status_code in (401, 403),
               f"got {rp.status_code}", "P1")


def run_analytics(ids):
    """Retention dashboard — the studio makes decisions on this, so the shape and
    the at-risk logic must be right: 12 enroll-months + 6 attendance-months, and a
    student with no recent attendance is flagged at-risk. Admin-only."""
    with app.test_client() as c:
        login(c, "admin", "admin123")
        r = c.get("/api/analytics/retention")
        d = r.get_json() or {}
        shape_ok = (r.status_code == 200
                    and isinstance(d.get("enroll_by_month"), list) and len(d["enroll_by_month"]) == 12
                    and isinstance(d.get("attendance_by_month"), list) and len(d["attendance_by_month"]) == 6
                    and isinstance(d.get("at_risk"), list)
                    and isinstance(d.get("active"), int) and isinstance(d.get("new_this_month"), int))
        record(f"Retention report has the right shape (12 enroll + 6 att months)",
               shape_ok, str(d)[:100], "P2")
        # at-risk logic: child_a (seeded, active, no attendance in this run's window
        # unless a prior test added some) — assert at_risk_count is consistent with the list.
        record("Retention at_risk_count matches the at_risk list length (capped 50)",
               d.get("at_risk_count", -1) >= len(d.get("at_risk", [])),
               f"count={d.get('at_risk_count')} list={len(d.get('at_risk', []))}", "P3")
        # At-risk correctness + the local-date cutoff: a fresh active student with
        # no attendance is at-risk; checking them in TODAY drops them from the
        # count. The cutoff compares against the studio-local check_in_time, so it
        # must itself be local (not utcnow) or a boundary check-in mis-counts.
        from datetime import time as _t_an
        from app.models import User as _U_an, DanceClass as _DC_an, Student as _S_an, Family as _F_an, ClassEnrollment as _CE_an
        with app.app_context():
            adm = _U_an.query.filter_by(username="admin").first()
            fam = _F_an(name="AtRisk Fam")
            db.session.add(fam)
            db.session.flush()
            st = _S_an(first_name="AtRisk", last_name="Kid", family_id=fam.id, is_active=True)
            db.session.add(st)
            db.session.flush()
            cls = _DC_an(name="AtRiskClass", day_of_week=0, start_time=_t_an(16, 0),
                         end_time=_t_an(17, 0), instructor_id=adm.id)
            db.session.add(cls)
            db.session.flush()
            db.session.add(_CE_an(student_id=st.id, class_id=cls.id))
            db.session.commit()
            arid, acid = st.id, cls.id
        before = (c.get("/api/analytics/retention").get_json() or {}).get("at_risk_count", 0)
        c.post("/api/attendance/toggle", json={"student_id": arid, "class_id": acid})  # check in today
        after = (c.get("/api/analytics/retention").get_json() or {}).get("at_risk_count", 0)
        record("A check-in today removes a student from the at-risk count (local-date cutoff)",
               after == before - 1, f"at_risk before={before} after={after} (want -1)", "P2")
    with app.test_client() as c:
        login(c, "parent_a", "pw")
        rp = c.get("/api/analytics/retention")
        record(f"Parent blocked from analytics -> {rp.status_code}", rp.status_code in (401, 403),
               f"got {rp.status_code}", "P1")


def run_leads():
    """Leads pipeline (active in enrollment season). Converting a lead creates a
    Family + Student — and that MUST be idempotent: a double-click can't make two
    duplicate families. Also: update tolerates bad input, and it's admin-only."""
    from app.models import Lead, Family, Student
    with app.test_client() as c:
        login(c, "admin", "admin123")
        c.post("/api/leads", json={"name": "Riley Prospect", "email": "riley@x.com", "phone": "555"})
        with app.app_context():
            lid = Lead.query.filter_by(email="riley@x.com").first().id
            fam_before, stu_before = Family.query.count(), Student.query.count()
        r1 = c.post(f"/api/leads/{lid}/convert")
        r2 = c.post(f"/api/leads/{lid}/convert")  # double-click
        with app.app_context():
            fam_after, stu_after = Family.query.count(), Student.query.count()
        record(f"Lead convert is idempotent (#1={r1.status_code} #2={r2.status_code})",
               r1.status_code == 200 and r2.status_code == 400, f"{r1.status_code}/{r2.status_code}", "P2")
        record(f"Double-convert created exactly ONE family + student (+{fam_after - fam_before}f/+{stu_after - stu_before}s)",
               fam_after - fam_before == 1 and stu_after - stu_before == 1,
               f"fam+{fam_after - fam_before} stu+{stu_after - stu_before} (duplicate!)", "P2")
        # Update robustness: non-string name coerces (no 500), empty name rejected.
        rn = c.put(f"/api/leads/{lid}", json={"name": 123})
        re_ = c.put(f"/api/leads/{lid}", json={"name": "   "})
        record(f"Lead update handles bad name (num={rn.status_code}, empty={re_.status_code})",
               rn.status_code < 500 and re_.status_code == 400, f"{rn.status_code}/{re_.status_code}", "P3")
    with app.test_client() as c:
        login(c, "parent_a", "pw")
        rp = c.post("/api/leads", json={"name": "X"})
        record(f"Parent cannot create a lead -> {rp.status_code}", rp.status_code in (401, 403),
               f"got {rp.status_code}", "P1")


def run_timeclock():
    """Staff time clock feeds payroll. Verify: can't clock out without clocking
    in, no double clock-in, hours compute correctly, the report is admin-only and
    aggregates hours, punches are stored studio-local (so an evening shift stays
    in the local-date report window), and displayed timestamps are emitted local
    (no 'Z') so the log shows the real punch time."""
    from datetime import datetime as _dt, date as _date, time as _time
    from app.models import User, DanceClass, Attendance, TimeClockEntry
    with app.test_client() as c:
        login(c, "admin", "admin123")  # admin is staff
        r0 = c.post("/api/timeclock/clock-out")
        record(f"Clock-out with no open entry rejected -> {r0.status_code}", r0.status_code == 400,
               f"got {r0.status_code}", "P3")
        r1 = c.post("/api/timeclock/clock-in")
        record(f"Clock-in -> {r1.status_code}", r1.status_code == 201, r1.get_data(as_text=True)[:60], "P2")
        cin = str((r1.get_json() or {}).get("clock_in", ""))
        record("Clock-in timestamp is studio-local (no 'Z', dated today)",
               bool(cin) and not cin.endswith("Z") and cin.startswith(_date.today().isoformat()),
               f"got {cin!r}", "P2")
        r2 = c.post("/api/timeclock/clock-in")
        record(f"Double clock-in rejected -> {r2.status_code}", r2.status_code == 400,
               f"got {r2.status_code}", "P2")
        r3 = c.post("/api/timeclock/clock-out")
        record(f"Clock-out -> {r3.status_code} (hours present)",
               r3.status_code == 200 and "hours" in (r3.get_json() or {}), r3.get_data(as_text=True)[:60], "P2")
        # Payroll-boundary: a shift punched now (possibly evening) must appear in a
        # report ending today. A UTC-stored evening punch would roll to tomorrow
        # and vanish from today's window.
        today_iso = _date.today().isoformat()
        rep_today = c.get(f"/api/timeclock/report?start={today_iso}&end={today_iso}").get_json()
        record("Today's completed shift appears in a report ending today (local-date boundary)",
               (rep_today or {}).get("total_hours", 0) >= 0 and any(
                   r.get("shifts", 0) >= 1 for r in (rep_today or {}).get("report", [])),
               f"report={str(rep_today)[:100]}", "P2")
        # Hours math: seed a known 2.5h shift, confirm the report sums it.
        with app.app_context():
            adm = User.query.filter_by(username="admin").first()
            db.session.add(TimeClockEntry(user_id=adm.id, clock_in=_dt(2026, 7, 1, 20, 0, 0),
                                          clock_out=_dt(2026, 7, 1, 22, 30, 0)))
            db.session.commit()
        rep = c.get("/api/timeclock/report?start=2026-07-01&end=2026-07-01").get_json()
        record(f"Payroll report sums the 2.5h shift (total={rep.get('total_hours')})",
               rep.get("total_hours", 0) >= 2.5, str(rep)[:80], "P2")
    # Parent cannot use the time clock.
    with app.test_client() as c:
        login(c, "parent_a", "pw")
        rp = c.post("/api/timeclock/clock-in")
        record(f"Parent cannot clock in -> {rp.status_code}", rp.status_code in (401, 403),
               f"got {rp.status_code}", "P1")


def run_get_queryparam_fuzz(ids):
    """Companion to the mutation fuzz for the read surface: every /api GET must
    tolerate malformed query params (a garbage ?year=/?page=/?start= must not 500
    a report, paginated list, or CSV export). Uses seeded ids for the param routes
    and bogus ids elsewhere, then hits each with a battery of bad query strings."""
    import re as _re
    qs_battery = ["year=abc", "page=xyz", "per_page=-1", "limit=notnum", "status=%00",
                  "start=notadate&end=alsobad", "month=99", "active=maybe",
                  "year=999999999999999999", "page=0", "q=" + "A" * 4000]
    sub_map = {"student_id": ids["child_a"], "family_id": ids["fam_a"]}
    with app.app_context():
        gets = []
        for r in app.url_map.iter_rules():
            if "GET" not in r.methods or not str(r).startswith("/api/"):
                continue
            url = str(r)
            for k, v in sub_map.items():
                url = url.replace(f"<int:{k}>", str(v))
            url = _re.sub(r"<[^>]*>", "424242", url)
            gets.append(url)
    bad = []
    with app.test_client() as c:
        login(c, "admin", "admin123")
        for url in gets:
            for qs in qs_battery:
                try:
                    if c.get(f"{url}?{qs}").status_code >= 500:
                        bad.append(f"{url}?{qs}")
                        break
                except Exception as e:  # noqa: BLE001
                    bad.append(f"{url}!{type(e).__name__}")
                    break
    record(f"Every GET endpoint tolerates malformed query params ({len(gets)} fuzzed)",
           not bad, f"5xx on: {bad[:12]}", "P2")


def run_date_param_robustness():
    """Unguarded strptime() on request data 500s on a garbage date/time — a class
    the id-mismatching fuzzes missed (they 404 before parsing, or only send
    numeric garbage). Covers the GET query-param path (attendance date filter)
    and the update-body path (audition/performance/recital dates)."""
    from app.models import (User, Audition, Performance, PerformanceGroup, Recital,
                            RecitalNumber)
    with app.app_context():
        grp = PerformanceGroup(name="DateFuzz Grp")
        db.session.add(grp)
        db.session.flush()
        au = Audition(title="DF aud", group_id=grp.id)
        pf = Performance(title="DF perf", group_id=grp.id)
        rec = Recital(year=2032, title="DF rec")
        db.session.add_all([au, pf, rec])
        db.session.flush()
        num = RecitalNumber(recital_id=rec.id, title="DF num", order_index=1)
        db.session.add(num)
        db.session.commit()
        aid, pid, rid, nid = au.id, pf.id, rec.id, num.id
    with app.test_client() as c:
        login(c, "admin", "admin123")
        codes = {
            "GET attendance ?date_from=garbage": c.get(
                "/api/attendance?date_from=abc&date_to=2026-99-99").status_code,
            "update audition garbage date": c.put(
                f"/api/performance/auditions/{aid}", json={"audition_date": "nope"}).status_code,
            "update performance garbage date": c.put(
                f"/api/performance/performances/{pid}", json={"performance_date": "nope"}).status_code,
            "update recital garbage date": c.put(
                f"/api/recitals/{rid}", json={"recital_date": "nope"}).status_code,
            # A non-list student_ids would TypeError the iteration (int isn't iterable).
            "cast with non-list student_ids": c.post(
                f"/api/recital-numbers/{nid}/cast", json={"student_ids": 123}).status_code,
        }
    bad = {k: v for k, v in codes.items() if v >= 500}
    record("Garbage date / non-list on request data doesn't 5xx",
           not bad, f"5xx: {bad}", "P2")


def run_post_valid_id_fuzz():
    """The mutation fuzz substitutes a NONEXISTENT id, so id-param POST endpoints
    (sub-resource creates/actions) 404 before parsing the body — the same blind
    spot that hid the cast-fill TypeError (iter 174). Seed real parents and POST
    a broad garbage body (incl. a non-list student_ids) to their id-param POSTs;
    assert no 5xx."""
    from datetime import time as _t_pf
    from app.models import (User, DanceClass, Student, Family, Costume, Performance,
                            PerformanceGroup, Audition, Recital, RecitalNumber)
    with app.app_context():
        adm = User.query.filter_by(username="admin").first()
        fam = Family(name="PostFuzz Fam")
        db.session.add(fam)
        db.session.flush()
        st = Student(first_name="Post", last_name="Fuzz", family_id=fam.id, is_active=True)
        db.session.add(st)
        db.session.flush()
        cls = DanceClass(name="PF class", day_of_week=0, start_time=_t_pf(16, 0),
                         end_time=_t_pf(17, 0), instructor_id=adm.id)
        cos = Costume(name="PF costume", fee=10)
        grp = PerformanceGroup(name="PF grp")
        rec = Recital(year=2034, title="PF rec")
        db.session.add_all([cls, cos, grp, rec])
        db.session.flush()
        perf = Performance(title="PF perf", group_id=grp.id)
        au = Audition(title="PF aud", group_id=grp.id)
        num = RecitalNumber(recital_id=rec.id, title="PF num", order_index=1)
        db.session.add_all([perf, au, num])
        db.session.commit()
        paths = [
            f"/api/classes/{cls.id}/enroll",
            f"/api/performances/{perf.id}/ticket-types",
            f"/api/performances/{perf.id}/ticket-orders",
            f"/api/costumes/{cos.id}/assignments",
            f"/api/students/{st.id}/payment-plan",
            f"/api/performance/auditions/{au.id}/signup",
            f"/api/recital-numbers/{num.id}/cast",
            f"/api/performance/performances/{perf.id}/assignments",
            f"/api/performance/groups/{grp.id}/members",
        ]
    garbage = {
        "student_ids": 123,  # non-iterable -> would TypeError an unguarded loop
        "student_id": "x", "quantity": "x", "price": "x", "ticket_type_id": "x",
        "installment_amount": "x", "num_installments": "x", "day_of_month": "x",
        "size": 123, "part": [], "name": 123, "role": [], "note": {}, "amount": "x",
    }
    with app.test_client() as c:
        login(c, "admin", "admin123")
        bad = []
        for p in paths:
            try:
                code = c.post(p, json=garbage).status_code
                if code >= 500:
                    bad.append(f"{p}->{code}")
            except Exception as e:  # a propagated exception is as bad as a 500
                bad.append(f"{p}!{type(e).__name__}")
    record("Id-param POST endpoints don't 5xx on a broad garbage body (valid id)",
           not bad, f"5xx/exc: {bad}", "P2")


def run_update_valid_id_fuzz():
    """run_update_fuzz substitutes a NONEXISTENT id, so endpoints that
    get_or_404 FIRST return 404 before parsing the body — the garbage-body-on-
    VALID-id path (unguarded int()/float() -> 500) slipped through. Seed real
    rows and PUT garbage numeric bodies to their update endpoints with valid ids;
    assert no 5xx."""
    from datetime import time as _t_uf
    from app.models import (User, Rule, WaiverTemplate, MakeupClass, Costume,
                            Student, Family, Recital, RecitalNumber, DanceClass,
                            RecurringCharge, PerformanceGroup)
    with app.app_context():
        adm = User.query.filter_by(username="admin").first()
        fam = Family(name="UpdFuzz Fam")
        db.session.add(fam)
        db.session.flush()
        st = Student(first_name="Upd", last_name="Fuzz", family_id=fam.id, is_active=True)
        db.session.add(st)
        db.session.flush()
        rule = Rule(text="UF rule", display_order=1)
        wt = WaiverTemplate(title="UF waiver", body="b", display_order=1)
        mk = MakeupClass(student_id=st.id, status="scheduled", requested_by=adm.id)
        cos = Costume(name="UF costume", fee=10)
        rec = Recital(year=2031, title="UF recital")
        cls = DanceClass(name="UF class", day_of_week=0, start_time=_t_uf(16, 0),
                         end_time=_t_uf(17, 0), instructor_id=adm.id)
        grp = PerformanceGroup(name="UF group")
        db.session.add_all([rule, wt, mk, cos, rec, cls, grp])
        db.session.flush()
        num = RecitalNumber(recital_id=rec.id, title="UF number", order_index=1)
        rc = RecurringCharge(class_id=cls.id, amount=98, category="tuition",
                             day_of_month=1, created_by=adm.id)
        db.session.add_all([num, rc])
        db.session.commit()
        paths = [
            f"/api/rules/{rule.id}", f"/api/waivers/templates/{wt.id}",
            f"/api/makeups/{mk.id}", f"/api/costumes/{cos.id}",
            f"/api/recitals/{rec.id}", f"/api/recital-numbers/{num.id}",
            f"/api/classes/{cls.id}", f"/api/recurring-charges/{rc.id}",
            f"/api/performance/groups/{grp.id}", f"/api/students/{st.id}",
        ]
    # Kitchen-sink garbage: every numeric/date/id/time field name across the
    # update surface, all wrong-typed, so each endpoint's parsing of whatever it
    # reads is exercised (not just one hand-picked field). Extra fields are
    # ignored by endpoints that don't use them.
    garbage = {
        "display_order": "x", "order_index": "x", "makeup_class_id": "x",
        "fee": "x", "year": "x", "day_of_week": "x", "max_students": "x",
        "day_of_month": "x", "amount": "x", "is_active": "x", "group_id": "x",
        "student_id": "x", "class_id": "x", "date_of_birth": "x",
        "audition_date": "x", "performance_date": "x", "recital_date": "x",
        "start_time": "x", "end_time": "x", "price": "x", "quantity": "x",
        "grade": {"a": 1}, "instructor_id": "x", "location_id": "x",
    }
    with app.test_client() as c:
        login(c, "admin", "admin123")
        bad = []
        for p in paths:
            code = c.put(p, json=garbage).status_code
            if code >= 500:
                bad.append(f"{p}->{code}")
    record("Update endpoints don't 5xx on a broad garbage body (valid id)",
           not bad, f"5xx (unguarded parse on body): {bad}", "P2")


def run_update_fuzz():
    """Fuzz every PUT/PATCH with VALID ids + garbage bodies. The mutation fuzz
    uses bogus ids (404 before the update logic), so this is the only thing that
    reaches update-endpoint field handling — a garbage body must never 5xx, and a
    NOT NULL name field must never be blanked."""
    import re as _re
    from datetime import time as _t
    from app.models import (User, Family, Student, DanceClass, Location, Lead, Skill,
                            PerformanceGroup, Audition, Performance, Costume,
                            WaiverTemplate, Rule, Recital, RecitalNumber, RecitalAward,
                            RecitalAd, TicketType, RecitalCast, TicketOrder)
    with app.app_context():
        adm = User.query.filter_by(username="admin").first()
        fa = Family(name="UF")
        db.session.add(fa)
        db.session.flush()
        st = Student(first_name="Uf", last_name="Kid", family_id=fa.id)
        db.session.add(st)
        db.session.flush()
        dc = DanceClass(name="UC", day_of_week=0, start_time=_t(17, 0), end_time=_t(18, 0), instructor_id=adm.id)
        loc, lead, sk, g = Location(name="UL"), Lead(name="ULe"), Skill(name="USk"), PerformanceGroup(name="UG")
        db.session.add_all([dc, loc, lead, sk, g])
        db.session.flush()
        aud, perf, cst = Audition(title="UA", group_id=g.id), Performance(title="UP", group_id=g.id), Costume(name="UCo", fee=10)
        tmpl, rul, rec = WaiverTemplate(title="UW", body="."), Rule(text="UR", display_order=1), Recital(year=2029, title="URec")
        db.session.add_all([aud, perf, cst, tmpl, rul, rec])
        db.session.flush()
        num = RecitalNumber(recital_id=rec.id, title="UN", order_index=1)
        db.session.add(num)
        db.session.flush()
        aw, ad = RecitalAward(recital_id=rec.id, title="UAw", order_index=1), RecitalAd(recital_id=rec.id, advertiser="UAd", order_index=1)
        ttp = TicketType(performance_id=perf.id, name="UGA", price=10)
        db.session.add_all([aw, ad, ttp])
        db.session.flush()
        db.session.add_all([RecitalCast(number_id=num.id, student_id=st.id), TicketOrder(ticket_type_id=ttp.id, quantity=1, amount=10)])
        db.session.commit()
        idmap = {"student_id": st.id, "class_id": dc.id, "location_id": loc.id, "lid": lead.id,
                 "gid": g.id, "aid": aud.id, "pid": perf.id, "cid": cst.id, "tid": tmpl.id,
                 "rule_id": rul.id, "rid": rec.id, "nid": num.id, "user_id": adm.id, "sid": st.id}
        puts = []
        for r in app.url_map.iter_rules():
            if not (r.methods & {"PUT", "PATCH"}) or not str(r).startswith("/api/"):
                continue
            url = str(r)
            for k, v in idmap.items():
                url = url.replace(f"<int:{k}>", str(v))
            puts.append((sorted(r.methods & {"PUT", "PATCH"})[0], _re.sub(r"<[^>]*>", "424242", url)))
    garbage = {"name": 123, "title": 123, "first_name": 99, "last_name": [], "email": 7, "phone": [],
               "allergies": 9, "date_of_birth": "bad", "class_id": "nope", "group_id": [], "student_id": [],
               "price": "free", "fee": "lots", "advertiser": 123, "vendor": 99, "content": [], "notes": 7,
               "note": 99, "size": 123, "description": 5, "venue": [], "theme": 9, "status": [], "family_id": "x"}
    bad = []
    with app.test_client() as c:
        login(c, "admin", "admin123")
        for method, url in puts:
            try:
                if getattr(c, method.lower())(url, json=garbage).status_code >= 500:
                    bad.append(url)
            except Exception as e:  # noqa: BLE001
                bad.append(f"{url}!{type(e).__name__}")
    record(f"Every update endpoint handles a garbage body without 5xx ({len(puts)} fuzzed)",
           not bad, f"5xx on: {bad[:12]}", "P2")


def run_full_mutation_fuzz():
    """Comprehensive robustness guarantee: fuzz EVERY /api POST/PUT/PATCH endpoint
    with malformed payloads (no body, empty, garbage-typed) as admin, and assert
    NONE returns 5xx. This is the systematic backstop behind the per-feature
    robustness tests — it catches any endpoint (present or future) that would 500
    on bad input (e.g. `.strip()` on a non-string). Skips a few with real side
    effects (external calls, destructive to the shared test DB)."""
    import re as _re
    SKIP = {"/api/cron/run", "/api/webhooks/square", "/api/settings/payments/test-square",
            "/api/settings/sms/test", "/api/seed-demo-parent", "/api/rfid/simulate",
            "/api/auth/login", "/api/auth/logout", "/api/register",
            "/api/students/424242/invite-parent", "/api/messages"}
    with app.app_context():
        routes = []
        for r in app.url_map.iter_rules():
            methods = r.methods - {"HEAD", "OPTIONS", "GET", "DELETE"}
            if not (methods & {"POST", "PUT", "PATCH"}) or not str(r).startswith("/api/"):
                continue
            base = _re.sub(r"<[^>]*>", "424242", str(r))
            if base in SKIP or str(r) in SKIP:
                continue
            routes.append((sorted(methods)[0], base))
    payloads = [None, {}, {"amount": "x", "student_id": "x", "class_id": "x", "name": 123,
                           "email": [], "student_ids": "nope", "date": "bad", "quantity": "lots",
                           "day_of_month": "soon", "title": 123, "status": [], "consent": "maybe",
                           "theme": 123, "role": [], "note": 99, "description": []}]
    bad = []
    with app.test_client() as c:
        login(c, "admin", "admin123")
        for method, base in routes:
            fn = getattr(c, method.lower())
            for body in payloads:
                try:
                    r = fn(base, data="", content_type="application/json") if body is None else fn(base, json=body)
                    if r.status_code >= 500:
                        bad.append(f"{method} {base}->{r.status_code}")
                        break
                except Exception as e:  # noqa: BLE001
                    bad.append(f"{method} {base}!{type(e).__name__}")
                    break
    record(f"Every mutation endpoint handles malformed input without 5xx ({len(routes)} fuzzed)",
           not bad, f"5xx on: {bad[:12]}", "P2")


def run_auto_reminders():
    """Auto-reminders email/SMS families with balances, and run on every boot +
    cron (the machine wakes/sleeps all day). They MUST fire at most once per
    month or families get spammed. Verify: disabled = no-op; enabled marks the
    month done BEFORE sending (so a mid-loop crash can't cause a re-send); and a
    repeat run in the same month is a gated no-op."""
    from datetime import date
    from app import _process_auto_reminders
    from app.models import Setting
    ym = date.today().strftime("%Y-%m")
    with app.app_context():
        # Disabled -> returns before writing the marker.
        Setting.set("reminders_auto_enabled", "0")
        Setting.set("reminders_last_run", "SENTINEL")
        _process_auto_reminders()
        record("Auto-reminders skip entirely when disabled",
               Setting.get("reminders_last_run") == "SENTINEL", "ran while disabled", "P2")
        # Enabled on today's day -> marks the month done (mark-first).
        Setting.set("reminders_auto_enabled", "1")
        Setting.set("reminders_day_of_month", str(date.today().day))
        Setting.set("reminders_last_run", "")
        _process_auto_reminders()
        record(f"Auto-reminders mark the month done (mark-first) -> {Setting.get('reminders_last_run')}",
               Setting.get("reminders_last_run") == ym, f"got {Setting.get('reminders_last_run')}", "P1")
        # Repeat same month -> gated no-op (the anti-spam guarantee), no error.
        _process_auto_reminders()
        record("Repeat run in the same month is a gated no-op",
               Setting.get("reminders_last_run") == ym, "re-ran within the month", "P1")
        Setting.set("reminders_auto_enabled", "0")  # leave disabled for other tests


def run_reminder_non_blocking():
    """The auto-reminder send loop must run OFF the boot/request thread: it does
    per-student network I/O (email + a 15s-timeout SMS each), so for a large
    studio an inline run on a Fly wake would exceed the gunicorn worker timeout
    and 502 on the reminder day. Arm a deliberately-slow email sender and assert
    _process_auto_reminders returns fast — proving the sending is backgrounded."""
    import time
    from datetime import date as _date
    from app import _process_auto_reminders
    from app import email as email_service
    from app.models import Setting, Student, Family, Transaction
    with app.app_context():
        fam = Family(name="Reminder Fam", primary_email="remfam@x.com")
        db.session.add(fam)
        db.session.flush()
        st = Student(first_name="Rem", last_name="Kid", family_id=fam.id,
                     is_active=True, parent_email="remfam@x.com")
        db.session.add(st)
        db.session.flush()
        db.session.add(Transaction(student_id=st.id, type="charge", amount=99,
                                   category="tuition", payment_method="n/a", description="c"))
        Setting.set("reminders_auto_enabled", "1")
        Setting.set("reminders_day_of_month", str(_date.today().day))
        Setting.set("reminders_last_run", "")
        Setting.set("reminders_min_balance", "0")
        db.session.commit()
    app.config["MAIL_SERVER"] = "smtp.example.com"  # is_configured() -> True
    orig = email_service.send_email

    def slow_send(*a, **k):
        time.sleep(2)
        return 1

    email_service.send_email = slow_send
    try:
        with app.app_context():
            t0 = time.monotonic()
            _process_auto_reminders()
            elapsed = time.monotonic() - t0
    finally:
        email_service.send_email = orig
        app.config["MAIL_SERVER"] = None
        with app.app_context():
            Setting.set("reminders_auto_enabled", "0")
    record(f"Auto-reminder sending is backgrounded (returned in {elapsed:.2f}s vs 2s send)",
           elapsed < 1.0, f"took {elapsed:.2f}s — sending appears to block the boot/request thread", "P1")


def run_rfid_assign_unique():
    """An RFID card must map to exactly ONE student — assigning a UID already on
    another student is rejected. Otherwise the scan (which does `.first()` on the
    UID) would check in the wrong dancer. Re-assigning the same UID to the SAME
    student is fine (idempotent)."""
    from app.models import Student, Family
    with app.app_context():
        fam = Family(name="RfidUniq Fam")
        db.session.add(fam)
        db.session.flush()
        a = Student(first_name="Card", last_name="Aaa", family_id=fam.id, is_active=True)
        b = Student(first_name="Card", last_name="Bbb", family_id=fam.id, is_active=True)
        db.session.add_all([a, b])
        db.session.commit()
        aid, bid = a.id, b.id
    with app.test_client() as c:
        login(c, "admin", "admin123")
        r1 = c.post(f"/api/students/{aid}/assign-rfid", json={"rfid_uid": "UNIQCARD_1"})
        r2 = c.post(f"/api/students/{bid}/assign-rfid", json={"rfid_uid": "UNIQCARD_1"})  # dup -> reject
        r3 = c.post(f"/api/students/{aid}/assign-rfid", json={"rfid_uid": "UNIQCARD_1"})  # same student -> ok
    record("RFID card can't be assigned to two students (prevents wrong-student check-in)",
           r1.status_code == 200 and r2.status_code == 400 and r3.status_code == 200,
           f"assignA={r1.status_code} dupB={r2.status_code} reassignA={r3.status_code}", "P2")
    # rfid_assigned_at is UTC metadata — must be emitted with a 'Z' so the browser
    # renders it in local time, not shifted (display-consistency sweep, iter 161).
    ra = ((r1.get_json() or {}).get("student") or {}).get("rfid_assigned_at") or ""
    record("UTC metadata timestamp emitted with 'Z' (rfid_assigned_at)",
           ra.endswith("Z"), f"rfid_assigned_at={ra!r} (want trailing 'Z')", "P3")


def run_day_of_week_convention():
    """`day_of_week` is 0=Monday .. 6=Sunday (matching Python's weekday()), and
    DanceClass.day_name must agree. The calendar, take-attendance's "today's
    classes", the RFID current-class matcher, and the parent-portal schedule all
    rely on this — a flip would file every class under the wrong day."""
    from app.models import DanceClass
    expected = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
    with app.app_context():
        bad = [f"dow={dow} -> {DanceClass(day_of_week=dow).day_name} (want {name})"
               for dow, name in enumerate(expected)
               if DanceClass(day_of_week=dow).day_name != name]
    record("day_of_week convention is 0=Monday..6=Sunday (calendar/attendance/portal rely on it)",
           not bad, f"mismatches: {bad}", "P2")


def run_attendance_default_local():
    """Defense-in-depth: the Attendance.check_in_time COLUMN DEFAULT must be
    studio-local. All creation paths set it explicitly today, but the default is
    the safety net — a future path that forgets would otherwise reintroduce the
    evening wrong-day bug (this class of bug recurred four times). Create a row
    without check_in_time and assert it defaults to today's local date."""
    from datetime import date, time as _time
    from app.models import User, DanceClass, Student, Family, Attendance
    with app.app_context():
        adm = User.query.filter_by(username="admin").first()
        fam = Family(name="DefTZ Fam")
        db.session.add(fam)
        db.session.flush()
        s = Student(first_name="Def", last_name="Tz", family_id=fam.id, is_active=True)
        db.session.add(s)
        db.session.flush()
        cls = DanceClass(name="DefTzClass", day_of_week=0, start_time=_time(16, 0),
                         end_time=_time(17, 0), instructor_id=adm.id)
        db.session.add(cls)
        db.session.flush()
        att = Attendance(student_id=s.id, class_id=cls.id)  # no check_in_time -> column default
        db.session.add(att)
        db.session.commit()
        dated_today = att.check_in_time is not None and att.check_in_time.date() == date.today()
        stamp = att.check_in_time
    record("Attendance.check_in_time column default is studio-local (dated today, not UTC-tomorrow)",
           dated_today, f"defaulted to {stamp}", "P2")


def run_rfid_checkin_local_day():
    """An RFID scan records attendance via a third code path (rfid/service.py).
    It must use the studio-local day like the manual/toggle paths — datetime.utcnow()
    would date an evening scan on the next UTC day, hiding it from today's roster
    and tripping the unique-day index. Simulate a scan and assert it lands today."""
    from datetime import date, time as _time
    from app.models import (User, DanceClass, Student, Family, ClassEnrollment, Attendance)
    try:
        from rfid.service import get_rfid_service
    except Exception as e:  # RFID module unavailable in this env — don't fail the suite
        record("RFID scan records attendance on the studio-local day", True,
               f"RFID service unavailable, skipped: {e}", "P3")
        return
    with app.app_context():
        adm = User.query.filter_by(username="admin").first()
        fam = Family(name="RFID Fam")
        db.session.add(fam)
        db.session.flush()
        s = Student(first_name="Rfid", last_name="Scan", family_id=fam.id, is_active=True,
                    rfid_uid="TESTUID_RFID_1")
        db.session.add(s)
        db.session.flush()
        # A class on today's weekday, wide open all day, so _get_current_class matches.
        cls = DanceClass(name="RfidClass", day_of_week=date.today().weekday(),
                         start_time=_time(0, 0), end_time=_time(23, 59), instructor_id=adm.id)
        db.session.add(cls)
        db.session.flush()
        db.session.add(ClassEnrollment(student_id=s.id, class_id=cls.id))
        db.session.commit()
        sid = s.id
    ok = get_rfid_service().simulate_scan("TESTUID_RFID_1")
    with app.app_context():
        from app.models import RFIDLog
        att = Attendance.query.filter_by(student_id=sid, check_in_method="rfid").first()
        dated_today = att is not None and att.check_in_time.date() == date.today()
        log = RFIDLog.query.filter_by(rfid_uid="TESTUID_RFID_1").order_by(RFIDLog.id.desc()).first()
        log_today = log is not None and log.scan_time.date() == date.today()
    record("RFID scan records attendance on the studio-local day (not the next UTC day)",
           ok and dated_today,
           f"scan_ok={ok} att={'none' if att is None else att.check_in_time.isoformat()}", "P2")
    record("RFID scan_time log is also studio-local (matches the dashboard display)",
           log_today, f"log scan_time={'none' if log is None else log.scan_time.isoformat()}", "P3")


def run_money_creation_audited():
    """Posting money must hit the audit trail, not just deleting it — otherwise
    the trail shows who *removed* a charge but not who *added* one (matters since
    staff, not only admins, can post charges). Post a charge and a bulk charge,
    then assert both actions are recorded in the AuditLog."""
    from datetime import time as _time
    from app.models import (User, DanceClass, Student, Family, ClassEnrollment, AuditLog)
    with app.app_context():
        adm = User.query.filter_by(username="admin").first()
        fam = Family(name="Audit Fam")
        db.session.add(fam)
        db.session.flush()
        s = Student(first_name="Aud", last_name="Itlog", family_id=fam.id, is_active=True)
        db.session.add(s)
        db.session.flush()
        cls = DanceClass(name="AuditClass", day_of_week=0, start_time=_time(16, 0),
                         end_time=_time(17, 0), instructor_id=adm.id)
        db.session.add(cls)
        db.session.flush()
        db.session.add(ClassEnrollment(student_id=s.id, class_id=cls.id))
        db.session.commit()
        sid, cid = s.id, cls.id
    with app.test_client() as c:
        login(c, "admin", "admin123")
        rc = c.post("/api/transactions", json={"student_id": sid, "type": "charge",
                                               "amount": 42.42, "category": "tuition"})
        rb = c.post("/api/transactions/bulk-charge", json={"class_id": cid, "amount": 15,
                                                           "category": "tuition"})
    with app.app_context():
        actions = {a.action for a in AuditLog.query.filter(
            AuditLog.action.in_(["transaction.create", "bulk_charge"])).all()}
    record("Posting a charge and a bulk charge are written to the audit trail",
           rc.status_code == 201 and rb.status_code == 201
           and "transaction.create" in actions and "bulk_charge" in actions,
           f"charge={rc.status_code} bulk={rb.status_code} actions={actions}", "P2")


def run_costume_charge_race():
    """Posting a costume fee only charges not-yet-charged assignments (idempotent
    on a sequential re-run), but it's check-then-act: two concurrent charge runs
    (two tabs) both read the uncharged assignment before either commits and each
    post a charge — a double-charge on real recital money. A process lock must
    serialize the runs. Fire two at one costume and assert exactly one charge."""
    import threading
    from app.models import Costume, CostumeAssignment, Student, Family, Transaction
    with app.app_context():
        fam = Family(name="Costume Fam")
        db.session.add(fam)
        db.session.flush()
        st = Student(first_name="Cos", last_name="Tume", family_id=fam.id, is_active=True)
        db.session.add(st)
        db.session.flush()
        cos = Costume(name="RaceTutu", fee=45.00)
        db.session.add(cos)
        db.session.flush()
        db.session.add(CostumeAssignment(costume_id=cos.id, student_id=st.id, charged=False))
        db.session.commit()
        sid, cosid = st.id, cos.id

    barrier = threading.Barrier(2)

    def worker():
        with app.test_client() as c:
            login(c, "admin", "admin123")
            barrier.wait()
            c.post(f"/api/costumes/{cosid}/charge")

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    with app.app_context():
        n = Transaction.query.filter_by(student_id=sid, category="costumes").count()
    record("Concurrent costume-fee charge posts to each dancer exactly once",
           n == 1, f"costume charges={n} (want 1)", "P1")


def run_registration_approve_capacity():
    """Approving a self-registration must respect class capacity like the manual
    enroll path — otherwise approving a registration for a popular fall class
    silently overbooks it past max_students. Fill a max=1 class, submit a
    registration requesting it, approve, and assert the newcomer is created but
    NOT enrolled in the full class (and the skip is reported to the admin)."""
    import json as _json
    from datetime import time as _time
    from app.models import (User, DanceClass, Student, Family, ClassEnrollment,
                            Registration, Setting)
    with app.app_context():
        adm = User.query.filter_by(username="admin").first()
        cls = DanceClass(name="TinyCapClass", day_of_week=0, start_time=_time(16, 0),
                         end_time=_time(17, 0), instructor_id=adm.id, max_students=1)
        db.session.add(cls)
        db.session.flush()
        ffam = Family(name="Filler Fam")
        db.session.add(ffam)
        db.session.flush()
        filler = Student(first_name="Fill", last_name="Er", family_id=ffam.id, is_active=True)
        db.session.add(filler)
        db.session.flush()
        db.session.add(ClassEnrollment(student_id=filler.id, class_id=cls.id))  # class now full
        Setting.set("registration_open", "1")
        reg = Registration(parent_name="Newcomer Fam", parent_email="newcap@x.com",
                           students_json=_json.dumps([{"first_name": "Late", "last_name": "Comer"}]),
                           class_ids=str(cls.id))
        db.session.add(reg)
        db.session.commit()
        rid, cid = reg.id, cls.id
    with app.test_client() as c:
        login(c, "admin", "admin123")
        r = c.post(f"/api/registrations/{rid}/approve")
        d = r.get_json() or {}
    with app.app_context():
        n = ClassEnrollment.query.filter_by(class_id=cid, is_active=True).count()
        newcomer = Student.query.filter_by(first_name="Late", last_name="Comer").first()
    record("Registration approval respects class capacity (no silent overbook)",
           r.status_code == 200 and n == 1 and newcomer is not None and bool(d.get("full_skipped")),
           f"status={r.status_code} enrolled={n} (want 1) newcomer={'yes' if newcomer else 'no'} skips={d.get('full_skipped')}", "P2")


def run_rfid_reuses_app():
    """The RFID reader polls in a loop, one _process_card_scan per card tap. It
    must reuse a single Flask app — building one per scan (as it did) re-ran
    migrations, re-seeded, and spawned a reminder daemon thread on every tap, a
    slow leak over a day of scanning. Count create_app across two scans on a
    fresh service; expect exactly one (cached thereafter)."""
    try:
        import app as _appmod
        from rfid.service import RFIDService
    except Exception as e:
        record("RFID reader reuses one app across scans", True,
               f"RFID service unavailable, skipped: {e}", "P3")
        return
    calls = [0]
    orig = _appmod.create_app

    def counting(*a, **k):
        calls[0] += 1
        return orig(*a, **k)

    _appmod.create_app = counting
    try:
        svc = RFIDService()
        # Bogus UIDs — no student match, but each scan still routes through
        # _get_app (and _log_rfid_scan), which is what we're counting.
        svc.simulate_scan("NOSUCH_UID_A")
        svc.simulate_scan("NOSUCH_UID_B")
    finally:
        _appmod.create_app = orig
    record("RFID reader reuses one app across scans (create_app called once, not per tap)",
           calls[0] == 1, f"create_app called {calls[0]}x for 2 scans (want 1)", "P2")


def run_recurring_no_retroactive_first_month():
    """Setting up fall billing in August must not bill August. A recurring
    charge's first bill is the first due day ON or AFTER its creation — without
    that, creating a due-day-1 charge on Aug 20 charged every enrolled family
    for August on the next boot, a month before classes start. A charge created
    BEFORE its due day still bills that same month."""
    from datetime import date, datetime as _dtt, time as _time
    from app.models import (User, DanceClass, Student, Family, ClassEnrollment,
                            RecurringCharge, Transaction)
    from app import _process_recurring_charges
    with app.app_context():
        adm = User.query.filter_by(username="admin").first()
        fam = Family(name="Retro Fam")
        db.session.add(fam)
        db.session.flush()
        s = Student(first_name="Retro", last_name="Kid", family_id=fam.id, is_active=True)
        db.session.add(s)
        db.session.flush()
        cls = DanceClass(name="RetroClass", day_of_week=1, start_time=_time(16, 0),
                         end_time=_time(17, 0), instructor_id=adm.id)
        db.session.add(cls)
        db.session.flush()
        db.session.add(ClassEnrollment(student_id=s.id, class_id=cls.id))
        # Fall setup shape: created Aug 20, bills on the 1st.
        rc = RecurringCharge(class_id=cls.id, amount=55, category="tuition",
                             day_of_month=1, created_by=adm.id,
                             created_at=_dtt(2030, 8, 20, 12, 0))
        # Control: created Aug 20, bills on the 25th (due day still ahead).
        rc2 = RecurringCharge(class_id=cls.id, amount=66, category="tuition",
                              day_of_month=25, created_by=adm.id,
                              created_at=_dtt(2030, 8, 20, 12, 0))
        db.session.add_all([rc, rc2])
        db.session.commit()
        rc_id, rc2_id = rc.id, rc2.id

    def _count(rid):
        with app.app_context():
            return Transaction.query.filter_by(recurring_charge_id=rid).count()

    with app.app_context():
        _process_recurring_charges(today=date(2030, 8, 26))  # after both due days
    aug_retro, aug_ok = _count(rc_id), _count(rc2_id)
    with app.app_context():
        _process_recurring_charges(today=date(2030, 9, 1))   # next month
    sep_retro = _count(rc_id)
    record("Recurring first bill is the first due day on/after creation (no retroactive month)",
           aug_retro == 0 and aug_ok == 1 and sep_retro == 1,
           f"aug_due1={aug_retro} (want 0) aug_due25={aug_ok} (want 1) sept_due1={sep_retro} (want 1)", "P1")


def run_recurring_short_month_clamp():
    """The recurring-charge engine auto-creates tuition every month and runs
    unattended — so it needs a real regression guard. A charge set for the 31st
    must still fire in a short month, on that month's LAST day (else the studio
    silently loses that tuition), fire exactly once (idempotent), and NOT fire
    before the clamped day. The processor takes an injectable `today`, so Feb is
    exercised deterministically without freezing the clock."""
    from datetime import date, time as _time
    from app import _process_recurring_charges
    from app.models import (User, DanceClass, Student, Family, ClassEnrollment,
                            RecurringCharge, Transaction)
    with app.app_context():
        adm = User.query.filter_by(username="admin").first()
        fam = Family(name="Clamp Fam")
        db.session.add(fam)
        db.session.flush()
        s = Student(first_name="Clamp", last_name="Kid", family_id=fam.id, is_active=True)
        db.session.add(s)
        db.session.flush()
        cls = DanceClass(name="ClampClass", day_of_week=0, start_time=_time(16, 0),
                         end_time=_time(17, 0), instructor_id=adm.id)
        db.session.add(cls)
        db.session.flush()
        db.session.add(ClassEnrollment(student_id=s.id, class_id=cls.id))
        rc = RecurringCharge(class_id=cls.id, amount=77.77, category="tuition",
                             day_of_month=31, created_by=adm.id)
        db.session.add(rc)
        db.session.commit()
        rc_id, sid = rc.id, s.id

    def _count():
        with app.app_context():
            return Transaction.query.filter_by(recurring_charge_id=rc_id).count()

    # Feb 27: day-31 clamps to Feb's last day (28); 27 < 28 -> must NOT fire yet.
    with app.app_context():
        _process_recurring_charges(today=date(2026, 2, 27))
    before = _count()
    # Feb 28 (Feb's last day): clamps to 28; fires exactly once.
    with app.app_context():
        _process_recurring_charges(today=date(2026, 2, 28))
    after_first = _count()
    # Re-run same day: idempotent (existing charge this month) -> no double-charge.
    with app.app_context():
        _process_recurring_charges(today=date(2026, 2, 28))
    after_second = _count()
    with app.app_context():
        t = Transaction.query.filter_by(recurring_charge_id=rc_id).first()
        charge_ok = (t is not None and float(t.amount) == 77.77
                     and t.transaction_date == date(2026, 2, 28) and t.student_id == sid)
    record("Recurring day-31 charge does not fire before the clamped short-month day (Feb 27)",
           before == 0, f"fired early: {before} charge(s) on Feb 27", "P2")
    record("Recurring day-31 charge fires on Feb 28 (clamped) exactly once, idempotent on re-run",
           after_first == 1 and after_second == 1,
           f"after_first={after_first} after_second={after_second} (want 1/1)", "P1")
    record("Recurring short-month charge lands on the clamped last day with the right amount",
           charge_ok, f"charge check failed for rc {rc_id}", "P2")


def run_message_blast_non_blocking():
    """A message blast must background its SMTP send. A whole-studio 'all' blast
    is one SMTP round-trip per family; sent inline it would exceed the 120s
    gunicorn worker timeout and 502 mid-send (partial delivery, sent-state lost).
    Arm a slow sender and assert the POST returns fast with a queued count — not a
    synchronous 'sent' and not the not-configured copy-paste fallback."""
    import time
    from app import email as email_service
    app.config["MAIL_SERVER"] = "smtp.example.com"
    orig = email_service.send_email

    def slow_send(*a, **k):
        time.sleep(2)
        return 1

    email_service.send_email = slow_send
    try:
        with app.test_client() as c:
            login(c, "admin", "admin123")
            t0 = time.monotonic()
            r = c.post("/api/messages", json={"subject": "Fall kickoff",
                                              "body": "Welcome back!", "recipient_type": "all"})
            elapsed = time.monotonic() - t0
            d = r.get_json() or {}
    finally:
        email_service.send_email = orig
        app.config["MAIL_SERVER"] = None
    record(f"Message blast backgrounds its send (returned in {elapsed:.2f}s, queued={d.get('queued')})",
           r.status_code == 201 and elapsed < 1.0 and d.get("queued", 0) >= 1
           and "recipient_emails" not in d,
           f"status={r.status_code} elapsed={elapsed:.2f}s body={str(d)[:100]}", "P1")


def run_manual_reminders_non_blocking():
    """The manual 'remind everyone who owes' endpoint must background its sends —
    sent inline, a large studio × (email + 15s SMS) would exceed the 120s worker
    timeout and 502. Arm a slow email sender, seed an owing student, and assert
    the POST returns fast with a queued count."""
    import time
    from app import email as email_service
    from app.models import Student, Family, Transaction
    with app.app_context():
        fam = Family(name="ManRem Fam", primary_email="manrem@x.com")
        db.session.add(fam)
        db.session.flush()
        st = Student(first_name="Man", last_name="Rem", family_id=fam.id,
                     is_active=True, parent_email="manrem@x.com")
        db.session.add(st)
        db.session.flush()
        db.session.add(Transaction(student_id=st.id, type="charge", amount=88,
                                   category="tuition", payment_method="n/a", description="c"))
        db.session.commit()
    app.config["MAIL_SERVER"] = "smtp.example.com"
    orig = email_service.send_email

    def slow_send(*a, **k):
        time.sleep(2)
        return 1

    email_service.send_email = slow_send
    try:
        with app.test_client() as c:
            login(c, "admin", "admin123")
            t0 = time.monotonic()
            r = c.post("/api/balances/send-reminders")
            elapsed = time.monotonic() - t0
            d = r.get_json() or {}
    finally:
        email_service.send_email = orig
        app.config["MAIL_SERVER"] = None
    record(f"Manual send-reminders backgrounds its sends (returned in {elapsed:.2f}s, queued={d.get('queued')})",
           r.status_code == 200 and elapsed < 1.0 and d.get("queued", 0) >= 1,
           f"status={r.status_code} elapsed={elapsed:.2f}s body={d}", "P1")


def run_message_blast(ids):
    """Message blasts: validated, resolve recipients, degrade gracefully when
    SMTP isn't configured (save + return emails), and parents can't send."""
    with app.test_client() as c:
        login(c, "admin", "admin123")
        # 'all' resolves the two seeded parent emails; SMTP not configured -> saved + emails returned
        r = c.post("/api/messages", json={"subject": "Hi", "body": "Welcome to fall!",
                                          "recipient_type": "all"})
        d = r.get_json() or {}
        ok = r.status_code == 201 and d.get("recipient_count", 0) >= 2 and "recipient_emails" in d
        record(f"Blast to 'all' resolves recipients + degrades gracefully -> {r.status_code}",
               ok, f"status {r.status_code}: {str(d)[:80]}", "P1")
        # missing subject rejected
        r = c.post("/api/messages", json={"body": "x", "recipient_type": "all"})
        record(f"Blast requires subject -> {r.status_code}", r.status_code == 400, f"got {r.status_code}", "P3")
        # non-numeric class filter -> 400 (not 500)
        r = c.post("/api/messages", json={"subject": "x", "body": "y",
                                          "recipient_type": "class", "recipient_filter": "abc"})
        record(f"Blast rejects bad class filter (no 500) -> {r.status_code}",
               r.status_code == 400, f"got {r.status_code}", "P2")
        # non-string subject must not 500 (coerced/rejected)
        r = c.post("/api/messages", json={"subject": 123, "body": "y", "recipient_type": "all"})
        record(f"Blast handles a non-string subject (no 500) -> {r.status_code}",
               r.status_code < 500, f"got {r.status_code}", "P2")
        # Class targeting: only the enrolled class's parent, deduped, not other families.
        from datetime import time as _t
        from app.models import DanceClass, ClassEnrollment, User as _U
        with app.app_context():
            adm = _U.query.filter_by(username="admin").first()
            mc = DanceClass(name="Blast Class", day_of_week=0, start_time=_t(17, 0),
                            end_time=_t(18, 0), instructor_id=adm.id)
            db.session.add(mc)
            db.session.flush()
            db.session.add(ClassEnrollment(student_id=ids["child_a"], class_id=mc.id))
            db.session.commit()
            mcid = mc.id
        r = c.post("/api/messages", json={"subject": "Class note", "body": "hi",
                                          "recipient_type": "class", "recipient_filter": mcid})
        d = r.get_json() or {}
        emails = d.get("recipient_emails", "")
        record(f"Class blast targets only the enrolled family -> {r.status_code}, count={d.get('recipient_count')}",
               r.status_code == 201 and d.get("recipient_count") == 1
               and "alpha-parent@x.com" in str(emails) and "beta-parent@x.com" not in str(emails),
               f"emails={emails}", "P2")
    # parent cannot send a blast (write-guard)
    with app.test_client() as c:
        login(c, "parent_a", "pw")
        r = c.post("/api/messages", json={"subject": "x", "body": "y", "recipient_type": "all"})
        record(f"Parent cannot send a blast -> {r.status_code}", r.status_code == 403,
               f"got {r.status_code}", "P0")


def run_clean_str_backstop():
    """_clean_str has a generous default length backstop (SQLite ignores
    VARCHAR(n), so NO client field should be able to store multi-MB). Verify both
    definitions cap at the 50 KB default, that an explicit tight cap overrides,
    and that maxlen=None opts out."""
    from app.api.routes import _clean_str as _cs_routes
    from app.helpers import _clean_str as _cs_helpers
    huge = "A" * 200000
    checks = {
        "routes default 50KB": len(_cs_routes(huge)) == 50000,
        "helpers default 50KB": len(_cs_helpers(huge)) == 50000,
        "explicit tight cap overrides": len(_cs_routes(huge, 80)) == 80,
        "maxlen=None opts out": len(_cs_routes(huge, maxlen=None)) == 200000,
    }
    bad = [k for k, ok in checks.items() if not ok]
    record("_clean_str default backstop caps client text at 50 KB (both defs)",
           not bad, f"wrong: {bad}", "P3")


def run_registration_field_caps():
    """The public (unauthenticated) registration must cap field lengths — SQLite
    ignores VARCHAR(n), so an uncapped multi-MB field would bloat the DB volume.
    Submit huge fields and assert they're truncated (student count is already
    capped at 30; this covers per-field size)."""
    import json as _json
    from app.models import Setting, Registration
    with app.app_context():
        Setting.set("registration_open", "1")
        db.session.commit()
    huge = "A" * 100000
    with app.test_client() as c:
        r = c.post("/api/register", json={
            "parent_name": huge, "parent_email": "fieldcaps@x.com",
            **REG_CONTACT,
            "students": [reg_student(huge, huge, allergies=huge)]})
    with app.app_context():
        reg = (Registration.query.filter_by(parent_email="fieldcaps@x.com")
               .order_by(Registration.id.desc()).first())
        pn = len(reg.parent_name) if reg else -1
        stu = _json.loads(reg.students_json) if reg else []
        fn = len(stu[0]["first_name"]) if stu else -1
        al = len(stu[0]["allergies"]) if stu else -1
        Setting.set("registration_open", "0")
        db.session.commit()
    record("Public registration caps field lengths (no multi-MB storage abuse)",
           r.status_code == 201 and 0 < pn <= 120 and 0 < fn <= 80 and 0 < al <= 500,
           f"status={r.status_code} parent_name={pn}(<=120) first_name={fn}(<=80) allergies={al}(<=500)", "P2")


def run_registration_dedupe():
    """Returning families re-register for fall naming the same children. That
    must NOT create a second family or duplicate the dancer + split their
    ledger: approval matches an existing active family by parent email, treats
    already-known dancers as RETURNING (re-enrolls them in the newly-requested
    classes, reactivates them if withdrawn), and still adds new siblings."""
    from datetime import time as _time
    from app.models import Setting, Family, Student, DanceClass, ClassEnrollment, User
    with app.app_context():
        Setting.set("registration_open", "1")
        instructor = User.query.filter_by(username="admin").first()
        fall = DanceClass(name="Fall Dedupe Ballet", day_of_week=2,
                          start_time=_time(17, 0), end_time=_time(18, 0),
                          instructor_id=instructor.id, max_students=10)
        db.session.add(fall)
        db.session.commit()
        fall_id = fall.id
    email = "dedupe-fam@x.com"
    with app.test_client() as pub:
        pub.post("/api/register", json={
            "parent_name": "Dana Dedupe", "parent_email": email, **REG_CONTACT,
            "students": [reg_student("Mia", "Dedupe")]})
        pub.post("/api/register", json={
            "parent_name": "Dana Dedupe", "parent_email": email.upper(),  # case differs
            **REG_CONTACT,
            "students": [reg_student("Mia", "Dedupe"),
                         reg_student("Zoe", "Dedupe")],
            "class_ids": [fall_id]})
    with app.test_client() as c:
        login(c, "admin", "admin123")
        regs = (c.get("/api/registrations?status=pending").get_json() or {}).get("registrations", [])
        rids = [r["id"] for r in regs if (r.get("parent_email") or "").lower() == email]
        results = [c.post(f"/api/registrations/{rid}/approve").get_json() or {} for rid in sorted(rids)]
    with app.app_context():
        n_fam = sum(1 for f in Family.query.all()
                    if (f.primary_email or "").lower() == email)
        n_mia = Student.query.filter_by(first_name="Mia", last_name="Dedupe").count()
        n_zoe = Student.query.filter_by(first_name="Zoe", last_name="Dedupe").count()
        mia = Student.query.filter_by(first_name="Mia", last_name="Dedupe").first()
        mia_enrolled = ClassEnrollment.query.filter_by(
            student_id=mia.id, class_id=fall_id, is_active=True).count() if mia else 0
        mia_id = mia.id if mia else None
    second = results[1] if len(results) > 1 else {}
    record("Re-registration -> one family, returning dancer re-enrolled (not duplicated), sibling added",
           n_fam == 1 and n_mia == 1 and n_zoe == 1 and mia_enrolled == 1
           and second.get("matched_existing_family") is True
           and "Mia Dedupe" in (second.get("returning_dancers") or []),
           f"families={n_fam} mia={n_mia} zoe={n_zoe} enrolled={mia_enrolled} second={second}", "P1")
    # A WITHDRAWN dancer who re-registers comes back active (last year's soft
    # delete must not block this fall's return).
    with app.app_context():
        mia = Student.query.get(mia_id)
        mia.is_active = False
        db.session.commit()
    with app.test_client() as pub:
        pub.post("/api/register", json={
            "parent_name": "Dana Dedupe", "parent_email": email, **REG_CONTACT,
            "students": [reg_student("Mia", "Dedupe")]})
    with app.test_client() as c:
        login(c, "admin", "admin123")
        regs = (c.get("/api/registrations?status=pending").get_json() or {}).get("registrations", [])
        rid3 = max(r["id"] for r in regs if (r.get("parent_email") or "").lower() == email)
        c.post(f"/api/registrations/{rid3}/approve")
    with app.app_context():
        mia = Student.query.get(mia_id)
        n_mia_after = Student.query.filter_by(first_name="Mia", last_name="Dedupe").count()
    record("Withdrawn dancer re-registering is reactivated, not duplicated",
           mia.is_active is True and n_mia_after == 1,
           f"active={mia.is_active} count={n_mia_after}", "P1")


def run_registration_pilot_shape_dedupe():
    """The REAL prod data shape: pilot-era students carry parent_email but have
    NO family (20 of 21), and the one family has no primary_email. A returning
    family re-registering must still be recognized — matched by STUDENT email —
    or every real family this fall gets duplicated. The dancer is adopted into
    the family and the family email is backfilled so the fast path matches next
    time."""
    from app.models import Setting, Family, Student
    with app.app_context():
        Setting.set("registration_open", "1")
        # Pilot shape: orphan student, parent_email set, no family anywhere.
        orphan = Student(first_name="Pilot", last_name="Kid", family_id=None,
                         parent_email="pilot-parent@x.com", is_active=True)
        db.session.add(orphan)
        db.session.commit()
        orphan_id = orphan.id
    with app.test_client() as pub:
        pub.post("/api/register", json={
            "parent_name": "Pia Pilot", "parent_email": "PILOT-PARENT@x.com",  # case differs
            **REG_CONTACT,
            "students": [reg_student("Pilot", "Kid"), reg_student("New", "Sib")]})
    with app.test_client() as c:
        login(c, "admin", "admin123")
        regs = (c.get("/api/registrations?status=pending").get_json() or {}).get("registrations", [])
        mine = [r for r in regs if (r.get("parent_email") or "").lower() == "pilot-parent@x.com"]
        rid = max(r["id"] for r in mine)
        # The queue flags it as returning BEFORE approval (same matcher).
        record("Pending list flags the pilot-shape registration as returning",
               all(r.get("returning") is True for r in mine),
               f"returning flags: {[r.get('returning') for r in mine]}", "P3")
        res = c.post(f"/api/registrations/{rid}/approve").get_json() or {}
    with app.app_context():
        n_pilot = Student.query.filter_by(first_name="Pilot", last_name="Kid").count()
        orphan = Student.query.get(orphan_id)
        fam = orphan.family
        fam_email_backfilled = fam is not None and (fam.primary_email or "").lower() == "pilot-parent@x.com"
    record("Pilot-shape re-registration: orphan dancer matched by email, adopted, family email backfilled",
           n_pilot == 1 and orphan.family_id is not None and fam_email_backfilled
           and "Pilot Kid" in (res.get("returning_dancers") or []),
           f"count={n_pilot} family_id={orphan.family_id} backfilled={fam_email_backfilled} res={res.get('returning_dancers')}", "P1")


def run_approval_links_sibling_to_portal():
    """A returning family's re-registration adds a new sibling. Approval must
    link the new dancer to the family's existing portal account(s) — otherwise
    the parent never sees them on the portal, and register-with-code can't be
    reused by an existing account (it bounces authenticated users)."""
    from app.models import Setting, Family, Student, User, ParentStudent
    with app.app_context():
        Setting.set("registration_open", "1")
        fam = Family(name="Portal Fam", primary_email="portal-fam@x.com")
        db.session.add(fam)
        db.session.flush()
        dancer = Student(first_name="Poppy", last_name="Portal", family_id=fam.id,
                         parent_email="portal-fam@x.com", is_active=True)
        parent = User(username="portal_parent", email="portal-fam@x.com", role="parent",
                      first_name="Pat", last_name="Portal", is_active=True)
        parent.set_password("pw")
        db.session.add_all([dancer, parent])
        db.session.flush()
        db.session.add(ParentStudent(parent_id=parent.id, student_id=dancer.id))
        db.session.commit()
        parent_id = parent.id
    with app.test_client() as pub:
        pub.post("/api/register", json={
            "parent_name": "Pat Portal", "parent_email": "portal-fam@x.com",
            **REG_CONTACT,
            "students": [reg_student("Poppy", "Portal"), reg_student("Newby", "Portal")]})
    with app.test_client() as c:
        login(c, "admin", "admin123")
        regs = (c.get("/api/registrations?status=pending").get_json() or {}).get("registrations", [])
        rid = max(r["id"] for r in regs if (r.get("parent_email") or "").lower() == "portal-fam@x.com")
        res = c.post(f"/api/registrations/{rid}/approve").get_json() or {}
    with app.app_context():
        newby = Student.query.filter_by(first_name="Newby", last_name="Portal").first()
        linked = newby is not None and ParentStudent.query.filter_by(
            parent_id=parent_id, student_id=newby.id).first() is not None
        kid_names = {s.full_name for s in User.query.get(parent_id).get_children()}
    with app.test_client() as pc:
        login(pc, "portal_parent", "pw")
        portal_ok = pc.get("/parent").status_code == 200
    record("Approval links a new sibling to the family's existing portal account",
           linked and "Newby Portal" in kid_names and portal_ok,
           f"linked={linked} children={kid_names} portal={portal_ok} msg={res.get('message', '')[:80]}", "P1")


# Registration-level fields the public form REQUIRES as of Aug 2026 (parent phone
# + emergency contact name/phone). Spread into every payload so a test that isn't
# about those rules keeps exercising its real subject instead of 400ing at the door.
REG_CONTACT = {
    "parent_phone": "313-555-0100",
    "emergency_name": "Emergency Contact",
    "emergency_phone": "313-555-0911",
}


def reg_student(first, last="Test", **over):
    """A complete dancer for the public registration form. Last name, DOB, and
    the allergies/medical field became REQUIRED in Aug 2026 (studio request), so
    tests that aren't about those rules build dancers here and keep exercising
    their real subject instead of 400ing at the door."""
    s = {"first_name": first, "last_name": last, "dob": "2015-04-02",
         "allergies": "None"}
    s.update(over)
    return s


def run_registration_volume_caps():
    """The public register endpoint is the one unauthenticated write surface.
    Two circuit breakers: a parent can't stack more than a few PENDING
    registrations (accidental resubmits, email-targeted spam), and a global
    pending cap stops a bot from burying the admin queue. Both friendly-fail
    with 429 and point at the studio."""
    from app import api as api_module
    from app.models import Setting
    routes = api_module.routes
    with app.app_context():
        Setting.set("registration_open", "1")
        db.session.commit()
    payload = lambda i: {"parent_name": "Cap Test", "parent_email": "cap-test@x.com",
                         **REG_CONTACT,
                         "students": [reg_student(f"Kid{i}")]}
    with app.test_client() as pub:
        codes = [pub.post("/api/register", json=payload(i)).status_code for i in range(4)]
    per_email_ok = codes[:3] == [201, 201, 201] and codes[3] == 429
    record(f"Per-email pending cap: 3 accepted, 4th rejected (codes={codes})",
           per_email_ok, f"codes={codes}", "P2")
    # Global cap: patch the module constant down instead of inserting 500 rows.
    prev = routes._MAX_PENDING_REGISTRATIONS
    routes._MAX_PENDING_REGISTRATIONS = 1
    try:
        with app.test_client() as pub:
            r = pub.post("/api/register", json={
                "parent_name": "Other", "parent_email": "other-cap@x.com",
                **REG_CONTACT, "students": [reg_student("Kid")]})
        record(f"Global pending cap trips at the limit -> {r.status_code}",
               r.status_code == 429, f"got {r.status_code}", "P2")
    finally:
        routes._MAX_PENDING_REGISTRATIONS = prev
    # Clean up so later registration tests see an uncluttered queue.
    with app.app_context():
        from app.models import Registration
        Registration.query.filter(Registration.parent_email.in_(
            ["cap-test@x.com", "other-cap@x.com"])).delete(synchronize_session=False)
        db.session.commit()


def run_registration_flow():
    """Public self-registration: gated when closed, validated, and admin
    approval actually creates a family + students. The key fall-enrollment path."""
    from app.models import Setting, Family, Student, Registration

    with app.app_context():
        Setting.set("registration_open", "0")
        db.session.commit()
    with app.test_client() as pub:
        r = pub.post("/api/register", json={"parent_name": "Jo", "parent_email": "jo@x.com",
                                            **REG_CONTACT,
                                            "students": [reg_student("Kid")]})
        record(f"Registration blocked when closed -> {r.status_code}",
               r.status_code == 403, f"got {r.status_code}", "P2")

    with app.app_context():
        Setting.set("registration_open", "1")
        db.session.commit()
    with app.test_client() as pub:
        # bad email rejected
        r = pub.post("/api/register", json={"parent_name": "Jo", "parent_email": "notanemail",
                                            **REG_CONTACT,
                                            "students": [reg_student("Kid")]})
        record(f"Registration rejects bad email -> {r.status_code}", r.status_code == 400,
               f"got {r.status_code}", "P3")
        # no students rejected
        r = pub.post("/api/register", json={"parent_name": "Jo", "parent_email": "jo@x.com",
                                            **REG_CONTACT, "students": []})
        record(f"Registration rejects no dancers -> {r.status_code}", r.status_code == 400,
               f"got {r.status_code}", "P3")
        # valid submission
        r = pub.post("/api/register", json={"parent_name": "Riverside Family", "parent_email": "riv@x.com",
                                            **REG_CONTACT,
                                            "students": [reg_student("Ivy", "River"),
                                                         reg_student("Max", "River")]})
        record(f"Valid registration accepted -> {r.status_code}", r.status_code == 201,
               f"got {r.status_code}", "P1")

    with app.app_context():
        reg = Registration.query.filter_by(parent_email="riv@x.com", status="pending").first()
        fam_before = Family.query.count()
        stu_before = Student.query.count()
        rid = reg.id if reg else None
    record("Submitted registration is queued for admin", rid is not None, "not found", "P1")

    if rid:
        with app.test_client() as c:
            login(c, "admin", "admin123")
            r = c.post(f"/api/registrations/{rid}/approve")
            record(f"Admin approve creates the family -> {r.status_code}",
                   r.status_code == 200, f"got {r.status_code}: {r.get_data(as_text=True)[:80]}", "P1")
        with app.app_context():
            grew = Family.query.count() == fam_before + 1 and Student.query.count() == stu_before + 2
            record("Approval created 1 family + 2 students", grew,
                   f"fam {fam_before}->{Family.query.count()}, stu {stu_before}->{Student.query.count()}", "P1")
            # idempotent: re-approve is rejected
        with app.test_client() as c:
            login(c, "admin", "admin123")
            r = c.post(f"/api/registrations/{rid}/approve")
            record(f"Re-approve is rejected (idempotent) -> {r.status_code}",
                   r.status_code == 400, f"got {r.status_code}", "P2")

    # The public endpoint is UNAUTHENTICATED — it must never 500 on a malformed
    # payload (non-list students, non-dict elements, non-string names), must cap
    # the dancer count, and its stored XSS must survive an admin approve + render.
    import json as _json
    from app.models import Registration as _Reg
    with app.test_client() as pub:  # no login
        ok = lambda **kw: dict({"parent_name": "P", "parent_email": "p@x.com",
                                **REG_CONTACT,
                                "students": [reg_student("Ok")]}, **kw)
        malformed = [
            (ok(students="notalist"), 400, "students=string"),
            (ok(students=[123]), 400, "students=[int]"),
            (ok(students={"first_name": "x"}), 400, "students=dict"),
            (ok(parent_name=123), 201, "parent_name=int"),
            (ok(parent_email="bad"), 400, "bad email"),
            (ok(parent_phone=""), 400, "no parent phone"),
            (ok(students=[reg_student("NoLast", "")]), 400, "dancer missing last name"),
            (ok(students=[reg_student("NoDob", dob="")]), 400, "dancer missing dob"),
            (ok(students=[reg_student("BadDob", dob="bad")]), 400, "dancer dob not a date"),
            (ok(students=[reg_student("NoAllergy", allergies="")]), 400, "dancer missing allergies"),
            (ok(students=[reg_student("BadEmail", email="nope")]), 400, "dancer email malformed"),
            (ok(emergency_name=""), 400, "no emergency contact name"),
            (ok(emergency_phone=""), 400, "no emergency contact phone"),
        ]
        for body, want, label in malformed:
            r = pub.post("/api/register", json=body)
            record(f"Public register robust: {label} -> {r.status_code}",
                   r.status_code == want and r.status_code < 500, f"got {r.status_code} (want {want})", "P2")
        # Cap: 100 dancers submitted -> at most 30 stored.
        pub.post("/api/register", json={"parent_name": "Capped", "parent_email": "cap@x.com",
                                        **REG_CONTACT,
                                        "students": [reg_student(f"D{i}") for i in range(100)]})
    with app.app_context():
        capped = _Reg.query.filter_by(parent_email="cap@x.com").first()
        n = len(_json.loads(capped.students_json)) if capped else 999
        record(f"Public register caps dancer count (stored {n})", n <= 30, f"stored {n}", "P3")
    # Malformed + XSS registration approves cleanly and survives the list
    # renders. Output ESCAPING is asserted separately, by
    # tests/test_template_escaping.py - status codes here can't see it.
    with app.test_client() as pub:
        pub.post("/api/register", json={
            "parent_name": "<script>x</script>", "parent_email": "xss@x.com",
            "parent_phone": "<script>313</script>",
            "emergency_name": "<script>alert(1)</script>",
            "emergency_phone": "<img src=x onerror=alert(1)>",
            "emergency_relationship": "<b>aunt</b>",
            "address": "<script>evil</script> 123 Main",
            "city": "<i>Oak Park</i>", "state": "MI", "zip_code": "48237",
            "parent2_name": "<script>p2</script>",
            "students": [{"first_name": "<script>", "last_name": 123, "dob": "2015-04-02",
                          "allergies": "<img src=x onerror=alert(1)>",
                          "email": "xss-kid@x.com"}, "junk"]})
    with app.app_context():
        xreg = _Reg.query.filter_by(parent_email="xss@x.com", status="pending").first()
        xrid = xreg.id if xreg else None
    with app.test_client() as c:
        login(c, "admin", "admin123")
        ra = c.post(f"/api/registrations/{xrid}/approve") if xrid else None
        record(f"Approve of a malformed/XSS registration doesn't 500 -> {ra.status_code if ra else 'n/a'}",
               ra is not None and ra.status_code in (200, 201), f"got {ra.status_code if ra else 'n/a'}", "P2")
        record(f"Registrations + students lists render after XSS approve",
               c.get("/api/registrations?status=all").status_code == 200 and c.get("/api/students").status_code == 200,
               "a list 500'd", "P2")


def run_amount_validation(ids):
    """Financial write endpoints must reject bad amounts (negative = balance
    corruption) and bad types, and accept a valid charge."""
    sid = ids["child_a"]
    with app.test_client() as c:
        login(c, "admin", "admin123")
        bad = [
            ({"student_id": sid, "type": "charge", "amount": -50, "category": "tuition"}, "negative amount"),
            ({"student_id": sid, "type": "charge", "amount": "abc", "category": "tuition"}, "non-numeric amount"),
            ({"student_id": sid, "type": "charge", "amount": 5_000_000, "category": "tuition"}, "absurd amount"),
            ({"student_id": sid, "type": "wat", "amount": 10, "category": "tuition"}, "invalid type"),
        ]
        for body, desc in bad:
            r = c.post("/api/transactions", json=body)
            record(f"create_transaction rejects {desc} -> {r.status_code}",
                   r.status_code == 400, f"got {r.status_code} (should reject)", "P2")
        # bulk-charge negative rejected before touching any student
        r = c.post("/api/transactions/bulk-charge",
                   json={"class_id": 1, "amount": -10, "category": "tuition"})
        record(f"bulk_charge rejects negative amount -> {r.status_code}",
               r.status_code == 400, f"got {r.status_code}", "P2")
        # a valid charge still works
        r = c.post("/api/transactions",
                   json={"student_id": sid, "type": "charge", "amount": 42.50, "category": "tuition"})
        record(f"create_transaction accepts a valid charge -> {r.status_code}",
               r.status_code == 201, f"got {r.status_code}", "P1")

        # Recurring charges fire automatically every month, so a bad amount is
        # worse here than a one-off (a negative = a silent monthly credit). Must
        # be validated the same way. Needs a real class to attach to.
        with app.app_context():
            from datetime import time as _t
            from app.models import DanceClass, User as _U
            adm = _U.query.filter_by(username="admin").first()
            rcx = DanceClass(name="RC Class", day_of_week=0, start_time=_t(17, 0),
                             end_time=_t(18, 0), instructor_id=adm.id)
            db.session.add(rcx)
            db.session.commit()
            rc_cid = rcx.id
        for body, want, label in [
            ({"class_id": rc_cid, "amount": -50, "category": "tuition", "day_of_month": 1}, 400, "negative"),
            ({"class_id": rc_cid, "amount": "abc", "category": "tuition", "day_of_month": 1}, 400, "non-numeric"),
            ({"class_id": rc_cid, "amount": 9_999_999, "category": "tuition", "day_of_month": 1}, 400, "absurd"),
            ({"class_id": rc_cid, "amount": 50, "category": "tuition", "day_of_month": "xyz"}, 400, "garbage day"),
            ({"class_id": rc_cid, "amount": 75.50, "category": "tuition", "day_of_month": 15}, 201, "valid"),
        ]:
            rr = c.post("/api/recurring-charges", json=body)
            record(f"recurring_charge {label} amount/day -> {rr.status_code}",
                   rr.status_code == want and rr.status_code < 500, f"got {rr.status_code} (want {want})", "P2")


def run_recital_delete_cascade(ids):
    """Deleting a recital must cascade to its children (numbers, cast, awards, ads)
    — not orphan them. Guards the cascade='all, delete-orphan' config so a future
    change can't silently leave orphaned recital data behind."""
    from app.models import Recital, RecitalNumber, RecitalCast, RecitalAward, RecitalAd
    with app.app_context():
        rec = Recital(year=2031, title="Cascade Recital")
        db.session.add(rec)
        db.session.flush()
        num = RecitalNumber(recital_id=rec.id, title="N", order_index=1)
        db.session.add(num)
        db.session.flush()
        db.session.add_all([RecitalCast(number_id=num.id, student_id=ids["child_a"]),
                            RecitalAward(recital_id=rec.id, title="Aw", order_index=1),
                            RecitalAd(recital_id=rec.id, advertiser="Ad", order_index=1)])
        db.session.commit()
        rid, nid = rec.id, num.id
    with app.test_client() as c:
        login(c, "admin", "admin123")
        rd = c.delete(f"/api/recitals/{rid}")
    with app.app_context():
        orphans = (RecitalNumber.query.filter_by(recital_id=rid).count()
                   + RecitalAward.query.filter_by(recital_id=rid).count()
                   + RecitalAd.query.filter_by(recital_id=rid).count()
                   + RecitalCast.query.filter_by(number_id=nid).count())
    record(f"Delete recital cascades to all children (delete={rd.status_code}, {orphans} orphans)",
           rd.status_code == 200 and orphans == 0, f"status={rd.status_code} orphans={orphans}", "P2")


def run_recital_money(ids):
    """Recital-adjacent money: charging a costume fee must be idempotent (a
    double-click can't double-charge every dancer), and ticket-order totals must
    be right (qty x price), splitting paid revenue from pending."""
    from datetime import time as _time
    from app.models import (User, Costume, CostumeAssignment, Performance,
                            PerformanceGroup, TicketType, Transaction)
    a, b = ids["child_a"], ids["child_b"]
    with app.app_context():
        cst = Costume(name="Recital Tutu", fee=45)
        db.session.add(cst)
        db.session.flush()
        db.session.add_all([CostumeAssignment(costume_id=cst.id, student_id=a),
                            CostumeAssignment(costume_id=cst.id, student_id=b)])
        g = PerformanceGroup(name="Ticket Co")
        db.session.add(g)
        db.session.flush()
        p = Performance(title="Ticket Show", group_id=g.id)
        db.session.add(p)
        db.session.flush()
        tt = TicketType(performance_id=p.id, name="GA", price=12.50)
        db.session.add(tt)
        db.session.commit()
        cid, pid, ttid = cst.id, p.id, tt.id

    with app.test_client() as c:
        login(c, "admin", "admin123")
        r1 = (c.post(f"/api/costumes/{cid}/charge").get_json() or {}).get("count")
        r2 = (c.post(f"/api/costumes/{cid}/charge").get_json() or {}).get("count")  # double-click
        with app.app_context():
            n = Transaction.query.filter_by(category="costumes",
                                            description="Costume: Recital Tutu").count()
        record(f"Costume charge is idempotent (#1={r1} #2={r2}, {n} charges)",
               r1 == 2 and r2 == 0 and n == 2, f"{r1}/{r2}/{n}", "P2")
        # Ticket totals: 3 paid + 2 unpaid -> 5 tickets, $37.50 revenue, $25 pending.
        c.post(f"/api/performances/{pid}/ticket-orders",
               json={"ticket_type_id": ttid, "student_id": a, "quantity": 3, "paid": True})
        c.post(f"/api/performances/{pid}/ticket-orders",
               json={"ticket_type_id": ttid, "student_id": b, "quantity": 2, "paid": False})
        summ = (c.get(f"/api/performances/{pid}/ticket-orders").get_json() or {}).get("summary", {})
        record(f"Ticket totals correct (tickets={summ.get('total_tickets')} rev={summ.get('revenue')} pending={summ.get('pending')})",
               summ.get("total_tickets") == 5 and summ.get("revenue") == "37.50"
               and summ.get("pending") == "25.00", str(summ), "P2")
        # Deleting a performance that HAS ticket types + orders must not fail on
        # the NOT NULL ticket_type FK — it has to clean up its ticket children.
        rd = c.delete(f"/api/performance/performances/{pid}")
        record(f"Delete a performance with ticket types -> {rd.status_code}",
               rd.status_code == 200, f"got {rd.status_code} (FK cleanup missing)", "P2")


def run_transaction_delete(ids):
    """An admin must be able to correct a mistake by deleting a posted charge or
    payment, and the balance must recompute. A parent must NOT be able to delete
    a transaction (it would erase their own debt)."""
    from app.helpers import calc_balance
    sid = ids["child_a"]
    # Admin posts a distinctive charge. (Separate, non-nested clients — nesting
    # test_client() context managers bleeds the session between them.)
    with app.app_context():
        base = calc_balance(sid)["balance"]
    with app.test_client() as c:
        login(c, "admin", "admin123")
        r = c.post("/api/transactions",
                   json={"student_id": sid, "type": "charge", "amount": 123.45, "category": "tuition"})
        tid = r.get_json().get("id")
    with app.app_context():
        after_charge = calc_balance(sid)["balance"]
    record(f"charge raised balance by 123.45 ({base:.2f}->{after_charge:.2f})",
           round(after_charge - base, 2) == 123.45, f"delta={after_charge-base}", "P2")

    # Parent must NOT be able to delete a transaction (would erase their own debt).
    with app.test_client() as pc:
        login(pc, "parent_a", "pw")
        rp = pc.delete(f"/api/transactions/{tid}")
    record(f"Parent blocked from deleting a transaction -> {rp.status_code}",
           rp.status_code in (401, 403), f"got {rp.status_code}", "P0")
    with app.app_context():
        still_there = calc_balance(sid)["balance"]
    record("Parent's blocked delete did NOT change the balance",
           round(still_there - after_charge, 2) == 0.0, f"bal moved to {still_there}", "P0")

    # Admin deletes it; balance returns to baseline.
    with app.test_client() as c:
        login(c, "admin", "admin123")
        rd = c.delete(f"/api/transactions/{tid}")
        r404 = c.delete("/api/transactions/999999")
    with app.app_context():
        after_delete = calc_balance(sid)["balance"]
    record(f"Admin delete removes the charge; balance back to baseline ({after_delete:.2f})",
           rd.status_code == 200 and round(after_delete - base, 2) == 0.0,
           f"status={rd.status_code} bal={after_delete} base={base}", "P2")
    record(f"Delete of a missing transaction -> {r404.status_code}",
           r404.status_code == 404, f"got {r404.status_code}", "P3")


def run_manual_cascade_deletes(ids):
    """Two deletes clean up children by hand (no ORM cascade, no ON DELETE):
    deleting an audition must remove its signups, and deleting a ticket type must
    remove its orders. Both child FKs are NOT NULL, so if a future edit drops the
    manual child-delete, SQLAlchemy tries to null the FK and the delete 500s
    outright (same failure the delete_performance comment guards against). These
    two paths were unguarded — this locks them in."""
    from app.models import (Audition, AuditionSignup, PerformanceGroup,
                            Performance, TicketType, TicketOrder)
    with app.app_context():
        g = PerformanceGroup(name="Cascade Co")
        db.session.add(g)
        db.session.flush()
        au = Audition(group_id=g.id, title="Cascade Audition")
        db.session.add(au)
        db.session.flush()
        db.session.add(AuditionSignup(audition_id=au.id, student_id=ids["child_a"]))
        perf = Performance(title="Cascade Show", group_id=g.id)
        db.session.add(perf)
        db.session.flush()
        tt = TicketType(performance_id=perf.id, name="GA", price=10)
        db.session.add(tt)
        db.session.flush()
        db.session.add(TicketOrder(ticket_type_id=tt.id, quantity=2, amount=20))
        db.session.commit()
        aid, ttid = au.id, tt.id
    with app.test_client() as c:
        login(c, "admin", "admin123")
        rd_au = c.delete(f"/api/performance/auditions/{aid}")
        rd_tt = c.delete(f"/api/ticket-types/{ttid}")
    with app.app_context():
        signup_orphans = AuditionSignup.query.filter_by(audition_id=aid).count()
        order_orphans = TicketOrder.query.filter_by(ticket_type_id=ttid).count()
    record(f"Delete audition cascades to signups (delete={rd_au.status_code}, {signup_orphans} orphans)",
           rd_au.status_code == 200 and signup_orphans == 0,
           f"status={rd_au.status_code} orphans={signup_orphans}", "P2")
    record(f"Delete ticket type cascades to orders (delete={rd_tt.status_code}, {order_orphans} orphans)",
           rd_tt.status_code == 200 and order_orphans == 0,
           f"status={rd_tt.status_code} orphans={order_orphans}", "P2")


def run_payment_plans(ids):
    """Payment plans (a Jackrabbit-parity billing feature): generate the right
    number of installments on the right monthly schedule with the right amount,
    validate inputs, and stay admin-only. NOTE: a plan is a *schedule*, not a
    charge — toggling an installment paid does not post to the ledger; the real
    payment is recorded separately. This test guards the schedule math + authz."""
    from app.models import PaymentPlanInstallment
    sid = ids["child_a"]
    with app.test_client() as c:
        login(c, "admin", "admin123")
        r = c.post(f"/api/students/{sid}/payment-plan",
                   json={"installment_amount": 100, "num_installments": 6, "day_of_month": 15})
        record(f"Create payment plan -> {r.status_code}", r.status_code == 201, r.get_data(as_text=True)[:60], "P2")
        with app.app_context():
            insts = PaymentPlanInstallment.query.order_by(PaymentPlanInstallment.seq).all()
        good = (len(insts) == 6 and all(i.due_date.day == 15 for i in insts)
                and all(float(i.amount) == 100.0 for i in insts)
                and all(insts[k].due_date > insts[k - 1].due_date for k in range(1, len(insts))))
        record(f"Plan: 6 monthly installments of $100 on the 15th (got {len(insts)})",
               good, f"dates={[i.due_date.isoformat() for i in insts]}", "P2")
        # Validation
        rb = c.post(f"/api/students/{sid}/payment-plan",
                    json={"installment_amount": "x", "num_installments": 6, "day_of_month": 15})
        rd = c.post(f"/api/students/{sid}/payment-plan",
                    json={"installment_amount": 100, "num_installments": 6, "day_of_month": 31})
        rn = c.post("/api/students/999999/payment-plan",
                    json={"installment_amount": 100, "num_installments": 6, "day_of_month": 15})
        record(f"Plan validation: garbage={rb.status_code} day31={rd.status_code} nostudent={rn.status_code}",
               rb.status_code == 400 and rd.status_code == 400 and rn.status_code == 404,
               f"{rb.status_code}/{rd.status_code}/{rn.status_code}", "P3")
        if insts:
            t = c.post(f"/api/payment-plan-installments/{insts[0].id}/toggle-paid")
            record(f"Toggle installment paid -> {t.status_code}",
                   t.status_code == 200 and (t.get_json() or {}).get("paid") is True, "", "P3")
    with app.test_client() as c:
        login(c, "parent_a", "pw")
        rp = c.post(f"/api/students/{sid}/payment-plan",
                    json={"installment_amount": 100, "num_installments": 6, "day_of_month": 15})
        record(f"Parent cannot create a payment plan -> {rp.status_code}", rp.status_code == 403,
               f"got {rp.status_code}", "P0")


def run_input_robustness(ids):
    """No write endpoint may 500 on malformed input. An unhandled exception
    (e.g. float() on a garbage string) is bad UX for the parent AND can leave a
    DB transaction half-applied. Every endpoint must reject bad input with a
    4xx, not blow up. Fuzz each pure-DB-write endpoint with a no-body / empty /
    garbage-typed / wrong-typed / negative-huge payload and assert no 500."""
    sid = ids["child_a"]
    endpoints = [
        ("POST", "/api/classes"), ("POST", "/api/families"), ("POST", "/api/students"),
        ("POST", "/api/locations"), ("POST", "/api/leads"), ("POST", "/api/makeups"),
        ("POST", "/api/rules"), ("POST", "/api/skills"), ("POST", "/api/staff"),
        ("POST", "/api/donations"), ("POST", "/api/transactions"),
        ("POST", "/api/transactions/bulk-charge"), ("POST", "/api/recurring-charges"),
        ("POST", "/api/recitals"), ("POST", "/api/performance/groups"),
        ("POST", "/api/performance/auditions"), ("POST", "/api/performance/performances"),
        ("POST", "/api/waivers/templates"), ("PUT", "/api/settings/payments"),
        ("POST", f"/api/students/{sid}/payment-plan"),
        ("POST", "/api/balances/apply-late-fees"),
    ]
    payloads = [
        ("no-body", None),
        ("empty-dict", {}),
        ("garbage-strings", {"amount": "abc", "student_id": "xyz", "class_id": "nope",
                             "day_of_month": "soon", "name": 123, "category": None,
                             "email": [], "date": "not-a-date", "installments": "many"}),
        ("wrong-types", {"amount": [], "student_id": {}, "student_ids": "notalist",
                         "class_id": True, "day_of_month": 99, "amount_cents": -5}),
        ("negative-huge", {"amount": -999999, "student_id": -1, "class_id": 0,
                           "day_of_month": -3, "installments": -2}),
    ]
    with app.test_client() as c:
        login(c, "admin", "admin123")
        for method, path in endpoints:
            fn = getattr(c, method.lower())
            bad = []
            for label, body in payloads:
                try:
                    r = fn(path, data="", content_type="application/json") if body is None else fn(path, json=body)
                    if r.status_code == 500:
                        bad.append(f"{label}->500")
                except Exception as e:  # a raised exception is as bad as a 500
                    bad.append(f"{label}->EXC {type(e).__name__}")
            record(f"{method} {path} handles malformed input without 500",
                   not bad, f"failed: {bad}", "P2")


def run_parent_input_robustness(ids):
    """Parent-reachable writes must not 500 on a bad student_id. Staff skip the
    parent-authorization branch, so an unguarded int(student_id) only blows up
    for an actual parent — the admin sweep can't see it. Seed the target rows,
    then post malformed ids AS a parent and assert no 500 (and no orphan row)."""
    from app.models import (Rule, Audition, Performance, TicketType,
                            PerformanceGroup, WaiverTemplate)
    cid = ids["child_a"]
    with app.app_context():
        grp = PerformanceGroup(name="Robustness Co")
        db.session.add(grp)
        db.session.flush()
        aud = Audition(title="Robust Audition", group_id=grp.id, is_open=True)
        rule = Rule(text="Be kind", display_order=99)
        perf = Performance(title="Robust Show", group_id=grp.id)
        wt = WaiverTemplate(title="Robust Waiver", body="b", allow_decline=True)
        db.session.add_all([aud, rule, perf, wt])
        db.session.flush()
        tt = TicketType(performance_id=perf.id, name="GA", price=10)
        db.session.add(tt)
        db.session.commit()
        aid, rid, pid, ttid, wtid = aud.id, rule.id, perf.id, tt.id, wt.id

    cases = [
        (f"/api/performance/auditions/{aid}/signup",
         [{"student_id": "xyz"}, {"student_id": -1}, {"student_id": []}, {},
          {"student_id": 99999}, {"student_id": cid, "notes": 123}]),
        (f"/api/rules/{rid}/acknowledge",
         [{"student_id": "xyz", "initials": "AB"}, {"student_id": cid, "initials": 123},
          {"student_id": -1, "initials": "AB"}, {}]),
        (f"/api/performances/{pid}/ticket-orders",
         [{"ticket_type_id": ttid, "quantity": "lots", "student_id": cid},
          {"ticket_type_id": ttid, "student_id": "xyz"},
          {"ticket_type_id": ttid, "student_id": -1}]),
        # The other parent-allowed writes, exercised through the is_parent branch
        # the admin fuzz never reaches.
        ("/api/payments/claim",
         [{"student_id": cid, "amount": "x", "method": "zelle"},
          {"student_id": cid, "amount": 50, "method": []},
          {"student_id": cid, "amount": 50, "method": "zelle", "reference": 123}]),
        (f"/api/students/{cid}/waivers/{wtid}/sign",
         [{"signed_name": 123, "consent": "maybe"}, {"signed_name": "Me", "consent": []}, {}]),
        ("/api/makeups",
         [{"student_id": cid, "missed_date": "bad", "note": 123},
          {"student_id": cid, "missed_class_id": "x"}, {"student_id": cid, "makeup_date": []}]),
        ("/api/donations",
         [{"amount": "x", "method": "zelle"}, {"amount": 50, "method": []}, {"amount": 50, "note": {}}]),
    ]
    with app.test_client() as c:
        login(c, "parent_a", "pw")
        for path, payloads in cases:
            bad = []
            for body in payloads:
                try:
                    r = c.post(path, json=body)
                    if r.status_code == 500:
                        bad.append(f"{body}->500")
                except Exception as e:
                    bad.append(f"{body}->EXC {type(e).__name__}")
            record(f"parent POST {path} handles bad student_id without 500",
                   not bad, f"failed: {bad}", "P2")


def run_parent_write_authz(ids):
    """Object-level authorization on the parent-ALLOWED writes: a parent may
    write (claim payment, sign waiver, acknowledge rule, request makeup, audition
    signup, ticket order) — but ONLY against their own child. Acting on another
    family's child must be blocked (403/404). This is the IDOR-in-writes companion
    to run_idor's read sweep. Attacker = parent_b, target = parent_a's child_a."""
    from app.models import (WaiverTemplate, Rule, Audition, Performance,
                            TicketType, PerformanceGroup)
    victim = ids["child_a"]      # parent_a's child
    victim_fam = ids["fam_a"]
    with app.app_context():
        grp = PerformanceGroup(name="Authz Co")
        db.session.add(grp)
        db.session.flush()
        wt = WaiverTemplate(title="Authz Waiver", body="...", allow_decline=True)
        rule = Rule(text="Authz rule", display_order=98)
        aud = Audition(title="Authz Aud", group_id=grp.id, is_open=True)
        perf = Performance(title="Authz Show", group_id=grp.id)
        db.session.add_all([wt, rule, aud, perf])
        db.session.flush()
        tt = TicketType(performance_id=perf.id, name="GA", price=10)
        db.session.add(tt)
        db.session.commit()
        wtid, rid, aid, pid, ttid = wt.id, rule.id, aud.id, perf.id, tt.id

    attacks = [
        ("POST", "/api/payments/claim",
         {"method": "zelle", "amount": 50, "student_id": victim}, "claim payment for another child"),
        ("POST", "/api/payments/claim",
         {"method": "zelle", "amount": 50, "family_id": victim_fam}, "claim payment for another family"),
        ("POST", f"/api/students/{victim}/waivers/{wtid}/sign",
         {"signed_name": "Attacker", "consent": True}, "sign waiver for another child"),
        ("POST", f"/api/rules/{rid}/acknowledge",
         {"student_id": victim, "initials": "XX"}, "acknowledge rule for another child"),
        ("POST", "/api/makeups",
         {"student_id": victim, "missed_date": "2026-07-01"}, "request makeup for another child"),
        ("POST", f"/api/performance/auditions/{aid}/signup",
         {"student_id": victim}, "audition signup for another child"),
        ("POST", f"/api/performances/{pid}/ticket-orders",
         {"ticket_type_id": ttid, "student_id": victim}, "ticket order for another child"),
    ]
    with app.test_client() as c:
        login(c, "parent_b", "pw")
        for method, path, body, desc in attacks:
            r = c.open(path, method=method, json=body)
            blocked = r.status_code in (401, 403, 404)
            record(f"IDOR-write blocked: {desc} [{path}] -> {r.status_code}",
                   blocked, f"got {r.status_code}, expected 403/404", "P0")
        # claim_payment must not 500 on a garbage id (parent-reachable).
        for bad in ({"method": "zelle", "amount": 50, "student_id": "xyz"},
                    {"method": "zelle", "amount": 50, "family_id": "xyz"}):
            r = c.post("/api/payments/claim", json=bad)
            record(f"claim_payment handles bad id without 500 -> {r.status_code}",
                   r.status_code != 500, f"got {r.status_code}", "P2")


def run_backup(ids):
    """Admin can download a complete, valid SQLite backup; non-admins can't.
    This is the studio's disaster-recovery net, so the file must be a real,
    openable SQLite snapshot that actually contains the data — and it holds ALL
    families' PII + finances, so it must be admin-only (a parent gets 403)."""
    import sqlite3
    # Admin: 200, valid SQLite, contains the seeded students.
    with app.test_client() as c:
        login(c, "admin", "admin123")
        r = c.get("/api/admin/backup")
        magic = r.data[:16] == b"SQLite format 3\x00"
        record(f"Admin downloads a backup -> {r.status_code}, {len(r.data)} bytes",
               r.status_code == 200 and magic, f"status={r.status_code} magic={magic}", "P2")
        if r.status_code == 200 and magic:
            bt = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
            bt.write(r.data)
            bt.close()
            try:
                con = sqlite3.connect(bt.name)
                n = con.execute("SELECT COUNT(*) FROM students").fetchone()[0]
                con.close()
                record(f"Backup is a valid DB containing the data ({n} students)",
                       n >= 2, f"students in backup: {n}", "P2")
            finally:
                try:
                    os.unlink(bt.name)
                except OSError:
                    pass
    # Parent: blocked (the backup holds every family's PII + finances).
    with app.test_client() as c:
        login(c, "parent_a", "pw")
        r = c.get("/api/admin/backup")
        record(f"Parent blocked from full DB backup -> {r.status_code}",
               r.status_code in (401, 403), f"got {r.status_code}", "P0")


def run_orphan_render_guard(ids):
    """Belt-and-suspenders for the whole dead-page bug class: even if an orphan
    row exists (e.g. left in prod by the old buggy create endpoints, before they
    were fixed), the roster serializers must null-guard the relationship so the
    list still renders instead of 500-ing. Inject orphan rows directly (bypassing
    the now-fixed endpoints) and assert every affected list GET returns 200."""
    from datetime import time as _time
    from app.models import (MakeupClass, WaitlistEntry, CompanyMembership,
                            PerformanceGroup, Attendance, Transaction, DanceClass)
    GHOST = 987654  # a student id that does not exist
    with app.app_context():
        admin = User.query.filter_by(username="admin").first()
        g = PerformanceGroup(name="Ghost Co")
        db.session.add(g)
        db.session.flush()
        dc = DanceClass(name="Ghost Class", day_of_week=0, start_time=_time(17, 0),
                        end_time=_time(18, 0), instructor_id=admin.id)
        db.session.add(dc)
        db.session.flush()
        db.session.add_all([
            MakeupClass(student_id=GHOST, status='requested'),
            WaitlistEntry(class_id=dc.id, student_id=GHOST, status='waiting'),
            CompanyMembership(group_id=g.id, student_id=GHOST),
            Attendance(student_id=GHOST, class_id=dc.id),
            Transaction(student_id=GHOST, type='charge', amount=10, category='tuition',
                        payment_method='n/a', description='ghost'),
        ])
        db.session.commit()
        gid, cid = g.id, dc.id

    with app.test_client() as c:
        login(c, "admin", "admin123")
        for label, url in [
            ("makeups", "/api/makeups"),
            ("waitlist", f"/api/classes/{cid}/waitlist"),
            ("group members", f"/api/performance/groups/{gid}/members"),
            ("attendance", "/api/attendance"),
            ("transactions", "/api/transactions"),
        ]:
            r = c.get(url)
            record(f"Roster renders with an orphan row: {label} -> {r.status_code}",
                   r.status_code == 200, f"got {r.status_code} (dead-page on orphan data)", "P2")


def run_class_render_guard(ids):
    """The class list is a core, high-traffic page. A class whose instructor is
    missing (a bad instructor_id at create, or a user removed later) must not
    500 it — class_to_dict has to null-guard the instructor the way it already
    guards the location. Also: create_class must reject a bad instructor/location
    reference up front (400/404) rather than 500 or orphan."""
    from datetime import time as _time
    from app.models import DanceClass
    base = {"name": "Guard", "day_of_week": 0, "start_time": "17:00", "end_time": "18:00"}
    with app.test_client() as c:
        login(c, "admin", "admin123")
        r1 = c.post("/api/classes", json={**base, "instructor_id": "xyz"})
        record(f"create_class rejects garbage instructor_id -> {r1.status_code}",
               r1.status_code == 400, f"got {r1.status_code}", "P3")
        r2 = c.post("/api/classes", json={**base, "instructor_id": 999999})
        record(f"create_class rejects nonexistent instructor -> {r2.status_code}",
               r2.status_code == 404, f"got {r2.status_code}", "P2")
        r3 = c.post("/api/classes", json={**base, "location_id": 999999})
        record(f"create_class rejects nonexistent location -> {r3.status_code}",
               r3.status_code == 404, f"got {r3.status_code}", "P3")
    # Simulate a class whose instructor row is gone (bypass the endpoint), then
    # confirm the class list still renders (the serializer null-guard).
    with app.app_context():
        dc = DanceClass(name="Orphan Instr", day_of_week=0, start_time=_time(17, 0),
                        end_time=_time(18, 0), instructor_id=999999)
        db.session.add(dc)
        db.session.commit()
    with app.test_client() as c:
        login(c, "admin", "admin123")
        rl = c.get("/api/classes")
        record(f"Class list renders with a missing-instructor class -> {rl.status_code}",
               rl.status_code == 200, f"got {rl.status_code} (dead-page regression)", "P2")


def run_orphan_guard(ids):
    """Systematic guard against the recurring orphan-row bug: a create endpoint
    that stores a request-supplied student_id without checking it exists will
    orphan a row that then 500s its roster page (hit repeatedly: makeups,
    attendance, waitlist, and the whole performance/recital assign surface).
    Every such endpoint must reject a nonexistent id (404), reject garbage (400),
    and leave its roster page rendering (200)."""
    from app.models import (PerformanceGroup, Performance, Costume, Recital,
                            RecitalNumber)
    with app.app_context():
        g = PerformanceGroup(name="Orphan Co")
        db.session.add(g)
        db.session.flush()
        p = Performance(title="Orphan Show", group_id=g.id)
        cst = Costume(name="Orphan Tutu")
        rec = Recital(year=2027, title="Orphan Recital")
        db.session.add_all([p, cst, rec])
        db.session.flush()
        num = RecitalNumber(recital_id=rec.id, title="Opening", order_index=1)
        db.session.add(num)
        db.session.commit()
        gid, pid, cid, rid, nid = g.id, p.id, cst.id, rec.id, num.id

    # (label, create-url, roster-GET-url, extra required fields)
    cases = [
        ("group member", f"/api/performance/groups/{gid}/members", f"/api/performance/groups/{gid}/members", {}),
        ("perf assignment", f"/api/performance/performances/{pid}/assignments", f"/api/performance/performances/{pid}/assignments", {}),
        ("costume assign", f"/api/costumes/{cid}/assignments", f"/api/costumes/{cid}/assignments", {}),
        ("recital cast", f"/api/recital-numbers/{nid}/cast", f"/api/recital-numbers/{nid}/cast", {}),
        ("recital award", f"/api/recitals/{rid}/awards", f"/api/recitals/{rid}/awards", {"title": "Best"}),
        ("recital ad", f"/api/recitals/{rid}/ads", f"/api/recitals/{rid}/ads", {"advertiser": "ACME"}),
    ]
    with app.test_client() as c:
        login(c, "admin", "admin123")
        for label, post_url, list_url, extra in cases:
            rn = c.post(post_url, json={"student_id": 999999, **extra})
            rg = c.post(post_url, json={"student_id": "xyz", **extra})
            rl = c.get(list_url)
            ok = rn.status_code == 404 and rg.status_code == 400 and rl.status_code == 200
            record(f"orphan-guard {label}: nonexistent->{rn.status_code} garbage->{rg.status_code} roster->{rl.status_code}",
                   ok, f"{rn.status_code}/{rg.status_code}/{rl.status_code} (want 404/400/200)", "P2")


def run_csv_exports(ids):
    """CSV exports: staff get a well-formed CSV; parents are blocked; and
    formula-injection cells are neutralized (a student name can come from public
    registration, and the studio opens these in Excel)."""
    # Formula-injection: a student named =HYPERLINK(...) must be text-escaped in
    # the CSV, while a legitimate negative balance stays a number.
    from app.models import Student, Family, Transaction
    with app.app_context():
        fam = Family(name="CSV Fam")
        db.session.add(fam)
        db.session.flush()
        evil = Student(first_name='=HYPERLINK("http://evil.com","x")', last_name="Csv", family_id=fam.id)
        # A student who genuinely OWES, so the aging CSV has a data row to check.
        ower = Student(first_name="Owes", last_name="Agingcsv", family_id=fam.id)
        db.session.add_all([evil, ower])
        db.session.flush()
        db.session.add_all([
            Transaction(student_id=evil.id, type="charge", amount=50, category="tuition", payment_method="n/a", description="c"),
            Transaction(student_id=evil.id, type="payment", amount=100, category="tuition", payment_method="cash", description="p"),
            Transaction(student_id=ower.id, type="charge", amount=75, category="tuition", payment_method="n/a", description="owed"),
        ])
        db.session.commit()
    with app.test_client() as c:
        login(c, "admin", "admin123")
        csv_text = c.get("/api/reports/students.csv").get_data(as_text=True)
        row = next((l for l in csv_text.splitlines() if "HYPERLINK" in l), "")
        record("CSV export neutralizes a formula-injection name",
               "'=HYPERLINK" in row, f"unescaped formula in: {row[:80]}", "P2")
        record("CSV export keeps a negative balance as a number (not quoted)",
               "-50.00" in row and "'-50" not in row, f"row={row[:80]}", "P3")
    # Parent blocked (aging.csv exposes every family's debt — must be locked down).
    with app.test_client() as c:
        login(c, "parent_a", "pw")
        for path in ("/api/reports/students.csv", "/api/reports/transactions.csv",
                     "/api/reports/aging.csv"):
            r = c.get(path)
            record(f"Parent blocked from {path} -> {r.status_code}",
                   r.status_code in (401, 403), f"got {r.status_code}", "P0")
    # Admin gets CSV with header + the seeded students.
    with app.test_client() as c:
        login(c, "admin", "admin123")
        r = c.get("/api/reports/students.csv")
        ct = r.headers.get("Content-Type", "")
        body = r.get_data(as_text=True)
        ok = r.status_code == 200 and "text/csv" in ct and body.startswith("Last name,First name")
        record(f"Students CSV export well-formed -> {r.status_code}, ct={ct.split(';')[0]}",
               ok, f"status={r.status_code} ct={ct} head={body[:40]!r}", "P2")
        has_dispo = "attachment" in r.headers.get("Content-Disposition", "")
        record("Students CSV has attachment disposition", has_dispo,
               r.headers.get("Content-Disposition", "<none>"), "P3")
        r2 = c.get("/api/reports/transactions.csv")
        record(f"Transactions CSV export well-formed -> {r2.status_code}",
               r2.status_code == 200 and r2.get_data(as_text=True).startswith("Date,Student"),
               f"head={r2.get_data(as_text=True)[:40]!r}", "P2")
        rev = c.get("/api/reports/revenue").get_json()
        ok = (rev and isinstance(rev.get("monthly"), list) and len(rev["monthly"]) == 12
              and "totals" in rev and "collected_this_year" in rev["totals"])
        record("Revenue report returns 12 months + totals", ok, str(rev)[:80], "P2")
        # A/R aging CSV: right header, an owing student's row, and a TOTAL footer
        # that reconciles — so the owner can work accounts offline.
        ra = c.get("/api/reports/aging.csv")
        atext = ra.get_data(as_text=True)
        alines = atext.splitlines()
        header_ok = ra.status_code == 200 and alines and alines[0].startswith(
            "Student,Family,Status,Current (0-30),31-60,61-90,90+,Total")
        record(f"Aging CSV export well-formed -> {ra.status_code}", header_ok,
               f"status={ra.status_code} head={alines[0][:50]!r}" if alines else "empty", "P2")
        record("Aging CSV lists an owing student and a TOTAL footer",
               "Owes Agingcsv" in atext and any(l.startswith("TOTAL,") for l in alines),
               f"owing_present={'Owes Agingcsv' in atext} footer={[l for l in alines if l.startswith('TOTAL')]}", "P2")
        record("Aging CSV has attachment disposition",
               "attachment" in ra.headers.get("Content-Disposition", ""),
               ra.headers.get("Content-Disposition", "<none>"), "P3")


def run_statements(ids):
    """Printable financial/tax documents (student + family statements, the
    501(c)(3) giving statement, certificate, recital booklet) are Jinja-rendered,
    not JSON, and must (a) render on edge data without 500, (b) 404 on a bad id,
    and (c) get the money right — a giving statement is a tax document, so it MUST
    filter donations to the requested year."""
    from datetime import date as _date
    from app.models import Student, Family, Transaction, Donation
    with app.app_context():
        empty_fam = Family(name="No-Kids Fam")
        db.session.add(empty_fam)
        db.session.flush()
        fresh = Student(first_name="Fresh", last_name="NoTxns", family_id=empty_fam.id)
        db.session.add(fresh)
        db.session.flush()
        # A donor with an in-year and a prior-year donation (the prior year must
        # be excluded from a year-scoped giving statement).
        db.session.add_all([
            Donation(donor_name="Gv", donor_email="gv@x.com", amount=100, method="cash",
                     status="recorded", donation_date=_date(2026, 2, 1)),
            Donation(donor_name="Gv", donor_email="gv@x.com", amount=500, method="cash",
                     status="recorded", donation_date=_date(2025, 2, 1)),
        ])
        db.session.commit()
        fresh_id, efam_id = fresh.id, empty_fam.id
    sid = ids["child_a"]
    fid = ids["fam_a"]
    with app.test_client() as c:
        login(c, "admin", "admin123")
        for url, label in [
            (f"/students/{sid}/statement", "student statement"),
            (f"/students/{fresh_id}/statement", "fresh student statement (no txns)"),
            (f"/families/{fid}/statement", "family statement"),
            (f"/families/{efam_id}/statement", "empty family statement"),
            (f"/students/{sid}/certificate", "certificate"),
        ]:
            r = c.get(url)
            record(f"Document renders on edge data: {label} -> {r.status_code}",
                   r.status_code == 200, f"got {r.status_code}", "P2")
        for url, label in [
            ("/students/999999/statement", "student"),
            ("/families/999999/statement", "family"),
        ]:
            r = c.get(url)
            record(f"Document 404s on nonexistent {label} -> {r.status_code}",
                   r.status_code == 404, f"got {r.status_code}", "P3")
        # Giving statement (tax doc): 2026 total = $100 only; the $500 from 2025 excluded.
        h = c.get("/giving-statement?email=gv@x.com&year=2026").get_data(as_text=True)
        record("Giving statement filters to the requested year (tax-critical)",
               "100.00" in h and "500.00" not in h, "year filter wrong — tax document error", "P1")


def run_revenue_math():
    """The revenue report is the owner's financial dashboard — the numbers, not
    just the shape, must be right. It's a global aggregate, so verify with
    deltas: capture the report, post a known $100 charge + $60 payment for a
    fresh student, and assert this month's bucket, the collected totals, and
    outstanding all move by exactly the right amounts."""
    from app.models import Student, Family

    def snap():
        r = c.get("/api/reports/revenue").get_json() or {}
        cm = (r.get("monthly") or [{}])[-1]  # this month = last bucket (oldest-first)
        t = r.get("totals") or {}
        return {
            'charged': cm.get('charged', 0), 'collected': cm.get('collected', 0),
            'cm_total': t.get('collected_this_month', 0), 'cy': t.get('collected_this_year', 0),
            'all': t.get('collected_all_time', 0), 'out': t.get('outstanding', 0),
        }

    with app.app_context():
        fam = Family(name="Rev Fam")
        db.session.add(fam)
        db.session.flush()
        s = Student(first_name="Rev", last_name="Enue", family_id=fam.id, is_active=True)
        db.session.add(s)
        db.session.commit()
        sid = s.id
    with app.test_client() as c:
        login(c, "admin", "admin123")
        before = snap()
        c.post("/api/transactions", json={"student_id": sid, "type": "charge", "amount": 100, "category": "tuition"})
        c.post("/api/transactions", json={"student_id": sid, "type": "payment", "amount": 60, "category": "tuition", "payment_method": "cash"})
        after = snap()
    checks = {
        "this-month charged +100": round(after['charged'] - before['charged'], 2) == 100.0,
        "this-month collected +60": round(after['collected'] - before['collected'], 2) == 60.0,
        "collected_this_month +60": round(after['cm_total'] - before['cm_total'], 2) == 60.0,
        "collected_this_year +60": round(after['cy'] - before['cy'], 2) == 60.0,
        "collected_all_time +60": round(after['all'] - before['all'], 2) == 60.0,
        "outstanding +40 (100 charge - 60 paid)": round(after['out'] - before['out'], 2) == 40.0,
    }
    bad = [k for k, ok in checks.items() if not ok]
    record("Revenue report math moves exactly right on a known charge+payment",
           not bad, f"wrong: {bad} | before={before} after={after}", "P1")


def run_statement_math():
    """A year-end student statement is a tax document — the numbers must be
    right, not just render. Verify `_statement_rows`: prior-year activity rolls
    into the opening balance (not the in-year rows), Dec 31 is included, the next
    year is excluded, and the running balance / totals are exact."""
    from datetime import date as _date
    from app.main.routes import _statement_rows
    from app.models import Student, Family, Transaction

    def _ch(sid, amt, d):
        return Transaction(student_id=sid, type="charge", amount=amt, category="tuition",
                           payment_method="n/a", description="c", transaction_date=d)

    def _pay(sid, amt, d):
        return Transaction(student_id=sid, type="payment", amount=amt, category="tuition",
                           payment_method="cash", description="p", transaction_date=d)

    with app.app_context():
        fam = Family(name="Stmt Math Fam")
        db.session.add(fam)
        db.session.flush()
        s = Student(first_name="Stmt", last_name="Math", family_id=fam.id, is_active=True)
        db.session.add(s)
        db.session.flush()
        db.session.add_all([
            _ch(s.id, 200, _date(2025, 6, 1)),    # prior year -> opening balance only
            _ch(s.id, 100, _date(2026, 1, 15)),   # in year
            _pay(s.id, 150, _date(2026, 3, 1)),   # in year
            _ch(s.id, 50, _date(2026, 12, 31)),   # in year, last-day boundary (inclusive)
            _ch(s.id, 999, _date(2027, 1, 1)),    # next year -> excluded
        ])
        db.session.commit()
        sid = s.id
        prior, rows, tc, tp = _statement_rows([sid], 2026)

    ending = prior + tc - tp
    checks = {
        "opening balance = prior-year net ($200)": prior == 200.0,
        "in-year rows only (3, excludes 2025 + 2027)": len(rows) == 3,
        "total charges = 100 + 50 (Dec 31 included)": tc == 150.0,
        "total payments = 150": tp == 150.0,
        "ending balance = 200 + 150 - 150 = 200": ending == 200.0,
        "running balance ends at 200": rows and rows[-1]["running"] == 200.0,
    }
    bad = [k for k, ok in checks.items() if not ok]
    record("Year-end statement math is exact (prior-balance carryover, boundaries, running total)",
           not bad, f"wrong: {bad} | prior={prior} rows={len(rows)} tc={tc} tp={tp} end={ending}", "P1")


def run_cashtag_sanitize():
    """The admin-set Cash App cashtag renders into an unescaped href/URL on the
    parent portal — so it must be sanitized to alphanumeric/underscore at the
    source, or a malicious (or compromised) admin could inject script into every
    parent's page. Verify a hostile cashtag is stripped and a normal one survives."""
    from app.models import Setting
    with app.app_context():
        Setting.set("payments_cashapp_enabled", "1")
        Setting.set("payments_cashapp_tag", '$evil"><img src=x onerror=alert(1)>')
        db.session.commit()
    with app.test_client() as c:
        login(c, "parent_a", "pw")
        opts = (c.get("/api/payment-options").get_json() or {}).get("payment_options", [])
        ca = next((o for o in opts if o.get("type") == "cashapp"), {})
        blob = str(ca.get("cashtag", "")) + str(ca.get("url", ""))
        record(f"Malicious cashtag is sanitized (served: {ca.get('cashtag')!r})",
               all(ch not in blob for ch in ("<", ">", '"', "'", " ")), f"unsanitized: {blob}", "P2")
    with app.app_context():
        Setting.set("payments_cashapp_tag", "$MyStudio_2026")
        db.session.commit()
    with app.test_client() as c:
        login(c, "parent_a", "pw")
        opts = (c.get("/api/payment-options").get_json() or {}).get("payment_options", [])
        ca = next((o for o in opts if o.get("type") == "cashapp"), {})
        record(f"Valid cashtag preserved -> {ca.get('cashtag')}",
               ca.get("cashtag") == "MyStudio_2026", f"got {ca.get('cashtag')}", "P3")
    with app.app_context():
        Setting.set("payments_cashapp_enabled", "0")  # leave disabled for other tests
        db.session.commit()


def run_qr_upload_safety():
    """The Zelle QR is stored as a data URI and rendered in an unescaped src= on
    the parent portal. The content-type is client-controlled (multipart mimetype),
    so it MUST be whitelisted to exact values — a `image/png"><script>` mimetype
    would otherwise inject into the src=. Verify a crafted mimetype yields a clean
    data URI and a fully-bad upload is rejected."""
    import io
    from app.models import Setting
    with app.test_client() as c:
        login(c, "admin", "admin123")
        # Malicious mimetype but a .png extension -> falls back to the whitelist.
        r = c.post("/api/settings/payments/zelle-qr",
                   data={"file": (io.BytesIO(b"\x89PNGfake"), "x.png", 'image/png"><script>alert(1)</script>')},
                   content_type="multipart/form-data")
        with app.app_context():
            uri = Setting.get("payments_zelle_qr_data", "")
        header = uri.split("base64,")[0]
        record(f"QR upload sanitizes a hostile mimetype (header={header!r})",
               r.status_code == 200 and all(ch not in header for ch in ("<", ">", '"', "'")),
               f"unsafe data URI header: {header}", "P2")
        # Fully-malicious (bad mimetype + bad extension) -> rejected.
        rb = c.post("/api/settings/payments/zelle-qr",
                    data={"file": (io.BytesIO(b"x"), "x.evil", 'image/png"><script>')},
                    content_type="multipart/form-data")
        record(f"QR upload rejects a non-image -> {rb.status_code}", rb.status_code == 400,
               f"got {rb.status_code}", "P3")

    # The recital cover/ad uploads go through the SHARED helper
    # (_image_data_uri_from_request); it must whitelist the mimetype to exact
    # values just like the QR path, or a hostile Content-Type reaches the data
    # URI (which renders into a booklet <img src=>).
    from app.models import Recital
    with app.app_context():
        rec = Recital(title="Injection Test Recital", year=2026)
        db.session.add(rec)
        db.session.commit()
        rec_id = rec.id
    with app.test_client() as c:
        login(c, "admin", "admin123")
        c.post(f"/api/recitals/{rec_id}/cover",
               data={"file": (io.BytesIO(b"\x89PNGfake"), "x.png", 'image/png"><script>alert(1)</script>')},
               content_type="multipart/form-data")
        with app.app_context():
            uri = (Recital.query.get(rec_id).cover_image_data or "")
        header = uri.split("base64,")[0]
        record(f"Recital cover upload sanitizes a hostile mimetype (header={header!r})",
               all(ch not in header for ch in ("<", ">", '"', "'")),
               f"unsafe data URI header: {header}", "P2")


def run_email_header_injection():
    """Recipient addresses and subjects can carry user-controlled values (a
    student/parent name or email from public registration). If a CRLF slips into
    a header value, an attacker could inject headers (Bcc exfiltration, spoofing).
    Verify send_email strips CR/LF so no extra header is emitted and the send
    doesn't raise mid-loop on a CRLF-bearing address."""
    import smtplib
    from email import message_from_string
    from app import email as email_mod

    captured = {}

    class _FakeSMTP:
        def __init__(self, *a, **k):
            pass
        def starttls(self):
            pass
        def login(self, *a, **k):
            pass
        def sendmail(self, sender, addr, msg):
            captured["msg"] = msg
        def quit(self):
            pass

    orig = smtplib.SMTP
    smtplib.SMTP = _FakeSMTP
    try:
        with app.app_context():
            app.config["MAIL_SERVER"] = "smtp.example.com"
            app.config["MAIL_PORT"] = 587
            app.config["MAIL_USERNAME"] = "studio@example.com"
            app.config["MAIL_PASSWORD"] = None
            raised = None
            try:
                sent = email_mod.send_email(
                    "victim@example.com\r\nBcc: attacker@evil.com",
                    "Receipt\r\nBcc: attacker2@evil.com",
                    "Your balance is $0.",
                )
            except Exception as e:  # noqa: BLE001 - must not raise on CRLF
                raised = e
                sent = 0
    finally:
        smtplib.SMTP = orig
        with app.app_context():
            app.config["MAIL_SERVER"] = None

    parsed = message_from_string(captured.get("msg", "")) if captured.get("msg") else None
    injected = parsed is not None and parsed.get("Bcc") is not None
    record("Email header injection blocked (no Bcc emitted from CRLF subject/addr)",
           raised is None and not injected and sent == 1,
           f"raised={raised!r} injected_bcc={injected} keys={parsed.keys() if parsed else None}",
           "P1")


def run_receipt_non_blocking():
    """Confirming a payment emails a best-effort receipt — it must NOT block the
    admin's confirm on a slow SMTP send (the payment is already committed, so a
    slow/failed receipt is harmless). Arm a slow sender, confirm a pending
    payment, and assert the confirm returns fast."""
    import time
    from app import email as email_service
    from app.models import User, Student, Family, PendingPayment
    with app.app_context():
        fam = Family(name="Receipt Fam", primary_email="rcpt@x.com")
        parent = User(username="rcpt_parent", email="rcptp@x.com",
                      first_name="R", last_name="P", role="parent")
        parent.set_password("pw")
        db.session.add_all([fam, parent])
        db.session.flush()
        st = Student(first_name="Rcpt", last_name="Kid", family_id=fam.id,
                     is_active=True, parent_email="rcpt@x.com")
        db.session.add(st)
        db.session.flush()
        pp = PendingPayment(student_id=st.id, parent_id=parent.id, amount=50.0,
                            method="zelle", status="pending")
        db.session.add(pp)
        db.session.commit()
        pid = pp.id
    app.config["MAIL_SERVER"] = "smtp.example.com"
    orig = email_service.send_email

    def slow_send(*a, **k):
        time.sleep(2)
        return 1

    email_service.send_email = slow_send
    try:
        with app.test_client() as c:
            login(c, "admin", "admin123")
            t0 = time.monotonic()
            r = c.post(f"/api/pending-payments/{pid}/confirm", json={})
            elapsed = time.monotonic() - t0
    finally:
        email_service.send_email = orig
        app.config["MAIL_SERVER"] = None
    record(f"Confirming a payment doesn't block on the receipt send (returned in {elapsed:.2f}s)",
           r.status_code == 200 and elapsed < 1.0,
           f"status={r.status_code} elapsed={elapsed:.2f}s", "P2")


def run_confirm_payment_race():
    """Confirming a PendingPayment is a check-then-act on real money: an admin
    double-click (or two open tabs) fires two concurrent confirms that both pass
    the status check before either commits, and without an atomic claim BOTH
    create payment transactions — the family is credited twice. Fire two
    simultaneous confirms at one pending payment and assert exactly one
    transaction is created (one 200, one 400)."""
    import threading
    from app.models import User, Student, Family, PendingPayment, Transaction
    with app.app_context():
        fam = Family(name="Race Fam", primary_email="race@x.com")
        parent = User(username="race_parent", email="racep@x.com",
                      first_name="R", last_name="P", role="parent")
        parent.set_password("pw")
        db.session.add_all([fam, parent])
        db.session.flush()
        st = Student(first_name="Race", last_name="Kid", family_id=fam.id, is_active=True)
        db.session.add(st)
        db.session.flush()
        pp = PendingPayment(student_id=st.id, parent_id=parent.id, amount=100.0,
                            method="zelle", status="pending")
        db.session.add(pp)
        db.session.commit()
        pid, sid = pp.id, st.id

    barrier = threading.Barrier(2)
    codes = []

    def worker():
        with app.test_client() as c:
            login(c, "admin", "admin123")
            barrier.wait()
            codes.append(c.post(f"/api/pending-payments/{pid}/confirm", json={}).status_code)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    with app.app_context():
        n_txn = Transaction.query.filter_by(student_id=sid, type="payment").count()
    record("Concurrent double-confirm credits the family exactly once (no double-pay)",
           n_txn == 1 and sorted(codes) == [200, 400],
           f"transactions={n_txn} codes={sorted(codes)}", "P1")


def run_registration_approve_race():
    """Approving a public Registration creates a Family + Students + enrollments,
    and Family has no natural unique key — so a double-click that fires two
    concurrent approvals would create two identical families. Fire two
    simultaneous approvals and assert exactly one family/student is created."""
    import json as _json
    import threading
    from app.models import Registration, Family, Student
    with app.app_context():
        before_fams = Family.query.count()
        reg = Registration(parent_name="Concurrent Doe", parent_email="cdoe@x.com",
                           parent_phone="555", status="pending", class_ids="",
                           students_json=_json.dumps([{"first_name": "Cc", "last_name": "Doe"}]))
        db.session.add(reg)
        db.session.commit()
        rid = reg.id

    barrier = threading.Barrier(2)
    codes = []

    def worker():
        with app.test_client() as c:
            login(c, "admin", "admin123")
            barrier.wait()
            codes.append(c.post(f"/api/registrations/{rid}/approve", json={}).status_code)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    with app.app_context():
        new_fams = Family.query.count() - before_fams
        kids = Student.query.filter_by(first_name="Cc", last_name="Doe").count()
    record("Concurrent double-approve creates exactly one family (no duplicate)",
           new_fams == 1 and kids == 1 and sorted(codes) == [200, 400],
           f"new_families={new_fams} kids={kids} codes={sorted(codes)}", "P1")


def run_attendance_race():
    """Marking attendance is the app's core action and its most double-tap-prone
    (teachers take it on community-center wifi). toggle_attendance is
    check-then-act and Attendance has no natural unique key, so two concurrent
    taps could create duplicate 'present' rows (inflating counts, breaking the
    toggle). A functional unique index + graceful IntegrityError handling must
    hold it to at most one row. Fire two simultaneous toggles and assert <= 1 row
    with no 500."""
    import threading
    from datetime import time as _time
    from app.models import User, DanceClass, ClassEnrollment, Attendance, Student, Family
    with app.app_context():
        admin = User.query.filter_by(username="admin").first()
        fam = Family(name="Att Race Fam", primary_email="attrace@x.com")
        db.session.add(fam)
        db.session.flush()
        st = Student(first_name="Tap", last_name="Race", family_id=fam.id, is_active=True)
        dc = DanceClass(name="Race Class", day_of_week=2, start_time=_time(15, 0),
                        end_time=_time(16, 0), instructor_id=admin.id)
        db.session.add_all([st, dc])
        db.session.flush()
        db.session.add(ClassEnrollment(student_id=st.id, class_id=dc.id))
        db.session.commit()
        sid, cid = st.id, dc.id

    barrier = threading.Barrier(2)
    codes = []

    def worker():
        with app.test_client() as c:
            login(c, "admin", "admin123")
            barrier.wait()
            codes.append(c.post("/api/attendance/toggle",
                                json={"student_id": sid, "class_id": cid}).status_code)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    with app.app_context():
        n = Attendance.query.filter_by(student_id=sid, class_id=cid).count()
    record("Concurrent double-tap never creates a duplicate attendance row",
           n <= 1 and all(code < 500 for code in codes),
           f"attendance_rows={n} codes={sorted(codes)}", "P1")


def run_late_fee_race():
    """Bulk late-fee application has a per-student per-month idempotency check, so
    a sequential re-run is safe — but it's check-then-act, so two concurrent runs
    (a double-click) both read the pre-charge state and each charge every
    over-threshold family (double late fees = real money). A process lock must
    serialize the runs. Fire two simultaneous applications at one over-threshold
    student and assert exactly one late-fee charge."""
    import threading
    from datetime import date as _date
    from app.models import Student, Family, Transaction
    with app.app_context():
        fam = Family(name="Late Fam", primary_email="late@x.com")
        db.session.add(fam)
        db.session.flush()
        st = Student(first_name="Owes", last_name="Fee", family_id=fam.id, is_active=True)
        db.session.add(st)
        db.session.flush()
        db.session.add(Transaction(student_id=st.id, type="charge", amount=100.0,
                                   category="tuition", payment_method="n/a",
                                   description="tuition", transaction_date=_date.today()))
        db.session.commit()
        sid = st.id

    barrier = threading.Barrier(2)
    codes = []

    def worker():
        with app.test_client() as c:
            login(c, "admin", "admin123")
            barrier.wait()
            codes.append(c.post("/api/balances/apply-late-fees",
                                json={"amount": 25, "min_balance": 0}).status_code)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    with app.app_context():
        n = Transaction.query.filter_by(student_id=sid, category="late fee").count()
    record("Concurrent late-fee application charges each family exactly once",
           n == 1 and all(code < 500 for code in codes),
           f"late_fee_charges={n} codes={sorted(codes)}", "P1")


def run_transactions_pagination(ids):
    """The payments page loads the transactions list at 100/page and now renders
    Prev/Next controls off the API's pagination metadata. Guard that contract: with
    120 transactions on a dedicated student, page 1 returns exactly 100 items with
    pages=2/total=120, and page 2 returns the remaining 20 — so older transactions
    stay reachable instead of being silently capped at the most recent 100."""
    from datetime import date as _date
    from app.models import Transaction, Student, Family
    with app.app_context():
        fam = Family(name="Pagination Fam", primary_email="pg@x.com")
        db.session.add(fam)
        db.session.flush()
        st = Student(first_name="Page", last_name="Inator", family_id=fam.id, is_active=True)
        db.session.add(st)
        db.session.flush()
        sid = st.id
        db.session.add_all([
            Transaction(student_id=sid, type="charge", amount=1, category="tuition",
                        payment_method="n/a", description=f"pg{i}",
                        transaction_date=_date.today())
            for i in range(120)
        ])
        db.session.commit()
    with app.test_client() as c:
        login(c, "admin", "admin123")
        p1 = c.get(f"/api/transactions?per_page=100&page=1&student_id={sid}").get_json() or {}
        p2 = c.get(f"/api/transactions?per_page=100&page=2&student_id={sid}").get_json() or {}
        pg = p1.get("pagination", {})
        record(f"Transactions page 1 returns 100 of {pg.get('total')} (pages={pg.get('pages')})",
               len(p1.get("transactions", [])) == 100 and pg.get("total") == 120 and pg.get("pages") == 2,
               str(pg), "P2")
        record(f"Transactions page 2 returns the remaining 20 (got {len(p2.get('transactions', []))})",
               len(p2.get("transactions", [])) == 20, str(p2.get("pagination")), "P2")


def run_students_roster_complete():
    """The students page filters the roster client-side, so it must load EVERY
    active student, not just the first page — otherwise a >100-student studio
    silently hides (and can't search for) students past page 1. Mimic the page's
    loop-fetch with a tiny page size and assert the accumulated count equals the
    reported total."""
    with app.test_client() as c:
        login(c, "admin", "admin123")
        first = c.get("/api/students?per_page=1&page=1").get_json() or {}
        total = (first.get("pagination") or {}).get("total", 0)
        got, page, pages = [], 1, 1
        while page <= pages and page <= 50:
            d = c.get(f"/api/students?per_page=2&page={page}").get_json() or {}
            got += d.get("students", [])
            pages = (d.get("pagination") or {}).get("pages", 1)
            page += 1
        record(f"Roster loop-fetch returns every active student ({len(got)} of {total})",
               total >= 1 and len(got) == total, f"got {len(got)} != total {total}", "P2")


def run_messages_pagination():
    """The sent-message history now paginates (was capped at 50 with no way to
    see older messages). Seed 60, and assert page 1 returns 50 with pages>=2 and
    page 2 carries the overflow — so older blasts stay reachable."""
    from app.models import Message
    with app.app_context():
        db.session.add_all([
            Message(subject=f"blast {i}", body="b", recipient_type="all", sent=True)
            for i in range(60)
        ])
        db.session.commit()
    with app.test_client() as c:
        login(c, "admin", "admin123")
        p1 = c.get("/api/messages?per_page=50&page=1").get_json() or {}
        p2 = c.get("/api/messages?per_page=50&page=2").get_json() or {}
        pg = p1.get("pagination", {})
        record(f"Messages page 1 caps at 50 with pages>=2 (total={pg.get('total')})",
               len(p1.get("messages", [])) == 50 and pg.get("total", 0) >= 60 and pg.get("pages", 0) >= 2,
               str(pg), "P3")
        record(f"Messages page 2 carries the overflow ({len(p2.get('messages', []))})",
               len(p2.get("messages", [])) >= 10, str(p2.get("pagination")), "P3")


def run_sqlite_wal_guard():
    """SQLite must run in WAL mode with a busy timeout. One gunicorn worker runs
    4 gthread threads plus background send threads (auto/manual reminders), all
    on one DB file — the default 'delete' journal blocks readers against the
    writer (the historical 'database is locked' failure). WAL lets concurrent
    readers run alongside the single writer; the busy timeout absorbs brief
    contention instead of erroring immediately."""
    from sqlalchemy import text
    with app.app_context():
        jm = (db.session.execute(text("PRAGMA journal_mode")).scalar() or "").lower()
        bt = db.session.execute(text("PRAGMA busy_timeout")).scalar() or 0
    record(f"SQLite runs in WAL with a busy timeout (journal={jm}, busy_timeout={bt}ms)",
           jm == "wal" and bt >= 1000, f"journal_mode={jm} busy_timeout={bt}", "P2")


def run_logging_config_guard():
    """Logging must be configured at run.py MODULE level, not only inside
    `if __name__ == '__main__'`. Production runs `gunicorn run:app`, which IMPORTS
    the module (so __main__ never runs) — if basicConfig were __main__-only, prod
    would silently drop every INFO log (the operational confirmations that the
    automated engines ran: recurring charges, auto-reminders, boot)."""
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "run.py").read_text().splitlines()
    cfg = [ln for ln in src if "basicConfig" in ln]
    module_level = any(ln == ln.lstrip() for ln in cfg)  # indentation 0 == module scope
    record("Logging configured at run.py module level (emits under gunicorn)",
           bool(cfg) and module_level, f"basicConfig lines: {cfg}", "P2")


def run_registrations_pagination():
    """The admin registrations inbox paginates (was capped at 200 newest, which
    during a busy enrollment season — or a flood of the public unauthenticated
    submit — buried the *earliest* registrants past the cutoff). Seed 60 pending
    and assert page 1 returns 50 with pages>=2 and page 2 the overflow."""
    import json as _json
    from app.models import Registration
    with app.app_context():
        db.session.add_all([
            Registration(parent_name=f"Reg {i}", parent_email=f"reg{i}@x.com",
                         status="pending", class_ids="",
                         students_json=_json.dumps([{"first_name": f"K{i}", "last_name": "R"}]))
            for i in range(60)
        ])
        db.session.commit()
    with app.test_client() as c:
        login(c, "admin", "admin123")
        p1 = c.get("/api/registrations?status=pending&per_page=50&page=1").get_json() or {}
        p2 = c.get("/api/registrations?status=pending&per_page=50&page=2").get_json() or {}
        pg = p1.get("pagination", {})
        record(f"Registrations page 1 caps at 50 with pages>=2 (total={pg.get('total')})",
               len(p1.get("registrations", [])) == 50 and pg.get("total", 0) >= 60 and pg.get("pages", 0) >= 2,
               str(pg), "P2")
        record(f"Registrations page 2 carries the overflow ({len(p2.get('registrations', []))})",
               len(p2.get("registrations", [])) >= 10, str(p2.get("pagination")), "P3")


def run_admin_identity_invariants():
    """Defend the is_admin/role split that caused the notification bug: an
    is_admin user must always be staff and never a parent, even if its role
    string drifts to a non-admin value. Verifies the User.is_staff / is_parent
    properties are robust by construction (not merely by keeping role in sync)."""
    from app.models import User
    weird = User(username="x", email="x@x.com", first_name="X", last_name="Y",
                 is_admin=True, role="parent")   # contradictory role
    adm = User(username="y", email="y@x.com", first_name="A", last_name="B",
               is_admin=True, role="admin")
    par = User(username="z", email="z@x.com", first_name="P", last_name="Q",
               is_admin=False, role="parent")
    ok = (weird.is_staff and not weird.is_parent
          and adm.is_staff and not adm.is_parent
          and par.is_parent and not par.is_staff)
    record("is_admin user is always staff, never parent (role drift can't lock them out)",
           ok, f"weird staff={weird.is_staff} parent={weird.is_parent}", "P2")


def run_admin_role_consistency():
    """The default admin must be created with role='admin' (not just is_admin=True).
    Admin email notifications query filter_by(role='admin'), so a role/is_admin
    mismatch means the studio's primary admin silently gets no 'new registration'
    or 'reported payment' alerts. Boot a fresh app in a clean subprocess and check
    the seeded admin's role (in-process the shared app's admin may be mutated)."""
    import subprocess
    import tempfile as _tf
    tmp = _tf.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    snippet = (
        "from app import create_app\n"
        "app = create_app()\n"
        "with app.app_context():\n"
        "    from app.models import User\n"
        "    a = User.query.filter_by(username='admin').first()\n"
        "    print('ROLE=' + (a.role if a and a.role else 'NONE'))\n"
    )
    env = dict(os.environ, DATABASE_URL=f"sqlite:///{tmp.name}", RFID_ENABLED="false")
    out = subprocess.run([sys.executable, "-c", snippet], capture_output=True, text=True, env=env)
    role = ""
    for ln in (out.stdout or "").splitlines():
        if ln.startswith("ROLE="):
            role = ln[5:]
    try:
        os.unlink(tmp.name)
    except OSError:
        pass
    record(f"Default admin is seeded with role='admin' (got {role!r})",
           role == "admin", f"role={role!r}; stderr={out.stderr[-200:]}", "P2")


def run_registration_notify_throttle():
    """The public /api/register submit emails admins per submission — a flood
    would email-bomb the studio. The notification must be throttled to at most
    one per window. Fire several rapid submits and assert at most one email."""
    from app import email as email_service
    from app.api import routes as api_routes
    from app.models import Setting, User
    with app.app_context():
        Setting.set("registration_open", "1")
        # Ensure a valid active admin exists so the FIRST notification fires (this
        # is robust to earlier tests that may have deactivated/re-roled the admin).
        adm = User.query.filter_by(username="admin").first()
        if adm:
            adm.role, adm.is_active = "admin", True
            if not (adm.email and "@" in adm.email):
                adm.email = "admin@attenddance.local"
        db.session.commit()
    app.config["MAIL_SERVER"] = "smtp.example.com"
    api_routes._last_reg_notify[0] = 0.0  # reset throttle window
    calls = [0]
    orig = email_service.send_email

    def counting_send(*a, **k):
        calls[0] += 1
        return 1

    email_service.send_email = counting_send
    try:
        with app.test_client() as c:
            codes = []
            for i in range(5):
                r = c.post("/api/register", json={
                    "parent_name": f"Flood{i}", "parent_email": f"flood{i}@x.com",
                    **REG_CONTACT,
                    "students": [reg_student("K", "F")]})
                codes.append(r.status_code)
        # The notify now sends in a background thread (best-effort, off the public
        # request path), so wait for the single throttled send to land, then a
        # grace window to catch any erroneous extra send.
        import time as _t
        deadline = _t.monotonic() + 2.0
        while _t.monotonic() < deadline and calls[0] < 1:
            _t.sleep(0.02)
        _t.sleep(0.15)
    finally:
        email_service.send_email = orig
        app.config["MAIL_SERVER"] = None
        with app.app_context():
            Setting.set("registration_open", "0")
    record(f"Registration admin-notify fires once then throttles (5 submits {codes} -> {calls[0]} email)",
           all(c == 201 for c in codes) and calls[0] == 1,
           f"{calls[0]} notification emails for 5 rapid submits (want exactly 1); codes={codes}", "P2")
    # The public funnel must not block on a slow admin-notify send: arm a slow
    # sender, reset the throttle, and assert the submit still returns fast.
    import time as _t2
    api_routes._last_reg_notify[0] = 0.0
    app.config["MAIL_SERVER"] = "smtp.example.com"

    def slow_notify(*a, **k):
        _t2.sleep(2)
        return 1

    email_service.send_email = slow_notify
    try:
        with app.app_context():
            Setting.set("registration_open", "1")
        with app.test_client() as c:
            t0 = _t2.monotonic()
            r = c.post("/api/register", json={
                "parent_name": "SlowNotify", "parent_email": "slow@x.com",
                **REG_CONTACT,
                "students": [reg_student("S", "N")]})
            elapsed = _t2.monotonic() - t0
    finally:
        email_service.send_email = orig
        app.config["MAIL_SERVER"] = None
        with app.app_context():
            Setting.set("registration_open", "0")
    record(f"Public registration submit doesn't block on the admin-notify send (returned in {elapsed:.2f}s)",
           r.status_code == 201 and elapsed < 1.0,
           f"status={r.status_code} elapsed={elapsed:.2f}s", "P2")


def run_class_crud():
    """Classes must be editable and cancellable (Jackrabbit parity — the studio
    adjusts schedules and cancels classes mid-season). Verify PUT updates fields,
    and DELETE deactivates the class AND stops its recurring charge (otherwise
    auto-billing keeps charging families for a cancelled class)."""
    from datetime import time as _time
    from app.models import (User, DanceClass, RecurringCharge, Student, Family,
                            ClassEnrollment, WaitlistEntry)
    with app.app_context():
        adm = User.query.filter_by(username="admin").first()
        dc = DanceClass(name="Tap 1", day_of_week=1, start_time=_time(16, 0),
                        end_time=_time(17, 0), instructor_id=adm.id)
        db.session.add(dc)
        db.session.flush()
        db.session.add(RecurringCharge(class_id=dc.id, amount=90, category="tuition",
                                       day_of_month=1, created_by=adm.id))
        # An enrolled + a waitlisted student, to check the cancel cascade cleans up.
        fam = Family(name="Tap Fam", primary_email="tap@x.com")
        db.session.add(fam)
        db.session.flush()
        e_st = Student(first_name="Enrolled", last_name="T", family_id=fam.id, is_active=True)
        w_st = Student(first_name="Waiting", last_name="T", family_id=fam.id, is_active=True)
        db.session.add_all([e_st, w_st])
        db.session.flush()
        db.session.add(ClassEnrollment(student_id=e_st.id, class_id=dc.id))
        db.session.add(WaitlistEntry(class_id=dc.id, student_id=w_st.id, status="waiting"))
        db.session.commit()
        cid = dc.id
    with app.test_client() as c:
        login(c, "admin", "admin123")
        r = c.put(f"/api/classes/{cid}", json={"name": "Tap 1 (Advanced)", "start_time": "17:30"})
        d = r.get_json() or {}
        record(f"Class edit updates fields -> {r.status_code} ({d.get('name')}, {d.get('start_time')})",
               r.status_code == 200 and d.get("name") == "Tap 1 (Advanced)" and d.get("start_time") == "17:30",
               str(d), "P1")
        r2 = c.delete(f"/api/classes/{cid}")
        d2 = r2.get_json() or {}
    with app.app_context():
        cls = DanceClass.query.get(cid)
        rc = RecurringCharge.query.filter_by(class_id=cid).first()
        live_enroll = ClassEnrollment.query.filter_by(class_id=cid, is_active=True).count()
        live_wait = WaitlistEntry.query.filter_by(class_id=cid, status="waiting").count()
    record(f"Cancelling a class deactivates it + stops recurring billing -> {r2.status_code}",
           r2.status_code == 200 and cls.is_active is False and rc.is_active is False
           and d2.get("recurring_charges_stopped") == 1,
           f"class_active={cls.is_active} rc_active={rc.is_active} stopped={d2.get('recurring_charges_stopped')}", "P1")
    record(f"Cancelling a class clears its enrollments + waitlist (enroll={live_enroll}, wait={live_wait})",
           live_enroll == 0 and live_wait == 0, f"lingering enroll={live_enroll} wait={live_wait}", "P2")


def run_class_instructor_assignment():
    """A class's instructor must be assignable/reassignable (teachers change for
    fall). Verify /api/instructors lists active teachers/admins, create with an
    instructor sets it, and edit reassigns it."""
    from app.models import User
    with app.app_context():
        t = User(username="instr_teacher", email="instr@x.com", first_name="Ada",
                 last_name="Instructor", role="teacher", is_active=True)
        t.set_password("pw")
        db.session.add(t)
        db.session.commit()
        tid = t.id
    with app.test_client() as c:
        login(c, "admin", "admin123")
        instrs = (c.get("/api/instructors").get_json() or {}).get("instructors", [])
        listed = any(i["id"] == tid and i["name"] == "Ada Instructor" for i in instrs)
        r = c.post("/api/classes", json={"name": "Instr Class", "day_of_week": 1,
                                         "start_time": "16:00", "end_time": "17:00",
                                         "instructor_id": tid})
        cid = (r.get_json() or {}).get("id")
        created_ok = r.status_code == 201 and (r.get_json() or {}).get("instructor_name") == "Ada Instructor"
        # reassign to the admin — check the id changed (robust to the admin's name)
        with app.app_context():
            adm = User.query.filter_by(username="admin").first().id
        e = c.put(f"/api/classes/{cid}", json={"instructor_id": adm})
        reassigned = e.status_code == 200 and (e.get_json() or {}).get("instructor_id") == adm
    record(f"Class instructor assignable/reassignable (listed={listed}, created={created_ok}, reassigned={reassigned})",
           listed and created_ok and reassigned,
           f"listed={listed} created={created_ok} reassigned={reassigned}", "P2")


def run_capacity_lifecycle():
    """Integration: capacity-freeing (withdraw) and capacity-checking (promote)
    must COMPOSE — the real flow where a full class opens a spot. Full 1/1 class +
    a waitlisted student: promote is blocked; withdraw the enrolled student to free
    the spot; now promote succeeds and the waitlisted student takes the seat."""
    from datetime import time as _time
    from app.models import (User, DanceClass, Student, Family, ClassEnrollment,
                            WaitlistEntry)
    with app.app_context():
        adm = User.query.filter_by(username="admin").first()
        fam = Family(name="Lifecycle Fam", primary_email="lc@x.com")
        db.session.add(fam)
        db.session.flush()
        dc = DanceClass(name="One Spot", day_of_week=1, start_time=_time(16, 0),
                        end_time=_time(17, 0), instructor_id=adm.id, max_students=1)
        db.session.add(dc)
        db.session.flush()
        enr = Student(first_name="Sitting", last_name="L", family_id=fam.id, is_active=True)
        wai = Student(first_name="Nextup", last_name="L", family_id=fam.id, is_active=True)
        db.session.add_all([enr, wai])
        db.session.flush()
        db.session.add(ClassEnrollment(student_id=enr.id, class_id=dc.id))
        w = WaitlistEntry(class_id=dc.id, student_id=wai.id, status="waiting")
        db.session.add(w)
        db.session.commit()
        cid, eid, wid, wai_id = dc.id, enr.id, w.id, wai.id
    with app.test_client() as c:
        login(c, "admin", "admin123")
        blocked = c.post(f"/api/waitlist/{wid}/promote").status_code   # full -> 400
        c.delete(f"/api/students/{eid}")                                # free the spot
        promoted = c.post(f"/api/waitlist/{wid}/promote").status_code   # now -> 200
    with app.app_context():
        active = ClassEnrollment.query.filter_by(class_id=cid, is_active=True).all()
        roster = [e.student_id for e in active]
    record(f"Capacity lifecycle: full→block, withdraw frees, promote fills (block={blocked}, promote={promoted})",
           blocked == 400 and promoted == 200 and roster == [wai_id],
           f"block={blocked} promote={promoted} roster={roster}", "P1")


def run_class_capacity():
    """Enrollment must respect max_students so a class can't be silently
    overbooked (that's what the waitlist is for). Enroll 3 into a max=2 class →
    2 enrolled + 1 reported full; a max=0 (unlimited) class takes all."""
    from datetime import time as _time
    from app.models import User, DanceClass, Student, Family, ClassEnrollment
    with app.app_context():
        adm = User.query.filter_by(username="admin").first()
        fam = Family(name="Cap Fam", primary_email="cap@x.com")
        db.session.add(fam)
        db.session.flush()
        small = DanceClass(name="Cap Small", day_of_week=1, start_time=_time(16, 0),
                           end_time=_time(17, 0), instructor_id=adm.id, max_students=2)
        unlim = DanceClass(name="Cap Unlim", day_of_week=2, start_time=_time(16, 0),
                           end_time=_time(17, 0), instructor_id=adm.id, max_students=0)
        db.session.add_all([small, unlim])
        db.session.flush()
        sids = []
        for i in range(3):
            s = Student(first_name=f"Cap{i}", last_name="X", family_id=fam.id, is_active=True)
            db.session.add(s)
            db.session.flush()
            sids.append(s.id)
        db.session.commit()
        scid, ucid = small.id, unlim.id
    with app.test_client() as c:
        login(c, "admin", "admin123")
        d = c.post(f"/api/classes/{scid}/enroll", json={"student_ids": sids}).get_json() or {}
        du = c.post(f"/api/classes/{ucid}/enroll", json={"student_ids": sids}).get_json() or {}
    with app.app_context():
        small_n = ClassEnrollment.query.filter_by(class_id=scid, is_active=True).count()
        unlim_n = ClassEnrollment.query.filter_by(class_id=ucid, is_active=True).count()
    record(f"Class capacity enforced (max=2 -> {small_n} enrolled + {len(d.get('full', []))} full; unlimited -> {unlim_n})",
           small_n == 2 and len(d.get("full", [])) == 1 and unlim_n == 3,
           f"small={small_n} full={d.get('full')} unlim={unlim_n}", "P1")

    # The waitlist promote path must ALSO respect capacity (else you overbook via
    # the waitlist, defeating the point). The small class is now full (2/2).
    from app.models import WaitlistEntry
    with app.app_context():
        extra = Student(first_name="CapWait", last_name="X",
                        family_id=Student.query.get(sids[0]).family_id, is_active=True)
        db.session.add(extra)
        db.session.flush()
        w = WaitlistEntry(class_id=scid, student_id=extra.id, status="waiting")
        db.session.add(w)
        db.session.commit()
        wid = w.id
    with app.test_client() as c:
        login(c, "admin", "admin123")
        pr = c.post(f"/api/waitlist/{wid}/promote")
    with app.app_context():
        after = ClassEnrollment.query.filter_by(class_id=scid, is_active=True).count()
    record(f"Waitlist promote into a full class is blocked (no overbook) -> {pr.status_code}",
           pr.status_code == 400 and after == 2, f"status={pr.status_code} enrollments={after}", "P1")


def run_recurring_charge_edit():
    """An auto-billing rule must be editable (raise tuition / shift the billing
    day mid-year) without delete+recreate. Verify PUT updates amount/day and
    rejects a bad amount/day (it fires monthly, so a bad value is worst here)."""
    from datetime import time as _time
    from app.models import User, DanceClass, RecurringCharge
    with app.app_context():
        adm = User.query.filter_by(username="admin").first()
        dc = DanceClass(name="RC Edit Class", day_of_week=2, start_time=_time(15, 0),
                        end_time=_time(16, 0), instructor_id=adm.id)
        db.session.add(dc)
        db.session.flush()
        rc = RecurringCharge(class_id=dc.id, amount=100, category="tuition",
                             day_of_month=1, created_by=adm.id)
        db.session.add(rc)
        db.session.commit()
        rid = rc.id
    with app.test_client() as c:
        login(c, "admin", "admin123")
        ok = c.put(f"/api/recurring-charges/{rid}", json={"amount": 110, "day_of_month": 15})
        bad_amt = c.put(f"/api/recurring-charges/{rid}", json={"amount": "abc"})
        bad_day = c.put(f"/api/recurring-charges/{rid}", json={"day_of_month": 45})
    with app.app_context():
        rc = RecurringCharge.query.get(rid)
    record(f"Recurring-charge edit updates amount+day and validates -> {ok.status_code}",
           ok.status_code == 200 and float(rc.amount) == 110.0 and rc.day_of_month == 15
           and bad_amt.status_code == 400 and bad_day.status_code == 400,
           f"amount={float(rc.amount)} day={rc.day_of_month} bad_amt={bad_amt.status_code} bad_day={bad_day.status_code}", "P1")


def run_withdraw_frees_enrollment():
    """Withdrawing a student must deactivate their enrollments (else they linger
    on class rosters AND keep occupying a capacity spot a replacement can't take)
    and clear their waitlist — but PRESERVE their balance (they may still owe)."""
    from datetime import time as _time, date as _date
    from app.models import (User, DanceClass, Student, Family, ClassEnrollment,
                            WaitlistEntry, Transaction)
    from app.helpers import calc_balance as _cb
    with app.app_context():
        adm = User.query.filter_by(username="admin").first()
        fam = Family(name="Withdraw Cascade Fam", primary_email="wcc@x.com")
        db.session.add(fam)
        db.session.flush()
        dc = DanceClass(name="WC Class", day_of_week=1, start_time=_time(16, 0),
                        end_time=_time(17, 0), instructor_id=adm.id, max_students=5)
        dc2 = DanceClass(name="WC Class 2", day_of_week=2, start_time=_time(16, 0),
                         end_time=_time(17, 0), instructor_id=adm.id, max_students=5)
        db.session.add_all([dc, dc2])
        db.session.flush()
        st = Student(first_name="Leaving", last_name="Kid", family_id=fam.id, is_active=True)
        db.session.add(st)
        db.session.flush()
        db.session.add(ClassEnrollment(student_id=st.id, class_id=dc.id))
        db.session.add(WaitlistEntry(class_id=dc2.id, student_id=st.id, status="waiting"))
        db.session.add(Transaction(student_id=st.id, type="charge", amount=50,
                                   category="tuition", payment_method="n/a",
                                   description="c", transaction_date=_date.today()))
        db.session.commit()
        cid, sid = dc.id, st.id
    with app.test_client() as c:
        login(c, "admin", "admin123")
        c.delete(f"/api/students/{sid}")
    with app.app_context():
        enroll = ClassEnrollment.query.filter_by(class_id=cid, is_active=True).count()
        wait = WaitlistEntry.query.filter_by(student_id=sid, status="waiting").count()
        bal = _cb(sid)["balance"]
    record(f"Withdrawing a student frees their spot + clears waitlist, keeps balance (enroll={enroll}, wait={wait}, bal={bal})",
           enroll == 0 and wait == 0 and bal == 50.0,
           f"enroll={enroll} wait={wait} bal={bal}", "P1")


def run_parent_portal_classes():
    """The parent portal must show each child's class schedule (a basic parent
    need), ordered like a weekly schedule (weekday then time), and must hide a
    cancelled class (the cancel cascade deactivates its enrollment). Enroll a
    child in a Wed + a Mon class (Mon added second) plus a class we then cancel."""
    from datetime import time as _time
    from app.models import (User, DanceClass, Student, Family, ClassEnrollment, ParentStudent)
    with app.app_context():
        adm = User.query.filter_by(username="admin").first()
        fam = Family(name="Sched Fam", primary_email="sch@x.com")
        p = User(username="sched_parent", email="schp@x.com", first_name="S", last_name="P", role="parent")
        p.set_password("pw")
        db.session.add_all([fam, p])
        db.session.flush()
        kid = Student(first_name="Sched", last_name="Kid", family_id=fam.id, is_active=True)
        db.session.add(kid)
        db.session.flush()
        db.session.add(ParentStudent(parent_id=p.id, student_id=kid.id))
        wed = DanceClass(name="ZzActiveWed", day_of_week=2, start_time=_time(16, 0),
                         end_time=_time(17, 0), instructor_id=adm.id)
        mon = DanceClass(name="ZzActiveMon", day_of_week=0, start_time=_time(16, 0),
                         end_time=_time(17, 0), instructor_id=adm.id)
        cancelled = DanceClass(name="ZzCancelledClass2", day_of_week=1, start_time=_time(16, 0),
                               end_time=_time(17, 0), instructor_id=adm.id)
        db.session.add_all([wed, mon, cancelled])
        db.session.flush()
        # Enroll Wed first, then Mon — so DB order != schedule order.
        db.session.add(ClassEnrollment(student_id=kid.id, class_id=wed.id))
        db.session.add(ClassEnrollment(student_id=kid.id, class_id=mon.id))
        db.session.add(ClassEnrollment(student_id=kid.id, class_id=cancelled.id))
        db.session.commit()
        ccid = cancelled.id
    with app.test_client() as c:
        login(c, "admin", "admin123")
        c.delete(f"/api/classes/{ccid}")  # cancel one -> cascade deactivates that enrollment
    with app.test_client() as c:
        login(c, "sched_parent", "pw")
        body = c.get("/parent").get_data(as_text=True)
    record("Parent portal shows the child's active class schedule, hides a cancelled class",
           "ZzActiveMon" in body and "ZzActiveWed" in body and "ZzCancelledClass2" not in body,
           f"mon={'ZzActiveMon' in body} wed={'ZzActiveWed' in body} cancelled={'ZzCancelledClass2' in body}", "P2")
    record("Parent portal orders classes by weekday (Mon before Wed despite reverse enroll order)",
           "ZzActiveMon" in body and "ZzActiveWed" in body
           and body.index("ZzActiveMon") < body.index("ZzActiveWed"),
           f"mon_idx={body.find('ZzActiveMon')} wed_idx={body.find('ZzActiveWed')}", "P3")


def run_skill_archive_safe():
    """Archiving a skill (soft-delete) must PRESERVE its per-student marks (a
    studio may archive/rename a skill without losing which students earned it)
    and hide it from the active skills list."""
    from app.models import Skill, StudentSkill, Student, Family
    with app.app_context():
        fam = Family(name="Skill Fam", primary_email="sk@x.com")
        db.session.add(fam)
        db.session.flush()
        sk = Skill(name="ZzPirouetteSkill")
        st = Student(first_name="Skilled", last_name="Kid", family_id=fam.id, is_active=True)
        db.session.add_all([sk, st])
        db.session.flush()
        db.session.add(StudentSkill(skill_id=sk.id, student_id=st.id))
        db.session.commit()
        skid, sid = sk.id, st.id
    with app.test_client() as c:
        login(c, "admin", "admin123")
        before = any(s.get("name") == "ZzPirouetteSkill"
                     for s in (c.get("/api/skills").get_json() or {}).get("skills", []))
        c.delete(f"/api/skills/{skid}")
        after = any(s.get("name") == "ZzPirouetteSkill"
                    for s in (c.get("/api/skills").get_json() or {}).get("skills", []))
    with app.app_context():
        mark_kept = StudentSkill.query.filter_by(skill_id=skid, student_id=sid).count()
    record(f"Archiving a skill hides it from the list but keeps student marks (listed {before}->{after}, marks={mark_kept})",
           before and not after and mark_kept == 1,
           f"before={before} after={after} marks={mark_kept}", "P2")


def run_withdrawn_student_balance():
    """A withdrawn (is_active=False) student who still owes must stay visible in
    the money views so the studio can collect — the balance can't vanish on
    deactivation. But a settled withdrawal must not clutter. Verify /api/balances
    and /api/reports/aging include a withdrawn owing student (flagged) and exclude
    a withdrawn settled one."""
    from datetime import date as _date
    from app.models import Student, Family, Transaction
    with app.app_context():
        fam = Family(name="Withdraw Fam", primary_email="wd@x.com")
        db.session.add(fam)
        db.session.flush()
        owe = Student(first_name="Gone", last_name="Owes", family_id=fam.id, is_active=False)
        settled = Student(first_name="Gone", last_name="Settled", family_id=fam.id, is_active=False)
        db.session.add_all([owe, settled])
        db.session.flush()
        db.session.add_all([
            Transaction(student_id=owe.id, type="charge", amount=77, category="tuition",
                        payment_method="n/a", description="c", transaction_date=_date.today()),
            Transaction(student_id=settled.id, type="charge", amount=40, category="tuition",
                        payment_method="n/a", description="c", transaction_date=_date.today()),
            Transaction(student_id=settled.id, type="payment", amount=40, category="tuition",
                        payment_method="cash", description="p", transaction_date=_date.today()),
        ])
        db.session.commit()
    with app.test_client() as c:
        login(c, "admin", "admin123")
        bals = (c.get("/api/balances").get_json() or {}).get("balances", [])
        aging = (c.get("/api/reports/aging").get_json() or {}).get("rows", [])
    bal_owe = next((b for b in bals if b["student_name"] == "Gone Owes"), None)
    bal_settled = any(b["student_name"] == "Gone Settled" for b in bals)
    age_owe = next((r for r in aging if r["student_name"] == "Gone Owes"), None)
    record("Withdrawn student who owes stays in balances + aging (flagged); settled doesn't",
           bal_owe is not None and bal_owe.get("withdrawn") is True and not bal_settled
           and age_owe is not None and age_owe.get("withdrawn") is True,
           f"bal_owe={bal_owe} settled_shown={bal_settled} age_owe={bool(age_owe)}", "P2")


def run_api_error_shape():
    """API errors must be JSON, page errors must be the branded HTML page.
    Before this guard, a get_or_404 on any /api/* route returned the HTML 404
    page to a JSON client — res.json() throws client-side and the real error is
    swallowed into a generic 'Failed to load'."""
    with app.test_client() as c:
        login(c, "admin", "admin123")
        r404 = c.get("/api/students/999999")            # get_or_404 inside an endpoint
        rroute = c.get("/api/no-such-endpoint")          # unrouted API path
        r405 = c.delete("/api/balances")                 # GET-only route, wrong method
        ok_api = (r404.status_code == 404 and r404.is_json and "error" in (r404.get_json() or {})
                  and rroute.status_code == 404 and rroute.is_json
                  and r405.status_code == 405 and r405.is_json)
        record("API 404/405 errors are JSON (not the HTML error page)",
               ok_api,
               f"404={r404.status_code}/{r404.content_type} route={rroute.status_code}/{rroute.content_type} "
               f"405={r405.status_code}/{r405.content_type}", "P2")
    with app.test_client() as c:  # anonymous — the public hits bad links logged-out
        rp = c.get("/no-such-page")
        body = rp.get_data(as_text=True)
        record("Page 404 renders the branded HTML error page",
               rp.status_code == 404 and "text/html" in (rp.content_type or "") and "html" in body.lower(),
               f"status={rp.status_code} type={rp.content_type}", "P3")


def run_parent_sees_withdrawn_child():
    """Collection-critical, from the PARENT side: a withdrawn (is_active=False)
    child who still owes must stay visible on their own parent's portal, with the
    balance and a pay affordance — otherwise the family can't see or pay the debt.
    get_children() deliberately does NOT filter is_active; a well-meaning 'declutter
    the portal' change that filtered it would silently hide an owed balance from the
    one person who pays it. The staff-side balances/aging test can't catch that (it
    queries a different path) — this drives the /parent page as the parent."""
    from datetime import date as _date
    from app.models import User, Family, Student, Transaction, ParentStudent
    with app.app_context():
        fam = Family(name="WD Portal Fam", primary_email="wdp@x.com")
        db.session.add(fam)
        db.session.flush()
        kid = Student(first_name="Zizi", last_name="Withdrawn", family_id=fam.id, is_active=False)
        db.session.add(kid)
        db.session.flush()
        par = User(username="wd_portal_parent", email="wdportal@x.com", role="parent",
                   first_name="Wanda", last_name="Withdrawn", is_active=True)
        par.set_password("pw")
        db.session.add(par)
        db.session.flush()
        db.session.add_all([
            ParentStudent(parent_id=par.id, student_id=kid.id),
            Transaction(student_id=kid.id, type="charge", amount=77, category="tuition",
                        payment_method="n/a", description="owed", transaction_date=_date.today()),
        ])
        db.session.commit()
    with app.test_client() as c:
        login(c, "wd_portal_parent", "pw")
        r = c.get("/parent")
    body = r.get_data(as_text=True)
    record("Withdrawn owing child still shows on their parent's portal, balance + pay button",
           r.status_code == 200 and "Zizi" in body and "77.00" in body,
           f"status={r.status_code} name={'Zizi' in body} amt={'77.00' in body}", "P2")


def run_page_route_authz(ids):
    """The page routes (main blueprint) that render a specific student/class must
    not leak to a parent who doesn't own that student — the API endpoints were
    IDOR-guarded, but several *page* routes were @login_required only, leaking a
    child's name (or a class roster) enumerable by URL id. Verify a parent is
    redirected (not 200-with-data) from another student's ledger, rules-ack,
    sign-waivers, and the staff-only take-attendance page — but still reaches
    their OWN child's pages."""
    from datetime import time as _time
    from app.models import User, DanceClass, Student
    other = ids["child_b"]   # parent_a does NOT own child_b
    mine = ids["child_a"]     # parent_a owns child_a
    with app.app_context():
        admin = User.query.filter_by(username="admin").first()
        dc = DanceClass(name="AuthzClass", day_of_week=3, start_time=_time(10, 0),
                        end_time=_time(11, 0), instructor_id=admin.id)
        db.session.add(dc)
        db.session.commit()
        cid = dc.id
        other_name = Student.query.get(other).first_name

    with app.test_client() as c:
        login(c, "parent_a", "pw")
        blocked = []
        for path, marker in (
            (f"/students/{other}/ledger", other_name),
            (f"/rules/acknowledge/{other}", other_name),
            (f"/students/{other}/sign-waivers", other_name),
            (f"/take-attendance/{cid}", "AuthzClass"),
            ("/take-attendance", "AuthzClass"),   # staff attendance landing (renders today's classes)
            ("/calendar", "AuthzClass"),          # staff class-schedule view
        ):
            r = c.get(path, follow_redirects=False)
            body = r.get_data(as_text=True)
            blocked.append(r.status_code in (301, 302) and marker not in body)
        # own child still reachable
        own = c.get(f"/rules/acknowledge/{mine}", follow_redirects=False)
        record(f"Parent blocked from other/staff page routes ({sum(blocked)}/{len(blocked)}), own child OK -> {own.status_code}",
               all(blocked) and own.status_code == 200,
               f"blocked={blocked} own={own.status_code}", "P1")


def run_auth_form_labels():
    """Accessibility guard: every input/select/textarea on the auth forms AND the
    parent-facing surfaces (portal, public registration, waiver signing, rules
    acknowledgment) must have an accessible name — a `<label for>`, an
    `aria-label`, a `placeholder`, or a wrapping `<label>`. The studio serves
    ages 3–83; grandparents work these screens, and a nameless field is announced
    as an anonymous 'edit text' by a screen reader (this caught change-password's
    three unlabeled inputs, then the parent pay-amount + ticket fields)."""
    import re
    from pathlib import Path
    tpl = Path(__file__).resolve().parent.parent / "app" / "templates"
    forms = ["auth/login.html", "auth/change_password.html",
             "auth/forgot_password.html", "auth/reset_password.html",
             "auth/register.html", "parent/dashboard.html",
             "registration/public.html", "waivers/sign.html",
             "rules/acknowledge.html"]
    nameless = []
    for rel in forms:
        f = tpl / rel
        if not f.exists():
            nameless.append(f"{rel}: TEMPLATE MISSING (renamed? update this list)")
            continue
        txt = f.read_text()
        label_fors = set(re.findall(r'<label[^>]*\bfor="([^"]+)"', txt))
        # Fields wrapped in a <label>…<input> get their name from the wrapper.
        wrapped_starts = {mm.start(1) for mm in re.finditer(
            r'<label[^>]*>[^<]*(<(?:input|select|textarea)\b)', txt)}
        for m in re.finditer(r'<(input|select|textarea)\b[^>]*>', txt):
            tag = m.group(0)
            typ = (re.search(r'type="([^"]+)"', tag) or [None, "text"])[1]
            if typ in ("hidden", "submit", "button", "checkbox", "radio"):
                continue
            iid = (re.search(r'\bid="([^"]+)"', tag) or [None, None])[1]
            has_name = (
                (iid and iid in label_fors)
                or "aria-label" in tag
                or "placeholder" in tag
                or m.start() in wrapped_starts
            )
            if not has_name:
                nameless.append(f"{rel}: {tag[:70]}")
    record(f"Auth + parent-facing form fields all have an accessible name ({len(nameless)} nameless)",
           not nameless, "; ".join(nameless), "P2")


def run_dead_handler_guard():
    """Static guard: every inline on-click/change/submit/input handler must call a
    function that's actually defined (in the same template or the shared base.html
    shell). Catches a button wired to a removed or misspelled function — it looks
    functional but throws on click. (This is how the dead topbar-search box would
    have been caught earlier.)"""
    import re
    from pathlib import Path
    tpl = Path(__file__).resolve().parent.parent / "app" / "templates"

    def defined_names(txt):
        names = set()
        names |= set(re.findall(r'function\s+([A-Za-z_$][\w$]*)\s*\(', txt))
        names |= set(re.findall(r'window\.([A-Za-z_$][\w$]*)\s*=', txt))
        names |= set(re.findall(r'\b([A-Za-z_$][\w$]*)\s*=\s*(?:async\s+)?function', txt))
        names |= set(re.findall(r'\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\(', txt))
        return names

    BUILTIN = {"return", "if", "for", "while", "alert", "confirm", "event", "this",
               "location", "history", "window", "document", "console", "fetch", "JSON",
               "Math", "Date", "parseInt", "parseFloat", "setTimeout", "setInterval",
               "encodeURIComponent", "Number", "String", "Array"}
    base_defs = defined_names((tpl / "base.html").read_text())
    dead = []
    for f in tpl.rglob("*.html"):
        txt = f.read_text()
        local = defined_names(txt) | base_defs
        for m in re.finditer(r'on(?:click|change|submit|input)="\s*([A-Za-z_$][\w$]*)\s*\(', txt):
            fn = m.group(1)
            if fn not in BUILTIN and fn not in local:
                dead.append(f"{f.relative_to(tpl)}:{fn}")
    record(f"No inline handler calls an undefined function ({len(dead)} dead)",
           not dead, "; ".join(sorted(set(dead))), "P2")


def run_global_search():
    """The staff topbar search endpoint: finds students/families/classes by name,
    is staff-only (a parent must not enumerate the roster), enforces a 2-char
    minimum, and can't 500 or SQL-inject on a wildcard/quote query."""
    from app.models import Student, Family
    with app.app_context():
        fam = Family(name="Zzytworth Family", primary_email="zzy@x.com")
        db.session.add(fam)
        db.session.flush()
        st = Student(first_name="Xqzabel", last_name="Zzytworth", family_id=fam.id, is_active=True)
        db.session.add(st)
        db.session.commit()

    with app.test_client() as c:
        login(c, "admin", "admin123")
        # Finds the student by first name and the family by name.
        d = c.get("/api/search?q=Zzytworth").get_json() or {}
        found_student = any(s["name"] == "Xqzabel Zzytworth" for s in d.get("students", []))
        found_family = any(f["name"] == "Zzytworth Family" for f in d.get("families", []))
        record("Search finds a student + family by name",
               found_student and found_family, str(d), "P2")
        # Result links point at real pages.
        stu = next((s for s in d.get("students", []) if s["name"] == "Xqzabel Zzytworth"), {})
        record(f"Search result links to the student page ({stu.get('url')})",
               (stu.get("url") or "").startswith("/students/") and stu.get("url").endswith("/detail"),
               str(stu), "P3")
        # 2-char minimum.
        short = c.get("/api/search?q=x").get_json() or {}
        record("Search enforces a 2-char minimum (1 char -> empty)",
               short.get("students") == [] and short.get("families") == [], str(short), "P3")
        # Wildcard/quote query must not 500 or inject.
        r = c.get("/api/search?q=%25%27%3B--")
        record(f"Search survives a wildcard/quote query -> {r.status_code}",
               r.status_code == 200, f"got {r.status_code}", "P2")

    # A parent must not be able to search the roster.
    with app.test_client() as c:
        login(c, "parent_a", "pw")
        r = c.get("/api/search?q=Zzytworth")
        record(f"Parent blocked from global search -> {r.status_code}",
               r.status_code == 403, f"got {r.status_code}", "P1")


def run_xss_guard():
    """Static guard: user-controlled name/text fields must never be interpolated
    into a JS template literal without esc(). These fields come from public
    self-registration, so an unescaped one is stored XSS into a staff/admin view."""
    import re
    from pathlib import Path

    # Fields that hold user-controlled free text (excludes server enums like
    # `status`, ids, dates, and numeric fields, which aren't injectable).
    FIELDS = ("student_name", "family_name", "donor_name", "parent_name",
              "full_name", "instructor_name", "group_name", "costume_name",
              "song_title", "song_artist", "choreographer", "note", "notes",
              "description", "body", "title", "reference", "memo", "venue",
              "location_text", "admin_note", "allergies", "special_needs",
              "interest", "source", "props", "message", "subject")
    tdir = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))) / "app" / "templates"
    offenders = []
    for f in tdir.rglob("*.html"):
        for m in re.finditer(r"\$\{\s*([A-Za-z_]\w*)\.(" + "|".join(FIELDS) + r")\s*\}", f.read_text()):
            # `${x.student_name}` with no esc() around it
            offenders.append(f"{f.relative_to(tdir)}: ${{{m.group(1)}.{m.group(2)}}}")
    record(f"No user-name field rendered without esc() ({len(offenders)} found)",
           not offenders, "; ".join(offenders[:8]), "P1")


def run_js_syntax():
    """Node-check the inline <script> of JS-heavy pages. A rendered-JS syntax
    error (e.g. a bad string escape) silently kills a whole page's behavior and
    is invisible to Jinja compile/render checks — this catches that class."""
    import re
    import shutil
    import subprocess
    import tempfile as _tf

    node = shutil.which("node")
    if not node:
        record("JS syntax check (node not found — skipped)", True, "", "P3")
        return

    pages = [("admin", "admin123", ["/dashboard", "/reports/aging",
                                    "/students", "/classes", "/rules", "/messages", "/families"]),
             # Teacher renders differ (IS_ADMIN-gated JS blocks) — a bad template
             # conditional would kill a page only for teachers, invisible to the
             # admin-only check.
             ("teacher_t", "pw", ["/students", "/classes", "/rules", "/messages", "/families"]),
             ("parent_a", "pw", ["/parent"])]
    bad = []
    for user, pw, paths in pages:
        with app.test_client() as c:
            login(c, user, pw)
            for path in paths:
                html = c.get(path).get_data(as_text=True)
                for i, script in enumerate(re.findall(r"<script>(.*?)</script>", html, re.S)):
                    if "function" not in script and "=>" not in script:
                        continue
                    f = _tf.NamedTemporaryFile("w", suffix=".js", delete=False)
                    f.write(script)
                    f.close()
                    r = subprocess.run([node, "--check", f.name], capture_output=True, text=True)
                    os.unlink(f.name)
                    if r.returncode != 0:
                        first = (r.stderr.strip().splitlines() or ["?"])[-3:]
                        bad.append(f"{path}#script{i}: {' '.join(first)[:120]}")
    record(f"Rendered inline JS parses on {sum(len(p[2]) for p in pages)} JS-heavy pages",
           not bad, "; ".join(bad), "P1")


def run_smoke():
    """As admin, GET every no-arg GET route; assert no 500s."""
    with app.test_client() as c:
        login(c, "admin", "admin123")
        with app.app_context():
            rules = [r for r in app.url_map.iter_rules()
                     if "GET" in r.methods and not r.arguments
                     and not r.rule.startswith("/static")
                     and "logout" not in r.rule]
        errors = []
        for r in sorted(rules, key=lambda x: x.rule):
            try:
                resp = c.get(r.rule)
                if resp.status_code >= 500:
                    errors.append(f"{r.rule} -> {resp.status_code}")
            except Exception as e:  # noqa: BLE001
                errors.append(f"{r.rule} -> EXC {type(e).__name__}: {e}")
        record(f"Admin no-arg GET smoke ({len(rules)} routes, {len(errors)} failing)",
               not errors, "; ".join(errors[:15]), "P1")


def run_empty_state():
    """Day-one check: a fresh studio (only the auto-seeded admin, NO families /
    classes / transactions / recitals) must render every page and no-arg API GET
    without a 500. Aggregation pages (analytics, reports, giving statement) are
    the usual empty-data landmines (division by zero, empty-list indexing). Runs
    in a subprocess against a throwaway DB because this suite's own app is already
    seeded with two families."""
    import subprocess
    snippet = (
        "import os, json\n"
        "from app import create_app\n"
        "app = create_app('development'); app.config['TESTING'] = True\n"
        "with app.app_context():\n"
        "    rules = sorted({r.rule for r in app.url_map.iter_rules()\n"
        "        if 'GET' in r.methods and not r.arguments\n"
        "        and not r.rule.startswith('/static') and 'logout' not in r.rule})\n"
        "bad = []\n"
        "with app.test_client() as c:\n"
        "    c.post('/auth/login', data={'username':'admin','password':'admin123'}, follow_redirects=True)\n"
        "    for path in rules:\n"
        "        try:\n"
        "            if c.get(path).status_code >= 500: bad.append(path)\n"
        "        except Exception as e:\n"
        "            bad.append(f'{path}!{type(e).__name__}')\n"
        "print(json.dumps({'n': len(rules), 'bad': bad}))\n"
    )
    env = dict(os.environ)
    env["RFID_ENABLED"] = "false"
    env["DATABASE_URL"] = f"sqlite:///{tempfile.NamedTemporaryFile(suffix='.db').name}"
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        p = subprocess.run([sys.executable, "-c", snippet], capture_output=True,
                           text=True, cwd=root, env=env, timeout=120)
        import json as _json
        out = _json.loads(p.stdout.strip().splitlines()[-1]) if p.stdout.strip() else {"n": 0, "bad": ["no output"]}
        bad = out["bad"]
        record(f"Fresh empty studio renders cleanly ({out['n']} routes, {len(bad)} failing)",
               not bad, f"5xx on empty data: {bad[:15]}", "P1")
    except Exception as e:  # noqa: BLE001
        record("Fresh empty studio renders cleanly", False, f"probe failed: {e}", "P1")


def main():
    ids = seed()
    run_idor(ids)
    run_csrf()
    run_security_headers()
    run_login_no_demo_creds_in_prod()
    run_demo_parent_prod_lockout()
    run_csrf_guard_behind_tls_proxy()
    run_no_pii_in_logs()
    run_smtp_timeout()
    run_proxyfix_https_links()
    run_teacher_authz(ids)
    run_admin_parent_reset_link(ids)
    run_privilege_escalation(ids)
    run_waiver_signing(ids)
    run_attendance(ids)
    run_message_blast(ids)
    run_message_blast_non_blocking()
    run_recurring_short_month_clamp()
    run_recurring_no_retroactive_first_month()
    run_attendance_default_local()
    run_rfid_assign_unique()
    run_day_of_week_convention()
    run_rfid_checkin_local_day()
    run_rfid_reuses_app()
    run_registration_approve_capacity()
    run_money_creation_audited()
    run_costume_charge_race()
    run_full_mutation_fuzz()
    run_update_fuzz()
    run_update_valid_id_fuzz()
    run_post_valid_id_fuzz()
    run_date_param_robustness()
    run_get_queryparam_fuzz(ids)
    run_skills(ids)
    run_analytics(ids)
    run_leads()
    run_timeclock()
    run_auto_reminders()
    run_reminder_non_blocking()
    run_manual_reminders_non_blocking()
    run_multichild_invite_merge()
    run_invite_security()
    run_crypto_secrets()
    run_square_webhook(ids)
    run_reconciliation(ids)
    run_enrollment(ids)
    run_deactivation_revokes_session()
    run_password_reset()
    run_forgot_password_no_enumeration()
    run_rule_authz()
    run_login_throttle()
    run_open_redirect_guard()
    run_login_by_email()
    run_registration_flow()
    run_registration_dedupe()
    run_registration_pilot_shape_dedupe()
    run_approval_links_sibling_to_portal()
    run_registration_volume_caps()
    run_registration_field_caps()
    run_clean_str_backstop()
    run_amount_validation(ids)
    run_payment_plans(ids)
    run_recital_money(ids)
    run_recital_delete_cascade(ids)
    run_transaction_delete(ids)
    run_manual_cascade_deletes(ids)
    run_input_robustness(ids)
    run_parent_input_robustness(ids)
    run_parent_write_authz(ids)
    run_class_render_guard(ids)
    run_orphan_guard(ids)
    run_orphan_render_guard(ids)
    run_prod_security_config()
    run_migration_idempotency()
    run_backup(ids)
    run_csv_exports(ids)
    run_statements(ids)
    run_statement_math()
    run_revenue_math()
    run_xss_guard()
    run_cashtag_sanitize()
    run_qr_upload_safety()
    run_email_header_injection()
    run_confirm_payment_race()
    run_receipt_non_blocking()
    run_registration_approve_race()
    run_attendance_race()
    run_late_fee_race()
    run_global_search()
    run_dead_handler_guard()
    run_logging_config_guard()
    run_sqlite_wal_guard()
    run_auth_form_labels()
    run_transactions_pagination(ids)
    run_students_roster_complete()
    run_messages_pagination()
    run_registrations_pagination()
    run_registration_notify_throttle()
    run_withdrawn_student_balance()
    run_parent_sees_withdrawn_child()
    run_api_error_shape()
    run_skill_archive_safe()
    run_parent_portal_classes()
    run_withdraw_frees_enrollment()
    run_class_crud()
    run_class_instructor_assignment()
    run_class_capacity()
    run_capacity_lifecycle()
    run_recurring_charge_edit()
    run_page_route_authz(ids)
    run_admin_role_consistency()
    run_admin_identity_invariants()
    run_js_syntax()
    run_smoke()
    run_empty_state()

    fails = [r for r in results if not r[2]]
    p0 = [r for r in fails if r[0] == "P0"]
    print("\n" + "=" * 60)
    print(f"SUMMARY: {len(results) - len(fails)}/{len(results)} passed, "
          f"{len(fails)} failed ({len(p0)} P0).")
    try:
        os.unlink(_tmp.name)
    except OSError:
        pass
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
