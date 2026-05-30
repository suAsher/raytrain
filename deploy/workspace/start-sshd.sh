#!/usr/bin/env bash
# Start sshd unless disabled. Public-key auth only; the user's authorized_keys
# lives on the PVC (~/.ssh/authorized_keys), uploaded via the Web UI.
set -euo pipefail

if [[ ",${RAYTRAIN_ENABLED_IDES:-jupyter,code,pycharm,ssh}," != *",ssh,"* ]]; then
    echo "[sshd] disabled via RAYTRAIN_ENABLED_IDES; idling"
    exec sleep infinity
fi

USER_NAME="${RAYTRAIN_USER:-raytrain}"
USER_HOME="/home/${USER_NAME}"
mkdir -p "${USER_HOME}/.ssh"
chmod 700 "${USER_HOME}/.ssh" || true

# Generate host keys on first boot (persisted on PVC under ~/.ssh/host-keys).
HOST_KEY_DIR="${USER_HOME}/.ssh/host-keys"
mkdir -p "${HOST_KEY_DIR}"
if [[ ! -f "${HOST_KEY_DIR}/ssh_host_ed25519_key" ]]; then
    ssh-keygen -t ed25519 -f "${HOST_KEY_DIR}/ssh_host_ed25519_key" -N "" || true
fi

# Minimal sshd config: pubkey only, no passwords, no root login.
cat > /tmp/sshd_config <<EOF
Port 22
HostKey ${HOST_KEY_DIR}/ssh_host_ed25519_key
PermitRootLogin no
PasswordAuthentication no
PubkeyAuthentication yes
AuthorizedKeysFile ${USER_HOME}/.ssh/authorized_keys
UsePAM no
X11Forwarding no
Subsystem sftp internal-sftp
EOF

exec /usr/sbin/sshd -D -f /tmp/sshd_config -e
