#!/usr/bin/env python3
"""
Browser based local control panel for OKX AI Assistant.

This file intentionally uses only Python standard library modules so the
Windows deployment can start the UI without installing a web framework.
"""

import base64
import html
import io
import json
import mimetypes
import os
from contextlib import contextmanager
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
import zipfile
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from monitor_config_summary import (  # noqa: E402
    ACCURACY_METRIC_SCOPES,
    apply_fixed_behavior_defaults,
    config_requires_monitor_restart,
    config_value,
    paper_settings_from_log_item,
    recommended_interval_for_strategy,
    recommended_accuracy_horizon_for_strategy,
    sync_strategy_bound_config,
    STRATEGY_ACCURACY_HORIZON_HINTS,
    STRATEGY_DEFAULT_ACCURACY_HORIZON_SECONDS,
    STRATEGY_DEFAULT_INTERVAL_SECONDS,
    STRATEGY_INTERVAL_BOUNDS,
)
from monitor_design_docs import render_design_docs_html  # noqa: E402

_SIGNAL_MONITOR = None


def get_signal_monitor():
    global _SIGNAL_MONITOR
    if _SIGNAL_MONITOR is not None:
        return _SIGNAL_MONITOR
    try:
        import okx_signal_monitor as mod
    except ModuleNotFoundError:
        import importlib.util

        signal_monitor_path = SCRIPT_DIR / "okx_signal_monitor.py"
        spec = importlib.util.spec_from_file_location("okx_signal_monitor", signal_monitor_path)
        if spec is None or spec.loader is None:
            raise
        mod = importlib.util.module_from_spec(spec)
        sys.modules["okx_signal_monitor"] = mod
        spec.loader.exec_module(mod)
    _SIGNAL_MONITOR = mod
    return mod


BUILD_DIR = SCRIPT_DIR / "build"
CONFIG_DIR = SCRIPT_DIR / "config"
PORTABLE_STATE_DIR = SCRIPT_DIR / "local_state"
LEGACY_STATE_DIR = BUILD_DIR / "local_state"
ASSETS_DIR = SCRIPT_DIR / "web_assets"
LOG_DIR = BUILD_DIR / "runtime_logs"

try:
    _monitor_boot = get_signal_monitor()
    REPLAY_DATASET_FILE = _monitor_boot.REPLAY_DATASET_FILE
    REPLAY_LOG_FILE = _monitor_boot.REPLAY_LOG_FILE
    replay_dataset_stats = _monitor_boot.replay_dataset_stats
    DEFAULT_LOG_MAX_BYTES = int(_monitor_boot.DEFAULT_LOG_MAX_BYTES)
    DEFAULT_LOG_TOTAL_MAX_BYTES = int(_monitor_boot.DEFAULT_LOG_TOTAL_MAX_BYTES)
    MIN_LOG_MAX_BYTES = int(_monitor_boot.MIN_LOG_MAX_BYTES)
    tail_analysis_log_text = _monitor_boot.tail_analysis_log_text
    iter_analysis_log_lines = _monitor_boot.iter_analysis_log_lines
    analysis_log_total_bytes = _monitor_boot.analysis_log_total_bytes
    list_analysis_log_segments = _monitor_boot.list_analysis_log_segments
    AI_TOKEN_STATS_FILE = _monitor_boot.AI_TOKEN_STATS_FILE
    load_ai_token_stats = _monitor_boot.load_ai_token_stats
    reset_ai_token_stats = _monitor_boot.reset_ai_token_stats
except Exception:
    REPLAY_DATASET_FILE = LOG_DIR / "replay_dataset.jsonl"
    REPLAY_LOG_FILE = LOG_DIR / "replay_analysis.jsonl"
    DEFAULT_LOG_MAX_BYTES = 500 * 1024 * 1024
    DEFAULT_LOG_TOTAL_MAX_BYTES = 8 * 1024 * 1024 * 1024
    MIN_LOG_MAX_BYTES = 50 * 1024 * 1024

    def replay_dataset_stats(path):  # type: ignore
        return {"exists": False, "lines": 0, "bytes": 0}

    def tail_analysis_log_text(path, max_bytes):  # type: ignore
        return tail_text(path, max_bytes)

    def iter_analysis_log_lines(path, *, read_full=False, max_tail_bytes=None):  # type: ignore
        yield from iter_json_log_lines(path, read_full=read_full, max_tail_bytes=max_tail_bytes)

    def analysis_log_total_bytes(path):  # type: ignore
        return path.stat().st_size if path.exists() else 0

    def list_analysis_log_segments(path):  # type: ignore
        return [path] if path.exists() else []

    AI_TOKEN_STATS_FILE = LOG_DIR / "ai_session_tokens.json"

    def load_ai_token_stats(path=AI_TOKEN_STATS_FILE):  # type: ignore
        return {"total_tokens": 0, "prompt_tokens": 0, "completion_tokens": 0, "calls": 0}

    def reset_ai_token_stats(path=AI_TOKEN_STATS_FILE, started_at=""):  # type: ignore
        return load_ai_token_stats(path)

DEFAULT_CONFIG_FILE = CONFIG_DIR / "trading_assistant_config.json"
DEFAULT_AUTH_FILE = CONFIG_DIR / "web_console_auth.default.json"
PORTABLE_CONFIG_FILE = PORTABLE_STATE_DIR / "trading_assistant_config.json"
PORTABLE_ENV_FILE = PORTABLE_STATE_DIR / "api_secrets.env"
PORTABLE_AUTH_FILE = PORTABLE_STATE_DIR / "web_console_auth.json"
LEGACY_CONFIG_FILE = LEGACY_STATE_DIR / "trading_assistant_config.json"
LEGACY_ENV_FILE = LEGACY_STATE_DIR / "api_secrets.env"
LEGACY_AUTH_FILE = LEGACY_STATE_DIR / "web_console_auth.json"
USER_STATE_DIR = (Path(os.getenv("LOCALAPPDATA")) / "OKX_AI_Assistant") if os.getenv("LOCALAPPDATA") else (Path.home() / ".okx_ai_assistant")
USER_CONFIG_FILE = USER_STATE_DIR / "trading_assistant_config.json"
USER_ENV_FILE = USER_STATE_DIR / "api_secrets.env"
USER_AUTH_FILE = USER_STATE_DIR / "web_console_auth.json"
MONITOR_JSON_LOG_FILE = LOG_DIR / "okx_signal_analysis.jsonl"
MONITOR_PROCESS_LOG_FILE = LOG_DIR / "signal_monitor_console.log"
PAPER_ACCOUNT_FILE = LOG_DIR / "paper_account.json"
PAPER_INITIAL_CAPITAL = 10000.0
REPLAY_ANALYSIS_LOG_FILE = REPLAY_LOG_FILE
REPLAY_PROCESS_LOG_FILE = LOG_DIR / "replay_console.log"
HISTORICAL_REPLAY_DATASET_FILE = LOG_DIR / "replay_dataset_historical.jsonl"
HOST = os.getenv("WEB_CONTROL_PANEL_HOST", "127.0.0.1")
PORT = int(os.getenv("WEB_CONTROL_PANEL_PORT", "8765"))
APP_VERSION = "1.3.1"
APP_NAME = "OKX AI Assistant"
PRESET_INSTRUMENTS = ("BTC-USDT-SWAP", "ETH-USDT-SWAP")
DEFAULT_MONITOR_INSTRUMENTS = ("ETH-USDT-SWAP",)
SUPPORTED_INSTRUMENTS = PRESET_INSTRUMENTS
MONITOR_BAR_CHANNELS = ("1m", "3m", "5m", "15m", "1H", "4H", "1D", "1W")
OKX_BASE_URL = "https://www.okx.com"
INST_ID_PATTERN = re.compile(r"^[A-Z0-9]+-[A-Z0-9]+-SWAP$")
_INST_VALIDATION_CACHE: Dict[str, Tuple[float, bool]] = {}
INST_VALIDATION_CACHE_TTL = 300.0

SESSIONS: Dict[str, float] = {}
SESSION_CREATED: Dict[str, float] = {}
SESSION_TTL_SECONDS = float(os.getenv("WEB_SESSION_TTL_SECONDS", str(7 * 24 * 3600)))
SESSION_ABSOLUTE_TTL_SECONDS = float(os.getenv("WEB_SESSION_ABSOLUTE_TTL_SECONDS", str(30 * 24 * 3600)))
REALTIME_LOG_TAIL_BYTES = int(os.getenv("WEB_REALTIME_LOG_TAIL_BYTES", str(16 * 1024 * 1024)))
ACCURACY_LOG_TAIL_BYTES = int(os.getenv("WEB_ACCURACY_LOG_TAIL_BYTES", str(32 * 1024 * 1024)))
DEFAULT_ACCURACY_RETENTION_HOURS = 24.0
LOG_DISPLAY_MAX_LINES = max(500, int(os.getenv("WEB_LOG_DISPLAY_MAX_LINES", "2000")))
CONSOLE_LOG_MAX_BYTES = int(os.getenv("WEB_CONSOLE_LOG_MAX_BYTES", str(100 * 1024 * 1024)))
CONSOLE_LOG_TOTAL_MAX_BYTES = int(os.getenv("WEB_CONSOLE_LOG_TOTAL_MAX_BYTES", str(300 * 1024 * 1024)))
API_RESPONSE_CACHE_TTL_SECONDS = float(os.getenv("WEB_API_CACHE_TTL_SECONDS", "4"))
WEB_CANDLE_CACHE_TTL_SECONDS = float(os.getenv("WEB_CANDLE_CACHE_TTL_SECONDS", "45"))
REALTIME_LOG_CACHE_SECONDS = float(os.getenv("WEB_REALTIME_LOG_CACHE_SECONDS", "2.5"))
MONITOR_STATUS_HEAVY_INTERVAL_SECONDS = float(
    os.getenv("WEB_MONITOR_STATUS_HEAVY_INTERVAL_SECONDS", "30")
)
WEB_MAX_CONCURRENT_REQUESTS = max(8, int(os.getenv("WEB_MAX_CONCURRENT_REQUESTS", "48")))
WEB_MONITOR_AUTO_RESTART = os.getenv("WEB_MONITOR_AUTO_RESTART", "1").strip().lower() in ("1", "true", "yes")
DIAG_EXPORT_MAX_FILE_BYTES = int(os.getenv("WEB_DIAG_EXPORT_MAX_FILE_BYTES", str(32 * 1024 * 1024)))
DIAG_EXPORT_MAX_ZIP_BYTES = int(os.getenv("WEB_DIAG_EXPORT_MAX_ZIP_BYTES", str(150 * 1024 * 1024)))
DIAG_EXPORT_REPLAY_DATASET_TAIL_BYTES = int(os.getenv("WEB_DIAG_EXPORT_REPLAY_DATASET_TAIL_BYTES", str(32 * 1024 * 1024)))
DIAG_EXPORT_REPLAY_DATASET_NAMES = frozenset({"replay_dataset.jsonl", "replay_dataset_historical.jsonl"})
ACCURACY_HORIZON_OPTIONS: Tuple[Tuple[int, str], ...] = (
    (5, "5秒"),
    (15, "15秒"),
    (30, "30秒"),
    (60, "1分钟"),
    (180, "3分钟"),
    (300, "5分钟"),
    (900, "15分钟"),
    (1200, "20分钟"),
    (3600, "1小时"),
    (14400, "4小时"),
)


def clamp_accuracy_horizon_seconds(horizon_seconds: int) -> int:
    return max(5, min(86400, int(horizon_seconds or 900)))


def build_accuracy_horizon_select_html(strategy_mode: str) -> str:
    mode = str(strategy_mode or "swing").strip().lower()
    recommended = recommended_accuracy_horizon_for_strategy(mode)
    hint = STRATEGY_ACCURACY_HORIZON_HINTS.get(mode, "推荐")
    options = []
    for seconds, label in ACCURACY_HORIZON_OPTIONS:
        text = f"{label} · {hint}" if seconds == recommended else label
        selected = " selected" if seconds == recommended else ""
        options.append(f'<option value="{seconds}" data-base-label="{esc(label)}"{selected}>{esc(text)}</option>')
    return (
        f'<select id="accuracyHorizon" title="随配置页策略周期自动推荐验证窗口，可手动调整">'
        f'{"".join(options)}</select>'
    )


def diagnostic_accuracy_horizons(config: Dict[str, Any]) -> Tuple[int, ...]:
    primary = recommended_accuracy_horizon_for_strategy(config.get("strategy_mode"))
    secondary = 900 if primary != 900 else 300
    return tuple(dict.fromkeys([primary, secondary]))
SECRET_ENV_KEYS = frozenset({"OPENAI_API_KEY", "WECHAT_SEND_KEY"})
WEB_MONITOR_MAX_AUTO_RESTARTS = max(0, int(os.getenv("WEB_MONITOR_MAX_AUTO_RESTARTS", "5")))
WEB_MONITOR_AUTO_RESTART_WINDOW_SECONDS = max(60, int(os.getenv("WEB_MONITOR_AUTO_RESTART_WINDOW_SECONDS", "3600")))
WEB_MONITOR_CRASH_ALERT = os.getenv("WEB_MONITOR_CRASH_ALERT", "1").strip().lower() not in ("0", "false", "no")
MONITOR_PID_FILE = PORTABLE_STATE_DIR / "monitor.pid"
REPLAY_PID_FILE = PORTABLE_STATE_DIR / "replay.pid"
WEB_RESTART_SESSIONS_FILE = PORTABLE_STATE_DIR / "web_restart_sessions.json"
_API_RESPONSE_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_CANDLE_CACHE: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}
_MONITOR_AUTO_RESTART_AT: List[float] = []
WEB_SERVER: Optional[ThreadingHTTPServer] = None
WEB_POWER_SHUTDOWN = False
WEB_POWER_RESTART = False
MONITOR_PROCESS: subprocess.Popen = None
MONITOR_STARTED_AT = ""
MONITOR_LOG_START_AT = ""
MONITOR_STOPPED_AT = ""
MONITOR_STOP_REQUESTED = False
MONITOR_LAST_EXIT_UNEXPECTED = False
REPLAY_PROCESS: subprocess.Popen = None
REPLAY_STARTED_AT = ""
REPLAY_LOG_START_AT = ""
REPLAY_STOPPED_AT = ""
REPLAY_STOP_REQUESTED = False
_REPLAY_BUILD_LOCK = threading.Lock()
_REPLAY_BUILD_THREAD: Optional[threading.Thread] = None
_REPLAY_BUILD_STATE: Dict[str, Any] = {
    "running": False,
    "phase": "",
    "current": 0,
    "total": 0,
    "message": "",
    "error": "",
    "result": None,
    "cancel_requested": False,
}
_DATASET_STATS_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_LOG_LINE_COUNT_CACHE: Dict[str, Tuple[float, int]] = {}
_MONITOR_STOP_LOCK = threading.Lock()
_MONITOR_STATUS_HEAVY_AT = 0.0
_STATUS_TOKEN_CACHE: Tuple[float, int] = (0.0, 0)
_REALTIME_POINTS_CACHE: Dict[str, Tuple[float, Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]]] = {}

FIXED_MONITOR_RUNTIME = 0
FIXED_OKX_FLAG = "0"
LEGACY_CONFIG_KEYS = ("runtime", "flag")


SUGGESTED_PUSH_SCORES = {
    "conservative": {"push_score": 80, "short_push_score": 78, "watch_push_score": 72, "spike_push_score": 68, "forecast_push_score": 62},
    "standard": {"push_score": 75, "short_push_score": 75, "watch_push_score": 65, "spike_push_score": 60, "forecast_push_score": 58},
    "aggressive": {"push_score": 70, "short_push_score": 68, "watch_push_score": 62, "spike_push_score": 58, "forecast_push_score": 55},
}

RISK_PREFERENCE_LABELS = {
    "conservative": "保守",
    "standard": "标准",
    "aggressive": "激进",
}

CONFIG_FIELDS = [
    ("基础运行", "log_max_mb", "number", "单文件日志上限(MB)", "当前分卷达到此大小后轮转；默认500MB，适合约12小时写入量。"),
    ("基础运行", "log_total_max_mb", "number", "日志总容量上限(MB)", "所有分卷合计超过此值时自动删除最旧分卷；默认8192MB（8GB）。监控图表与压测依赖分析日志，建议保持开启。"),
    ("基础运行", "record_replay_enabled", "checkbox", "录制回放数据集", "监控运行时把每轮 collect_snapshot 原始输入写入 replay_dataset.jsonl，供离线回放压测。"),
    ("策略", "strategy_mode", "strategy_choice", "策略周期", "决定画像参数、主方向与评分周期：超短线/短线/中线/长线。与下方「轮询间隔」配合使用。"),
    ("策略", "interval", "number", "轮询间隔(秒)", "监控主循环 sleep 间隔；中线推荐 60，可改为 180/300 等。修改后需重启监控。"),
    ("策略", "risk_preference", "risk_choice", "确认严格度", "保守：更高动量阈值与确认分(88)；标准：默认平衡；激进（推荐）：更低阈值(65)且短线可凭短窗压力给方向。变更后可在下方推送说明中一键填入建议分数。"),
    ("AI与推送", "ai_enabled", "checkbox", "启用AI分析", "开启后除 L2/L3 事件触发外，还可按下方「定时 AI 间隔」固定复核；事件触发仍受策略冷却约束。", "left"),
    ("AI与推送", "push_enabled", "checkbox", "启用微信推送", "每轮最多 1 条；trade/spike/watch 须 AI 复核；演变/静默简报规则见各间隔配置；同币种最短间隔 10 分钟。", "right"),
    ("AI与推送", "ai_periodic_interval_minutes", "number", "定时 AI 间隔(分钟)", "启用 AI 后，每个监控币种按此间隔固定调用一次分析（无信号也会调）；0 表示关闭。默认 30。修改后需重启监控。", "left"),
    ("AI与推送", "wechat_silence_brief_minutes", "number", "静默简报间隔(分钟)", "启用后：监控启动/停止各推一次[简报]；距上次静默简报满此分钟后再发例行[简报]（与结构单/急变是否推送无关）。0=关闭。需 AI+微信推送。修改后需重启监控。", "right"),
    ("AI与推送", "push_score", "number", "做多推送门槛(trade)", "direction 为做多且 confidence ≥ 此值时可推 trade。建议见下方「推送分数建议」。", "right"),
    ("AI与推送", "short_push_score", "number", "做空推送门槛(trade)", "direction 为做空且 confidence ≥ 此值时可推 trade；标准模式建议与做多对称(75/75)。", "right"),
]

ENV_DEFAULTS = {
    "AI_MODEL": "",
    "AI_BASE_URL": "",
}

ENV_FIELDS = [
    ("OPENAI_API_KEY", "AI API Key", "在 DeepSeek、OpenAI 等平台申请的密钥；须与下方 Base URL 对应。"),
    ("AI_MODEL", "AI模型", "须与接口匹配，如 DeepSeek 用 deepseek-chat；留空则须自行填写。"),
    ("AI_BASE_URL", "AI Base URL", "OpenAI 兼容 Base URL；DeepSeek 填 https://api.deepseek.com。"),
    ("WECHAT_SEND_KEY", "微信推送 SendKey", "Server酱 SendKey，用于推送到个人微信。在 https://sct.ftqq.com 获取。"),
]
SAVED_AI_ENV_KEYS = ("OPENAI_API_KEY", "AI_API_KEY", "AI_BASE_URL", "AI_MODEL")


def default_config() -> Dict[str, Any]:
    return {
        "inst_ids": list(DEFAULT_MONITOR_INSTRUMENTS),
        "custom_inst_ids": [],
        "removed_inst_ids": [],
        "interval": 60,
        "log_max_mb": DEFAULT_LOG_MAX_BYTES // (1024 * 1024),
        "log_total_max_mb": DEFAULT_LOG_TOTAL_MAX_BYTES // (1024 * 1024),
        "analysis_log_enabled": True,
        "record_replay_enabled": True,
        "strategy_mode": "swing",
        "risk_preference": "aggressive",
        "signal_trade_enabled": True,
        "signal_spike_enabled": True,
        "ai_output_style": "steady",
        "allow_scalp_trade": False,
        "allow_counter_4h_scalp": False,
        "allow_oi_divergence_momentum": False,
        "scalp_move_pct_5m": 0.22,
        "scalp_move_pct_10m": 0.35,
        "ai_enabled": True,
        "ai_periodic_interval_minutes": 30,
        "wechat_silence_brief_minutes": 120,
        "dry_run_ai": False,
        "push_enabled": True,
        "push_score": 65,
        "short_push_score": 65,
        "volume_multiplier": 2.0,
        "oi_change_pct_15m": 5.0,
        "funding_abs_threshold": 0.0008,
        "funding_change_threshold": 0.0003,
        "long_short_extreme": 0.75,
        "retry_times": 3,
        "retry_backoff": 1.5,
        "push_cooldown_seconds": 900,
        "log_max_bytes": DEFAULT_LOG_MAX_BYTES,
        "log_total_max_bytes": DEFAULT_LOG_TOTAL_MAX_BYTES,
    }


def migrate_legacy_build_state() -> None:
    for source, target in (
        (LEGACY_CONFIG_FILE, PORTABLE_CONFIG_FILE),
        (LEGACY_ENV_FILE, PORTABLE_ENV_FILE),
        (LEGACY_AUTH_FILE, PORTABLE_AUTH_FILE),
    ):
        if not source.exists() or target.exists():
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
        except OSError:
            continue


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def active_config_file() -> Path:
    migrate_legacy_build_state()
    for path in (PORTABLE_CONFIG_FILE, USER_CONFIG_FILE, DEFAULT_CONFIG_FILE):
        if path.exists():
            return path
    return DEFAULT_CONFIG_FILE


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8-sig"))


def visible_config_keys() -> set:
    keys = {"inst_ids", "custom_inst_ids", "removed_inst_ids"}
    for item in CONFIG_FIELDS:
        _, key, _, _, _ = item[:5]
        keys.add(key)
    return keys


def suggested_push_scores(risk_preference: str) -> Dict[str, int]:
    risk = str(risk_preference or "standard").strip().lower()
    return dict(SUGGESTED_PUSH_SCORES.get(risk, SUGGESTED_PUSH_SCORES["standard"]))


def push_score_guide_text(risk_preference: str, ai_enabled: bool = False) -> str:
    risk = str(risk_preference or "standard").strip().lower()
    suggested = suggested_push_scores(risk)
    label = RISK_PREFERENCE_LABELS.get(risk, risk)
    ai_note = "已启用 AI：trade 建议可贴近下表。" if ai_enabled else "未启用 AI：建议 trade 取偏保守一档（如标准 75→78~80）。"
    gap = abs(int(suggested["push_score"]) - int(suggested["short_push_score"]))
    gap_note = ""
    if gap < 8:
        gap_note = f" 注意：做多/做空 trade 门槛差仅 {gap} 分（建议≥8），易压线做空泛滥。"
    return (
        f"当前严格度「{label}」建议 做多 {suggested['push_score']} · 做空 {suggested['short_push_score']}。"
        f"{ai_note} watch/spike/演变/冷却等推送细则已内置为固定默认值。"
        f"{gap_note}"
    )


def derive_ai_output_style(config: Dict[str, Any]) -> str:
    risk = str(config.get("risk_preference", "standard") or "standard")
    mode = str(config.get("strategy_mode", "short") or "short")
    if risk == "aggressive":
        return "momentum"
    if mode in ("swing", "long"):
        return "trend"
    return "steady"


def normalize_log_size_config(merged: Dict[str, Any], loaded: Dict[str, Any]) -> None:
    default_single_mb = DEFAULT_LOG_MAX_BYTES // (1024 * 1024)
    default_total_mb = DEFAULT_LOG_TOTAL_MAX_BYTES // (1024 * 1024)
    if "log_max_mb" in loaded:
        single_mb = int(loaded["log_max_mb"])
    elif "log_max_bytes" in loaded:
        single_mb = max(1, int(loaded["log_max_bytes"]) // (1024 * 1024))
    else:
        single_mb = default_single_mb
    if "log_total_max_mb" in loaded:
        total_mb = int(loaded["log_total_max_mb"])
    elif "log_total_max_bytes" in loaded:
        total_mb = max(1, int(loaded["log_total_max_bytes"]) // (1024 * 1024))
    else:
        total_mb = default_total_mb
    single_mb = max(50, min(4096, single_mb))
    total_mb = max(single_mb, min(16384, total_mb))
    merged["log_max_mb"] = single_mb
    merged["log_total_max_mb"] = total_mb
    merged["log_max_bytes"] = single_mb * 1024 * 1024
    merged["log_total_max_bytes"] = total_mb * 1024 * 1024
    merged["analysis_log_enabled"] = True


def normalize_inst_id(value: Any) -> str:
    return str(value or "").strip().upper()


def parse_inst_id_tokens(raw: Any) -> List[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        chunks = [str(item) for item in raw]
    else:
        chunks = re.split(r"[\s,;，；]+", str(raw))
    seen = set()
    ordered: List[str] = []
    for chunk in chunks:
        inst_id = normalize_inst_id(chunk)
        if not inst_id or inst_id in seen:
            continue
        seen.add(inst_id)
        ordered.append(inst_id)
    return ordered


def is_valid_inst_id_format(inst_id: str) -> bool:
    return bool(INST_ID_PATTERN.match(normalize_inst_id(inst_id)))


def order_configured_inst_ids(inst_ids: Any) -> List[str]:
    normalized = parse_inst_id_tokens(inst_ids)
    if not normalized:
        return []
    selected = set(normalized)
    ordered = [inst for inst in PRESET_INSTRUMENTS if inst in selected]
    for inst_id in normalized:
        if inst_id not in ordered:
            ordered.append(inst_id)
    return ordered


def okx_swap_instrument_exists(inst_id: str) -> bool:
    inst_id = normalize_inst_id(inst_id)
    if not is_valid_inst_id_format(inst_id):
        return False
    cached = _INST_VALIDATION_CACHE.get(inst_id)
    now = time.time()
    if cached and now - cached[0] < INST_VALIDATION_CACHE_TTL:
        return cached[1]
    query = urllib.parse.urlencode({"instType": "SWAP", "instId": inst_id})
    request = urllib.request.Request(
        f"{OKX_BASE_URL}/api/v5/public/instruments?{query}",
        headers={"Accept": "application/json", "User-Agent": "okx-ai-assistant-web/1.0"},
    )
    exists = False
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            payload = json.loads(response.read().decode("utf-8", errors="ignore"))
        if str(payload.get("code")) == "0":
            rows = payload.get("data") or []
            exists = any(str(row.get("instId", "")).upper() == inst_id for row in rows if isinstance(row, dict))
    except (OSError, urllib.error.URLError, json.JSONDecodeError, TypeError, ValueError):
        exists = False
    _INST_VALIDATION_CACHE[inst_id] = (now, exists)
    return exists


def validate_inst_ids(inst_ids: Any) -> Tuple[List[str], List[str]]:
    ordered = order_configured_inst_ids(inst_ids)
    valid: List[str] = []
    errors: List[str] = []
    for inst_id in ordered:
        if not is_valid_inst_id_format(inst_id):
            errors.append(f"{inst_id}：格式无效，应为类似 SOL-USDT-SWAP 的 OKX 永续合约 ID。")
            continue
        if not okx_swap_instrument_exists(inst_id):
            errors.append(f"{inst_id}：OKX 未找到该永续合约，请检查拼写或是否已下线。")
            continue
        valid.append(inst_id)
    return valid, errors


def format_inst_id_errors(errors: List[str]) -> str:
    if not errors:
        return ""
    if len(errors) == 1:
        return errors[0]
    return "以下合约无效：\n" + "\n".join(f"- {item}" for item in errors)


def parse_custom_inst_ids_from_form(form: Dict[str, Any]) -> List[str]:
    raw = form.get("custom_inst_ids", [])
    if isinstance(raw, str):
        raw = [raw]
    custom = order_configured_inst_ids(raw)
    return [inst for inst in custom if inst not in PRESET_INSTRUMENTS]


def parse_inst_ids_from_form(form: Dict[str, Any]) -> List[str]:
    checked = form.get("inst_ids", [])
    if isinstance(checked, str):
        checked = [checked]
    return order_configured_inst_ids(checked)


def resolve_configured_inst_ids(form: Dict[str, Any]) -> List[str]:
    candidate = parse_inst_ids_from_form(form)
    if not candidate:
        raise ValueError("至少选择一个监控币种。")
    valid, errors = validate_inst_ids(candidate)
    if errors:
        raise ValueError(format_inst_id_errors(errors))
    return valid


def resolve_custom_inst_ids(form: Dict[str, Any]) -> List[str]:
    candidate = parse_custom_inst_ids_from_form(form)
    if not candidate:
        return []
    valid, errors = validate_inst_ids(candidate)
    if errors:
        raise ValueError(format_inst_id_errors(errors))
    return valid


def validate_single_inst_id(inst_id: str, *, known_inst_ids: Optional[List[str]] = None) -> str:
    inst_id = normalize_inst_id(inst_id)
    if not inst_id:
        raise ValueError("请输入合约 ID，例如 SOL-USDT-SWAP。")
    known = set(known_inst_ids or [])
    if inst_id in known:
        raise ValueError(f"{inst_id} 已在列表中，直接勾选即可。")
    if inst_id in PRESET_INSTRUMENTS:
        return inst_id
    valid, errors = validate_inst_ids([inst_id])
    if errors:
        raise ValueError(errors[0])
    return valid[0]


def visible_inst_pool(config: Dict[str, Any]) -> List[str]:
    removed = set(order_configured_inst_ids(config.get("removed_inst_ids", [])))
    presets = [inst for inst in PRESET_INSTRUMENTS if inst not in removed]
    custom = order_configured_inst_ids(config.get("custom_inst_ids", []))
    custom = [inst for inst in custom if inst not in PRESET_INSTRUMENTS]
    pool: List[str] = []
    seen = set()
    for inst in [*presets, *custom]:
        if inst in seen:
            continue
        seen.add(inst)
        pool.append(inst)
    return pool


def inst_tile_html(inst_id: str, *, checked: bool) -> str:
    checked_attr = " checked" if checked else ""
    is_preset = inst_id in PRESET_INSTRUMENTS
    preset_flag = "1" if is_preset else "0"
    hidden = "" if is_preset else f'<input type="hidden" name="custom_inst_ids" value="{esc(inst_id)}">'
    return (
        f'<div class="check-tile check-tile-custom" data-inst="{esc(inst_id)}" data-preset="{preset_flag}">'
        f'<label><input type="checkbox" name="inst_ids" value="{esc(inst_id)}"{checked_attr}><span>{esc(inst_id)}</span></label>'
        f'<button class="inst-remove-btn" type="button" data-remove-inst="{esc(inst_id)}" aria-label="删除 {esc(inst_id)}">删除</button>'
        f"{hidden}</div>"
    )


def normalize_config(config: Dict[str, Any]) -> Dict[str, Any]:
    merged = default_config()
    visible = visible_config_keys()
    if isinstance(config, dict):
        for key, value in config.items():
            if key in visible:
                merged[key] = value
    defaults = default_config()
    for key, value in defaults.items():
        if key not in visible:
            merged[key] = value
    merged["ai_output_style"] = derive_ai_output_style(merged)
    loaded = config if isinstance(config, dict) else {}
    suggested = suggested_push_scores(str(merged.get("risk_preference", "standard")))
    if "push_score" not in loaded and "risk_preference" in loaded:
        merged["push_score"] = suggested["push_score"]
    if "short_push_score" not in loaded and "risk_preference" in loaded:
        merged["short_push_score"] = suggested.get("short_push_score", suggested["push_score"])
    merged["push_score"] = max(0, min(100, int(merged.get("push_score", suggested["push_score"]))))
    merged["short_push_score"] = max(0, min(100, int(merged.get("short_push_score", suggested.get("short_push_score", merged["push_score"])))))
    try:
        merged["ai_periodic_interval_minutes"] = max(
            0, min(1440, int(merged.get("ai_periodic_interval_minutes", 30) or 0))
        )
    except (TypeError, ValueError):
        merged["ai_periodic_interval_minutes"] = 30
    try:
        merged["wechat_silence_brief_minutes"] = max(
            0, min(1440, int(merged.get("wechat_silence_brief_minutes", 120) or 0))
        )
    except (TypeError, ValueError):
        merged["wechat_silence_brief_minutes"] = 120
    merged["allow_scalp_trade"] = merged.get("strategy_mode") == "scalp" or bool(merged.get("allow_scalp_trade"))
    merged["_interval_explicit"] = "interval" in loaded
    normalize_log_size_config(merged, loaded)
    inst_ids = order_configured_inst_ids(merged.get("inst_ids", [])) or list(DEFAULT_MONITOR_INSTRUMENTS)
    custom = order_configured_inst_ids(merged.get("custom_inst_ids", []))
    custom = [inst for inst in custom if inst not in PRESET_INSTRUMENTS]
    for inst in inst_ids:
        if inst not in PRESET_INSTRUMENTS and inst not in custom:
            custom.append(inst)
    removed = order_configured_inst_ids(merged.get("removed_inst_ids", []))
    removed = [inst for inst in removed if inst in PRESET_INSTRUMENTS]
    merged["inst_ids"] = inst_ids
    merged["custom_inst_ids"] = custom
    merged["removed_inst_ids"] = removed
    return strip_legacy_config_keys(sync_strategy_bound_config(merged))


def strip_legacy_config_keys(config: Dict[str, Any]) -> Dict[str, Any]:
    for key in LEGACY_CONFIG_KEYS:
        config.pop(key, None)
    return config


def load_config() -> Dict[str, Any]:
    loaded = load_json(active_config_file(), {})
    if not isinstance(loaded, dict):
        loaded = {}
    return normalize_config(loaded)


def configured_instruments() -> List[str]:
    return order_configured_inst_ids(load_config().get("inst_ids", []))


def analysis_log_enabled() -> bool:
    return True


def log_size_limits() -> Tuple[int, int]:
    config = load_config()
    single = int(config.get("log_max_bytes", DEFAULT_LOG_MAX_BYTES))
    total = int(config.get("log_total_max_bytes", DEFAULT_LOG_TOTAL_MAX_BYTES))
    return max(single, MIN_LOG_MAX_BYTES), max(total, single)


def realtime_log_tail_bytes() -> int:
    _, total_limit = log_size_limits()
    return min(max(REALTIME_LOG_TAIL_BYTES, MIN_LOG_MAX_BYTES), total_limit)


def accuracy_log_tail_bytes() -> int:
    _, total_limit = log_size_limits()
    return min(max(ACCURACY_LOG_TAIL_BYTES, MIN_LOG_MAX_BYTES), total_limit)


def console_log_backup_path(active_path: Path, index: int) -> Path:
    return active_path.with_name(f"{active_path.name}.{index}")


def list_console_log_segments(active_path: Path) -> List[Path]:
    backups: List[Path] = []
    index = 1
    while True:
        backup = console_log_backup_path(active_path, index)
        if backup.exists():
            backups.append(backup)
            index += 1
        else:
            break
    backups.reverse()
    if active_path.exists():
        return backups + [active_path]
    return backups


def console_log_total_bytes(active_path: Path) -> int:
    total = 0
    for path in list_console_log_segments(active_path):
        try:
            total += path.stat().st_size
        except OSError:
            continue
    return total


def _shift_console_log_backups(active_path: Path) -> None:
    index = 1
    while console_log_backup_path(active_path, index).exists():
        index += 1
    for slot in range(index - 1, 0, -1):
        src = console_log_backup_path(active_path, slot)
        dst = console_log_backup_path(active_path, slot + 1)
        if dst.exists():
            dst.unlink()
        src.replace(dst)


def rotate_console_log_if_needed(
    active_path: Path,
    max_bytes: int = CONSOLE_LOG_MAX_BYTES,
    total_max_bytes: int = CONSOLE_LOG_TOTAL_MAX_BYTES,
) -> None:
    max_bytes = max(int(max_bytes), 1024 * 1024)
    total_max_bytes = max(int(total_max_bytes), max_bytes)
    try:
        active_path.parent.mkdir(parents=True, exist_ok=True)
        if active_path.exists() and active_path.stat().st_size >= max_bytes:
            backup1 = console_log_backup_path(active_path, 1)
            if backup1.exists():
                _shift_console_log_backups(active_path)
            active_path.replace(backup1)
        while console_log_total_bytes(active_path) > total_max_bytes:
            segments = list_console_log_segments(active_path)
            if not segments:
                return
            oldest = segments[0]
            if len(segments) == 1 and oldest.resolve() == active_path.resolve():
                return
            oldest.unlink()
    except OSError:
        pass


def log_file_cache_token(path: Path) -> str:
    try:
        if not path.exists():
            return "0:0"
        stat = path.stat()
        return f"{stat.st_mtime_ns}:{stat.st_size}"
    except OSError:
        return "0:0"


def cached_api_response(cache_key: str, loader) -> Dict[str, Any]:
    now = time.time()
    cached = _API_RESPONSE_CACHE.get(cache_key)
    if cached and now - cached[0] < API_RESPONSE_CACHE_TTL_SECONDS:
        return cached[1]
    payload = loader()
    _API_RESPONSE_CACHE[cache_key] = (now, payload)
    if len(_API_RESPONSE_CACHE) > 256:
        stale_before = now - API_RESPONSE_CACHE_TTL_SECONDS * 4
        for key, (saved_at, _) in list(_API_RESPONSE_CACHE.items()):
            if saved_at < stale_before:
                _API_RESPONSE_CACHE.pop(key, None)
    return payload


def session_expires_at(now: float = None) -> float:
    return (now or time.time()) + SESSION_TTL_SECONDS


def prune_expired_sessions(now: float = None) -> None:
    now = now or time.time()
    for token, expires in list(SESSIONS.items()):
        created = SESSION_CREATED.get(token, 0.0)
        if expires <= now or (created and now - created > SESSION_ABSOLUTE_TTL_SECONDS):
            revoke_session(token)
    prune_inst_validation_cache(now)


def register_session(token: str) -> None:
    prune_expired_sessions()
    now = time.time()
    SESSIONS[token] = session_expires_at(now)
    SESSION_CREATED[token] = now


def is_valid_session(token: Optional[str], *, refresh: bool = True) -> bool:
    if not token:
        return False
    prune_expired_sessions()
    expires = SESSIONS.get(token)
    created = SESSION_CREATED.get(token, 0.0)
    if expires is None or expires <= time.time():
        revoke_session(token)
        return False
    if created and time.time() - created > SESSION_ABSOLUTE_TTL_SECONDS:
        revoke_session(token)
        return False
    if refresh:
        SESSIONS[token] = session_expires_at()
    return True


def revoke_session(token: Optional[str]) -> None:
    if token:
        SESSIONS.pop(token, None)
        SESSION_CREATED.pop(token, None)


def prune_inst_validation_cache(now: float = None) -> None:
    now = now or time.time()
    stale = [
        inst_id
        for inst_id, (saved_at, _) in _INST_VALIDATION_CACHE.items()
        if now - saved_at >= INST_VALIDATION_CACHE_TTL
    ]
    for inst_id in stale:
        _INST_VALIDATION_CACHE.pop(inst_id, None)


def write_child_pid(path: Path, pid: int) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(int(pid)), encoding="utf-8")
    except OSError:
        pass


def clear_child_pid(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def read_child_pid(path: Path) -> Optional[int]:
    try:
        if not path.exists():
            return None
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            output = (result.stdout or "").strip()
            return str(pid) in output and "No tasks are running" not in output
        except (OSError, subprocess.TimeoutExpired):
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def terminate_pid(pid: int, *, fast: bool = False) -> None:
    if not process_alive(pid):
        return
    if os.name == "nt":
        args = ["taskkill", "/PID", str(pid), "/T"]
        if fast:
            args.append("/F")
        try:
            subprocess.run(args, capture_output=True, timeout=8, check=False)
        except (OSError, subprocess.TimeoutExpired):
            pass
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    if fast:
        time.sleep(0.5)
        if process_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass


def cleanup_stale_child_processes() -> None:
    """Remove pid files for dead child processes. Alive processes are left running (7x24 headless)."""
    for pid_file in (MONITOR_PID_FILE, REPLAY_PID_FILE):
        pid = read_child_pid(pid_file)
        if pid is None:
            continue
        if process_alive(pid):
            continue
        clear_child_pid(pid_file)


def maybe_send_monitor_crash_alert(message: str) -> None:
    if not WEB_MONITOR_CRASH_ALERT:
        return
    send_key = build_child_env().get("WECHAT_SEND_KEY", "").strip()
    if not send_key:
        return
    title = "[OKX AI助手] 监控进程异常退出"
    try:
        post_json(
            f"https://sctapi.ftqq.com/{send_key}.send",
            {"title": title, "desp": message},
        )
    except Exception:
        pass


def can_auto_restart_monitor() -> bool:
    if not WEB_MONITOR_AUTO_RESTART:
        return False
    if WEB_MONITOR_MAX_AUTO_RESTARTS <= 0:
        return False
    now = time.time()
    window_start = now - WEB_MONITOR_AUTO_RESTART_WINDOW_SECONDS
    recent = [stamp for stamp in _MONITOR_AUTO_RESTART_AT if stamp >= window_start]
    _MONITOR_AUTO_RESTART_AT[:] = recent
    return len(recent) < WEB_MONITOR_MAX_AUTO_RESTARTS


def handle_monitor_unexpected_exit(exit_code: int) -> bool:
    global MONITOR_PROCESS, MONITOR_LAST_EXIT_UNEXPECTED
    MONITOR_LAST_EXIT_UNEXPECTED = True
    message = (
        f"监控进程意外退出\n"
        f"退出码: {exit_code}\n"
        f"启动时间: {MONITOR_STARTED_AT or '-'}\n"
        f"停止时间: {MONITOR_STOPPED_AT or '-'}\n"
        f"日志: {MONITOR_PROCESS_LOG_FILE}"
    )
    try:
        with MONITOR_PROCESS_LOG_FILE.open("a", encoding="utf-8") as log_file:
            log_file.write(f"\n===== signal monitor crashed at {MONITOR_STOPPED_AT} exit_code={exit_code} =====\n")
    except OSError:
        pass
    maybe_send_monitor_crash_alert(message)
    MONITOR_PROCESS = None
    clear_child_pid(MONITOR_PID_FILE)
    if can_auto_restart_monitor():
        _MONITOR_AUTO_RESTART_AT.append(time.time())
        restarted = start_monitor()
        try:
            with MONITOR_PROCESS_LOG_FILE.open("a", encoding="utf-8") as log_file:
                log_file.write(f"===== auto restart result: {restarted} =====\n")
        except OSError:
            pass
        return True
    return False


def log_size_summary_text(config: Dict[str, Any] = None) -> str:
    cfg = normalize_config(config or load_config())
    segments = len(list_analysis_log_segments(MONITOR_JSON_LOG_FILE))
    used_mb = analysis_log_total_bytes(MONITOR_JSON_LOG_FILE) / (1024 * 1024)
    return (
        f"单文件 {cfg.get('log_max_mb', 500)}MB · 总容量 {cfg.get('log_total_max_mb', 8192)}MB"
        f" · 当前约 {used_mb:.0f}MB / {segments} 卷"
    )


def save_config(config: Dict[str, Any]) -> Tuple[Path, bool]:
    before = load_config()
    normalized = normalize_config(config)
    visible = visible_config_keys()
    to_save = {key: normalized[key] for key in visible if key in normalized}
    text = json.dumps(to_save, indent=2, ensure_ascii=False) + "\n"
    requires_restart = config_requires_monitor_restart(before, normalized)
    try:
        PORTABLE_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        PORTABLE_CONFIG_FILE.write_text(text, encoding="utf-8")
        if USER_CONFIG_FILE.exists():
            USER_CONFIG_FILE.write_text(text, encoding="utf-8")
        return PORTABLE_CONFIG_FILE, requires_restart
    except PermissionError:
        USER_STATE_DIR.mkdir(parents=True, exist_ok=True)
        USER_CONFIG_FILE.write_text(text, encoding="utf-8")
        return USER_CONFIG_FILE, requires_restart


def ensure_portable_auth_seed() -> None:
    if PORTABLE_AUTH_FILE.exists():
        return
    if not DEFAULT_AUTH_FILE.exists():
        return
    try:
        PORTABLE_AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
        PORTABLE_AUTH_FILE.write_text(DEFAULT_AUTH_FILE.read_text(encoding="utf-8"), encoding="utf-8")
    except OSError:
        pass


def load_auth() -> Dict[str, str]:
    migrate_legacy_build_state()
    ensure_portable_auth_seed()
    defaults = {"username": "admin", "password": "admin123"}
    for path in (PORTABLE_AUTH_FILE, DEFAULT_AUTH_FILE, USER_AUTH_FILE):
        if path.exists():
            data = load_json(path, defaults)
            if isinstance(data, dict):
                return {
                    "username": str(data.get("username") or defaults["username"]).strip() or defaults["username"],
                    "password": str(data.get("password") or defaults["password"]),
                }
    return dict(defaults)


def save_auth(username: str, password: str) -> Path:
    data = {
        "username": username.strip() or "admin",
        "password": password if password is not None else "admin123",
    }
    if not str(data["password"]):
        raise ValueError("密码不能为空")
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    saved_paths: List[Path] = []
    for path in (PORTABLE_AUTH_FILE, USER_AUTH_FILE):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            saved_paths.append(path)
        except OSError:
            continue
    if not saved_paths:
        raise PermissionError("无法写入登录配置文件，请检查 local_state 目录权限。")
    return saved_paths[0]


def update_auth_from_form(form: Dict[str, Any]) -> Tuple[str, Path]:
    auth = load_auth()
    current_password = str(form.get("auth_current_password", "")).strip()
    if not current_password:
        raise ValueError("请先输入当前密码以确认身份。")
    if current_password != auth.get("password"):
        raise ValueError("当前密码不正确。")
    username = str(form.get("auth_username", auth.get("username", "admin"))).strip() or "admin"
    raw_new_password = str(form.get("auth_password", "")).strip()
    password = auth.get("password", "admin123") if not raw_new_password else raw_new_password
    if not str(password):
        raise ValueError("新密码不能为空。")
    saved_path = save_auth(username, password)
    service_note = stop_all_background_services()
    return (
        f"登录账号已更新。{service_note} 请使用新凭据重新登录。",
        saved_path,
    )


def stop_all_background_services(*, fast: bool = False) -> str:
    notes = [stop_replay(fast=fast), stop_monitor(fast=fast)]
    return " ".join(note for note in notes if note)


WEB_RESTART_DELAY_SECONDS = 2.5
WEB_SKIP_BROWSER_ENV = "OKX_WEB_SKIP_BROWSER"
TRAY_LAUNCH_ENV = "OKX_LAUNCHED_BY_TRAY"
EXIT_CODE_TRAY_SHUTDOWN = 100
EXIT_CODE_TRAY_RESTART = 101


def launched_by_tray() -> bool:
    return os.getenv(TRAY_LAUNCH_ENV, "").strip().lower() in ("1", "true", "yes")


def should_auto_open_browser() -> bool:
    return os.getenv(WEB_SKIP_BROWSER_ENV, "").strip().lower() not in ("1", "true", "yes")


def stash_restart_sessions() -> None:
    if not SESSIONS:
        return
    try:
        PORTABLE_STATE_DIR.mkdir(parents=True, exist_ok=True)
        WEB_RESTART_SESSIONS_FILE.write_text(
            json.dumps(sorted(SESSIONS.keys()), ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        pass


def restore_restart_sessions() -> None:
    try:
        if not WEB_RESTART_SESSIONS_FILE.exists():
            return
        payload = json.loads(WEB_RESTART_SESSIONS_FILE.read_text(encoding="utf-8"))
        if isinstance(payload, list):
            for token in payload:
                if isinstance(token, str) and token:
                    register_session(token)
                    SESSION_CREATED[token] = time.time()
    except (OSError, json.JSONDecodeError, TypeError):
        pass
    finally:
        try:
            WEB_RESTART_SESSIONS_FILE.unlink(missing_ok=True)
        except OSError:
            pass


def _spawn_in_new_console(command: str, *, cwd: Path) -> None:
    subprocess.Popen(
        command,
        shell=True,
        cwd=str(cwd),
        close_fds=True,
    )


def spawn_web_control_panel_restart() -> None:
    script = SCRIPT_DIR / "web_control_panel.py"
    python_exe = sys.executable
    start_bat = SCRIPT_DIR / "start_web_control_panel_windows.bat"
    wait_ticks = max(3, int(WEB_RESTART_DELAY_SECONDS) + 2)

    if os.name == "nt":
        if start_bat.exists():
            inner = (
                f'set {WEB_SKIP_BROWSER_ENV}=1&& '
                f'ping 127.0.0.1 -n {wait_ticks} >nul&& '
                f'"{start_bat}"'
            )
            _spawn_in_new_console(
                f'start "OKX AI Web" /D "{SCRIPT_DIR}" cmd /c "{inner}"',
                cwd=SCRIPT_DIR,
            )
            return
        inner = (
            f'set {WEB_SKIP_BROWSER_ENV}=1&& '
            f'ping 127.0.0.1 -n {wait_ticks} >nul&& '
            f'"{python_exe}" "{script}"'
        )
        _spawn_in_new_console(
            f'start "OKX AI Web" /D "{SCRIPT_DIR}" cmd /c "{inner}"',
            cwd=SCRIPT_DIR,
        )
        return
    env = os.environ.copy()
    env[WEB_SKIP_BROWSER_ENV] = "1"
    subprocess.Popen(
        [python_exe, str(script)],
        cwd=str(SCRIPT_DIR),
        env=env,
        start_new_session=True,
        close_fds=True,
    )


def _power_action_worker(*, restart: bool) -> None:
    global WEB_POWER_SHUTDOWN, WEB_POWER_RESTART
    if restart:
        WEB_POWER_RESTART = True
        if not launched_by_tray():
            try:
                spawn_web_control_panel_restart()
            except Exception:
                pass
        stash_restart_sessions()
    else:
        WEB_POWER_SHUTDOWN = True
    time.sleep(0.4)
    try:
        stop_all_background_services(fast=True)
    except Exception:
        pass
    server = WEB_SERVER
    if server is not None:
        server.shutdown()
    elif not restart:
        os._exit(0)


def request_power_restart() -> Dict[str, Any]:
    threading.Thread(target=_power_action_worker, kwargs={"restart": True}, daemon=True).start()
    return {
        "ok": True,
        "action": "restart",
        "message": "正在停止服务并重启 Web 控制台…",
    }


def request_power_shutdown() -> Dict[str, Any]:
    threading.Thread(target=_power_action_worker, kwargs={"restart": False}, daemon=True).start()
    return {
        "ok": True,
        "action": "shutdown",
        "message": "正在停止服务并关闭 Web 控制台…",
    }


def clear_session_token(token: Optional[str]) -> None:
    revoke_session(token)


def load_env() -> Dict[str, str]:
    env: Dict[str, str] = {}
    migrate_legacy_build_state()
    for path in (PORTABLE_ENV_FILE, USER_ENV_FILE):
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip().strip('"').strip("'")
    if not env.get("WECHAT_SEND_KEY") and env.get("WECHAT_WEBHOOK_URL"):
        env["WECHAT_SEND_KEY"] = env["WECHAT_WEBHOOK_URL"]
    return env


def save_env(env: Dict[str, str]) -> Path:
    lines = [
        "# OKX AI短线助手环境变量配置",
        "",
        f'OPENAI_API_KEY="{env.get("OPENAI_API_KEY", "")}"',
        f'AI_MODEL="{env.get("AI_MODEL", ENV_DEFAULTS["AI_MODEL"])}"',
        f'AI_BASE_URL="{env.get("AI_BASE_URL", ENV_DEFAULTS["AI_BASE_URL"])}"',
        f'WECHAT_SEND_KEY="{env.get("WECHAT_SEND_KEY", "")}"',
        "",
    ]
    text = "\n".join(lines)
    try:
        PORTABLE_ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
        PORTABLE_ENV_FILE.write_text(text, encoding="utf-8")
        return PORTABLE_ENV_FILE
    except PermissionError:
        USER_STATE_DIR.mkdir(parents=True, exist_ok=True)
        USER_ENV_FILE.write_text(text, encoding="utf-8")
        return USER_ENV_FILE


def parse_cookies(header: str) -> Dict[str, str]:
    cookies = {}
    for item in header.split(";"):
        if "=" in item:
            key, value = item.split("=", 1)
            cookies[key.strip()] = value.strip()
    return cookies


def parse_bool(form: Dict[str, Any], key: str) -> bool:
    return key in form


def parse_removed_inst_ids_from_form(form: Dict[str, Any]) -> List[str]:
    raw = form.get("removed_inst_ids", [])
    if isinstance(raw, str):
        raw = [raw]
    return [inst for inst in order_configured_inst_ids(raw) if inst in PRESET_INSTRUMENTS]


def update_from_form(form: Dict[str, Any]) -> Tuple[Path, Path, bool]:
    config = load_config()
    env = load_env()
    config["inst_ids"] = resolve_configured_inst_ids(form)
    config["custom_inst_ids"] = resolve_custom_inst_ids(form)
    config["removed_inst_ids"] = parse_removed_inst_ids_from_form(form)

    for item in CONFIG_FIELDS:
        _, key, kind, _, _ = item[:5]
        if kind == "checkbox":
            config[key] = parse_bool(form, key)
        elif kind in ("choice", "strategy_choice", "risk_choice", "ai_style_choice"):
            config[key] = str(form.get(key, config.get(key, "0")))
        else:
            raw = str(form.get(key, "")).strip()
            old = config.get(key)
            config[key] = float(raw) if isinstance(old, float) or "." in raw else int(raw)

    for key, _, _ in ENV_FIELDS:
        env[key] = str(form.get(f"env_{key}", "")).strip()
    saved_path, requires_restart = save_config(config)
    env_path = save_env(env)
    return saved_path, env_path, requires_restart


def restore_factory_default_config() -> Tuple[Path, bool]:
    """Reset user-visible config to factory defaults; env keys are untouched."""
    before = load_config()
    fresh = normalize_config({})
    visible = visible_config_keys()
    to_save = {key: fresh[key] for key in visible if key in fresh}
    saved_path, _ = save_config(to_save)
    requires_restart = config_requires_monitor_restart(before, fresh)
    return saved_path, requires_restart


def verify_current_password(password: str) -> None:
    current = str(password or "").strip()
    if not current:
        raise ValueError("请先输入当前密码以确认身份。")
    auth = load_auth()
    if current != str(auth.get("password", "")):
        raise ValueError("当前密码不正确。")


def _remove_path(path: Path) -> bool:
    try:
        if path.is_file() or path.is_symlink():
            path.unlink(missing_ok=True)
            return True
        if path.is_dir():
            shutil.rmtree(path)
            return True
    except OSError:
        return False
    return False


def clear_runtime_generated_files() -> List[str]:
    cleared: List[str] = []
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    for child in list(LOG_DIR.iterdir()):
        if _remove_path(child):
            cleared.append(str(child))
    runtime_state_files = (
        MONITOR_PID_FILE,
        REPLAY_PID_FILE,
        WEB_RESTART_SESSIONS_FILE,
        PORTABLE_STATE_DIR / "tray_launcher.pid",
        PORTABLE_STATE_DIR / "tray_launcher.log",
    )
    for path in runtime_state_files:
        if path.exists() and _remove_path(path):
            cleared.append(str(path))
    if LEGACY_STATE_DIR.exists():
        for child in list(LEGACY_STATE_DIR.iterdir()):
            if _remove_path(child):
                cleared.append(str(child))
    try:
        reset_ai_token_stats(AI_TOKEN_STATS_FILE)
    except Exception:
        pass
    return cleared


def restore_default_env() -> Path:
    env = {key: "" for key, _, _ in ENV_FIELDS}
    env["AI_MODEL"] = ENV_DEFAULTS.get("AI_MODEL", "")
    env["AI_BASE_URL"] = ENV_DEFAULTS.get("AI_BASE_URL", "")
    lines = [
        "# OKX AI短线助手环境变量配置",
        "",
        f'OPENAI_API_KEY="{env.get("OPENAI_API_KEY", "")}"',
        f'AI_MODEL="{env.get("AI_MODEL", ENV_DEFAULTS["AI_MODEL"])}"',
        f'AI_BASE_URL="{env.get("AI_BASE_URL", ENV_DEFAULTS["AI_BASE_URL"])}"',
        f'WECHAT_SEND_KEY="{env.get("WECHAT_SEND_KEY", "")}"',
        "",
    ]
    text = "\n".join(lines)
    saved_paths: List[Path] = []
    for path in (PORTABLE_ENV_FILE, USER_ENV_FILE):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            saved_paths.append(path)
        except OSError:
            continue
    if not saved_paths:
        raise PermissionError("无法写入环境变量配置文件，请检查 local_state 目录权限。")
    _remove_path(LEGACY_ENV_FILE)
    return saved_paths[0]


def restore_default_auth() -> Path:
    defaults = load_json(DEFAULT_AUTH_FILE, {"username": "admin", "password": "admin123"})
    username = str(defaults.get("username") or "admin").strip() or "admin"
    password = str(defaults.get("password") or "admin123")
    return save_auth(username, password)


def revoke_all_sessions() -> None:
    for token in list(SESSIONS.keys()):
        revoke_session(token)


def perform_factory_reset(password: str) -> Dict[str, Any]:
    verify_current_password(password)
    stop_note = stop_all_background_services(fast=True)
    cleared = clear_runtime_generated_files()
    config_path, _ = restore_factory_default_config()
    env_path = restore_default_env()
    auth_path = restore_default_auth()
    revoke_all_sessions()
    _API_RESPONSE_CACHE.clear()
    _CANDLE_CACHE.clear()
    _DATASET_STATS_CACHE.clear()
    _LOG_LINE_COUNT_CACHE.clear()
    _REALTIME_POINTS_CACHE.clear()
    return {
        "ok": True,
        "message": (
            f"已恢复出厂设置。{stop_note} "
            "请使用默认账号 admin / admin123 重新登录。"
        ),
        "config_path": str(config_path),
        "env_path": str(env_path),
        "auth_path": str(auth_path),
        "cleared_paths": len(cleared),
        "redirect": "/login?factory_reset=1",
    }


def export_config_bundle(name: str = "") -> Dict[str, Any]:
    config = load_config()
    visible = visible_config_keys()
    return {
        "name": name or "OKX_AI_Config",
        "version": "1.0",
        "config": {key: config[key] for key in visible if key in config},
        "env": load_env(),
    }


def import_config_bundle(bundle: Dict[str, Any]) -> None:
    config = bundle.get("config", bundle)
    env = bundle.get("env", {})
    save_config(config)
    if isinstance(env, dict):
        save_env(env)


def build_child_env() -> Dict[str, str]:
    child_env = os.environ.copy()
    child_env["PYTHONUNBUFFERED"] = "1"
    for key, value in load_env().items():
        child_env[key] = value
    return child_env


@contextmanager
def use_saved_env():
    """Apply config-page env vars to the current process (same as monitor subprocess)."""
    child = build_child_env()
    backup = {key: os.environ[key] for key in SAVED_AI_ENV_KEYS if key in os.environ}
    try:
        for key in SAVED_AI_ENV_KEYS:
            if key in child:
                os.environ[key] = child[key]
        yield
    finally:
        for key in SAVED_AI_ENV_KEYS:
            if key in backup:
                os.environ[key] = backup[key]
            elif key in os.environ:
                del os.environ[key]


def build_monitor_args(config: Dict[str, Any]) -> List[str]:
    config = normalize_config(config)
    args = [
        sys.executable,
        str(SCRIPT_DIR / "okx_signal_monitor.py"),
        "--inst-ids",
        ",".join(config.get("inst_ids", [])),
        "--interval",
        str(config.get("interval", 5)),
        "--runtime",
        str(FIXED_MONITOR_RUNTIME),
        "--flag",
        FIXED_OKX_FLAG,
        "--push-score",
        str(config.get("push_score", 75)),
        "--short-push-score",
        str(config.get("short_push_score", config.get("push_score", 75))),
        "--retry-times",
        str(config.get("retry_times", 3)),
        "--retry-backoff",
        str(config.get("retry_backoff", 1.5)),
        "--push-cooldown",
        str(config_value(config, "push_cooldown_seconds")),
        "--spike-push-cooldown",
        str(config_value(config, "spike_push_cooldown_seconds")),
        "--watch-push-cooldown",
        str(config_value(config, "watch_push_cooldown_seconds")),
        "--reverse-trade-cooldown",
        str(config_value(config, "reverse_trade_cooldown_seconds")),
        "--forecast-push-cooldown",
        str(config_value(config, "forecast_push_cooldown_seconds")),
        "--log-max-bytes",
        str(config.get("log_max_bytes", DEFAULT_LOG_MAX_BYTES)),
        "--log-total-max-bytes",
        str(config.get("log_total_max_bytes", DEFAULT_LOG_TOTAL_MAX_BYTES)),
        "--volume-multiplier",
        str(config.get("volume_multiplier", 2.0)),
        "--oi-change-pct-15m",
        str(config.get("oi_change_pct_15m", 5.0)),
        "--funding-threshold",
        str(config.get("funding_abs_threshold", 0.0008)),
        "--funding-change-threshold",
        str(config.get("funding_change_threshold", 0.0003)),
        "--long-short-extreme",
        str(config.get("long_short_extreme", 0.75)),
        "--strategy-mode",
        str(config.get("strategy_mode", "short")),
        "--risk-preference",
        str(config.get("risk_preference", "standard")),
        "--ai-output-style",
        str(config.get("ai_output_style", "steady")),
        "--scalp-move-pct-5m",
        str(config.get("scalp_move_pct_5m", 0.22)),
        "--scalp-move-pct-10m",
        str(config.get("scalp_move_pct_10m", 0.35)),
        "--watch-push-score",
        str(config_value(config, "watch_push_score")),
        "--spike-push-score",
        str(config_value(config, "spike_push_score")),
        "--forecast-push-score",
        str(config_value(config, "forecast_push_score")),
        "--forecast-horizon-minutes",
        str(config_value(config, "forecast_horizon_minutes")),
    ]
    if config_value(config, "calibration_enabled"):
        args.append("--calibration")
    else:
        args.append("--no-calibration")
    args.extend(
        [
            "--calibration-min-samples",
            str(config_value(config, "calibration_min_samples")),
            "--calibration-blend-weight",
            str(config_value(config, "calibration_blend_weight")),
            "--calibration-disable-below-hit-rate",
            str(config_value(config, "calibration_disable_below_hit_rate")),
        ]
    )
    if config_value(config, "signal_forecast_enabled"):
        args.append("--forecast-alerts")
    else:
        args.append("--no-forecast-alerts")
    if config_value(config, "ai_conflict_guard"):
        args.append("--ai-conflict-guard")
    else:
        args.append("--no-ai-conflict-guard")
    if config_value(config, "l3_local_spike_push"):
        args.append("--l3-local-spike-push")
    else:
        args.append("--no-l3-local-spike-push")
    if config_value(config, "l2_require_volume_or_structure"):
        args.append("--l2-require-volume-or-structure")
    else:
        args.append("--no-l2-require-volume-or-structure")
    args.append("--trade-signals" if config.get("signal_trade_enabled", True) else "--no-trade-signals")
    args.append("--watch-signals" if config_value(config, "signal_watch_enabled") else "--no-watch-signals")
    args.append("--spike-alerts" if config.get("signal_spike_enabled", True) else "--no-spike-alerts")
    if config.get("allow_scalp_trade"):
        args.append("--allow-scalp-trade")
    if config.get("allow_counter_4h_scalp"):
        args.append("--allow-counter-4h-scalp")
    if config.get("allow_oi_divergence_momentum"):
        args.append("--allow-oi-divergence-momentum")
    if config.get("ai_enabled"):
        args.append("--ai")
    if config.get("dry_run_ai"):
        args.append("--dry-run-ai")
    if config.get("push_enabled"):
        args.append("--push")
    args.extend(
        [
            "--ai-periodic-interval-minutes",
            str(config_value(config, "ai_periodic_interval_minutes")),
            "--wechat-silence-brief-minutes",
            str(config_value(config, "wechat_silence_brief_minutes")),
        ]
    )
    if config.get("record_replay_enabled"):
        args.extend(["--record-replay", "--record-replay-file", str(REPLAY_DATASET_FILE)])
    if config_value(config, "paper_follow_ai_only"):
        args.append("--paper-follow-ai-only")
    else:
        args.append("--no-paper-follow-ai-only")
    args.extend(["--paper-fee-bps", str(config_value(config, "paper_fee_bps"))])
    if config_value(config, "forward_require_forecast_alignment"):
        args.append("--forward-require-forecast-alignment")
    else:
        args.append("--no-forward-require-forecast-alignment")
    if config_value(config, "replay_ai_cache_enabled"):
        args.append("--replay-ai-cache")
    else:
        args.append("--no-replay-ai-cache")
    args.append("--analysis-log")
    return args


def build_replay_args(config: Dict[str, Any], replay_interval: float, dataset_path: Path = None) -> List[str]:
    args = build_monitor_args(config)
    dataset = dataset_path or REPLAY_DATASET_FILE
    args.extend(
        [
            "--replay-file",
            str(dataset),
            "--replay-interval",
            str(max(0.0, float(replay_interval))),
            "--replay-log-file",
            str(REPLAY_ANALYSIS_LOG_FILE),
        ]
    )
    return args


def replay_status() -> Dict[str, Any]:
    global REPLAY_PROCESS, REPLAY_STOPPED_AT
    elapsed_seconds = 0
    if REPLAY_STARTED_AT:
        try:
            start_time = parse_history_time(REPLAY_STARTED_AT)
            end_time = parse_history_time(REPLAY_STOPPED_AT) if REPLAY_STOPPED_AT else datetime.now()
            elapsed_seconds = max(0, int((end_time - start_time).total_seconds()))
        except Exception:
            elapsed_seconds = 0
    if REPLAY_PROCESS is None:
        return {"running": False, "text": "未回放", "started_at": "", "elapsed_seconds": 0}
    code = REPLAY_PROCESS.poll()
    if code is None:
        return {"running": True, "text": f"回放中 PID={REPLAY_PROCESS.pid}", "started_at": REPLAY_STARTED_AT, "elapsed_seconds": elapsed_seconds}
    if not REPLAY_STOPPED_AT:
        REPLAY_STOPPED_AT = time.strftime("%Y-%m-%d %H:%M:%S")
        return replay_status()
    return {"running": False, "text": f"回放已结束，退出码={code}", "started_at": REPLAY_STARTED_AT, "elapsed_seconds": elapsed_seconds}


def start_replay(replay_interval: float = 0.5, dataset_path: Path = None) -> str:
    global REPLAY_PROCESS, REPLAY_STARTED_AT, REPLAY_LOG_START_AT, REPLAY_STOPPED_AT
    if monitor_status()["running"]:
        return "请先停止正式监控，再启动离线回放。"
    status = replay_status()
    if status["running"]:
        return status["text"]
    dataset = dataset_path or REPLAY_DATASET_FILE
    stats = replay_dataset_stats(dataset)
    if not stats.get("exists") or not stats.get("frame_count"):
        return f"未找到可回放数据集：{dataset}。请先在配置页勾选「录制回放数据集」并运行监控。"
    REPLAY_STARTED_AT = time.strftime("%Y-%m-%d %H:%M:%S")
    REPLAY_LOG_START_AT = REPLAY_STARTED_AT
    REPLAY_STOPPED_AT = ""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    rotate_console_log_if_needed(REPLAY_PROCESS_LOG_FILE)
    if REPLAY_ANALYSIS_LOG_FILE.exists():
        REPLAY_ANALYSIS_LOG_FILE.unlink()
    _LOG_LINE_COUNT_CACHE.pop(str(REPLAY_ANALYSIS_LOG_FILE), None)
    log_file = REPLAY_PROCESS_LOG_FILE.open("a", encoding="utf-8")
    log_file.write(f"\n===== replay started at {REPLAY_STARTED_AT} dataset={dataset} =====\n")
    log_file.flush()
    config = load_config()
    REPLAY_PROCESS = subprocess.Popen(
        build_replay_args(config, replay_interval, dataset),
        cwd=str(SCRIPT_DIR),
        env=build_child_env(),
        stdout=log_file,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    log_file.close()
    write_child_pid(REPLAY_PID_FILE, REPLAY_PROCESS.pid)
    return f"回放已启动，PID={REPLAY_PROCESS.pid}，数据集 {stats.get('frame_count')} 帧"


def stop_replay(*, fast: bool = False) -> str:
    global REPLAY_PROCESS, REPLAY_STOPPED_AT, REPLAY_STOP_REQUESTED
    status = replay_status()
    if not status["running"]:
        return "回放未运行。"
    REPLAY_STOP_REQUESTED = True
    wait_timeout = 1.5 if fast else 8
    kill_wait = 1.0 if fast else 5
    REPLAY_PROCESS.terminate()
    try:
        REPLAY_PROCESS.wait(timeout=wait_timeout)
    except subprocess.TimeoutExpired:
        REPLAY_PROCESS.kill()
        REPLAY_PROCESS.wait(timeout=kill_wait)
    REPLAY_STOPPED_AT = time.strftime("%Y-%m-%d %H:%M:%S")
    REPLAY_PROCESS = None
    clear_child_pid(REPLAY_PID_FILE)
    try:
        with REPLAY_PROCESS_LOG_FILE.open("a", encoding="utf-8") as log_file:
            log_file.write(f"\n===== replay stopped at {REPLAY_STOPPED_AT} =====\n")
    except OSError:
        pass
    REPLAY_STOP_REQUESTED = False
    return "回放已停止。"


def count_nonempty_lines(path: Path, *, fast: bool = False) -> int:
    if not path.exists():
        return 0
    try:
        stat = path.stat()
    except OSError:
        return 0
    cache_key = str(path)
    if fast and replay_status()["running"]:
        return max(0, int(stat.st_size / 1200))
    cached = _LOG_LINE_COUNT_CACHE.get(cache_key)
    if cached and cached[0] == stat.st_mtime:
        return cached[1]
    count = 0
    with path.open("r", encoding="utf-8-sig") as file:
        for line in file:
            if line.strip():
                count += 1
    _LOG_LINE_COUNT_CACHE[cache_key] = (stat.st_mtime, count)
    return count


def cached_replay_dataset_stats(path: Path) -> Dict[str, Any]:
    cache_key = str(path)
    try:
        mtime = path.stat().st_mtime if path.exists() else 0.0
    except OSError:
        mtime = 0.0
    cached = _DATASET_STATS_CACHE.get(cache_key)
    if cached and cached[0] == mtime:
        return cached[1]
    stats = replay_dataset_stats(path)
    _DATASET_STATS_CACHE[cache_key] = (mtime, stats)
    return stats


def analysis_log_bytes() -> int:
    if not REPLAY_ANALYSIS_LOG_FILE.exists():
        return 0
    try:
        return int(REPLAY_ANALYSIS_LOG_FILE.stat().st_size)
    except OSError:
        return 0


def resolve_replay_dataset_path(source: str = "recorded") -> Path:
    normalized = str(source or "recorded").strip().lower()
    if normalized == "historical":
        return HISTORICAL_REPLAY_DATASET_FILE
    return REPLAY_DATASET_FILE


def replay_build_status_snapshot() -> Dict[str, Any]:
    with _REPLAY_BUILD_LOCK:
        state = dict(_REPLAY_BUILD_STATE)
    return {
        **state,
        "historical_dataset": cached_replay_dataset_stats(HISTORICAL_REPLAY_DATASET_FILE),
        "historical_dataset_path": str(HISTORICAL_REPLAY_DATASET_FILE),
    }


def start_historical_replay_build(payload: Dict[str, Any]) -> Dict[str, Any]:
    global _REPLAY_BUILD_THREAD, _REPLAY_BUILD_STATE
    with _REPLAY_BUILD_LOCK:
        if _REPLAY_BUILD_STATE.get("running"):
            return {"ok": False, "error": "已有历史回放生成任务在进行中"}
    if monitor_status()["running"]:
        return {"ok": False, "error": "请先停止正式监控，再生成历史回放数据"}
    if replay_status()["running"]:
        return {"ok": False, "error": "请先停止回放，再生成历史回放数据"}

    inst_id = str(payload.get("inst_id") or "").strip().upper()
    if not inst_id:
        configured = order_configured_inst_ids(load_config().get("inst_ids"))
        inst_id = configured[0] if configured else DEFAULT_MONITOR_INSTRUMENTS[0]
    start_time = str(payload.get("start_time") or "").strip()
    end_time = str(payload.get("end_time") or "").strip()
    if not start_time or not end_time:
        return {"ok": False, "error": "请填写开始时间与结束时间"}
    try:
        step_seconds = max(5, min(3600, int(payload.get("step_seconds") or 60)))
    except (TypeError, ValueError):
        return {"ok": False, "error": "帧步长无效"}

    def worker() -> None:
        global _REPLAY_BUILD_STATE
        try:
            from historical_replay_builder import build_historical_replay_dataset

            def progress(phase: str, current: int, total: int, message: str) -> None:
                with _REPLAY_BUILD_LOCK:
                    _REPLAY_BUILD_STATE.update(
                        {
                            "phase": phase,
                            "current": current,
                            "total": total,
                            "message": message,
                        }
                    )

            result = build_historical_replay_dataset(
                inst_id=inst_id,
                start_time=start_time,
                end_time=end_time,
                step_seconds=step_seconds,
                output_path=HISTORICAL_REPLAY_DATASET_FILE,
                progress_callback=progress,
                cancel_check=lambda: bool(_REPLAY_BUILD_STATE.get("cancel_requested")),
            )
            with _REPLAY_BUILD_LOCK:
                _REPLAY_BUILD_STATE.update(
                    {
                        "running": False,
                        "phase": "done",
                        "message": "生成完成",
                        "error": "",
                        "result": result,
                    }
                )
            _DATASET_STATS_CACHE.pop(str(HISTORICAL_REPLAY_DATASET_FILE), None)
        except Exception as exc:
            with _REPLAY_BUILD_LOCK:
                _REPLAY_BUILD_STATE.update(
                    {
                        "running": False,
                        "phase": "error",
                        "message": str(exc),
                        "error": str(exc),
                        "result": None,
                    }
                )

    with _REPLAY_BUILD_LOCK:
        _REPLAY_BUILD_STATE = {
            "running": True,
            "phase": "init",
            "current": 0,
            "total": 0,
            "message": "准备生成...",
            "error": "",
            "result": None,
            "cancel_requested": False,
        }
    _REPLAY_BUILD_THREAD = threading.Thread(target=worker, daemon=True, name="historical-replay-build")
    _REPLAY_BUILD_THREAD.start()
    return {"ok": True, "message": "已开始生成历史回放数据", **replay_build_status_snapshot()}


def replay_dataset_info(*, lite: bool = False) -> Dict[str, Any]:
    stats = cached_replay_dataset_stats(REPLAY_DATASET_FILE)
    historical_stats = cached_replay_dataset_stats(HISTORICAL_REPLAY_DATASET_FILE)
    status = replay_status()
    config = load_config()
    running = bool(status["running"])
    line_count = count_nonempty_lines(REPLAY_ANALYSIS_LOG_FILE, fast=lite or running)
    build_status = replay_build_status_snapshot()
    return {
        **stats,
        "historical_dataset": historical_stats,
        "historical_dataset_path": str(HISTORICAL_REPLAY_DATASET_FILE),
        "build_status": build_status,
        "record_enabled": bool(config.get("record_replay_enabled")),
        "monitor_running": bool(monitor_status()["running"]),
        "replay_running": running,
        "replay_status": status,
        "analysis_log_path": str(REPLAY_ANALYSIS_LOG_FILE),
        "analysis_log_lines": line_count,
        "analysis_log_bytes": analysis_log_bytes(),
        "replay_log_start_at": REPLAY_LOG_START_AT,
        "ai_enabled": bool(config.get("ai_enabled")),
        "dry_run_ai": bool(config.get("dry_run_ai")),
        "push_enabled": bool(config.get("push_enabled")),
    }


def _clear_monitor_runtime_caches() -> None:
    _REALTIME_POINTS_CACHE.clear()


def _cached_monitor_ai_tokens() -> int:
    global _STATUS_TOKEN_CACHE
    now = time.time()
    if now - _STATUS_TOKEN_CACHE[0] < 5.0:
        return _STATUS_TOKEN_CACHE[1]
    tokens = int(load_ai_token_stats(AI_TOKEN_STATS_FILE).get("total_tokens", 0))
    _STATUS_TOKEN_CACHE = (now, tokens)
    return tokens


def _send_monitor_stop_lifecycle_brief(config: Dict[str, Any]) -> None:
    if int(config.get("wechat_silence_brief_minutes", 0) or 0) <= 0:
        return
    if not config.get("push_enabled") or not config.get("ai_enabled"):
        return
    try:
        with use_saved_env():
            get_signal_monitor().push_monitor_lifecycle_briefs("monitor_stop", config)
    except Exception as exc:
        print(f"[web] monitor_stop lifecycle brief failed: {exc}", flush=True)


def monitor_status_text(running: bool, pid: Optional[int] = None, ai_total_tokens: int = 0) -> str:
    if running and pid:
        return f"运行中 PID={pid} · Token {ai_total_tokens:,}"
    if ai_total_tokens > 0:
        return f"已停止 · 本次 Token {ai_total_tokens:,}"
    return "未启动"


def monitor_status() -> Dict[str, Any]:
    global MONITOR_PROCESS, MONITOR_STOPPED_AT, _MONITOR_STATUS_HEAVY_AT
    elapsed_seconds = 0
    ai_total_tokens = _cached_monitor_ai_tokens()
    if MONITOR_STARTED_AT:
        try:
            start_time = datetime.strptime(MONITOR_STARTED_AT, "%Y-%m-%d %H:%M:%S")
            end_time = datetime.strptime(MONITOR_STOPPED_AT, "%Y-%m-%d %H:%M:%S") if MONITOR_STOPPED_AT else datetime.now()
            elapsed_seconds = max(0, int((end_time - start_time).total_seconds()))
        except Exception:
            elapsed_seconds = 0
    if MONITOR_PROCESS is None:
        return {
            "running": False,
            "text": monitor_status_text(False, ai_total_tokens=ai_total_tokens),
            "started_at": MONITOR_STARTED_AT,
            "stopped_at": MONITOR_STOPPED_AT,
            "elapsed_seconds": elapsed_seconds if MONITOR_STOPPED_AT else 0,
            "pid": None,
            "ai_total_tokens": ai_total_tokens,
            "analysis_log_enabled": analysis_log_enabled(),
            "unexpected_exit": MONITOR_LAST_EXIT_UNEXPECTED,
        }
    code = MONITOR_PROCESS.poll()
    if code is None:
        now = time.time()
        if now - _MONITOR_STATUS_HEAVY_AT >= MONITOR_STATUS_HEAVY_INTERVAL_SECONDS:
            rotate_console_log_if_needed(MONITOR_PROCESS_LOG_FILE)
            _MONITOR_STATUS_HEAVY_AT = now
        pid = MONITOR_PROCESS.pid
        return {
            "running": True,
            "text": monitor_status_text(True, pid=pid, ai_total_tokens=ai_total_tokens),
            "started_at": MONITOR_STARTED_AT,
            "stopped_at": "",
            "elapsed_seconds": elapsed_seconds,
            "pid": pid,
            "ai_total_tokens": ai_total_tokens,
            "analysis_log_enabled": analysis_log_enabled(),
            "unexpected_exit": False,
        }
    if not MONITOR_STOPPED_AT:
        MONITOR_STOPPED_AT = time.strftime("%Y-%m-%d %H:%M:%S")
        restarted = False
        if not MONITOR_STOP_REQUESTED:
            restarted = handle_monitor_unexpected_exit(code)
        if not restarted:
            MONITOR_PROCESS = None
            clear_child_pid(MONITOR_PID_FILE)
        return monitor_status()
    MONITOR_PROCESS = None
    clear_child_pid(MONITOR_PID_FILE)
    return {
        "running": False,
        "text": f"已停止，退出码={code} · Token {ai_total_tokens:,}",
        "started_at": MONITOR_STARTED_AT,
        "stopped_at": MONITOR_STOPPED_AT,
        "elapsed_seconds": elapsed_seconds,
        "pid": None,
        "ai_total_tokens": ai_total_tokens,
        "analysis_log_enabled": analysis_log_enabled(),
        "unexpected_exit": MONITOR_LAST_EXIT_UNEXPECTED,
    }


def analysis_log_disabled_message() -> str:
    return "分析日志未就绪。请启动监控并确认配置页日志容量设置，保存后重新启动监控。"


def start_monitor() -> str:
    global MONITOR_PROCESS, MONITOR_STARTED_AT, MONITOR_LOG_START_AT, MONITOR_STOPPED_AT, MONITOR_STOP_REQUESTED, MONITOR_LAST_EXIT_UNEXPECTED
    if replay_status()["running"]:
        return "请先停止离线回放，再启动正式监控。"
    status = monitor_status()
    if status["running"]:
        return status["text"]
    if not configured_instruments():
        return "请先在配置页至少选择一个监控币种。"
    MONITOR_STOP_REQUESTED = False
    MONITOR_LAST_EXIT_UNEXPECTED = False
    MONITOR_STARTED_AT = time.strftime("%Y-%m-%d %H:%M:%S")
    MONITOR_LOG_START_AT = MONITOR_STARTED_AT
    MONITOR_STOPPED_AT = ""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    rotate_console_log_if_needed(MONITOR_PROCESS_LOG_FILE)
    reset_ai_token_stats(AI_TOKEN_STATS_FILE, started_at=MONITOR_STARTED_AT)
    config = load_config()
    if config.get("record_replay_enabled") and REPLAY_DATASET_FILE.exists():
        REPLAY_DATASET_FILE.unlink()
    log_file = MONITOR_PROCESS_LOG_FILE.open("a", encoding="utf-8")
    log_file.write(f"\n===== signal monitor started at {MONITOR_STARTED_AT} =====\n")
    log_file.flush()
    MONITOR_PROCESS = subprocess.Popen(
        build_monitor_args(load_config()),
        cwd=str(SCRIPT_DIR),
        env=build_child_env(),
        stdout=log_file,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        creationflags=0,
    )
    log_file.close()
    write_child_pid(MONITOR_PID_FILE, MONITOR_PROCESS.pid)
    return f"监控已启动，PID={MONITOR_PROCESS.pid} · Token 0"


def monitor_start_payload() -> Dict[str, Any]:
    message = start_monitor()
    status = monitor_status()
    ok = bool(status.get("running")) or "已启动" in message
    return {"ok": ok, "message": message, **status}


def stop_monitor(*, fast: bool = False, lifecycle_brief: bool = True) -> str:
    global MONITOR_PROCESS, MONITOR_STOPPED_AT, MONITOR_STOP_REQUESTED, MONITOR_LAST_EXIT_UNEXPECTED
    brief_config: Optional[Dict[str, Any]] = None
    with _MONITOR_STOP_LOCK:
        status = monitor_status()
        if not status["running"]:
            return "监控未运行。"
        if lifecycle_brief and not fast:
            brief_config = normalize_config(load_config())
        MONITOR_STOP_REQUESTED = True
        MONITOR_LAST_EXIT_UNEXPECTED = False
        wait_timeout = 1.5 if fast else 8
        kill_wait = 1.0 if fast else 5
        MONITOR_PROCESS.terminate()
        try:
            MONITOR_PROCESS.wait(timeout=wait_timeout)
        except subprocess.TimeoutExpired:
            MONITOR_PROCESS.kill()
            MONITOR_PROCESS.wait(timeout=kill_wait)
        MONITOR_STOPPED_AT = time.strftime("%Y-%m-%d %H:%M:%S")
        MONITOR_PROCESS = None
        clear_child_pid(MONITOR_PID_FILE)
        try:
            with MONITOR_PROCESS_LOG_FILE.open("a", encoding="utf-8") as log_file:
                log_file.write(f"\n===== signal monitor stopped at {MONITOR_STOPPED_AT} =====\n")
        except OSError:
            pass
        MONITOR_STOP_REQUESTED = False
        _clear_monitor_runtime_caches()
    if brief_config is not None:
        threading.Thread(
            target=_send_monitor_stop_lifecycle_brief,
            args=(brief_config,),
            daemon=True,
        ).start()
    return "监控已停止。"


def monitor_stop_payload(*, fast: bool = False) -> Dict[str, Any]:
    message = stop_monitor(fast=fast, lifecycle_brief=not fast)
    status = monitor_status()
    ok = "已停止" in message or "未运行" in message
    return {"ok": ok, "message": message, **status}


def post_json(url: str, payload: dict) -> str:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.read().decode("utf-8", errors="ignore")


def get_quick_ticker(inst_id: str) -> Dict[str, Any]:
    payload = read_monitor_points(inst_id, max_points=2)
    points = payload.get("points") or []
    payload["points"] = points[-1:] if points else []
    payload["quick"] = True
    payload["source"] = "signal-monitor-log"
    return payload


def normalize_monitor_bar(bar: Any) -> str:
    text = str(bar or "1m").strip()
    aliases = {"1h": "1H", "4h": "4H", "1d": "1D", "1w": "1W"}
    normalized = aliases.get(text.lower(), text)
    if normalized not in MONITOR_BAR_CHANNELS:
        raise ValueError(f"不支持的 K 线周期：{bar}，可选 {', '.join(MONITOR_BAR_CHANNELS)}")
    return normalized


def candle_fetch_limit(bar: str) -> int:
    return {"1m": 120, "3m": 120, "5m": 120, "15m": 96, "1H": 96, "4H": 90, "1D": 120, "1W": 104}.get(bar, 120)


def metrics_overlay_window_seconds(bar: str) -> int:
    return {
        "1m": 180,
        "3m": 540,
        "5m": 900,
        "15m": 1800,
        "1H": 7200,
        "4H": 28800,
        "1D": 172800,
        "1W": 1209600,
    }.get(bar, 180)


def get_history_candle_points(inst_id: str, bar: str = "1m", limit: int = 120) -> List[Dict[str, Any]]:
    inst_id = normalize_inst_id(inst_id)
    if inst_id not in configured_instruments():
        return []
    bar = normalize_monitor_bar(bar)
    limit = max(10, min(300, int(limit or candle_fetch_limit(bar))))
    cache_key = f"{inst_id}:{bar}:{limit}"
    now = time.time()
    cached = _CANDLE_CACHE.get(cache_key)
    if cached and now - cached[0] < WEB_CANDLE_CACHE_TTL_SECONDS:
        return cached[1]
    query = urllib.parse.urlencode({"instId": inst_id, "bar": bar, "limit": str(limit)})
    request = urllib.request.Request(
        f"{OKX_BASE_URL}/api/v5/market/candles?{query}",
        headers={"Accept": "application/json", "User-Agent": "okx-ai-assistant-web/1.0"},
    )
    with urllib.request.urlopen(request, timeout=8) as response:
        payload = json.loads(response.read().decode("utf-8", errors="ignore"))
    rows = payload.get("data") or []
    points = []
    for row in reversed(rows):
        if not isinstance(row, list) or len(row) < 6:
            continue
        try:
            close = float(row[4])
            points.append({
                "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(row[0]) / 1000)),
                "price": close,
                "kind": "history",
                "bar": bar,
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": close,
                "volume": float(row[5]),
                "confirmed": str(row[8]) if len(row) > 8 else "0",
            })
        except (TypeError, ValueError):
            continue
    _CANDLE_CACHE[cache_key] = (now, points)
    if len(_CANDLE_CACHE) > 128:
        stale_before = now - WEB_CANDLE_CACHE_TTL_SECONDS * 4
        for key, (saved_at, _) in list(_CANDLE_CACHE.items()):
            if saved_at < stale_before:
                _CANDLE_CACHE.pop(key, None)
    return points


def chart_points_from_log_item(item: Dict[str, Any]) -> List[Dict[str, Any]]:
    chart = item.get("chart") if isinstance(item.get("chart"), dict) else {}
    rows = chart.get("points") if chart.get("bar") == "1m" and isinstance(chart.get("points"), list) else []
    points = []
    for row in reversed(rows):
        if not isinstance(row, dict):
            continue
        try:
            price = float(row.get("close"))
        except (TypeError, ValueError):
            continue
        points.append({
            "time": row.get("time", ""),
            "price": price,
            "kind": "history",
            "open": row.get("open"),
            "high": row.get("high"),
            "low": row.get("low"),
            "close": row.get("close"),
            "volume": row.get("volume"),
            "confirmed": row.get("confirmed"),
        })
    return points


def minute_bucket_key(value: Any) -> str:
    text = str(value or "")
    if len(text) >= 16 and text[4:5] == "-" and text[7:8] == "-":
        return f"{text[:16]}:00"
    return text


def seconds_between_time_text(left: Any, right: Any) -> int:
    try:
        return abs(int((parse_history_time(str(left)) - parse_history_time(str(right))).total_seconds()))
    except Exception:
        return 10 ** 9


def merge_price_points(history_points: List[Dict[str, Any]], realtime_points: List[Dict[str, Any]], max_points: int) -> List[Dict[str, Any]]:
    merged = []
    index_by_time = {}
    for point in history_points + realtime_points:
        try:
            price = float(point.get("price"))
        except (AttributeError, TypeError, ValueError):
            continue
        key = minute_bucket_key(point.get("time", ""))
        normalized = dict(point)
        normalized["time"] = key
        normalized["price"] = price
        if key and key in index_by_time:
            merged[index_by_time[key]] = {**merged[index_by_time[key]], **normalized}
            continue
        if key:
            index_by_time[key] = len(merged)
        merged.append(normalized)
    merged.sort(key=lambda point: minute_bucket_key(point.get("time", "")))
    return merged[-max_points:]


def read_paper_account(inst_id: str) -> Dict[str, Any]:
    if not PAPER_ACCOUNT_FILE.exists():
        return {}
    try:
        payload = json.loads(PAPER_ACCOUNT_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    account = payload.get("accounts", {}).get(inst_id, {})
    if not account:
        return {}
    return {
        **account,
        "initial_capital": payload.get("initial_capital"),
        "session_started_at": payload.get("session_started_at"),
        "session_label": payload.get("session_label"),
        "note": payload.get("note"),
    }


def local_analysis_mode_from_log_item(item: Dict[str, Any]) -> bool:
    """True only when the monitor explicitly recorded AI as disabled for this frame."""
    snapshot = item.get("config_snapshot")
    return isinstance(snapshot, dict) and snapshot.get("ai_enabled") is False


def effective_fields_from_log_item(item: Dict[str, Any]) -> Dict[str, Any]:
    score = item.get("score") if isinstance(item.get("score"), dict) else {}
    final_decision = item.get("final_decision") if isinstance(item.get("final_decision"), dict) else {}
    if local_analysis_mode_from_log_item(item):
        # AI-off is a local-model test mode. Keep score exactly as produced by the
        # monitor and only attach display metadata; do not let the external
        # local_screening=观望 wrapper overwrite local direction or levels.
        merged = dict(score)
        screening = final_decision.get("local_screening") if isinstance(final_decision.get("local_screening"), dict) else {}
        structure_forecast = score.get("structure_forecast") if isinstance(score.get("structure_forecast"), dict) else {}
        local_direction = score.get("final_direction", score.get("direction", "观望"))
        merged.update(
            {
                "direction": local_direction,
                "final_direction": local_direction,
                "confidence": score.get("direction_score", score.get("confidence", score.get("final_trade_score", 0))),
                "local_final_trade_score": score.get("final_trade_score", 0),
                "push_recommendation": score.get("trade_action_level", "none"),
                "decision_source": "local_screening",
                "ai_called": False,
                "trigger_level": final_decision.get("trigger_level"),
                "local_hint_direction": local_direction,
                "local_bias": local_direction,
                "summary": screening.get("summary") or final_decision.get("summary", ""),
                "forward_direction": None,
                "forward_probability": None,
                "forward_horizon_minutes": None,
                "structure_forecast_direction": (
                    structure_forecast.get("direction") if structure_forecast.get("active") else None
                ),
                "analysis_mode": "local",
            }
        )
        return merged
    if not final_decision:
        return score

    direction = final_decision.get("direction", score.get("direction", "观望"))
    confidence = final_decision.get("confidence")
    if confidence is None:
        confidence = score.get("final_trade_score", 0) if direction in ("做多", "做空") else score.get("raw_total_score", 0)
    local_final_trade_score = score.get("final_trade_score", 0)
    raw_total_score = score.get("raw_total_score", confidence)
    entry_plan = score.get("entry_plan") if isinstance(score.get("entry_plan"), dict) else {}
    merged = dict(score)
    merged.update(
        {
            "direction": direction,
            "final_direction": direction,
            "confidence": max(0, min(100, int(confidence or 0))),
            "raw_total_score": raw_total_score,
            "local_final_trade_score": local_final_trade_score,
            "final_trade_score": max(0, min(100, int(confidence or 0))) if direction in ("做多", "做空") else 0,
            "total_score": max(0, min(100, int(confidence or 0))) if direction in ("做多", "做空") else raw_total_score,
            "entry": final_decision.get("entry", score.get("entry")),
            "stop_loss": final_decision.get("stop_loss", score.get("stop_loss")),
            "take_profit": final_decision.get("take_profit", score.get("take_profit")),
            "risk_level": final_decision.get("risk_level", score.get("risk_level")),
            "push_recommendation": final_decision.get("push_recommendation", score.get("trade_action_level", "none")),
            "trade_action_level": final_decision.get("push_recommendation", score.get("trade_action_level")),
            "decision_source": final_decision.get("decision_source"),
            "ai_called": final_decision.get("ai_called"),
            "trigger_level": final_decision.get("trigger_level"),
            "local_hint_direction": final_decision.get("local_hint_direction", score.get("raw_direction")),
            "market_regime": final_decision.get("market_regime", score.get("market_regime")),
            "strategy_label": final_decision.get("strategy_label", score.get("strategy_label")),
            "summary": final_decision.get("summary", ""),
        }
    )
    if not entry_plan:
        merged["entry_plan"] = {}
    screening = final_decision.get("local_screening") if isinstance(final_decision.get("local_screening"), dict) else {}
    forward = final_decision.get("forward_view") if isinstance(final_decision.get("forward_view"), dict) else {}
    local_bias = final_decision.get("local_bias") or final_decision.get("local_hint_direction") or screening.get("local_bias")
    structure_forecast = score.get("structure_forecast") if isinstance(score.get("structure_forecast"), dict) else {}
    merged["local_bias"] = local_bias or score.get("direction", "观望")
    merged["forward_direction"] = forward.get("direction")
    merged["forward_probability"] = forward.get("probability")
    merged["forward_horizon_minutes"] = forward.get("horizon_minutes")
    merged["structure_forecast_direction"] = structure_forecast.get("direction") if structure_forecast.get("active") else None
    return merged


def prediction_direction_from_log_item(item: Dict[str, Any]) -> Tuple[str, str]:
    """Prediction track for accuracy chart: AI forward_view when available, else raw_direction."""
    fields = effective_fields_from_log_item(item)
    decision_source = str(fields.get("decision_source", "") or "")
    forward = fields.get("forward_direction")
    if decision_source == "ai" and forward in ("做多", "做空", "观望"):
        return str(forward), "ai_forward"
    raw = fields.get("raw_direction")
    if raw in ("做多", "做空", "观望"):
        return str(raw), "raw_direction"
    structure = fields.get("structure_forecast_direction")
    if structure in ("做多", "做空"):
        return str(structure), "structure_forecast"
    fallback = fields.get("final_direction", fields.get("direction", "观望"))
    return str(fallback or "观望"), "final_fallback"


def _indicator_num(value: Any, digits: int = 2) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return round(number, digits)


def compact_profile_indicators(profile: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(profile, dict) or not profile:
        return {}
    ema = profile.get("ema") if isinstance(profile.get("ema"), dict) else {}
    rsi = profile.get("rsi") if isinstance(profile.get("rsi"), dict) else {}
    macd = profile.get("macd") if isinstance(profile.get("macd"), dict) else {}
    kdj = profile.get("kdj") if isinstance(profile.get("kdj"), dict) else {}
    boll = profile.get("boll") if isinstance(profile.get("boll"), dict) else {}
    adx = profile.get("adx") if isinstance(profile.get("adx"), dict) else {}
    data_quality = profile.get("data_quality") if isinstance(profile.get("data_quality"), dict) else {}
    ema_parts: List[str] = []
    for key in sorted(ema.keys(), key=lambda item: int(str(item)) if str(item).isdigit() else 0):
        value = _indicator_num(ema.get(key), 2)
        if value is not None:
            ema_parts.append(f"{key}:{value:g}")
    return {
        "bar": profile.get("bar"),
        "trend": profile.get("trend"),
        "ema": " · ".join(ema_parts) if ema_parts else None,
        "ema_slope_pct": _indicator_num(profile.get("ema_slope_pct"), 3),
        "atr": _indicator_num(profile.get("atr"), 4),
        "atr_pct": _indicator_num(profile.get("atr_pct"), 3),
        "recent_high": _indicator_num(profile.get("recent_high"), 2),
        "recent_low": _indicator_num(profile.get("recent_low"), 2),
        "breakout": profile.get("breakout"),
        "rsi_6": _indicator_num(rsi.get("6"), 1),
        "rsi_14": _indicator_num(rsi.get("14"), 1),
        "rsi_24": _indicator_num(rsi.get("24"), 1),
        "macd_dif": _indicator_num(macd.get("dif"), 4),
        "macd_dea": _indicator_num(macd.get("dea"), 4),
        "macd_hist": _indicator_num(macd.get("hist"), 4),
        "macd_hist_slope": _indicator_num(macd.get("hist_slope"), 4),
        "kdj_k": _indicator_num(kdj.get("k"), 1),
        "kdj_d": _indicator_num(kdj.get("d"), 1),
        "kdj_j": _indicator_num(kdj.get("j"), 1),
        "boll_bw_pct": _indicator_num(boll.get("bandwidth_pct"), 3),
        "boll_pos": _indicator_num(boll.get("position"), 3),
        "adx": _indicator_num(adx.get("adx"), 1),
        "plus_di": _indicator_num(adx.get("plus_di"), 1),
        "minus_di": _indicator_num(adx.get("minus_di"), 1),
        "dist_ema20_atr": _indicator_num(profile.get("distance_to_ema20_atr"), 2),
        "divergence": profile.get("divergence"),
        "body_ratio": _indicator_num(profile.get("body_ratio"), 3),
        "data_reliable": data_quality.get("is_reliable"),
        "data_count": data_quality.get("confirmed_count"),
    }


def compact_trend_profiles_for_ui(profiles: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(profiles, dict):
        return {}
    compact: Dict[str, Any] = {}
    for bar in ("5m", "15m", "1H", "4H"):
        profile = profiles.get(bar)
        if isinstance(profile, dict) and profile:
            item = compact_profile_indicators(profile)
            if item:
                compact[bar] = item
    return compact


def point_from_log_item(item: Dict[str, Any], price: float) -> Dict[str, Any]:
    score = effective_fields_from_log_item(item)
    volume = item.get("volume") if isinstance(item.get("volume"), dict) else {}
    long_short = item.get("long_short_ratio") if isinstance(item.get("long_short_ratio"), dict) else {}
    signals = item.get("signals") if isinstance(item.get("signals"), list) else []
    context = item.get("market_context") if isinstance(item.get("market_context"), dict) else {}
    order_book = item.get("order_book") if isinstance(item.get("order_book"), dict) else {}
    volatility = item.get("volatility") if isinstance(item.get("volatility"), dict) else {}
    dynamic = item.get("dynamic_thresholds") if isinstance(item.get("dynamic_thresholds"), dict) else {}
    profiles = item.get("trend_profiles") if isinstance(item.get("trend_profiles"), dict) else {}
    data_quality = profiles.get("15m", {}).get("data_quality", {}) if isinstance(profiles.get("15m"), dict) else {}
    snapshot_quality = item.get("snapshot_quality") if isinstance(item.get("snapshot_quality"), dict) else {}
    profile_15m = profiles.get("15m", {}) if isinstance(profiles.get("15m"), dict) else {}
    trends = score.get("trends") if isinstance(score.get("trends"), dict) else {}
    entry_plan = score.get("entry_plan") if isinstance(score.get("entry_plan"), dict) else {}
    layer_scores = score.get("layer_scores") if isinstance(score.get("layer_scores"), dict) else {}
    tracking = item.get("signal_tracking") if isinstance(item.get("signal_tracking"), dict) else {}
    paper = item.get("paper_account") if isinstance(item.get("paper_account"), dict) else {}
    raw_total_score = score.get("raw_total_score", score.get("confidence", score.get("total_score")))
    final_direction = score.get("final_direction", score.get("direction"))
    final_trade_score = score.get("final_trade_score")
    if final_trade_score is None:
        final_trade_score = score.get("total_score") if final_direction in ("做多", "做空") else 0
    return {
        "time": item.get("time", ""),
        "price": price,
        "kind": "realtime",
        "open_interest": item.get("open_interest"),
        "oi_change_pct_15m": item.get("oi_change_pct_15m"),
        "oi_warmup_ready": item.get("oi_warmup_ready"),
        "funding_rate": item.get("funding_rate"),
        "funding_change": item.get("funding_change"),
        "funding_warmup_ready": item.get("funding_warmup_ready"),
        "volume_multiplier": volume.get("multiplier"),
        "volume_current": volume.get("current"),
        "volume_average_20": volume.get("average_20"),
        "volume_direction": volume.get("direction"),
        "volume_trend": volume.get("trend"),
        "volume_source": volume.get("source"),
        "long_ratio": long_short.get("long_ratio"),
        "short_ratio": long_short.get("short_ratio"),
        "long_short_available": long_short.get("available"),
        "score": score.get("total_score"),
        "raw_total_score": raw_total_score,
        "final_trade_score": final_trade_score,
        "direction": final_direction,
        "raw_direction": score.get("raw_direction", final_direction),
        "final_direction": final_direction,
        "confidence": score.get("confidence"),
        "push_recommendation": score.get("push_recommendation"),
        "local_final_trade_score": score.get("local_final_trade_score", final_trade_score),
        "local_hint_direction": score.get("local_hint_direction"),
        "local_bias": score.get("local_bias"),
        "forward_direction": score.get("forward_direction"),
        "forward_probability": score.get("forward_probability"),
        "forward_horizon_minutes": score.get("forward_horizon_minutes"),
        "structure_forecast_direction": score.get("structure_forecast_direction"),
        "decision_source": score.get("decision_source"),
        "trigger_level": score.get("trigger_level"),
        "summary": score.get("summary"),
        "trend_profile_15m": profile_15m.get("trend"),
        "trend_simple_15m": trends.get("15m"),
        "strategy_label": score.get("strategy_label"),
        "risk_control_score": score.get("risk_control_score"),
        "entry_quality_score": score.get("entry_quality_score"),
        "entry_quality": entry_plan.get("quality"),
        "entry": score.get("entry"),
        "stop_loss": score.get("stop_loss"),
        "take_profit": score.get("take_profit"),
        "invalidation": entry_plan.get("invalidation"),
        "wait_for": entry_plan.get("wait_for", []),
        "layer_scores": layer_scores,
        "risk_level": score.get("risk_level"),
        "market_risk_level": score.get("market_risk_level"),
        "trade_action_level": score.get("trade_action_level"),
        "market_regime": score.get("market_regime", context.get("regime")),
        "bias": score.get("bias", context.get("bias")),
        "strategy_template": context.get("strategy_template"),
        "atr_pct_15m": volatility.get("atr_pct_15m"),
        "volatility_regime": volatility.get("regime"),
        "order_book_available": order_book.get("available"),
        "order_book_imbalance": order_book.get("imbalance"),
        "order_book_imbalance_5": order_book.get("imbalance_5"),
        "spread_pct": order_book.get("spread_pct"),
        "volume_threshold_used": context.get("volume_threshold_used", dynamic.get("volume_multiplier_p85")),
        "data_quality_reliable": data_quality.get("is_reliable"),
        "data_quality_count": data_quality.get("confirmed_count"),
        "snapshot_quality_overall": snapshot_quality.get("overall"),
        "snapshot_stale_sources": snapshot_quality.get("stale_sources", []),
        "snapshot_max_source_age_seconds": snapshot_quality.get("max_source_age_seconds"),
        "snapshot_collection_duration_ms": snapshot_quality.get("collection_duration_ms"),
        "signal_tracking": tracking,
        "signals": [signal.get("type", "") for signal in signals if isinstance(signal, dict)],
        "trend_indicators": compact_trend_profiles_for_ui(profiles),
        "paper_equity": paper.get("equity"),
        "paper_pnl_usd": paper.get("pnl_usd"),
        "paper_pnl_pct": paper.get("pnl_pct"),
        "paper_position": paper.get("position_label", paper.get("position")),
        "paper_trade_count": paper.get("trade_count"),
        "paper_initial_capital": paper.get("initial_capital"),
    }


def parse_serverchan_response(raw: str) -> Tuple[bool, str]:
    text = (raw or "").strip()
    if not text:
        return False, "Server酱返回空响应"
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return True, text[:160]
    if not isinstance(payload, dict):
        return True, text[:160]
    code = payload.get("code")
    if code is not None:
        try:
            code_ok = int(code) == 0
        except (TypeError, ValueError):
            code_ok = True
        if not code_ok:
            detail = payload.get("message") or payload.get("msg") or text
            return False, str(detail)
    detail = payload.get("message") or payload.get("data") or text
    return True, str(detail)[:160]


def extract_chat_completion_text(response: Any) -> str:
    choices = getattr(response, "choices", None) or []
    if choices:
        message = getattr(choices[0], "message", None)
        content = getattr(message, "content", None) if message else None
        if content:
            return str(content).strip()
    output_text = getattr(response, "output_text", None)
    if output_text:
        return str(output_text).strip()
    return str(response).strip()


def call_ai_chat(user_message: str, history: Optional[List[Dict[str, str]]] = None) -> Dict[str, Any]:
    env = build_child_env()
    api_key = env.get("OPENAI_API_KEY", "")
    base_url = env.get("AI_BASE_URL", ENV_DEFAULTS["AI_BASE_URL"]).strip()
    model = env.get("AI_MODEL", ENV_DEFAULTS["AI_MODEL"])
    message = str(user_message or "").strip()
    if not api_key:
        return {"ok": False, "error": "AI API Key 未配置。请先在配置页填写并保存。"}
    if not message:
        return {"ok": False, "error": "请输入消息内容。"}
    if len(message) > 8000:
        return {"ok": False, "error": "消息过长，请控制在 8000 字以内。"}

    messages: List[Dict[str, str]] = []
    for item in (history or [])[-20:]:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role", "")).strip()
        content = str(item.get("content", "")).strip()
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": message})

    try:
        from openai import OpenAI

        client = OpenAI(api_key=api_key, base_url=base_url, timeout=120, max_retries=0)
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=0.7,
            max_tokens=2048,
        )
        reply = extract_chat_completion_text(response)
        usage_obj = getattr(response, "usage", None)
        usage = None
        if usage_obj is not None:
            usage = {
                "prompt_tokens": getattr(usage_obj, "prompt_tokens", None),
                "completion_tokens": getattr(usage_obj, "completion_tokens", None),
                "total_tokens": getattr(usage_obj, "total_tokens", None),
            }
        return {"ok": True, "reply": reply, "model": model, "usage": usage}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def test_ai_connection() -> Dict[str, Any]:
    result = call_ai_chat("请只回复：AI接口连通性测试成功。")
    if result.get("ok"):
        return {"ok": True, "message": f"AI测试成功：{result.get('reply', '')}"}
    return {"ok": False, "message": f"AI测试失败：{result.get('error', '未知错误')}"}


def fetch_manual_brief(inst_id: str = "") -> Dict[str, Any]:
    env = build_child_env()
    if not str(env.get("OPENAI_API_KEY", "")).strip():
        return {"ok": False, "error": "AI API Key 未配置。请先在配置页填写并保存。"}
    config = normalize_config(load_config())
    if not configured_instruments():
        return {"ok": False, "error": "请先在配置页选择监控币种。"}
    try:
        with use_saved_env():
            return get_signal_monitor().generate_manual_brief(config, inst_id)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def redact_secrets_mapping(data: Dict[str, Any]) -> Dict[str, Any]:
    redacted: Dict[str, Any] = {}
    for key, value in data.items():
        if key in SECRET_ENV_KEYS and str(value or "").strip():
            redacted[key] = "***REDACTED***"
        else:
            redacted[key] = value
    return redacted


def export_file_bytes(path: Path, *, max_bytes: int, prefer_tail: bool = True) -> Tuple[Optional[bytes], Dict[str, Any]]:
    meta: Dict[str, Any] = {"path": str(path), "included": False, "bytes": 0, "truncated": False}
    if not path.exists() or not path.is_file():
        meta["note"] = "file missing"
        return None, meta
    try:
        size = path.stat().st_size
    except OSError as exc:
        meta["note"] = f"stat failed: {exc}"
        return None, meta
    meta["size_bytes"] = size
    if size <= max_bytes:
        try:
            content = path.read_bytes()
        except OSError as exc:
            meta["note"] = f"read failed: {exc}"
            return None, meta
        meta["included"] = True
        meta["bytes"] = len(content)
        return content, meta
    if prefer_tail:
        text = tail_text(path, max_bytes)
        content = text.encode("utf-8")
        meta["included"] = True
        meta["truncated"] = True
        meta["bytes"] = len(content)
        meta["note"] = f"tail only (max {max_bytes} bytes)"
        return content, meta
    meta["note"] = f"skipped (>{max_bytes} bytes)"
    return None, meta


def diagnostic_replay_log_has_data() -> bool:
    if not REPLAY_ANALYSIS_LOG_FILE.exists():
        return False
    try:
        return REPLAY_ANALYSIS_LOG_FILE.stat().st_size > 0
    except OSError:
        return False


def iter_diagnostic_log_entries() -> List[Tuple[str, Path, int, bool]]:
    """Archive paths for runtime logs/state (oldest segments first where applicable)."""
    entries: List[Tuple[str, Path, int, bool]] = []
    seen: Set[str] = set()

    def add(path: Path, archive_name: str, *, max_bytes: int, prefer_tail: bool = True) -> None:
        try:
            key = str(path.resolve())
        except OSError:
            key = str(path)
        if key in seen:
            return
        seen.add(key)
        entries.append((archive_name, path, max_bytes, prefer_tail))

    for seg in list_analysis_log_segments(MONITOR_JSON_LOG_FILE):
        add(seg, f"logs/{seg.name}", max_bytes=DIAG_EXPORT_MAX_FILE_BYTES)
    if LOG_DIR.exists():
        for path in sorted(LOG_DIR.iterdir(), key=lambda item: item.name.lower()):
            if not path.is_file():
                continue
            if path in list_analysis_log_segments(MONITOR_JSON_LOG_FILE):
                continue
            max_bytes = (
                DIAG_EXPORT_REPLAY_DATASET_TAIL_BYTES
                if path.name in DIAG_EXPORT_REPLAY_DATASET_NAMES
                else DIAG_EXPORT_MAX_FILE_BYTES
            )
            add(path, f"logs/{path.name}", max_bytes=max_bytes)
    for path in (MONITOR_PID_FILE, REPLAY_PID_FILE, WEB_RESTART_SESSIONS_FILE, PORTABLE_CONFIG_FILE):
        if path.exists() and path.is_file():
            add(path, f"state/{path.name}", max_bytes=512 * 1024, prefer_tail=False)
    return entries


def _accuracy_direction_value(direction_text: str) -> int:
    text = str(direction_text or "").strip().lower()
    if text in ("做多", "long"):
        return 1
    if text in ("做空", "short"):
        return -1
    return 0


def _accuracy_confirm_value(point: Dict[str, Any]) -> int:
    direction = str(point.get("confirm_direction") or point.get("final_direction") or point.get("direction") or "")
    return _accuracy_direction_value(direction)


def _accuracy_prediction_value(point: Dict[str, Any]) -> int:
    direction = str(point.get("prediction_direction") or point.get("raw_direction") or "")
    return _accuracy_direction_value(direction)


def _decimate_chart_points(points: List[Dict[str, Any]], max_points: int = 900) -> List[Dict[str, Any]]:
    if len(points) <= max_points:
        return points
    indices = {0, len(points) - 1}
    slots = max(1, max_points - 2)
    for slot in range(slots):
        indices.add(int(round(slot * (len(points) - 1) / max(1, slots - 1))))
    return [points[index] for index in sorted(indices)]


def render_accuracy_chart_svg(bundle: Dict[str, Any]) -> str:
    raw_points: List[Dict[str, Any]] = []
    for point in bundle.get("points") or []:
        if not isinstance(point, dict):
            continue
        try:
            float(point.get("price"))
        except (TypeError, ValueError):
            continue
        raw_points.append(point)
    points = _decimate_chart_points(raw_points)
    width, height = 1280, 720
    pad_l, pad_r, pad_t, pad_b = 72, 72, 56, 52
    chart_w = width - pad_l - pad_r
    chart_h = height - pad_t - pad_b
    inst_id = html.escape(str(bundle.get("inst_id") or ""))
    scope = html.escape(str(bundle.get("scope") or ""))
    horizon = int(bundle.get("horizon_seconds") or 0)
    summary = bundle.get("summary") if isinstance(bundle.get("summary"), dict) else {}
    acc_pct = summary.get("prediction_accuracy_pct", summary.get("decision_accuracy_pct"))
    title = f"{inst_id} · {scope} · {horizon}s"
    subtitle = f"样本 {summary.get('total', summary.get('raw_log_total', len(raw_points)))} · 综合准确 {acc_pct if acc_pct is not None else '--'}%"
    if not points:
        return (
            f'<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">'
            f'<rect width="100%" height="100%" fill="#0f172a"/>'
            f'<text x="{width/2:.1f}" y="{height/2:.1f}" fill="#cbd5e1" font-family="Segoe UI, Microsoft YaHei, sans-serif" '
            f'font-size="18" text-anchor="middle">{title} · 暂无压测点</text></svg>'
        )
    if len(points) == 1:
        points = [points[0], dict(points[0])]
    prices = [safe_float(point.get("price")) for point in points]
    confirm_vals: List[int] = []
    forecast_vals: List[int] = []
    confirm_acc = 0
    forecast_acc = 0
    for point in points:
        confirm_acc += _accuracy_confirm_value(point)
        forecast_acc += _accuracy_prediction_value(point)
        confirm_vals.append(confirm_acc)
        forecast_vals.append(forecast_acc)
    price_min, price_max = min(prices), max(prices)
    dir_min, dir_max = min(confirm_vals + forecast_vals), max(confirm_vals + forecast_vals)
    price_span = max((price_max - price_min) * 1.12, price_max * 0.001, 0.01)
    dir_span = max((dir_max - dir_min) * 1.12, 1.0)
    price_base = price_min - (price_span - (price_max - price_min)) / 2
    dir_base = dir_min - (dir_span - (dir_max - dir_min)) / 2

    def x_at(index: int) -> float:
        return pad_l + chart_w * index / max(1, len(points) - 1)

    def y_price(value: float) -> float:
        norm = (value - price_base) / price_span
        return pad_t + chart_h - norm * chart_h

    def y_dir(value: int) -> float:
        norm = (value - dir_base) / dir_span
        return pad_t + chart_h - norm * chart_h

    def polyline(values: List[float], y_fn, color: str, width: float = 2.0) -> str:
        coords = " ".join(f"{x_at(i):.1f},{y_fn(values[i]):.1f}" for i in range(len(values)))
        return f'<polyline fill="none" stroke="{color}" stroke-width="{width}" points="{coords}"/>'

    grid_lines = []
    for step in range(5):
        y = pad_t + chart_h * step / 4
        grid_lines.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width-pad_r}" y2="{y:.1f}" stroke="rgba(148,163,184,0.22)" stroke-width="1"/>')
    markers = []
    push_times = {str(point.get("time") or "") for point in points if point.get("would_push")}
    for marker in bundle.get("push_markers") or []:
        if not isinstance(marker, dict):
            continue
        key = str(marker.get("time") or "")
        if not key or key in push_times:
            continue
        try:
            price = float(marker.get("price"))
        except (TypeError, ValueError):
            continue
        index = min(range(len(points)), key=lambda idx: abs(idx - (len(points) - 1) * 0.5))
        for idx, point in enumerate(points):
            if str(point.get("time") or "") == key:
                index = idx
                break
        markers.append(
            f'<circle cx="{x_at(index):.1f}" cy="{y_price(price)-16:.1f}" r="5" fill="#f472b6" stroke="#831843" stroke-width="1.2"/>'
        )
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        f'<rect width="100%" height="100%" fill="#0f172a"/>',
        f'<text x="{pad_l}" y="28" fill="#e2e8f0" font-family="Segoe UI, Microsoft YaHei, sans-serif" font-size="18">{title}</text>',
        f'<text x="{pad_l}" y="48" fill="#94a3b8" font-family="Segoe UI, Microsoft YaHei, sans-serif" font-size="13">{html.escape(subtitle)}</text>',
        *grid_lines,
        polyline(prices, y_price, "#60a5fa", 2.4),
        polyline([float(v) for v in forecast_vals], y_dir, "#a78bfa", 2.0),
        polyline([float(v) for v in confirm_vals], y_dir, "#fbbf24", 2.0),
        *markers,
        f'<text x="{pad_l}" y="{height-18}" fill="#60a5fa" font-family="Segoe UI, Microsoft YaHei, sans-serif" font-size="12">蓝线价格</text>',
        f'<text x="{pad_l+72}" y="{height-18}" fill="#4ade80" font-family="Segoe UI, Microsoft YaHei, sans-serif" font-size="12">绿线模拟</text>',
        f'<text x="{width-pad_r}" y="{height-18}" fill="#94a3b8" font-family="Segoe UI, Microsoft YaHei, sans-serif" font-size="12" text-anchor="end">绘制 {len(points)}/{len(raw_points)} 点</text>',
        "</svg>",
    ]
    return "\n".join(parts)


def build_accuracy_export_bundle(
    inst_id: str,
    scope: str,
    horizon_seconds: int,
    *,
    retention_hours: float = DEFAULT_ACCURACY_RETENTION_HOURS,
    interval_seconds: Optional[int] = None,
    for_diagnostic: bool = False,
) -> Dict[str, Any]:
    config = normalize_config(load_config())
    interval = max(1, int(interval_seconds or config.get("interval", 5)))
    retention = max(0.5, min(168.0, float(retention_hours or DEFAULT_ACCURACY_RETENTION_HOURS)))
    horizon = clamp_accuracy_horizon_seconds(horizon_seconds)
    try:
        report = accuracy_report(
            inst_id,
            horizon_seconds=horizon,
            scope=scope,
            retention_hours=retention,
            interval_seconds=interval,
            for_diagnostic=for_diagnostic,
        )
        return {
            "name": "OKX_Accuracy_Chart",
            "version": "1.0",
            "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "inst_id": inst_id,
            "horizon_seconds": horizon,
            "scope": scope,
            "retention_hours": retention,
            "interval_seconds": interval,
            "max_points": report.get("max_points"),
            "start_at": report.get("start_at", ""),
            "time_start": report.get("time_start", ""),
            "time_end": report.get("time_end", ""),
            "summary": report.get("summary") or {},
            "points": report.get("points") or [],
            "push_markers": report.get("push_markers") or [],
            "chart_points": report.get("chart_points"),
            "replay_pending": report.get("replay_pending"),
            "hint": report.get("hint"),
        }
    except Exception as exc:
        return {
            "name": "OKX_Accuracy_Chart",
            "version": "1.0",
            "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "inst_id": inst_id,
            "horizon_seconds": horizon,
            "scope": scope,
            "error": str(exc),
            "points": [],
            "summary": {},
        }


def diagnostic_readme_text(manifest: Dict[str, Any]) -> str:
    lines = [
        "OKX AI Assistant 诊断包",
        "=" * 40,
        f"导出时间: {manifest.get('exported_at', '')}",
        f"应用版本: {manifest.get('app_version', '')}",
        "",
        "目录说明:",
        "- README.txt / manifest.json — 本说明与文件清单",
        "- config/bundle.json — 配置与环境变量（API Key / SendKey 已打码）",
        "- status/ — 监控、回放、Token 统计",
        "- logs/ — 运行时全量日志（分析 JSONL 分卷、控制台、回放、校准、模拟账户等）",
        "- state/ — 当前 PID、配置快照等运行态文件",
        "- accuracy/ — 实时/回放/全部历史压测数据（JSON，可导入测试页「导入图表」）",
        "- charts/svg/ — 压测走势图（SVG，浏览器可直接打开）",
        "- charts/png/ — 压测/监控走势图（PNG，浏览器导出时附带）",
        "- client/ — 浏览器侧附加（AI 对话、当前压测 UI 快照，如有）",
        "",
        "注意:",
        "- 超大文件仅包含尾部片段，详见 manifest.json 中 truncated 标记",
        "- 请勿将未打码的密钥分享给他人",
        "",
        "文件清单:",
    ]
    for item in manifest.get("files", []):
        flag = ""
        if item.get("truncated"):
            flag = " [tail]"
        elif item.get("note"):
            flag = f" [{item['note']}]"
        lines.append(f"- {item.get('name')} ({item.get('bytes', 0)} bytes){flag}")
    return "\n".join(lines) + "\n"


def create_diagnostic_zip(extras: Optional[Dict[str, Any]] = None) -> Tuple[bytes, str, Dict[str, Any]]:
    extras = extras or {}
    config = normalize_config(load_config())
    insts = configured_instruments()
    interval = max(1, int(config.get("interval", 5)))
    retention = DEFAULT_ACCURACY_RETENTION_HOURS
    files_meta: List[Dict[str, Any]] = []
    total_added = 0
    buf = io.BytesIO()

    def budget_left() -> int:
        return max(0, DIAG_EXPORT_MAX_ZIP_BYTES - total_added)

    def add_bytes(name: str, data: bytes, meta: Optional[Dict[str, Any]] = None) -> bool:
        nonlocal total_added
        if not data:
            return False
        if len(data) > budget_left():
            files_meta.append({"name": name, "included": False, "bytes": 0, "note": "zip budget exceeded", **(meta or {})})
            return False
        zf.writestr(name, data)
        total_added += len(data)
        files_meta.append({"name": name, "included": True, "bytes": len(data), **(meta or {})})
        return True

    def add_json(name: str, payload: Any, meta: Optional[Dict[str, Any]] = None) -> bool:
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        return add_bytes(name, text.encode("utf-8"), meta)

    def add_path(name: str, path: Path, *, max_bytes: int, prefer_tail: bool = True) -> None:
        content, meta = export_file_bytes(path, max_bytes=max_bytes, prefer_tail=prefer_tail)
        meta["archive_name"] = name
        if content is not None:
            add_bytes(name, content, meta)
        else:
            files_meta.append({"name": name, **meta})

    exported_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"okx_diagnostic_{stamp}.zip"

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        config_bundle = export_config_bundle()
        add_json(
            "config/bundle.json",
            {
                "name": "OKX_AI_Diagnostic",
                "version": "1.1",
                "exported_at": exported_at,
                "config": config_bundle.get("config", {}),
                "env": redact_secrets_mapping(config_bundle.get("env", {})),
            },
        )
        add_json("status/monitor.json", monitor_status())
        add_json("status/replay.json", replay_dataset_info(lite=False))
        add_json("status/tokens.json", load_ai_token_stats(AI_TOKEN_STATS_FILE))
        add_json(
            "status/runtime.json",
            {
                "app_version": APP_VERSION,
                "log_dir": str(LOG_DIR),
                "monitor_log_start_at": MONITOR_LOG_START_AT,
                "monitor_started_at": MONITOR_STARTED_AT,
                "monitor_stopped_at": MONITOR_STOPPED_AT,
                "replay_started_at": REPLAY_STARTED_AT,
                "replay_stopped_at": REPLAY_STOPPED_AT,
                "log_size_summary": log_size_summary_text(config),
                "diagnostic_log_files": [
                    {"archive": name, "path": str(path), "max_bytes": max_bytes, "prefer_tail": prefer_tail}
                    for name, path, max_bytes, prefer_tail in iter_diagnostic_log_entries()
                ],
            },
        )

        for archive_name, path, max_bytes, prefer_tail in iter_diagnostic_log_entries():
            add_path(archive_name, path, max_bytes=max_bytes, prefer_tail=prefer_tail)

        diagnostic_scopes = ["session", "all"]
        if diagnostic_replay_log_has_data():
            diagnostic_scopes.append("replay")
        for inst in insts:
            safe_inst = str(inst).replace("/", "_")
            for horizon in diagnostic_accuracy_horizons(config):
                for scope in diagnostic_scopes:
                    bundle = build_accuracy_export_bundle(
                        inst,
                        scope,
                        horizon,
                        retention_hours=retention,
                        interval_seconds=interval,
                        for_diagnostic=True,
                    )
                    base = f"accuracy/{scope}/{safe_inst}_h{horizon}"
                    add_json(f"{base}.json", bundle)
                    svg = render_accuracy_chart_svg(bundle)
                    add_bytes(
                        f"charts/svg/{scope}_{safe_inst}_h{horizon}.svg",
                        svg.encode("utf-8"),
                        {"kind": "accuracy_chart_svg", "scope": scope, "inst_id": inst},
                    )

        ai_chat = extras.get("ai_chat")
        if isinstance(ai_chat, list) and ai_chat:
            add_json("client/ai_chat.json", {"exported_at": exported_at, "messages": ai_chat})
        accuracy_snapshot = extras.get("accuracy_snapshot")
        if isinstance(accuracy_snapshot, dict) and accuracy_snapshot:
            add_json("client/accuracy_ui_snapshot.json", accuracy_snapshot)
        accuracy_images = extras.get("accuracy_images")
        if isinstance(accuracy_images, list):
            for item in accuracy_images:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "accuracy_chart.png").replace("\\", "_").replace("/", "_")
                if not name.lower().endswith(".png"):
                    name += ".png"
                raw_b64 = str(item.get("data") or item.get("png_base64") or "").strip()
                if raw_b64.startswith("data:"):
                    raw_b64 = raw_b64.split(",", 1)[-1]
                if not raw_b64:
                    continue
                try:
                    png_bytes = base64.b64decode(raw_b64, validate=True)
                except ValueError:
                    continue
                add_bytes(f"charts/png/{name}", png_bytes, {"kind": "accuracy_chart_png"})
        monitor_chart_png = extras.get("monitor_chart_png")
        if isinstance(monitor_chart_png, str) and monitor_chart_png.strip():
            raw_b64 = monitor_chart_png.strip()
            if raw_b64.startswith("data:"):
                raw_b64 = raw_b64.split(",", 1)[-1]
            try:
                png_bytes = base64.b64decode(raw_b64, validate=True)
                add_bytes("charts/png/monitor_kline.png", png_bytes, {"kind": "monitor_chart_png"})
            except ValueError:
                pass

        manifest = {
            "name": "OKX_AI_Diagnostic",
            "version": "1.1",
            "exported_at": exported_at,
            "app_version": APP_VERSION,
            "zip_bytes": total_added,
            "zip_budget_bytes": DIAG_EXPORT_MAX_ZIP_BYTES,
            "inst_ids": insts,
            "files": files_meta,
        }
        add_json("manifest.json", manifest)
        add_bytes("README.txt", diagnostic_readme_text(manifest).encode("utf-8"))

    return buf.getvalue(), filename, manifest


def test_push_connection() -> Dict[str, Any]:
    send_key = build_child_env().get("WECHAT_SEND_KEY", "").strip()
    if not send_key:
        return {"ok": False, "message": "推送测试失败：未配置 WECHAT_SEND_KEY。请先在配置页填写并保存。"}
    try:
        monitor = get_signal_monitor()
        config = normalize_config(load_config())
        title, desp = monitor.build_wechat_push_format_preview(config)
        title = f"[格式预览] {title}"
        desp = "> 以下为模拟数据，仅用于查看推送排版；真实监控推送使用当时行情与 AI 结果。\n\n" + desp
        raw = post_json(
            f"https://sctapi.ftqq.com/{send_key}.send",
            {"title": title, "desp": desp},
        )
        ok, detail = parse_serverchan_response(raw)
        if ok:
            return {"ok": True, "message": f"微信推送成功（格式预览已发送）：{detail}"}
        return {"ok": False, "message": f"微信推送失败：{detail}"}
    except Exception as exc:
        return {"ok": False, "message": f"微信推送失败：{exc}"}


def tail_text(path: Path, max_bytes: int = 160000) -> str:
    if not path.exists():
        return ""
    size = path.stat().st_size
    with path.open("rb") as file:
        if size > max_bytes:
            file.seek(size - max_bytes)
        return file.read().decode("utf-8", errors="replace")


def iter_json_log_lines(path: Path, *, read_full: bool = False, max_tail_bytes: int = None):
    """Stream JSONL lines. Replay logs are one session and must be read in full."""
    if max_tail_bytes is None:
        max_tail_bytes = DEFAULT_LOG_MAX_BYTES
    if not path.exists():
        return
    try:
        size = path.stat().st_size
    except OSError:
        return
    if read_full or size <= max_tail_bytes:
        with path.open("r", encoding="utf-8-sig") as file:
            for line in file:
                stripped = line.strip()
                if stripped:
                    yield stripped
        return
    for line in tail_text(path, max_tail_bytes).splitlines():
        stripped = line.strip()
        if stripped:
            yield stripped


def monitor_log_text() -> str:
    if not MONITOR_LOG_START_AT:
        return "等待启动监控。本窗口只显示本次启动后的 JSON 分析日志，供图表与压测使用。"
    tail_limit = realtime_log_tail_bytes()
    lines = []
    for line in tail_analysis_log_text(MONITOR_JSON_LOG_FILE, tail_limit).splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if str(item.get("time", "")) >= MONITOR_LOG_START_AT:
            lines.append(line)
    if len(lines) > LOG_DISPLAY_MAX_LINES:
        lines = lines[-LOG_DISPLAY_MAX_LINES:]
        header = f"... 仅显示最近 {LOG_DISPLAY_MAX_LINES} 行（共读取更多） ...\n"
        return header + "\n".join(lines)
    return "\n".join(lines) or "暂无本次启动后的JSON分析日志。"


def monitor_console_log_text() -> str:
    if not MONITOR_LOG_START_AT:
        return "等待启动监控。本窗口只显示本次启动后的控制台摘要，包含信号、推送与异常。"
    rotate_console_log_if_needed(MONITOR_PROCESS_LOG_FILE)
    if not MONITOR_PROCESS_LOG_FILE.exists():
        return "暂无控制台日志文件。"
    marker = f"===== signal monitor started at {MONITOR_LOG_START_AT} ====="
    text = tail_text(MONITOR_PROCESS_LOG_FILE, 4 * 1024 * 1024)
    idx = text.rfind(marker)
    if idx >= 0:
        session_text = text[idx + len(marker) :].lstrip("\n")
        return session_text or "监控已启动，等待控制台输出..."
    return text.strip() or "暂无本次启动后的控制台日志。"


def read_realtime_log_points(inst_id: str, max_points: int = 20000) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    cache_key = f"{inst_id}:{MONITOR_LOG_START_AT}:{realtime_log_tail_bytes()}"
    now = time.time()
    cached = _REALTIME_POINTS_CACHE.get(cache_key)
    if cached and now - cached[0] < REALTIME_LOG_CACHE_SECONDS:
        realtime_points, log_chart_points = cached[1]
        return realtime_points[-max_points:], log_chart_points

    realtime_points: List[Dict[str, Any]] = []
    log_chart_points: List[Dict[str, Any]] = []
    tail_limit = realtime_log_tail_bytes()
    for line in tail_analysis_log_text(MONITOR_JSON_LOG_FILE, tail_limit).splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if item.get("inst_id") != inst_id:
            continue
        try:
            price = float(item.get("price"))
        except (TypeError, ValueError):
            continue
        if MONITOR_LOG_START_AT and str(item.get("time", "")) < MONITOR_LOG_START_AT:
            continue
        item_chart_points = chart_points_from_log_item(item)
        if item_chart_points:
            log_chart_points = item_chart_points
        realtime_points.append(point_from_log_item(item, price))
    result = (realtime_points[-max_points:], log_chart_points)
    _REALTIME_POINTS_CACHE[cache_key] = (now, result)
    if len(_REALTIME_POINTS_CACHE) > 16:
        stale_before = now - REALTIME_LOG_CACHE_SECONDS * 4
        for key, (saved_at, _) in list(_REALTIME_POINTS_CACHE.items()):
            if saved_at < stale_before:
                _REALTIME_POINTS_CACHE.pop(key, None)
    return result


def read_monitor_candles(inst_id: str, bar: str = "1m", max_points: int = 200) -> Dict[str, Any]:
    bar = normalize_monitor_bar(bar)
    inst_id = normalize_inst_id(inst_id)
    running = bool(monitor_status()["running"])
    realtime_points, log_chart_points = read_realtime_log_points(inst_id)
    latest_metrics = realtime_points[-1] if realtime_points else None
    points: List[Dict[str, Any]] = []
    source = "okx-candles"
    try:
        points = get_history_candle_points(inst_id, bar, candle_fetch_limit(bar))
    except Exception:
        points = []
    if not points and bar == "1m" and log_chart_points:
        points = log_chart_points
        source = "signal-monitor-chart"
    if points and latest_metrics and running:
        if points[-1].get("raw_total_score") is None:
            window = metrics_overlay_window_seconds(bar)
            if seconds_between_time_text(points[-1].get("time"), latest_metrics.get("time")) <= window:
                last = points[-1]
                points[-1] = {
                    **latest_metrics,
                    **last,
                    "price": last.get("close", last.get("price")),
                    "kind": "history",
                    "bar": bar,
                }
    if not points:
        return {
            "ok": True,
            "inst_id": inst_id,
            "bar": bar,
            "running": running,
            "points": [],
            "price": 0,
            "change": 0,
            "change_pct": 0,
            "source": source,
            "latest_snapshot": latest_metrics,
            "paper_account": read_paper_account(inst_id) if running else {},
        }
    if len(points) == 1:
        dup = dict(points[0])
        points.append(dup)
    first_close = float(points[0].get("close", points[0].get("price", 0)))
    last_close = float(points[-1].get("close", points[-1].get("price", 0)))
    change = last_close - first_close
    paper_account = read_paper_account(inst_id)
    if not paper_account and latest_metrics:
        if latest_metrics.get("paper_equity") is not None:
            paper_account = {
                "equity": latest_metrics.get("paper_equity"),
                "pnl_usd": latest_metrics.get("paper_pnl_usd"),
                "pnl_pct": latest_metrics.get("paper_pnl_pct"),
                "position_label": latest_metrics.get("paper_position"),
                "trade_count": latest_metrics.get("paper_trade_count"),
                "initial_capital": latest_metrics.get("paper_initial_capital"),
                "direction": latest_metrics.get("final_direction"),
            }
    return {
        "ok": True,
        "inst_id": inst_id,
        "bar": bar,
        "running": running,
        "points": points[-max(2, max_points) :],
        "price": last_close,
        "change": change,
        "change_pct": (change / first_close * 100) if first_close else 0,
        "source": source,
        "latest_snapshot": latest_metrics,
        "paper_account": paper_account,
    }


def read_monitor_points(inst_id: str, max_points: int = 20000) -> Dict[str, Any]:
    return read_monitor_candles(inst_id, "1m", max_points=min(max_points, 200))


def parse_history_time(value: str) -> datetime:
    text = value.strip().replace("T", " ")
    if len(text) == 16:
        text += ":00"
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")


def parse_level_pair(text: Any) -> Tuple[float, float]:
    if not text or text == "-":
        return 0.0, 0.0
    parts = [part.strip() for part in str(text).split("-")]
    if len(parts) < 2:
        value = float(parts[0])
        return value, value
    low, high = float(parts[0]), float(parts[1])
    return (low, high) if low <= high else (high, low)


def parse_targets(text: Any) -> List[float]:
    if not text or text == "-":
        return []
    out = []
    for part in str(text).replace("/", " ").split():
        try:
            out.append(float(part))
        except ValueError:
            continue
    return out


def direction_value(direction: Any) -> int:
    if direction == "做多":
        return 1
    if direction == "做空":
        return -1
    return 0


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if number == number else default
    except Exception:
        return default


def nearest_future_price(points: List[Tuple[datetime, float]], target: datetime, tolerance_seconds: int = 15) -> Tuple[float, str]:
    for point_time, price in points:
        if point_time >= target and (point_time - target).total_seconds() <= tolerance_seconds:
            return price, point_time.strftime("%Y-%m-%d %H:%M:%S")
    return 0.0, ""


def read_accuracy_items(
    inst_id: str,
    since_time: datetime = None,
    log_file: Path = None,
    retention_hours: float = None,
) -> Tuple[List[Dict[str, Any]], List[Tuple[datetime, float]]]:
    log_path = log_file or MONITOR_JSON_LOG_FILE
    tail_limit = accuracy_log_tail_bytes()
    if log_path.resolve() == REPLAY_ANALYSIS_LOG_FILE.resolve():
        try:
            log_size = log_path.stat().st_size if log_path.exists() else 0
        except OSError:
            log_size = 0
        read_full = log_size <= tail_limit
        line_iter = iter_json_log_lines(log_path, read_full=read_full, max_tail_bytes=tail_limit)
    else:
        total_bytes = analysis_log_total_bytes(log_path)
        read_full = total_bytes <= tail_limit
        line_iter = iter_analysis_log_lines(log_path, read_full=read_full, max_tail_bytes=tail_limit)
    items = []
    price_by_time: Dict[str, Tuple[datetime, float]] = {}
    for line in line_iter:
        try:
            item = json.loads(line)
            if item.get("inst_id") != inst_id:
                continue
            item_time = parse_history_time(str(item.get("time", "")))
            price = float(item.get("price"))
        except Exception:
            continue
        if since_time and item_time < since_time:
            continue
        # 准确度统计用于验证“本轮预测在下一小段时间内是否正确”，必须保留秒级日志价格。
        # 之前按分钟聚合会把5秒、15秒这类实时验证窗口抹平，导致用户看到的结果滞后且不直观。
        price_by_time[item_time.strftime("%Y-%m-%d %H:%M:%S")] = (item_time, price)
        chart = item.get("chart") if isinstance(item.get("chart"), dict) else {}
        rows = chart.get("points") if chart.get("bar") == "1m" and isinstance(chart.get("points"), list) else []
        for row in rows:
            try:
                row_time = parse_history_time(str(row.get("time", "")))
                close = float(row.get("close"))
            except Exception:
                continue
            # K线收盘价只作为日志价格缺口时的兜底；秒级日志点会自然覆盖同一时间的价格。
            price_by_time.setdefault(row_time.strftime("%Y-%m-%d %H:%M:%S"), (row_time, close))
        items.append(item)
    if retention_hours and items:
        keep_hours = max(0.5, min(168.0, float(retention_hours)))
        latest = max(parse_history_time(str(item.get("time", ""))) for item in items)
        cutoff = latest - timedelta(hours=keep_hours)
        items = [item for item in items if parse_history_time(str(item.get("time", ""))) >= cutoff]
    points = sorted(price_by_time.values(), key=lambda pair: pair[0])
    items.sort(key=lambda item: str(item.get("time", "")))
    return items, points


def realtime_accuracy_threshold_pct(horizon_seconds: int) -> float:
    """给短窗实时验证使用的最小有效涨跌阈值。

    5秒级验证不能沿用15分钟复盘的0.03%，否则绝大多数样本都会被判成“震荡”。
    这里按验证窗口逐步放宽/收紧：窗口越短，阈值越低；窗口越长，过滤更多噪声。
    """
    if horizon_seconds <= 5:
        return 0.005
    if horizon_seconds <= 15:
        return 0.008
    if horizon_seconds <= 30:
        return 0.010
    if horizon_seconds <= 60:
        return 0.015
    if horizon_seconds <= 180:
        return 0.025
    if horizon_seconds <= 600:
        return 0.040
    if horizon_seconds <= 1200:
        return 0.055
    if horizon_seconds <= 3600:
        return 0.080
    if horizon_seconds <= 14400:
        return 0.100
    return 0.120


def pct_rate(hit: int, total: int) -> float:
    return (hit / total * 100) if total else 0.0


PUSH_KIND_LABELS = {
    "trade": "结构单",
    "spike": "急变",
    "watch": "观察",
    "forecast": "演变",
}
PUSH_KIND_PRIORITY = ("trade", "spike", "forecast", "watch")


def push_marker_from_log_item(item: Dict[str, Any]) -> Dict[str, Any]:
    empty: Dict[str, Any] = {
        "would_push": False,
        "push_kind": "",
        "push_kinds": [],
        "push_direction": "",
        "push_label": "",
    }
    push_analysis = item.get("push_analysis")
    if not isinstance(push_analysis, dict) or not push_analysis.get("would_push"):
        return empty
    tracks = push_analysis.get("tracks") if isinstance(push_analysis.get("tracks"), list) else []
    would = [track for track in tracks if isinstance(track, dict) and track.get("status") == "would_push"]
    if not would:
        return empty
    kinds: List[str] = []
    for kind in PUSH_KIND_PRIORITY:
        if any(str(track.get("kind", "") or "") == kind for track in would):
            kinds.append(kind)
    for track in would:
        kind = str(track.get("kind", "") or "")
        if kind and kind not in kinds:
            kinds.append(kind)
    primary = kinds[0] if kinds else str(would[0].get("kind", "") or "")
    final_decision = item.get("final_decision") if isinstance(item.get("final_decision"), dict) else {}
    direction = str(final_decision.get("direction", "观望") or "观望")
    for track in would:
        if str(track.get("kind", "") or "") == "forecast":
            direction = str(track.get("direction", direction) or direction)
            break
    kind_labels = [PUSH_KIND_LABELS.get(kind, kind) for kind in kinds]
    return {
        "would_push": True,
        "push_kind": primary,
        "push_kinds": kinds,
        "push_direction": direction,
        "push_label": "+".join(kind_labels) if kind_labels else primary,
    }


def new_paper_account_state() -> Dict[str, Any]:
    return {
        "initial_capital": PAPER_INITIAL_CAPITAL,
        "cash": PAPER_INITIAL_CAPITAL,
        "equity": PAPER_INITIAL_CAPITAL,
        "pnl_usd": 0.0,
        "pnl_pct": 0.0,
        "position": "flat",
        "position_label": "空仓",
        "entry_price": 0.0,
        "basis_equity": 0.0,
        "direction": "观望",
        "trade_count": 0,
    }


def direction_from_log_item(item: Dict[str, Any]) -> str:
    if local_analysis_mode_from_log_item(item):
        score = item.get("score") if isinstance(item.get("score"), dict) else {}
        direction = score.get("final_direction", score.get("direction", "观望"))
        return str(direction or "观望")
    final_decision = item.get("final_decision") if isinstance(item.get("final_decision"), dict) else {}
    direction = final_decision.get("direction")
    if direction in ("做多", "做空", "观望"):
        return str(direction)
    score = item.get("score") if isinstance(item.get("score"), dict) else {}
    direction = score.get("final_direction", score.get("direction", "观望"))
    return str(direction or "观望")


def paper_direction_from_log_item(item: Dict[str, Any], *, paper_follow_ai_only: bool = True) -> str:
    if local_analysis_mode_from_log_item(item):
        return direction_from_log_item(item)
    final_decision = item.get("final_decision") if isinstance(item.get("final_decision"), dict) else {}
    if not paper_follow_ai_only:
        return direction_from_log_item(item)
    if str(final_decision.get("decision_source", "") or "") != "ai":
        return "观望"
    forward = final_decision.get("forward_view") if isinstance(final_decision.get("forward_view"), dict) else {}
    direction = forward.get("direction") or final_decision.get("direction", "观望")
    return str(direction or "观望")


def _paper_fee_rate(fee_bps: float) -> float:
    return max(0.0, float(fee_bps)) / 10000.0


def _apply_paper_fee(amount: float, fee_bps: float) -> float:
    if amount <= 0:
        return amount
    return amount * (1.0 - _paper_fee_rate(fee_bps))


def _paper_position_from_direction(direction: str) -> str:
    if direction == "做多":
        return "long"
    if direction == "做空":
        return "short"
    return "flat"


def _paper_position_label(position: str) -> str:
    return {"long": "做多", "short": "做空", "flat": "空仓"}.get(position, "空仓")


def _mark_paper_equity(state: Dict[str, Any], price: float) -> None:
    position = state.get("position", "flat")
    entry_price = safe_float(state.get("entry_price"), 0.0)
    basis_equity = safe_float(state.get("basis_equity"), 0.0)
    if position == "long" and entry_price > 0:
        equity = basis_equity * (price / entry_price)
    elif position == "short" and entry_price > 0:
        equity = basis_equity * (1 + (entry_price - price) / entry_price)
    else:
        equity = safe_float(state.get("cash"), PAPER_INITIAL_CAPITAL)
    initial = safe_float(state.get("initial_capital"), PAPER_INITIAL_CAPITAL)
    state["equity"] = equity
    state["pnl_usd"] = equity - initial
    state["pnl_pct"] = (state["pnl_usd"] / initial * 100) if initial else 0.0


def _close_paper_position(state: Dict[str, Any], price: float, fee_bps: float = 5.0) -> None:
    position = state.get("position", "flat")
    entry_price = safe_float(state.get("entry_price"), 0.0)
    basis_equity = safe_float(state.get("basis_equity"), 0.0)
    if position == "long" and entry_price > 0:
        state["cash"] = _apply_paper_fee(basis_equity * (price / entry_price), fee_bps)
    elif position == "short" and entry_price > 0:
        state["cash"] = _apply_paper_fee(basis_equity * (1 + (entry_price - price) / entry_price), fee_bps)
    state["position"] = "flat"
    state["position_label"] = "空仓"
    state["entry_price"] = 0.0
    state["basis_equity"] = 0.0


def _open_paper_position(state: Dict[str, Any], position: str, price: float, direction: str, fee_bps: float = 5.0) -> None:
    state["position"] = position
    state["position_label"] = _paper_position_label(position)
    state["entry_price"] = price
    state["basis_equity"] = _apply_paper_fee(safe_float(state.get("cash"), PAPER_INITIAL_CAPITAL), fee_bps)
    state["cash"] = 0.0
    state["direction"] = direction
    state["trade_count"] = int(state.get("trade_count", 0) or 0) + 1


def step_paper_account_state(state: Dict[str, Any], price: float, direction: str, fee_bps: float = 5.0) -> None:
    target = _paper_position_from_direction(direction)
    current = state.get("position", "flat")
    if target != current:
        if current != "flat":
            _close_paper_position(state, price, fee_bps)
        if target != "flat":
            _open_paper_position(state, target, price, direction, fee_bps)
        else:
            state["direction"] = "观望"
            state["position_label"] = "空仓"
    else:
        state["direction"] = direction
        if target != "flat":
            state["position_label"] = _paper_position_label(target)
    _mark_paper_equity(state, price)


def simulate_paper_account_series(
    items: List[Dict[str, Any]],
    config: Optional[Dict[str, Any]] = None,
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Any], Dict[str, int]]:
    """按日志 config_snapshot（有则用）或当前配置，对日志逐轮模拟 $10k 跟单账户。"""
    fallback = config or load_config()
    state = new_paper_account_state()
    by_time: Dict[str, Dict[str, Any]] = {}
    from_log_snapshot = 0
    from_current_config = 0
    for item in items:
        try:
            price = float(item.get("price"))
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue
        settings, source = paper_settings_from_log_item(item, fallback)
        if source == "log_snapshot":
            from_log_snapshot += 1
        else:
            from_current_config += 1
        step_paper_account_state(
            state,
            price,
            paper_direction_from_log_item(item, paper_follow_ai_only=settings["paper_follow_ai_only"]),
            settings["paper_fee_bps"],
        )
        by_time[str(item.get("time", ""))] = dict(state)
    return by_time, dict(state), {
        "from_log_snapshot": from_log_snapshot,
        "from_current_config": from_current_config,
    }


def trade_direction_hit(
    pred_value: int,
    actual_return_pct: float,
    threshold_pct: float,
) -> bool:
    """交易方向是否在验证窗内按预期波动（与策略周期匹配，不看入场/止盈）。"""
    if pred_value > 0:
        return actual_return_pct > threshold_pct
    if pred_value < 0:
        return actual_return_pct < -threshold_pct
    return False


def direction_reasonable_hit(direction: str, advice: Dict[str, Any], threshold_pct: float) -> bool:
    value = direction_value(str(direction or "观望"))
    actual_return = safe_float(advice.get("actual_return_pct"), 0.0)
    if value == 0:
        return bool(advice.get("baseline_watch_hit"))
    return trade_direction_hit(value, actual_return, threshold_pct)


def build_accuracy_pending_row(
    item: Dict[str, Any],
    item_time: datetime,
    price: float,
    paper_by_time: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    effective = effective_fields_from_log_item(item)
    score = item.get("score") if isinstance(item.get("score"), dict) else {}
    item_time_text = item_time.strftime("%Y-%m-%d %H:%M:%S")
    paper = paper_by_time.get(item_time_text, {})
    prediction_direction, prediction_source = prediction_direction_from_log_item(item)
    push_marker = push_marker_from_log_item(item)
    predicted = str(effective.get("final_direction", effective.get("direction", "观望")) or "观望")
    raw_direction = effective.get("raw_direction", predicted)
    ai_forward_ctx = ai_forward_from_log_item(item)
    ai_row: Dict[str, Any] = {}
    if ai_forward_ctx:
        ai_row = {
            "ai_forward_direction": ai_forward_ctx.get("direction"),
            "ai_forward_horizon_minutes": ai_forward_ctx.get("horizon_minutes"),
            "ai_forward_probability": ai_forward_ctx.get("probability"),
            "ai_forward_hit": None,
            "ai_forward_outcome": "pending",
        }
    return {
        "time": item_time_text,
        "future_time": "",
        "price": price,
        "future_price": None,
        "raw_direction": raw_direction,
        "prediction_direction": prediction_direction,
        "prediction_source": prediction_source,
        "confirm_direction": predicted,
        "final_direction": predicted,
        "actual_direction": None,
        "actual_return_pct": None,
        "verified": False,
        "hit": None,
        "trade_direction_hit": None,
        "trade_plan_hit": None,
        "price_strict_hit": None,
        "baseline_watch_hit": None,
        "trend_bias_hit": None,
        "watch_threshold_pct": None,
        "outcome_type": "pending",
        "entry_touched": False,
        "no_fill": False,
        "stop_hit": False,
        "take_profit_hit": False,
        "mfe_pct": None,
        "mae_pct": None,
        "raw_total_score": score.get("raw_total_score", score.get("total_score")),
        "final_trade_score": score.get("final_trade_score"),
        "market_regime": score.get("market_regime"),
        "market_risk_level": score.get("market_risk_level", score.get("risk_level")),
        "paper_equity": paper.get("equity"),
        "paper_pnl_usd": paper.get("pnl_usd"),
        "paper_pnl_pct": paper.get("pnl_pct"),
        "paper_position": paper.get("position_label"),
        "paper_trade_count": paper.get("trade_count"),
        "would_push": push_marker.get("would_push", False),
        "push_kind": push_marker.get("push_kind", ""),
        "push_kinds": push_marker.get("push_kinds", []),
        "push_direction": push_marker.get("push_direction", ""),
        "push_label": push_marker.get("push_label", ""),
        **ai_row,
    }


def watch_reasonable_threshold_pct(item: Dict[str, Any], base_threshold_pct: float) -> float:
    """判断“观望是否合理”的短窗阈值。

    观望不是预测价格绝对不动，而是认为当前没有足够交易价值。这里用短窗基础阈值、
    盘口价差和15m ATR共同抬高噪声过滤线，避免把几美元级别的BTC跳动误判成观望失败。
    """
    order_book = item.get("order_book") if isinstance(item.get("order_book"), dict) else {}
    volatility = item.get("volatility") if isinstance(item.get("volatility"), dict) else {}
    spread_pct = safe_float(item.get("spread_pct", order_book.get("spread_pct")), 0.0)
    atr_pct_15m = safe_float(item.get("atr_pct_15m", volatility.get("atr_pct_15m")), 0.0)
    return max(base_threshold_pct * 4.0, spread_pct * 5.0, min(0.08, atr_pct_15m * 0.12))


def future_price_path(points: List[Tuple[datetime, float]], start: datetime, horizon_seconds: int) -> Tuple[List[Tuple[datetime, float]], bool]:
    target = datetime.fromtimestamp(start.timestamp() + horizon_seconds)
    max_grace = max(30, min(120, horizon_seconds * 6))
    end = datetime.fromtimestamp(target.timestamp() + max_grace)
    path = [(point_time, price) for point_time, price in points if start < point_time <= end]
    mature = any(point_time >= target for point_time, _ in path)
    return path, mature


def extend_price_points_for_live_validation(
    price_points: List[Tuple[datetime, float]],
    *,
    session_scope: bool,
    replay_scope: bool,
) -> List[Tuple[datetime, float]]:
    # 实时压测：验证窗口已过后，用最新日志价作为“当前价”补齐，避免必须等下一条日志才成熟。
    if replay_scope or not session_scope or not price_points:
        return price_points
    latest_time, latest_price = price_points[-1]
    now = datetime.now()
    if now <= latest_time:
        return price_points
    return price_points + [(now, latest_price)]


def next_pending_maturity_seconds(
    items: List[Dict[str, Any]],
    price_points: List[Tuple[datetime, float]],
    horizon_seconds: int,
) -> int:
    now = datetime.now()
    wait_seconds: Optional[int] = None
    for item in items:
        try:
            item_time = parse_history_time(str(item.get("time", "")))
        except Exception:
            continue
        _, mature = future_price_path(price_points, item_time, horizon_seconds)
        if mature:
            continue
        target = datetime.fromtimestamp(item_time.timestamp() + horizon_seconds)
        remaining = int(max(0, (target - now).total_seconds()))
        wait_seconds = remaining if wait_seconds is None else min(wait_seconds, remaining)
    return wait_seconds or 0


def ai_forward_from_log_item(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Extract AI forward_view for dedicated hit-rate stats (per-item horizon)."""
    final_decision = item.get("final_decision") if isinstance(item.get("final_decision"), dict) else {}
    analysis = item.get("analysis") if isinstance(item.get("analysis"), dict) else {}
    parsed = analysis.get("parsed") if isinstance(analysis.get("parsed"), dict) else {}
    decision_source = str(final_decision.get("decision_source", "") or "")
    if decision_source != "ai":
        return None

    forward = final_decision.get("forward_view") if isinstance(final_decision.get("forward_view"), dict) else {}
    if not forward and isinstance(parsed.get("forward_view"), dict):
        forward = parsed.get("forward_view")
    if not forward and decision_source == "ai":
        direction = str(final_decision.get("direction", "观望") or "观望")
        if direction not in ("做多", "做空", "观望"):
            direction = "观望"
        forward = {
            "direction": direction,
            "horizon_minutes": 15,
            "probability": final_decision.get("confidence", 0),
            "summary": final_decision.get("summary", ""),
            "invalidation": "-",
        }
    if not forward:
        return None

    direction = str(forward.get("direction") or final_decision.get("direction") or "观望")
    if direction not in ("做多", "做空", "观望"):
        direction = "观望"
    try:
        horizon_minutes = max(5, min(10080, int(round(float(forward.get("horizon_minutes", 15))))))
    except (TypeError, ValueError):
        horizon_minutes = 15
    try:
        probability = max(0, min(100, int(round(float(forward.get("probability", final_decision.get("confidence", 0)))))))
    except (TypeError, ValueError):
        probability = int(final_decision.get("confidence", 0) or 0)

    return {
        "direction": direction,
        "horizon_minutes": horizon_minutes,
        "horizon_seconds": horizon_minutes * 60,
        "probability": probability,
        "summary": str(forward.get("summary", "") or "").strip(),
        "invalidation": str(forward.get("invalidation", "-") or "-"),
        "decision_source": decision_source,
        "ai_called": bool(final_decision.get("ai_called")) or bool(analysis),
    }


def evaluate_ai_forward_advice(
    forward_ctx: Dict[str, Any],
    price: float,
    future_path: List[Tuple[datetime, float]],
    threshold_pct: float,
) -> Dict[str, Any]:
    """Validate AI forward_view against future price path (uses forward horizon)."""
    direction = str(forward_ctx.get("direction", "观望") or "观望")
    pred_value = direction_value(direction)
    future_price = future_path[-1][1] if future_path else 0.0
    future_time = future_path[-1][0].strftime("%Y-%m-%d %H:%M:%S") if future_path else ""
    actual_return = (future_price - price) / price * 100 if price and future_price else 0.0
    if actual_return > threshold_pct:
        actual_value, actual_direction = 1, "上涨"
    elif actual_return < -threshold_pct:
        actual_value, actual_direction = -1, "下跌"
    else:
        actual_value, actual_direction = 0, "震荡"

    path_returns = [((path_price - price) / price * 100) for _, path_price in future_path if price]
    max_abs_pct = max(abs(max(path_returns) if path_returns else 0.0), abs(min(path_returns) if path_returns else 0.0))
    watch_threshold_pct = max(threshold_pct * 4.0, 0.08)

    result = {
        "future_time": future_time,
        "future_price": future_price,
        "direction": direction,
        "actual_direction": actual_direction,
        "actual_return_pct": actual_return,
        "horizon_minutes": forward_ctx.get("horizon_minutes", 15),
        "probability": forward_ctx.get("probability", 0),
        "price_strict_hit": pred_value == actual_value,
        "trade_direction_hit": False,
        "hit": False,
        "outcome_type": "ai_watch",
    }
    if pred_value == 0:
        result["hit"] = max_abs_pct <= watch_threshold_pct
        result["outcome_type"] = "ai_watch_ok" if result["hit"] else "ai_watch_missed"
        return result

    result["trade_direction_hit"] = trade_direction_hit(pred_value, actual_return, threshold_pct)
    result["hit"] = result["trade_direction_hit"]
    result["outcome_type"] = "ai_forward_hit" if result["hit"] else "ai_forward_miss"
    return result


def price_crosses_range(prev_price: float, price: float, low: float, high: float) -> bool:
    if low <= price <= high:
        return True
    if prev_price <= 0:
        return False
    return min(prev_price, price) <= high and max(prev_price, price) >= low


def evaluate_realtime_advice(item: Dict[str, Any], price: float, future_path: List[Tuple[datetime, float]], threshold_pct: float) -> Dict[str, Any]:
    score = effective_fields_from_log_item(item)
    raw_direction = score.get("raw_direction")
    final_direction = score.get("final_direction", score.get("direction", "观望")) or "观望"
    pred_value = direction_value(final_direction)
    raw_value = direction_value(raw_direction)
    future_price = future_path[-1][1] if future_path else 0.0
    future_time = future_path[-1][0].strftime("%Y-%m-%d %H:%M:%S") if future_path else ""
    actual_return = (future_price - price) / price * 100 if price and future_price else 0.0
    if actual_return > threshold_pct:
        actual_value, actual_direction = 1, "上涨"
    elif actual_return < -threshold_pct:
        actual_value, actual_direction = -1, "下跌"
    else:
        actual_value, actual_direction = 0, "震荡"
    path_returns = [((path_price - price) / price * 100) for _, path_price in future_path if price]
    max_up_pct = max(path_returns) if path_returns else 0.0
    max_down_pct = min(path_returns) if path_returns else 0.0
    max_abs_pct = max(abs(max_up_pct), abs(max_down_pct))
    strict_hit = pred_value == actual_value
    trend_hit = raw_value == actual_value if raw_value != 0 else None
    watch_threshold_pct = watch_reasonable_threshold_pct(item, threshold_pct)

    result = {
        "future_time": future_time,
        "future_price": future_price,
        "raw_direction": raw_direction,
        "final_direction": final_direction,
        "actual_direction": actual_direction,
        "actual_return_pct": actual_return,
        "max_up_pct": max_up_pct,
        "max_down_pct": max_down_pct,
        "watch_threshold_pct": watch_threshold_pct,
        "price_strict_hit": strict_hit,
        "trend_bias_hit": trend_hit,
        "baseline_watch_hit": max_abs_pct <= watch_threshold_pct,
        "trade_direction_hit": False,
        "trade_plan_hit": False,
        "entry_touched": False,
        "no_fill": False,
        "stop_hit": False,
        "take_profit_hit": False,
        "trade_resolved": False,
        "trade_win": False,
        "mfe_pct": 0.0,
        "mae_pct": 0.0,
        "hit": False,
        "outcome_type": "watch",
    }
    if pred_value == 0:
        result["hit"] = result["baseline_watch_hit"]
        result["outcome_type"] = "watch_ok" if result["hit"] else "watch_missed_opportunity"
        return result

    result["trade_direction_hit"] = trade_direction_hit(pred_value, actual_return, threshold_pct)

    try:
        entry_low, entry_high = parse_level_pair(score.get("entry"))
        stop = safe_float(score.get("stop_loss"), 0.0) if score.get("stop_loss") not in (None, "-") else 0.0
        targets = parse_targets(score.get("take_profit"))
    except Exception:
        result["outcome_type"] = "trade_bad_levels"
        result["hit"] = result["trade_direction_hit"]
        return result
    first_target = targets[0] if targets else 0.0
    if not entry_low or not entry_high:
        result["outcome_type"] = "trade_missing_levels"
        result["hit"] = result["trade_direction_hit"]
        return result

    entry_price = 0.0
    prev_price = price
    active = False
    for _, path_price in future_path:
        if not active and price_crosses_range(prev_price, path_price, entry_low, entry_high):
            active = True
            result["entry_touched"] = True
            entry_price = min(max(path_price, entry_low), entry_high)
        if active and entry_price:
            move = (path_price - entry_price) / entry_price * 100
            if pred_value < 0:
                move = -move
            result["mfe_pct"] = max(result["mfe_pct"], move)
            result["mae_pct"] = min(result["mae_pct"], move)
            if stop and not result["trade_resolved"]:
                if pred_value > 0 and path_price <= stop:
                    result["stop_hit"] = True
                    result["trade_resolved"] = True
                if pred_value < 0 and path_price >= stop:
                    result["stop_hit"] = True
                    result["trade_resolved"] = True
            if first_target and not result["trade_resolved"]:
                if pred_value > 0 and path_price >= first_target:
                    result["take_profit_hit"] = True
                    result["trade_win"] = True
                    result["trade_resolved"] = True
                if pred_value < 0 and path_price <= first_target:
                    result["take_profit_hit"] = True
                    result["trade_win"] = True
                    result["trade_resolved"] = True
        prev_price = path_price

    if not result["entry_touched"]:
        result["no_fill"] = True
        result["trade_plan_hit"] = True
        result["outcome_type"] = "trade_no_fill"
    elif result["trade_win"]:
        result["trade_plan_hit"] = True
        result["outcome_type"] = "trade_take_profit"
    elif result["stop_hit"]:
        result["trade_plan_hit"] = False
        result["outcome_type"] = "trade_stop_loss"
    else:
        result["trade_plan_hit"] = result["mfe_pct"] >= abs(result["mae_pct"])
        result["outcome_type"] = "trade_open_favorable" if result["trade_plan_hit"] else "trade_open_adverse"
    result["hit"] = result["trade_direction_hit"]
    return result


def empty_accuracy_summary() -> Dict[str, Any]:
    return {
        "total": 0,
        "raw_log_total": 0,
        "analysis_total": 0,
        "ai_call_total": 0,
        "ai_token_total": 0,
        "ai_prompt_token_total": 0,
        "ai_completion_token_total": 0,
        "pending_total": 0,
        "next_pending_seconds": 0,
        "mature_rate_pct": 0.0,
        "reliability_score": 0.0,
        "reliability_level": "样本不足",
        "horizon_seconds": 0,
        "threshold_pct": 0.0,
        "prediction_accuracy_pct": 0.0,
        "prediction_total": 0,
        "prediction_reasonable_pct": 0.0,
        "confirm_total": 0,
        "confirm_reasonable_pct": 0.0,
        "push_total": 0,
        "push_reasonable_pct": 0.0,
        "ai_analysis_total": 0,
        "ai_analysis_reasonable_pct": 0.0,
        "decision_total": 0,
        "decision_accuracy_pct": 0.0,
        "baseline_watch_pct": 0.0,
        "model_edge_pct": 0.0,
        "watch_edge_pct": 0.0,
        "trade_signal_total": 0,
        "trade_direction_accuracy_pct": 0.0,
        "trade_signal_accuracy_pct": 0.0,
        "watch_missed_pct": 0.0,
        "entry_touch_total": 0,
        "entry_touch_pct": 0.0,
        "no_fill_total": 0,
        "no_fill_pct": 0.0,
        "trade_resolved_total": 0,
        "trade_win_rate_pct": 0.0,
        "stop_hit_pct": 0.0,
        "take_profit_pct": 0.0,
        "avg_mfe_pct": 0.0,
        "avg_mae_pct": 0.0,
        "trend_bias_total": 0,
        "trend_bias_accuracy_pct": 0.0,
        "watch_total": 0,
        "watch_reasonable_pct": 0.0,
        "price_strict_accuracy_pct": 0.0,
        "avg_signed_error": 0.0,
        "avg_abs_error": 0.0,
        "paper_initial_capital": PAPER_INITIAL_CAPITAL,
        "paper_equity": PAPER_INITIAL_CAPITAL,
        "paper_pnl_usd": 0.0,
        "paper_pnl_pct": 0.0,
        "paper_position_label": "空仓",
        "paper_trade_count": 0,
        "paper_log_points": 0,
        "ai_invoked_total": 0,
        "ai_forward_total": 0,
        "ai_forward_pending": 0,
        "ai_forward_accuracy_pct": 0.0,
        "ai_forward_direction_total": 0,
        "ai_forward_direction_accuracy_pct": 0.0,
        "ai_forward_watch_total": 0,
        "ai_forward_watch_accuracy_pct": 0.0,
        "ai_forward_horizon_minutes": 15,
        "metric_scopes": dict(ACCURACY_METRIC_SCOPES),
        "primary_forward_metric": "ai_forward_direction_accuracy_pct",
    }


def accuracy_activity_stats(items: List[Dict[str, Any]]) -> Dict[str, int]:
    analysis_total = 0
    ai_call_total = 0
    ai_token_total = 0
    ai_prompt_token_total = 0
    ai_completion_token_total = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        analysis_total += 1
        trigger = item.get("local_trigger") if isinstance(item.get("local_trigger"), dict) else {}
        analysis = item.get("analysis") if isinstance(item.get("analysis"), dict) else {}
        if bool(trigger.get("ai_invoked")) or bool(analysis):
            ai_call_total += 1
        usage = analysis.get("usage") if isinstance(analysis.get("usage"), dict) else {}
        prompt_tokens = max(0, int(safe_float(usage.get("prompt_tokens"), 0)))
        completion_tokens = max(0, int(safe_float(usage.get("completion_tokens"), 0)))
        total_tokens = max(0, int(safe_float(usage.get("total_tokens"), 0)))
        if total_tokens <= 0:
            total_tokens = prompt_tokens + completion_tokens
        ai_prompt_token_total += prompt_tokens
        ai_completion_token_total += completion_tokens
        ai_token_total += total_tokens
    return {
        "analysis_total": analysis_total,
        "ai_call_total": ai_call_total,
        "ai_token_total": ai_token_total,
        "ai_prompt_token_total": ai_prompt_token_total,
        "ai_completion_token_total": ai_completion_token_total,
    }


def reliability_level(
    total: int,
    mature_rate_pct: float,
    watch_reasonable_pct: float,
    watch_total: int,
    trade_direction_pct: float,
    trade_total: int,
) -> Tuple[float, str]:
    """压测可靠性：样本量 + 成熟率 + 分层预测质量（观望/交易分开加权）。"""
    sample_score = min(35.0, total / 120.0 * 35.0)
    mature_score = min(20.0, max(0.0, mature_rate_pct) / 100.0 * 20.0)
    watch_weight = watch_total / total if total else 1.0
    trade_weight = trade_total / total if total else 0.0
    if trade_total >= 8:
        quality_pct = watch_reasonable_pct * watch_weight + trade_direction_pct * trade_weight
    else:
        quality_pct = watch_reasonable_pct
    quality_score = max(0.0, min(35.0, (quality_pct - 55.0) / 35.0 * 35.0))
    trade_coverage = min(10.0, trade_total / 15.0 * 10.0)
    score = sample_score + mature_score + quality_score + trade_coverage
    if total < 30:
        level = "样本不足"
    elif score >= 75 and quality_pct >= 78:
        level = "较可靠"
    elif score >= 55 and quality_pct >= 65:
        level = "观察中"
    else:
        level = "需优化"
    return score, level


def accuracy_chart_max_points(retention_hours: float, interval_seconds: int) -> int:
    """图表保留点数 = 保留时长 / 轮询间隔，与配置页 interval 对齐。"""
    interval_seconds = max(1, int(interval_seconds or 5))
    retention_hours = max(0.5, min(168.0, float(retention_hours or DEFAULT_ACCURACY_RETENTION_HOURS)))
    return max(100, min(50000, int(retention_hours * 3600 / interval_seconds)))


def replay_results_available() -> bool:
    """Only expose replay chart data while replay runs or after it finishes in the current web session."""
    if not REPLAY_ANALYSIS_LOG_FILE.exists():
        return False
    try:
        if REPLAY_ANALYSIS_LOG_FILE.stat().st_size <= 0:
            return False
    except OSError:
        return False
    if replay_status()["running"]:
        return True
    return bool(REPLAY_STARTED_AT and REPLAY_STOPPED_AT)


def accuracy_report(
    inst_id: str,
    horizon_seconds: int = 5,
    max_points: int = 2400,
    scope: str = "session",
    retention_hours: float = DEFAULT_ACCURACY_RETENTION_HOURS,
    interval_seconds: int = 5,
    for_diagnostic: bool = False,
) -> Dict[str, Any]:
    if inst_id not in configured_instruments():
        raise ValueError(f"{inst_id} 未在配置中启用，不能统计压测。")
    horizon_seconds = clamp_accuracy_horizon_seconds(horizon_seconds)
    interval_seconds = max(1, int(interval_seconds or 5))
    retention_hours = max(0.5, min(168.0, float(retention_hours or DEFAULT_ACCURACY_RETENTION_HOURS)))
    max_points = max(100, min(50000, int(max_points or accuracy_chart_max_points(retention_hours, interval_seconds))))
    replay_scope = scope == "replay"
    session_scope = scope == "session"
    log_file = REPLAY_ANALYSIS_LOG_FILE if replay_scope else MONITOR_JSON_LOG_FILE
    threshold_pct = realtime_accuracy_threshold_pct(horizon_seconds)
    empty_accuracy_payload = {
        "ok": True,
        "inst_id": inst_id,
        "horizon_seconds": horizon_seconds,
        "scope": scope,
        "start_at": "",
        "threshold_pct": threshold_pct,
        "rolling_window": 25,
        "summary_scope": "cumulative",
        "summary": empty_accuracy_summary(),
        "points": [],
        "push_markers": [],
        "recent": [],
        "retention_hours": retention_hours,
        "interval_seconds": interval_seconds,
        "max_points": max_points,
        "log_path": str(log_file),
    }
    if replay_scope:
        # 回放日志里的 time 是录制时的虚拟时间，不能用启动回放的墙钟时间过滤。
        # 未点击「开始回放」时不展示磁盘上的旧 replay_analysis.jsonl，避免误以为自动回放。
        if not for_diagnostic and not replay_results_available():
            pending = dict(empty_accuracy_payload)
            pending["replay_pending"] = True
            pending["hint"] = "请先点击下方「开始回放」；切换「回放会话」仅切换压测数据源，不会自动启动回放。"
            return pending
        since_time = None
    elif session_scope:
        since_time = parse_history_time(MONITOR_LOG_START_AT) if MONITOR_LOG_START_AT else None
        if not since_time:
            return empty_accuracy_payload
    else:
        since_time = None
    items, price_points = read_accuracy_items(
        inst_id,
        since_time,
        log_file=log_file,
        retention_hours=retention_hours,
    )
    price_points = extend_price_points_for_live_validation(
        price_points,
        session_scope=session_scope,
        replay_scope=replay_scope,
    )
    next_pending_seconds = next_pending_maturity_seconds(items, price_points, horizon_seconds)
    activity_stats = accuracy_activity_stats(items)
    paper_by_time, paper_final, paper_meta = simulate_paper_account_series(items)
    push_markers: List[Dict[str, Any]] = []
    for item in items:
        marker = push_marker_from_log_item(item)
        if not marker.get("would_push"):
            continue
        try:
            marker_price = float(item.get("price"))
        except (TypeError, ValueError):
            marker_price = 0.0
        push_markers.append(
            {
                "time": str(item.get("time", "") or ""),
                "price": marker_price,
                "would_push": True,
                "push_kind": marker.get("push_kind", ""),
                "push_kinds": marker.get("push_kinds", []),
                "push_direction": marker.get("push_direction", ""),
                "push_label": marker.get("push_label", ""),
            }
        )
    rows = []
    raw_log_total = 0
    pending_total = 0
    decision_total = 0
    decision_hit = 0
    trade_signal_total = 0
    trade_signal_hit = 0
    trade_direction_hit = 0
    entry_touch_total = 0
    no_fill_total = 0
    trade_resolved_total = 0
    trade_win_total = 0
    stop_hit_total = 0
    take_profit_total = 0
    mfe_sum = 0.0
    mae_sum = 0.0
    trend_bias_total = 0
    trend_bias_hit = 0
    watch_total = 0
    watch_hit = 0
    watch_baseline_hit = 0
    baseline_watch_hit = 0
    price_strict_hit = 0
    push_would_total = len(push_markers)
    signed_error_sum = 0.0
    abs_error_sum = 0.0
    ai_invoked_total = 0
    prediction_total = 0
    prediction_hit = 0
    push_validated_total = 0
    push_validated_hit = 0
    ai_forward_total = 0
    ai_forward_pending = 0
    ai_forward_hit = 0
    ai_forward_direction_total = 0
    ai_forward_direction_hit = 0
    ai_forward_watch_total = 0
    ai_forward_watch_hit = 0
    ai_horizon_minutes_sum = 0
    for item in items:
        try:
            item_time = parse_history_time(str(item.get("time", "")))
            price = float(item.get("price"))
        except Exception:
            continue
        raw_log_total += 1
        path, mature = future_price_path(price_points, item_time, horizon_seconds)
        if not mature or not path or not price:
            pending_total += 1
            rows.append(build_accuracy_pending_row(item, item_time, price, paper_by_time))
            continue
        score = item.get("score") if isinstance(item.get("score"), dict) else {}
        advice = evaluate_realtime_advice(item, price, path, threshold_pct)
        raw_direction = advice["raw_direction"]
        predicted = advice["final_direction"]
        pred_value = direction_value(predicted)
        raw_value = direction_value(raw_direction)
        actual_value = direction_value("做多" if advice["actual_direction"] == "上涨" else ("做空" if advice["actual_direction"] == "下跌" else "观望"))
        price_strict_hit_current = bool(advice["price_strict_hit"])
        price_strict_hit += 1 if price_strict_hit_current else 0
        if raw_value != 0:
            trend_bias_total += 1
            trend_bias_hit += 1 if advice["trend_bias_hit"] else 0
        baseline_watch_current = bool(advice["baseline_watch_hit"])
        baseline_watch_hit += 1 if baseline_watch_current else 0
        if pred_value != 0:
            trade_signal_total += 1
            trade_direction_hit += 1 if advice["trade_direction_hit"] else 0
            entry_touch_total += 1 if advice["entry_touched"] else 0
            no_fill_total += 1 if advice["no_fill"] else 0
            stop_hit_total += 1 if advice["stop_hit"] else 0
            take_profit_total += 1 if advice["take_profit_hit"] else 0
            if advice["entry_touched"]:
                mfe_sum += safe_float(advice["mfe_pct"], 0.0)
                mae_sum += safe_float(advice["mae_pct"], 0.0)
            if advice["trade_resolved"]:
                trade_resolved_total += 1
                trade_win_total += 1 if advice["trade_win"] else 0
            trade_signal_hit += 1 if advice["trade_plan_hit"] else 0
        else:
            watch_total += 1
            watch_hit += 1 if advice["hit"] else 0
            watch_baseline_hit += 1 if baseline_watch_current else 0
        decision_hit_current = bool(advice["hit"])
        decision_total += 1
        decision_hit += 1 if decision_hit_current else 0
        push_marker = push_marker_from_log_item(item)
        prediction_direction, prediction_source = prediction_direction_from_log_item(item)
        prediction_total += 1
        if direction_reasonable_hit(prediction_direction, advice, threshold_pct):
            prediction_hit += 1
        if push_marker.get("would_push"):
            push_validated_total += 1
            if direction_reasonable_hit(str(push_marker.get("push_direction", "观望") or "观望"), advice, threshold_pct):
                push_validated_hit += 1
        signed_error = (pred_value - actual_value)
        signed_error_sum += signed_error
        abs_error_sum += abs(signed_error)
        ai_forward_ctx = ai_forward_from_log_item(item)
        ai_row: Dict[str, Any] = {}
        if ai_forward_ctx:
            ai_invoked_total += 1
            ai_horizon = int(ai_forward_ctx.get("horizon_seconds", 900) or 900)
            ai_path, ai_mature = future_price_path(price_points, item_time, ai_horizon)
            if not ai_mature or not ai_path or not price:
                ai_forward_pending += 1
            else:
                ai_threshold = realtime_accuracy_threshold_pct(ai_horizon)
                ai_advice = evaluate_ai_forward_advice(ai_forward_ctx, price, ai_path, ai_threshold)
                ai_forward_total += 1
                ai_horizon_minutes_sum += int(ai_forward_ctx.get("horizon_minutes", 15) or 15)
                if ai_advice.get("hit"):
                    ai_forward_hit += 1
                if direction_value(ai_forward_ctx.get("direction")) != 0:
                    ai_forward_direction_total += 1
                    if ai_advice.get("trade_direction_hit"):
                        ai_forward_direction_hit += 1
                else:
                    ai_forward_watch_total += 1
                    if ai_advice.get("hit"):
                        ai_forward_watch_hit += 1
                ai_row = {
                    "ai_forward_direction": ai_forward_ctx.get("direction"),
                    "ai_forward_hit": bool(ai_advice.get("hit")),
                    "ai_forward_horizon_minutes": ai_forward_ctx.get("horizon_minutes"),
                    "ai_forward_probability": ai_forward_ctx.get("probability"),
                    "ai_forward_outcome": ai_advice.get("outcome_type"),
                }
        item_time_text = item_time.strftime("%Y-%m-%d %H:%M:%S")
        paper = paper_by_time.get(item_time_text, {})
        rows.append({
            "time": item_time_text,
            "future_time": advice["future_time"],
            "price": price,
            "future_price": advice["future_price"],
            "raw_direction": raw_direction,
            "prediction_direction": prediction_direction,
            "prediction_source": prediction_source,
            "confirm_direction": predicted,
            "final_direction": predicted,
            "actual_direction": advice["actual_direction"],
            "actual_return_pct": advice["actual_return_pct"],
            "verified": True,
            "hit": decision_hit_current,
            "trade_direction_hit": advice["trade_direction_hit"],
            "trade_plan_hit": advice["trade_plan_hit"],
            "price_strict_hit": price_strict_hit_current,
            "baseline_watch_hit": baseline_watch_current,
            "trend_bias_hit": advice["trend_bias_hit"],
            "watch_threshold_pct": advice["watch_threshold_pct"],
            "outcome_type": advice["outcome_type"],
            "entry_touched": advice["entry_touched"],
            "no_fill": advice["no_fill"],
            "stop_hit": advice["stop_hit"],
            "take_profit_hit": advice["take_profit_hit"],
            "mfe_pct": advice["mfe_pct"],
            "mae_pct": advice["mae_pct"],
            "raw_total_score": score.get("raw_total_score", score.get("total_score")),
            "final_trade_score": score.get("final_trade_score"),
            "market_regime": score.get("market_regime"),
            "market_risk_level": score.get("market_risk_level", score.get("risk_level")),
            "paper_equity": paper.get("equity"),
            "paper_pnl_usd": paper.get("pnl_usd"),
            "paper_pnl_pct": paper.get("pnl_pct"),
            "paper_position": paper.get("position_label"),
            "paper_trade_count": paper.get("trade_count"),
            "would_push": push_marker.get("would_push", False),
            "push_kind": push_marker.get("push_kind", ""),
            "push_kinds": push_marker.get("push_kinds", []),
            "push_direction": push_marker.get("push_direction", ""),
            "push_label": push_marker.get("push_label", ""),
            **ai_row,
        })
    rolling = []
    window = 25
    for index, row in enumerate(rows):
        sample = rows[max(0, index - window + 1): index + 1]
        verified_sample = [item for item in sample if item.get("verified")]
        rolling.append({
            "time": row["time"],
            "accuracy_pct": (
                sum(1 for item in verified_sample if item.get("hit")) / len(verified_sample) * 100
                if verified_sample
                else None
            ),
            "price": row["price"],
            "future_price": row.get("future_price"),
            "return_pct": row.get("actual_return_pct"),
            "hit": row.get("hit"),
            "verified": bool(row.get("verified")),
            "direction": row["final_direction"],
            "raw_direction": row.get("raw_direction"),
            "prediction_direction": row.get("prediction_direction"),
            "prediction_source": row.get("prediction_source"),
            "confirm_direction": row.get("confirm_direction", row["final_direction"]),
            "final_direction": row["final_direction"],
            "actual_direction": row["actual_direction"],
            "outcome_type": row.get("outcome_type", ""),
            "paper_equity": row.get("paper_equity"),
            "paper_pnl_usd": row.get("paper_pnl_usd"),
            "paper_pnl_pct": row.get("paper_pnl_pct"),
            "paper_position": row.get("paper_position"),
            "would_push": row.get("would_push", False),
            "push_kind": row.get("push_kind", ""),
            "push_kinds": row.get("push_kinds", []),
            "push_direction": row.get("push_direction", ""),
            "push_label": row.get("push_label", ""),
            "ai_forward_direction": row.get("ai_forward_direction"),
            "ai_forward_hit": row.get("ai_forward_hit"),
            "ai_forward_horizon_minutes": row.get("ai_forward_horizon_minutes"),
            "ai_forward_probability": row.get("ai_forward_probability"),
            "ai_forward_outcome": row.get("ai_forward_outcome"),
        })
    chart_total = len(rows)
    verified_total = decision_total
    ai_forward_horizon_minutes = (
        int(round(ai_horizon_minutes_sum / ai_forward_total))
        if ai_forward_total
        else 15
    )
    mature_rate_pct = pct_rate(verified_total, raw_log_total)
    prediction_accuracy_pct = pct_rate(decision_hit, decision_total)
    decision_accuracy_pct = prediction_accuracy_pct
    baseline_watch_pct = pct_rate(baseline_watch_hit, verified_total)
    watch_reasonable_pct = pct_rate(watch_hit, watch_total)
    watch_baseline_pct = pct_rate(watch_baseline_hit, watch_total)
    watch_edge_pct = watch_reasonable_pct - watch_baseline_pct if watch_total else 0.0
    trade_direction_accuracy_pct = pct_rate(trade_direction_hit, trade_signal_total)
    model_edge_pct = prediction_accuracy_pct - baseline_watch_pct if verified_total else 0.0
    reliability_score, reliability_label = reliability_level(
        verified_total,
        mature_rate_pct,
        watch_reasonable_pct,
        watch_total,
        trade_direction_accuracy_pct,
        trade_signal_total,
    )
    return {
        "ok": True,
        "inst_id": inst_id,
        "horizon_seconds": horizon_seconds,
        "scope": "replay" if replay_scope else ("session" if session_scope else "all"),
        "start_at": REPLAY_LOG_START_AT if replay_scope else (since_time.strftime("%Y-%m-%d %H:%M:%S") if since_time else ""),
        "threshold_pct": threshold_pct,
        "rolling_window": window,
        "summary_scope": "cumulative",
        "summary": {
            "total": verified_total,
            "chart_total": chart_total,
            "raw_log_total": raw_log_total,
            **activity_stats,
            "pending_total": pending_total,
            "next_pending_seconds": next_pending_seconds,
            "mature_rate_pct": mature_rate_pct,
            "reliability_score": reliability_score,
            "reliability_level": reliability_label,
            "horizon_seconds": horizon_seconds,
            "threshold_pct": threshold_pct,
            "prediction_accuracy_pct": prediction_accuracy_pct,
            "prediction_total": prediction_total,
            "prediction_reasonable_pct": pct_rate(prediction_hit, prediction_total),
            "confirm_total": decision_total,
            "confirm_reasonable_pct": pct_rate(decision_hit, decision_total),
            "push_total": push_validated_total,
            "push_reasonable_pct": pct_rate(push_validated_hit, push_validated_total),
            "ai_analysis_total": ai_forward_total,
            "ai_analysis_reasonable_pct": pct_rate(ai_forward_hit, ai_forward_total),
            "decision_total": decision_total,
            "decision_accuracy_pct": decision_accuracy_pct,
            "baseline_watch_pct": baseline_watch_pct,
            "model_edge_pct": model_edge_pct,
            "watch_edge_pct": watch_edge_pct,
            "trade_signal_total": trade_signal_total,
            "trade_direction_accuracy_pct": trade_direction_accuracy_pct,
            "trade_signal_accuracy_pct": pct_rate(trade_signal_hit, trade_signal_total),
            "watch_missed_pct": pct_rate(watch_total - watch_hit, watch_total) if watch_total else 0.0,
            "entry_touch_total": entry_touch_total,
            "entry_touch_pct": pct_rate(entry_touch_total, trade_signal_total),
            "no_fill_total": no_fill_total,
            "no_fill_pct": pct_rate(no_fill_total, trade_signal_total),
            "trade_resolved_total": trade_resolved_total,
            "trade_win_rate_pct": pct_rate(trade_win_total, trade_resolved_total),
            "stop_hit_pct": pct_rate(stop_hit_total, entry_touch_total),
            "take_profit_pct": pct_rate(take_profit_total, entry_touch_total),
            "avg_mfe_pct": mfe_sum / entry_touch_total if entry_touch_total else 0.0,
            "avg_mae_pct": mae_sum / entry_touch_total if entry_touch_total else 0.0,
            "trend_bias_total": trend_bias_total,
            "trend_bias_accuracy_pct": pct_rate(trend_bias_hit, trend_bias_total),
            "watch_total": watch_total,
            "watch_reasonable_pct": pct_rate(watch_hit, watch_total),
            "price_strict_accuracy_pct": pct_rate(price_strict_hit, verified_total),
            "avg_signed_error": signed_error_sum / verified_total if verified_total else 0.0,
            "avg_abs_error": abs_error_sum / verified_total if verified_total else 0.0,
            "paper_initial_capital": paper_final.get("initial_capital", PAPER_INITIAL_CAPITAL),
            "paper_equity": paper_final.get("equity", PAPER_INITIAL_CAPITAL),
            "paper_pnl_usd": paper_final.get("pnl_usd", 0.0),
            "paper_pnl_pct": paper_final.get("pnl_pct", 0.0),
            "paper_position_label": paper_final.get("position_label", "空仓"),
            "paper_trade_count": paper_final.get("trade_count", 0),
            "paper_log_points": len(paper_by_time),
            "push_would_total": push_would_total,
            "ai_invoked_total": ai_invoked_total,
            "ai_forward_total": ai_forward_total,
            "ai_forward_pending": ai_forward_pending,
            "ai_forward_accuracy_pct": pct_rate(ai_forward_hit, ai_forward_total),
            "ai_forward_direction_total": ai_forward_direction_total,
            "ai_forward_direction_accuracy_pct": pct_rate(ai_forward_direction_hit, ai_forward_direction_total),
            "ai_forward_watch_total": ai_forward_watch_total,
            "ai_forward_watch_accuracy_pct": pct_rate(ai_forward_watch_hit, ai_forward_watch_total),
            "ai_forward_horizon_minutes": ai_forward_horizon_minutes,
            "metric_scopes": dict(ACCURACY_METRIC_SCOPES),
            "primary_forward_metric": "ai_forward_direction_accuracy_pct",
            "paper_config_from_log_snapshot": paper_meta.get("from_log_snapshot", 0),
            "paper_config_from_current": paper_meta.get("from_current_config", 0),
        },
        "metric_scopes": dict(ACCURACY_METRIC_SCOPES),
        "primary_forward_metric": "ai_forward_direction_accuracy_pct",
        "paper_simulation": {
            "from_log_snapshot": paper_meta.get("from_log_snapshot", 0),
            "from_current_config": paper_meta.get("from_current_config", 0),
            "note": (
                "模拟 PnL 优先使用每条日志的 config_snapshot；"
                "无快照行按当前 Web 配置重算（与当时 live paper_account.json 可能不一致）。"
            ),
        },
        "points": rolling[-max_points:],
        # 推送事件独立于准确率成熟点；短验证窗缺少未来价时，也不能丢失推送标记。
        "push_markers": push_markers,
        "recent": rows[-20:],
        "time_start": rows[0]["time"] if rows else "",
        "time_end": rows[-1]["time"] if rows else "",
        "chart_points": len(rolling[-max_points:]),
        "retention_hours": retention_hours,
        "interval_seconds": interval_seconds,
        "max_points": max_points,
        "log_path": str(log_file),
    }


def open_log_dir() -> str:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        if hasattr(os, "startfile"):
            explorer = os.environ.get("WINDIR", r"C:\Windows") + r"\explorer.exe"
            subprocess.Popen([explorer, str(LOG_DIR)])
            subprocess.Popen(
                [
                    "powershell",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    "Start-Sleep -Milliseconds 350; $shell = New-Object -ComObject WScript.Shell; $shell.AppActivate('runtime_logs') | Out-Null",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(LOG_DIR)])
        else:
            subprocess.Popen(["xdg-open", str(LOG_DIR)])
        return f"已打开日志目录：{LOG_DIR}"
    except Exception as exc:
        raise RuntimeError(f"打开日志目录失败：{exc}") from exc


def field_html(
    key: str,
    label: str,
    kind: str,
    help_text: str,
    value: Any,
    column: Optional[str] = None,
) -> str:
    if kind == "checkbox":
        checked = "checked" if bool(value) else ""
        control = f'<label class="switch"><input type="checkbox" name="{esc(key)}" value="1" {checked}><span></span></label>'
    elif kind == "choice":
        control = (
            f'<select name="{esc(key)}">'
            f'<option value="0" {"selected" if str(value) == "0" else ""}>0 正式环境</option>'
            f'<option value="1" {"selected" if str(value) == "1" else ""}>1 模拟盘</option>'
            "</select>"
        )
    elif kind == "strategy_choice":
        current = str(value or "swing")
        options = (
            ("scalp", "超短线"),
            ("short", "短线"),
            ("swing", "中线（推荐）"),
            ("long", "长线"),
        )
        control = '<select name="' + esc(key) + '">' + "".join(
            f'<option value="{esc(opt)}" {"selected" if current == opt else ""}>{esc(text)}</option>' for opt, text in options
        ) + "</select>"
    elif kind == "risk_choice":
        current = str(value or "aggressive")
        options = (
            ("conservative", "保守"),
            ("standard", "标准"),
            ("aggressive", "激进（推荐）"),
        )
        control = '<select name="' + esc(key) + '">' + "".join(
            f'<option value="{esc(opt)}" {"selected" if current == opt else ""}>{esc(text)}</option>' for opt, text in options
        ) + "</select>"
    elif kind == "ai_style_choice":
        current = str(value or "steady")
        options = (
            ("steady", "稳健确认"),
            ("momentum", "动量捕捉"),
            ("trend", "趋势跟随"),
        )
        control = '<select name="' + esc(key) + '">' + "".join(
            f'<option value="{esc(opt)}" {"selected" if current == opt else ""}>{esc(text)}</option>' for opt, text in options
        ) + "</select>"
    else:
        step = "any" if isinstance(value, float) else "1"
        control = f'<input type="number" step="{step}" name="{esc(key)}" value="{esc(value)}">'
    col_class = ""
    if column == "left":
        col_class = " field-col-left"
    elif column == "right":
        col_class = " field-col-right"
    return f'<div class="field{col_class}"><label>{esc(label)}</label><div>{control}<p>{esc(help_text)}</p></div></div>'


def masked_input_html(name: str, value: Any, masked: bool = True) -> str:
    safe_name = esc(name)
    safe_value = esc(value)
    if masked:
        return (
            f'<div class="password-wrap">'
            f'<input type="password" name="{safe_name}" value="{safe_value}">'
            f'<button class="eye-btn" type="button" data-toggle-password aria-label="显示或隐藏内容"></button>'
            f"</div>"
        )
    return f'<input type="text" name="{safe_name}" value="{safe_value}">'


def render_login(message: str = "", success: bool = False) -> bytes:
    if message:
        notice_class = "notice notice-success" if success else "notice"
        notice = f'<div class="{notice_class}">{esc(message)}</div>'
    else:
        notice = ""
    body = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>OKX AI Assistant Login</title>
<style>
*{{box-sizing:border-box}} body{{margin:0;min-height:100vh;overflow:hidden;display:grid;place-items:center;font-family:"Segoe UI","Microsoft YaHei",Arial,sans-serif;background:#020617;color:#f8fafc}}
.earth-video{{position:fixed;inset:0;width:100%;height:100%;object-fit:cover;transform:scale(1.08);filter:saturate(1.32) contrast(1.12) brightness(1.08);opacity:.96}}
.shade{{position:fixed;inset:0;background:linear-gradient(90deg,rgba(2,6,23,.62),rgba(15,23,42,.18) 48%,rgba(49,46,129,.38));pointer-events:none}}
#login-bg{{position:fixed;inset:0;width:100%;height:100%;display:block}}
.grid{{position:fixed;inset:0;background:linear-gradient(rgba(148,163,184,.08) 1px,transparent 1px),linear-gradient(90deg,rgba(148,163,184,.08) 1px,transparent 1px);background-size:46px 46px;pointer-events:none}}
.box{{position:relative;z-index:3;width:min(440px,calc(100vw - 34px));background:rgba(15,23,42,.66);border:1px solid rgba(148,163,184,.32);border-radius:24px;padding:34px;box-shadow:0 28px 90px rgba(0,0,0,.42);backdrop-filter:blur(18px)}}
h1{{margin:0 0 8px;font-size:28px}} p{{margin:0 0 24px;color:#cbd5e1}} label{{display:block;margin:16px 0 8px;font-weight:750}}
.password-wrap{{position:relative}} input{{width:100%;padding:13px 14px;border-radius:14px;border:1px solid rgba(148,163,184,.35);background:rgba(15,23,42,.78);color:#fff;font-size:15px;outline:none}} .password-wrap input{{padding-right:50px}} .eye-btn{{position:absolute;right:8px;top:50%;transform:translateY(-50%);width:36px;height:36px;margin:0;border:1px solid rgba(191,219,254,.42);border-radius:12px;background:rgba(30,41,59,.82);cursor:pointer;padding:0;box-shadow:0 4px 14px rgba(0,0,0,.24)}} .eye-btn::before{{content:"";position:absolute;left:8px;top:12px;width:18px;height:10px;border:2px solid #dbeafe;border-radius:18px 18px 12px 12px;transform:rotate(-6deg)}} .eye-btn::after{{content:"";position:absolute;left:15px;top:15px;width:6px;height:6px;border-radius:50%;background:#60a5fa;box-shadow:0 0 8px rgba(96,165,250,.75)}} .eye-btn.is-visible::before{{border-color:#38bdf8}} .eye-btn.is-visible::after{{left:9px;top:17px;width:20px;height:2px;border-radius:2px;background:#f8fafc;transform:rotate(-42deg);box-shadow:0 0 0 1px rgba(15,23,42,.45)}}
button{{margin-top:22px;width:100%;border:0;border-radius:14px;padding:13px;background:linear-gradient(135deg,#60a5fa,#8b5cf6 58%,#14b8a6);color:white;font-weight:850;cursor:pointer}}
.notice{{background:#fee2e2;color:#991b1b;border:1px solid #fecaca;padding:10px 12px;border-radius:12px;margin-bottom:14px}} .notice-success{{background:#ecfdf5;color:#065f46;border-color:#a7f3d0}}
</style></head><body>
<video class="earth-video" autoplay muted loop playsinline preload="auto"><source src="/web-assets/earth_rotation.webm" type="video/webm"></video>
<div class="shade"></div><canvas id="login-bg"></canvas><div class="grid"></div>
<form class="box" method="post" action="/login"><h1>OKX AI Assistant</h1><p>请输入账号密码进入本地控制台。</p>{notice}
<label>用户名</label><input name="username" autocomplete="username" value="admin"><label>密码</label><div class="password-wrap"><input type="password" name="password" autocomplete="current-password"><button class="eye-btn" type="button" data-toggle-password aria-label="显示或隐藏密码"></button></div>
<button type="submit">登录</button></form>
<script>
document.querySelectorAll('[data-toggle-password]').forEach(function(btn){{btn.addEventListener('click',function(){{const input=btn.parentElement.querySelector('input');if(!input)return;input.type=input.type==='password'?'text':'password';btn.classList.toggle('is-visible',input.type==='text');}});}});
const c=document.getElementById('login-bg'),x=c.getContext('2d');let ps=[];
function r(){{const d=window.devicePixelRatio||1;c.width=innerWidth*d;c.height=innerHeight*d;x.setTransform(d,0,0,d,0,0);ps=Array.from({{length:70}},()=>({{x:Math.random()*innerWidth,y:Math.random()*innerHeight,vx:(Math.random()-.5)*.35,vy:(Math.random()-.5)*.35}}));}}
function a(){{x.clearRect(0,0,innerWidth,innerHeight);ps.forEach(p=>{{p.x+=p.vx;p.y+=p.vy;if(p.x<0)p.x=innerWidth;if(p.x>innerWidth)p.x=0;if(p.y<0)p.y=innerHeight;if(p.y>innerHeight)p.y=0;x.fillStyle='rgba(191,219,254,.7)';x.beginPath();x.arc(p.x,p.y,2,0,7);x.fill();}});requestAnimationFrame(a);}}
addEventListener('resize',r);r();a();
</script></body></html>"""
    return body.encode("utf-8")


def design_docs_html() -> str:
    return render_design_docs_html()


def help_manual_html(config: Dict[str, Any]) -> str:
    panel_url = f"http://{HOST}:{PORT}"
    return f"""
	<div class="page-panel" data-page="help"><section class="card toolbar-card"><div><h2>帮助</h2><p class="section-sub">操作手册：如何启动、配置、监控、压测与排查。算法与推送规则详见「设计」页 §0–§K。</p></div><div class="toolbar-right"><a class="button btn-view" href="#design">查看设计文档</a></div></section><div class="help-panel doc-panel">
	<section class="card help-card"><h2>快速开始</h2>
	<ol class="help-list">
	<li>启动 Web 控制台（安装包双击桌面图标，或运行 <code>web_control_panel.py</code>），浏览器打开 {esc(panel_url)}。</li>
	<li>「配置」页勾选合约、填写 AI 密钥与微信 SendKey（可选），保存配置。</li>
	<li>「监控」页点击<strong>开始监控</strong>；需要离线验证时，勾选「录制回放数据集」，停止监控后在「测试」页<strong>开始回放</strong>。</li>
	<li>「测试」页可测 AI / 微信连通性、查看预测压测、<strong>导出诊断包</strong>反馈问题。</li>
	</ol>
	</section>
	<section class="card help-card"><h2>版本与组成</h2>
	<table class="help-table"><thead><tr><th>项</th><th>说明</th></tr></thead><tbody>
	<tr><td>产品</td><td>{esc(APP_NAME)} · {esc(APP_VERSION)}</td></tr>
	<tr><td>Web 控制台</td><td><code>web_control_panel.py</code></td></tr>
	<tr><td>监控内核</td><td><code>okx_signal_monitor.py</code> · 由 Web 启停</td></tr>
	<tr><td>托盘入口</td><td><code>tray_launcher.py</code> · 安装包默认入口</td></tr>
	<tr><td>合约</td><td>OKX USDT 永续；配置页可添加自定义 SWAP</td></tr>
	</tbody></table>
	</section>
	<section class="card help-card"><h2>页面导航</h2>
	<table class="help-table"><thead><tr><th>页面</th><th>用途</th><th>常用操作</th></tr></thead><tbody>
	<tr><td><strong>监控</strong></td><td>实时 K 线、最新分析快照、模拟账户</td><td>开始/停止监控；切换币种与 K 线周期；右上角可关 K 线显示（不影响后台写日志）</td></tr>
	<tr><td><strong>配置</strong></td><td>策略、轮询间隔、AI/推送、密钥</td><td>保存配置；一键填入推送分数建议；导入/导出配置文件</td></tr>
	<tr><td><strong>日志</strong></td><td>查看 JSON 分析日志与控制台摘要</td><td>打开日志目录；另存为；开关仅控制 Web 是否读取展示</td></tr>
	<tr><td><strong>测试</strong></td><td>AI 对话、连通性、压测、回放、诊断导出</td><td>测试推送；刷新压测；开始/停止回放；导出诊断包</td></tr>
	<tr><td><strong>设计</strong></td><td>架构与模块技术文档</td><td>§0 总览 · §A–§K 各阶段五维说明</td></tr>
	<tr><td><strong>设置</strong></td><td>Web 登录账号、恢复出厂</td><td>改密码须输入当前密码；恢复出厂会清空运行日志与密钥</td></tr>
	</tbody></table>
	<p>侧边栏<strong>电源按钮</strong>：重启 Web 或完整退出（托盘模式同步退出托盘）。</p>
	</section>
	<section class="card help-card"><h2>监控页</h2>
	<ul class="help-list">
	<li><strong>K 线开关</strong>：关闭后不再请求 OKX/日志绘图，监控进程与磁盘日志照常运行。</li>
	<li>未启动监控时显示虚拟行情；启动后叠加本次会话写入的分析日志指标。</li>
	<li>快照区：未选中 K 线时显示最新 <code>final_decision</code>、置信度、推送与 AI 摘要；选中 K 线时显示 OHLC 与成交量。</li>
	<li><strong>模拟账户</strong>：按 <code>final_direction</code> 从 $10,000 满仓跟单，方向变才换仓（非真实成交）。</li>
	<li>图表：滚轮缩放时间轴；Shift+滚轮缩放价格；拖动平移；双击重置。</li>
	</ul>
	</section>
	<section class="card help-card"><h2>配置页</h2>
	<ul class="help-list">
	<li><strong>策略周期</strong>：超短线 / 短线 / 中线 / 长线，决定画像参数、主方向周期与压测推荐验证窗口。</li>
	<li><strong>轮询间隔</strong>：监控主循环 sleep 秒数，可独立于策略手动调整（如中线 60→180）；修改后须重启监控。</li>
	<li><strong>确认严格度</strong>：保守 / 标准 / 激进，影响动量阈值与降级门槛；可一键填入建议推送分数。</li>
	<li><strong>AI 与推送</strong>：启用 AI、定时 AI 间隔、静默简报、微信推送；<code>push_score</code> / <code>short_push_score</code> 分别控制做多/做空 trade 门槛。</li>
	<li><strong>录制回放</strong>：勾选后监控会把每轮原始 snapshot 写入 <code>replay_dataset.jsonl</code>。</li>
	<li><strong>日志容量</strong>：默认单文件 500MB、总 8GB；满则轮转删最旧分卷。</li>
	<li><strong>AI 异常告警</strong>：启用 AI 且配置 SendKey 时，AI 连续异常约 5 分钟会发运维微信（与交易推送独立）。</li>
	<li><strong>监控意外退出</strong>：默认自动重启（<code>WEB_MONITOR_*</code>）；有 SendKey 时另发运维告警。</li>
	</ul>
	</section>
	<section class="card help-card"><h2>日志页</h2>
	<ul class="help-list">
	<li>开关默认关闭，仅控制 Web 是否展示；不影响 {esc(MONITOR_JSON_LOG_FILE)} / {esc(MONITOR_PROCESS_LOG_FILE)} 写入。</li>
	<li>页面显示本次启动监控后的尾部；完整分卷请「打开日志目录」查看。</li>
	<li>主要文件：分析 JSONL、控制台 log、回放数据集、校准与模拟账户等均在 <code>build/runtime_logs/</code>。</li>
	</ul>
	</section>
	<section class="card help-card"><h2>测试页</h2>
	<h3>导出诊断包</h3>
	<ul class="help-list">
	<li>打包配置（密钥打码）、监控/回放状态、<code>runtime_logs</code> 下全部日志、回放数据集、各范围压测 JSON 与 SVG/PNG 走势图。</li>
	<li>超大单文件仅含尾部，详见包内 <code>manifest.json</code> 与 <code>README.txt</code>。</li>
	</ul>
	<h3>AI 与微信测试</h3>
	<ul class="help-list">
	<li><strong>AI 对话</strong>：发送前自动保存配置；历史仅保留在本页浏览器内存。</li>
	<li><strong>连通性测试</strong> / <strong>获取简报</strong>：验证 API 与监控同款简报逻辑（不推微信）。</li>
	<li><strong>测试微信推送</strong>：发送格式预览；异常告警样式为 <code>[AI异常] …</code>，正文含失败原因。</li>
	</ul>
	<h3>实时预测压测</h3>
	<ul class="help-list">
	<li>右上角<strong>开关</strong>默认开启；打开后读取分析日志并自动刷新（关闭后不再请求，可手动点「刷新压测」）。</li>
	<li>选择合约、验证窗口（随策略推荐，可手动改）、范围（本次启动后 / 全部历史 / 回放会话）。</li>
	<li><strong>蓝线</strong>为价格，<strong>绿线</strong>为 $10k 模拟账户权益；绿/红/灰点表示该点方向判定（已验证合理 / 不合理 / 待验证）。</li>
	<li>价格上方标记表示该帧<strong>应推送</strong>（◆急变 ■观察 △演变 ★结构单等），与是否实际发微信无关。</li>
	<li>顶栏摘要含分析次数、AI 调用与 Token；可导出/导入图表 JSON 快照。</li>
	</ul>
	<h3>离线回放</h3>
	<ul class="help-list">
	<li>数据集 {esc(REPLAY_DATASET_FILE)} · 结果 {esc(REPLAY_ANALYSIS_LOG_FILE)}。</li>
	<li>须先停止监控，再在测试页点「开始回放」；回放与监控不能并行。</li>
	<li>每行含 <code>analysis</code> 与 <code>push_analysis</code>；微信是否发送取决于配置页「启用微信推送」。</li>
	<li>回放完成后，压测范围选「回放会话」查看曲线与推送标记。</li>
	</ul>
	</section>
	<section class="card help-card"><h2>启动、停止与退出</h2>
	<ul class="help-list">
	<li><strong>监控</strong>：配置页保存后，监控页「开始监控 / 停止监控」。改 interval、推送分数、日志容量等须停止后重启。</li>
	<li><strong>回放</strong>：停止监控 → 测试页「开始回放 / 停止回放」。</li>
	<li><strong>托盘退出</strong>：与 Web 电源「关机」等效，停止子进程并退出托盘。</li>
	<li><strong>仅关浏览器</strong>不会停止后台；须显式停止监控或使用关机/托盘退出。</li>
	<li>Web 重启后若页面空白，按 <strong>F5</strong> 刷新。</li>
	</ul>
	</section>
	<section class="card help-card"><h2>本地文件</h2>
	<ul class="help-list">
	<li>运行日志：<code>build/runtime_logs/</code></li>
	<li>出厂默认模板：<code>config/</code></li>
	<li>本机配置与密钥：<code>local_state/</code>（勿提交版本库）</li>
	<li>当前生效配置：{esc(active_config_file())}</li>
	</ul>
	</section>
	<section class="card help-card"><h2>常见问题</h2>
	<ul class="help-list">
	<li><strong>监控已开但 K 线无指标</strong>：等待几轮轮询；确认日志目录有写入且未被总容量回收。</li>
	<li><strong>压测一直待验证</strong>：验证窗口未到期；到期后点「刷新压测」。</li>
	<li><strong>有方向但没推 trade</strong>：微信看 confidence 是否达 <code>push_score</code> / <code>short_push_score</code>，以及冷却、AI 复核与 <code>push_analysis</code> 阻断原因。</li>
	<li><strong>同向推送太密</strong>：同趋势 leg 有加长冷却；重复推送需更高分数或更长间隔。</li>
	<li><strong>修改配置不生效</strong>：须停止并重启监控；密钥在测试页也会先自动保存。</li>
	<li><strong>回放无压测点</strong>：须先录制数据集并点击「开始回放」，再选「回放会话」。</li>
	<li><strong>收到 [AI异常] 微信</strong>：查看 JSON / 控制台 <code>analysis.error</code>；修复密钥或网络后探活恢复，改密钥建议重启监控。</li>
	</ul>
	<div class="help-note">信号条件、八层评分、AI merge、post-audit 与推送推导见「设计」页 §B–§J。</div>
	</section></div></div>
"""


def render_page(message: str = "") -> bytes:
    config = load_config()
    env = load_env()
    auth = load_auth()
    selected_instruments = order_configured_inst_ids(config.get("inst_ids", []))
    selected = set(selected_instruments)
    inst_pool = visible_inst_pool(config)
    removed_inst_ids = [inst for inst in order_configured_inst_ids(config.get("removed_inst_ids", [])) if inst in PRESET_INSTRUMENTS]
    monitor_initial = selected_instruments[0] if selected_instruments else ""
    accuracy_inst_options = "".join(
        f'<option value="{esc(inst)}">{esc(inst)}</option>' for inst in selected_instruments
    ) or '<option value="">未配置</option>'
    if selected_instruments:
        monitor_tabs = "".join(
            f'<button class="button coin-tab {"active" if index == 0 else ""}" type="button" data-monitor-inst="{esc(inst)}">{esc(inst)}</button>'
            for index, inst in enumerate(selected_instruments)
        )
    else:
        monitor_tabs = '<span class="empty-coin">请先在配置页选择监控币种</span>'
    monitor_bar_tabs = "".join(
        f'<button type="button" class="button bar-tab {"active" if bar == "1m" else ""}" data-monitor-bar="{bar}">{bar}</button>'
        for bar in MONITOR_BAR_CHANNELS
    )
    accuracy_horizon_select_html = build_accuracy_horizon_select_html(config.get("strategy_mode", "swing"))
    rows = []
    rows.append('<section class="card page-section" data-page="config"><h2>配置币种</h2><p class="section-sub">勾选需要监控的 OKX USDT 永续；可通过「增加新币种」添加更多合约。每个币种右侧可点<strong>删除</strong>从列表移除。</p><div class="checks" id="instChecks">')
    for inst in inst_pool:
        rows.append(inst_tile_html(inst, checked=inst in selected))
    for inst in removed_inst_ids:
        rows.append(f'<input type="hidden" name="removed_inst_ids" value="{esc(inst)}">')
    rows.append(
        '</div><div class="add-inst-field"><label for="newInstId">增加新币种</label><div>'
        '<div class="add-inst-row"><input type="text" id="newInstId" autocomplete="off" placeholder="例如 SOL-USDT-SWAP">'
        '<button class="button btn-save action-control" type="button" id="addInstBtn">添加</button></div>'
        '<p id="addInstFeedback" class="inst-feedback">输入 OKX 永续合约 ID 并点击添加；校验通过后会出现在上方，右侧「删除」可移除。</p>'
        '</div></div></section>'
    )
    current = ""
    for item in CONFIG_FIELDS:
        section, key, kind, label, help_text = item[:5]
        column = item[5] if len(item) > 5 else None
        if section != current:
            if current:
                rows.append("</section>")
            current = section
            rows.append(f'<section class="card page-section" data-page="config"><h2>{esc(section)}</h2>')
            if section == "AI与推送":
                rows.append(
                    '<div class="field-group-head field-col-left">AI 分析</div>'
                    '<div class="field-group-head field-col-right">微信推送</div>'
                )
        rows.append(field_html(key, label, kind, help_text, config.get(key), column))
        if key == "interval":
            lo, hi = STRATEGY_INTERVAL_BOUNDS.get(
                str(config.get("strategy_mode", "swing") or "swing").strip().lower(),
                (1, 3600),
            )
            rows.append(
                f'<p id="strategyIntervalHint" class="section-sub" '
                f'style="grid-column:2;margin:-4px 0 0 174px;font-size:12px;color:var(--muted);">'
                f'当前策略推荐 {recommended_interval_for_strategy(config.get("strategy_mode"))} 秒；'
                f'可配置范围 {lo}–{hi} 秒</p>'
            )
    rows.append("</section>")
    risk = str(config.get("risk_preference", "standard"))
    suggested_rows = "".join(
        f"<tr><td>{esc(RISK_PREFERENCE_LABELS.get(risk_key, risk_key))}</td>"
        f"<td>{values['push_score']}</td>"
        f"<td>{values['short_push_score']}</td></tr>"
        for risk_key, values in SUGGESTED_PUSH_SCORES.items()
    )
    rows.append(
        f'<section class="card page-section" data-page="config" id="pushScoreGuideCard" style="display:block;">'
        f'<h2>推送分数建议</h2>'
        f'<p class="section-sub" id="pushScoreGuideText">{esc(push_score_guide_text(risk, bool(config.get("ai_enabled"))))}</p>'
        f'<table class="help-table" style="grid-column:1/-1;"><thead><tr>'
        f'<th>确认严格度</th><th>做多 trade</th><th>做空 trade</th>'
        f'</tr></thead><tbody>{suggested_rows}</tbody></table>'
        f'<div class="toolbar-right" style="grid-column:1/-1;justify-content:flex-start;">'
        f'<button class="btn-save action-control" type="button" id="applySuggestedPushScores">填入当前严格度建议值</button>'
        f'</div></section>'
    )
    rows.append('<section class="card page-section" data-page="config"><h2>AI密钥与微信推送</h2>')
    for key, label, help_text in ENV_FIELDS:
        value = env.get(key, ENV_DEFAULTS.get(key, ""))
        input_html = masked_input_html(f"env_{key}", value, masked="KEY" in key)
        rows.append(f'<div class="field"><label>{esc(label)}</label><div>{input_html}<p>{esc(help_text)}</p></div></div>')
    rows.append("</section>")

    notice_html = ""
    if message:
        is_error = ("失败" in message) or ("错误" in message)
        notice_class = "notice notice-error" if is_error else "notice"
        notice_html = f'<div class="{notice_class}">{esc(message)}</div>'

    tray_mode = launched_by_tray()
    if tray_mode:
        power_mode_hint = "托盘模式：关机同步退出托盘；重启由托盘重拉 Web"
        power_menu_restart_hint = "托盘保持运行，恢复后请 F5 刷新"
        power_menu_shutdown_hint = "停止服务并退出托盘"
        power_confirm_restart = "将停止监控/回放并由托盘重启 Web 控制台，是否继续？"
        power_confirm_shutdown = "将停止所有服务并退出托盘，是否继续？"
        power_shutdown_done = "服务已关闭，托盘已退出。可关闭此浏览器标签页。"
    else:
        power_mode_hint = "终端模式：关机将关闭 cmd 窗口"
        power_menu_restart_hint = "延迟后新开 cmd 窗口"
        power_menu_shutdown_hint = "停止服务并关闭 Web"
        power_confirm_restart = "将停止监控/回放并重启 Web 控制台，是否继续？"
        power_confirm_shutdown = "将停止所有服务并关闭 Web 控制台，是否继续？"
        power_shutdown_done = "Web 控制台已关闭。可关闭此浏览器标签页。"

    body = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>OKX AI Assistant</title>
<style>
:root{{--bg:#f5f5f6;--panel:#fff;--text:#172033;--muted:#6b7a90;--line:#e1e8f0;--primary:#8b6cf6;--shadow:0 8px 26px rgba(15,23,42,.08)}}
*{{box-sizing:border-box}} body{{margin:0;font-family:"Segoe UI","Microsoft YaHei",Arial,sans-serif;background:var(--bg);color:var(--text)}} .app{{min-height:100vh;display:grid;grid-template-columns:258px 1fr}}
.sidebar{{position:sticky;top:0;height:100vh;display:flex;flex-direction:column;background:#fff;border-right:1px solid #eceef3;padding:26px 16px 18px;box-shadow:4px 0 24px rgba(15,23,42,.04)}} .sidebar-main{{flex:1;min-height:0}} .sidebar-footer{{position:relative;margin-top:auto;padding-top:12px}} .sidebar-footer-panel{{border:1px solid #eceef3;border-radius:16px;background:linear-gradient(180deg,#fafbfc 0%,#f4f6f8 100%);box-shadow:inset 0 1px 0 rgba(255,255,255,.9),0 4px 14px rgba(15,23,42,.04);overflow:visible}} .sidebar-footer-power{{position:relative;display:flex;align-items:center;gap:10px;padding:10px 12px;cursor:pointer;transition:background .18s ease;border-radius:16px 16px 0 0;overflow:visible}} .sidebar-footer-power:hover{{background:rgba(255,255,255,.55)}} .power-btn-label{{flex:1;font-size:13px;font-weight:650;color:#475569;line-height:1.2}} .power-btn-chevron{{color:#94a3b8;font-size:16px;line-height:1;user-select:none}} .power-mode-hint{{padding:0 12px 10px;font-size:11px;line-height:1.45;color:#64748b;border-bottom:1px solid #e8ecf1}} .sidebar-version{{display:flex;align-items:center;justify-content:space-between;gap:10px;border-top:0;padding:9px 12px;background:rgba(255,255,255,.72)}} .sidebar-version-name{{font-size:11px;font-weight:650;color:#64748b;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}} .sidebar-version-tag{{flex-shrink:0;padding:3px 8px;border-radius:999px;background:#ede9fe;color:#6d28d9;font-size:11px;font-weight:750;letter-spacing:.03em;line-height:1.2}} .power-btn{{width:40px;height:40px;border-radius:12px;border:1px solid #fca5a5;background:linear-gradient(180deg,#fff7f7 0%,#ffe4e6 100%);cursor:pointer;display:grid;place-items:center;color:#dc2626;padding:0;min-width:40px;box-shadow:0 4px 12px rgba(220,38,38,.12),inset 0 1px 0 rgba(255,255,255,.85);transition:background .18s ease,color .18s ease,border-color .18s ease,box-shadow .18s ease,transform .18s ease}} .power-btn:hover{{background:linear-gradient(180deg,#ffe4e6 0%,#fecaca 100%);color:#b91c1c;border-color:#f87171;box-shadow:0 6px 16px rgba(220,38,38,.18);transform:translateY(-1px)}} .power-btn:focus-visible{{outline:2px solid #fca5a5;outline-offset:2px}} .power-btn svg{{display:block;width:20px;height:20px;filter:drop-shadow(0 1px 0 rgba(255,255,255,.75))}} .power-menu{{position:absolute;left:8px;right:8px;bottom:calc(100% + 8px);background:#fff;border:1px solid #e5e7eb;border-radius:14px;padding:6px;box-shadow:0 16px 34px rgba(15,23,42,.14);z-index:40}} .power-menu[hidden]{{display:none}} .power-menu button{{display:flex;flex-direction:column;align-items:flex-start;gap:2px;width:100%;text-align:left;padding:10px 12px;border:0;background:transparent;border-radius:10px;cursor:pointer;font-size:13px;font-weight:650;color:#334155;min-width:0}} .power-menu button:hover{{background:#f1f5f9}} .power-menu-desc{{font-size:11px;font-weight:500;color:#64748b;line-height:1.35}} .power-menu button.danger{{color:#dc2626}} .power-menu button.danger:hover{{background:#fef2f2}} .power-menu button.danger .power-menu-desc{{color:#b91c1c;opacity:.85}} .power-overlay{{position:fixed;inset:0;display:grid;place-items:center;background:rgba(15,23,42,.42);z-index:9999;color:#fff;font-size:16px;font-weight:650;padding:24px;text-align:center}} .brand{{display:flex;align-items:center;gap:10px;margin:0 0 24px;padding:0 8px;font-size:22px;font-weight:800;color:#201a38}} .logo{{width:28px;height:28px;border-radius:8px;display:grid;place-items:center;color:#fff;background:linear-gradient(135deg,#ec4899,#8b5cf6 58%,#38bdf8)}}
.nav-item{{display:flex;align-items:center;min-height:44px;padding:0 14px;border-radius:12px;color:#3c4050;text-decoration:none;font-weight:650;margin-bottom:6px}} .nav-item:hover{{background:#f1edff}} .nav-item.active{{background:#ddd5ff;color:#201a38;box-shadow:inset 4px 0 0 #8b6cf6}}
.content{{min-width:0;min-height:100vh;padding:18px 22px 22px}} .page-panel,.page-section{{display:none}} .page-panel.active{{display:block}} .page-panel[data-page="monitor"].active{{height:100%}} .page-section.active{{display:grid}} .page-section.hero-card.active{{display:flex}}
.card{{position:relative;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px 22px;background:#fff;border:1px solid #edf0f5;border-radius:18px;padding:22px;margin-bottom:18px;box-shadow:var(--shadow)}} .card::before{{content:"";position:absolute;inset:0 0 auto;height:3px;background:linear-gradient(90deg,#8b6cf6,#60a5fa,transparent);opacity:.55}} .hero-card{{justify-content:space-between;align-items:center}} h2{{grid-column:1/-1;margin:0 0 4px;font-size:18px}} .section-sub{{margin:0;color:var(--muted);font-size:13px}}
.field{{display:grid;grid-template-columns:160px minmax(160px,1fr);gap:6px 14px;align-items:center;min-height:74px;padding:12px 14px;border:1px solid var(--line);border-radius:14px;background:#fff}} .field-col-left{{grid-column:1}} .field-col-right{{grid-column:2}} .field-group-head{{align-self:end;margin:0;padding:0 4px 2px;font-size:12px;font-weight:750;color:#64748b;letter-spacing:.04em;text-transform:none}} .field p{{margin:0;color:var(--muted);font-size:12px}} .autofill-trap{{position:absolute;left:-9999px;width:1px;height:1px;opacity:0;pointer-events:none}} label{{font-weight:650}} input[type=text],input[type=password],input[type=number],select{{width:100%;padding:11px 12px;border:1px solid #cbd7e5;border-radius:12px;outline:none;background:#fff;color:var(--text);font-size:14px}} input[type=checkbox]{{width:16px;height:16px;accent-color:var(--primary)}} .password-wrap{{position:relative}} .password-wrap input{{padding-right:50px}} .eye-btn{{position:absolute;right:7px;top:50%;transform:translateY(-50%);width:34px;min-width:34px;height:34px;padding:0;border-radius:11px;background:#e2e8f0;border:1px solid #94a3b8;color:#334155;box-shadow:0 3px 10px rgba(15,23,42,.12)}} .eye-btn::before{{content:"";position:absolute;left:8px;top:12px;width:16px;height:9px;border:2px solid #334155;border-radius:18px 18px 12px 12px;transform:rotate(-6deg)}} .eye-btn::after{{content:"";position:absolute;left:14px;top:15px;width:6px;height:6px;border-radius:50%;background:#2563eb}} .eye-btn.is-visible{{background:#dbeafe;border-color:#60a5fa}} .eye-btn.is-visible::before{{border-color:#1d4ed8}} .eye-btn.is-visible::after{{left:8px;top:16px;width:19px;height:2px;border-radius:2px;background:#1d4ed8;transform:rotate(-42deg)}}
.switch{{display:inline-flex;width:46px;height:26px;border-radius:999px;background:#cbd5e1;position:relative}} .switch input{{opacity:0}} .switch span{{position:absolute;width:20px;height:20px;left:3px;top:3px;border-radius:50%;background:white;box-shadow:0 2px 8px rgba(15,23,42,.22)}} .switch:has(input:checked){{background:linear-gradient(135deg,#8b6cf6,#5b8cff)}} .switch:has(input:checked) span{{transform:translateX(20px)}}
.checks{{display:flex;gap:14px;flex-wrap:wrap;align-items:stretch;width:100%;grid-column:1/-1}} .check-tile{{display:inline-flex;align-items:center;gap:10px;padding:10px 12px 10px 14px;border-radius:14px;background:#f8fafc;border:1px solid var(--line)}} .check-tile label{{display:inline-flex;align-items:center;gap:9px;cursor:pointer;margin:0;font-weight:650}} .check-tile:has(input:checked){{background:#efeaff;border-color:#a78bfa;color:#4c1d95}} .check-tile-custom{{padding-right:8px}} .inst-remove-btn{{min-width:auto!important;width:auto!important;height:30px!important;padding:0 10px!important;border-radius:10px!important;border:1px solid #fecaca!important;background:#fff5f5!important;color:#b91c1c!important;font-size:12px!important;font-weight:650!important;line-height:1!important;box-shadow:none!important}} .inst-remove-btn:hover{{background:#fee2e2!important;border-color:#f87171!important;color:#991b1b!important}} .add-inst-field{{width:100%;grid-column:1/-1;display:grid;grid-template-columns:160px minmax(160px,1fr);gap:6px 14px;align-items:start;padding:12px 14px;border:1px solid var(--line);border-radius:14px;background:#fff;margin-top:4px}} .add-inst-field label{{font-weight:650;padding-top:11px}} .add-inst-row{{display:flex;gap:10px;align-items:center}} .add-inst-row input{{flex:1}} .add-inst-row button{{min-width:88px}} .inst-feedback{{margin:0;color:var(--muted);font-size:12px;white-space:pre-wrap}} .inst-feedback.is-error{{color:#b91c1c}} .inst-feedback.is-ok{{color:#047857}}
.actions{{display:flex;justify-content:flex-end;gap:10px;flex-wrap:wrap;position:sticky;bottom:0;margin-top:20px;padding:14px;border-radius:18px;background:rgba(255,255,255,.9);border:1px solid rgba(255,255,255,.9);box-shadow:var(--shadow);backdrop-filter:blur(12px)}} .action-group,.toolbar-right,.toolbar-left{{display:flex;align-items:center;gap:10px;flex-wrap:wrap}}
button,.button{{border:0;border-radius:12px;padding:11px 16px;background:#f1f3f8;color:#263449;min-width:94px;justify-content:center;white-space:nowrap;cursor:pointer;text-decoration:none;display:inline-flex;align-items:center;font-size:14px;font-weight:650;transition:background .18s ease,color .18s ease,box-shadow .18s ease,transform .18s ease,opacity .18s ease}} button:disabled,.button.disabled{{cursor:not-allowed;opacity:.72}} .btn-save,.btn-log{{background:#ede9fe;color:#5b21b6}} .btn-run{{background:#dcfce7;color:#047857}} .btn-run.is-running{{background:linear-gradient(135deg,#ef4444,#f97316);color:#fff;box-shadow:0 10px 26px rgba(239,68,68,.24)}} .btn-run.is-starting{{background:linear-gradient(135deg,#60a5fa,#8b5cf6);color:#fff;box-shadow:0 10px 26px rgba(99,102,241,.25)}} .btn-danger{{background:#fee2e2;color:#b91c1c}} .btn-danger.is-ready{{background:#ef4444;color:#fff;box-shadow:0 10px 26px rgba(239,68,68,.22)}} .btn-test{{background:#e0f2fe;color:#0369a1}} .btn-view{{background:#f1f5f9;color:#334155}}
.toolbar-card{{display:flex;align-items:center;justify-content:space-between;gap:14px;margin-bottom:16px}} .toolbar-card::before{{display:none}} .logs-toolbar-card{{align-items:flex-start}} .logs-toolbar-card .toolbar-right{{align-items:center;justify-content:flex-end;flex-wrap:wrap;row-gap:8px}} .coin-tabs{{display:inline-flex;gap:8px;padding:4px;border-radius:14px;background:#f1f5f9;border:1px solid #e2e8f0}} .coin-tab.active{{background:#8b6cf6;color:#fff}} .empty-coin{{display:inline-flex;align-items:center;padding:0 12px;color:#64748b;font-size:13px;font-weight:650}} .bar-tabs{{display:inline-flex;gap:6px;flex-wrap:wrap;padding:4px;border-radius:12px;background:#2a2a2a;border:1px solid #404040;margin-top:8px}} .bar-tab{{min-width:42px;padding:7px 10px;font-size:12px;background:transparent;color:#cbd5e1;border:0;box-shadow:none}} .bar-tab.active{{background:#8b6cf6;color:#fff}}
	.market-card{{background:#242424;border:1px solid #3a3a3a;border-radius:18px;margin:0;padding:18px 20px 16px;color:#f8fafc;box-shadow:0 12px 34px rgba(15,23,42,.16);overflow:hidden;height:calc(100vh - 40px);display:flex;flex-direction:column}} .monitor-card{{height:calc(100vh - 188px);min-height:520px}} .monitor-toolbar-card{{align-items:flex-start}} .monitor-toolbar-card .toolbar-right{{align-items:center;justify-content:flex-end;flex-wrap:wrap;row-gap:8px;gap:10px}} .monitor-chart-off .snapshot-panel{{display:none}} .monitor-chart-off .bar-tabs,.monitor-chart-off .market-price{{opacity:.45;pointer-events:none}} .monitor-chart-off-hint{{margin:0 0 14px}} .market-head{{display:flex;justify-content:space-between;align-items:flex-start;gap:14px;margin-bottom:12px}} .market-title{{font-size:18px;font-weight:800;margin-bottom:4px}} .market-sub{{color:#a3a3a3;font-size:12px}} .market-price{{text-align:right}} .market-price strong{{display:block;font-size:24px;line-height:1.1}} .market-price span{{font-size:13px;color:#94a3b8}} .market-price.up strong,.market-price.up span{{color:#22c55e}} .market-price.down strong,.market-price.down span{{color:#ef4444}} .market-canvas-wrap{{position:relative;flex:1;min-height:0;border-top:1px solid #393939;background:linear-gradient(rgba(255,255,255,.055) 1px,transparent 1px),linear-gradient(90deg,rgba(255,255,255,.035) 1px,transparent 1px);background-size:100% 72px,72px 100%}} canvas{{width:100%;height:100%;display:block;cursor:grab}} canvas.dragging{{cursor:grabbing}} .market-loading{{position:absolute;inset:0;display:grid;place-items:center;color:#a3a3a3;pointer-events:none}} .snapshot-panel{{position:absolute;left:16px;top:16px;z-index:2;min-width:260px;max-width:min(500px,calc(100% - 32px));max-height:calc(100% - 32px);overflow:hidden;padding:0;border-radius:12px;background:rgba(15,23,42,.62);border:1px solid rgba(148,163,184,.20);box-shadow:0 12px 28px rgba(0,0,0,.18);backdrop-filter:blur(8px);font-size:12px;line-height:1.35;color:#dbeafe;pointer-events:none;display:flex;flex-direction:column}} .snapshot-panel.is-collapsed{{max-height:none}} .snapshot-head{{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:8px 10px;border-bottom:1px solid rgba(148,163,184,.14);pointer-events:auto}} .snapshot-panel.is-collapsed .snapshot-head{{border-bottom:0}} .snapshot-panel strong{{display:block;color:#fff;font-size:13px;margin:0;flex:1;min-width:0}} .snapshot-toggle{{flex-shrink:0;padding:2px 8px;border-radius:999px;border:1px solid rgba(148,163,184,.35);background:rgba(30,41,59,.75);color:#cbd5e1;font-size:11px;font-weight:650;line-height:1.4;cursor:pointer}} .snapshot-toggle:hover{{background:rgba(51,65,85,.85);color:#fff}} .snapshot-body{{overflow:auto;padding:8px 10px 10px;max-height:min(420px,calc(100vh - 320px));pointer-events:auto}} .snapshot-panel.is-collapsed .snapshot-body{{display:none}} .snapshot-grid{{display:grid;grid-template-columns:68px minmax(0,1fr);gap:3px 8px;align-items:start}} .snapshot-grid span{{color:#9ca3af}} .snapshot-grid b{{font-weight:700;color:#e5e7eb;word-break:break-word}} .snapshot-note{{margin-top:8px;padding:8px 10px;border-radius:10px;background:rgba(255,255,255,.06);color:#cbd5e1;font-size:12px;line-height:1.45;word-break:break-word}} .market-time-range{{margin-top:10px;padding-top:10px;border-top:1px solid rgba(255,255,255,.08);color:#a3a3a3;font-size:12px;display:flex;justify-content:flex-end;gap:10px;flex-wrap:wrap}} #monitorPaperAccount.paper-up{{color:#4ade80}} #monitorPaperAccount.paper-down{{color:#fb7185}}
	.log-window{{width:100%;min-height:340px;height:100%;resize:vertical;border:1px solid #dbe4ef;border-radius:16px;padding:16px;background:#0f172a;color:#d1fae5;font-family:Consolas,"Courier New",monospace;font-size:13px;line-height:1.55;white-space:pre;box-sizing:border-box}} .log-window-console{{background:#111827;color:#e5e7eb}} .logs-card{{display:block;padding:18px 20px 20px}} .logs-card::before{{opacity:.35}} .logs-grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px 18px;align-items:stretch}} .log-panel{{display:flex;flex-direction:column;min-height:560px;margin:0}} .log-panel h3{{margin:0 0 8px;font-size:16px;color:#111827}} .log-panel-desc{{min-height:4.6em;margin:0 0 12px;color:#64748b;font-size:12px;line-height:1.5}} .log-panel-body{{flex:1 1 auto;display:flex;flex-direction:column;min-height:360px}} .log-panel-footer{{flex:0 0 auto;margin-top:12px}} .log-panel-footer .toolbar-card{{display:flex;align-items:center;justify-content:space-between;gap:10px;margin:0;padding:0;border:0;box-shadow:none;background:transparent}} .log-panel-footer .toolbar-card::before{{display:none}} .log-panel-footer .section-sub{{margin:0}} .accuracy-off-hint{{margin:0 0 14px}} .notice{{background:#ecfdf5;color:#065f46;border:1px solid #a7f3d0;padding:13px 15px;border-radius:14px;margin-bottom:16px}} .notice-error{{background:#fee2e2;color:#991b1b;border-color:#fecaca}}
	.help-panel{{display:grid;grid-template-columns:1fr;gap:16px}} .help-card{{display:block;line-height:1.68}} .help-card::before{{display:none}} .help-card h3{{margin:18px 0 8px;font-size:16px;color:#111827}} .help-card h3:first-child{{margin-top:0}} .help-card p{{margin:6px 0;color:#475569;font-size:13px}} .help-grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}} .help-item{{border:1px solid #e2e8f0;border-radius:12px;padding:12px;background:#f8fafc}} .help-item strong{{display:block;margin-bottom:4px;color:#1f2937}} .help-list{{margin:8px 0 0;padding-left:18px;color:#475569;font-size:13px}} .help-list li{{margin:4px 0}} .help-table{{width:100%;border-collapse:collapse;margin-top:10px;font-size:13px}} .help-table th,.help-table td{{border:1px solid #e2e8f0;padding:9px 10px;text-align:left;vertical-align:top}} .help-table th{{background:#f1f5f9;color:#334155}} .help-note{{border-left:4px solid #8b6cf6;background:#f5f3ff;padding:10px 12px;border-radius:10px;color:#4c1d95;font-size:13px;margin-top:10px}} .flow-chain{{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:12px 0 4px}} .flow-chain span{{display:inline-flex;align-items:center;padding:8px 12px;border-radius:999px;background:#eef2ff;border:1px solid #c7d2fe;color:#3730a3;font-size:12px;font-weight:650}} .flow-chain span:not(:last-child)::after{{content:"→";margin-left:8px;color:#94a3b8;font-weight:400}} code{{background:#eef2ff;color:#4338ca;border-radius:6px;padding:1px 5px}}
	.accuracy-card{{display:block}} .accuracy-controls{{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:10px 0 12px}} .accuracy-controls select{{width:auto;min-width:150px}} .accuracy-controls .btn-save{{min-width:88px}} .accuracy-summary{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:12px}} .accuracy-summary div{{border:1px solid #e2e8f0;border-radius:12px;padding:10px;background:#f8fafc}} .accuracy-summary div.accuracy-primary{{border-color:#93c5fd;background:#eff6ff}} .accuracy-summary div.accuracy-paper-primary{{border-color:#86efac;background:#ecfdf5}} .accuracy-summary div.accuracy-rate{{border-color:#c4b5fd;background:#f5f3ff}} .accuracy-summary span{{display:block;color:#64748b;font-size:12px}} .accuracy-summary b{{display:block;margin-top:3px;font-size:18px;color:#111827}} .accuracy-canvas-wrap{{position:relative;height:320px;border:1px solid #e2e8f0;border-radius:14px;background:#0f172a;overflow:hidden}} .accuracy-canvas-wrap canvas{{width:100%;height:100%;cursor:grab}} .accuracy-canvas-wrap canvas.dragging{{cursor:grabbing}} .accuracy-point-panel{{position:absolute;left:12px;top:12px;z-index:2;min-width:240px;max-width:min(420px,calc(100% - 24px));max-height:calc(100% - 24px);overflow:auto;padding:10px 12px;border-radius:12px;background:rgba(15,23,42,.78);border:1px solid rgba(148,163,184,.28);box-shadow:0 12px 28px rgba(0,0,0,.22);backdrop-filter:blur(8px);font-size:12px;line-height:1.35;color:#dbeafe;pointer-events:none}} .accuracy-point-panel strong{{display:block;color:#fff;font-size:13px;margin-bottom:5px}} .accuracy-point-panel .snapshot-grid{{display:grid;grid-template-columns:72px minmax(0,1fr);gap:3px 8px;align-items:start}} .accuracy-point-panel .snapshot-grid span{{color:#9ca3af}} .accuracy-point-panel .snapshot-grid b{{font-weight:700;color:#e5e7eb;word-break:break-word}} .accuracy-note{{margin:10px 0 0;color:#64748b;font-size:12px}} .ai-chat-card{{display:block;padding:18px 20px 20px}} .ai-chat-card::before{{opacity:.35}} .ai-chat-head{{display:flex;align-items:flex-start;justify-content:space-between;gap:14px;margin-bottom:12px}} .ai-chat-head .toolbar-right{{align-items:center;gap:8px;flex-wrap:wrap}} .ai-chat-window{{min-height:320px;max-height:480px;overflow-y:auto;border:1px solid #e2e8f0;border-radius:14px;background:#f8fafc;padding:14px;display:flex;flex-direction:column;gap:10px;margin-bottom:12px}} .ai-chat-empty{{color:#64748b;font-size:13px;text-align:center;padding:40px 12px;line-height:1.6}} .ai-chat-msg{{max-width:88%;padding:10px 12px;border-radius:14px;font-size:14px;line-height:1.55;white-space:pre-wrap;word-break:break-word}} .ai-chat-msg.user{{align-self:flex-end;background:#dbeafe;color:#1e3a8a;border-bottom-right-radius:4px}} .ai-chat-msg.assistant{{align-self:flex-start;background:#fff;color:#1f2937;border:1px solid #e2e8f0;border-bottom-left-radius:4px}} .ai-chat-msg.error{{align-self:stretch;background:#fee2e2;color:#991b1b;border:1px solid #fecaca}} .ai-chat-msg.pending{{align-self:flex-start;background:#eef2ff;color:#4338ca;font-style:italic}} .ai-chat-compose{{display:flex;gap:10px;align-items:flex-end}} .ai-chat-compose textarea{{flex:1;min-height:72px;max-height:180px;resize:vertical;border:1px solid #dbe4ef;border-radius:14px;padding:12px 14px;font-size:14px;line-height:1.5;font-family:inherit;box-sizing:border-box}} .ai-chat-compose .btn-test{{min-width:88px;align-self:stretch}} .ai-chat-usage{{margin-top:8px;padding-top:8px;border-top:1px solid #e2e8f0;color:#64748b;font-size:11px;text-align:right;line-height:1.4}} .diagnostic-export-card{{display:block;padding:18px 20px 20px;margin-bottom:16px}} .diagnostic-export-card::before{{opacity:.35}}
	@media(max-width:860px){{.app{{grid-template-columns:1fr}}.sidebar{{position:relative;height:auto;min-height:auto}}.card{{grid-template-columns:1fr}}.field{{grid-template-columns:1fr}}.field-col-left,.field-col-right,.field-group-head{{grid-column:auto}}.logs-grid{{grid-template-columns:1fr}}.log-panel{{min-height:420px}}}}
	</style></head><body><div class="app"><aside class="sidebar"><div class="sidebar-main"><div class="brand"><span class="logo">O</span><span>OKX AI</span></div>
	<a class="nav-item active" href="#monitor" data-page-link="monitor">监控</a><a class="nav-item" href="#config" data-page-link="config">配置</a><a class="nav-item" href="#logs" data-page-link="logs">日志</a><a class="nav-item" href="#tests" data-page-link="tests">测试</a><a class="nav-item" href="#design" data-page-link="design">设计</a><a class="nav-item" href="#help" data-page-link="help">帮助</a><a class="nav-item" href="#settings" data-page-link="settings">设置</a></div>
	<div class="sidebar-footer"><div class="sidebar-footer-panel"><div class="sidebar-footer-power"><button class="power-btn" type="button" id="powerMenuBtn" title="电源管理" aria-label="电源管理" aria-haspopup="true" aria-expanded="false"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" aria-hidden="true"><path d="M12 3v8"/><path d="M8.4 5.5a7 7 0 1 0 7.2 0"/></svg></button><span class="power-btn-label">电源管理</span><span class="power-btn-chevron" aria-hidden="true">›</span><div class="power-menu" id="powerMenu" hidden><button type="button" id="powerRestartBtn"><span>重启控制台</span><span class="power-menu-desc">{esc(power_menu_restart_hint)}</span></button><button type="button" class="danger" id="powerShutdownBtn"><span>关机</span><span class="power-menu-desc">{esc(power_menu_shutdown_hint)}</span></button></div></div><div class="power-mode-hint">{esc(power_mode_hint)}</div><div class="sidebar-version"><span class="sidebar-version-name">{esc(APP_NAME)}</span><span class="sidebar-version-tag">v{esc(APP_VERSION)}</span></div></div></div>
</aside><div class="content"><main>
{notice_html}
<form class="config-form" method="post" action="/save#config">{''.join(rows)}<div class="actions config-actions" data-page-actions="config"><div class="action-group"><button class="action-control btn-save" type="button" id="saveConfigBtn">另存为配置</button><button class="action-control btn-save" type="button" id="importConfigBtn">导入配置</button><button class="action-control btn-danger" type="button" id="resetConfigBtn">恢复默认配置</button><a class="button action-control btn-save" href="/config-json#config">查看配置</a></div></div></form>
<form class="settings-form" method="post" action="/save-auth#settings" autocomplete="off"><input type="text" name="fake_username" autocomplete="username" tabindex="-1" aria-hidden="true" class="autofill-trap"><input type="password" name="fake_password" autocomplete="current-password" tabindex="-1" aria-hidden="true" class="autofill-trap"><section class="card page-panel" data-page="settings"><h2>登录账号</h2><div class="field"><label>用户名</label><div><input type="text" name="auth_username" autocomplete="off" value="{esc(auth.get("username","admin"))}"><p>Web 控制台登录用户名（单账户）。</p></div></div><div class="field"><label>当前密码</label><div><div class="password-wrap"><input type="password" name="auth_current_password" autocomplete="off" placeholder="请手动输入当前密码" readonly data-verify-password><button class="eye-btn" type="button" data-toggle-password aria-label="显示或隐藏密码"></button></div><p>须手动输入当前密码验证身份，不会自动填充。</p></div></div><div class="field"><label>新密码</label><div><div class="password-wrap"><input type="password" name="auth_password" autocomplete="off" placeholder="留空则不修改" readonly data-verify-password><button class="eye-btn" type="button" data-toggle-password aria-label="显示或隐藏密码"></button></div><p>留空表示保留当前密码；保存后将停止监控/回放并退出登录。</p></div></div></section><section class="card page-panel" data-page="settings"><h2>恢复出厂设置</h2><p class="section-sub">清除 <code>build/runtime_logs/</code> 下运行日志、回放数据、模拟账户与 Token 统计，并将交易配置、AI 密钥、微信 SendKey 与登录账号恢复为出厂默认。会先停止监控与回放；操作不可撤销。</p><div class="field"><label>确认密码</label><div><div class="password-wrap"><input type="password" name="factory_reset_password" autocomplete="off" placeholder="请手动输入当前密码" readonly data-verify-password><button class="eye-btn" type="button" data-toggle-password aria-label="显示或隐藏密码"></button></div><p>须验证当前登录密码后才会执行；完成后请使用默认账号 admin / admin123 重新登录。</p></div></div><div class="actions" style="padding:0;margin-top:12px;"><button class="button action-control btn-danger" type="button" id="factoryResetBtn">恢复出厂设置</button></div><p class="accuracy-note" id="factoryResetHint" style="margin-top:10px;"></p></section><div class="actions settings-actions" data-page-actions="settings"><div class="action-group"><button class="action-control btn-save" type="submit">保存账号密码</button><a class="button action-control btn-danger" href="/logout">退出登录</a></div></div></form>
<div class="page-panel active" data-page="monitor"><section class="card toolbar-card monitor-toolbar-card"><div><h2>实时监控</h2><p class="section-sub">K 线默认显示；关闭开关后不再请求 OKX/日志绘图，<strong>监控进程照常运行</strong>。下方模拟账户按 final_direction 满仓跟单（会话 $10,000 重置）。</p></div><div class="toolbar-right"><span class="section-sub" id="monitorChartLabel" style="margin:0;white-space:nowrap;">K线显示已开启</span><label class="switch" title="仅控制 Web 是否绘制 K 线与快照，不影响后台监控"><input type="checkbox" id="monitorChartEnableToggle" checked><span></span></label><div class="coin-tabs">{monitor_tabs}</div><button class="button btn-run action-control" type="button" id="monitorToggleBtn">开始监控</button></div></section><p class="accuracy-note accuracy-off-hint monitor-chart-off-hint" id="monitorChartOffHint" hidden>K 线显示已关闭。监控进程与磁盘日志照常运行；打开右上角开关后将请求 OKX 并绘制图表。</p><section class="market-card monitor-card" id="monitorChartPanel"><div class="market-head"><div><div class="market-title" id="monitorTitle">{esc(monitor_initial or "未配置币种")} K线</div><div class="market-sub" id="monitorMeta">{esc("选择周期查看蜡烛图 · 启动监控后叠加分析指标" if monitor_initial else "请先在配置页选择监控币种")}</div><div class="bar-tabs" id="monitorBarTabs">{monitor_bar_tabs}</div></div><div class="market-price" id="monitorPrice"><strong>--</strong><span>加载中</span></div></div><div class="market-canvas-wrap"><canvas id="monitorChart"></canvas><div class="market-loading" id="monitorLoading">正在加载 K 线...</div><div class="snapshot-panel" id="snapshotPanel"><div class="snapshot-head"><strong id="snapshotPanelTitle">最新快照</strong><button type="button" class="snapshot-toggle" id="snapshotToggleBtn" title="收起或展开快照面板">收起</button></div><div class="snapshot-body" id="snapshotPanelBody"><div class="snapshot-grid"><span>状态</span><b>加载中...</b><span>提示</span><b>点击 K 线查看 OHLC</b></div></div></div></div><div class="market-time-range"><span id="monitorProcessInfo">PID -- · Token --</span><span id="monitorUptime">已监控：未启动</span><span id="monitorPaperAccount">模拟账户：--</span><span id="monitorPointCount">K线：0</span></div></section></div>
	<div class="page-panel" data-page="logs"><section class="card toolbar-card logs-toolbar-card"><div><h2>实时日志</h2><p class="section-sub">监控进程仍会照常写入磁盘（JSON 分析 + 控制台）；本页<strong>默认不拉取显示</strong>以节省资源。配置页可设单文件/总容量上限。</p></div><div class="toolbar-right" style="align-items:center;gap:10px;flex-wrap:wrap;"><span class="section-sub" id="logsDisplayLabel" style="margin:0;white-space:nowrap;">显示已关闭</span><label class="switch" title="仅控制 Web 是否读取并展示日志，不影响磁盘写入"><input type="checkbox" id="logsDisplayToggle"><span></span></label><span class="section-sub" id="analysisLogSwitchHint" style="margin:0;">{esc(log_size_summary_text(config))} · 修改后需重启监控</span><button class="button btn-log" type="button" id="refreshLogBtn">刷新全部</button><button class="button btn-log" type="button" id="openLogDirBtn">打开日志目录</button></div></section><p class="accuracy-note accuracy-off-hint" id="logsOffHint">Web 显示已关闭。监控仍正常写日志到磁盘；打开右上角开关后可在此查看本次启动后的内容。</p><section class="card logs-card" id="logPanelBody" hidden><div class="logs-grid"><div class="log-panel"><h3>JSON 分析日志</h3><p class="log-panel-desc">Web 图表/压测依赖的完整分析记录，分卷保存：{esc(MONITOR_JSON_LOG_FILE)} 及 .jsonl.1/.2 …</p><div class="log-panel-body"><textarea class="log-window" id="logWindow" readonly>正在加载日志...</textarea></div><div class="log-panel-footer"><div class="toolbar-card"><div><p class="section-sub" id="saveLogHint">可另存为 .jsonl 文件，便于回放与统计。</p></div><div class="toolbar-right"><button class="button btn-log" type="button" id="clearLogBtn">清除窗口</button><button class="btn-save" type="button" id="saveLogBtn">另存为文件</button></div></div></div></div><div class="log-panel"><h3>控制台日志</h3><p class="log-panel-desc">监控进程精简调试输出（信号摘要、推送结果、错误）；详细重试日志需设置 CONSOLE_VERBOSE=1，默认保存：{esc(MONITOR_PROCESS_LOG_FILE)}</p><div class="log-panel-body"><textarea class="log-window log-window-console" id="consoleLogWindow" readonly>正在加载控制台日志...</textarea></div><div class="log-panel-footer"><div class="toolbar-card"><div><p class="section-sub" id="saveConsoleLogHint">可另存为 .log 文件，便于快速排查信号与推送。</p></div><div class="toolbar-right"><button class="button btn-log" type="button" id="clearConsoleLogBtn">清除窗口</button><button class="btn-save" type="button" id="saveConsoleLogBtn">另存为文件</button></div></div></div></div></div></section></div>
	<div class="page-panel" data-page="tests"><section class="card diagnostic-export-card"><div class="ai-chat-head"><div><h2 style="margin:0 0 6px;">问题排查导出</h2><p class="section-sub" style="margin:0;">一键打包配置（密钥已打码）、监控/回放状态、运行时近期日志与回放数据、实时/回放压测走势图（SVG/PNG）与 JSON，便于反馈 bug 时分析。超大单文件仅含尾部。</p></div><div class="toolbar-right"><button class="button action-control btn-save" type="button" id="diagnosticExportBtn">导出诊断包</button></div></div><p class="accuracy-note" id="diagnosticExportHint">将下载 ZIP；含 runtime_logs 下全部日志、回放数据集尾部、各币种 session/replay/all 压测 JSON+SVG 图，以及当前浏览器压测/监控 K 线 PNG（如有）。</p></section><section class="card ai-chat-card"><div class="ai-chat-head"><div><h2 style="margin:0 0 6px;">AI 对话测试</h2><p class="section-sub" style="margin:0;">使用配置页中的 AI 密钥与模型；发送前会先保存当前配置。对话历史仅保留在本页浏览器内存中。「获取简报」按监控同款逻辑拉取 OKX 盘面并生成 [简报]（不推送微信）。</p></div><div class="toolbar-right"><button class="button btn-log" type="button" id="aiChatClearBtn">清空对话</button><button class="button action-control btn-test" type="button" id="aiChatBriefBtn">获取简报</button><button class="button action-control btn-test" type="button" id="aiChatPingBtn">连通性测试</button></div></div><div class="ai-chat-window" id="aiChatWindow"><div class="ai-chat-empty">输入下方消息开始与 AI 对话。可先点「连通性测试」验证配置是否可用。</div></div><div class="ai-chat-compose"><textarea id="aiChatInput" placeholder="输入消息，Enter 发送，Shift+Enter 换行"></textarea><button class="button action-control btn-test" type="button" id="aiChatSendBtn">发送</button></div></section><section class="card toolbar-card"><div><h2>微信推送测试</h2><p class="section-sub">测试 Server酱 推送是否可用。点击后会先保存配置页中的密钥，再发送与真实监控相同结构的格式预览（模拟 AI 全字段示例）。</p></div><div class="toolbar-right"><button class="button action-control btn-test" type="button" id="testPushBtn">测试微信推送</button></div></section><div id="connectivityTestNotice" class="notice" hidden></div><section class="card accuracy-card"><div class="toolbar-card" style="margin:0 0 12px;box-shadow:none;padding:0;background:transparent;border:none;align-items:flex-start;"><div><h2 style="margin:0 0 6px;">实时预测压测</h2><p class="section-sub" style="margin:0;">顶部<strong>分析次数、AI调用次数、Token总消耗</strong>按当前币种、范围和保留时长统计。模拟账户按 final_direction 从 $10,000 满仓跟单（方向变才换仓）；绿线为权益曲线。下方为分层预测准确度；<strong>AI前瞻命中率</strong>按每条 <code>forward_view</code> 的 horizon 独立验证。回放会话在价线上方标记<strong>应推送</strong>。<strong>默认关闭</strong>以节省资源，需要时再打开。</p></div><div class="toolbar-right" style="align-items:center;gap:10px;flex-shrink:0;"><span class="section-sub" id="accuracyEnableLabel" style="margin:0;white-space:nowrap;">压测已关闭</span><label class="switch" title="启用后才会读取分析日志并自动刷新压测图表"><input type="checkbox" id="accuracyEnableToggle" checked><span></span></label></div></div><p class="accuracy-note accuracy-off-hint" id="accuracyOffHint">压测已关闭。打开右上角开关后将读取分析日志并自动刷新图表。</p><div id="accuracyPanelBody" hidden><div class="accuracy-controls"><select id="accuracyInst">{accuracy_inst_options}</select>{accuracy_horizon_select_html}<select id="accuracyScope"><option value="session">本次启动后</option><option value="replay">回放会话</option><option value="all">全部历史日志</option></select><select id="accuracyRetentionHours" title="结合配置页轮询间隔计算图表最多保留多少点"><option value="1">保留1小时</option><option value="2">保留2小时</option><option value="4">保留4小时</option><option value="8">保留8小时</option><option value="12">保留12小时</option><option value="24" selected>保留24小时</option><option value="48">保留48小时</option></select><button class="btn-test" type="button" id="accuracyRefreshBtn">刷新压测</button><button class="btn-save" type="button" id="accuracyExportBtn">导出图表</button><button class="btn-save" type="button" id="accuracyImportBtn">导入图表</button><button class="btn-test" type="button" id="accuracyLiveBtn" style="display:none">返回实时</button><input type="file" id="accuracyImportInput" accept=".json,application/json" hidden></div><div class="accuracy-summary" id="accuracySummary"><div class="accuracy-primary"><span>分析次数</span><b>--</b></div><div class="accuracy-primary"><span>AI调用次数</span><b>--</b></div><div class="accuracy-primary"><span>Token总消耗</span><b>--</b></div></div><div class="accuracy-canvas-wrap"><canvas id="accuracyChart"></canvas><div class="accuracy-point-panel" id="accuracyPointPanel" hidden></div></div><p class="accuracy-note" id="accuracyNote">综合准确度用上方验证窗口；AI前瞻命中率用每条 forward_view 的 horizon（通常 15m）。观望：后续波动未超阈值即合理。交易：验证窗内价格朝做多/做空方向走即方向命中。</p></div></section><section class="card"><h2>历史回放生成</h2><p class="section-sub">从 OKX 拉取指定时间段的历史 K 线，并附带 OI / 资金费率 / 多空比，生成 <code>replay_dataset_historical.jsonl</code>。盘口为占位，盘口失衡类信号不会触发。合约默认与上方压测区所选一致。</p><div class="field"><label>开始时间</label><div><input type="datetime-local" id="replayBuildStart"><p>本地时间，建议不超过 48 小时跨度。</p></div></div><div class="field"><label>结束时间</label><div><input type="datetime-local" id="replayBuildEnd"></div></div><div class="field"><label>帧步长(秒)</label><div><input type="number" id="replayBuildStep" value="60" min="5" max="3600" step="1"><p>与策略轮询间隔接近即可（swing 建议 60，short/scalp 建议 5）。</p></div></div><div class="field"><label>操作</label><div><div class="toolbar-right" style="justify-content:flex-start;gap:8px;"><button class="button action-control btn-save" type="button" id="replayBuildBtn">生成回放数据</button></div><p class="accuracy-note" id="replayBuildStatus">尚未生成</p></div></div></section><section class="card"><h2>离线回放压测</h2><p class="section-sub">配置页勾选「录制回放数据集」并运行监控，每轮原始输入写入 {esc(REPLAY_DATASET_FILE)}；停止监控后点击下方「开始回放」。回放按配置启用 AI 与微信推送（未开推送则只写日志），结论写入 {esc(REPLAY_ANALYSIS_LOG_FILE)} 的 <code>push_analysis</code> 与 <code>analysis</code> 字段，便于同一数据集多次回放对比；再选上方「回放会话」查看压测曲线。</p><div class="replay-status" id="replayDatasetInfo">正在加载数据集状态...</div><div class="field"><label>回放间隔(秒)</label><div><input type="number" id="replayInterval" value="0" min="0" max="120" step="0.1"><p>0 表示尽快跑完；大于 0 可在回放过程中观察压测曲线刷新。</p></div></div><div class="field"><label>控制</label><div><div class="toolbar-right" style="justify-content:flex-start;gap:8px;"><button class="btn-test" type="button" id="replayStartBtn">开始回放</button><button class="btn-danger action-control" type="button" id="replayStopBtn">停止回放</button><button class="button btn-log" type="button" id="replayRefreshBtn">刷新状态</button></div><p id="replayStatusText">等待加载...</p></div></div></section></div>
	{help_manual_html(config)}
	{design_docs_html()}
	</main></div></div>
<script>
const SUGGESTED_PUSH_SCORES = {json.dumps(SUGGESTED_PUSH_SCORES, ensure_ascii=False)};
const RISK_PREFERENCE_LABELS = {json.dumps(RISK_PREFERENCE_LABELS, ensure_ascii=False)};
const STRATEGY_INTERVAL_SECONDS = {json.dumps(STRATEGY_DEFAULT_INTERVAL_SECONDS, ensure_ascii=False)};
const STRATEGY_INTERVAL_BOUNDS = {json.dumps(STRATEGY_INTERVAL_BOUNDS, ensure_ascii=False)};
const STRATEGY_ACCURACY_HORIZON_SECONDS = {json.dumps(STRATEGY_DEFAULT_ACCURACY_HORIZON_SECONDS, ensure_ascii=False)};
const STRATEGY_ACCURACY_HORIZON_HINTS = {json.dumps(STRATEGY_ACCURACY_HORIZON_HINTS, ensure_ascii=False)};
const LAUNCHED_BY_TRAY = {json.dumps(tray_mode)};
const POWER_CONFIRM_RESTART = {json.dumps(power_confirm_restart, ensure_ascii=False)};
const POWER_CONFIRM_SHUTDOWN = {json.dumps(power_confirm_shutdown, ensure_ascii=False)};
const POWER_SHUTDOWN_DONE = {json.dumps(power_shutdown_done, ensure_ascii=False)};
function currentRiskPreference() {{
  const el = document.querySelector('.config-form select[name="risk_preference"]');
  return el && el.value ? el.value : 'aggressive';
}}
function currentStrategyMode() {{
  const el = document.querySelector('.config-form select[name="strategy_mode"]');
  return el && el.value ? el.value : 'swing';
}}
function syncStrategyIntervalHint() {{
  const mode = currentStrategyMode();
  const recommended = Number(STRATEGY_INTERVAL_SECONDS[mode]);
  const bounds = STRATEGY_INTERVAL_BOUNDS[mode] || [1, 3600];
  const hint = document.getElementById('strategyIntervalHint');
  if (hint && Number.isFinite(recommended)) {{
    hint.textContent = '当前策略推荐 ' + recommended + ' 秒；可配置范围 ' + bounds[0] + '–' + bounds[1] + ' 秒';
  }}
}}
function configuredMonitorInterval() {{
  const el = document.querySelector('.config-form input[name="interval"]');
  if (el && el.value) {{
    const n = Number(el.value);
    if (Number.isFinite(n) && n >= 1) return n;
  }}
  const mapped = Number(STRATEGY_INTERVAL_SECONDS[currentStrategyMode()]);
  return Number.isFinite(mapped) && mapped >= 1 ? mapped : monitorIntervalSeconds;
}}
function configuredAccuracyHorizon() {{
  const sel = document.getElementById('accuracyHorizon');
  if (sel && sel.value) {{
    const n = Number(sel.value);
    if (Number.isFinite(n) && n >= 5) return n;
  }}
  const mapped = Number(STRATEGY_ACCURACY_HORIZON_SECONDS[currentStrategyMode()]);
  return Number.isFinite(mapped) && mapped >= 5 ? mapped : 900;
}}
function syncStrategyAccuracyHorizon(options) {{
  options = options || {{}};
  const mode = currentStrategyMode();
  const recommended = Number(STRATEGY_ACCURACY_HORIZON_SECONDS[mode]);
  const hint = STRATEGY_ACCURACY_HORIZON_HINTS[mode] || '推荐';
  const sel = document.getElementById('accuracyHorizon');
  if (!sel || !Number.isFinite(recommended) || recommended < 5) return configuredAccuracyHorizon();
  Array.from(sel.options).forEach(function(opt) {{
    const base = opt.getAttribute('data-base-label') || String(opt.textContent || '').replace(/ · .+$/, '');
    if (!opt.getAttribute('data-base-label')) opt.setAttribute('data-base-label', base);
    const sec = Number(opt.value);
    opt.textContent = sec === recommended ? (base + ' · ' + hint) : base;
  }});
  if (options.forceSelect) sel.value = String(recommended);
  return configuredAccuracyHorizon();
}}
function currentAiEnabled() {{
  const el = document.querySelector('.config-form input[name="ai_enabled"]');
  return !!(el && el.checked);
}}
function updatePushScoreGuide() {{
  const risk = currentRiskPreference();
  const suggested = SUGGESTED_PUSH_SCORES[risk] || SUGGESTED_PUSH_SCORES.standard;
  const label = RISK_PREFERENCE_LABELS[risk] || risk;
  const textEl = document.getElementById('pushScoreGuideText');
  if (textEl) {{
    const aiNote = currentAiEnabled()
      ? '已启用 AI：trade 建议可贴近下表。'
      : '未启用 AI：建议 trade 取偏保守一档（如标准 75→78~80）。';
    textEl.textContent = '当前严格度「' + label + '」建议 做多 ' + suggested.push_score
      + ' · 做空 ' + suggested.short_push_score
      + '。' + aiNote + ' watch/spike/演变/冷却等推送细则已内置为固定默认值。';
  }}
}}
function applySuggestedPushScores() {{
  const form = document.querySelector('.config-form');
  if (!form) return;
  const suggested = SUGGESTED_PUSH_SCORES[currentRiskPreference()] || SUGGESTED_PUSH_SCORES.standard;
  const tradeEl = form.querySelector('[name="push_score"]');
  const shortTradeEl = form.querySelector('[name="short_push_score"]');
  if (tradeEl) tradeEl.value = String(suggested.push_score);
  if (shortTradeEl) shortTradeEl.value = String(suggested.short_push_score);
  updatePushScoreGuide();
  autoSaveConfig().catch(function(error) {{ alert('保存建议分数失败：' + error); }});
}}
const applySuggestedPushScoresBtn = document.getElementById('applySuggestedPushScores');
if (applySuggestedPushScoresBtn) {{
  applySuggestedPushScoresBtn.addEventListener('click', applySuggestedPushScores);
}}
const riskPreferenceSelect = document.querySelector('.config-form select[name="risk_preference"]');
if (riskPreferenceSelect) {{
  riskPreferenceSelect.addEventListener('change', updatePushScoreGuide);
}}
const strategyModeSelect = document.querySelector('.config-form select[name="strategy_mode"]');
if (strategyModeSelect) {{
  strategyModeSelect.addEventListener('change', function() {{
    syncStrategyIntervalHint();
    syncStrategyAccuracyHorizon({{ forceSelect: true }});
    if (typeof isAccuracyEnabled === 'function' && isAccuracyEnabled()) {{
      fetchAccuracy({{ resetView: true }});
    }}
  }});
}}
const aiEnabledCheckbox = document.querySelector('.config-form input[name="ai_enabled"]');
if (aiEnabledCheckbox) {{
  aiEnabledCheckbox.addEventListener('change', updatePushScoreGuide);
}}
updatePushScoreGuide();
function currentPage() {{
  return (location.hash || '#monitor').replace('#', '') || 'monitor';
}}
function showPage(page) {{
  document.querySelectorAll('.page-panel, .page-section').forEach(function(section) {{
    section.classList.toggle('active', section.getAttribute('data-page') === page);
  }});
  document.querySelectorAll('[data-page-link]').forEach(function(item) {{
    item.classList.toggle('active', item.getAttribute('data-page-link') === page);
  }});
  const configForm = document.querySelector('.config-form');
  const settingsForm = document.querySelector('.settings-form');
  if (configForm) configForm.style.display = page === 'config' ? 'block' : 'none';
  if (settingsForm) settingsForm.style.display = page === 'settings' ? 'block' : 'none';
  document.querySelectorAll('[data-page-actions]').forEach(function(actions) {{
    actions.style.display = actions.getAttribute('data-page-actions') === page ? 'flex' : 'none';
  }});
  if (page === 'logs' && isLogsDisplayEnabled()) {{ refreshLogs(false); refreshConsoleLogs(false); }}
  if (page === 'monitor') {{
    if (isMonitorChartEnabled()) fetchMonitor(false);
    else showMonitorChartOffState();
  }}
  if (page === 'settings' && settingsForm) initVerifyPasswordFields(settingsForm);
  if (page === 'tests') {{
    refreshReplayInfo({{ lite: false }}).then(function(info) {{
      if (isAccuracyEnabled()) fetchAccuracy({{ resetView: true }});
      if (info && info.replay_running) startReplayProgress();
    }});
  }}
}}
function encodeForm(form) {{
  const params = new URLSearchParams();
  new FormData(form).forEach(function(value, key) {{
    params.append(key, value);
  }});
  return params.toString();
}}
async function postFormJson(url, form) {{
  const response = await fetch(url, {{
    method: 'POST',
    headers: {{ 'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8' }},
    body: encodeForm(form),
    cache: 'no-store'
  }});
  const payload = await response.json();
  if (!response.ok || payload.ok === false) throw new Error(payload.error || '请求失败');
  return payload;
}}
async function autoSaveConfig() {{
  const form = document.querySelector('.config-form');
  if (!form) return {{ ok: true }};
  const payload = await postFormJson('/api/config/save', form);
  if (payload.inst_ids) refreshInstSelectors(payload.inst_ids);
  let feedback = '配置已保存。';
  if (payload.restart_hint) feedback = payload.restart_hint;
  else if (payload.requires_monitor_restart) feedback = '配置已保存。请停止并重新启动监控以加载新配置。';
  setAddInstFeedback(feedback, true);
  return payload;
}}
function setAddInstFeedback(text, ok) {{
  const box = document.getElementById('addInstFeedback');
  if (!box) return;
  box.textContent = text || '输入 OKX 永续合约 ID 并点击添加；校验通过后会出现在上方，右侧「删除」可移除。';
  box.classList.remove('is-error', 'is-ok');
  if (ok === true) box.classList.add('is-ok');
  if (ok === false) box.classList.add('is-error');
}}
function ensureHiddenValue(form, name, value) {{
  if (!form || !value) return;
  const exists = Array.from(form.querySelectorAll('input[name="' + name + '"]')).some(function(el) {{ return el.value === value; }});
  if (exists) return;
  const input = document.createElement('input');
  input.type = 'hidden';
  input.name = name;
  input.value = value;
  form.appendChild(input);
}}
function removeHiddenValue(form, name, value) {{
  if (!form || !value) return;
  form.querySelectorAll('input[name="' + name + '"]').forEach(function(el) {{
    if (el.value === value) el.remove();
  }});
}}
function knownInstIds() {{
  const ids = [];
  document.querySelectorAll('#instChecks [data-inst]').forEach(function(el) {{
    const inst = el.getAttribute('data-inst');
    if (inst) ids.push(inst);
  }});
  return ids;
}}
function appendInstTile(instId, checked, isPreset) {{
  const box = document.getElementById('instChecks');
  const form = document.querySelector('.config-form');
  if (!box || !instId || knownInstIds().indexOf(instId) >= 0) return false;
  if (form) removeHiddenValue(form, 'removed_inst_ids', instId);
  const tile = document.createElement('div');
  tile.className = 'check-tile check-tile-custom';
  tile.setAttribute('data-inst', instId);
  tile.setAttribute('data-preset', isPreset ? '1' : '0');
  tile.innerHTML = '<label><input type="checkbox" name="inst_ids" value="' + instId + '"' + (checked ? ' checked' : '') + '><span>' + instId + '</span></label>'
    + '<button class="inst-remove-btn" type="button" data-remove-inst="' + instId + '" aria-label="删除 ' + instId + '">删除</button>'
    + (isPreset ? '' : '<input type="hidden" name="custom_inst_ids" value="' + instId + '">');
  box.appendChild(tile);
  bindInstTileActions(tile);
  return true;
}}
function removeInstTile(instId) {{
  const form = document.querySelector('.config-form');
  const tile = document.querySelector('#instChecks [data-inst="' + instId + '"]');
  if (!tile) return;
  const isPreset = tile.getAttribute('data-preset') === '1';
  tile.remove();
  if (form && isPreset) ensureHiddenValue(form, 'removed_inst_ids', instId);
}}
function bindInstTileActions(root) {{
  (root || document).querySelectorAll('.inst-remove-btn').forEach(function(btn) {{
    if (btn.dataset.bound === '1') return;
    btn.dataset.bound = '1';
    btn.addEventListener('click', function(event) {{
      event.preventDefault();
      event.stopPropagation();
      const instId = btn.getAttribute('data-remove-inst');
      if (!instId) return;
      if (!window.confirm('确定删除 ' + instId + ' 吗？')) return;
      removeInstTile(instId);
      autoSaveConfig().catch(function(error) {{
        showMainNotice('配置保存失败：' + error, false);
        setAddInstFeedback(String(error), false);
      }});
    }});
  }});
}}
async function addNewInstId() {{
  const input = document.getElementById('newInstId');
  const btn = document.getElementById('addInstBtn');
  const instId = input ? String(input.value || '').trim().toUpperCase() : '';
  if (!instId) {{
    setAddInstFeedback('请输入合约 ID，例如 SOL-USDT-SWAP。', false);
    if (input) input.focus();
    return;
  }}
  if (knownInstIds().indexOf(instId) >= 0) {{
    setAddInstFeedback(instId + ' 已在列表中，直接勾选即可。', false);
    return;
  }}
  if (btn) {{ btn.disabled = true; btn.textContent = '校验中...'; }}
  try {{
    const response = await fetch('/api/add-inst-id', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8' }},
      body: 'inst_id=' + encodeURIComponent(instId),
      cache: 'no-store'
    }});
    const payload = await response.json();
    if (!response.ok || payload.ok === false) throw new Error(payload.error || '添加失败');
    appendInstTile(payload.inst_id || instId, true, !!payload.is_preset);
    if (input) input.value = '';
    await autoSaveConfig();
    setAddInstFeedback('已添加 ' + (payload.inst_id || instId) + '，并已加入监控列表。', true);
  }} catch (error) {{
    setAddInstFeedback(String(error), false);
    showMainNotice('添加币种失败：' + error, false);
  }} finally {{
    if (btn) {{ btn.disabled = false; btn.textContent = '添加'; }}
  }}
}}
function refreshInstSelectors(insts) {{
  if (!Array.isArray(insts)) return;
  refreshMonitorTabs(insts);
  const sel = document.getElementById('accuracyInst');
  if (!sel) return;
  const current = sel.value;
  sel.innerHTML = insts.map(function(inst) {{ return '<option value="' + inst + '">' + inst + '</option>'; }}).join('');
  if (insts.indexOf(current) >= 0) sel.value = current;
  else if (insts.length) sel.value = insts[0];
}}
let autoSaveTimer = null;
const configForm = document.querySelector('.config-form');
if (configForm) {{
  bindInstTileActions(configForm);
  configForm.addEventListener('input', function(event) {{
    if (event.target && event.target.id === 'newInstId') return;
    clearTimeout(autoSaveTimer);
    autoSaveTimer = setTimeout(function() {{
      autoSaveConfig().catch(function(error) {{
        showMainNotice('配置保存失败：' + error, false);
        setAddInstFeedback(String(error), false);
      }});
    }}, 450);
  }});
  configForm.addEventListener('change', function(event) {{
    if (event.target && (event.target.name === 'inst_ids' || event.target.name === 'strategy_mode' || event.target.name === 'interval')) {{
      if (event.target.name === 'strategy_mode') {{
        syncStrategyIntervalHint();
        syncStrategyAccuracyHorizon({{ forceSelect: true }});
      }}
      if (event.target.name === 'interval') {{
        monitorIntervalSeconds = configuredMonitorInterval();
      }}
      clearTimeout(autoSaveTimer);
      autoSaveTimer = setTimeout(function() {{
        autoSaveConfig().catch(function(error) {{
          showMainNotice('配置保存失败：' + error, false);
          setAddInstFeedback(String(error), false);
        }});
      }}, 120);
    }}
  }});
  const addInstBtn = document.getElementById('addInstBtn');
  const newInstInput = document.getElementById('newInstId');
  if (addInstBtn) addInstBtn.addEventListener('click', function() {{ addNewInstId(); }});
  if (newInstInput) {{
    newInstInput.addEventListener('keydown', function(event) {{
      if (event.key === 'Enter') {{
        event.preventDefault();
        addNewInstId();
      }}
    }});
  }}
}}
document.querySelectorAll('[data-toggle-password]').forEach(function(btn) {{
  btn.addEventListener('click', function() {{
    const input = btn.parentElement.querySelector('input');
    if (!input) return;
    input.type = input.type === 'password' ? 'text' : 'password';
    btn.classList.toggle('is-visible', input.type === 'text');
  }});
}});
function initVerifyPasswordFields(root) {{
  (root || document).querySelectorAll('input[data-verify-password]').forEach(function(input) {{
    input.value = '';
    input.setAttribute('readonly', 'readonly');
    input.addEventListener('focus', function() {{ input.removeAttribute('readonly'); }}, {{ once: true }});
  }});
}}
initVerifyPasswordFields(document.querySelector('.settings-form'));
function showMainNotice(text, ok) {{
  let box = document.querySelector('main > .notice');
  if (!text) {{
    if (box) box.remove();
    return;
  }}
  if (!box) {{
    box = document.createElement('div');
    box.className = ok === false ? 'notice notice-error' : 'notice';
    const main = document.querySelector('main');
    if (main) main.insertBefore(box, main.firstChild);
  }} else {{
    box.className = ok === false ? 'notice notice-error' : 'notice';
  }}
  box.textContent = text;
}}
const settingsForm = document.querySelector('.settings-form');
if (settingsForm) {{
  settingsForm.addEventListener('submit', async function(event) {{
    event.preventDefault();
    const btn = settingsForm.querySelector('button[type="submit"]');
    const currentPwd = settingsForm.querySelector('input[name="auth_current_password"]');
    const newPwd = settingsForm.querySelector('input[name="auth_password"]');
    if (!currentPwd || !String(currentPwd.value || '').trim()) {{
      showMainNotice('请先输入当前密码。', false);
      if (currentPwd) currentPwd.focus();
      return;
    }}
    if (btn) {{ btn.disabled = true; btn.textContent = '保存中...'; }}
    try {{
      const payload = await postFormJson('/api/save-auth', settingsForm);
      if (currentPwd) currentPwd.value = '';
      if (newPwd) newPwd.value = '';
      window.location.href = payload.redirect || '/login?auth_changed=1';
    }} catch (error) {{
      showMainNotice('保存账号失败：' + error, false);
      if (btn) {{ btn.disabled = false; btn.textContent = '保存账号密码'; }}
    }}
  }});
}}
function autoName() {{
  const d = new Date();
  const p = function(n) {{ return String(n).padStart(2, '0'); }};
  return 'okx_ai_config_' + d.getFullYear() + p(d.getMonth() + 1) + p(d.getDate()) + '_' + p(d.getHours()) + p(d.getMinutes()) + p(d.getSeconds()) + '.json';
}}
const saveConfigBtn = document.getElementById('saveConfigBtn');
if (saveConfigBtn) {{
  saveConfigBtn.addEventListener('click', async function() {{
    try {{
      const payload = await postFormJson('/api/config/export', document.querySelector('.config-form'));
      const text = JSON.stringify(payload.bundle, null, 2);
      const name = autoName();
      if (window.showSaveFilePicker) {{
        const handle = await showSaveFilePicker({{
          suggestedName: name,
          types: [{{ description: 'OKX AI配置文件', accept: {{ 'application/json': ['.json'] }} }}]
        }});
        const writable = await handle.createWritable();
        await writable.write(text);
        await writable.close();
      }} else {{
        const blob = new Blob([text], {{ type: 'application/json;charset=utf-8' }});
        const url = URL.createObjectURL(blob);
        const link = document.createElement('a');
        link.href = url;
        link.download = name;
        link.click();
        URL.revokeObjectURL(url);
      }}
    }} catch (error) {{
      alert('保存配置失败：' + error);
    }}
  }});
}}
const importConfigBtn = document.getElementById('importConfigBtn');
	if (importConfigBtn) {{
	  importConfigBtn.addEventListener('click', async function() {{
    try {{
      let file = null;
      if (window.showOpenFilePicker) {{
        const handles = await showOpenFilePicker({{
          multiple: false,
          types: [{{ description: 'OKX AI配置文件', accept: {{ 'application/json': ['.json'] }} }}]
        }});
        file = await handles[0].getFile();
      }} else {{
        const input = document.createElement('input');
        input.type = 'file';
        input.accept = '.json,application/json';
        file = await new Promise(function(resolve) {{
          input.onchange = function() {{ resolve(input.files && input.files[0]); }};
          input.click();
        }});
      }}
      if (!file) return;
      const response = await fetch('/api/config/import', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: await file.text(),
        cache: 'no-store'
      }});
      const payload = await response.json();
      if (!response.ok || payload.ok === false) throw new Error(payload.error || '导入失败');
      location.href = '/#config';
      location.reload();
    }} catch (error) {{
      alert('导入配置失败：' + error);
    }}
	  }});
	}}
const resetConfigBtn = document.getElementById('resetConfigBtn');
if (resetConfigBtn) {{
  resetConfigBtn.addEventListener('click', async function() {{
    const confirmed = window.confirm(
      '将把所有可配置项恢复为出厂默认值（AI 密钥与微信 SendKey 不变）。\\n'
      + '内置固定的 watch/spike/演变/自校准/模拟等细则不会出现在表单中，但会一并恢复。\\n'
      + '若监控正在运行，需停止后重新启动才生效。\\n\\n是否继续？'
    );
    if (!confirmed) return;
    resetConfigBtn.disabled = true;
    resetConfigBtn.textContent = '恢复中...';
    try {{
      const response = await fetch('/api/config/reset-defaults', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: '{{}}',
        cache: 'no-store'
      }});
      const payload = await response.json();
      if (!response.ok || payload.ok === false) throw new Error(payload.error || '恢复失败');
      location.href = '/#config';
      location.reload();
    }} catch (error) {{
      alert('恢复默认配置失败：' + error);
      resetConfigBtn.disabled = false;
      resetConfigBtn.textContent = '恢复默认配置';
    }}
  }});
}}
const factoryResetBtn = document.getElementById('factoryResetBtn');
if (factoryResetBtn) {{
  factoryResetBtn.addEventListener('click', async function() {{
    const pwdInput = document.querySelector('input[name="factory_reset_password"]');
    const hint = document.getElementById('factoryResetHint');
    const password = pwdInput ? String(pwdInput.value || '').trim() : '';
    if (!password) {{
      showMainNotice('请先输入当前密码以确认恢复出厂设置。', false);
      if (pwdInput) pwdInput.focus();
      return;
    }}
    const confirmed = window.confirm(
      '将清除所有运行日志、回放数据、模拟账户与 Token 统计，\\n'
      + '并重置交易配置、AI 密钥、微信 SendKey 与登录账号为出厂默认（admin / admin123）。\\n'
      + '监控与回放会先停止。此操作不可撤销。\\n\\n是否继续？'
    );
    if (!confirmed) return;
    factoryResetBtn.disabled = true;
    factoryResetBtn.textContent = '恢复中...';
    if (hint) hint.textContent = '正在清除运行数据并重置配置，请稍候...';
    try {{
      const response = await fetch('/api/factory-reset', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ password: password }}),
        cache: 'no-store'
      }});
      const payload = await response.json();
      if (!response.ok || payload.ok === false) throw new Error(payload.error || payload.message || '恢复失败');
      window.location.href = payload.redirect || '/login?factory_reset=1';
    }} catch (error) {{
      showMainNotice('恢复出厂设置失败：' + error, false);
      if (hint) hint.textContent = '';
      factoryResetBtn.disabled = false;
      factoryResetBtn.textContent = '恢复出厂设置';
    }}
  }});
}}
	let replayBuildTimer=null;
	function toDatetimeLocalValue(date){{const pad=n=>String(n).padStart(2,'0');return date.getFullYear()+'-'+pad(date.getMonth()+1)+'-'+pad(date.getDate())+'T'+pad(date.getHours())+':'+pad(date.getMinutes());}}
	function initReplayBuildDefaults(){{const startEl=document.getElementById('replayBuildStart'),endEl=document.getElementById('replayBuildEnd'),stepEl=document.getElementById('replayBuildStep');if(!startEl||!endEl)return;if(!startEl.value||!endEl.value){{const end=new Date();end.setMinutes(0,0,0);const start=new Date(end.getTime()-6*3600*1000);startEl.value=toDatetimeLocalValue(start);endEl.value=toDatetimeLocalValue(end);}}if(stepEl&&!stepEl.value){{const mapped=Number(STRATEGY_INTERVAL_SECONDS[currentStrategyMode()]);stepEl.value=String(Number.isFinite(mapped)&&mapped>=5?mapped:60);}}}}
	function renderReplayBuildStatus(st){{const box=document.getElementById('replayBuildStatus');if(!box)return;st=st||{{}};if(st.running){{const total=Number(st.total)||0,current=Number(st.current)||0,prefix=total>0?(current+'/'+total+' · '):'';box.textContent=prefix+(st.message||'生成中...');return;}}if(st.error){{box.textContent='生成失败：'+st.error;return;}}if(st.result&&st.result.frame_count){{box.textContent='已生成 '+st.result.frame_count+' 帧 · '+st.result.range_start+' ~ '+st.result.range_end;return;}}const hist=st.historical_dataset||{{}};box.textContent=hist.exists?('上次生成 '+hist.frame_count+' 帧 · '+hist.path):'尚未生成';}}
	function stopReplayBuildPoll(){{if(replayBuildTimer){{clearInterval(replayBuildTimer);replayBuildTimer=null;}}}}
	function startReplayBuildPoll(){{stopReplayBuildPoll();replayBuildTimer=setInterval(async function(){{if(currentPage()!=='tests')return;try{{const p=await fetch('/api/replay-build-status',{{cache:'no-store'}}).then(r=>r.json());if(!p.ok&&p.error)throw new Error(p.error);renderReplayBuildStatus(p);if(!p.running){{stopReplayBuildPoll();await refreshReplayInfo({{lite:false}});if(p.error)alert('历史回放生成失败：'+p.error);else if(p.result&&p.result.frame_count)alert('历史回放已生成 '+p.result.frame_count+' 帧');}}}}catch(e){{const box=document.getElementById('replayBuildStatus');if(box)box.textContent='生成状态加载失败：'+e;stopReplayBuildPoll();}}}},2000);}}
	async function buildHistoricalReplay(){{const btn=document.getElementById('replayBuildBtn'),statusEl=document.getElementById('replayBuildStatus');initReplayBuildDefaults();const startEl=document.getElementById('replayBuildStart'),endEl=document.getElementById('replayBuildEnd'),stepEl=document.getElementById('replayBuildStep');if(!startEl||!endEl||!startEl.value||!endEl.value){{alert('请填写开始与结束时间');return;}}if(btn)btn.disabled=true;if(statusEl)statusEl.textContent='正在提交生成任务...';try{{const instEl=document.getElementById('accuracyInst');const payload={{inst_id:instEl&&instEl.value?instEl.value:'ETH-USDT-SWAP',start_time:startEl.value,end_time:endEl.value,step_seconds:Number(stepEl&&stepEl.value)||configuredMonitorInterval()}};const r=await fetch('/api/replay-build-historical',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(payload),cache:'no-store'}});const p=await r.json();if(!r.ok||p.ok===false)throw new Error(p.error||p.message||'提交失败');renderReplayBuildStatus(p);startReplayBuildPoll();}}catch(e){{if(statusEl)statusEl.textContent='提交失败：'+e;alert('生成回放数据失败：'+e);}}finally{{if(btn)btn.disabled=false;}}}}
	function renderReplayDatasetInfo(info){{const box=document.getElementById('replayDatasetInfo');if(!box)return;const lines=[];const hist=info.historical_dataset||{{}};lines.push('录制数据集: '+(info.exists?info.frame_count+' 帧 · '+((info.inst_ids||[]).join(', ')||'--'):'尚未录制'));if(info.exists){{lines.push('录制路径: '+(info.path||'--'));if(info.recorded_at)lines.push('录制 meta: '+info.recorded_at);if(info.interval_seconds)lines.push('录制间隔: '+info.interval_seconds+' 秒');}}lines.push('历史数据集: '+(hist.exists?hist.frame_count+' 帧 · '+((hist.inst_ids||[]).join(', ')||'--'):'尚未生成'));if(hist.exists)lines.push('历史路径: '+(hist.path||info.historical_dataset_path||'--'));if(info.replay_running&&info.analysis_log_bytes)lines.push('回放日志: 约 '+Math.max(1,Math.round(info.analysis_log_bytes/1024))+' KB');else if(info.analysis_log_lines)lines.push('上次回放分析日志: '+info.analysis_log_lines+' 行');const aiLabel=info.ai_enabled?(info.dry_run_ai?'AI: 干跑(不调接口)':'AI: 已启用'):'AI: 未启用';const pushLabel=info.push_enabled?'微信: 已启用':'微信: 未启用(仅日志)';lines.push(aiLabel+' · '+pushLabel+' · 录制开关: '+(info.record_enabled?'已勾选':'未勾选')+' · 监控: '+(info.monitor_running?'运行中':'未运行'));box.textContent=lines.join('\\n');const histToggle=document.getElementById('replayUseHistoricalDataset');if(histToggle&&!histToggle.dataset.userSet&&hist.exists)histToggle.checked=true;renderReplayBuildStatus(info.build_status||{{}});}}
	function renderReplayStatus(info){{const text=document.getElementById('replayStatusText');if(!text)return;const st=info&&info.replay_status?info.replay_status:{{}};let msg=(st.text||'--')+(st.started_at?' · 开始 '+st.started_at:'')+(st.elapsed_seconds!=null?' · 已运行 '+st.elapsed_seconds+' 秒':'');if(info&&info.replay_running&&info.analysis_log_bytes)msg+=' · 日志约 '+Math.max(1,Math.round(info.analysis_log_bytes/1024))+' KB';else if(!info.replay_running&&info.analysis_log_lines)msg+=' · 分析日志 '+info.analysis_log_lines+' 行';text.textContent=msg;}}
	function setReplayStartButtonState(mode){{const btn=document.getElementById('replayStartBtn');if(!btn)return;if(mode==='running'){{btn.disabled=false;btn.textContent='回放中...';btn.classList.add('is-starting');}}else if(mode==='starting'){{btn.disabled=true;btn.textContent='启动中...';btn.classList.add('is-starting');}}else{{btn.disabled=false;btn.textContent='开始回放';btn.classList.remove('is-starting');}}}}
	async function refreshReplayInfo(options){{options=options||{{}};const lite=options.lite!==false;try{{const url='/api/replay-dataset'+(lite?'?lite=1':'');const r=await fetch(url,{{cache:'no-store'}}),p=await r.json();if(!r.ok||p.ok===false)throw new Error(p.error||'加载失败');renderReplayDatasetInfo(p);renderReplayStatus(p);if(p.build_status&&p.build_status.running)startReplayBuildPoll();if(!lite&&syncAccuracyScopeWithReplay(p)&&isAccuracyEnabled())fetchAccuracy({{resetView:true}});if(!lite&&p.replay_running)startReplayProgress();return p;}}catch(e){{const box=document.getElementById('replayDatasetInfo');if(box)box.textContent='加载回放状态失败：'+e;renderReplayStatus({{replay_status:{{text:String(e)}}}});return null;}}}}
	let replayProgressTimer=null,replayFinishedNotified=false,fetchAccuracyInFlight=null;
	function stopReplayProgress(){{stopReplayAccuracyPoll();if(replayProgressTimer){{clearInterval(replayProgressTimer);replayProgressTimer=null;}}}}
	function startReplayProgress(){{stopReplayProgress();replayFinishedNotified=false;switchAccuracyToReplaySession();replayProgressTimer=setInterval(async function(){{if(currentPage()!=='tests')return;const info=await refreshReplayInfo({{lite:true}});if(!info)return;setReplayStartButtonState(info.replay_running?'running':'idle');if(info.replay_running){{if(isAccuracyEnabled())fetchAccuracy({{resetView:false}});return;}}stopReplayProgress();if(isAccuracyEnabled())fetchAccuracy({{resetView:true}});if(!replayFinishedNotified){{replayFinishedNotified=true;const full=await refreshReplayInfo({{lite:false}});const lines=(full&&full.analysis_log_lines!=null)?full.analysis_log_lines:(info.analysis_log_lines!=null?info.analysis_log_lines:'--');alert('回放完成 · 分析日志 '+lines+' 行 · 上方压测已切到「回放会话」');}}}},2500);}}
	async function startReplayRun(){{const intervalEl=document.getElementById('replayInterval'),interval=Number(intervalEl&&intervalEl.value),statusEl=document.getElementById('replayStatusText');setReplayStartButtonState('starting');if(statusEl)statusEl.textContent='正在启动回放，请稍候...';try{{const r=await fetch('/api/replay-start',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{interval:Number.isFinite(interval)?interval:0,dataset:(document.getElementById('replayUseHistoricalDataset')&&document.getElementById('replayUseHistoricalDataset').checked)?'historical':'recorded'}}),cache:'no-store'}});let p=null;const ct=(r.headers.get('content-type')||'').toLowerCase();if(ct.includes('application/json')){{p=await r.json();}}else{{throw new Error('服务器返回异常页面（HTTP '+r.status+'），请刷新并重新登录后再试');}}if(!r.ok||p.ok===false)throw new Error(p.error||p.message||'启动失败');renderReplayDatasetInfo(p);renderReplayStatus(p);if(statusEl)statusEl.textContent=p.message||'回放已启动';setReplayStartButtonState('running');setAccuracyEnabled(true,{{fetchNow:true}});startReplayProgress();}}catch(e){{setReplayStartButtonState('idle');if(statusEl)statusEl.textContent='启动失败：'+e;alert('启动回放失败：'+e);}}}}
	async function stopReplayRun(){{const btn=document.getElementById('replayStopBtn');if(btn)btn.disabled=true;try{{const r=await fetch('/api/replay-stop',{{method:'POST',cache:'no-store'}}),p=await r.json();if(!r.ok||p.ok===false)throw new Error(p.error||p.message||'停止失败');stopReplayProgress();setReplayStartButtonState('idle');await refreshReplayInfo({{lite:false}});if(isAccuracyEnabled())fetchAccuracy({{resetView:true}});alert(p.message||'回放已停止');}}catch(e){{alert('停止回放失败：'+e);}}finally{{if(btn)btn.disabled=false;}}}}
	const replayStartBtn=document.getElementById('replayStartBtn'),replayStopBtn=document.getElementById('replayStopBtn'),replayRefreshBtn=document.getElementById('replayRefreshBtn');
	if(replayStartBtn)replayStartBtn.addEventListener('click',startReplayRun);
	if(replayStopBtn)replayStopBtn.addEventListener('click',stopReplayRun);
	if(replayRefreshBtn)replayRefreshBtn.addEventListener('click',refreshReplayInfo);
	const replayBuildBtn=document.getElementById('replayBuildBtn'),replayUseHistorical=document.getElementById('replayUseHistoricalDataset');
	if(replayBuildBtn)replayBuildBtn.addEventListener('click',buildHistoricalReplay);
	if(replayUseHistorical)replayUseHistorical.addEventListener('change',function(){{replayUseHistorical.dataset.userSet='1';}});
	initReplayBuildDefaults();
	const accuracyRefreshBtn=document.getElementById('accuracyRefreshBtn');
	const ACCURACY_ENABLE_KEY='okx_accuracy_enabled';
	function isAccuracyEnabled(){{const el=document.getElementById('accuracyEnableToggle');return!!(el&&el.checked);}}
	function setAccuracyEnabled(enabled,options){{options=options||{{}};const toggle=document.getElementById('accuracyEnableToggle');if(toggle)toggle.checked=!!enabled;updateAccuracyEnableUI(!!options.fetchNow);}}
	function updateAccuracyEnableUI(fetchNow){{const enabled=isAccuracyEnabled(),body=document.getElementById('accuracyPanelBody'),label=document.getElementById('accuracyEnableLabel'),offHint=document.getElementById('accuracyOffHint');if(body)body.hidden=!enabled;if(offHint)offHint.hidden=enabled;if(label)label.textContent=enabled?'压测已开启 · 自动刷新':'压测已关闭 · 按需打开';try{{localStorage.setItem(ACCURACY_ENABLE_KEY,enabled?'1':'0');}}catch(e){{}}if(!enabled)return;if(fetchNow)fetchAccuracy({{resetView:true}});}}
	function initAccuracyEnableToggle(){{const toggle=document.getElementById('accuracyEnableToggle');if(!toggle)return;try{{const saved=localStorage.getItem(ACCURACY_ENABLE_KEY);toggle.checked=saved!=='0';}}catch(e){{toggle.checked=true;}}updateAccuracyEnableUI(false);toggle.addEventListener('change',function(){{updateAccuracyEnableUI(true);syncReplayAccuracyPoll();}});}}
	initAccuracyEnableToggle();
	let monitorIntervalSeconds={int(config.get("interval", recommended_interval_for_strategy(config.get("strategy_mode", "swing"))))};
	const accuracyView={{points:[],start:0,end:1,yZoom:1,yPan:0,priceRg:1,drag:null,followLatest:true,selectedKey:''}};
	let accuracyPlotPoints=[];
	let accuracyQueryKey='',accuracyLivePayload=null,accuracyImportedMode=false,accuracyImportedLabel='',accuracyScopeSyncing=false,replayAccuracyPollTimer=null;
	function stopReplayAccuracyPoll(){{if(replayAccuracyPollTimer){{clearInterval(replayAccuracyPollTimer);replayAccuracyPollTimer=null;}}}}
	async function isReplayRunning(){{try{{const p=await fetch('/api/replay-status',{{cache:'no-store'}}).then(r=>r.json());return!!(p&&p.replay_running);}}catch(e){{return false;}}}}
	function syncReplayAccuracyPoll(){{const scopeEl=document.getElementById('accuracyScope');if(!scopeEl||scopeEl.value!=='replay'||currentPage()!=='tests'){{stopReplayProgress();return;}}isReplayRunning().then(function(running){{if(running)startReplayProgress();else stopReplayProgress();}});}}
	syncStrategyIntervalHint();
	syncStrategyAccuracyHorizon({{ forceSelect: false }});
	function accuracyRetentionHours(){{const n=Number((document.getElementById('accuracyRetentionHours')||{{}}).value);return Number.isFinite(n)&&n>0?n:{int(DEFAULT_ACCURACY_RETENTION_HOURS)};}}
	function accuracyQuerySignature(){{return [(document.getElementById('accuracyInst')||{{}}).value||'BTC-USDT-SWAP',String(configuredAccuracyHorizon()),(document.getElementById('accuracyScope')||{{}}).value||'session',accuracyRetentionHours(),configuredMonitorInterval()].join('|');}}
	function setAccuracyImportedMode(active,label){{accuracyImportedMode=!!active;accuracyImportedLabel=label||'';const liveBtn=document.getElementById('accuracyLiveBtn');if(liveBtn)liveBtn.style.display=active?'inline-flex':'none';}}
	function normalizeAccuracyBundle(raw){{if(!raw||typeof raw!=='object')throw new Error('无效JSON文件');const points=Array.isArray(raw.points)?raw.points:[];const clean=points.filter(o=>Number.isFinite(Number(o&&o.price))),pushMarkers=Array.isArray(raw.push_markers)?raw.push_markers.filter(o=>o&&o.time&&Number.isFinite(Number(o.price))):[];if(!clean.length)throw new Error('文件中没有可用的图表点位');return{{name:raw.name||'OKX_Accuracy_Chart',version:raw.version||'1.0',exported_at:raw.exported_at||'',inst_id:raw.inst_id||'',horizon_seconds:Number(raw.horizon_seconds)||configuredAccuracyHorizon(),scope:raw.scope||'imported',retention_hours:Number(raw.retention_hours)||accuracyRetentionHours(),interval_seconds:Number(raw.interval_seconds)||configuredMonitorInterval(),max_points:Number(raw.max_points)||clean.length,start_at:raw.start_at||'',summary:(raw.summary&&typeof raw.summary==='object')?raw.summary:{{}},points:clean,push_markers:pushMarkers}};}}
	function buildAccuracyExportBundle(){{if(!accuracyLivePayload||!accuracyView.points.length)throw new Error('暂无图表数据，请先刷新压测');const inst=(document.getElementById('accuracyInst')||{{}}).value||accuracyLivePayload.inst_id||'BTC-USDT-SWAP',h=String(configuredAccuracyHorizon()),scope=(document.getElementById('accuracyScope')||{{}}).value||accuracyLivePayload.scope||'session',retention=accuracyRetentionHours(),interval=configuredMonitorInterval();return{{name:'OKX_Accuracy_Chart',version:'1.0',exported_at:new Date().toISOString().slice(0,19).replace('T',' '),inst_id:inst,horizon_seconds:Number(h)||5,scope:scope,retention_hours:retention,interval_seconds:interval,max_points:accuracyLivePayload.max_points||accuracyView.points.length,start_at:accuracyLivePayload.start_at||'',summary:accuracyLivePayload.summary||{{}},points:accuracyView.points.slice(),push_markers:Array.isArray(accuracyLivePayload.push_markers)?accuracyLivePayload.push_markers.slice():[]}};}}
	function accuracyExportFileName(){{const d=new Date(),p=n=>String(n).padStart(2,'0');return 'okx_accuracy_chart_'+d.getFullYear()+p(d.getMonth()+1)+p(d.getDate())+'_'+p(d.getHours())+p(d.getMinutes())+p(d.getSeconds())+'.json';}}
	async function saveJsonFile(text,name,description){{if(window.showSaveFilePicker){{const handle=await showSaveFilePicker({{suggestedName:name,types:[{{description:description,accept:{{'application/json':['.json']}}}}]}});const writable=await handle.createWritable();await writable.write(text);await writable.close();return handle.name||name;}}const blob=new Blob([text],{{type:'application/json;charset=utf-8'}}),url=URL.createObjectURL(blob),link=document.createElement('a');link.href=url;link.download=name;link.click();URL.revokeObjectURL(url);return name;}}
	function applyImportedAccuracyBundle(bundle){{setAccuracyImportedMode(true,bundle.exported_at||bundle.start_at||'导入快照');accuracyLivePayload=Object.assign({{ok:true}},bundle);if(bundle.inst_id){{const instEl=document.getElementById('accuracyInst');if(instEl)instEl.value=bundle.inst_id;}}if(bundle.horizon_seconds){{const hEl=document.getElementById('accuracyHorizon');if(hEl)hEl.value=String(bundle.horizon_seconds);}}if(bundle.scope){{const sEl=document.getElementById('accuracyScope');if(sEl&&['session','all','imported'].indexOf(bundle.scope)>=0)sEl.value=bundle.scope==='imported'?'session':bundle.scope;}}if(bundle.retention_hours){{const rEl=document.getElementById('accuracyRetentionHours');if(rEl)rEl.value=String(bundle.retention_hours);}}accuracyQueryKey=accuracyQuerySignature();updateAccuracySummary(bundle.summary||{{}});syncAccuracyPoints(bundle.points||[],{{resetView:true}});redrawAccuracyChart();const note=document.getElementById('accuracyNote');if(note)note.textContent='导入快照 · '+accuracyImportedLabel+' · '+bundle.inst_id+' · '+bundle.points.length+' 点 · 窗口 '+bundle.horizon_seconds+' 秒 · 双击图表重置缩放 · 点「返回实时」恢复自动刷新';}}
	async function exportAccuracyChart(){{try{{const bundle=buildAccuracyExportBundle(),text=JSON.stringify(bundle,null,2),name=await saveJsonFile(text,accuracyExportFileName(),'OKX压测图表');const note=document.getElementById('accuracyNote');if(note)note.textContent='已导出 '+name+' · '+bundle.points.length+' 点 · 可在测试页「导入图表」回放';}}catch(e){{alert('导出图表失败：'+e);}}}}
	async function pickAccuracyImportFile(){{if(window.showOpenFilePicker){{const handles=await showOpenFilePicker({{multiple:false,types:[{{description:'OKX压测图表',accept:{{'application/json':['.json']}}}}]}});return await handles[0].getFile();}}return await new Promise(resolve=>{{const input=document.getElementById('accuracyImportInput');if(!input){{resolve(null);return;}}input.onchange=()=>resolve(input.files&&input.files[0]?input.files[0]:null);input.value='';input.click();}});}}
	async function importAccuracyChart(){{try{{const file=await pickAccuracyImportFile();if(!file)return;const bundle=normalizeAccuracyBundle(JSON.parse(await file.text()));setAccuracyEnabled(true,{{fetchNow:false}});applyImportedAccuracyBundle(bundle);}}catch(e){{alert('导入图表失败：'+e);}}}}
	function exitAccuracyImportedMode(){{setAccuracyImportedMode(false,'');fetchAccuracy({{resetView:true}});}}
	function clamp(v,a,b){{return Math.max(a,Math.min(b,v));}}
	function accuracyDirectionFromText(v){{if(v==='\\u505a\\u591a')return 1;if(v==='\\u505a\\u7a7a')return -1;return 0;}}
	function accuracyConfirmDirection(o){{return String((o&&(o.confirm_direction||o.final_direction||o.direction))||'');}}
	function accuracyPredictionDirection(o){{return String((o&&(o.prediction_direction||o.raw_direction))||'');}}
	function accuracyDirectionValue(o){{return accuracyDirectionFromText(String((o&&o.direction)||''));}}
	function accuracyConfirmValue(o){{return accuracyDirectionFromText(accuracyConfirmDirection(o));}}
	function accuracyPredictionValue(o){{return accuracyDirectionFromText(accuracyPredictionDirection(o));}}
	function resetAccuracyView(){{accuracyView.start=0;accuracyView.end=1;accuracyView.yZoom=1;accuracyView.yPan=0;accuracyView.priceRg=1;accuracyView.followLatest=true;accuracyView.selectedKey='';updateAccuracyPointPanel(null);}}
	function accuracyIsFullView(){{return accuracyView.start<=0.001&&accuracyView.end>=0.999&&Math.abs((accuracyView.yZoom||1)-1)<0.05&&Math.abs(accuracyView.yPan||0)<0.05;}}
	function syncAccuracyFollowLatestFlag(){{accuracyView.followLatest=accuracyIsFullView();}}
	function accuracyPointHtml(o){{if(!o)return '';const verified=!!o.verified;const hit=verified?(o.hit?'合理':'不合理'):'未验证',predDir=accuracyPredictionDirection(o)||'--',confirmDir=accuracyConfirmDirection(o)||'--',actual=verified?(o.actual_direction||'--'):'--',ret=verified&&o.return_pct!=null?fmt(o.return_pct,3)+'%':'--',outcome=verified?(o.outcome_type||'--'):'未验证',paper=o.paper_equity!=null?('$'+fmt(o.paper_equity,0)+' / '+fmtSignedPct(o.paper_pnl_pct,2)+' / '+(o.paper_position||'--')):'--',pushBlock=o.would_push?('<span>应推送</span><b>'+escHtml(o.push_label||o.push_kind||'--')+' · '+escHtml(o.push_direction||'--')+'</b>'):'',aiBlock=(o.ai_forward_direction?('<span>AI前瞻</span><b>'+escHtml(o.ai_forward_direction)+' · '+(o.ai_forward_horizon_minutes||'--')+'m · P'+(o.ai_forward_probability??'--')+' · '+(verified&&o.ai_forward_hit!=null?(o.ai_forward_hit?'命中':'未中'):'未验证')+'</b>'):''),predSrc=o.prediction_source?('<span>预测来源</span><b>'+escHtml(o.prediction_source)+'</b>'):'',leadBlock=(predDir!==confirmDir&&predDir!=='--'&&confirmDir!=='--')?('<span>先后</span><b>预测 '+escHtml(predDir)+' · 确认 '+escHtml(confirmDir)+'</b>'):'';return '<strong>选中压测点</strong><div class="snapshot-grid"><span>时间</span><b>'+escHtml(o.time||'--')+'</b><span>价格</span><b>'+fmt(o.price,2)+'</b><span>预测方向</span><b>'+escHtml(predDir)+'</b><span>确认方向</span><b>'+escHtml(confirmDir)+'</b>'+predSrc+leadBlock+'<span>实际</span><b>'+actual+'</b>'+aiBlock+'<span>模拟账户</span><b>'+paper+'</b><span>后续价</span><b>'+(o.future_price!=null?fmt(o.future_price,2):'--')+'</b><span>涨跌</span><b>'+ret+'</b><span>判定</span><b>'+hit+'</b><span>类型</span><b>'+escHtml(outcome)+'</b>'+pushBlock+'<span>累计准确</span><b>'+(o.accuracy_pct!=null?fmt(o.accuracy_pct,1)+'%':'--')+'</b></div>';}}
	function escHtml(v){{return String(v==null?'':v).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}}
	function updateAccuracyPointPanel(point){{const panel=document.getElementById('accuracyPointPanel');if(!panel)return;if(!point){{panel.hidden=true;panel.innerHTML='';return;}}panel.hidden=false;panel.innerHTML=accuracyPointHtml(point);}}
	function drawAccuracyLeftAxis(ctx,pad,W,H,mn,mx){{ctx.fillStyle='rgba(226,232,240,.82)';ctx.font='12px Segoe UI, Microsoft YaHei';ctx.textAlign='right';ctx.textBaseline='middle';const ch=H-pad.t-pad.b;for(let i=0;i<=4;i++){{const value=mx-(mx-mn)*i/4,y=pad.t+ch*i/4;ctx.fillText(fmt(value,2),pad.l-8,y);}}}}
	function drawAccuracyRightAxis(ctx,pad,W,H,mn,mx){{ctx.fillStyle='rgba(251,191,36,.82)';ctx.font='12px Segoe UI, Microsoft YaHei';ctx.textAlign='left';ctx.textBaseline='middle';const ch=H-pad.t-pad.b;for(let i=0;i<=4;i++){{const value=mx-(mx-mn)*i/4,y=pad.t+ch*i/4;ctx.fillText(fmt(value,0),W-pad.r+6,y);}}}}
	function drawAccuracyCrosshair(ctx,pad,W,H,plot){{const pt=plot.point||{{}};ctx.save();ctx.strokeStyle='rgba(226,232,240,.72)';ctx.setLineDash([5,5]);ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(plot.x,pad.t);ctx.lineTo(plot.x,H-pad.b);ctx.stroke();ctx.beginPath();ctx.moveTo(pad.l,plot.yPrice);ctx.lineTo(W-pad.r,plot.yPrice);ctx.stroke();ctx.strokeStyle='rgba(167,139,250,.55)';ctx.beginPath();ctx.moveTo(pad.l,plot.yForecast);ctx.lineTo(W-pad.r,plot.yForecast);ctx.stroke();ctx.strokeStyle='rgba(251,191,36,.55)';ctx.beginPath();ctx.moveTo(pad.l,plot.yConfirm);ctx.lineTo(W-pad.r,plot.yConfirm);ctx.stroke();ctx.setLineDash([]);ctx.font='12px Segoe UI, Microsoft YaHei';ctx.fillStyle='rgba(96,165,250,.95)';ctx.textAlign='right';ctx.textBaseline='middle';ctx.fillText(fmt(pt.price,2),pad.l-8,plot.yPrice);ctx.fillStyle='rgba(167,139,250,.95)';ctx.textAlign='left';ctx.textBaseline='bottom';ctx.fillText('预 '+fmt(plot.forecastVal!=null?plot.forecastVal:accuracyPredictionValue(pt),0),W-pad.r+6,plot.yForecast-2);ctx.fillStyle='rgba(251,191,36,.95)';ctx.textBaseline='top';ctx.fillText('确 '+fmt(plot.confirmVal!=null?plot.confirmVal:accuracyConfirmValue(pt),0),W-pad.r+6,plot.yConfirm+2);ctx.fillStyle='rgba(229,231,235,.95)';ctx.textAlign='center';ctx.textBaseline='top';ctx.fillText(shortTime(pt.time||''),plot.x,H-pad.b+4);ctx.fillStyle='#60a5fa';ctx.beginPath();ctx.arc(plot.x,plot.yPrice,5,0,Math.PI*2);ctx.fill();ctx.fillStyle='#a78bfa';ctx.beginPath();ctx.arc(plot.x,plot.yForecast,4.5,0,Math.PI*2);ctx.fill();ctx.fillStyle='#fbbf24';ctx.beginPath();ctx.arc(plot.x,plot.yConfirm,4.5,0,Math.PI*2);ctx.fill();ctx.restore();}}
	function selectAccuracyPoint(event,strict){{const c=document.getElementById('accuracyChart');if(!c||!accuracyPlotPoints.length)return;const rect=c.getBoundingClientRect(),x=event.clientX-rect.left,y=event.clientY-rect.top;let best=null,bestDist=Infinity;accuracyPlotPoints.forEach(o=>{{const dPrice=Math.hypot(o.x-x,o.yPrice-y),dConfirm=Math.hypot(o.x-x,o.yConfirm-y),dForecast=Math.hypot(o.x-x,o.yForecast-y),dist=Math.min(dPrice,dConfirm,dForecast);if(dist<bestDist){{best=o;bestDist=dist;}}}});const limit=strict?36:30;if(best&&bestDist<limit){{accuracyView.selectedKey=best.point.time||'';updateAccuracyPointPanel(best.point);drawAccuracyChart();}}else if(!strict){{accuracyView.selectedKey='';updateAccuracyPointPanel(null);drawAccuracyChart();}}}}
	function visibleAccuracyPoints(){{const pts=accuracyView.points||[];if(pts.length<=1)return pts;const n=pts.length,span=Math.max(0.001,accuracyView.end-accuracyView.start),a=Math.floor(accuracyView.start*(n-1)),b=Math.min(n,Math.max(a+2,Math.ceil((accuracyView.start+span)*(n-1))+1));return pts.slice(a,b);}}
	function accuracyLinePointBudget(cw,count){{if(count<=160)return count;const cap=Math.max(120,Math.floor(cw*1.2));return Math.min(count,cap);}}
	function accuracyShowPointMarkers(cw,count){{if(count<=80)return true;const budget=accuracyLinePointBudget(cw,count);return count<=Math.min(100,budget);}}
	function decimateAccuracySeries(points,confirmVals,forecastVals,maxPoints){{if(points.length<=maxPoints)return{{points:points,confirmVals:confirmVals,forecastVals:forecastVals}};const idx=new Set([0,points.length-1]);const buckets=Math.max(1,Math.floor((maxPoints-2)/2));for(let b=0;b<buckets;b++){{const start=Math.floor(b*(points.length-1)/buckets),end=Math.min(points.length-1,Math.floor((b+1)*(points.length-1)/buckets));if(start>=end){{idx.add(start);continue;}}let minI=start,maxI=start;for(let i=start;i<=end;i++){{const p=Number(points[i].price);if(p<Number(points[minI].price))minI=i;if(p>Number(points[maxI].price))maxI=i;}}idx.add(minI);idx.add(maxI);}}let order=Array.from(idx).sort((a,b)=>a-b);if(order.length>maxPoints){{const slim=[];for(let i=0;i<maxPoints;i++)slim.push(order[Math.round(i*(order.length-1)/(maxPoints-1))]);order=slim;}}return{{points:order.map(i=>points[i]),confirmVals:order.map(i=>confirmVals[i]),forecastVals:order.map(i=>forecastVals[i])}};}}
	function accuracyMinTimeSpan(){{const n=(accuracyView.points||[]).length;return n<=1?1:Math.max(0.02,Math.min(1,36/Math.max(36,n)));}}
	function accuracySpanHours(points){{if(!points||points.length<2)return 0;const a=parsePointTime(points[0].time).getTime(),b=parsePointTime(points[points.length-1].time).getTime();return Math.abs(b-a)/3600000;}}
	function accuracyTimeLabel(t,spanHours){{if(spanHours>=4)return compactTime(t);return shortTime(t);}}
	function pushKindStyle(kind){{const k=String(kind||'').split('+')[0];if(k==='spike')return{{fill:'#fb923c',stroke:'#7c2d12'}};if(k==='watch')return{{fill:'#22d3ee',stroke:'#155e75'}};if(k==='forecast')return{{fill:'#c084fc',stroke:'#581c87'}};if(k==='trade')return{{fill:'#4ade80',stroke:'#166534'}};return{{fill:'#f472b6',stroke:'#831843'}};}}
	function drawPushMarker(ctx,x,y,kind,direction){{const st=pushKindStyle(kind),r=6.5;ctx.save();ctx.lineWidth=1.4;ctx.strokeStyle=st.stroke;ctx.fillStyle=st.fill;const k=String(kind||'').split('+')[0];if(k==='watch'){{ctx.fillRect(x-r,y-r,r*2,r*2);ctx.strokeRect(x-r,y-r,r*2,r*2);}}else if(k==='forecast'){{ctx.beginPath();ctx.moveTo(x,y-r-1);ctx.lineTo(x+r+1,y+r);ctx.lineTo(x-r-1,y+r);ctx.closePath();ctx.fill();ctx.stroke();}}else if(k==='trade'){{ctx.beginPath();for(let i=0;i<5;i++){{const a=-Math.PI/2+i*Math.PI*2/5,b=-Math.PI/2+(i+2)*Math.PI*2/5;ctx.lineTo(x+Math.cos(a)*r,y+Math.sin(a)*r);ctx.lineTo(x+Math.cos(b)*r*0.45,y+Math.sin(b)*r*0.45);}}ctx.closePath();ctx.fill();ctx.stroke();}}else{{ctx.beginPath();ctx.moveTo(x,y-r-1);ctx.lineTo(x+r+1,y);ctx.lineTo(x,y+r+1);ctx.lineTo(x-r-1,y);ctx.closePath();ctx.fill();ctx.stroke();}}const dir=String(direction||'');if(dir==='\\u505a\\u591a'||dir==='\\u505a\\u7a7a'){{ctx.fillStyle=st.fill;ctx.beginPath();if(dir==='\\u505a\\u591a'){{ctx.moveTo(x,y-r-11);ctx.lineTo(x-4,y-r-5);ctx.lineTo(x+4,y-r-5);}}else{{ctx.moveTo(x,y+r+11);ctx.lineTo(x-4,y+r+5);ctx.lineTo(x+4,y+r+5);}}ctx.closePath();ctx.fill();}}ctx.restore();}}
	function drawAccuracyStandalonePushMarkers(ctx,clean,xAtFull,priceY){{const markers=(accuracyLivePayload&&Array.isArray(accuracyLivePayload.push_markers))?accuracyLivePayload.push_markers:[];if(!markers.length||!clean.length)return;const pointPushTimes=new Set(clean.filter(o=>o&&o.would_push).map(o=>String(o.time||''))),times=clean.map(o=>parsePointTime(o.time).getTime()),first=times[0],last=times[times.length-1],stacks={{}};markers.forEach(m=>{{const key=String(m.time||'');if(!key||pointPushTimes.has(key))return;const mt=parsePointTime(key).getTime();if(!Number.isFinite(mt)||mt<first||mt>last)return;let hi=times.findIndex(t=>t>=mt);if(hi<0)hi=times.length-1;let pos=hi;if(hi>0&&times[hi]!==mt){{const lo=hi-1,span=Math.max(1,times[hi]-times[lo]);pos=lo+(mt-times[lo])/span;}}const x=xAtFull(pos),stack=stacks[Math.round(x)]||0,y=priceY(m)-16-stack*15;stacks[Math.round(x)]=stack+1;drawPushMarker(ctx,x,y,m.push_kind||'spike',m.push_direction||'');}});}}
	function syncAccuracyPoints(points,options){{const opts=options||{{}},clean=(points||[]).filter(o=>Number.isFinite(Number(o&&o.price)));if(opts.resetView){{accuracyView.points=clean;resetAccuracyView();return;}}const prevSpan=Math.max(0.001,accuracyView.end-accuracyView.start);accuracyView.points=clean;if(clean.length<=2)return;if(accuracyView.followLatest){{accuracyView.end=1;accuracyView.start=Math.max(0,1-prevSpan);}}}}
	function redrawAccuracyChart(){{drawAccuracyChart();}}
	async function fetchAccuracy(options){{options=options||{{}};if(!isAccuracyEnabled())return null;if(fetchAccuracyInFlight&&!options.force)return fetchAccuracyInFlight;const run=async()=>{{const canvas=document.getElementById('accuracyChart');if(!canvas)return;const resetView=!!options.resetView||accuracyQuerySignature()!==accuracyQueryKey;if(accuracyImportedMode&&!options.resetView)return;if(options.resetView)setAccuracyImportedMode(false,'');const inst=(document.getElementById('accuracyInst')||{{}}).value||'BTC-USDT-SWAP',h=String(configuredAccuracyHorizon()),scope=(document.getElementById('accuracyScope')||{{}}).value||'session',retention=accuracyRetentionHours(),interval=configuredMonitorInterval(),note=document.getElementById('accuracyNote');const queryKey=accuracyQuerySignature();accuracyQueryKey=queryKey;try{{if(note&&resetView)note.textContent='正在统计实时预测压测...';const qs='inst_id='+encodeURIComponent(inst)+'&horizon='+encodeURIComponent(h)+'&scope='+encodeURIComponent(scope)+'&retention_hours='+encodeURIComponent(retention)+'&interval_seconds='+encodeURIComponent(interval);const r=await fetch('/api/accuracy-data?'+qs,{{cache:'no-store'}}),p=await r.json();if(!r.ok||p.ok===false)throw new Error(p.error||'统计失败');accuracyLivePayload=p;const s=p.summary||{{}};updateAccuracySummary(s);syncAccuracyPoints(p.points||[],{{resetView:resetView}});redrawAccuracyChart();if(p.replay_pending){{if(note)note.textContent=p.hint||'请先点击下方「开始回放」；切换「回放会话」不会自动启动回放。';return;}}const maxPts=p.max_points||0,intervalSec=p.interval_seconds||interval,retainH=p.retention_hours||retention;let noteExtra='';if(p.scope==='replay'&&(s.total??0)===0&&((s.raw_log_total??0)>0||(s.pending_total??0)>0))noteExtra=' · 回放日志已有数据，验证窗口成熟后曲线才会加点';else if(p.scope==='replay'&&(s.raw_log_total??0)===0)noteExtra=' · 回放进行中，请稍候';else if(p.scope==='session'&&(s.raw_log_total??0)===0)noteExtra=' · 监控刚启动，请等待几轮轮询';else if((s.pending_total??0)>0&&(s.next_pending_seconds??0)>0)noteExtra=' · 还有 '+s.pending_total+' 条待验证，最近约 '+s.next_pending_seconds+' 秒后可加点';else if((s.pending_total??0)>0)noteExtra=' · 还有 '+s.pending_total+' 条待验证，点「刷新压测」即可尝试更新';if(note)note.textContent=(p.scope==='replay'?'回放会话':p.scope==='session'?'本次启动后':'全部历史')+' · 模拟 '+formatPaperSummary(s)+' · 综合 '+fmt(s.prediction_accuracy_pct!=null?s.prediction_accuracy_pct:s.decision_accuracy_pct,1)+'% · AI前瞻 '+((s.ai_forward_direction_total??0)>0?(fmt(s.ai_forward_direction_accuracy_pct,1)+'%/'+s.ai_forward_direction_total+'@'+(s.ai_forward_horizon_minutes||15)+'m'):((s.ai_invoked_total??0)>0?'待验证':'--'))+' · 窗口 '+formatHorizonLabel(p.horizon_seconds||h)+((p.time_start&&p.time_end)?(' · 范围 '+compactTime(p.time_start)+' ~ '+compactTime(p.time_end)):'')+' · '+(p.chart_points||accuracyView.points.length||0)+'点 · 双击重置缩放'+((s.push_would_total??0)>0?(' · 应推送 '+s.push_would_total+' 帧（价线上方标记）'):'')+noteExtra;}}catch(e){{updateAccuracySummary({{}});syncAccuracyPoints([],{{resetView:true}});redrawAccuracyChart();if(note)note.textContent='预测压测统计失败：'+e;}}}};fetchAccuracyInFlight=run();try{{await fetchAccuracyInFlight;}}finally{{fetchAccuracyInFlight=null;}}}}
	function switchAccuracyToLiveSession(){{const scopeEl=document.getElementById('accuracyScope');if(scopeEl&&scopeEl.value!=='session'){{accuracyScopeSyncing=true;scopeEl.value='session';accuracyScopeSyncing=false;}}setAccuracyImportedMode(false,'');accuracyQueryKey='';if(isAccuracyEnabled())fetchAccuracy({{resetView:true}});}}
	function switchAccuracyToReplaySession(){{const scopeEl=document.getElementById('accuracyScope');if(scopeEl&&scopeEl.value!=='replay'){{accuracyScopeSyncing=true;scopeEl.value='replay';accuracyScopeSyncing=false;}}setAccuracyImportedMode(false,'');accuracyQueryKey='';}}
	function syncAccuracyScopeWithReplay(info){{if(!info||!info.replay_running)return false;const scopeEl=document.getElementById('accuracyScope');if(!scopeEl||scopeEl.value==='replay')return false;switchAccuracyToReplaySession();return true;}}
	async function onAccuracyScopeChange(){{if(accuracyScopeSyncing)return;const scopeEl=document.getElementById('accuracyScope');if(scopeEl&&scopeEl.value!=='replay')stopReplayAccuracyPoll();if(scopeEl&&scopeEl.value==='replay'){{let monitorRunning=false;try{{const st=await fetch('/api/status',{{cache:'no-store'}}).then(r=>r.json());monitorRunning=!!(st&&st.running);}}catch(e){{}}if(monitorRunning){{alert('监控运行中无法查看回放压测，请先停止监控');accuracyScopeSyncing=true;scopeEl.value='session';accuracyScopeSyncing=false;}}}}await fetchAccuracy({{resetView:true}});syncReplayAccuracyPoll();}}
	if(accuracyRefreshBtn){{accuracyRefreshBtn.addEventListener('click',()=>fetchAccuracy({{resetView:true}}));}}
	const accuracyExportBtn=document.getElementById('accuracyExportBtn'),accuracyImportBtn=document.getElementById('accuracyImportBtn'),accuracyLiveBtn=document.getElementById('accuracyLiveBtn');
	if(accuracyExportBtn)accuracyExportBtn.addEventListener('click',exportAccuracyChart);
	if(accuracyImportBtn)accuracyImportBtn.addEventListener('click',importAccuracyChart);
	if(accuracyLiveBtn)accuracyLiveBtn.addEventListener('click',exitAccuracyImportedMode);
	['accuracyInst','accuracyHorizon','accuracyRetentionHours'].forEach(id=>{{const el=document.getElementById(id);if(el)el.addEventListener('change',()=>fetchAccuracy({{resetView:true}}));}});
	const accuracyScopeEl=document.getElementById('accuracyScope');if(accuracyScopeEl)accuracyScopeEl.addEventListener('change',onAccuracyScopeChange);
	function formatHorizonLabel(sec){{const n=Number(sec)||0;if(n>=60&&n%60===0)return (n/60)+'分钟';return n+'秒';}}
	function formatPaperSummary(s){{if(!s||s.paper_equity==null)return '--';const pnl=Number(s.paper_pnl_usd)||0,pct=Number(s.paper_pnl_pct)||0,sign=pnl>=0?'+':'';return '$'+fmt(s.paper_equity,0)+' ('+sign+fmt(pnl,0)+' / '+sign+fmt(pct,2)+'%) · '+(s.paper_position_label||'--');}}
	function updateAccuracySummary(s){{const box=document.getElementById('accuracySummary');if(!box)return;s=s||{{}};const vals=[['分析次数',String(s.analysis_total??s.raw_log_total??0)],['AI调用次数',String(s.ai_call_total??0)],['Token总消耗',formatTokenCount(s.ai_token_total??0)]];box.innerHTML=vals.map(v=>'<div class="accuracy-primary"><span>'+v[0]+'</span><b>'+v[1]+'</b></div>').join('');}}
	function drawAccuracyChart(points){{if(Array.isArray(points))syncAccuracyPoints(points,{{resetView:true}});const c=document.getElementById('accuracyChart');if(!c)return;accuracyPlotPoints=[];const d=window.devicePixelRatio||1,r=c.getBoundingClientRect();c.width=Math.max(1,r.width*d);c.height=Math.max(1,r.height*d);const ctx=c.getContext('2d');ctx.setTransform(d,0,0,d,0,0);const W=r.width,H=r.height,pad={{l:58,r:52,t:28,b:40}},cw=W-pad.l-pad.r,ch=H-pad.t-pad.b;ctx.clearRect(0,0,W,H);ctx.fillStyle='#0f172a';ctx.fillRect(0,0,W,H);let clean=visibleAccuracyPoints();if(!clean.length){{updateAccuracyPointPanel(null);ctx.fillStyle='rgba(203,213,225,.82)';ctx.textAlign='center';ctx.textBaseline='middle';ctx.font='13px Segoe UI, Microsoft YaHei';const scopeNow=(document.getElementById('accuracyScope')||{{}}).value||'session',emptyMsg=scopeNow==='replay'?'回放会话暂无数据：请先点击下方「开始回放」，切换范围不会自动启动回放':(scopeNow==='all'?'全部历史暂无压测点：请确认日志中有该币种数据':'暂无压测样本：请先启动监控或选择有日志的范围');ctx.fillText(emptyMsg,W/2,H/2);return;}}const visibleCount=clean.length,totalCount=(accuracyView.points||[]).length;if(clean.length===1)clean=[clean[0],Object.assign({{}},clean[0])];const priceVals=clean.map(o=>Number(o.price)),mn=Math.min(...priceVals),mx=Math.max(...priceVals),basePriceRg=Math.max((mx||1)*0.001,(mx-mn)*1.16,0.01);let confirmAcc=0,forecastAcc=0;const confirmVals=clean.map(o=>{{confirmAcc+=accuracyConfirmValue(o);return confirmAcc;}}),forecastVals=clean.map(o=>{{forecastAcc+=accuracyPredictionValue(o);return forecastAcc;}}),dirMn=Math.min(...confirmVals,...forecastVals),dirMx=Math.max(...confirmVals,...forecastVals),baseDirRg=Math.max(1,(dirMx-dirMn)*1.16,1);const yZoom=Math.max(0.35,accuracyView.yZoom||1),yPan=accuracyView.yPan||0,normTop=0.5+yPan+0.5/yZoom,normBottom=0.5+yPan-0.5/yZoom,priceSpan=Math.max(0.000001,basePriceRg),dirSpan=Math.max(0.000001,baseDirRg),priceAxisBottom=mn+normBottom*priceSpan,priceAxisTop=mn+normTop*priceSpan,dirAxisBottom=dirMn+normBottom*dirSpan,dirAxisTop=dirMn+normTop*dirSpan;accuracyView.priceRg=priceSpan/yZoom;const pricePlotRg=Math.max(0.000001,priceAxisTop-priceAxisBottom),dirPlotRg=Math.max(0.000001,dirAxisTop-dirAxisBottom),priceFlat=Math.abs(mx-mn)<1e-9,dirFlat=Math.abs(dirMx-dirMn)<1e-9,bothFlat=priceFlat&&dirFlat,priceY=o=>{{let norm=priceFlat?0.5:(Number(o.price)-priceAxisBottom)/pricePlotRg;return pad.t+ch-norm*ch-(bothFlat?16:0);}},dirValY=val=>{{let norm=dirFlat?0.5:(val-dirAxisBottom)/dirPlotRg;return pad.t+ch-norm*ch+(bothFlat?16:0);}},stepFull=cw/Math.max(1,clean.length-1),xAtFull=i=>pad.l+stepFull*i;const drawBudget=accuracyLinePointBudget(cw,clean.length),showMarkers=accuracyShowPointMarkers(cw,clean.length),decimated=decimateAccuracySeries(clean,confirmVals,forecastVals,drawBudget),drawPts=decimated.points,drawConfirm=decimated.confirmVals,drawForecast=decimated.forecastVals,stepDraw=cw/Math.max(1,drawPts.length-1),xAtDraw=i=>pad.l+stepDraw*i,denseMode=drawPts.length<clean.length,priceLineWidth=denseMode?1.5:2.6,dirLineWidth=denseMode?1.1:2;ctx.strokeStyle='rgba(148,163,184,.22)';ctx.lineWidth=1;for(let i=0;i<=4;i++){{const y=pad.t+ch*i/4;ctx.beginPath();ctx.moveTo(pad.l,y);ctx.lineTo(W-pad.r,y);ctx.stroke();}}drawAccuracyLeftAxis(ctx,pad,W,H,priceAxisBottom,priceAxisTop);drawAccuracyRightAxis(ctx,pad,W,H,dirAxisBottom,dirAxisTop);drawAccuracyTimeAxis(ctx,W,H,pad,clean,cw);ctx.beginPath();drawPts.forEach((o,i)=>{{const px=xAtDraw(i),py=dirValY(drawForecast[i]);if(i===0)ctx.moveTo(px,py);else ctx.lineTo(px,py);}});ctx.strokeStyle='rgba(167,139,250,.92)';ctx.lineWidth=dirLineWidth;ctx.stroke();ctx.beginPath();drawPts.forEach((o,i)=>{{const px=xAtDraw(i),py=dirValY(drawConfirm[i]);if(i===0)ctx.moveTo(px,py);else ctx.lineTo(px,py);}});ctx.strokeStyle='rgba(251,191,36,.92)';ctx.lineWidth=dirLineWidth;ctx.stroke();ctx.beginPath();drawPts.forEach((o,i)=>{{const px=xAtDraw(i),py=priceY(o);if(i===0)ctx.moveTo(px,py);else ctx.lineTo(px,py);}});ctx.strokeStyle='rgba(96,165,250,.95)';ctx.lineWidth=priceLineWidth;ctx.stroke();const paperEq=clean.map(o=>Number(o.paper_equity)).filter(v=>Number.isFinite(v));if(paperEq.length>=2){{const pMn=Math.min(...paperEq),pMx=Math.max(...paperEq),pRg=Math.max(0.01,pMx-pMn),paperY=v=>pad.t+ch-((v-pMn)/pRg)*ch;let started=false;ctx.beginPath();clean.forEach((o,i)=>{{const pe=Number(o.paper_equity);if(!Number.isFinite(pe))return;const px=xAtFull(i),py=paperY(pe);if(!started){{ctx.moveTo(px,py);started=true;}}else ctx.lineTo(px,py);}});if(started){{ctx.strokeStyle='rgba(74,222,128,.92)';ctx.lineWidth=denseMode?1.2:2;ctx.stroke();}}}}const markerPriceRadius=showMarkers?(visibleCount>160?1.6:2.8):0,markerDirRadius=showMarkers?(visibleCount>160?1.4:2.4):0;clean.forEach((o,i)=>{{const px=xAtFull(i),pyPrice=priceY(o),pyConfirm=dirValY(confirmVals[i]),pyForecast=dirValY(forecastVals[i]),selected=(o.time||'')===accuracyView.selectedKey;accuracyPlotPoints.push({{x:px,yPrice:pyPrice,yConfirm:pyConfirm,yForecast:pyForecast,confirmVal:confirmVals[i],forecastVal:forecastVals[i],point:o}});if(o.would_push)drawPushMarker(ctx,px,pyPrice-16,o.push_kind||'spike',o.push_direction||'');if(!showMarkers&&!selected)return;const pr=selected?4.2:(markerPriceRadius||2.8),cr=selected?3.8:(markerDirRadius||2.4),fr=selected?3.6:(markerDirRadius||2.2);ctx.lineWidth=selected?1.5:1;ctx.strokeStyle='rgba(15,23,42,.85)';ctx.fillStyle=selected?'#22d3ee':(!o.verified?'#94a3b8':(o.hit?'#34d399':'#fb7185'));ctx.beginPath();ctx.arc(px,pyPrice,pr,0,Math.PI*2);ctx.fill();ctx.stroke();ctx.fillStyle=selected?'#ddd6fe':'#a78bfa';ctx.beginPath();ctx.arc(px,pyForecast,fr,0,Math.PI*2);ctx.fill();ctx.stroke();ctx.fillStyle=selected?'#fde047':'#fbbf24';ctx.beginPath();ctx.arc(px,pyConfirm,cr,0,Math.PI*2);ctx.fill();ctx.stroke();}});drawAccuracyStandalonePushMarkers(ctx,clean,xAtFull,priceY);let selectedPlot=null;if(accuracyView.selectedKey){{selectedPlot=accuracyPlotPoints.find(o=>(o.point.time||'')===accuracyView.selectedKey)||null;if(selectedPlot){{drawAccuracyCrosshair(ctx,pad,W,H,selectedPlot);updateAccuracyPointPanel(selectedPlot.point);}}else{{accuracyView.selectedKey='';updateAccuracyPointPanel(null);}}}}else{{updateAccuracyPointPanel(null);}}ctx.fillStyle='rgba(226,232,240,.92)';ctx.font='12px Segoe UI, Microsoft YaHei';ctx.textAlign='left';ctx.textBaseline='top';ctx.fillText('蓝线：价格',pad.l,pad.t+4);ctx.fillStyle='#4ade80';ctx.fillText('绿线：模拟账户',pad.l+72,pad.t+4);ctx.fillStyle='#a78bfa';ctx.fillText('紫线：预测方向',pad.l+168,pad.t+4);ctx.fillStyle='#fbbf24';ctx.fillText('黄线：确认方向',pad.l+268,pad.t+4);ctx.fillStyle='rgba(226,232,240,.78)';ctx.font='11px Segoe UI, Microsoft YaHei';ctx.fillText('紫先黄后=预测领先确认 · 灰点=未验证 · 应推送 ◆急变 ■观察 △演变 ★结构单',pad.l,pad.t+20);ctx.fillStyle=denseMode?'#fcd34d':'rgba(203,213,225,.72)';ctx.font='12px Segoe UI, Microsoft YaHei';ctx.fillText(' · 绘制 '+drawPts.length+'/'+visibleCount+(denseMode?' 已抽稀':' 全量'),pad.l+420,pad.t+4);ctx.textAlign='right';ctx.fillStyle='rgba(203,213,225,.68)';ctx.textBaseline='top';ctx.fillText('总计 '+totalCount+' 点 · 滚轮缩放 · 拖动平移 · 点击选点',W-pad.r,H-26);}}
	function setupAccuracyChartInteractions(){{const c=document.getElementById('accuracyChart');if(!c||c.dataset.panZoomBound)return;c.dataset.panZoomBound='1';c.addEventListener('wheel',e=>{{if(!accuracyView.points.length)return;e.preventDefault();const rect=c.getBoundingClientRect(),mx=(e.clientX-rect.left)/Math.max(1,rect.width),factor=e.deltaY>0?1.18:0.85,n=accuracyView.points.length;if(e.shiftKey||n<=2){{accuracyView.yZoom=clamp(accuracyView.yZoom/factor,0.35,12);}}else{{const span=accuracyView.end-accuracyView.start,newSpan=clamp(span*factor,accuracyMinTimeSpan(),1),anchor=accuracyView.start+span*mx;accuracyView.start=clamp(anchor-newSpan*mx,0,1-newSpan);accuracyView.end=accuracyView.start+newSpan;}}syncAccuracyFollowLatestFlag();drawAccuracyChart();}},{{passive:false}});c.addEventListener('mousedown',e=>{{accuracyView.drag={{x:e.clientX,y:e.clientY,start:accuracyView.start,end:accuracyView.end,yPan:accuracyView.yPan,moved:false}};c.classList.add('dragging');}});window.addEventListener('mousemove',e=>{{const g=accuracyView.drag;if(!g)return;const rect=c.getBoundingClientRect(),dx=(e.clientX-g.x)/Math.max(1,rect.width),dy=(e.clientY-g.y)/Math.max(1,rect.height),span=g.end-g.start;if(Math.abs(e.clientX-g.x)>3||Math.abs(e.clientY-g.y)>3)g.moved=true;let ns=clamp(g.start-dx*span,0,1-span),ne=ns+span;accuracyView.start=ns;accuracyView.end=ne;accuracyView.yPan=clamp(g.yPan+dy*1.35,-3,3);syncAccuracyFollowLatestFlag();drawAccuracyChart();}});window.addEventListener('mouseup',()=>{{if(accuracyView.drag){{setTimeout(function(){{accuracyView.drag=null;}},0);}}c.classList.remove('dragging');}});c.addEventListener('click',e=>{{if(accuracyView.drag&&accuracyView.drag.moved)return;selectAccuracyPoint(e,false);}});c.addEventListener('dblclick',e=>{{if(accuracyView.drag&&accuracyView.drag.moved)return;resetAccuracyView();drawAccuracyChart();}});}}
	setupAccuracyChartInteractions();
	let virtualTick=0;
let configuredMonitorInsts={json.dumps(selected_instruments, ensure_ascii=False)};
let monitorInst={json.dumps(monitor_initial, ensure_ascii=False)}, monitorPayload=null, monitorLiveMode=false;
let monitorSeriesByInst={{}}, monitorLastTickerAt=0;
let virtualSeriesByInst={{}};
let monitorViewStart=0, monitorViewEnd=1;
let monitorVisiblePoints=[], monitorPlotPoints=[], monitorSelectedKey='', monitorLatestPoint=null, monitorLatestSnapshot=null;
let monitorYZoom=1, monitorYPan=0, monitorYRange=1, monitorDrag=null;
let monitorStartedAt='', monitorElapsedSeconds=0, monitorStatusRunning=false, monitorWasRunning=false;
let monitorBar='1m', monitorCandlesByKey={{}}, monitorMetaText='';
function monitorCacheKey(inst,bar){{return (inst||'')+'|'+(bar||'1m');}}
function bindMonitorBarTabs(){{document.querySelectorAll('[data-monitor-bar]').forEach(function(btn){{btn.onclick=function(){{const next=btn.getAttribute('data-monitor-bar');if(!next||next===monitorBar)return;monitorBar=next;document.querySelectorAll('[data-monitor-bar]').forEach(function(el){{el.classList.toggle('active',el.getAttribute('data-monitor-bar')===monitorBar);}});monitorViewStart=0;monitorViewEnd=1;monitorYZoom=1;monitorYPan=0;monitorSelectedKey='';monitorLastTickerAt=0;if(isMonitorChartEnabled())bootstrapMonitorChart();}};}});}}
const MONITOR_CHART_KEY='okx_monitor_chart_enabled';
function isMonitorChartEnabled(){{const el=document.getElementById('monitorChartEnableToggle');return!!(el&&el.checked);}}
function showMonitorChartOffState(){{const panel=document.getElementById('monitorChartPanel'),offHint=document.getElementById('monitorChartOffHint'),c=document.getElementById('monitorChart'),l=document.getElementById('monitorLoading'),t=document.getElementById('monitorTitle'),m=document.getElementById('monitorMeta'),p=document.getElementById('monitorPrice');if(panel)panel.classList.add('monitor-chart-off');if(offHint)offHint.hidden=false;monitorSelectedKey='';monitorVisiblePoints=[];monitorPlotPoints=[];if(c){{const r=c.getBoundingClientRect(),x=c.getContext('2d');c.width=Math.max(1,r.width);c.height=Math.max(1,r.height);x.clearRect(0,0,r.width,r.height);}}if(l){{l.style.display='grid';l.textContent='K 线显示已关闭。打开右上角开关后可加载图表。';}}if(t)t.textContent=monitorInst?monitorTitleText():'未配置币种';if(m)m.textContent='图表已暂停 · 监控进程不受影响';if(p)p.innerHTML='<strong>--</strong><span>显示已关闭</span>';updateChartFooter([]);}}
function updateMonitorChartUI(fetchNow){{const enabled=isMonitorChartEnabled(),panel=document.getElementById('monitorChartPanel'),label=document.getElementById('monitorChartLabel'),offHint=document.getElementById('monitorChartOffHint');if(panel)panel.classList.toggle('monitor-chart-off',!enabled);if(offHint)offHint.hidden=enabled;if(label)label.textContent=enabled?'K线显示已开启':'K线显示已关闭';try{{localStorage.setItem(MONITOR_CHART_KEY,enabled?'1':'0');}}catch(e){{}}if(!enabled){{showMonitorChartOffState();return;}}const l=document.getElementById('monitorLoading');if(l&&l.textContent.indexOf('K 线显示已关闭')>=0)l.textContent='正在加载 K 线...';if(fetchNow)bootstrapMonitorChart();}}
function initMonitorChartToggle(){{const toggle=document.getElementById('monitorChartEnableToggle');if(!toggle)return;try{{const saved=localStorage.getItem(MONITOR_CHART_KEY);toggle.checked=saved!=='0';}}catch(e){{toggle.checked=true;}}updateMonitorChartUI(false);toggle.addEventListener('change',function(){{updateMonitorChartUI(true);}});}}
const CANDLE_COLOR_RISE='#22c55e';
const CANDLE_COLOR_FALL='#ef4444';
function candleTrendColor(isRise){{return isRise?CANDLE_COLOR_RISE:CANDLE_COLOR_FALL;}}
function candleTrendFill(isRise){{return isRise?'rgba(34,197,94,.12)':'rgba(239,68,68,.12)';}}
function candleOhlc(point){{if(!point)return null;const close=Number(point.close!=null?point.close:point.price);if(!Number.isFinite(close))return null;const open=Number(point.open!=null?point.open:close);const high=Number(point.high!=null?point.high:Math.max(open,close));const low=Number(point.low!=null?point.low:Math.min(open,close));return {{open:open,high:high,low:low,close:close}};}}
function seriesToCandles(series){{if(!Array.isArray(series)||!series.length)return [];const base=virtualBase();return series.map(function(pt,i){{const close=Number(pt.price);const open=i>0?Number(series[i-1].price):close;const wick=Math.max(Math.abs(close-open)*0.35,base*0.00008,0.01);return Object.assign({{}},pt,{{open:open,close:close,high:Math.max(open,close)+wick,low:Math.min(open,close)-wick,price:close,kind:pt.kind||'virtual'}});}});}}
function setMonitorCandles(points){{const series=(points||[]).slice();monitorCandlesByKey[monitorCacheKey(monitorInst,monitorBar)]=series;monitorSeriesByInst[monitorInst]=series;monitorPayload={{points:series,bar:monitorBar}};return series;}}
function monitorTitleText(){{return monitorInst+' · '+monitorBar+' K线';}}
function currentMonitorSeries(){{return monitorCandlesByKey[monitorCacheKey(monitorInst,monitorBar)]||monitorSeriesByInst[monitorInst]||[];}}
function refreshMonitorTabs(insts){{if(!Array.isArray(insts))return;configuredMonitorInsts=insts;const box=document.querySelector('.coin-tabs');if(!box)return;if(!configuredMonitorInsts.length){{monitorInst='';box.innerHTML='<span class="empty-coin">请先在配置页选择监控币种</span>';clearChartMessage('请先在配置页选择监控币种');return;}}if(configuredMonitorInsts.indexOf(monitorInst)<0)monitorInst=configuredMonitorInsts[0];box.innerHTML=configuredMonitorInsts.map(inst=>'<button class="button coin-tab '+(inst===monitorInst?'active':'')+'" type="button" data-monitor-inst="'+inst+'">'+inst+'</button>').join('');bindMonitorTabs();}}
function bindMonitorTabs(){{document.querySelectorAll('[data-monitor-inst]').forEach(b=>b.onclick=()=>{{const next=b.getAttribute('data-monitor-inst');if(configuredMonitorInsts.indexOf(next)<0)return;monitorInst=next;monitorViewStart=0;monitorViewEnd=1;monitorYZoom=1;monitorYPan=0;monitorSelectedKey='';monitorLastTickerAt=0;document.querySelectorAll('[data-monitor-inst]').forEach(o=>o.classList.remove('active'));b.classList.add('active');if(isMonitorChartEnabled())bootstrapMonitorChart();}});}}
function virtualBase(){{if(monitorInst==='ETH-USDT-SWAP')return 3200;if(monitorInst==='BTC-USDT-SWAP')return 63000;return 100;}}
function gen(n,base){{let a=[],v=base,now=Date.now();const step=Math.max(.18,base*.000055);for(let i=0;i<n;i++){{v+=(Math.random()-.5)*step+Math.sin((i+virtualTick)/18)*step*.18;a.push({{time:new Date(now-(n-i-1)*1000).toLocaleString(),price:v,kind:'virtual'}});}}return a;}}
function nextVirtualSeries(){{if(!monitorInst)return [];virtualTick++;let series=virtualSeriesByInst[monitorInst]||[];if(series.length<2){{series=gen(260,virtualBase());}}else{{const last=Number(series[series.length-1].price)||virtualBase(),step=Math.max(.18,virtualBase()*.00007),drift=Math.sin(virtualTick/16)*step*.2,price=Math.max(.01,last+(Math.random()-.5)*step+drift);series=[...series.slice(-259),{{time:new Date().toLocaleString(),price:price,kind:'virtual'}}];}}virtualSeriesByInst[monitorInst]=series;return series;}}
function visiblePoints(points){{if(!points||points.length<2)return points||[];const last=points.length-1;let start=Math.max(0,Math.min(last,Math.floor(monitorViewStart*last)));let end=Math.max(start+1,Math.min(last,Math.ceil(monitorViewEnd*last)));return points.slice(start,end+1);}}
function fmt(v,d){{const n=Number(v);return Number.isFinite(n)?n.toFixed(d):'--';}}
function fmtPct(v){{const n=Number(v);return Number.isFinite(n)?n.toFixed(4)+'%':'--';}}
function shortTime(t){{if(!t)return '--';const d=parsePointTime(t),pad=n=>String(n).padStart(2,'0');return pad(d.getHours())+':'+pad(d.getMinutes());}}
function compactTime(t){{if(!t)return '--';const d=parsePointTime(t),pad=n=>String(n).padStart(2,'0');return pad(d.getMonth()+1)+'-'+pad(d.getDate())+' '+pad(d.getHours())+':'+pad(d.getMinutes());}}
function chartDate(t){{if(!t)return '--';const d=parsePointTime(t),pad=n=>String(n).padStart(2,'0');return pad(d.getMonth()+1)+'-'+pad(d.getDate());}}
function monitorSpanHours(points){{if(!points||points.length<2)return 0;const a=parsePointTime(points[0].time).getTime(),b=parsePointTime(points[points.length-1].time).getTime();return Math.abs(b-a)/3600000;}}
function monitorSpanDays(points){{return monitorSpanHours(points)/24;}}
function monitorSameDay(left,right){{const a=parsePointTime(left),b=parsePointTime(right);return a.getFullYear()===b.getFullYear()&&a.getMonth()===b.getMonth()&&a.getDate()===b.getDate();}}
function monitorSpansMultipleDays(points){{if(!points||points.length<2)return false;return !monitorSameDay(points[0].time,points[points.length-1].time);}}
function monitorTimeLabel(t,spanHours,bar,points){{if(!t)return '--';const hours=Number(spanHours)||0,period=String(bar||monitorBar||'1m'),multiDay=monitorSpansMultipleDays(points);if(hours>=24*5||period==='4H'||period==='1D'||period==='1W')return chartDate(t);if(multiDay||hours>=18||period==='1H'||period==='15m')return compactTime(t);if(hours>=6)return compactTime(t);return shortTime(t);}}
function timeAxisX(p,cw,points,index,candleMode){{if(candleMode)return p.l+(cw/points.length)*(index+0.5);return p.l+cw*index/Math.max(1,points.length-1);}}
function drawTimeAxis(x,W,H,p,points,cw,options){{options=options||{{}};if(!points||points.length<2)return;const spanHours=monitorSpanHours(points),maxLabels=Math.max(2,Math.min(8,Math.floor(W/120))),step=Math.max(1,Math.floor((points.length-1)/(maxLabels-1))),candleMode=!!options.candleMode,labelFn=function(t){{return monitorTimeLabel(t,spanHours,options.bar||monitorBar,points);}};x.fillStyle='rgba(229,231,235,.9)';x.font='11px Segoe UI, Microsoft YaHei, Arial';x.textAlign='center';x.textBaseline='top';for(let i=0;i<points.length;i+=step){{const px=timeAxisX(p,cw,points,i,candleMode);x.fillText(labelFn(points[i].time),px,H-26);}}const lastIndex=points.length-1;if(lastIndex%step!==0){{x.fillText(labelFn(points[lastIndex].time),timeAxisX(p,cw,points,lastIndex,candleMode),H-26);}}}}
function displayValue(v){{return v===undefined||v===null||v===''?'--':v;}}
function compactList(v,limit){{if(!Array.isArray(v)||!v.length)return '--';return v.slice(0,limit||3).join(' / ')+(v.length>(limit||3)?' ...':'');}}
function fmtScore(v){{const n=Number(v);return Number.isFinite(n)?String(Math.round(n)):'--';}}
function fmtSignedPct(v,d){{const n=Number(v);return Number.isFinite(n)?(n>=0?'+':'')+n.toFixed(d)+'%':'--';}}
function fmtBool(v){{return v===true?'是':(v===false?'否':'--');}}
function summarizeLayers(layers){{if(!layers||typeof layers!=='object')return '--';const keys=['market_regime_score','trend_score','momentum_score','volume_price_score','derivatives_score','orderbook_score','entry_quality_score','risk_control_score'];return keys.filter(k=>layers[k]!==undefined&&layers[k]!==null).map(k=>k.replace('_score','').replace('market_regime','状态').replace('volume_price','量价').replace('entry_quality','入场').replace('risk_control','风控').replace('orderbook','盘口').replace('derivatives','合约').replace('momentum','动量').replace('trend','趋势')+':'+fmtScore(layers[k])).join(' / ')||'--';}}
function formatDuration(seconds){{let s=Math.max(0,Math.floor(Number(seconds)||0)),h=Math.floor(s/3600),m=Math.floor((s%3600)/60),sec=s%60,pad=n=>String(n).padStart(2,'0');return h>0?h+'小时 '+pad(m)+'分 '+pad(sec)+'秒':pad(m)+':'+pad(sec);}}
function updateMonitorProcessInfo(status){{const el=document.getElementById('monitorProcessInfo');if(!el)return;if(status&&status.running&&status.pid){{el.textContent='PID '+status.pid+' · Token '+formatTokenCount(status.ai_total_tokens);return;}}if(status&&status.ai_total_tokens>0){{el.textContent='PID -- · Token '+formatTokenCount(status.ai_total_tokens);return;}}el.textContent='PID -- · Token --';}}
function formatTokenCount(value){{const n=Number(value)||0;return n.toLocaleString('en-US');}}
function updateMonitorUptime(status){{updateMonitorProcessInfo(status);const el=document.getElementById('monitorUptime');if(status){{monitorStatusRunning=!!status.running;monitorStartedAt=status.started_at||'';monitorElapsedSeconds=Number(status.elapsed_seconds)||0;}}if(!el)return;if(monitorStatusRunning)el.textContent='已监控：'+formatDuration(monitorElapsedSeconds);else el.textContent=monitorStartedAt?'已停止：'+formatDuration(monitorElapsedSeconds):'已监控：未启动';}}
function formatPaperAccount(paper){{if(!paper||paper.equity==null)return '--';const eq=Number(paper.equity),pnl=Number(paper.pnl_usd)||0,pct=Number(paper.pnl_pct)||0,sign=pnl>=0?'+':'';return '$'+fmt(eq,0)+' ('+sign+fmt(pnl,0)+' / '+sign+fmt(pct,2)+'%) · '+(paper.position_label||paper.position||'--');}}
function updatePaperAccount(paper){{const el=document.getElementById('monitorPaperAccount');if(!el)return;if(!paper||paper.equity==null){{el.textContent='模拟账户：--';el.classList.remove('paper-up','paper-down');return;}}const pnl=Number(paper.pnl_usd)||0;el.textContent='模拟账户 '+formatPaperAccount(paper);el.classList.remove('paper-up','paper-down');el.classList.add(pnl>=0?'paper-up':'paper-down');}}
function updateChartFooter(points){{const c=document.getElementById('monitorPointCount');if(!c)return;c.textContent='K线：'+((points&&points.length)||0);}}
function parsePointTime(t){{const s=String(t||'');const m=s.match(/^(\\d{{4}})-(\\d{{2}})-(\\d{{2}})[ T](\\d{{2}}):(\\d{{2}})(?::(\\d{{2}}))?/);if(m)return new Date(Number(m[1]),Number(m[2])-1,Number(m[3]),Number(m[4]),Number(m[5]),Number(m[6]||0));const d=new Date(s);return Number.isFinite(d.getTime())?d:new Date();}}
function formatPointTime(d){{const pad=n=>String(n).padStart(2,'0');return d.getFullYear()+'-'+pad(d.getMonth()+1)+'-'+pad(d.getDate())+' '+pad(d.getHours())+':'+pad(d.getMinutes())+':'+pad(d.getSeconds());}}
function bucket1mTime(t){{const d=parsePointTime(t);d.setSeconds(0,0);return formatPointTime(d);}}
function normalizeClientPoint(point,fallbackKind){{if(!point)return null;const price=Number(point.price);if(!Number.isFinite(price))return null;const normalized=Object.assign({{}},point);normalized.kind=normalized.kind||fallbackKind||'realtime';normalized.time=String(point.time||new Date().toLocaleString());if(normalized.kind!=='virtual')normalized.time=bucket1mTime(normalized.time);normalized.price=price;return normalized;}}
function mergeClientPoints(existing,incoming,maxPoints){{const merged=[],indexByTime={{}};[...(existing||[]),...(incoming||[])].forEach(point=>{{const normalized=normalizeClientPoint(point,'realtime');if(!normalized)return;const key=normalized.time||'';if(key&&Object.prototype.hasOwnProperty.call(indexByTime,key)){{merged[indexByTime[key]]=Object.assign({{}},merged[indexByTime[key]],normalized);return;}}if(key)indexByTime[key]=merged.length;merged.push(normalized);}});merged.sort((a,b)=>parsePointTime(a.time).getTime()-parsePointTime(b.time).getTime());return merged.slice(-Math.max(2,maxPoints||20000));}}
function setMonitorSeries(points){{return setMonitorCandles(points);}}
function drawMonitorSeries(metaText){{return drawMonitorCandles(metaText);}}
function drawMonitorCandles(metaText){{if(metaText)monitorMetaText=metaText;const series=currentMonitorSeries();if(!series.length)return false;drawCandleChart('monitorChart',series,document.getElementById('monitorPrice'),document.getElementById('monitorMeta'),document.getElementById('monitorLoading'));const t=document.getElementById('monitorTitle');if(t)t.textContent=monitorTitleText();const m=document.getElementById('monitorMeta');if(m&&monitorMetaText)m.textContent=monitorMetaText;return true;}}
function formatDecisionSource(v){{const key=String(v||'').toLowerCase();if(key==='ai')return 'AI前瞻';if(key==='local_screening')return '本地筛查';if(key==='local')return '本地';if(key==='local_fallback')return '本地兜底';return displayValue(v);}}
function formatPushRecommendation(v){{const text=String(v||'none').toLowerCase();return text==='none'?'none':text;}}
function formatSnapshotDirection(point){{const op=point.forward_direction||point.final_direction||point.direction||'--',localBias=point.local_bias||point.local_hint_direction||point.raw_direction;if(String(point.decision_source||'').toLowerCase()==='ai'&&point.forward_direction)return escHtml(op);if(localBias&&localBias!==op)return escHtml(op)+' (本地 '+escHtml(localBias)+')';return escHtml(op);}}
function formatDualTrack(point){{const localBias=displayValue(point.local_bias||point.local_hint_direction||point.raw_direction),forecastDir=displayValue(point.structure_forecast_direction),forwardDir=displayValue(point.forward_direction),forwardProb=point.forward_probability!=null&&point.forward_probability!==''?('P='+fmtScore(point.forward_probability)):'';const aiPart=forwardDir!=='--'?forwardDir+(forwardProb?' · '+forwardProb:''):'--';return '本地回顾 '+localBias+(forecastDir!=='--'?' · 演变 '+forecastDir:'')+' · AI前瞻 '+aiPart;}}
function formatTrend15m(point){{const profile=displayValue(point.trend_profile_15m),simple=displayValue(point.trend_simple_15m);if(profile!=='--'&&simple!=='--'&&profile!==simple)return escHtml(profile)+' · trends '+escHtml(simple);return escHtml(profile!=='--'?profile:simple);}}
function candleChangeText(open,close){{const chg=close-open,pct=open?chg/open*100:0;return (chg>=0?'+':'')+fmt(chg,2)+' / '+(pct>=0?'+':'')+fmt(pct,2)+'%';}}
function candleInfoContent(point){{if(!point)return {{title:'选中K线',html:'<div class="snapshot-grid"><span>时间</span><b>--</b></div>'}};const o=candleOhlc(point);if(!o)return {{title:'选中K线',html:'<div class="snapshot-grid"><span>数据</span><b>--</b></div>'}};const bar=point.bar||monitorBar,up=o.close>=o.open,color=candleTrendColor(up),vol=point.volume!=null&&point.volume!==''?fmt(point.volume,2):'--',confirmed=String(point.confirmed)==='1'?'已确认':(String(point.confirmed)==='0'?'未确认':'--'),amp=o.low?((o.high-o.low)/o.low*100):0;return {{title:'选中K线 · '+bar,html:'<div class="snapshot-grid"><span>时间</span><b>'+compactTime(point.time)+'</b><span>开盘</span><b>'+fmt(o.open,2)+'</b><span>最高</span><b>'+fmt(o.high,2)+'</b><span>最低</span><b>'+fmt(o.low,2)+'</b><span>收盘</span><b style="color:'+color+'">'+fmt(o.close,2)+'</b><span>涨跌</span><b style="color:'+color+'">'+candleChangeText(o.open,o.close)+'</b><span>振幅</span><b>'+fmt(amp,2)+'%</b><span>成交量</span><b>'+vol+'</b><span>状态</span><b>'+confirmed+'</b></div>'}};}}
function formatIndicatorValue(v,digits){{if(v===undefined||v===null||v==='')return '--';const n=Number(v);if(Number.isFinite(n))return fmt(n,digits==null?2:digits);return escHtml(String(v));}}
function snapshotIndicatorGridRows(ind){{if(!ind||typeof ind!=='object')return '<span>15m指标</span><b>暂无 trend_profiles 数据</b>';const rsi=[formatIndicatorValue(ind.rsi_6,1),formatIndicatorValue(ind.rsi_14,1),formatIndicatorValue(ind.rsi_24,1)].join(' / '),macd='DIF '+formatIndicatorValue(ind.macd_dif,4)+' · DEA '+formatIndicatorValue(ind.macd_dea,4)+' · HIST '+formatIndicatorValue(ind.macd_hist,4)+(ind.macd_hist_slope!=null&&ind.macd_hist_slope!==''?' · Δ'+formatIndicatorValue(ind.macd_hist_slope,4):''),kdj='K '+formatIndicatorValue(ind.kdj_k,1)+' · D '+formatIndicatorValue(ind.kdj_d,1)+' · J '+formatIndicatorValue(ind.kdj_j,1),boll='BW '+formatIndicatorValue(ind.boll_bw_pct,3)+'% · 位 '+formatIndicatorValue(ind.boll_pos,3),adx=formatIndicatorValue(ind.adx,1)+' / +DI '+formatIndicatorValue(ind.plus_di,1)+' / -DI '+formatIndicatorValue(ind.minus_di,1),structure=formatIndicatorValue(ind.recent_high,2)+' / '+formatIndicatorValue(ind.recent_low,2)+' · '+displayValue(ind.breakout),quality=(ind.data_reliable===true?'可靠':(ind.data_reliable===false?'不足':'--'))+' / K '+displayValue(ind.data_count),emaLine=(ind.ema?escHtml(String(ind.ema)):'--')+(ind.ema_slope_pct!=null&&ind.ema_slope_pct!==''?' · 斜率 '+formatIndicatorValue(ind.ema_slope_pct,3)+'%':''),rows=[['15m趋势',displayValue(ind.trend)],['EMA',emaLine],['ATR',formatIndicatorValue(ind.atr,4)+' / '+formatIndicatorValue(ind.atr_pct,3)+'%'],['结构高低',structure],['RSI',rsi],['MACD',macd],['KDJ',kdj],['BOLL',boll],['ADX',adx],['偏离EMA20',formatIndicatorValue(ind.dist_ema20_atr,2)+' ATR'],['背离',displayValue(ind.divergence)],['K线实体',formatIndicatorValue(ind.body_ratio,3)],['数据质量',quality]];return rows.map(function(row){{return '<span>'+row[0]+'</span><b>'+row[1]+'</b>';}}).join('');}}
function formatMultiTfTrends(indicators){{const src=indicators||{{}};const parts=['5m '+displayValue(src['5m']&&src['5m'].trend),'15m '+displayValue(src['15m']&&src['15m'].trend),'1H '+displayValue(src['1H']&&src['1H'].trend),'4H '+displayValue(src['4H']&&src['4H'].trend)];return parts.join(' · ');}}
function localRealtimeSnapshotContent(point){{if(!point)return {{title:'最新快照',html:'<div class="snapshot-grid"><span>状态</span><b>启动监控后显示分析结果</b><span>提示</span><b>点击 K 线查看 OHLC</b></div>'}};const indicators=point.trend_indicators||{{}},ind15=indicators['15m']||null;let html='<div class="snapshot-grid"><span>时间</span><b>'+compactTime(point.time)+'</b><span>价格</span><b>'+fmt(point.price,2)+'</b><span>多周期趋势</span><b>'+formatMultiTfTrends(indicators)+'</b>'+snapshotIndicatorGridRows(ind15)+'</div>';return {{title:'最新快照 · 本地指标',html:html}};}}
function analysisSnapshotContent(point){{if(!point)return {{title:'最新快照',html:'<div class="snapshot-grid"><span>状态</span><b>启动监控后显示分析结果</b><span>提示</span><b>点击 K 线查看 OHLC</b></div>'}};if(point.kind==='realtime')return localRealtimeSnapshotContent(point);const hasMetrics=point.raw_total_score!==undefined&&point.raw_total_score!==null,kind=hasMetrics?'历史回放':'日志快照',localRef=hasMetrics?('观察 '+fmtScore(point.raw_total_score)+' / 本地 '+fmtScore(point.local_final_trade_score??point.final_trade_score)):'--',dualTrack=formatDualTrack(point),qualityText=(point.data_quality_reliable===true?'可靠':(point.data_quality_reliable===false?'不足':'--'))+' / 15m K '+displayValue(point.data_quality_count),warmup='OI '+fmtBool(point.oi_warmup_ready)+' / 费率 '+fmtBool(point.funding_warmup_ready),signals=compactList(point.signals,3),levels=displayValue(point.entry)+' · SL '+displayValue(point.stop_loss)+' · TP '+displayValue(point.take_profit),summary=point.summary?String(point.summary).trim():'',aiFlag=point.ai_called===true?'是':(point.ai_called===false?'否':'--');let html='<div class="snapshot-grid"><span>时间</span><b>'+compactTime(point.time)+'</b><span>价格</span><b>'+fmt(point.price,2)+'</b><span>操作方向</span><b>'+formatSnapshotDirection(point)+'</b><span>双轨</span><b>'+dualTrack+'</b><span>置信度</span><b>'+fmtScore(point.confidence??point.final_trade_score)+'</b><span>推送</span><b>'+formatPushRecommendation(point.push_recommendation||point.trade_action_level)+'</b><span>来源</span><b>'+formatDecisionSource(point.decision_source)+'</b><span>AI</span><b>'+aiFlag+'</b><span>触发</span><b>'+displayValue(point.trigger_level)+'</b><span>本地分数</span><b>'+localRef+'</b><span>15m趋势</span><b>'+formatTrend15m(point)+'</b><span>市场/策略</span><b>'+displayValue(point.market_regime)+' / '+displayValue(point.strategy_label||point.strategy_template)+'</b><span>风险</span><b>'+displayValue(point.risk_level)+'</b><span>入场/止损/止盈</span><b>'+levels+'</b><span>数据质量</span><b>'+qualityText+'</b><span>预热</span><b>'+warmup+'</b><span>触发信号</span><b>'+signals+'</b></div>';if(summary)html+='<div class="snapshot-note">'+escHtml(summary)+'</div>';return {{title:'最新快照 · '+kind,html:html}};}}
const MONITOR_SNAPSHOT_COLLAPSED_KEY='okx_monitor_snapshot_collapsed';
let monitorSnapshotCollapsed=false;
function updateSnapshotToggleLabel(){{const btn=document.getElementById('snapshotToggleBtn');if(btn)btn.textContent=monitorSnapshotCollapsed?'展开':'收起';}}
function applySnapshotCollapsedState(){{const panel=document.getElementById('snapshotPanel');if(!panel)return;panel.classList.toggle('is-collapsed',monitorSnapshotCollapsed);updateSnapshotToggleLabel();}}
function initSnapshotPanelToggle(){{try{{monitorSnapshotCollapsed=localStorage.getItem(MONITOR_SNAPSHOT_COLLAPSED_KEY)==='1';}}catch(e){{monitorSnapshotCollapsed=false;}}applySnapshotCollapsedState();const btn=document.getElementById('snapshotToggleBtn');if(!btn)return;btn.addEventListener('click',function(e){{e.stopPropagation();monitorSnapshotCollapsed=!monitorSnapshotCollapsed;try{{localStorage.setItem(MONITOR_SNAPSHOT_COLLAPSED_KEY,monitorSnapshotCollapsed?'1':'0');}}catch(err){{}}applySnapshotCollapsedState();}});}}
function refreshMonitorSnapshotPanel(){{const titleEl=document.getElementById('snapshotPanelTitle'),bodyEl=document.getElementById('snapshotPanelBody');if(!titleEl||!bodyEl)return;let content;if(monitorSelectedKey){{const hit=monitorPlotPoints.find(function(o){{return (o.point.time||'')===monitorSelectedKey;}});if(hit)content=candleInfoContent(hit.point);else{{monitorSelectedKey='';content=analysisSnapshotContent(monitorLatestSnapshot);}}}}else content=analysisSnapshotContent(monitorLatestSnapshot);titleEl.textContent=content.title;bodyEl.innerHTML=content.html;}}
function drawMonitorTimeAxis(x,W,H,p,points,cw){{drawTimeAxis(x,W,H,p,points,cw,{{candleMode:true,bar:monitorBar}});}}
function drawAccuracyTimeAxis(x,W,H,p,points,cw){{if(!points||points.length<2)return;const spanHours=accuracySpanHours(points),maxLabels=Math.max(2,Math.min(10,Math.floor(W/110))),step=Math.max(1,Math.floor((points.length-1)/(maxLabels-1)));x.fillStyle='rgba(229,231,235,.9)';x.font='12px Segoe UI, Microsoft YaHei, Arial';x.textAlign='center';x.textBaseline='top';for(let i=0;i<points.length;i+=step){{const px=p.l+cw*i/(points.length-1);x.fillText(accuracyTimeLabel(points[i].time,spanHours),px,H-24);}}const lastIndex=points.length-1;if((lastIndex%step)!==0){{x.fillText(accuracyTimeLabel(points[lastIndex].time,spanHours),W-p.r,H-24);}}}}
function drawPriceAxis(x,W,H,p,mn,mx){{x.fillStyle='rgba(229,231,235,.82)';x.font='12px Segoe UI, Microsoft YaHei, Arial';x.textAlign='right';x.textBaseline='middle';for(let i=0;i<=4;i++){{const value=mx-(mx-mn)*i/4,y=p.t+(H-p.t-p.b)*i/4;x.fillText(value.toFixed(2),W-8,y);}}}}
function clearChartMessage(text){{const c=document.getElementById('monitorChart'),l=document.getElementById('monitorLoading'),p=document.getElementById('monitorPrice'),m=document.getElementById('monitorMeta'),t=document.getElementById('monitorTitle');monitorSelectedKey='';monitorVisiblePoints=[];monitorPlotPoints=[];monitorLatestPoint=null;monitorLatestSnapshot=null;if(c){{const r=c.getBoundingClientRect(),x=c.getContext('2d');c.width=Math.max(1,r.width);c.height=Math.max(1,r.height);x.clearRect(0,0,r.width,r.height);}}if(l){{l.style.display='grid';l.textContent=text;}}if(p)p.innerHTML='<strong>--</strong><span>无数据</span>';if(m)m.textContent=text;if(t)t.textContent='未配置币种';updateChartFooter([]);updatePaperAccount(null);refreshMonitorSnapshotPanel();}}
function drawChart(id,points,priceBox,metaBox,loading){{const c=document.getElementById(id);if(!c||!points||points.length<1)return;if(points.length===1)points=[points[0],{{time:points[0].time,price:points[0].price}}];monitorLatestPoint=points[points.length-1];points=visiblePoints(points);monitorVisiblePoints=points;updateChartFooter(points);if(loading)loading.style.display='none';const d=window.devicePixelRatio||1,r=c.getBoundingClientRect();c.width=r.width*d;c.height=r.height*d;const x=c.getContext('2d');x.setTransform(d,0,0,d,0,0);const W=r.width,H=r.height,p={{l:34,r:74,t:18,b:58}},cw=W-p.l-p.r,ch=H-p.t-p.b,prices=points.map(q=>q.price);let mn=Math.min(...prices),mx=Math.max(...prices);const rawCenter=(mn+mx)/2,baseRg=Math.max(.01,(mx-mn)*1.16),center=rawCenter+monitorYPan,rg=baseRg/monitorYZoom;monitorYRange=rg;mn=center-rg/2;mx=center+rg/2;monitorPlotPoints=[];x.clearRect(0,0,W,H);x.strokeStyle='rgba(255,255,255,.12)';for(let i=0;i<=4;i++){{const y=p.t+ch*i/4;x.beginPath();x.moveTo(p.l,y);x.lineTo(W-p.r,y);x.stroke();}}x.beginPath();points.forEach((q,i)=>{{const px=p.l+cw*i/(points.length-1),py=p.t+ch-((q.price-mn)/rg)*ch;monitorPlotPoints.push({{x:px,y:py,point:q}});if(i===0)x.moveTo(px,py);else x.lineTo(px,py);}});const up=points[points.length-1].price>=points[0].price;x.strokeStyle=candleTrendColor(up);x.lineWidth=2;x.stroke();x.fillStyle=candleTrendFill(up);x.lineTo(W-p.r,H-p.b);x.lineTo(p.l,H-p.b);x.closePath();x.fill();drawTimeAxis(x,W,H,p,points,cw);drawPriceAxis(x,W,H,p,mn,mx);if(monitorSelectedKey){{const hit=monitorPlotPoints.find(o=>(o.point.time||'')===monitorSelectedKey);if(hit){{x.strokeStyle='rgba(255,255,255,.62)';x.setLineDash([4,5]);x.beginPath();x.moveTo(hit.x,p.t);x.lineTo(hit.x,H-p.b);x.stroke();x.beginPath();x.moveTo(p.l,hit.y);x.lineTo(W-p.r,hit.y);x.stroke();x.setLineDash([]);x.fillStyle='#60a5fa';x.beginPath();x.arc(hit.x,hit.y,5,0,7);x.fill();}}else{{monitorSelectedKey='';}}}}refreshMonitorSnapshotPanel();if(priceBox){{const first=points[0].price,last=points[points.length-1].price,chg=last-first,pct=first?chg/first*100:0;priceBox.classList.toggle('up',chg>=0);priceBox.classList.toggle('down',chg<0);priceBox.innerHTML='<strong>'+last.toFixed(2)+'</strong><span>'+(chg>=0?'+':'')+chg.toFixed(2)+' / '+(pct>=0?'+':'')+pct.toFixed(2)+'%</span>';}}if(metaBox)metaBox.textContent='更新：'+new Date().toLocaleTimeString();}}
function drawCandleChart(id,points,priceBox,metaBox,loading){{const c=document.getElementById(id);if(!c||!points||points.length<1)return;const normalized=points.map(function(pt){{const o=candleOhlc(pt);if(!o)return null;return Object.assign({{}},pt,{{open:o.open,high:o.high,low:o.low,close:o.close,price:o.close}});}}).filter(Boolean);if(normalized.length<1)return;if(normalized.length===1)normalized.push(Object.assign({{}},normalized[0]));monitorLatestPoint=normalized[normalized.length-1];points=visiblePoints(normalized);monitorVisiblePoints=points;updateChartFooter(points);if(loading)loading.style.display='none';const d=window.devicePixelRatio||1,r=c.getBoundingClientRect();c.width=r.width*d;c.height=r.height*d;const x=c.getContext('2d');x.setTransform(d,0,0,d,0,0);const W=r.width,H=r.height,p={{l:34,r:74,t:18,b:58}},cw=W-p.l-p.r,ch=H-p.t-p.b;let mn=Infinity,mx=-Infinity;points.forEach(function(pt){{const o=candleOhlc(pt);if(!o)return;mn=Math.min(mn,o.low);mx=Math.max(mx,o.high);}});if(!Number.isFinite(mn)||!Number.isFinite(mx)){{mn=0;mx=1;}}const rawCenter=(mn+mx)/2,baseRg=Math.max(.01,(mx-mn)*1.12),center=rawCenter+monitorYPan,rg=Math.max(.01,baseRg/monitorYZoom);monitorYRange=rg;mn=center-rg/2;mx=center+rg/2;monitorPlotPoints=[];x.clearRect(0,0,W,H);x.strokeStyle='rgba(255,255,255,.12)';x.lineWidth=1;for(let i=0;i<=4;i++){{const y=p.t+ch*i/4;x.beginPath();x.moveTo(p.l,y);x.lineTo(W-p.r,y);x.stroke();}}const slotW=cw/points.length,bodyW=Math.max(2,Math.min(14,slotW*0.62));points.forEach(function(pt,i){{const o=candleOhlc(pt);if(!o)return;const cx=p.l+slotW*(i+0.5),yHigh=p.t+ch-((o.high-mn)/rg)*ch,yLow=p.t+ch-((o.low-mn)/rg)*ch,yOpen=p.t+ch-((o.open-mn)/rg)*ch,yClose=p.t+ch-((o.close-mn)/rg)*ch,up=o.close>=o.open,color=candleTrendColor(up);x.strokeStyle=color;x.lineWidth=1;x.beginPath();x.moveTo(cx,yHigh);x.lineTo(cx,yLow);x.stroke();const top=Math.min(yOpen,yClose),bodyH=Math.max(1,Math.abs(yClose-yOpen));x.fillStyle=color;x.fillRect(cx-bodyW/2,top,bodyW,bodyH);monitorPlotPoints.push({{x:cx,y:(yHigh+yLow)/2,point:pt}});}});drawMonitorTimeAxis(x,W,H,p,points,cw);drawPriceAxis(x,W,H,p,mn,mx);if(monitorSelectedKey){{const hit=monitorPlotPoints.find(function(o){{return (o.point.time||'')===monitorSelectedKey;}});if(hit){{x.strokeStyle='rgba(255,255,255,.62)';x.setLineDash([4,5]);x.beginPath();x.moveTo(hit.x,p.t);x.lineTo(hit.x,H-p.b);x.stroke();x.beginPath();x.moveTo(p.l,hit.y);x.lineTo(W-p.r,hit.y);x.stroke();x.setLineDash([]);x.fillStyle='#60a5fa';x.beginPath();x.arc(hit.x,hit.y,5,0,7);x.fill();}}else{{monitorSelectedKey='';}}}}refreshMonitorSnapshotPanel();if(priceBox){{const first=Number(points[0].close!=null?points[0].close:points[0].price),last=Number(points[points.length-1].close!=null?points[points.length-1].close:points[points.length-1].price),chg=last-first,pct=first?chg/first*100:0;priceBox.classList.toggle('up',chg>=0);priceBox.classList.toggle('down',chg<0);priceBox.innerHTML='<strong>'+last.toFixed(2)+'</strong><span>'+(chg>=0?'+':'')+chg.toFixed(2)+' / '+(pct>=0?'+':'')+pct.toFixed(2)+'%</span>';}}}}
function drawVirtualMonitor(){{if(!isMonitorChartEnabled())return;if(!monitorInst){{clearChartMessage('请先在配置页选择监控币种');return;}}const candles=seriesToCandles(nextVirtualSeries());setMonitorCandles(candles);drawCandleChart('monitorChart',candles,document.getElementById('monitorPrice'),document.getElementById('monitorMeta'),document.getElementById('monitorLoading'));const t=document.getElementById('monitorTitle');if(t)t.textContent=monitorTitleText()+' · 虚拟';const m=document.getElementById('monitorMeta');if(m)m.textContent='虚拟 K 线预览 · 启动监控后切换 OKX 真实数据';}}
function setMonitorButtonState(state,text){{const btn=document.getElementById('monitorToggleBtn'),meta=document.getElementById('monitorMeta');if(!btn)return;btn.classList.remove('is-running','is-starting');btn.disabled=false;if(state==='starting'){{btn.classList.add('is-starting');btn.textContent='启动中...';btn.disabled=true;if(meta)meta.textContent=text||'正在启动监控进程...';}}else if(state==='running'){{btn.classList.add('is-running');btn.textContent='停止监控';if(meta&&text)meta.textContent=text;}}else if(state==='stopping'){{btn.classList.add('is-starting');btn.textContent='停止中...';btn.disabled=true;if(meta)meta.textContent=text||'正在停止监控进程...';}}else{{btn.textContent='开始监控';if(meta&&text)meta.textContent=text;}}}}
async function syncMonitorStatus(){{try{{const r=await fetch('/api/status',{{cache:'no-store'}}),p=await r.json();updateMonitorUptime(p);setMonitorButtonState(p.running?'running':'stopped',p.text||'');if(p.running&&!monitorWasRunning&&typeof switchAccuracyToLiveSession==='function')switchAccuracyToLiveSession();monitorWasRunning=!!p.running;return p;}}catch(e){{return null;}}}}
function showRealtimeWaiting(clearChart){{if(!monitorInst){{clearChartMessage('请先在配置页选择监控币种');return;}}monitorLiveMode=true;const c=document.getElementById('monitorChart'),l=document.getElementById('monitorLoading'),m=document.getElementById('monitorMeta'),t=document.getElementById('monitorTitle'),p=document.getElementById('monitorPrice');if(clearChart&&c){{const r=c.getBoundingClientRect(),x=c.getContext('2d');c.width=Math.max(1,r.width);c.height=Math.max(1,r.height);x.clearRect(0,0,r.width,r.height);updateChartFooter([]);}}if(l){{l.style.display=clearChart?'grid':'none';l.textContent='监控已启动，正在加载 '+monitorBar+' K 线...';}}if(m)m.textContent=clearChart?'实时监控已启动 · 等待 '+monitorBar+' K 线':'已连接 · 等待 K 线刷新';if(t)t.textContent=monitorTitleText();if(clearChart&&p)p.innerHTML='<strong>--</strong><span>等待 K 线</span>';}}
function sleep(ms){{return new Promise(resolve=>setTimeout(resolve,ms));}}
async function fetchMonitor(){{if(!isMonitorChartEnabled())return null;if(!monitorInst){{clearChartMessage('请先在配置页选择监控币种');return null;}}try{{const qs='inst_id='+encodeURIComponent(monitorInst)+'&bar='+encodeURIComponent(monitorBar);const r=await fetch('/api/monitor-candles?'+qs,{{cache:'no-store'}}),p=await r.json();updatePaperAccount(p.paper_account||null);monitorLatestSnapshot=p.latest_snapshot||null;if(!r.ok||p.ok===false){{clearChartMessage(p.error||'当前币种未配置，不能读取 K 线');return p;}}const metaPrefix=p.running?'OKX '+monitorBar+' · 指标来自日志':'OKX '+monitorBar+' · 预览';if(p.points&&p.points.length>0){{monitorLiveMode=!!p.running;setMonitorCandles(p.points);drawMonitorCandles(metaPrefix+' · '+new Date().toLocaleTimeString());return p;}}if(p.running){{monitorLiveMode=true;if(!drawMonitorCandles(metaPrefix+' · 等待 K 线'))showRealtimeWaiting(false);return p;}}monitorLiveMode=false;updatePaperAccount(null);drawVirtualMonitor();return p;}}catch(e){{if(monitorLiveMode){{if(!drawMonitorCandles('保留最近 K 线 · 等待刷新'))showRealtimeWaiting(false);}}else drawVirtualMonitor();return null;}}}}
async function bootstrapMonitorChart(){{if(!isMonitorChartEnabled())return;monitorLastTickerAt=0;for(let i=0;i<12;i++){{const payload=await fetchMonitor();if(payload&&payload.points&&payload.points.length>0)break;await sleep(i<4?500:1000);}}}}
function redrawMonitorCached(){{if(!isMonitorChartEnabled())return;const series=currentMonitorSeries();if(series.length){{drawCandleChart('monitorChart',series,document.getElementById('monitorPrice'),document.getElementById('monitorMeta'),document.getElementById('monitorLoading'));return;}}if(monitorLiveMode)return;drawVirtualMonitor();}}
initMonitorChartToggle();
initSnapshotPanelToggle();
bindMonitorTabs();
bindMonitorBarTabs();
const monitorToggleBtn=document.getElementById('monitorToggleBtn');
if(monitorToggleBtn){{monitorToggleBtn.addEventListener('click',async()=>{{const status=await syncMonitorStatus();if(status&&status.running){{setMonitorButtonState('stopping','正在停止监控进程...');try{{const response=await fetch('/api/monitor-stop',{{method:'POST',cache:'no-store'}});const payload=await response.json();if(!response.ok||payload.ok===false)throw new Error(payload.error||payload.message||'停止失败');}}catch(e){{setMonitorButtonState('running','停止失败：'+e);return;}}await syncMonitorStatus();monitorLiveMode=false;monitorViewStart=0;monitorViewEnd=1;monitorYZoom=1;monitorYPan=0;monitorSeriesByInst[monitorInst]=[];setMonitorButtonState('stopped','监控已停止，当前显示虚拟行情');if(isMonitorChartEnabled())drawVirtualMonitor();else showMonitorChartOffState();return;}}if(!monitorInst){{clearChartMessage('请先在配置页选择监控币种');return;}}setMonitorButtonState('starting','正在保存配置并启动监控...');monitorYPan=0;if(isMonitorChartEnabled())showRealtimeWaiting(true);try{{await autoSaveConfig();const response=await fetch('/api/monitor-start',{{method:'POST',cache:'no-store'}});const payload=await response.json();if(!response.ok||payload.ok===false)throw new Error(payload.error||payload.message||'启动失败');await syncMonitorStatus();switchAccuracyToLiveSession();if(isMonitorChartEnabled())bootstrapMonitorChart();}}catch(e){{setMonitorButtonState('stopped','启动监控失败');const l=document.getElementById('monitorLoading');if(l)l.textContent='启动监控失败：'+e;}}}});}}
let logCleared=false,consoleLogCleared=false;
const LOGS_DISPLAY_KEY='okx_logs_display_enabled';
function isLogsDisplayEnabled(){{const el=document.getElementById('logsDisplayToggle');return!!(el&&el.checked);}}
function setLogsDisplayEnabled(enabled,options){{options=options||{{}};const toggle=document.getElementById('logsDisplayToggle');if(toggle)toggle.checked=!!enabled;updateLogsDisplayUI(!!options.fetchNow);}}
function updateLogsDisplayUI(fetchNow){{const enabled=isLogsDisplayEnabled(),body=document.getElementById('logPanelBody'),label=document.getElementById('logsDisplayLabel'),offHint=document.getElementById('logsOffHint');if(body)body.hidden=!enabled;if(offHint)offHint.hidden=enabled;if(label)label.textContent=enabled?'显示已开启 · 自动刷新':'显示已关闭 · 按需打开';try{{localStorage.setItem(LOGS_DISPLAY_KEY,enabled?'1':'0');}}catch(e){{}}if(!enabled)return;if(fetchNow){{refreshLogs(true);refreshConsoleLogs(true);}}}}
function initLogsDisplayToggle(){{const toggle=document.getElementById('logsDisplayToggle');if(!toggle)return;try{{toggle.checked=localStorage.getItem(LOGS_DISPLAY_KEY)==='1';}}catch(e){{}}updateLogsDisplayUI(false);toggle.addEventListener('change',function(){{updateLogsDisplayUI(true);}});}}
initLogsDisplayToggle();
function syncAnalysisLogUi(){{const hint=document.getElementById('analysisLogSwitchHint');if(hint&&window.latestLogSizeSummary)hint.textContent=window.latestLogSizeSummary+' · 修改后需重启监控';}}
async function refreshLogs(force){{if(!isLogsDisplayEnabled()&&!force)return;const box=document.getElementById('logWindow');if(!box)return;if(logCleared&&!force)return;try{{const r=await fetch('/api/logs',{{cache:'no-store'}}),p=await r.json();if(p.log_size_summary){{window.latestLogSizeSummary=p.log_size_summary;syncAnalysisLogUi();}}logCleared=false;box.value=p.text||'暂无日志。';box.scrollTop=box.scrollHeight;}}catch(e){{box.value='日志读取失败：'+e;}}}}
async function refreshConsoleLogs(force){{if(!isLogsDisplayEnabled()&&!force)return;const box=document.getElementById('consoleLogWindow');if(!box)return;if(consoleLogCleared&&!force)return;try{{const r=await fetch('/api/console-logs',{{cache:'no-store'}}),p=await r.json();if(p.log_size_summary){{window.latestLogSizeSummary=p.log_size_summary;syncAnalysisLogUi();}}consoleLogCleared=false;box.value=p.text||'暂无控制台日志。';box.scrollTop=box.scrollHeight;}}catch(e){{box.value='控制台日志读取失败：'+e;}}}}
window.latestLogSizeSummary={json.dumps(log_size_summary_text(config), ensure_ascii=False)};
syncAnalysisLogUi();
function showConnectivityNotice(text, ok) {{
  const box = document.getElementById('connectivityTestNotice');
  if (!box) return;
  if (!text) {{
    box.hidden = true;
    box.textContent = '';
    return;
  }}
  box.hidden = false;
  box.className = ok ? 'notice' : 'notice notice-error';
  box.textContent = text;
}}
async function runConnectivityTest() {{
  const pushBtn = document.getElementById('testPushBtn');
  const label = '测试微信推送';
  if (pushBtn) {{
    pushBtn.disabled = true;
    pushBtn.textContent = '测试中...';
  }}
  showConnectivityNotice('正在保存配置并测试 ' + label + '...', true);
  try {{
    await autoSaveConfig();
    const response = await fetch('/api/test-push', {{ cache: 'no-store' }});
    const payload = await response.json();
    const message = payload.message || payload.error || '未知结果';
    showConnectivityNotice(message, !!payload.ok);
    if (!response.ok || payload.ok === false) alert(message);
  }} catch (error) {{
    const message = label + '失败：' + error;
    showConnectivityNotice(message, false);
    alert(message);
  }} finally {{
    if (pushBtn) {{
      pushBtn.disabled = false;
      pushBtn.textContent = label;
    }}
  }}
}}
let aiChatMessages = [];
function aiChatHistoryPayload() {{
  return aiChatMessages.filter(function(item) {{
    return !item.pending && !item.error && (item.role === 'user' || item.role === 'assistant');
  }}).map(function(item) {{
    return {{ role: item.role, content: item.content }};
  }});
}}
function formatAiChatUsage(usage) {{
  if (!usage || typeof usage !== 'object') return '';
  const prompt = usage.prompt_tokens;
  const completion = usage.completion_tokens;
  const total = usage.total_tokens;
  if (prompt == null && completion == null && total == null) return '';
  const parts = [];
  if (prompt != null) parts.push('输入 ' + prompt);
  if (completion != null) parts.push('输出 ' + completion);
  if (total != null) parts.push('合计 ' + total);
  return parts.join(' · ') + ' tokens';
}}
function renderAiChatMessages() {{
  const box = document.getElementById('aiChatWindow');
  if (!box) return;
  if (!aiChatMessages.length) {{
    box.innerHTML = '<div class="ai-chat-empty">输入下方消息开始与 AI 对话。可先点「连通性测试」验证配置是否可用。</div>';
    return;
  }}
  box.innerHTML = aiChatMessages.map(function(item) {{
    let cls = 'ai-chat-msg ';
    if (item.pending) cls += 'pending';
    else if (item.error) cls += 'error';
    else cls += item.role === 'user' ? 'user' : 'assistant';
    let body = escHtml(item.content);
    if (item.role === 'assistant' && !item.pending && !item.error) {{
      const usageText = formatAiChatUsage(item.usage);
      if (usageText) body += '<div class="ai-chat-usage">' + escHtml(usageText) + '</div>';
    }}
    return '<div class="' + cls + '">' + body + '</div>';
  }}).join('');
  box.scrollTop = box.scrollHeight;
}}
async function sendAiChatMessage(textOverride) {{
  const input = document.getElementById('aiChatInput');
  const sendBtn = document.getElementById('aiChatSendBtn');
  const pingBtn = document.getElementById('aiChatPingBtn');
  const text = String(textOverride != null ? textOverride : (input ? input.value : '')).trim();
  if (!text) return;
  if (input && textOverride == null) input.value = '';
  aiChatMessages.push({{ role: 'user', content: text }});
  renderAiChatMessages();
  if (sendBtn) {{ sendBtn.disabled = true; sendBtn.textContent = '发送中...'; }}
  if (pingBtn) pingBtn.disabled = true;
  aiChatMessages.push({{ role: 'assistant', content: '正在思考...', pending: true }});
  renderAiChatMessages();
  try {{
    await autoSaveConfig();
    const history = aiChatHistoryPayload().slice(0, -1);
    const response = await fetch('/api/ai-chat', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ message: text, history: history }}),
      cache: 'no-store'
    }});
    const payload = await response.json();
    aiChatMessages.pop();
    if (!response.ok || payload.ok === false) {{
      aiChatMessages.push({{ role: 'assistant', content: payload.error || payload.message || '请求失败', error: true }});
    }} else {{
      aiChatMessages.push({{ role: 'assistant', content: payload.reply || '', usage: payload.usage || null }});
    }}
  }} catch (error) {{
    aiChatMessages.pop();
    aiChatMessages.push({{ role: 'assistant', content: String(error), error: true }});
  }} finally {{
    if (sendBtn) {{ sendBtn.disabled = false; sendBtn.textContent = '发送'; }}
    if (pingBtn) pingBtn.disabled = false;
    renderAiChatMessages();
  }}
}}
async function runAiChatPing() {{
  const pingBtn = document.getElementById('aiChatPingBtn');
  if (pingBtn) {{ pingBtn.disabled = true; pingBtn.textContent = '测试中...'; }}
  try {{
    await sendAiChatMessage('请只回复：AI接口连通性测试成功。');
  }} finally {{
    if (pingBtn) {{ pingBtn.disabled = false; pingBtn.textContent = '连通性测试'; }}
  }}
}}
async function runAiChatBrief() {{
  const briefBtn = document.getElementById('aiChatBriefBtn');
  const sendBtn = document.getElementById('aiChatSendBtn');
  const pingBtn = document.getElementById('aiChatPingBtn');
  const inst = monitorInst || (configuredMonitorInsts && configuredMonitorInsts.length ? configuredMonitorInsts[0] : '');
  if (!inst) {{
    alert('请先在配置页选择监控币种');
    return;
  }}
  if (briefBtn) {{ briefBtn.disabled = true; briefBtn.textContent = '生成中...'; }}
  if (sendBtn) sendBtn.disabled = true;
  if (pingBtn) pingBtn.disabled = true;
  aiChatMessages.push({{ role: 'user', content: '[获取简报] ' + inst }});
  renderAiChatMessages();
  aiChatMessages.push({{ role: 'assistant', content: '正在拉取盘面并生成简报...', pending: true }});
  renderAiChatMessages();
  try {{
    await autoSaveConfig();
    const response = await fetch('/api/ai-brief', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ inst_id: inst }}),
      cache: 'no-store'
    }});
    const payload = await response.json();
    aiChatMessages.pop();
    if (!response.ok || payload.ok === false) {{
      aiChatMessages.push({{ role: 'assistant', content: payload.error || payload.message || '简报生成失败', error: true }});
    }} else {{
      aiChatMessages.push({{ role: 'assistant', content: payload.text || ((payload.title || '') + '\\n\\n' + (payload.body || '')), usage: payload.usage || null }});
    }}
  }} catch (error) {{
    aiChatMessages.pop();
    aiChatMessages.push({{ role: 'assistant', content: String(error), error: true }});
  }} finally {{
    if (briefBtn) {{ briefBtn.disabled = false; briefBtn.textContent = '获取简报'; }}
    if (sendBtn) sendBtn.disabled = false;
    if (pingBtn) pingBtn.disabled = false;
    renderAiChatMessages();
  }}
}}
const aiChatSendBtn = document.getElementById('aiChatSendBtn');
if (aiChatSendBtn) aiChatSendBtn.addEventListener('click', function() {{ sendAiChatMessage(); }});
const aiChatInput = document.getElementById('aiChatInput');
if (aiChatInput) aiChatInput.addEventListener('keydown', function(event) {{
  if (event.key === 'Enter' && !event.shiftKey) {{
    event.preventDefault();
    sendAiChatMessage();
  }}
}});
const aiChatClearBtn = document.getElementById('aiChatClearBtn');
if (aiChatClearBtn) aiChatClearBtn.addEventListener('click', function() {{
  aiChatMessages = [];
  renderAiChatMessages();
}});
const aiChatPingBtn = document.getElementById('aiChatPingBtn');
if (aiChatPingBtn) aiChatPingBtn.addEventListener('click', function() {{ runAiChatPing(); }});
const aiChatBriefBtn = document.getElementById('aiChatBriefBtn');
if (aiChatBriefBtn) aiChatBriefBtn.addEventListener('click', function() {{ runAiChatBrief(); }});
renderAiChatMessages();
function parseDownloadFilename(disposition, fallback) {{
  if (!disposition) return fallback;
  const match = /filename\\*?=(?:UTF-8''|")?([^";]+)/i.exec(disposition);
  if (!match) return fallback;
  try {{ return decodeURIComponent(match[1].replace(/"/g, '').trim()); }} catch (e) {{ return match[1].replace(/"/g, '').trim(); }}
}}
async function fetchAccuracyPayloadForExport(scope, inst, horizon, retention, interval) {{
  const qs='inst_id='+encodeURIComponent(inst)+'&horizon='+encodeURIComponent(String(horizon))+'&scope='+encodeURIComponent(scope)+'&retention_hours='+encodeURIComponent(String(retention))+'&interval_seconds='+encodeURIComponent(String(interval))+'&for_diagnostic=1';
  const r=await fetch('/api/accuracy-data?'+qs,{{cache:'no-store'}});
  const p=await r.json();
  if(!r.ok||p.ok===false||!(Array.isArray(p.points)&&p.points.length))return null;
  return p;
}}
async function captureAccuracyChartPng(payload) {{
  const canvas=document.getElementById('accuracyChart');
  if(!canvas||!payload||!Array.isArray(payload.points)||!payload.points.length)return null;
  const saved={{payload:accuracyLivePayload,points:accuracyView.points.slice(),start:accuracyView.start,end:accuracyView.end,yZoom:accuracyView.yZoom,yPan:accuracyView.yPan,selectedKey:accuracyView.selectedKey,followLatest:accuracyView.followLatest,imported:accuracyImportedMode,importedLabel:accuracyImportedLabel,queryKey:accuracyQueryKey}};
  try {{
    setAccuracyImportedMode(false,'');
    accuracyLivePayload=Object.assign({{ok:true}},payload);
    resetAccuracyView();
    syncAccuracyPoints(payload.points,{{resetView:true}});
    drawAccuracyChart();
    return canvas.toDataURL('image/png');
  }} finally {{
    accuracyLivePayload=saved.payload;
    accuracyView.points=saved.points.slice();
    accuracyView.start=saved.start;
    accuracyView.end=saved.end;
    accuracyView.yZoom=saved.yZoom;
    accuracyView.yPan=saved.yPan;
    accuracyView.selectedKey=saved.selectedKey;
    accuracyView.followLatest=saved.followLatest;
    accuracyQueryKey=saved.queryKey;
    setAccuracyImportedMode(saved.imported,saved.importedLabel);
    drawAccuracyChart();
  }}
}}
async function collectDiagnosticAccuracyImages() {{
  const insts=(Array.isArray(configuredMonitorInsts)&&configuredMonitorInsts.length)?configuredMonitorInsts.slice():((monitorInst&&String(monitorInst).trim())?[monitorInst]:[]);
  if(!insts.length)return [];
  const horizon=configuredAccuracyHorizon();
  const retention=(typeof accuracyRetentionHours==='function')?accuracyRetentionHours():{int(DEFAULT_ACCURACY_RETENTION_HOURS)};
  const interval=configuredMonitorInterval();
  const scopes=['session','all'];
  try {{
    const replayInfo=await fetch('/api/replay-status',{{cache:'no-store'}}).then(r=>r.json());
    if(replayInfo&&Number(replayInfo.analysis_log_bytes||0)>0)scopes.push('replay');
  }} catch(e) {{}}
  const images=[];
  for(const inst of insts) {{
    for(const scope of scopes) {{
      try {{
        const payload=await fetchAccuracyPayloadForExport(scope,inst,horizon,retention,interval);
        if(!payload)continue;
        const png=await captureAccuracyChartPng(payload);
        if(!png)continue;
        images.push({{name:scope+'_'+String(inst).replace(/\\//g,'_')+'_h'+horizon+'.png',data:png}});
      }} catch(e) {{}}
    }}
  }}
  return images;
}}
function captureMonitorChartPng() {{
  const canvas=document.getElementById('monitorChart');
  if(!canvas||!isMonitorChartEnabled())return '';
  try {{
    const series=currentMonitorSeries();
    if(!Array.isArray(series)||!series.length)return '';
    return canvas.toDataURL('image/png');
  }} catch(e) {{ return ''; }}
}}
async function exportDiagnosticBundle() {{
  const btn = document.getElementById('diagnosticExportBtn');
  const hint = document.getElementById('diagnosticExportHint');
  const label = '导出诊断包';
  if (btn) {{ btn.disabled = true; btn.textContent = '打包中...'; }}
  if (hint) hint.textContent = '正在收集日志、回放数据与压测走势图，请稍候...';
  const body = {{}};
  const chat = aiChatMessages.filter(function(item) {{
    return !item.pending && (item.role === 'user' || item.role === 'assistant');
  }}).map(function(item) {{
    return {{ role: item.role, content: item.content, usage: item.usage || null, error: !!item.error }};
  }});
  if (chat.length) body.ai_chat = chat;
  try {{
    if (typeof buildAccuracyExportBundle === 'function' && accuracyLivePayload && accuracyView.points && accuracyView.points.length) {{
      body.accuracy_snapshot = buildAccuracyExportBundle();
    }}
  }} catch (e) {{}}
  try {{
    const images = await collectDiagnosticAccuracyImages();
    if (images.length) body.accuracy_images = images;
  }} catch (e) {{}}
  try {{
    const monitorPng = captureMonitorChartPng();
    if (monitorPng) body.monitor_chart_png = monitorPng;
  }} catch (e) {{}}
  try {{
    const response = await fetch('/api/diagnostic-export', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify(body),
      cache: 'no-store'
    }});
    const ct = (response.headers.get('content-type') || '').toLowerCase();
    if (!response.ok || ct.includes('application/json')) {{
      let message = '导出失败';
      try {{
        const payload = await response.json();
        message = payload.error || payload.message || message;
      }} catch (e) {{}}
      throw new Error(message);
    }}
    const blob = await response.blob();
    const fallback = 'okx_diagnostic_' + new Date().toISOString().slice(0, 19).replace(/[-:T]/g, '').replace(' ', '') + '.zip';
    const filename = parseDownloadFilename(response.headers.get('Content-Disposition'), fallback);
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = filename;
    link.click();
    URL.revokeObjectURL(url);
    if (hint) hint.textContent = '已下载 ' + filename + '（约 ' + Math.max(1, Math.round(blob.size / 1024)) + ' KB）。请将此 ZIP 发给维护人员分析。';
  }} catch (error) {{
    if (hint) hint.textContent = '导出失败：' + error;
    alert('导出诊断包失败：' + error);
  }} finally {{
    if (btn) {{ btn.disabled = false; btn.textContent = label; }}
  }}
}}
const diagnosticExportBtn = document.getElementById('diagnosticExportBtn');
if (diagnosticExportBtn) diagnosticExportBtn.addEventListener('click', function() {{ exportDiagnosticBundle(); }});
const testPushBtn = document.getElementById('testPushBtn');
if (testPushBtn) {{
  testPushBtn.addEventListener('click', function() {{ runConnectivityTest(); }});
}}
const refreshLogBtn = document.getElementById('refreshLogBtn');
if (refreshLogBtn) refreshLogBtn.addEventListener('click', function() {{ setLogsDisplayEnabled(true, {{ fetchNow: true }}); }});
const clearLogBtn = document.getElementById('clearLogBtn');
if (clearLogBtn) {{
  clearLogBtn.addEventListener('click', function() {{
    const box = document.getElementById('logWindow');
    if (box) box.value = '';
    logCleared = true;
  }});
}}
const clearConsoleLogBtn = document.getElementById('clearConsoleLogBtn');
if (clearConsoleLogBtn) {{
  clearConsoleLogBtn.addEventListener('click', function() {{
    const box = document.getElementById('consoleLogWindow');
    if (box) box.value = '';
    consoleLogCleared = true;
  }});
}}
const openLogDirBtn = document.getElementById('openLogDirBtn');
if (openLogDirBtn) {{
  openLogDirBtn.addEventListener('click', async function() {{
    try {{
      const response = await fetch('/api/open-log-dir', {{ cache: 'no-store' }});
      const payload = await response.json();
      if (!response.ok || payload.ok === false) throw new Error(payload.error || '打开失败');
    }} catch (error) {{
      alert('打开日志目录失败：' + error);
    }}
  }});
}}
async function saveLogText(apiPath,suggestedName,hintId,failLabel){{const response=await fetch(apiPath,{{cache:'no-store'}});const payload=await response.json();const text=payload.text||'';const hint=document.getElementById(hintId);if(window.showSaveFilePicker){{const handle=await showSaveFilePicker({{suggestedName:suggestedName,types:[{{description:'日志文件',accept:{{'text/plain':['.log','.txt','.jsonl']}}}}]}});const writable=await handle.createWritable();await writable.write(text);await writable.close();if(hint)hint.textContent='已另存为：'+(handle.name||suggestedName)+'。浏览器安全限制不会暴露完整本地路径。';return;}}const blob=new Blob([text],{{type:'text/plain;charset=utf-8'}}),url=URL.createObjectURL(blob),link=document.createElement('a');link.href=url;link.download=suggestedName;link.click();URL.revokeObjectURL(url);if(hint)hint.textContent='已触发下载：'+suggestedName+'。';}}
const saveLogBtn = document.getElementById('saveLogBtn');
if (saveLogBtn) {{
  saveLogBtn.addEventListener('click', async function() {{
    try {{
      await saveLogText('/api/logs','okx_signal_analysis.jsonl','saveLogHint','JSON日志');
    }} catch (error) {{
      alert('保存JSON日志失败：' + error);
    }}
  }});
}}
const saveConsoleLogBtn = document.getElementById('saveConsoleLogBtn');
if (saveConsoleLogBtn) {{
  saveConsoleLogBtn.addEventListener('click', async function() {{
    try {{
      await saveLogText('/api/console-logs','signal_monitor_console.log','saveConsoleLogHint','控制台日志');
    }} catch (error) {{
      alert('保存控制台日志失败：' + error);
    }}
  }});
}}
const monitorCanvas=document.getElementById('monitorChart');
if(monitorCanvas){{monitorCanvas.addEventListener('wheel',function(event){{event.preventDefault();const rect=monitorCanvas.getBoundingClientRect(),focus=Math.min(1,Math.max(0,(event.clientX-rect.left)/Math.max(1,rect.width))),span=monitorViewEnd-monitorViewStart,zoom=(event.deltaY<0?0.82:1.22),newSpan=Math.min(1,Math.max(.06,span*zoom)),center=monitorViewStart+span*focus;let ns=center-newSpan*focus,ne=ns+newSpan;if(ns<0){{ne-=ns;ns=0;}}if(ne>1){{ns-=ne-1;ne=1;}}monitorViewStart=Math.max(0,ns);monitorViewEnd=Math.min(1,ne);monitorYZoom=Math.max(.45,Math.min(10,monitorYZoom*(event.deltaY<0?1.14:.88)));redrawMonitorCached();}},{{passive:false}});monitorCanvas.addEventListener('mousedown',function(event){{monitorDrag={{x:event.clientX,y:event.clientY,start:monitorViewStart,end:monitorViewEnd,yPan:monitorYPan,yRange:monitorYRange,moved:false}};monitorCanvas.classList.add('dragging');}});window.addEventListener('mousemove',function(event){{if(!monitorDrag)return;const rect=monitorCanvas.getBoundingClientRect(),span=monitorDrag.end-monitorDrag.start,dx=(event.clientX-monitorDrag.x)/Math.max(1,rect.width),dy=(event.clientY-monitorDrag.y)/Math.max(1,rect.height);if(Math.abs(event.clientX-monitorDrag.x)>3||Math.abs(event.clientY-monitorDrag.y)>3)monitorDrag.moved=true;let ns=monitorDrag.start-dx*span,ne=monitorDrag.end-dx*span;if(ns<0){{ne-=ns;ns=0;}}if(ne>1){{ns-=ne-1;ne=1;}}monitorViewStart=Math.max(0,ns);monitorViewEnd=Math.min(1,ne);monitorYPan=monitorDrag.yPan+dy*Math.max(.01,monitorDrag.yRange||monitorYRange);redrawMonitorCached();}});window.addEventListener('mouseup',function(){{if(monitorDrag){{setTimeout(function(){{monitorDrag=null;}},0);}}monitorCanvas.classList.remove('dragging');}});function selectNearestPoint(event,strict){{if(!monitorPlotPoints.length)return;const rect=monitorCanvas.getBoundingClientRect(),x=event.clientX-rect.left,y=event.clientY-rect.top;let best=null,bestDist=Infinity;monitorPlotPoints.forEach(o=>{{const dx=o.x-x,dy=o.y-y,dist=Math.sqrt(dx*dx+dy*dy);if(dist<bestDist){{best=o;bestDist=dist;}}}});if(best&&bestDist<(strict?42:34)){{monitorSelectedKey=best.point.time||'';redrawMonitorCached();}}else if(!strict){{monitorSelectedKey='';redrawMonitorCached();}}}}monitorCanvas.addEventListener('click',function(event){{if(monitorDrag&&monitorDrag.moved)return;selectNearestPoint(event,false);}});monitorCanvas.addEventListener('dblclick',function(event){{selectNearestPoint(event,true);}});}}
window.addEventListener('resize',()=>{{if(currentPage()==='monitor'&&isMonitorChartEnabled())redrawMonitorCached();else if(currentPage()==='tests')redrawAccuracyChart();}});
setInterval(async()=>{{if(document.hidden||currentPage()!=='monitor')return;const status=await syncMonitorStatus();if(!isMonitorChartEnabled())return;if(status&&status.running){{monitorLiveMode=true;const now=Date.now();if(now-monitorLastTickerAt>=5000){{monitorLastTickerAt=now;fetchMonitor();}}}}else{{monitorLiveMode=false;drawVirtualMonitor();}}}},2000);
setInterval(()=>{{if(document.hidden||currentPage()!=='tests')return;if(replayProgressTimer)return;if(!isAccuracyEnabled())return;refreshReplayInfo().then(function(){{fetchAccuracy({{resetView:false}});}});}},5000);
setInterval(()=>{{if(document.hidden||currentPage()!=='logs')return;if(!isLogsDisplayEnabled())return;refreshLogs(false);refreshConsoleLogs(false);}},5000);window.addEventListener('hashchange',()=>showPage(currentPage()));
function closePowerMenu(){{const menu=document.getElementById('powerMenu'),btn=document.getElementById('powerMenuBtn');if(menu)menu.hidden=true;if(btn)btn.setAttribute('aria-expanded','false');}}
function togglePowerMenu(){{const menu=document.getElementById('powerMenu'),btn=document.getElementById('powerMenuBtn');if(!menu||!btn)return;const next=menu.hidden;menu.hidden=!next;btn.setAttribute('aria-expanded',next?'true':'false');}}
function showPowerOverlay(message){{const overlay=document.createElement('div');overlay.className='power-overlay';overlay.id='powerOverlay';overlay.textContent=message||'正在处理…';document.body.appendChild(overlay);return overlay;}}
async function waitForWebRestart(){{let tries=0,overlay=document.getElementById('powerOverlay');while(tries<120){{tries+=1;await new Promise(function(resolve){{setTimeout(resolve,1000);}});try{{const probe=await fetch('/api/status',{{cache:'no-store'}});if(probe.ok||probe.status===401){{if(overlay)overlay.textContent='服务已恢复，请手动刷新浏览器';return;}}}}catch(err){{}}try{{const loginProbe=await fetch('/login',{{cache:'no-store'}});if(loginProbe.ok){{if(overlay)overlay.textContent='服务已恢复，请手动刷新浏览器';return;}}}}catch(err){{}}}}if(overlay)overlay.textContent='重启超时，请手动刷新浏览器或重新打开控制台';}}
async function runPowerAction(action){{const isRestart=action==='restart';if(!confirm(isRestart?POWER_CONFIRM_RESTART:POWER_CONFIRM_SHUTDOWN))return;closePowerMenu();try{{const response=await fetch('/api/power/'+action,{{method:'POST',cache:'no-store'}});let payload={{}};try{{payload=await response.json();}}catch(err){{payload={{}};}}if(!response.ok||payload.ok===false){{alert(payload.error||(isRestart?'重启失败':'关机失败'));return;}}showPowerOverlay(payload.message||(isRestart?'正在重启 Web 控制台…':'正在关闭 Web 控制台…'));if(isRestart){{waitForWebRestart();}}else{{setTimeout(function(){{try{{window.open('','_self');window.close();}}catch(err){{}}document.body.innerHTML='<div class="power-overlay" style="position:fixed;inset:0;display:grid;place-items:center;background:#0f172a;color:#e2e8f0;font-size:16px;font-weight:650;">'+POWER_SHUTDOWN_DONE+'</div>';}},1200);}}}}catch(err){{if(isRestart){{showPowerOverlay('正在重启 Web 控制台…');waitForWebRestart();}}else{{alert('关机请求失败：'+err);}}}}}}
(function initPowerMenu(){{const btn=document.getElementById('powerMenuBtn'),row=document.querySelector('.sidebar-footer-power'),menu=document.getElementById('powerMenu'),restartBtn=document.getElementById('powerRestartBtn'),shutdownBtn=document.getElementById('powerShutdownBtn');function onPowerRowClick(event){{if(!menu||!row)return;if(menu.contains(event.target))return;if(event.target===restartBtn||event.target===shutdownBtn)return;event.stopPropagation();togglePowerMenu();}}if(row)row.addEventListener('click',onPowerRowClick);if(restartBtn)restartBtn.addEventListener('click',function(event){{event.stopPropagation();runPowerAction('restart');}});if(shutdownBtn)shutdownBtn.addEventListener('click',function(event){{event.stopPropagation();runPowerAction('shutdown');}});document.addEventListener('click',function(event){{if(!menu||menu.hidden)return;if(row&&row.contains(event.target))return;if(menu.contains(event.target))return;closePowerMenu();}});document.addEventListener('keydown',function(event){{if(event.key==='Escape')closePowerMenu();}});}})();
syncMonitorStatus();showPage(currentPage());
</script></body></html>"""
    return body.encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    _request_slots = threading.BoundedSemaphore(WEB_MAX_CONCURRENT_REQUESTS)

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def handle(self) -> None:
        acquired = self._request_slots.acquire(timeout=30)
        if not acquired:
            try:
                self.send_error(503, "Server busy")
            except Exception:
                pass
            return
        try:
            super().handle()
        finally:
            self._request_slots.release()

    def is_authenticated(self) -> bool:
        token = parse_cookies(self.headers.get("Cookie", "")).get("okx_ai_session")
        return is_valid_session(token)

    def send_html(self, content: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def send_json(self, payload: Dict[str, Any], status: int = 200, cookie: str = "") -> None:
        content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(content)

    def send_bytes(
        self,
        content: bytes,
        content_type: str,
        *,
        filename: str = "",
        status: int = 200,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(content)

    def redirect(self, location: str, cookie: str = "") -> None:
        self.send_response(302)
        self.send_header("Location", location)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()

    def require_auth(self, path: str) -> bool:
        if self.is_authenticated():
            return True
        if path.startswith("/api/"):
            self.send_json({"ok": False, "error": "登录已失效，请刷新页面重新登录。"}, status=401)
        else:
            self.redirect("/login")
        return False

    def send_asset(self, request_path: str) -> None:
        name = urllib.parse.unquote(request_path.removeprefix("/web-assets/"))
        asset_path = (ASSETS_DIR / name).resolve()
        try:
            asset_path.relative_to(ASSETS_DIR.resolve())
        except ValueError:
            self.send_error(403)
            return
        if not asset_path.is_file():
            self.send_error(404)
            return
        content = asset_path.read_bytes()
        content_type = mimetypes.guess_type(str(asset_path))[0] or "application/octet-stream"
        if asset_path.suffix.lower() == ".webm":
            content_type = "video/webm"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if path.startswith("/web-assets/"):
            self.send_asset(path)
            return
        if path == "/login":
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if params.get("auth_changed", [""])[0] == "1":
                self.send_html(
                    render_login(
                        "登录账号已更新，监控与回放已停止。请使用新用户名和密码登录。",
                        success=True,
                    )
                )
            elif params.get("factory_reset", [""])[0] == "1":
                self.send_html(
                    render_login(
                        "已恢复出厂设置。请使用默认账号 admin / admin123 登录。",
                        success=True,
                    )
                )
            else:
                self.send_html(render_login())
            return
        if path == "/logout":
            token = parse_cookies(self.headers.get("Cookie", "")).get("okx_ai_session")
            revoke_session(token)
            self.redirect("/login", "okx_ai_session=; Path=/; Max-Age=0; HttpOnly")
            return
        if not self.require_auth(path):
            return
        if path == "/":
            self.send_html(render_page())
        elif path == "/save-auth":
            self.redirect("/#settings")
        elif path == "/api/status":
            self.send_json(monitor_status())
        elif path == "/api/validate-inst-ids":
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            inst_id = normalize_inst_id(params.get("inst_id", [""])[0])
            if inst_id:
                candidate = [inst_id]
            else:
                candidate = parse_inst_ids_from_form(
                    {
                        "inst_ids": params.get("inst_ids", []),
                        "custom_inst_ids": params.get("custom_inst_ids", []),
                    }
                )
            if not candidate:
                self.send_json(
                    {
                        "ok": False,
                        "error": "请输入或选择一个合约 ID。",
                        "valid": [],
                        "errors": [],
                    },
                    status=400,
                )
                return
            valid, errors = validate_inst_ids(candidate)
            if errors:
                self.send_json(
                    {
                        "ok": False,
                        "error": format_inst_id_errors(errors),
                        "valid": valid,
                        "errors": errors,
                    },
                    status=400,
                )
                return
            self.send_json({"ok": True, "valid": valid, "errors": [], "inst_id": valid[0] if len(valid) == 1 else ""})
        elif path == "/api/monitor-candles":
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            inst_id = params.get("inst_id", ["BTC-USDT-SWAP"])[0]
            bar = params.get("bar", ["1m"])[0]
            configured = configured_instruments()
            if inst_id not in configured:
                self.send_json({"ok": False, "error": f"{inst_id} 未在配置中启用，不能读取 K 线。", "configured": configured}, status=400)
                return
            try:
                cache_key = (
                    f"monitor-candles:{inst_id}:{bar}:"
                    f"{int(monitor_status()['running'])}:{MONITOR_LOG_START_AT}:"
                    f"{log_file_cache_token(MONITOR_JSON_LOG_FILE)}"
                )
                self.send_json(
                    cached_api_response(cache_key, lambda: read_monitor_candles(inst_id, bar))
                )
            except ValueError as exc:
                self.send_json({"ok": False, "error": str(exc)}, status=400)
        elif path == "/api/monitor-data":
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            inst_id = params.get("inst_id", ["BTC-USDT-SWAP"])[0]
            configured = configured_instruments()
            if inst_id not in configured:
                self.send_json({"ok": False, "error": f"{inst_id} 未在配置中启用，不能读取监控数据。", "configured": configured}, status=400)
                return
            self.send_json(read_monitor_points(inst_id))
        elif path == "/api/ticker":
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            inst_id = params.get("inst_id", ["BTC-USDT-SWAP"])[0]
            configured = configured_instruments()
            if inst_id not in configured:
                self.send_json({"ok": False, "error": f"{inst_id} 未在配置中启用。", "configured": configured}, status=400)
                return
            try:
                self.send_json(get_quick_ticker(inst_id))
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, status=502)
        elif path == "/api/logs":
            cfg = load_config()
            self.send_json({
                "text": monitor_log_text(),
                "running": bool(monitor_status()["running"]),
                "enabled": True,
                "log_size_summary": log_size_summary_text(cfg),
                "log_max_mb": cfg.get("log_max_mb"),
                "log_total_max_mb": cfg.get("log_total_max_mb"),
                "default_path": str(MONITOR_JSON_LOG_FILE),
                "start_at": MONITOR_LOG_START_AT,
            })
        elif path == "/api/console-logs":
            cfg = load_config()
            self.send_json({
                "text": monitor_console_log_text(),
                "running": bool(monitor_status()["running"]),
                "enabled": True,
                "log_size_summary": log_size_summary_text(cfg),
                "default_path": str(MONITOR_PROCESS_LOG_FILE),
                "start_at": MONITOR_LOG_START_AT,
            })
        elif path == "/api/replay-build-status":
            self.send_json({"ok": True, **replay_build_status_snapshot()})
        elif path == "/api/replay-dataset":
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            lite = params.get("lite", ["0"])[0] in ("1", "true", "yes")
            self.send_json({"ok": True, **replay_dataset_info(lite=lite)})
        elif path == "/api/replay-status":
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            lite = params.get("lite", ["1"])[0] in ("1", "true", "yes")
            if lite:
                self.send_json({"ok": True, **replay_dataset_info(lite=True)})
            else:
                self.send_json({"ok": True, **replay_status()})
        elif path == "/api/accuracy-data":
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            inst_id = params.get("inst_id", ["BTC-USDT-SWAP"])[0]
            horizon = int(params.get("horizon", ["5"])[0])
            scope = params.get("scope", ["session"])[0]
            retention_hours = float(params.get("retention_hours", [str(int(DEFAULT_ACCURACY_RETENTION_HOURS))])[0])
            interval_seconds = int(params.get("interval_seconds", [str(load_config().get("interval", 5))])[0])
            max_points = accuracy_chart_max_points(retention_hours, interval_seconds)
            if "max_points" in params:
                max_points = max(100, min(50000, int(params.get("max_points", [str(max_points)])[0])))
            for_diagnostic = params.get("for_diagnostic", ["0"])[0] in ("1", "true", "yes")
            try:
                accuracy_log_path = REPLAY_ANALYSIS_LOG_FILE if scope == "replay" else MONITOR_JSON_LOG_FILE
                cache_key = (
                    f"accuracy:{inst_id}:{horizon}:{scope}:{retention_hours}:"
                    f"{interval_seconds}:{max_points}:"
                    f"{MONITOR_LOG_START_AT}:{REPLAY_LOG_START_AT}:"
                    f"{log_file_cache_token(accuracy_log_path)}:"
                    f"diag:{for_diagnostic}"
                )
                self.send_json(
                    cached_api_response(
                        cache_key,
                        lambda: accuracy_report(
                            inst_id,
                            horizon,
                            max_points=max_points,
                            scope=scope,
                            retention_hours=retention_hours,
                            interval_seconds=interval_seconds,
                            for_diagnostic=for_diagnostic,
                        ),
                    )
                )
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, status=400)
        elif path == "/api/open-log-dir":
            try:
                self.send_json({"ok": True, "message": open_log_dir(), "path": str(LOG_DIR)})
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc), "path": str(LOG_DIR)}, status=500)
        elif path == "/api/test-ai":
            result = test_ai_connection()
            self.send_json(result, status=200 if result.get("ok") else 400)
        elif path == "/api/test-push":
            result = test_push_connection()
            self.send_json(result, status=200 if result.get("ok") else 400)
        elif path == "/api/diagnostic-export":
            try:
                content, filename, manifest = create_diagnostic_zip()
                self.send_bytes(content, "application/zip", filename=filename)
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, status=500)
        elif path == "/config-json":
            content = f"<pre>{esc(active_config_file().read_text(encoding='utf-8-sig'))}</pre>".encode("utf-8")
            self.send_html(content)
        elif path == "/start":
            start_monitor()
            self.redirect("/#monitor")
        elif path == "/stop":
            stop_monitor()
            self.redirect("/#monitor")
        elif path == "/test-ai":
            result = test_ai_connection()
            self.send_html(render_page(result["message"]))
        elif path == "/test-push":
            result = test_push_connection()
            self.send_html(render_page(result["message"]))
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8")
        if path == "/api/tray/shutdown":
            client_host = self.client_address[0]
            if client_host not in ("127.0.0.1", "::1"):
                self.send_json({"ok": False, "error": "forbidden"}, status=403)
                return
            self.send_json(request_power_shutdown())
            return
        if path == "/api/config/import":
            if not self.require_auth(path):
                return
            try:
                import_config_bundle(json.loads(raw or "{}"))
                self.send_json({"ok": True})
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, status=400)
            return
        if path == "/api/factory-reset":
            if not self.require_auth(path):
                return
            try:
                payload = json.loads(raw or "{}") if raw else {}
                password = str(payload.get("password", "")).strip()
                token = parse_cookies(self.headers.get("Cookie", "")).get("okx_ai_session")
                result = perform_factory_reset(password)
                clear_session_token(token)
                self.send_json(
                    result,
                    cookie="okx_ai_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax",
                )
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, status=400)
            return
        if path == "/api/config/reset-defaults":
            if not self.require_auth(path):
                return
            try:
                saved_path, config_changed = restore_factory_default_config()
                running = bool(monitor_status()["running"])
                requires_restart = config_changed and running
                restart_hint = ""
                if requires_restart:
                    restart_hint = "已恢复默认配置。监控仍在运行，请停止后重新「开始监控」以生效。"
                elif config_changed:
                    restart_hint = "已恢复默认配置；下次启动监控时生效。"
                else:
                    restart_hint = "已恢复默认配置。"
                self.send_json(
                    {
                        "ok": True,
                        "path": str(saved_path),
                        "inst_ids": configured_instruments(),
                        "config_changed": config_changed,
                        "requires_monitor_restart": requires_restart,
                        "restart_hint": restart_hint,
                    }
                )
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, status=400)
            return
        form = {key: values if len(values) > 1 else values[0] for key, values in urllib.parse.parse_qs(raw).items()}
        if path == "/login":
            auth = load_auth()
            if form.get("username") == auth.get("username") and form.get("password") == auth.get("password"):
                token = secrets.token_urlsafe(32)
                register_session(token)
                self.redirect("/", f"okx_ai_session={token}; Path=/; HttpOnly; SameSite=Lax")
            else:
                self.send_html(render_login("用户名或密码错误。"))
            return
        if path.startswith("/api/"):
            if not self.require_auth(path):
                return
        elif not self.is_authenticated():
            self.redirect("/login")
            return
        if path == "/api/config/export":
            try:
                update_from_form(form)
                self.send_json({"ok": True, "bundle": export_config_bundle()})
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, status=400)
            return
        if path == "/api/add-inst-id":
            try:
                config = load_config()
                inst_id = validate_single_inst_id(
                    str(form.get("inst_id", "")),
                    known_inst_ids=visible_inst_pool(config),
                )
                self.send_json({"ok": True, "inst_id": inst_id, "is_preset": inst_id in PRESET_INSTRUMENTS})
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, status=400)
            return
        if path == "/api/power/restart":
            self.send_json(request_power_restart())
            return
        if path == "/api/power/shutdown":
            self.send_json(request_power_shutdown())
            return
        if path == "/api/config/save":
            try:
                saved_path, _, config_changed = update_from_form(form)
                running = bool(monitor_status()["running"])
                requires_restart = config_changed and running
                restart_hint = ""
                if requires_restart:
                    restart_hint = "监控仍在运行且配置已变更，请停止后重新「开始监控」以生效。"
                elif config_changed:
                    restart_hint = "配置已保存；下次启动监控时生效。"
                self.send_json(
                    {
                        "ok": True,
                        "path": str(saved_path),
                        "inst_ids": configured_instruments(),
                        "config_changed": config_changed,
                        "requires_monitor_restart": requires_restart,
                        "restart_hint": restart_hint,
                    }
                )
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, status=400)
            return
        if path == "/api/save-auth":
            try:
                message, saved_path = update_auth_from_form(form)
                token = parse_cookies(self.headers.get("Cookie", "")).get("okx_ai_session")
                clear_session_token(token)
                self.send_json(
                    {
                        "ok": True,
                        "message": message,
                        "path": str(saved_path),
                        "redirect": "/login?auth_changed=1",
                    },
                    cookie="okx_ai_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax",
                )
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, status=400)
            return
        if path == "/api/replay-build-historical":
            try:
                payload = json.loads(raw or "{}")
                result = start_historical_replay_build(payload)
                self.send_json(result, status=200 if result.get("ok") else 400)
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, status=400)
            return
        if path == "/api/replay-start":
            try:
                payload = json.loads(raw or "{}")
                replay_interval = float(payload.get("interval", 0))
                dataset_path = resolve_replay_dataset_path(str(payload.get("dataset", "recorded")))
                message = start_replay(replay_interval, dataset_path)
                ok = "已启动" in message
                body = {"ok": ok, "message": message, **replay_dataset_info(lite=True)}
                if not ok:
                    body["error"] = message
                self.send_json(body, status=200 if ok else 400)
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, status=400)
            return
        if path == "/api/monitor-start":
            if not self.require_auth(path):
                return
            try:
                payload = monitor_start_payload()
                self.send_json(payload, status=200 if payload.get("ok") else 400)
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, status=500)
            return
        if path == "/api/monitor-stop":
            if not self.require_auth(path):
                return
            try:
                payload = monitor_stop_payload()
                self.send_json(payload, status=200 if payload.get("ok") else 400)
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, status=500)
            return
        if path == "/api/replay-stop":
            try:
                message = stop_replay()
                ok = "已停止" in message or "未运行" in message
                self.send_json({"ok": ok, "message": message, **replay_dataset_info(lite=True)}, status=200 if ok else 400)
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, status=400)
            return
        if path == "/api/ai-chat":
            try:
                payload = json.loads(raw or "{}")
                result = call_ai_chat(
                    str(payload.get("message", "")),
                    payload.get("history") if isinstance(payload.get("history"), list) else None,
                )
                self.send_json(result, status=200 if result.get("ok") else 400)
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, status=400)
            return
        if path == "/api/ai-brief":
            try:
                payload = json.loads(raw or "{}")
                inst_id = str(payload.get("inst_id", "") or "").strip()
                result = fetch_manual_brief(inst_id)
                self.send_json(result, status=200 if result.get("ok") else 400)
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, status=400)
            return
        if path == "/api/diagnostic-export":
            try:
                payload = json.loads(raw or "{}") if raw else {}
                extras = {}
                if isinstance(payload.get("ai_chat"), list):
                    extras["ai_chat"] = payload["ai_chat"]
                if isinstance(payload.get("accuracy_snapshot"), dict):
                    extras["accuracy_snapshot"] = payload["accuracy_snapshot"]
                if isinstance(payload.get("accuracy_images"), list):
                    extras["accuracy_images"] = payload["accuracy_images"]
                if isinstance(payload.get("monitor_chart_png"), str):
                    extras["monitor_chart_png"] = payload["monitor_chart_png"]
                content, filename, manifest = create_diagnostic_zip(extras)
                self.send_bytes(content, "application/zip", filename=filename)
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, status=500)
            return
        if path == "/save-auth":
            try:
                update_auth_from_form(form)
                token = parse_cookies(self.headers.get("Cookie", "")).get("okx_ai_session")
                clear_session_token(token)
                self.redirect("/login?auth_changed=1", "okx_ai_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax")
            except Exception as exc:
                self.send_html(render_page(f"保存账号失败：{exc}"))
            return
        try:
            _, env_path, config_changed = update_from_form(form)
        except Exception as exc:
            self.send_html(render_page(f"保存失败：{exc}"))
            return
        if path == "/save-and-run":
            self.send_html(render_page(f"配置已保存，{start_monitor()}。"))
        else:
            note = "" if env_path == PORTABLE_ENV_FILE else f" 密钥已保存到用户目录：{env_path}"
            restart_note = " 监控若已在运行，请重新启动以加载新配置。" if config_changed else ""
            self.send_html(render_page(f"配置已保存。{note}{restart_note}"))


class PanelHTTPServer(ThreadingHTTPServer):
    daemon_threads = True


def main() -> int:
    global WEB_SERVER
    cleanup_stale_child_processes()
    restore_restart_sessions()
    try:
        server = PanelHTTPServer((HOST, PORT), Handler)
    except OSError as exc:
        print(f"配置页面启动失败: {exc}")
        print(f"可能是端口 {PORT} 被占用。可以设置 WEB_CONTROL_PANEL_PORT 后重试。")
        return 1
    WEB_SERVER = server
    url = f"http://{HOST}:{PORT}"
    print(f"配置页面已启动: {url}")
    print("如果浏览器没有自动打开，请手动访问该地址；按 Ctrl+C 停止。")
    if HOST in ("127.0.0.1", "localhost") and should_auto_open_browser():
        browser_timer = threading.Timer(0.6, lambda: webbrowser.open(url))
        browser_timer.daemon = True
        browser_timer.start()
    stopped_by_user = False

    def _request_stop(signum: int, frame: Any) -> None:
        nonlocal stopped_by_user
        if stopped_by_user:
            return
        stopped_by_user = True
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, _request_stop)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _request_stop)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        stopped_by_user = True
    finally:
        server.server_close()
        WEB_SERVER = None
    if stopped_by_user:
        try:
            stop_all_background_services(fast=True)
        except Exception:
            pass
        print("\n配置页面已停止。")
        os._exit(130)
    if WEB_POWER_RESTART:
        print("\n配置页面正在重启…")
        os._exit(EXIT_CODE_TRAY_RESTART if launched_by_tray() else 0)
    if WEB_POWER_SHUTDOWN:
        print("\n配置页面已关闭。")
        os._exit(EXIT_CODE_TRAY_SHUTDOWN if launched_by_tray() else 0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
