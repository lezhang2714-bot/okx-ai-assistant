#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${SCRIPT_DIR}/.venv"
SERVICE_SRC="${SCRIPT_DIR}/okx-ai-assistant.service"
USER_SERVICE_DIR="${HOME}/.config/systemd/user"
USER_SERVICE_DST="${USER_SERVICE_DIR}/okx-ai-assistant.service"

echo "[install] create python venv: ${VENV_DIR}"
python3 -m venv "${VENV_DIR}"

echo "[install] upgrade pip"
"${VENV_DIR}/bin/python" -m pip install --upgrade pip

echo "[install] install dependencies"
"${VENV_DIR}/bin/python" -m pip install python-okx openai

chmod +x \
    "${SCRIPT_DIR}/run.sh" \
    "${SCRIPT_DIR}/view_logs.sh" \
    "${SCRIPT_DIR}/test_push.py" \
    "${SCRIPT_DIR}/test_ai.py" \
    "${SCRIPT_DIR}/stability_24h.sh"

if [ "${1:-}" = "--user-systemd" ]; then
    echo "[install] install user systemd service"
    mkdir -p "${USER_SERVICE_DIR}"
    cp "${SERVICE_SRC}" "${USER_SERVICE_DST}"
    systemctl --user daemon-reload
    systemctl --user enable okx-ai-assistant
    echo "[install] user service installed"
    echo "Start:  systemctl --user start okx-ai-assistant"
    echo "Status: systemctl --user status okx-ai-assistant"
else
    echo "[install] systemd install skipped"
    echo "Run './install.sh --user-systemd' to install user service."
fi

echo "[install] done"
