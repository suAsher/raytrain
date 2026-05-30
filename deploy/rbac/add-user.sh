#!/usr/bin/env bash
#
# add-user.sh —— 为某个用户开通一个 namespace 的 raytrain 访问权限。
#
# 做的事（幂等，可重复跑）：
#   1. 在目标 namespace 内创建 ServiceAccount: raytrain-<user>
#   2. 创建 RoleBinding 把 raytrain-user Role 绑给这个 SA
#   3. 给这个 SA 签发一个 token（默认 30 天）
#   4. 输出一份 kubeconfig 文件 kubeconfig-<user>-<ns>.yaml 到当前目录
#
# 使用：
#   ./add-user.sh zhangsan ray-cluster-3                  # 默认 1 年
#   ./add-user.sh zhangsan ray-cluster-3 --duration 720h  # 30 天
#   ./add-user.sh zhangsan ray-cluster-3 --duration 168h  # 7 天
#
# 前提：
#   - bootstrap-namespace.sh 已经在 ${NAMESPACE} 上跑过
#   - 执行者具备 cluster-admin 权限
#   - kubectl >= 1.24（需要 `kubectl create token` 子命令）
#   - 集群 API server 允许的最大 token 时长 ≥ --duration
#     （由 kube-apiserver --service-account-max-token-expiration 决定，
#      多数发行版默认就是 1 年；如果 add-user 报 token 时长超限，
#      让平台同事调大这个参数，或缩短 --duration）
#
# 注意：
#   - 同一个 user 在不同 namespace 是不同的 SA（raytrain-<user>），互不影响
#   - token 是有时效的；过期后用 rotate-token.sh 续期
#   - 把生成的 kubeconfig 通过受控渠道发给本人，不要 commit 到 git
set -euo pipefail

# ----------------------------- 解析参数 ----------------------------- #
USER=""
NAMESPACE=""
DURATION="8760h"   # 默认 1 年（365 天 × 24h）
OUT_DIR="$(pwd)"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --duration) DURATION="$2"; shift 2 ;;
    --out-dir)  OUT_DIR="$2"; shift 2 ;;
    -h|--help)
      cat <<EOF
Usage: $0 <user> <namespace> [--duration 8760h] [--out-dir <path>]

Provision a raytrain user inside a single namespace and emit a kubeconfig.

Arguments:
  <user>        User identifier (used as SA name suffix and kubeconfig user name).
                Must be a valid DNS-1123 label: lowercase, alphanumeric, '-' allowed.
  <namespace>   Target namespace (must already be bootstrapped).

Options:
  --duration    Token validity (e.g. 168h, 720h, 8760h). Default: 8760h (1 year).
  --out-dir     Where to write kubeconfig-<user>-<ns>.yaml. Default: cwd.
  -h, --help    Show this help.

Example:
  $0 zhangsan ray-cluster-3
  $0 zhangsan ray-cluster-3 --duration 720h
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

if [[ -z "$USER" || -z "$NAMESPACE" ]]; then
  echo "error: <user> and <namespace> are required. See --help." >&2
  exit 1
fi

# DNS-1123 label 校验：lowercase a-z 0-9 -，开头/结尾必须是字母数字
if ! [[ "$USER" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?$ ]]; then
  echo "error: user '$USER' is not a valid DNS-1123 label." >&2
  echo "       use only lowercase letters, digits and '-'." >&2
  exit 1
fi

SA_NAME="raytrain-${USER}"
RB_NAME="raytrain-${USER}"
KUBECONFIG_OUT="${OUT_DIR}/kubeconfig-${USER}-${NAMESPACE}.yaml"

# ----------------------------- 校验 namespace 已 bootstrap ----------------------------- #
if ! kubectl get ns "${NAMESPACE}" >/dev/null 2>&1; then
  echo "error: namespace '${NAMESPACE}' does not exist." >&2
  echo "       run bootstrap-namespace.sh ${NAMESPACE} first." >&2
  exit 1
fi
if ! kubectl -n "${NAMESPACE}" get role raytrain-user >/dev/null 2>&1; then
  echo "error: Role 'raytrain-user' missing in '${NAMESPACE}'." >&2
  echo "       run bootstrap-namespace.sh ${NAMESPACE} first." >&2
  exit 1
fi

# ----------------------------- 1. ServiceAccount ----------------------------- #
echo ">>> ensuring ServiceAccount: ${NAMESPACE}/${SA_NAME}"
kubectl -n "${NAMESPACE}" apply -f - <<EOF
apiVersion: v1
kind: ServiceAccount
metadata:
  name: ${SA_NAME}
  labels:
    raytrain.io/managed: "true"
    raytrain.io/user: "${USER}"
EOF

# ----------------------------- 2. RoleBinding ----------------------------- #
echo ">>> ensuring RoleBinding: ${NAMESPACE}/${RB_NAME}"
kubectl -n "${NAMESPACE}" apply -f - <<EOF
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: ${RB_NAME}
  labels:
    raytrain.io/managed: "true"
    raytrain.io/user: "${USER}"
subjects:
  - kind: ServiceAccount
    name: ${SA_NAME}
    namespace: ${NAMESPACE}
roleRef:
  kind: Role
  name: raytrain-user
  apiGroup: rbac.authorization.k8s.io
EOF

# ----------------------------- 3. 签发 token ----------------------------- #
echo ">>> issuing token (duration=${DURATION})"
TOKEN="$(kubectl -n "${NAMESPACE}" create token "${SA_NAME}" --duration="${DURATION}")"

# ----------------------------- 4. 拼 kubeconfig ----------------------------- #
echo ">>> building kubeconfig"
SERVER="$(kubectl config view --raw --minify -o jsonpath='{.clusters[0].cluster.server}')"
CA_DATA="$(kubectl config view --raw --minify -o jsonpath='{.clusters[0].cluster.certificate-authority-data}')"

if [[ -z "$SERVER" ]]; then
  echo "error: could not read cluster.server from current kubeconfig." >&2
  exit 1
fi

# 如果 cluster.certificate-authority-data 为空，回退到 certificate-authority 文件
if [[ -z "$CA_DATA" ]]; then
  CA_FILE="$(kubectl config view --raw --minify -o jsonpath='{.clusters[0].cluster.certificate-authority}')"
  if [[ -n "$CA_FILE" && -f "$CA_FILE" ]]; then
    CA_DATA="$(base64 < "$CA_FILE" | tr -d '\n')"
  fi
fi

mkdir -p "${OUT_DIR}"
cat > "${KUBECONFIG_OUT}" <<EOF
apiVersion: v1
kind: Config
clusters:
- name: raytrain-cluster
  cluster:
    server: ${SERVER}
$( [[ -n "$CA_DATA" ]] && echo "    certificate-authority-data: ${CA_DATA}" || echo "    insecure-skip-tls-verify: true" )
contexts:
- name: ${USER}@${NAMESPACE}
  context:
    cluster: raytrain-cluster
    namespace: ${NAMESPACE}
    user: ${USER}
current-context: ${USER}@${NAMESPACE}
users:
- name: ${USER}
  user:
    token: ${TOKEN}
EOF

chmod 0600 "${KUBECONFIG_OUT}"

# ----------------------------- 完成提示 ----------------------------- #
cat <<EOF

>>> done.

artifact:    ${KUBECONFIG_OUT}
identity:    system:serviceaccount:${NAMESPACE}:${SA_NAME}
context:     ${USER}@${NAMESPACE}
namespace:   ${NAMESPACE}
duration:    ${DURATION}

deliver to user (any of):
  - send file ${KUBECONFIG_OUT} via secure channel
  - user puts it at ~/.kube/config (or sets KUBECONFIG env var)
  - user verifies with: kubectl get rayjobs

before token expires, run:
  $(dirname "$0")/rotate-token.sh ${USER} ${NAMESPACE}
EOF
