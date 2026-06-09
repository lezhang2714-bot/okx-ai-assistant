#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MONITOR="${SCRIPT_DIR}/monitor.py"
CONFIG="${SCRIPT_DIR}/config.json"
ENV_FILE="${SCRIPT_DIR}/.env"
VENV_DIR="${SCRIPT_DIR}/.venv"

mkdir -p "${SCRIPT_DIR}/logs"

if [ -f "${ENV_FILE}" ]; then
    set -a
    # shellcheck disable=SC1090
    source "${ENV_FILE}"
    set +a
fi

PYTHON_BIN="python3"
if [ -x "${VENV_DIR}/bin/python" ]; then
    PYTHON_BIN="${VENV_DIR}/bin/python"
fi

ARGS="$("${PYTHON_BIN}" - "${CONFIG}" <<'PY'
import json
import shlex
import sys

config_path = sys.argv[1]
with open(config_path, "r", encoding="utf-8") as f:
    cfg = json.load(f)

args = []
inst_ids = cfg.get("inst_ids") or ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]
args += ["--inst-ids", ",".join(inst_ids)]

for key, option in [
    ("interval", "--interval"),
    ("runtime", "--runtime"),
    ("flag", "--flag"),
    ("push_score", "--push-score"),
    ("retry_times", "--retry-times"),
    ("retry_backoff", "--retry-backoff"),
    ("push_cooldown_seconds", "--push-cooldown"),
    ("log_max_bytes", "--log-max-bytes"),
    ("volume_multiplier", "--volume-multiplier"),
    ("oi_change_pct_15m", "--oi-change-pct-15m"),
    ("funding_abs_threshold", "--funding-threshold"),
    ("funding_change_threshold", "--funding-change-threshold"),
    ("long_short_extreme", "--long-short-extreme"),
]:
    if key in cfg:
        args += [option, str(cfg[key])]

if cfg.get("ai_enabled"):
    args.append("--ai")
if cfg.get("dry_run_ai"):
    args.append("--dry-run-ai")
if cfg.get("push_enabled"):
    args.append("--push")

print(" ".join(shlex.quote(x) for x in args))
PY
)"

cd "${SCRIPT_DIR}"
exec "${PYTHON_BIN}" "${MONITOR}" ${ARGS}
