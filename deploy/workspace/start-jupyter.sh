#!/usr/bin/env bash
# Start Jupyter Lab unless disabled via RAYTRAIN_ENABLED_IDES.
set -euo pipefail

if [[ ",${RAYTRAIN_ENABLED_IDES:-jupyter,code,pycharm,ssh}," != *",jupyter,"* ]]; then
    echo "[jupyter] disabled via RAYTRAIN_ENABLED_IDES; idling"
    exec sleep infinity
fi

USER_HOME="/home/${RAYTRAIN_USER:-raytrain}"
WORKDIR="${USER_HOME}/workspace"
mkdir -p "${WORKDIR}"

# No token / password: access control is enforced at the Ingress layer
# (per-workspace subdomain + platform auth), not inside the pod.
exec jupyter lab \
    --ip=0.0.0.0 \
    --port=8888 \
    --no-browser \
    --allow-root \
    --ServerApp.token='' \
    --ServerApp.password='' \
    --ServerApp.base_url=/jupyter/ \
    --ServerApp.allow_origin='*' \
    --ServerApp.root_dir="${WORKDIR}"
