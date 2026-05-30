#!/usr/bin/env bash
#
# rotate-token.sh —— 给已存在的用户重新签发 token，输出新的 kubeconfig。
#
# 使用场景：token 即将过期 / 怀疑 token 泄露 / 用户换设备。
# 不会动 ServiceAccount / RoleBinding，只是把 token 字段刷新一下。
#
# 使用：
#   ./rotate-token.sh zhangsan ray-cluster-3
#   ./rotate-token.sh zhangsan ray-cluster-3 --duration 168h
set -euo pipefail

USER=""; NAMESPACE=""; DURATION="8760h"; OUT_DIR="$(pwd)"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --duration) DURATION="$2"; shift 2 ;;
    --out-dir)  OUT_DIR="$2";  shift 2 ;;
    -h|--help)
      cat <<EOF
Usage: $0 <user> <namespace> [--duration 8760h] [--out-dir <path>]

Re-issue a token for an existing raytrain user and emit a fresh kubeconfig.
The user's ServiceAccount / RoleBinding are NOT touched.

Default duration: 8760h (1 year).

Example:
  $0 zhangsan ray-cluster-3
  $0 zhangsan ray-cluster-3 --duration 720h
EOF
      exit 0 ;;
    -*) echo "unknown option: $1" >&2; exit 1 ;;
    *)
      if   [[ -z "$USER" ]];      then USER="$1"
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

SA_NAME="raytrain-${USER}"
KUBECONFIG_OUT="${OUT_DIR}/kubeconfig-${USER}-${NAMESPACE}.yaml"

# 校验 SA 存在
if ! kubectl -n "${NAMESPACE}" get sa "${SA_NAME}" >/dev/null 2>&1; then
  echo "error: ServiceAccount ${NAMESPACE}/${SA_NAME} does not exist." >&2
  echo "       use add-user.sh to create the user first." >&2
  exit 1
fi

echo ">>> rotating token for ${NAMESPACE}/${SA_NAME} (duration=${DURATION})"
TOKEN="$(kubectl -n "${NAMESPACE}" create token "${SA_NAME}" --duration="${DURATION}")"

SERVER="$(kubectl config view --raw --minify -o jsonpath='{.clusters[0].cluster.server}')"
CA_DATA="$(kubectl config view --raw --minify -o jsonpath='{.clusters[0].cluster.certificate-authority-data}')"
if [[ -z "$CA_DATA" ]]; then
  CA_FILE="$(kubectl config view --raw --minify -o jsonpath='{.clusters[0].cluster.certificate-authority}')"
  [[ -n "$CA_FILE" && -f "$CA_FILE" ]] && CA_DATA="$(base64 < "$CA_FILE" | tr -d '\n')"
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

cat <<EOF

>>> done.

new kubeconfig: ${KUBECONFIG_OUT}
duration:       ${DURATION}

note: previous tokens for this SA remain valid until they expire.
      to invalidate everything, run remove-user.sh and then add-user.sh again.
EOF
