# Nightly database backups

The whole studio lives in one SQLite file on a single Fly volume. This wires up
an automated nightly copy to a private, versioned S3 bucket so a lost volume
isn't a lost studio.

## How it works

```
GitHub Actions (nightly cron)
        │  1. GET /api/cron/backup   (X-Cron-Token header)
        ▼
Fly app  ── SQLite online-backup API → consistent snapshot → streams the .db
        │  2. verify magic header + PRAGMA integrity_check + users table
        ▼
S3  s3://attenddance-backups/db/attendance-YYYY-MM-DD.db   (private, SSE, versioned)
```

The backup runs **off** the Fly box on purpose: the machine is 256MB,
OOM-sensitive, and auto-sleeps. The job's HTTPS request wakes it, and all the
heavy lifting (download, verify, upload) happens on the GitHub runner — no
sidecar, no boto3, no memory pressure on the app.

Retention: 90 days for daily objects, 30 days for superseded versions
(lifecycle rule in `setup-s3.sh`).

## One-time setup

### 1. Create the bucket (done from a machine with AWS access)

```bash
AWS_PROFILE=default ./backups/setup-s3.sh
```

Idempotent — creates the bucket and asserts: public-access blocked, versioning
on, default AES256 encryption, 90/30-day lifecycle.

### 2. Create a scoped IAM user for the uploader

The GitHub job needs an AWS key that can do exactly one thing — put objects
under `db/` in this one bucket. In the AWS console → IAM → Users → create user
`attenddance-backup-uploader` (no console access), attach this inline policy:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "s3:PutObject",
      "Resource": "arn:aws:s3:::attenddance-backups/db/*"
    }
  ]
}
```

Create an access key for it. You'll paste the pair into GitHub next.

### 3. Set the app's cron token (Fly)

The endpoint is rejected unless a token is configured. Generate one and set it
as a Fly secret (this also unlocks `/api/cron/run` for auto-reminders):

```bash
TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
echo "CRON_TOKEN=$TOKEN"          # keep this — GitHub needs the same value
fly secrets set CRON_TOKEN="$TOKEN" -a attenddance
```

### 4. Add the GitHub Actions secrets

Repo → Settings → Secrets and variables → Actions → New repository secret:

| Secret | Value |
|---|---|
| `BACKUP_URL` | `https://attenddance.fly.dev/api/cron/backup` |
| `CRON_TOKEN` | the same token from step 3 |
| `BACKUP_AWS_ACCESS_KEY_ID` | the IAM key from step 2 |
| `BACKUP_AWS_SECRET_ACCESS_KEY` | the IAM secret from step 2 |

### 5. Test it

GitHub → Actions → **nightly-backup** → **Run workflow**. It should pull, verify,
and upload. Confirm the object landed:

```bash
aws s3 ls s3://attenddance-backups/db/ --profile default
```

## Restore

Download the day you want and point the app at it (or swap it onto the volume):

```bash
aws s3 cp s3://attenddance-backups/db/attendance-2026-07-26.db ./restore.db --profile default
sqlite3 restore.db 'PRAGMA integrity_check;'   # expect: ok
```

To restore into production, copy it to the Fly volume at `/data/attendance.db`
(stop the machine first so nothing is mid-write), then start it back up.

## Manual backup anytime

Admins can always pull one from the app UI — the same consistent snapshot, via
`/api/admin/backup` (session-authenticated). Good for an ad-hoc copy before a
risky change.
