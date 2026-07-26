"""Backup endpoint tests for AttenDANCE.

  1. /api/cron/backup rejects a missing or wrong cron token (403).
  2. With the right token it returns a real, intact SQLite database that the
     nightly S3 job can trust (magic header + integrity_check + expected table).

Run:  RFID_ENABLED=false python3 tests/test_backup.py
Exit 0 = all green, 1 = failures.
"""
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("RFID_ENABLED", "false")
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp.name}"

from app import create_app, db  # noqa: E402
from app.models import User  # noqa: E402

TOKEN = "test-cron-token-123"

app = create_app("development")
app.config["TESTING"] = True
app.config["CRON_TOKEN"] = TOKEN

results = []


def record(name, passed, detail=""):
    results.append((name, passed))
    print(f"[{'PASS' if passed else 'FAIL'}] {name}" + (f" — {detail}" if detail and not passed else ""))


with app.app_context():
    db.create_all()
    if not User.query.filter_by(username="admin").first():
        u = User(username="admin", email="admin@example.test", role="admin",
                 first_name="Admin", last_name="User")
        u.set_password("x")
        db.session.add(u)
        db.session.commit()

client = app.test_client()

# 1. No token → 403
r = client.get("/api/cron/backup")
record("no token is rejected (403)", r.status_code == 403, f"got {r.status_code}")

# 2. Wrong token → 403 (query param path)
r = client.get("/api/cron/backup?token=wrong")
record("wrong token is rejected (403)", r.status_code == 403, f"got {r.status_code}")

# 3. Correct token via header → 200 + real SQLite file
r = client.get("/api/cron/backup", headers={"X-Cron-Token": TOKEN})
record("valid token returns 200", r.status_code == 200, f"got {r.status_code}")
body = r.data
record("body has the SQLite magic header",
       body[:16] == b"SQLite format 3\x00", f"got {body[:16]!r}")

# 4. The returned bytes are an intact DB with the expected schema
snap = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
snap.write(body)
snap.close()
try:
    con = sqlite3.connect(snap.name)
    integrity = con.execute("PRAGMA integrity_check;").fetchone()[0]
    record("snapshot passes integrity_check", integrity == "ok", f"got {integrity!r}")
    user_count = con.execute("SELECT count(*) FROM users;").fetchone()[0]
    record("snapshot contains the users table with rows", user_count >= 1, f"got {user_count}")
    con.close()
finally:
    os.unlink(snap.name)

# 5. Query-param token also works (the workflow uses the header, but /cron/run
#    accepts either, so keep parity).
r = client.get(f"/api/cron/backup?token={TOKEN}")
record("valid token via query param returns 200", r.status_code == 200, f"got {r.status_code}")

passed = sum(1 for _, p in results if p)
total = len(results)
print("\n" + "=" * 56)
print(f"SUMMARY: {passed}/{total} passed, {total - passed} failed.")
sys.exit(0 if passed == total else 1)
