#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MONITOR_LOG="${SCRIPT_DIR}/okx_ai_monitor.log"
SYSTEMD_OUT="${SCRIPT_DIR}/logs/systemd.out.log"
SYSTEMD_ERR="${SCRIPT_DIR}/logs/systemd.err.log"

case "${1:-monitor}" in
    monitor)
        touch "${MONITOR_LOG}"
        tail -n "${2:-100}" -f "${MONITOR_LOG}"
        ;;
    systemd)
        touch "${SYSTEMD_OUT}" "${SYSTEMD_ERR}"
        tail -n "${2:-100}" -f "${SYSTEMD_OUT}" "${SYSTEMD_ERR}"
        ;;
    journal)
        journalctl --user -u okx-ai-assistant -f
        ;;
    *)
        echo "Usage: $0 [monitor|systemd|journal] [lines]"
        exit 1
        ;;
esac
