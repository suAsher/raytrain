#!/usr/bin/env bash
# =============================================================================
# issue-token.sh —— 为 raytrain 用户签发一个 HS256 自签 JWT（self-signed token）。
#
# 该 token 可被提交服务器（raytrain/server/auth.py 的 _verify_self_signed）验证：
#   jwt.decode(token, secret, algorithms=["HS256"], options={"require": ["exp"]})
#   - 算法 HS256；必须带 exp；sub = 用户名；可选 tenant；iss 缺省 "raytrain"。
#
# ── 签名密钥来源（两条路径）──────────────────────────────────────────────────
# 1. 若环境变量 RAYTRAIN_JWT_SECRET 已设置且非空 → 直接使用，跳过 kubectl。
#    便于本地 / dev 自测，无需集群：
#      RAYTRAIN_JWT_SECRET=xxxx ./issue-token.sh alice
# 2. 否则从 K8s Secret 读（默认 namespace=raytrain-system，secret=raytrain-jwt-key）：
#      kubectl -n <ns> get secret <secret-name> \
#          -o jsonpath="{.data.<secret-key>}" | base64 --decode
#    未显式给 --secret-key 时，依次尝试常见 key：
#      RAYTRAIN_JWT_SECRET, secret, jwt-secret —— 取第一个有值的。
#
# ── 重要：密钥必须与 server 一致 ─────────────────────────────────────────────
# 本脚本默认从 Secret `raytrain-jwt-key` 读取（task 9.1）；而 server Deployment
# （deploy/server/deployment.yaml, task 7.6）从 Secret `raytrain-server-secrets`
# 的 key `RAYTRAIN_JWT_SECRET` 读取。两个 Secret 名字不同 —— 这是已知的命名差异，
# 因此 secret 名 / key 都做成可被 flag / env 覆盖。无论 server 实际从哪里读密钥，
# 本脚本签名用的值都必须与之 **完全相同**，token 才能验证通过。
# 推荐：用同一个值创建两处 Secret：
#   VAL="$(openssl rand -hex 32)"
#   kubectl -n raytrain-system create secret generic raytrain-jwt-key \
#       --from-literal=RAYTRAIN_JWT_SECRET="$VAL"
#   kubectl -n raytrain-system create secret generic raytrain-server-secrets \
#       --from-literal=RAYTRAIN_JWT_SECRET="$VAL"
#
# ── 用法 ────────────────────────────────────────────────────────────────────
#   ./issue-token.sh <user> [--days 30] [--tenant <id>] \
#       [--namespace raytrain-system] [--secret-name raytrain-jwt-key] \
#       [--secret-key <key>] [--issuer raytrain]
#
# 示例：
#   ./issue-token.sh alice
#   ./issue-token.sh alice --days 7 --tenant team-a
#   RAYTRAIN_JWT_SECRET=test-secret ./issue-token.sh bob   # 本地，无需集群
#
# 输出：token 打印到 stdout，并写入当前目录 token-<user>.txt（权限 0600）。
#
# ── 签名实现 ────────────────────────────────────────────────────────────────
# 纯 bash + openssl，无 python / PyJWT 依赖，便于在 ops 机器上直接跑：
#   signing_input = base64url(header) "." base64url(payload)
#   signature     = base64url( HMAC-SHA256(signing_input, secret) )
#   token         = signing_input "." signature
# 注意用 `printf '%s'`（非 echo）喂签名输入，避免尾随换行污染签名。
# macOS 的 base64 无 -w 标志，统一用 `tr -d '\n'` 去换行。
# =============================================================================
set -euo pipefail

# --------------------------------------------------------------------------- #
# Defaults
# --------------------------------------------------------------------------- #
USER_NAME=""
DAYS=30
TENANT=""
NAMESPACE="raytrain-system"
SECRET_NAME="raytrain-jwt-key"
SECRET_KEY=""          # 空 = auto：依次尝试常见 key
ISSUER="raytrain"
# 默认 key 候选（auto 模式，按顺序尝试，取第一个有值的）。
DEFAULT_SECRET_KEYS="RAYTRAIN_JWT_SECRET secret jwt-secret"

usage() {
  cat <<EOF
Usage: $0 <user> [--days 30] [--tenant <id>] [--namespace raytrain-system]
                 [--secret-name raytrain-jwt-key] [--secret-key <key>]
                 [--issuer raytrain]

Issue an HS256 self-signed JWT for a raytrain user. The token is verifiable by
the submission server (raytrain/server/auth.py): HS256, requires 'exp',
sub=<user>, iss=<issuer>, optional tenant.

Arguments:
  <user>                 (required) username -> JWT 'sub' claim.

Options:
  --days <n>             token lifetime in days (default: 30).
  --tenant <id>          add a 'tenant' claim (default: none / omitted).
  --namespace <ns>       K8s namespace of the secret (default: raytrain-system).
  --secret-name <name>   K8s Secret holding the HS256 key (default: raytrain-jwt-key).
  --secret-key <key>     key inside the Secret's .data. Default: auto-try
                         ${DEFAULT_SECRET_KEYS}.
  --issuer <iss>         JWT 'iss' claim (default: raytrain).
  -h, --help             show this help.

Signing secret resolution (in order):
  1. If env RAYTRAIN_JWT_SECRET is set & non-empty -> use it (skips kubectl).
     Handy for local/dev:  RAYTRAIN_JWT_SECRET=xxxx $0 alice
  2. Else read from K8s:
       kubectl -n <ns> get secret <secret-name> \\
           -o jsonpath="{.data.<secret-key>}" | base64 --decode

NOTE: the secret used here MUST equal the value the server reads as
      RAYTRAIN_JWT_SECRET, or verification will fail. (The server's Secret is
      'raytrain-server-secrets'; this script defaults to 'raytrain-jwt-key' —
      keep their values identical, or override --secret-name/--secret-key.)

Examples:
  $0 alice
  $0 alice --days 7 --tenant team-a
  RAYTRAIN_JWT_SECRET=test-secret-at-least-32-bytes-long $0 bob   # no cluster
EOF
}

# --------------------------------------------------------------------------- #
# Arg parsing
# --------------------------------------------------------------------------- #
while [ $# -gt 0 ]; do
  case "$1" in
    --days)        DAYS="${2:-}";        shift 2 ;;
    --tenant)      TENANT="${2:-}";      shift 2 ;;
    --namespace)   NAMESPACE="${2:-}";   shift 2 ;;
    --secret-name) SECRET_NAME="${2:-}"; shift 2 ;;
    --secret-key)  SECRET_KEY="${2:-}";  shift 2 ;;
    --issuer)      ISSUER="${2:-}";      shift 2 ;;
    -h|--help)     usage; exit 0 ;;
    --)            shift; break ;;
    -*)            echo "error: unknown option: $1" >&2; usage >&2; exit 1 ;;
    *)
      if [ -z "$USER_NAME" ]; then
        USER_NAME="$1"
      else
        echo "error: unexpected argument: $1" >&2; exit 1
      fi
      shift ;;
  esac
done

if [ -z "$USER_NAME" ]; then
  echo "error: <user> is required." >&2
  usage >&2
  exit 1
fi

# Validate --days is a positive integer.
case "$DAYS" in
  ''|*[!0-9]*) echo "error: --days must be a positive integer (got: '$DAYS')." >&2; exit 1 ;;
  0)           echo "error: --days must be >= 1 (got: '$DAYS')." >&2; exit 1 ;;
esac

if [ -z "$ISSUER" ]; then
  echo "error: --issuer must not be empty." >&2
  exit 1
fi

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
# base64url encode from stdin: standard base64 (no line wraps), '+/'->'-_',
# strip '=' padding. macOS base64 has no -w flag, so we drop newlines via tr.
b64url() {
  openssl base64 | tr -d '\n' | tr '+/' '-_' | tr -d '='
}

# HMAC-SHA256 over stdin with raw secret -> raw bytes -> base64url.
hmac_b64url() {
  openssl dgst -sha256 -hmac "$1" -binary | b64url
}

now_epoch() {
  date +%s
}

# Human-readable UTC for an epoch second. GNU (-d @) then BSD (-r) then python3.
human_utc() {
  epoch="$1"
  if date -u -d "@${epoch}" +"%Y-%m-%d %H:%M:%S UTC" 2>/dev/null; then
    return 0
  fi
  if date -u -r "${epoch}" +"%Y-%m-%d %H:%M:%S UTC" 2>/dev/null; then
    return 0
  fi
  python3 -c "import datetime,sys; print(datetime.datetime.utcfromtimestamp(int(sys.argv[1])).strftime('%Y-%m-%d %H:%M:%S UTC'))" "$epoch"
}

# Minimal JSON string escaping for user-supplied values (backslash, quote).
# Keeps the hand-built payload JSON safe for typical usernames / tenant ids.
json_escape() {
  printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'
}

# --------------------------------------------------------------------------- #
# Resolve signing secret
# --------------------------------------------------------------------------- #
SECRET=""
if [ -n "${RAYTRAIN_JWT_SECRET:-}" ]; then
  echo ">>> using RAYTRAIN_JWT_SECRET from environment (skipping kubectl)" >&2
  SECRET="$RAYTRAIN_JWT_SECRET"
else
  if ! command -v kubectl >/dev/null 2>&1; then
    echo "error: kubectl not found on PATH." >&2
    echo "       Install kubectl, or set RAYTRAIN_JWT_SECRET in env for local use." >&2
    exit 1
  fi

  # Build the list of keys to try: explicit --secret-key, else the auto list.
  if [ -n "$SECRET_KEY" ]; then
    KEYS_TO_TRY="$SECRET_KEY"
  else
    KEYS_TO_TRY="$DEFAULT_SECRET_KEYS"
  fi

  RAW_B64=""
  FOUND_KEY=""
  for k in $KEYS_TO_TRY; do
    val="$(kubectl -n "$NAMESPACE" get secret "$SECRET_NAME" \
        -o jsonpath="{.data.${k}}" 2>/dev/null || true)"
    if [ -n "$val" ]; then
      RAW_B64="$val"
      FOUND_KEY="$k"
      break
    fi
  done

  if [ -z "$RAW_B64" ]; then
    echo "error: could not read an HS256 secret from Secret '${SECRET_NAME}' in namespace '${NAMESPACE}'." >&2
    echo "       tried key(s): ${KEYS_TO_TRY}" >&2
    echo "       (secret/key missing, empty, or kubectl failed)" >&2
    echo "" >&2
    echo "hint: create it with a fresh random key:" >&2
    echo "  kubectl -n ${NAMESPACE} create secret generic ${SECRET_NAME} \\" >&2
    echo "      --from-literal=RAYTRAIN_JWT_SECRET=\"\$(openssl rand -hex 32)\"" >&2
    echo "" >&2
    echo "  ...and make sure the server's secret (raytrain-server-secrets" >&2
    echo "  .RAYTRAIN_JWT_SECRET) holds the SAME value." >&2
    exit 1
  fi
  echo ">>> read secret from K8s: ns=${NAMESPACE} secret=${SECRET_NAME} key=${FOUND_KEY}" >&2

  SECRET="$(printf '%s' "$RAW_B64" | base64 --decode 2>/dev/null || true)"
  if [ -z "$SECRET" ]; then
    echo "error: secret value decoded to empty. Check Secret '${SECRET_NAME}' key '${FOUND_KEY}'." >&2
    exit 1
  fi
fi

# --------------------------------------------------------------------------- #
# Build claims and mint the JWT (HS256)
# --------------------------------------------------------------------------- #
IAT="$(now_epoch)"
EXP=$(( IAT + DAYS * 86400 ))

USER_ESC="$(json_escape "$USER_NAME")"
ISSUER_ESC="$(json_escape "$ISSUER")"

HEADER='{"alg":"HS256","typ":"JWT"}'

# Assemble payload JSON with no stray whitespace. tenant only when non-empty.
if [ -n "$TENANT" ]; then
  TENANT_ESC="$(json_escape "$TENANT")"
  PAYLOAD="$(printf '{"sub":"%s","iss":"%s","iat":%s,"exp":%s,"tenant":"%s"}' \
    "$USER_ESC" "$ISSUER_ESC" "$IAT" "$EXP" "$TENANT_ESC")"
else
  PAYLOAD="$(printf '{"sub":"%s","iss":"%s","iat":%s,"exp":%s}' \
    "$USER_ESC" "$ISSUER_ESC" "$IAT" "$EXP")"
fi

HEADER_B64="$(printf '%s' "$HEADER" | b64url)"
PAYLOAD_B64="$(printf '%s' "$PAYLOAD" | b64url)"
SIGNING_INPUT="${HEADER_B64}.${PAYLOAD_B64}"
SIGNATURE_B64="$(printf '%s' "$SIGNING_INPUT" | hmac_b64url "$SECRET")"
TOKEN="${SIGNING_INPUT}.${SIGNATURE_B64}"

# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
OUT_FILE="token-${USER_NAME}.txt"
# Create with restrictive perms up front, then write.
( umask 077; : > "$OUT_FILE" )
printf '%s\n' "$TOKEN" > "$OUT_FILE"
chmod 0600 "$OUT_FILE"

EXP_HUMAN="$(human_utc "$EXP")"

# Raw token first, on its own line (easy to copy / pipe), then a summary.
echo "$TOKEN"
echo ""
echo ">>> token issued for user='${USER_NAME}'${TENANT:+ tenant='${TENANT}'}"
echo "    algorithm: HS256 (self-signed, iss=${ISSUER})"
echo "    expires:   ${EXP_HUMAN} (in ${DAYS} day(s))"
echo "    saved to:  $(pwd)/${OUT_FILE} (mode 0600)"
echo ""
echo "    put it in ~/.raytrain/config.yaml under 'token:' —"
echo "      token: ${TOKEN}"
