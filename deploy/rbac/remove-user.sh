#!/usr/bin/env bash
#
# remove-user.sh —— 撤销某个用户对某个 namespace 的 raytrain 访问权限。
#
# 做的事：
#   1. 删除 RoleBinding raytrain-<user>
#   2. 删除 ServiceAccount raytrain-<user>（这一步会让所有为它签发的 token 立即失效）
#
# 使用：
#   ./remove-user.sh zhangsan ray-cluster-3
#   ./remove-user.sh zhangsan --all-namespaces      # 从所有 raytrain 管理的 ns 移除
set -euo pipefail

USER=""
NAMESPACE=""
ALL_NS="false"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --all-namespaces) ALL_NS="true"; shift ;;
    -h|--help)
      cat <<EOF
Usage: $0 <user> <namespace>
       $0 <user> --all-namespaces

Revoke a user's raytrain access in one namespace, or in every raytrain-managed
namespace at once.

Side effects:
  - Existing tokens for this user become invalid immediately.
  - The user's RayJobs / ConfigMaps / Secrets created earlier are NOT deleted.
EOF
      exit 0 ;;
    -*) echo "unknown option: $1" >&2; exit 1 ;;
    *)
      if   [[ -z "$USER" ]]; then USER="$1"
      elif [[ -z "$NAMESPACE" ]]; then NAMESPACE="$1"
      else echo "unexpected argument: $1" >&2; exit 1
      fi
      shift ;;
  esac
done

if [[ -z "$USER" ]]; then
  echo "error: <user> is required. See --help." >&2
  exit 1
fi
if [[ "$ALL_NS" != "true" && -z "$NAMESPACE" ]]; then
  echo "error: <namespace> is required (or use --all-namespaces). See --help." >&2
  exit 1
fi

SA_NAME="raytrain-${USER}"
RB_NAME="raytrain-${USER}"

remove_from_ns() {
  local ns="$1"
  if ! kubectl get ns "$ns" >/dev/null 2>&1; then
    echo "    namespace '$ns' not found, skipping"
    return
  fi
  echo ">>> revoking ${USER} from namespace ${ns}"
  kubectl -n "$ns" delete rolebinding    "${RB_NAME}" --ignore-not-found
  kubectl -n "$ns" delete serviceaccount "${SA_NAME}" --ignore-not-found
}

if [[ "$ALL_NS" == "true" ]]; then
  # 只操作 raytrain 管理的 ns（带 raytrain.io/managed=true 标签）
  mapfile -t NS_LIST < <(kubectl get ns -l raytrain.io/managed=true -o jsonpath='{.items[*].metadata.name}' | tr ' ' '\n')
  if [[ ${#NS_LIST[@]} -eq 0 ]]; then
    echo "no raytrain-managed namespaces found (label raytrain.io/managed=true)"
    exit 0
  fi
  for ns in "${NS_LIST[@]}"; do
    remove_from_ns "$ns"
  done
else
  remove_from_ns "$NAMESPACE"
fi

echo ""
echo ">>> done."
echo "    note: any RayJobs / Secrets created by ${USER} remain in place."
echo "          clean them up manually if needed: kubectl get rayjobs -l raytrain.owner=${USER}"
