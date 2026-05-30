#!/usr/bin/env bash
#
# list-users.sh —— 列出 raytrain 管理范围内的用户/namespace 状况。
#
# 使用：
#   ./list-users.sh                       # 全部 raytrain 管理的 ns + 各自的用户
#   ./list-users.sh ray-cluster-3         # 单个 ns
set -euo pipefail

TARGET_NS="${1:-}"

print_ns_users() {
  local ns="$1"
  echo "==> namespace: ${ns}"

  # Role 是否存在
  if kubectl -n "$ns" get role raytrain-user >/dev/null 2>&1; then
    echo "    Role:           raytrain-user (ok)"
  else
    echo "    Role:           MISSING (run bootstrap-namespace.sh ${ns})"
  fi

  # ResourceQuota
  if kubectl -n "$ns" get resourcequota raytrain-quota >/dev/null 2>&1; then
    local quota
    quota="$(kubectl -n "$ns" get resourcequota raytrain-quota -o jsonpath='{.status.hard.requests\.nvidia\.com/gpu}')"
    echo "    ResourceQuota:  raytrain-quota (gpu=${quota:-?})"
  else
    echo "    ResourceQuota:  none"
  fi

  # 用户列表（按 SA 找）
  local sas
  sas=$(kubectl -n "$ns" get sa -l raytrain.io/managed=true \
        -o jsonpath='{range .items[*]}{.metadata.labels.raytrain\.io/user}{"\n"}{end}' \
        2>/dev/null | sort -u | sed '/^$/d' || true)
  if [[ -z "$sas" ]]; then
    echo "    users:          (none)"
  else
    echo "    users:"
    while IFS= read -r u; do
      echo "      - ${u}"
    done <<< "$sas"
  fi
  echo ""
}

if [[ -n "$TARGET_NS" ]]; then
  print_ns_users "$TARGET_NS"
  exit 0
fi

mapfile -t NS_LIST < <(kubectl get ns -l raytrain.io/managed=true -o jsonpath='{.items[*].metadata.name}' | tr ' ' '\n')
if [[ ${#NS_LIST[@]} -eq 0 ]]; then
  echo "no raytrain-managed namespaces found."
  echo "to bootstrap one: ./bootstrap-namespace.sh <namespace>"
  exit 0
fi
for ns in "${NS_LIST[@]}"; do
  print_ns_users "$ns"
done
