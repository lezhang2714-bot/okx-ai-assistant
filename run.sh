#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MONITOR="${SCRIPT_DIR}/monitor.py"
CONFIG="${SCRIPT_DIR}/config.json"
ENV_FILE="${SCRIPT_DIR}/.env"
VENV_DIR="${SCRIPT_DIR}/.venv"
PARSE_ARGS="${SCRIPT_DIR}/_parse_args.py"

mkdir -p "${SCRIPT_DIR}/logs"

# ---------- 加载 .env 文件 ----------
if [ -f "${ENV_FILE}" ]; then
    set -a
    # shellcheck disable=SC1090
    source "${ENV_FILE}"
    set +a
fi

# ---------- 选择 Python ----------
PYTHON_BIN="python3"
if [ -x "${VENV_DIR}/bin/python" ]; then
    PYTHON_BIN="${VENV_DIR}/bin/python"
fi

# ---------- 解析 config.json 生成参数 ----------
ARGS="$("${PYTHON_BIN}" "${PARSE_ARGS}")"

# ---------- 启动监控 ----------
cd "${SCRIPT_DIR}"
exec "${PYTHON_BIN}" "${MONITOR}" ${ARGS}
