#!/usr/bin/env bash
# Start PyCharm via JetBrains Projector unless disabled.
#
# NOTE: Projector / PyCharm licensing is the operator's responsibility. The
# image build can skip Projector entirely (set ARG INSTALL_PYCHARM=false) for
# environments without a JetBrains license; this script then just idles.
set -euo pipefail

if [[ ",${RAYTRAIN_ENABLED_IDES:-jupyter,code,pycharm,ssh}," != *",pycharm,"* ]]; then
    echo "[pycharm] disabled via RAYTRAIN_ENABLED_IDES; idling"
    exec sleep infinity
fi

if ! command -v projector >/dev/null 2>&1; then
    echo "[pycharm] projector not installed in this image; idling"
    exec sleep infinity
fi

# Projector serves on 8887; access control at Ingress.
exec projector run --port 8887
