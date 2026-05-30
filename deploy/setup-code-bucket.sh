#!/usr/bin/env bash
#
# setup-code-bucket.sh —— 给 raytrain code-as-submission 准备 MinIO bucket。
#
# 做的事（幂等）：
#   1. 创建 bucket（默认 raytrain-code）
#   2. 写入 7 天 lifecycle policy（"对象创建 7 天后自动删除"）
#   3. 打印一条 sanity 行，方便人工确认
#
# 运行前需要：
#   - 已安装 MinIO 客户端 mc（https://min.io/docs/minio/linux/reference/minio-mc.html）
#   - 已知 MinIO endpoint / access_key / secret_key
#
# 使用：
#   MINIO_ENDPOINT=http://172.31.16.3:30950 \
#   MINIO_ACCESS_KEY=xxx \
#   MINIO_SECRET_KEY=xxx \
#       ./setup-code-bucket.sh
#
#   # 或者覆盖 bucket 名：
#   ./setup-code-bucket.sh my-team-code
#
# 注意：
#   - lifecycle 是 7 天硬过期。超过这个窗口就无法用 `raytrain reproduce`
#     恢复历史代码。如果业务需要更长，自行调高（修改 LIFECYCLE_DAYS）。
#   - bucket 不区分 user，但 key 前缀按 user 分隔（<user>/<job>.zip）。
#     若启用 dedup，blob 路径在 _blobs/<sha>.zip，同一 7 天 lifecycle。
#
set -euo pipefail

ENDPOINT="${MINIO_ENDPOINT:?must set MINIO_ENDPOINT (e.g. http://host:30950)}"
ACCESS_KEY="${MINIO_ACCESS_KEY:?must set MINIO_ACCESS_KEY}"
SECRET_KEY="${MINIO_SECRET_KEY:?must set MINIO_SECRET_KEY}"
BUCKET="${1:-raytrain-code}"
LIFECYCLE_DAYS="${LIFECYCLE_DAYS:-7}"
ALIAS="raytrain-setup"

echo ">>> setting up MinIO bucket: ${BUCKET} (lifecycle=${LIFECYCLE_DAYS}d)"

# 1. mc alias（一次性，幂等）
mc alias set "${ALIAS}" "${ENDPOINT}" "${ACCESS_KEY}" "${SECRET_KEY}" \
    >/dev/null

# 2. bucket（已存在则跳过）
if mc ls "${ALIAS}/${BUCKET}" >/dev/null 2>&1; then
    echo "    bucket '${BUCKET}' already exists, reusing"
else
    echo "    creating bucket '${BUCKET}'"
    mc mb -p "${ALIAS}/${BUCKET}"
fi

# 3. lifecycle policy
TMP_LIFECYCLE="$(mktemp /tmp/raytrain-lifecycle.XXXXXX.json)"
trap 'rm -f "$TMP_LIFECYCLE"' EXIT

cat > "${TMP_LIFECYCLE}" <<EOF
{
  "Rules": [
    {
      "ID": "raytrain-code-expire-${LIFECYCLE_DAYS}d",
      "Status": "Enabled",
      "Filter": {},
      "Expiration": {
        "Days": ${LIFECYCLE_DAYS}
      }
    }
  ]
}
EOF

echo "    applying lifecycle: ${LIFECYCLE_DAYS}d expiration"
mc ilm import "${ALIAS}/${BUCKET}" < "${TMP_LIFECYCLE}"

# 4. sanity 行
echo ""
echo ">>> done."
echo ""
echo "    bucket:       s3://${BUCKET}/"
echo "    lifecycle:    ${LIFECYCLE_DAYS} days"
echo "    object key pattern:  <user>/<job_name>.zip"
echo ""
echo "    verify with:  mc ilm export ${ALIAS}/${BUCKET}"
