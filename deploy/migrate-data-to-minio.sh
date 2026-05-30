#!/usr/bin/env bash
#
# One-time migration: push ~10 TB of training data from H20-2's local disk
# into MinIO. Safe to re-run; `mc mirror` skips files already present with the
# same size.
#
# Run on H20-2 (it has the data). Requires `mc` (MinIO client).
#
set -euo pipefail

MINIO_ENDPOINT="${MINIO_ENDPOINT:-http://172.31.16.3:30950}"
MINIO_ACCESS_KEY="${MINIO_ACCESS_KEY:?set MINIO_ACCESS_KEY}"
MINIO_SECRET_KEY="${MINIO_SECRET_KEY:?set MINIO_SECRET_KEY}"
SRC_ROOT="${SRC_ROOT:-/storage/data-acc}"
BUCKET="${BUCKET:-pointcept-data}"

mc alias set waterpool "$MINIO_ENDPOINT" "$MINIO_ACCESS_KEY" "$MINIO_SECRET_KEY"
mc mb --ignore-existing "waterpool/$BUCKET"

# Adjust subtrees to whatever you actually want in MinIO. The framework needs
# one "top-level" per dataset so .raytrain.yaml can reference s3://$BUCKET/<name>.
for sub in labeled seg workspace; do
  if [[ -d "$SRC_ROOT/$sub" ]]; then
    echo ">>> mirroring $SRC_ROOT/$sub -> waterpool/$BUCKET/$sub"
    mc mirror --overwrite --remove=false \
      "$SRC_ROOT/$sub" "waterpool/$BUCKET/$sub"
  fi
done

echo ">>> summary:"
mc du "waterpool/$BUCKET"
