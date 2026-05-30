#!/usr/bin/env bash
#
# bootstrap-namespace.sh —— 初始化一个 raytrain 可用的 namespace。
#
# 做的事（幂等，可重复跑）：
#   1. 如果 namespace 不存在则创建；存在就跳过
#   2. apply Role (raytrain-user) 到这个 namespace
#   3. 可选 apply ResourceQuota（默认开，传 --no-quota 关）
#   4. 给 namespace 打标签 raytrain.io/managed=true，便于以后批量识别
#
# 使用：
#   ./bootstrap-namespace.sh ray-cluster-3
#   ./bootstrap-namespace.sh ray-cluster-3 --no-quota
#   ./bootstrap-namespace.sh new-team-a   # 新建 ns 同时初始化
#
# 前提：执行者具备 cluster-admin 权限（你作为运维管理员）。
set -euo pipefail

# ----------------------------- 解析参数 ----------------------------- #
NAMESPACE=""
APPLY_QUOTA="true"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-quota) APPLY_QUOTA="false"; shift ;;
    -h|--help)
      cat <<EOF
Usage: $0 <namespace> [--no-quota]

Initialize a namespace so raytrain users can be granted access to it.

Arguments:
  <namespace>   Target namespace (created if missing)

Options:
  --no-quota    Skip applying the default ResourceQuota
  -h, --help    Show this help

Examples:
  $0 ray-cluster-3
  $0 new-team-a --no-quota
EOF
      exit 0 ;;
    -*) echo "unknown option: $1" >&2; exit 1 ;;
    *)  NAMESPACE="$1"; shift ;;
  esac
done

if [[ -z "$NAMESPACE" ]]; then
  echo "error: namespace is required. See --help." >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ----------------------------- 主流程 ----------------------------- #
echo ">>> bootstrap namespace: ${NAMESPACE}"

# 1. namespace
if kubectl get ns "${NAMESPACE}" >/dev/null 2>&1; then
  echo "    namespace '${NAMESPACE}' already exists, reusing"
else
  echo "    creating namespace '${NAMESPACE}'"
  kubectl create namespace "${NAMESPACE}"
fi

# 标识为 raytrain 管理（list-namespaces 时用）
kubectl label namespace "${NAMESPACE}" raytrain.io/managed=true --overwrite >/dev/null

# 2. Role
echo "    applying Role: raytrain-user"
kubectl -n "${NAMESPACE}" apply -f "${SCRIPT_DIR}/role.yaml"

# 3. ResourceQuota（可选）
if [[ "${APPLY_QUOTA}" == "true" ]]; then
  echo "    applying ResourceQuota: raytrain-quota"
  kubectl -n "${NAMESPACE}" apply -f "${SCRIPT_DIR}/resource-quota.yaml"
else
  echo "    skipping ResourceQuota (--no-quota)"
fi

# 4. 简要打印结果
echo ""
echo ">>> done. namespace '${NAMESPACE}' is ready."
echo ""
echo "next steps:"
echo "  - add a user:  ${SCRIPT_DIR}/add-user.sh <user> ${NAMESPACE}"
echo "  - list users:  ${SCRIPT_DIR}/list-users.sh ${NAMESPACE}"
