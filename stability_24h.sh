#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="${SCRIPT_DIR}/logs"
LOG_FILE="${LOG_DIR}/stability_24h.log"
SUMMARY_FILE="${LOG_DIR}/stability_24h.summary.json"

mkdir -p "${LOG_DIR}"

START_TS="$(date +%s)"
echo "[stability] start: $(date -Is)" | tee "${LOG_FILE}"

set +e
OKX_RUNTIME=86400 "${SCRIPT_DIR}/run.sh" >> "${LOG_FILE}" 2>&1
EXIT_CODE="$?"
set -e

END_TS="$(date +%s)"
DURATION="$((END_TS - START_TS))"
FAIL_COUNT="$(grep -c "collect/analyze failed" "${LOG_FILE}" || true)"
RETRY_COUNT="$(grep -c "retry" "${LOG_FILE}" || true)"
PUSH_COUNT="$(grep -c "\\[OKX AI短线助手\\]" "${LOG_FILE}" || true)"

cat > "${SUMMARY_FILE}" <<EOF
{
  "started_at": "${START_TS}",
  "ended_at": "${END_TS}",
  "duration_seconds": ${DURATION},
  "exit_code": ${EXIT_CODE},
  "collect_analyze_failed_count": ${FAIL_COUNT},
  "retry_count": ${RETRY_COUNT},
  "push_message_count": ${PUSH_COUNT},
  "log_file": "${LOG_FILE}"
}
EOF

echo "[stability] end: $(date -Is)" | tee -a "${LOG_FILE}"
cat "${SUMMARY_FILE}"
exit "${EXIT_CODE}"
