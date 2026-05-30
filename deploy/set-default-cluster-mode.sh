#!/usr/bin/env bash
#
# set-default-cluster-mode.sh —— 设置某个 namespace 的默认 cluster-mode。
#
# 做的事（幂等，可重复跑）：
#   在目标 namespace 写入 ConfigMap `raytrain-defaults`，其 data 里
#   key `default_cluster_mode` 取值 per_job 或 shared。
#   不存在则创建，已存在则更新该 key（kubectl apply 是声明式的）。
#
# ── 这个 ConfigMap 是什么 ────────────────────────────────────────────────────
# 它是 cluster-mode 解析的「中间层」。raytrain CLI（submit.py, task 8.2 的
# _resolve_cluster_mode / _configmap_cluster_mode）按以下优先级决定 cluster-mode：
#   1. CLI flag `--cluster-mode`（最高）
#   2. namespace ConfigMap `raytrain-defaults`（key `default_cluster_mode`）  ← 本脚本写这里
#   3. 用户本地配置 ~/.raytrain/config.yaml 的 `default_cluster_mode`
#   4. 最终兜底 `per_job`
# 也就是：CLI flag > 本 ConfigMap > 用户配置 > per_job。
# 运维可以用本脚本把「整个 namespace 的默认模式」一次性切到 per_job 或 shared，
# 用户无需各自改本地配置；个别命令仍可用 `--cluster-mode` 临时覆盖。
#
# ── 用法 ────────────────────────────────────────────────────────────────────
#   ./set-default-cluster-mode.sh <per_job|shared> [--namespace <ns>]
#
#   默认 namespace = ray-cluster-3（与 user_config.DEFAULT_NAMESPACE 一致）。
#   也可用环境变量 RAYTRAIN_NAMESPACE 指定默认 namespace；
#   命令行 --namespace / -n 优先级最高。
#
# 示例：
#   ./set-default-cluster-mode.sh shared
#   ./set-default-cluster-mode.sh per_job --namespace team-a
#   RAYTRAIN_NAMESPACE=team-b ./set-default-cluster-mode.sh shared
#
# 前提：
#   - kubectl 在 PATH 上，且当前 kubeconfig 有权限在目标 namespace 写 ConfigMap。
#
set -euo pipefail

# --------------------------------------------------------------------------- #
# Defaults
# --------------------------------------------------------------------------- #
# 默认 namespace：先看环境变量 RAYTRAIN_NAMESPACE，否则用 ray-cluster-3
# （与 raytrain/user_config.py 的 DEFAULT_NAMESPACE 保持一致）。
NAMESPACE="${RAYTRAIN_NAMESPACE:-ray-cluster-3}"
MODE=""
CONFIGMAP_NAME="raytrain-defaults"

usage() {
  cat <<EOF
Usage: $0 <per_job|shared> [--namespace <ns>]

Set the default cluster-mode for a namespace by writing the ConfigMap
'${CONFIGMAP_NAME}' (key default_cluster_mode=<mode>). Idempotent: creates the
ConfigMap if missing, updates the key if it already exists.

Arguments:
  <per_job|shared>       (required) the default cluster mode for the namespace.

Options:
  -n, --namespace <ns>   target namespace (default: ray-cluster-3, or
                         \$RAYTRAIN_NAMESPACE if set).
  -h, --help             show this help.

This ConfigMap is the MIDDLE tier of cluster-mode resolution used by the CLI
(raytrain/cli/submit.py, task 8.2):
  CLI flag --cluster-mode > this ConfigMap > ~/.raytrain config > per_job

Examples:
  $0 shared
  $0 per_job --namespace team-a
  RAYTRAIN_NAMESPACE=team-b $0 shared
EOF
}

# --------------------------------------------------------------------------- #
# Arg parsing
# --------------------------------------------------------------------------- #
while [ $# -gt 0 ]; do
  case "$1" in
    -n|--namespace) NAMESPACE="${2:-}"; shift 2 ;;
    -h|--help)      usage; exit 0 ;;
    --)             shift; break ;;
    -*)             echo "error: unknown option: $1" >&2; usage >&2; exit 1 ;;
    *)
      if [ -z "$MODE" ]; then
        MODE="$1"
      else
        echo "error: unexpected argument: $1" >&2; usage >&2; exit 1
      fi
      shift ;;
  esac
done

# --------------------------------------------------------------------------- #
# Validate args (BEFORE requiring kubectl/cluster, so this works locally).
# --------------------------------------------------------------------------- #
if [ -z "$MODE" ]; then
  echo "error: <per_job|shared> is required." >&2
  usage >&2
  exit 1
fi

case "$MODE" in
  per_job|shared) ;;
  *)
    echo "error: mode must be exactly 'per_job' or 'shared' (got: '$MODE')." >&2
    usage >&2
    exit 1 ;;
esac

if [ -z "$NAMESPACE" ]; then
  echo "error: --namespace must not be empty." >&2
  usage >&2
  exit 1
fi

# --------------------------------------------------------------------------- #
# Guard: kubectl must be on PATH (only needed once we actually touch a cluster).
# --------------------------------------------------------------------------- #
if ! command -v kubectl >/dev/null 2>&1; then
  echo "error: kubectl not found on PATH." >&2
  echo "       install kubectl and ensure your kubeconfig can write ConfigMaps" >&2
  echo "       in namespace '${NAMESPACE}'." >&2
  exit 1
fi

# --------------------------------------------------------------------------- #
# Apply (idempotent create-or-update).
# --------------------------------------------------------------------------- #
# create --dry-run=client renders the desired ConfigMap as YAML without touching
# the cluster; piping into `kubectl apply -f -` makes it declarative: created if
# missing, key updated if it already exists.
echo ">>> setting default_cluster_mode=${MODE} in ConfigMap '${CONFIGMAP_NAME}' (namespace=${NAMESPACE})"
kubectl -n "${NAMESPACE}" create configmap "${CONFIGMAP_NAME}" \
    --from-literal=default_cluster_mode="${MODE}" \
    --dry-run=client -o yaml | kubectl apply -f -

# --------------------------------------------------------------------------- #
# Confirmation + verify hint.
# --------------------------------------------------------------------------- #
echo ""
echo ">>> done. namespace '${NAMESPACE}' default cluster-mode = ${MODE}"
echo ""
echo "    this ConfigMap is the MIDDLE tier of cluster-mode resolution (submit.py task 8.2):"
echo "      CLI flag --cluster-mode  >  this ConfigMap  >  ~/.raytrain config  >  per_job"
echo ""
echo "    verify with:"
echo "      kubectl -n ${NAMESPACE} get configmap ${CONFIGMAP_NAME} -o jsonpath='{.data.default_cluster_mode}'"
