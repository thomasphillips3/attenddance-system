#!/usr/bin/env bash
#
# One-time (idempotent) setup of the private S3 bucket that holds the studio's
# nightly database backups. Safe to re-run — every step either creates or
# re-asserts the desired config.
#
# The bucket holds student/parent PII, so it is: all-public-access blocked,
# versioned (an overwrite or delete can't wipe history), encrypted at rest, and
# lifecycle-pruned. The nightly GitHub Actions job (.github/workflows/
# nightly-backup.yml) writes one object per day under db/.
#
#   AWS_PROFILE=default ./backups/setup-s3.sh
#
set -euo pipefail

BUCKET="attenddance-backups"
REGION="us-east-1"
PROFILE="${AWS_PROFILE:-default}"

echo "== Backup bucket: s3://$BUCKET (profile: $PROFILE, region: $REGION) =="

# 1. Bucket. us-east-1 must NOT get a LocationConstraint (API quirk).
aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" --profile "$PROFILE" >/dev/null 2>&1 \
  && echo "  created" || echo "  already exists"

# 2. Block ALL public access — this data is PII and must never be web-reachable.
aws s3api put-public-access-block --bucket "$BUCKET" --profile "$PROFILE" \
  --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
echo "  public access blocked"

# 3. Versioning — a same-day re-run overwrites db/attendance-YYYY-MM-DD.db, so
#    versioning keeps the earlier copy instead of losing it.
aws s3api put-bucket-versioning --bucket "$BUCKET" --profile "$PROFILE" \
  --versioning-configuration Status=Enabled
echo "  versioning enabled"

# 4. Default encryption at rest.
aws s3api put-bucket-encryption --bucket "$BUCKET" --profile "$PROFILE" \
  --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
echo "  default encryption on (AES256)"

# 5. Retention: keep daily backups 90 days; drop superseded versions after 30.
aws s3api put-bucket-lifecycle-configuration --bucket "$BUCKET" --profile "$PROFILE" \
  --lifecycle-configuration '{
    "Rules": [{
      "ID": "expire-daily-backups",
      "Filter": {"Prefix": "db/"},
      "Status": "Enabled",
      "Expiration": {"Days": 90},
      "NoncurrentVersionExpiration": {"NoncurrentDays": 30}
    }]
  }'
echo "  lifecycle set (90-day current, 30-day noncurrent)"

echo "Done."
