#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

if [[ ! -f "${PROJECT_DIR}/app/main.py" ]]; then
  echo "Could not find app/main.py under ${PROJECT_DIR}" >&2
  exit 1
fi

VENV_PYTHON="${PROJECT_DIR}/.venv/bin/python"
if [[ ! -x "${VENV_PYTHON}" ]]; then
  echo "Missing virtualenv Python at ${VENV_PYTHON}" >&2
  echo "Create it first:"
  echo "  cd ${PROJECT_DIR}"
  echo "  python -m venv .venv"
  echo "  source .venv/bin/activate"
  echo "  pip install -r requirements.txt"
  exit 1
fi

ENV_FILE="${PROJECT_DIR}/.env"
if [[ ! -f "${ENV_FILE}" ]]; then
  echo "Missing ${ENV_FILE}" >&2
  echo "Create it first:"
  echo "  cp ${PROJECT_DIR}/.env.example ${ENV_FILE}"
  exit 1
fi

USER_SYSTEMD_DIR="${HOME}/.config/systemd/user"
SERVICE_NAME="sdr-monitor.service"
SERVICE_PATH="${USER_SYSTEMD_DIR}/${SERVICE_NAME}"

mkdir -p "${USER_SYSTEMD_DIR}"

cat > "${SERVICE_PATH}" <<SERVICE
[Unit]
Description=SDR Monitor (FastAPI + scanner)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${PROJECT_DIR}
EnvironmentFile=${ENV_FILE}
ExecStart=${VENV_PYTHON} -m app.main
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
SERVICE

systemctl --user daemon-reload
systemctl --user enable --now "${SERVICE_NAME}"

cat <<OUT
Installed and started ${SERVICE_NAME}

Useful commands:
  systemctl --user status ${SERVICE_NAME}
  journalctl --user -u ${SERVICE_NAME} -f

To keep it running after logout, enable lingering once (requires sudo):
  sudo loginctl enable-linger ${USER}
OUT
