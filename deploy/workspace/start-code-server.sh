#!/usr/bin/env bash
# Start code-server (browser VS Code) unless disabled.
set -euo pipefail

if [[ ",${RAYTRAIN_ENABLED_IDES:-jupyter,code,pycharm,ssh}," != *",code,"* ]]; then
    echo "[code-server] disabled via RAYTRAIN_ENABLED_IDES; idling"
    exec sleep infinity
fi

USER_HOME="/home/${RAYTRAIN_USER:-raytrain}"
WORKDIR="${USER_HOME}/workspace"
mkdir -p "${WORKDIR}"

# auth=none: access control at the Ingress layer.
exec code-server \
    --bind-addr 0.0.0.0:8080 \
    --auth none \
    --disable-telemetry \
    --disable-update-check \
    "${WORKDIR}"
