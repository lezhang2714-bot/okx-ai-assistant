#!/usr/bin/env python3
"""
Browser based local control panel for OKX AI Assistant.

This file intentionally uses only Python standard library modules so the
Windows deployment can start the UI without installing a web framework.
"""

import html
import json
import mimetypes
import os
import secrets
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

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
except Exception:
    REPLAY_DATASET_FILE = LOG_DIR / "replay_dataset.jsonl"
    REPLAY_LOG_FILE = LOG_DIR / "replay_analysis.jsonl"
    DEFAULT_LOG_MAX_BYTES = 500 * 1024 * 1024

    def replay_dataset_stats(path):  # type: ignore
        return {"exists": False, "lines": 0, "bytes": 0}

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
HOST = os.getenv("WEB_CONTROL_PANEL_HOST", "127.0.0.1")
PORT = int(os.getenv("WEB_CONTROL_PANEL_PORT", "8765"))
SUPPORTED_INSTRUMENTS = ("BTC-USDT-SWAP", "ETH-USDT-SWAP")
OKX_BASE_URL = "https://www.okx.com"

SESSIONS = set()
MONITOR_PROCESS: subprocess.Popen = None
MONITOR_STARTED_AT = ""
MONITOR_LOG_START_AT = ""
MONITOR_STOPPED_AT = ""
REPLAY_PROCESS: subprocess.Popen = None
REPLAY_STARTED_AT = ""
REPLAY_LOG_START_AT = ""
REPLAY_STOPPED_AT = ""
_DATASET_STATS_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_LOG_LINE_COUNT_CACHE: Dict[str, Tuple[float, int]] = {}

FIXED_MONITOR_RUNTIME = 0
FIXED_OKX_FLAG = "0"
LEGACY_CONFIG_KEYS = ("runtime", "flag")


SUGGESTED_PUSH_SCORES = {
    "conservative": {"push_score": 80, "watch_push_score": 72, "spike_push_score": 68},
    "standard": {"push_score": 75, "watch_push_score": 65, "spike_push_score": 62},
    "aggressive": {"push_score": 70, "watch_push_score": 62, "spike_push_score": 58},
}

RISK_PREFERENCE_LABELS = {
    "conservative": "保守",
    "standard": "标准",
    "aggressive": "激进",
}

CONFIG_FIELDS = [
    ("基础运行", "interval", "number", "轮询间隔(秒)", "默认5秒执行一轮监控。"),
    ("基础运行", "analysis_log_enabled", "checkbox", "写入分析日志", "开启后每轮写入 JSON 分析日志与控制台摘要；关闭可减磁盘与 Web 轮询开销。监控指标叠加与 live 压测依赖此项。"),
    ("基础运行", "record_replay_enabled", "checkbox", "录制回放数据集", "监控运行时把每轮 collect_snapshot 原始输入写入 replay_dataset.jsonl，供离线回放压测。"),
    ("策略", "strategy_mode", "strategy_choice", "策略周期", "超短线抓5-10分钟脉冲；短线需5m+15m profile 同向或20分钟延伸；中线看1H/15m/4H结构。会切换本地主路径与 final_direction。"),
    ("策略", "risk_preference", "risk_choice", "确认严格度", "保守：更高动量阈值与确认分(88)；标准：默认；激进：更低阈值(65)且短线可凭短窗压力给方向。变更后可在下方推送说明中一键填入建议分数。"),
    ("AI与推送", "ai_enabled", "checkbox", "启用AI分析", "L2/L3 触发后调用 AI 做主决策。"),
    ("AI与推送", "push_enabled", "checkbox", "启用微信推送", "final_decision 过门槛后通过 Server酱 推送。"),
    ("AI与推送", "push_score", "number", "交易推送门槛(trade)", "direction 为做多/做空且 confidence ≥ 此值时可推 trade。建议见下方「推送分数建议」。"),
    ("AI与推送", "watch_push_score", "number", "观察推送门槛(watch)", "观望或风险提示类 watch 推送。建议见下方「推送分数建议」。"),
    ("AI与推送", "spike_push_score", "number", "异动推送门槛(spike)", "超短线急速异动 L3 / spike 推送。建议见下方「推送分数建议」。"),
]

ENV_DEFAULTS = {
    "AI_MODEL": "gpt-5.5",
    "AI_BASE_URL": "https://www.right.codes/codex/v1",
}

ENV_FIELDS = [
    ("OPENAI_API_KEY", "AI API Key", "OpenAI 兼容接口密钥，适用于各类模型。"),
    ("AI_MODEL", "AI模型", "默认gpt-5.5，可改为更便宜模型。"),
    ("AI_BASE_URL", "AI Base URL", "OpenAI兼容接口地址，默认 https://www.right.codes/codex/v1。"),
    ("WECHAT_SEND_KEY", "微信推送 SendKey", "Server酱 SendKey，用于推送到个人微信。在 https://sct.ftqq.com 获取。"),
]


def default_config() -> Dict[str, Any]:
    return {
        "inst_ids": ["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
        "interval": 5,
        "analysis_log_enabled": False,
        "record_replay_enabled": False,
        "strategy_mode": "short",
        "risk_preference": "standard",
        "signal_trade_enabled": True,
        "signal_watch_enabled": True,
        "signal_spike_enabled": True,
        "ai_output_style": "steady",
        "allow_scalp_trade": False,
        "allow_counter_4h_scalp": False,
        "allow_oi_divergence_momentum": False,
        "scalp_move_pct_5m": 0.22,
        "scalp_move_pct_10m": 0.35,
        "ai_enabled": False,
        "dry_run_ai": False,
        "push_enabled": False,
        "push_score": 75,
        "watch_push_score": 65,
        "spike_push_score": 62,
        "volume_multiplier": 2.0,
        "oi_change_pct_15m": 5.0,
        "funding_abs_threshold": 0.0008,
        "funding_change_threshold": 0.0003,
        "long_short_extreme": 0.75,
        "retry_times": 3,
        "retry_backoff": 1.5,
        "push_cooldown_seconds": 900,
        "log_max_bytes": DEFAULT_LOG_MAX_BYTES,
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
    keys = {"inst_ids"}
    for _, key, _, _, _ in CONFIG_FIELDS:
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
    return (
        f"当前严格度「{label}」建议 trade {suggested['push_score']} · "
        f"watch {suggested['watch_push_score']} · spike {suggested['spike_push_score']}。"
        f"{ai_note} watch/spike 应低于 trade，避免噪音推送。"
    )


def derive_ai_output_style(config: Dict[str, Any]) -> str:
    risk = str(config.get("risk_preference", "standard") or "standard")
    mode = str(config.get("strategy_mode", "short") or "short")
    if risk == "aggressive":
        return "momentum"
    if mode == "swing":
        return "trend"
    return "steady"


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
    if "watch_push_score" not in loaded:
        merged["watch_push_score"] = suggested["watch_push_score"]
    if "spike_push_score" not in loaded:
        merged["spike_push_score"] = suggested["spike_push_score"]
    if "push_score" not in loaded:
        merged["push_score"] = suggested["push_score"]
    merged["push_score"] = max(0, min(100, int(merged.get("push_score", suggested["push_score"]))))
    merged["watch_push_score"] = max(0, min(100, int(merged.get("watch_push_score", suggested["watch_push_score"]))))
    merged["spike_push_score"] = max(0, min(100, int(merged.get("spike_push_score", suggested["spike_push_score"]))))
    merged["allow_scalp_trade"] = merged.get("strategy_mode") == "scalp" or bool(merged.get("allow_scalp_trade"))
    return strip_legacy_config_keys(merged)


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
    inst_ids = load_config().get("inst_ids", [])
    if isinstance(inst_ids, str):
        inst_ids = [inst_ids]
    return [inst for inst in inst_ids if inst in SUPPORTED_INSTRUMENTS]


def analysis_log_enabled() -> bool:
    return bool(load_config().get("analysis_log_enabled", False))


def save_config(config: Dict[str, Any]) -> Path:
    normalized = normalize_config(config)
    visible = visible_config_keys()
    to_save = {key: normalized[key] for key in visible if key in normalized}
    text = json.dumps(to_save, indent=2, ensure_ascii=False) + "\n"
    try:
        PORTABLE_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        PORTABLE_CONFIG_FILE.write_text(text, encoding="utf-8")
        if USER_CONFIG_FILE.exists():
            USER_CONFIG_FILE.write_text(text, encoding="utf-8")
        return PORTABLE_CONFIG_FILE
    except PermissionError:
        USER_STATE_DIR.mkdir(parents=True, exist_ok=True)
        USER_CONFIG_FILE.write_text(text, encoding="utf-8")
        return USER_CONFIG_FILE


def load_auth() -> Dict[str, str]:
    migrate_legacy_build_state()
    for path in (PORTABLE_AUTH_FILE, USER_AUTH_FILE, DEFAULT_AUTH_FILE):
        if path.exists():
            return load_json(path, {"username": "admin", "password": "admin123"})
    save_auth("admin", "admin123")
    return {"username": "admin", "password": "admin123"}


def save_auth(username: str, password: str) -> Path:
    data = {"username": username.strip() or "admin", "password": password.strip() or "admin123"}
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    try:
        PORTABLE_AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
        PORTABLE_AUTH_FILE.write_text(text, encoding="utf-8")
        return PORTABLE_AUTH_FILE
    except PermissionError:
        USER_STATE_DIR.mkdir(parents=True, exist_ok=True)
        USER_AUTH_FILE.write_text(text, encoding="utf-8")
        return USER_AUTH_FILE


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


def update_from_form(form: Dict[str, Any]) -> Path:
    config = load_config()
    env = load_env()
    inst_ids = form.get("inst_ids", [])
    if isinstance(inst_ids, str):
        inst_ids = [inst_ids]
    config["inst_ids"] = [item for item in inst_ids if item in SUPPORTED_INSTRUMENTS]
    if not config["inst_ids"]:
        raise ValueError("至少选择一个监控币种")

    for _, key, kind, _, _ in CONFIG_FIELDS:
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
    save_config(config)
    return save_env(env)


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
    if not isinstance(config, dict):
        raise ValueError("配置文件格式错误")
    save_config(config)
    if isinstance(env, dict):
        save_env(env)


def build_child_env() -> Dict[str, str]:
    child_env = os.environ.copy()
    child_env["PYTHONUNBUFFERED"] = "1"
    for key, value in load_env().items():
        child_env[key] = value
    return child_env


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
        str(config.get("push_score", 80)),
        "--retry-times",
        str(config.get("retry_times", 3)),
        "--retry-backoff",
        str(config.get("retry_backoff", 1.5)),
        "--push-cooldown",
        str(config.get("push_cooldown_seconds", 900)),
        "--log-max-bytes",
        str(config.get("log_max_bytes", DEFAULT_LOG_MAX_BYTES)),
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
        str(config.get("watch_push_score", 65)),
        "--spike-push-score",
        str(config.get("spike_push_score", 62)),
    ]
    args.append("--trade-signals" if config.get("signal_trade_enabled", True) else "--no-trade-signals")
    args.append("--watch-signals" if config.get("signal_watch_enabled", True) else "--no-watch-signals")
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
    if config.get("record_replay_enabled"):
        args.extend(["--record-replay", "--record-replay-file", str(REPLAY_DATASET_FILE)])
    args.append("--analysis-log" if config.get("analysis_log_enabled") else "--no-analysis-log")
    return args


def build_replay_args(config: Dict[str, Any], replay_interval: float, dataset_path: Path = None) -> List[str]:
    args = build_monitor_args(config)
    args = [arg for arg in args if arg not in ("--push", "--ai") and arg != "--dry-run-ai"]
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
    if REPLAY_ANALYSIS_LOG_FILE.exists():
        REPLAY_ANALYSIS_LOG_FILE.unlink()
    _LOG_LINE_COUNT_CACHE.pop(str(REPLAY_ANALYSIS_LOG_FILE), None)
    log_file = REPLAY_PROCESS_LOG_FILE.open("a", encoding="utf-8")
    log_file.write(f"\n===== replay started at {REPLAY_STARTED_AT} dataset={dataset} =====\n")
    log_file.flush()
    config = load_config()
    config["push_enabled"] = False
    config["ai_enabled"] = False
    REPLAY_PROCESS = subprocess.Popen(
        build_replay_args(config, replay_interval, dataset),
        cwd=str(SCRIPT_DIR),
        env=build_child_env(),
        stdout=log_file,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    return f"回放已启动，PID={REPLAY_PROCESS.pid}，数据集 {stats.get('frame_count')} 帧"


def stop_replay() -> str:
    global REPLAY_PROCESS, REPLAY_STOPPED_AT
    status = replay_status()
    if not status["running"]:
        return "回放未运行。"
    REPLAY_PROCESS.terminate()
    try:
        REPLAY_PROCESS.wait(timeout=8)
    except subprocess.TimeoutExpired:
        REPLAY_PROCESS.kill()
        REPLAY_PROCESS.wait(timeout=5)
    REPLAY_STOPPED_AT = time.strftime("%Y-%m-%d %H:%M:%S")
    REPLAY_PROCESS = None
    try:
        with REPLAY_PROCESS_LOG_FILE.open("a", encoding="utf-8") as log_file:
            log_file.write(f"\n===== replay stopped at {REPLAY_STOPPED_AT} =====\n")
    except OSError:
        pass
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


def replay_dataset_info(*, lite: bool = False) -> Dict[str, Any]:
    stats = cached_replay_dataset_stats(REPLAY_DATASET_FILE)
    status = replay_status()
    config = load_config()
    running = bool(status["running"])
    line_count = count_nonempty_lines(REPLAY_ANALYSIS_LOG_FILE, fast=lite or running)
    return {
        **stats,
        "record_enabled": bool(config.get("record_replay_enabled")),
        "monitor_running": bool(monitor_status()["running"]),
        "replay_running": running,
        "replay_status": status,
        "analysis_log_path": str(REPLAY_ANALYSIS_LOG_FILE),
        "analysis_log_lines": line_count,
        "analysis_log_bytes": analysis_log_bytes(),
        "replay_log_start_at": REPLAY_LOG_START_AT,
    }


def monitor_status() -> Dict[str, Any]:
    global MONITOR_PROCESS, MONITOR_STOPPED_AT
    elapsed_seconds = 0
    if MONITOR_STARTED_AT:
        try:
            start_time = datetime.strptime(MONITOR_STARTED_AT, "%Y-%m-%d %H:%M:%S")
            end_time = datetime.strptime(MONITOR_STOPPED_AT, "%Y-%m-%d %H:%M:%S") if MONITOR_STOPPED_AT else datetime.now()
            elapsed_seconds = max(0, int((end_time - start_time).total_seconds()))
        except Exception:
            elapsed_seconds = 0
    if MONITOR_PROCESS is None:
        return {"running": False, "text": "未启动", "started_at": "", "elapsed_seconds": 0, "analysis_log_enabled": analysis_log_enabled()}
    code = MONITOR_PROCESS.poll()
    if code is None:
        return {
            "running": True,
            "text": f"运行中 PID={MONITOR_PROCESS.pid}",
            "started_at": MONITOR_STARTED_AT,
            "elapsed_seconds": elapsed_seconds,
            "analysis_log_enabled": analysis_log_enabled(),
        }
    if not MONITOR_STOPPED_AT:
        MONITOR_STOPPED_AT = time.strftime("%Y-%m-%d %H:%M:%S")
        return monitor_status()
    return {"running": False, "text": f"已停止，退出码={code}", "started_at": MONITOR_STARTED_AT, "elapsed_seconds": elapsed_seconds, "analysis_log_enabled": analysis_log_enabled()}


def analysis_log_disabled_message() -> str:
    return "分析日志写入已关闭。请在配置页或本页打开「写入分析日志」开关，保存后重新启动监控才会输出。"


def start_monitor() -> str:
    global MONITOR_PROCESS, MONITOR_STARTED_AT, MONITOR_LOG_START_AT, MONITOR_STOPPED_AT
    if replay_status()["running"]:
        return "请先停止离线回放，再启动正式监控。"
    status = monitor_status()
    if status["running"]:
        return status["text"]
    if not configured_instruments():
        return "请先在配置页至少选择一个监控币种。"
    MONITOR_STARTED_AT = time.strftime("%Y-%m-%d %H:%M:%S")
    MONITOR_LOG_START_AT = MONITOR_STARTED_AT
    MONITOR_STOPPED_AT = ""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
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
    return f"监控已启动，PID={MONITOR_PROCESS.pid}"


def stop_monitor() -> str:
    global MONITOR_PROCESS, MONITOR_STOPPED_AT
    status = monitor_status()
    if not status["running"]:
        return "监控未运行。"
    MONITOR_PROCESS.terminate()
    try:
        MONITOR_PROCESS.wait(timeout=8)
    except subprocess.TimeoutExpired:
        MONITOR_PROCESS.kill()
        MONITOR_PROCESS.wait(timeout=5)
    MONITOR_STOPPED_AT = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with MONITOR_PROCESS_LOG_FILE.open("a", encoding="utf-8") as log_file:
            log_file.write(f"\n===== signal monitor stopped at {MONITOR_STOPPED_AT} =====\n")
    except OSError:
        pass
    return "监控已停止。"


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


def get_history_candle_points(inst_id: str, bar: str = "1m", limit: int = 120) -> List[Dict[str, Any]]:
    if inst_id not in SUPPORTED_INSTRUMENTS:
        inst_id = "BTC-USDT-SWAP"
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
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": close,
                "volume": float(row[5]),
                "confirmed": str(row[8]) if len(row) > 8 else "0",
            })
        except (TypeError, ValueError):
            continue
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


def effective_fields_from_log_item(item: Dict[str, Any]) -> Dict[str, Any]:
    score = item.get("score") if isinstance(item.get("score"), dict) else {}
    final_decision = item.get("final_decision") if isinstance(item.get("final_decision"), dict) else {}
    if not final_decision:
        return score

    direction = final_decision.get("direction", score.get("direction", "观望"))
    confidence = final_decision.get("confidence", score.get("raw_total_score", 0))
    final_trade_score = confidence if direction in ("做多", "做空") else 0
    raw_total_score = score.get("raw_total_score", confidence)
    entry_plan = score.get("entry_plan") if isinstance(score.get("entry_plan"), dict) else {}
    merged = dict(score)
    merged.update(
        {
            "direction": direction,
            "final_direction": direction,
            "raw_total_score": raw_total_score,
            "final_trade_score": final_trade_score,
            "total_score": confidence if direction in ("做多", "做空") else raw_total_score,
            "entry": final_decision.get("entry", score.get("entry")),
            "stop_loss": final_decision.get("stop_loss", score.get("stop_loss")),
            "take_profit": final_decision.get("take_profit", score.get("take_profit")),
            "risk_level": final_decision.get("risk_level", score.get("risk_level")),
            "trade_action_level": final_decision.get("push_recommendation", score.get("trade_action_level")),
            "decision_source": final_decision.get("decision_source"),
            "ai_called": final_decision.get("ai_called"),
            "trigger_level": final_decision.get("trigger_level"),
        }
    )
    if not entry_plan:
        merged["entry_plan"] = {}
    return merged


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
        "signal_tracking": tracking,
        "signals": [signal.get("type", "") for signal in signals if isinstance(signal, dict)],
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


def test_ai_connection() -> Dict[str, Any]:
    env = build_child_env()
    api_key = env.get("OPENAI_API_KEY", "")
    base_url = env.get("AI_BASE_URL", ENV_DEFAULTS["AI_BASE_URL"]).strip()
    model = env.get("AI_MODEL", ENV_DEFAULTS["AI_MODEL"])
    if not api_key:
        return {"ok": False, "message": "AI测试失败：AI API Key 未配置。请先在配置页填写并保存。"}
    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key, base_url=base_url)
        response = client.responses.create(model=model, input="请回复：AI接口连通性测试成功。")
        return {"ok": True, "message": f"AI测试成功：{getattr(response, 'output_text', response)}"}
    except Exception as exc:
        return {"ok": False, "message": f"AI测试失败：{exc}"}


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
    if not analysis_log_enabled():
        return analysis_log_disabled_message()
    if not MONITOR_LOG_START_AT:
        return "等待启动监控。本窗口只显示本次启动后的 JSON 分析日志，供图表与压测使用。"
    lines = []
    for line in tail_text(MONITOR_JSON_LOG_FILE, DEFAULT_LOG_MAX_BYTES).splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if str(item.get("time", "")) >= MONITOR_LOG_START_AT:
            lines.append(line)
    return "\n".join(lines) or "暂无本次启动后的JSON分析日志。"


def monitor_console_log_text() -> str:
    if not analysis_log_enabled():
        return analysis_log_disabled_message() + " 推送等关键事件仍可能写入进程控制台。"
    if not MONITOR_LOG_START_AT:
        return "等待启动监控。本窗口只显示本次启动后的控制台摘要，包含信号、推送与异常。"
    if not MONITOR_PROCESS_LOG_FILE.exists():
        return "暂无控制台日志文件。"
    marker = f"===== signal monitor started at {MONITOR_LOG_START_AT} ====="
    text = tail_text(MONITOR_PROCESS_LOG_FILE, 4 * 1024 * 1024)
    idx = text.rfind(marker)
    if idx >= 0:
        session_text = text[idx + len(marker) :].lstrip("\n")
        return session_text or "监控已启动，等待控制台输出..."
    return text.strip() or "暂无本次启动后的控制台日志。"


def read_monitor_points(inst_id: str, max_points: int = 20000) -> Dict[str, Any]:
    realtime_points = []
    log_chart_points = []
    running = bool(monitor_status()["running"])
    log_enabled = analysis_log_enabled()
    if log_enabled:
        for line in tail_text(MONITOR_JSON_LOG_FILE, DEFAULT_LOG_MAX_BYTES).splitlines():
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
    realtime_points = realtime_points[-max_points:]
    source = "signal-monitor-log"
    points = realtime_points
    web_chart_points = []
    if running:
        try:
            web_chart_points = get_history_candle_points(inst_id, "1m", 120)
        except Exception:
            web_chart_points = []
    chart_points = web_chart_points or log_chart_points
    if chart_points:
        points = merge_price_points(chart_points, [], max_points)
        latest_by_minute = {minute_bucket_key(point.get("time", "")): point for point in realtime_points}
        for index, point in enumerate(points):
            metrics = latest_by_minute.get(minute_bucket_key(point.get("time", "")))
            if metrics:
                enriched = {**metrics, **point}
                enriched["price"] = point["price"]
                enriched["kind"] = "history"
                points[index] = enriched
        if points and realtime_points and points[-1].get("raw_total_score") is None:
            # Web画图会优先使用OKX最新1m K线；日志分析可能晚几秒或落在上一分钟。
            # 如果两者时间相差不超过3分钟，只把“指标字段”贴到最新K线上展示，
            # 价格、时间、K线高低点仍保留图表数据，避免用户看到最新快照指标突然全空。
            latest_metrics = realtime_points[-1]
            if seconds_between_time_text(points[-1].get("time"), latest_metrics.get("time")) <= 180:
                points[-1] = {**latest_metrics, **points[-1], "price": points[-1]["price"], "kind": "history"}
        source = "web-chart" if web_chart_points else "signal-monitor-chart"
    if len(points) == 1:
        points.append({"time": points[0]["time"], "price": points[0]["price"]})
    first = points[0]["price"] if points else 0
    last = points[-1]["price"] if points else 0
    change = last - first if points else 0
    paper_account = read_paper_account(inst_id)
    if not paper_account and realtime_points:
        latest = realtime_points[-1]
        if latest.get("paper_equity") is not None:
            paper_account = {
                "equity": latest.get("paper_equity"),
                "pnl_usd": latest.get("paper_pnl_usd"),
                "pnl_pct": latest.get("paper_pnl_pct"),
                "position_label": latest.get("paper_position"),
                "trade_count": latest.get("paper_trade_count"),
                "initial_capital": latest.get("paper_initial_capital"),
                "direction": latest.get("final_direction"),
            }
    return {
        "inst_id": inst_id,
        "running": running,
        "points": points,
        "price": last,
        "change": change,
        "change_pct": (change / first * 100) if first else 0,
        "source": source,
        "paper_account": paper_account,
    }


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
    try:
        log_size = log_path.stat().st_size if log_path.exists() else 0
    except OSError:
        log_size = 0
    read_full = log_path.resolve() == REPLAY_ANALYSIS_LOG_FILE.resolve() or log_size <= DEFAULT_LOG_MAX_BYTES
    items = []
    price_by_time: Dict[str, Tuple[datetime, float]] = {}
    for line in iter_json_log_lines(log_path, read_full=read_full):
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
    return 0.070


def pct_rate(hit: int, total: int) -> float:
    return (hit / total * 100) if total else 0.0


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
    final_decision = item.get("final_decision") if isinstance(item.get("final_decision"), dict) else {}
    direction = final_decision.get("direction")
    if direction in ("做多", "做空", "观望"):
        return str(direction)
    score = item.get("score") if isinstance(item.get("score"), dict) else {}
    direction = score.get("final_direction", score.get("direction", "观望"))
    return str(direction or "观望")


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


def _close_paper_position(state: Dict[str, Any], price: float) -> None:
    position = state.get("position", "flat")
    entry_price = safe_float(state.get("entry_price"), 0.0)
    basis_equity = safe_float(state.get("basis_equity"), 0.0)
    if position == "long" and entry_price > 0:
        state["cash"] = basis_equity * (price / entry_price)
    elif position == "short" and entry_price > 0:
        state["cash"] = basis_equity * (1 + (entry_price - price) / entry_price)
    state["position"] = "flat"
    state["position_label"] = "空仓"
    state["entry_price"] = 0.0
    state["basis_equity"] = 0.0


def _open_paper_position(state: Dict[str, Any], position: str, price: float, direction: str) -> None:
    state["position"] = position
    state["position_label"] = _paper_position_label(position)
    state["entry_price"] = price
    state["basis_equity"] = safe_float(state.get("cash"), PAPER_INITIAL_CAPITAL)
    state["cash"] = 0.0
    state["direction"] = direction
    state["trade_count"] = int(state.get("trade_count", 0) or 0) + 1


def step_paper_account_state(state: Dict[str, Any], price: float, direction: str) -> None:
    target = _paper_position_from_direction(direction)
    current = state.get("position", "flat")
    if target != current:
        if current != "flat":
            _close_paper_position(state, price)
        if target != "flat":
            _open_paper_position(state, target, price, direction)
        else:
            state["direction"] = "观望"
            state["position_label"] = "空仓"
    else:
        state["direction"] = direction
        if target != "flat":
            state["position_label"] = _paper_position_label(target)
    _mark_paper_equity(state, price)


def simulate_paper_account_series(items: List[Dict[str, Any]]) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
    """按 final_direction 对日志逐轮模拟 $10k 跟单账户，返回 time->状态 与最终状态。"""
    state = new_paper_account_state()
    by_time: Dict[str, Dict[str, Any]] = {}
    for item in items:
        try:
            price = float(item.get("price"))
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue
        step_paper_account_state(state, price, direction_from_log_item(item))
        by_time[str(item.get("time", ""))] = dict(state)
    return by_time, dict(state)


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
        "pending_total": 0,
        "next_pending_seconds": 0,
        "mature_rate_pct": 0.0,
        "reliability_score": 0.0,
        "reliability_level": "样本不足",
        "horizon_seconds": 0,
        "threshold_pct": 0.0,
        "prediction_accuracy_pct": 0.0,
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
    retention_hours = max(0.5, min(168.0, float(retention_hours or 12)))
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
    retention_hours: float = 12.0,
    interval_seconds: int = 5,
) -> Dict[str, Any]:
    if inst_id not in SUPPORTED_INSTRUMENTS:
        raise ValueError("仅支持 BTC-USDT-SWAP 或 ETH-USDT-SWAP")
    horizon_seconds = max(5, min(3600, int(horizon_seconds or 5)))
    interval_seconds = max(1, int(interval_seconds or 5))
    retention_hours = max(0.5, min(168.0, float(retention_hours or 12)))
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
        "recent": [],
        "retention_hours": retention_hours,
        "interval_seconds": interval_seconds,
        "max_points": max_points,
        "log_path": str(log_file),
    }
    if replay_scope:
        # 回放日志里的 time 是录制时的虚拟时间，不能用启动回放的墙钟时间过滤。
        # 未点击「开始回放」时不展示磁盘上的旧 replay_analysis.jsonl，避免误以为自动回放。
        if not replay_results_available():
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
    paper_by_time, paper_final = simulate_paper_account_series(items)
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
    signed_error_sum = 0.0
    abs_error_sum = 0.0
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
        signed_error = (pred_value - actual_value)
        signed_error_sum += signed_error
        abs_error_sum += abs(signed_error)
        item_time_text = item_time.strftime("%Y-%m-%d %H:%M:%S")
        paper = paper_by_time.get(item_time_text, {})
        rows.append({
            "time": item_time_text,
            "future_time": advice["future_time"],
            "price": price,
            "future_price": advice["future_price"],
            "raw_direction": raw_direction,
            "final_direction": predicted,
            "actual_direction": advice["actual_direction"],
            "actual_return_pct": advice["actual_return_pct"],
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
        })
    rolling = []
    window = 25
    for index, row in enumerate(rows):
        sample = rows[max(0, index - window + 1): index + 1]
        rolling.append({
            "time": row["time"],
            "accuracy_pct": sum(1 for item in sample if item["hit"]) / len(sample) * 100 if sample else 0.0,
            "price": row["price"],
            "future_price": row["future_price"],
            "return_pct": row["actual_return_pct"],
            "hit": row["hit"],
            "direction": row["final_direction"],
            "actual_direction": row["actual_direction"],
            "outcome_type": row.get("outcome_type", ""),
            "paper_equity": row.get("paper_equity"),
            "paper_pnl_usd": row.get("paper_pnl_usd"),
            "paper_pnl_pct": row.get("paper_pnl_pct"),
            "paper_position": row.get("paper_position"),
        })
    total = len(rows)
    mature_rate_pct = pct_rate(total, raw_log_total)
    prediction_accuracy_pct = pct_rate(decision_hit, decision_total)
    decision_accuracy_pct = prediction_accuracy_pct
    baseline_watch_pct = pct_rate(baseline_watch_hit, total)
    watch_reasonable_pct = pct_rate(watch_hit, watch_total)
    watch_baseline_pct = pct_rate(watch_baseline_hit, watch_total)
    watch_edge_pct = watch_reasonable_pct - watch_baseline_pct if watch_total else 0.0
    trade_direction_accuracy_pct = pct_rate(trade_direction_hit, trade_signal_total)
    model_edge_pct = prediction_accuracy_pct - baseline_watch_pct if total else 0.0
    reliability_score, reliability_label = reliability_level(
        total,
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
            "total": total,
            "raw_log_total": raw_log_total,
            "pending_total": pending_total,
            "next_pending_seconds": next_pending_seconds,
            "mature_rate_pct": mature_rate_pct,
            "reliability_score": reliability_score,
            "reliability_level": reliability_label,
            "horizon_seconds": horizon_seconds,
            "threshold_pct": threshold_pct,
            "prediction_accuracy_pct": prediction_accuracy_pct,
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
            "price_strict_accuracy_pct": pct_rate(price_strict_hit, total),
            "avg_signed_error": signed_error_sum / total if total else 0.0,
            "avg_abs_error": abs_error_sum / total if total else 0.0,
            "paper_initial_capital": paper_final.get("initial_capital", PAPER_INITIAL_CAPITAL),
            "paper_equity": paper_final.get("equity", PAPER_INITIAL_CAPITAL),
            "paper_pnl_usd": paper_final.get("pnl_usd", 0.0),
            "paper_pnl_pct": paper_final.get("pnl_pct", 0.0),
            "paper_position_label": paper_final.get("position_label", "空仓"),
            "paper_trade_count": paper_final.get("trade_count", 0),
            "paper_log_points": len(paper_by_time),
        },
        "points": rolling[-max_points:],
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


def field_html(key: str, label: str, kind: str, help_text: str, value: Any) -> str:
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
        current = str(value or "short")
        options = (
            ("scalp", "超短线"),
            ("short", "短线（推荐）"),
            ("swing", "中线"),
        )
        control = '<select name="' + esc(key) + '">' + "".join(
            f'<option value="{esc(opt)}" {"selected" if current == opt else ""}>{esc(text)}</option>' for opt, text in options
        ) + "</select>"
    elif kind == "risk_choice":
        current = str(value or "standard")
        options = (
            ("conservative", "保守"),
            ("standard", "标准（推荐）"),
            ("aggressive", "激进"),
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
    return f'<div class="field"><label>{esc(label)}</label><div>{control}<p>{esc(help_text)}</p></div></div>'


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


def render_login(message: str = "") -> bytes:
    notice = f'<div class="notice">{esc(message)}</div>' if message else ""
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
.notice{{background:#fee2e2;color:#991b1b;border:1px solid #fecaca;padding:10px 12px;border-radius:12px;margin-bottom:14px}}
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


def pipeline_design_html() -> str:
    return """
	<div class="page-panel" data-page="design"><section class="card toolbar-card"><div><h2>流程设计</h2><p class="section-sub">本地触发 → AI 主决策 → final_decision 统一出口。与 okx_signal_monitor.py 中 <code>_process_inst</code> 实现一致。</p></div></section><div class="help-panel">
	<section class="card help-card"><h2>架构原则</h2>
	<table class="help-table"><thead><tr><th>层级</th><th>职责</th><th>写入字段</th><th>是否决定推送</th></tr></thead><tbody>
	<tr><td>本地检测 + 评分</td><td>便宜预筛、方向/价位参考、AI 触发判定</td><td><code>signals</code>、<code>score</code>、<code>local_trigger</code></td><td>否（仅 L1 可走 local 的 watch）</td></tr>
	<tr><td>AI 分析</td><td>L2/L3 深分析，输出语义结论</td><td><code>analysis</code></td><td>否（需经 merge）</td></tr>
	<tr><td>final_decision</td><td>权威结论：方向、置信度、推送建议、价位</td><td><code>final_decision</code></td><td>是</td></tr>
	</tbody></table>
	<div class="help-note">Web 监控图、压测、微信推送、信号跟踪均优先读 <code>final_decision</code>；<code>score</code> 保留为本地参考分，便于对比 AI 与规则差异。</div>
	</section>
	<section class="card help-card"><h2>总览：单轮处理链路</h2>
	<p>主循环 <code>run_forever → run_once → _process_inst</code>，每个币种每轮固定执行：</p>
	<ol class="help-list">
	<li><code>collect_snapshot</code> — 采集市场快照</li>
	<li><code>detect_signals</code> — 阈值检测，判断「是否值得关注」</li>
	<li><code>score_snapshot</code> — 本地多层评分，产出参考方向/分数/价位</li>
	<li><code>evaluate_ai_trigger</code> — 判定 L0–L3，决定是否调用 AI</li>
	<li><code>analyze_with_ai</code> — 仅 L2/L3 且满足间隔/指纹时执行</li>
	<li><code>merge_final_decision</code> — AI 有效 JSON → AI 结论；否则 local / local_fallback</li>
	<li><code>update_signal_tracking</code> — 基于 final_decision 登记/结算样本</li>
	<li><code>push_gate(final_decision)</code> + <code>push_if_needed</code> — 微信推送</li>
	<li><code>log_result</code> — 写入 JSONL</li>
	</ol>
	<h3>流程图</h3>
	<div class="flow-chain"><span>采集</span><span>检测</span><span>本地评分</span><span>AI触发</span><span>AI(可选)</span><span>merge</span><span>跟踪</span><span>推送</span><span>JSONL</span></div>
	<h3>AI 触发等级 evaluate_ai_trigger</h3>
	<table class="help-table"><thead><tr><th>等级</th><th>典型条件</th><th>调 AI</th><th>推送预期</th></tr></thead><tbody>
	<tr><td>L0</td><td>无信号</td><td>否</td><td>不推</td></tr>
	<tr><td>L1</td><td>单条弱信号</td><td>否</td><td>最多 watch，不推 trade</td></tr>
	<tr><td>L2</td><td>多信号 / 交易类信号 / raw≥72 / 多观察信号</td><td>是（同指纹 <code>AI_CALL_MIN_INTERVAL_SECONDS</code> 内不重复，默认 60s）</td><td>看 final_decision</td></tr>
	<tr><td>L3</td><td>scalp 急速异动 或 资金费率极端</td><td>是（可 bypass 间隔）</td><td>看 final_decision，可能 spike</td></tr>
	</tbody></table>
	</section>
	<section class="card help-card"><h2>第一阶段：数据采集 collect_snapshot</h2>
	<table class="help-table"><thead><tr><th>数据</th><th>用途</th></tr></thead><tbody>
	<tr><td>ticker（最新价、买卖价）</td><td>价格基准</td></tr>
	<tr><td>多周期 K 线（1m/3m/5m/15m/1H/4H）</td><td>趋势、结构、技术指标</td></tr>
	<tr><td>1m 成交量统计</td><td>放量检测</td></tr>
	<tr><td>OI 持仓量</td><td>15 分钟变化率</td></tr>
	<tr><td>资金费率</td><td>过热 / 快速变化</td></tr>
	<tr><td>5m 多空比</td><td>拥挤度</td></tr>
	<tr><td>订单簿 top20</td><td>盘口压力</td></tr>
	</tbody></table>
	<p>衍生：<code>trend_profiles</code>（各周期 EMA/ADX/结构 trend 标签）、<code>volatility</code>、<code>dynamic_thresholds</code>、<code>market_context</code>（含 bias、trade_up/down、recent_move_pct 含 20m）。OI/资金费率按分钟采样，<strong>满 15 分钟预热</strong>后变化类信号才生效。</p>
	</section>
	<section class="card help-card"><h2>第二阶段：信号检测 detect_signals</h2>
	<p>职责：判断「是否值得进一步分析」，<strong>不直接定最终交易方向</strong>。以下任一满足即追加 signal（可多条并存）：</p>
	<table class="help-table"><thead><tr><th>信号类型</th><th>触发条件（概要）</th></tr></thead><tbody>
	<tr><td><code>volume_spike</code></td><td>1m 放量 ≥ max(用户阈值, 动态 P85)</td></tr>
	<tr><td><code>structure_break</code></td><td>5m/15m 结构突破 up/down</td></tr>
	<tr><td><code>boll_squeeze</code></td><td>15m 布林收口 + ADX 偏弱</td></tr>
	<tr><td><code>rsi_divergence</code></td><td>15m RSI 顶/底背离</td></tr>
	<tr><td><code>rsi_extreme</code></td><td>15m RSI ≥80 或 ≤20</td></tr>
	<tr><td><code>macd_momentum_change</code></td><td>MACD 柱体斜率显著</td></tr>
	<tr><td><code>oi_change</code></td><td>预热完成 且 |15m OI 变化| ≥ 配置阈值</td></tr>
	<tr><td><code>funding_hot</code></td><td>|资金费率| ≥ 绝对值阈值</td></tr>
	<tr><td><code>funding_fast_change</code></td><td>预热完成 且 |15m 费率变化| ≥ 阈值</td></tr>
	<tr><td><code>long_short_extreme</code></td><td>多/空账户占比 ≥ 75%</td></tr>
	<tr><td><code>order_book_imbalance</code></td><td>top20 盘口不平衡 ≥ max(0.35, 动态 P85)</td></tr>
	</tbody></table>
	<div class="help-note">检测不受「交易/观察/急速异动」推送开关影响；开关只影响 push_gate 最终是否发出对应类型。</div>
	</section>
	<section class="card help-card"><h2>第三阶段：本地评分 score_snapshot（参考分 → 权威方向）</h2>
	<h3>方向决策流水线（与配置页「策略周期」「确认严格度」联动）</h3>
	<ol class="help-list">
	<li><code>_raw_direction_for_mode</code> — 按当前 <code>strategy_mode</code> 选路径：scalp / short / swing 各自独立逻辑（见下）</li>
	<li><code>_direction_guard</code> — 按策略 + 风险偏好拦截逆势/结构不足（如短线 neutral 时 trade 票不足）</li>
	<li><code>_suggest_levels</code> + <code>_should_downgrade_direction</code> — 入场质量与观察分不足时降为观望</li>
	<li><code>strategy_views</code> — 每轮同时计算三种策略视图（scalp / short / swing）</li>
	<li><code>_apply_selected_strategy_view</code> — <strong>仅当前 strategy_mode</strong> 且无 guard 时，用对应视图覆盖 <code>final_direction</code>、价位与展示分</li>
	</ol>
	<div class="flow-chain"><span>raw_direction</span><span>guard</span><span>降级</span><span>三视图</span><span>选中视图覆盖</span><span>final_direction</span></div>
	<p>七层评分加权 → <code>raw_total_score</code>（0–100）；权重随 strategy_mode 的 <code>score_weights</code> 变化。推送与压测读 merge 后的 <code>final_decision</code>，本地 <code>score</code> 供对比与 fallback。</p>
	<h3>15m 趋势标签（短线/中线共用瓶颈）</h3>
	<p>策略判断用的是 <code>trend_profiles["15m"].trend</code>（EMA9/20/60 排列 + 近 4 根 15m 斜率），<strong>不是</strong>简单数几根阳线。日志里 <code>score.trends["15m"]</code> 仅为 5 根首尾对比，可能与 profile 不一致；<strong>以 profile 为准</strong>。急涨时 15m 常长期为 <code>mixed</code>，故短线/中线都会滞后。</p>
	<h3>多策略视图 strategy_views（三选一覆盖 final）</h3><div class="help-grid">
	<div class="help-item"><strong>scalp 超短线</strong><p><em>主看 1m/3m/5m，持仓 3–15 分钟。</em> 5m/10m 涨跌幅达阈值（默认约 0.22%/0.35%，随严格度缩放），或 1m/3m/5m 投票 + 反弹/回落形态。<strong>过滤：</strong>15m profile 明显反向时不追小脉冲。仅 <code>strategy_mode=scalp</code>（或 allow_scalp_trade）时主方向可执行；否则视图仅供参考、L3/spike 仍可触发。</p></div>
	<div class="help-item"><strong>short 短线</strong><p><em>主看 5m+15m profile，持仓 15 分钟–数小时。</em> 由严到宽：① bias 偏多且 5m/15m 未背离 ② 5m+15m 均 <code>up/down</code> ③ developing：同向 + 20m 延伸 ④ momentum：20m 达阈值且 15m 已 <code>up/down</code>。<strong>标准模式无</strong>「单靠短窗 pressure」档（仅激进有）。止损参考 15m ATR。</p></div>
	<div class="help-item"><strong>swing 中线</strong><p><em>主看 1H/4H，15m 确认，持仓数小时–数天。</em> aligned：1H+4H 同向；developing：1H 领先 + 15m 确认 + 4H 不反向；momentum：30–60 分钟延伸。动量档允许 15m 为 <code>mixed</code>（比短线宽）。止损参考 1H ATR。</p></div>
	</div>
	<h3>确认严格度 risk_preference（与策略正交）</h3>
	<table class="help-table"><thead><tr><th>档位</th><th>动量阈值</th><th>wait_confirmation 保留方向约需分</th><th>其它</th></tr></thead><tbody>
	<tr><td>保守</td><td>×1.15（要更大波动）</td><td>scalp ≥61 / short ≥67 / swing ≥65</td><td>短线 neutral 需 5m+15m 双票；风控层 ×1.15</td></tr>
	<tr><td>标准</td><td>×1.0</td><td>scalp ≥55 / short ≥60 / swing ≥58</td><td>短线 neutral 需 1 票；默认推荐</td></tr>
	<tr><td>激进</td><td>×0.9（scalp ×0.82）</td><td>scalp ≥55 / short ≥60 / swing ≥58</td><td>短线可凭 pressure 给方向；scalp guard 最松</td></tr>
	</tbody></table>
	<h3>周期分工（避免混用预期）</h3>
	<table class="help-table"><thead><tr><th>行情特征</th><th>更合适策略</th></tr></thead><tbody>
	<tr><td>5–10 分钟脉冲，15m 尚未转多</td><td>超短线（主策略须选 scalp）</td></tr>
	<tr><td>20–40 分钟延伸，5m+15m profile 同向</td><td>短线</td></tr>
	<tr><td>30–60 分钟及以上，1H/4H 结构</td><td>中线</td></tr>
	</tbody></table>
	</section>
	<section class="card help-card"><h2>第四阶段：AI 触发 evaluate_ai_trigger</h2>
	<p>在 <code>ai_enabled=true</code> 时，仅 L2/L3 可能置 <code>should_call_ai=true</code>。L2 需满足：指纹变化、或首次调用、或距上次调用 ≥ <code>AI_CALL_MIN_INTERVAL_SECONDS</code>（默认 60）。L3 可跳过间隔。</p>
	<p>未调 AI 时，<code>merge_final_decision</code> 走 <code>decision_source=local</code>；L1 时 local 的 trade 建议会降级为 watch 或 none。</p>
	</section>
	<section class="card help-card"><h2>第五阶段：AI 分析 analyze_with_ai</h2>
	<p><strong>前置：</strong><code>should_call_ai=true</code>。本地 <code>score</code> 与 <code>local_hint</code> 仅作参考，AI 需独立输出 direction、confidence、push_recommendation 等。</p>
	<table class="help-table"><thead><tr><th>条件</th><th>结果</th></tr></thead><tbody>
	<tr><td><code>ai_enabled=false</code></td><td>不调用，merge 走 local</td></tr>
	<tr><td><code>dry_run_ai=true</code></td><td>只构造 payload，不调 API</td></tr>
	<tr><td>API Key 未配置 / openai 未安装</td><td>merge 走 local_fallback</td></tr>
	<tr><td>AI 熔断 open</td><td>探活失败则 local_fallback</td></tr>
	<tr><td>以上通过</td><td>chat completion → 解析 JSON</td></tr>
	</tbody></table>
	<p>校验：<code>_validate_ai_result</code>。JSON 无效或缺字段 → 保留原文但 merge 为 local_fallback。</p>
	</section>
	<section class="card help-card"><h2>第六阶段：合并 merge_final_decision</h2>
	<table class="help-table"><thead><tr><th>decision_source</th><th>含义</th></tr></thead><tbody>
	<tr><td><code>ai</code></td><td>AI 返回有效 JSON，由 <code>_build_ai_final_decision</code> 生成</td></tr>
	<tr><td><code>local</code></td><td>未调 AI（L0/L1 或 ai 关闭），由本地规则生成</td></tr>
	<tr><td><code>local_fallback</code></td><td>已调 AI 但失败/无效，回退本地规则</td></tr>
	</tbody></table>
	<p><code>final_decision</code> 核心字段：<code>direction</code>、<code>confidence</code>、<code>push_recommendation</code>（none/watch/trade/spike）、<code>entry/stop_loss/take_profit</code>、<code>risk_level</code>、<code>reasons</code>、<code>rule_audit</code>、<code>trigger_level</code>。</p>
	<p>AI 侧 <code>push_recommendation</code> 可由模型显式给出；否则由 direction、confidence、audit、trigger level 推导。audit 为「不可信」时 trade 会降级。</p>
	</section>
	<section class="card help-card"><h2>第七阶段：信号跟踪 update_signal_tracking</h2>
	<ul class="help-list"><li>依据 <code>final_decision.direction</code> 与 <code>confidence</code> 登记样本</li><li>价格触达 entry → active_review，跟踪 MFE/MAE</li><li>到期结算 → <code>signal_performance.jsonl</code></li></ul>
	<p>与推送无直接关系，供压测与复盘。</p>
	</section>
	<section class="card help-card"><h2>第八阶段：微信推送 push_gate</h2>
	<ol class="help-list"><li><code>signals</code> 非空</li><li><code>final_decision.push_recommendation</code> ≠ none</li><li><code>push_gate</code> 校验 confidence 阈值与类型开关</li><li>不在冷却期</li><li><code>push_enabled</code> + <code>WECHAT_SEND_KEY</code> 决定是否真发</li></ol>
	<table class="help-table"><thead><tr><th>push_recommendation</th><th>push_gate 额外校验</th></tr></thead><tbody>
	<tr><td><strong>trade</strong></td><td>direction 为做多/做空，confidence ≥ push_score</td></tr>
	<tr><td><strong>watch</strong></td><td>confidence ≥ watch_push_score；本地来源需命中观察类信号或 AI 来源</td></tr>
	<tr><td><strong>spike</strong></td><td>confidence ≥ spike_push_score；L3 超短线急速异动</td></tr>
	</tbody></table>
	<p>冷却 key：<code>{push_kind}:{inst_id}:{direction}:{信号类型组合}</code>。推送正文同样来自 final_decision。<strong>trade/watch/spike 三类通道 Web 配置默认全开</strong>，是否发出由 final_decision 与 push_score 决定。</p>
	</section>
	<section class="card help-card"><h2>第九阶段：日志 log_result</h2>
	<p>配置页或日志页可开关「写入分析日志」。关闭时 live 监控跳过 JSON 与控制台摘要写入；离线回放仍写入 <code>replay_analysis.jsonl</code>。开启后写入 <code>okx_signal_analysis.jsonl</code>：<code>score</code>（本地参考）、<code>local_trigger</code>、<code>analysis</code>、<code>final_decision</code>（权威）。控制台以 <code>【AI分析/本地规则/本地兜底】</code> 前缀输出触发原因、分析结论与推送摘要；详细 debug 设 <code>CONSOLE_VERBOSE=1</code>。</p>
	<h3>关键配置对照</h3>
	<table class="help-table"><thead><tr><th>Web 可配项</th><th>影响阶段</th></tr></thead><tbody>
	<tr><td>策略周期 / 确认严格度</td><td>raw_direction 路径、guard、降级门槛、strategy_view 覆盖、动量阈值、score_weights</td></tr>
	<tr><td>push_score / watch_push_score / spike_push_score</td><td>trade / watch / spike 三类推送门槛（可拆分配置）</td></tr>
	<tr><td>ai_enabled / push_enabled</td><td>是否调 AI / 是否发微信</td></tr>
	<tr><td>写入分析日志</td><td>是否每轮写 JSON 与控制台摘要；关闭可减磁盘与 Web 开销</td></tr>
	<tr><td>AI 密钥与 Base URL</td><td>AI 连接</td></tr>
	</tbody></table>
	<p>以下使用内置默认，不在 Web 暴露：策略检测阈值、高级 scalp 开关、trade/watch/spike 通道开关（默认全开）、网络重试与日志轮转等。</p>
	<div class="help-note">一句话：本地负责「发现异常 + 决定是否叫 AI」；AI 负责「被叫时的主决策」；final_decision 负责「推送、跟踪、Web 展示」的统一出口。</div>
	</section></div></div>
"""


def render_page(message: str = "") -> bytes:
    config = load_config()
    env = load_env()
    auth = load_auth()
    selected = set(config.get("inst_ids", []))
    selected_instruments = [inst for inst in SUPPORTED_INSTRUMENTS if inst in selected]
    monitor_initial = selected_instruments[0] if selected_instruments else ""
    if selected_instruments:
        monitor_tabs = "".join(
            f'<button class="button coin-tab {"active" if index == 0 else ""}" type="button" data-monitor-inst="{inst}">{inst}</button>'
            for index, inst in enumerate(selected_instruments)
        )
    else:
        monitor_tabs = '<span class="empty-coin">请先在配置页选择监控币种</span>'
    rows = []
    rows.append('<section class="card hero-card page-section" data-page="config"><div><h2>配置币种</h2><p class="section-sub">选择本次需要监控的OKX永续合约。</p></div><div class="checks">')
    for inst in SUPPORTED_INSTRUMENTS:
        checked = "checked" if inst in selected else ""
        rows.append(f'<label class="check-tile"><input type="checkbox" name="inst_ids" value="{inst}" {checked}><span>{inst}</span></label>')
    rows.append("</div></section>")
    current = ""
    for section, key, kind, label, help_text in CONFIG_FIELDS:
        if section != current:
            if current:
                rows.append("</section>")
            current = section
            rows.append(f'<section class="card page-section" data-page="config"><h2>{esc(section)}</h2>')
        rows.append(field_html(key, label, kind, help_text, config.get(key)))
    rows.append("</section>")
    risk = str(config.get("risk_preference", "standard"))
    suggested_rows = "".join(
        f"<tr><td>{esc(RISK_PREFERENCE_LABELS.get(risk_key, risk_key))}</td>"
        f"<td>{values['push_score']}</td>"
        f"<td>{values['watch_push_score']}</td>"
        f"<td>{values['spike_push_score']}</td></tr>"
        for risk_key, values in SUGGESTED_PUSH_SCORES.items()
    )
    rows.append(
        f'<section class="card page-section" data-page="config" id="pushScoreGuideCard" style="display:block;">'
        f'<h2>推送分数建议</h2>'
        f'<p class="section-sub" id="pushScoreGuideText">{esc(push_score_guide_text(risk, bool(config.get("ai_enabled"))))}</p>'
        f'<table class="help-table" style="grid-column:1/-1;"><thead><tr>'
        f'<th>确认严格度</th><th>trade</th><th>watch</th><th>spike</th>'
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

    body = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>OKX AI Assistant</title>
<style>
:root{{--bg:#f5f5f6;--panel:#fff;--text:#172033;--muted:#6b7a90;--line:#e1e8f0;--primary:#8b6cf6;--shadow:0 8px 26px rgba(15,23,42,.08)}}
*{{box-sizing:border-box}} body{{margin:0;font-family:"Segoe UI","Microsoft YaHei",Arial,sans-serif;background:var(--bg);color:var(--text)}} .app{{min-height:100vh;display:grid;grid-template-columns:258px 1fr}}
.sidebar{{position:sticky;top:0;height:100vh;background:#fff;border-right:1px solid #eceef3;padding:26px 16px;box-shadow:4px 0 24px rgba(15,23,42,.04)}} .brand{{display:flex;align-items:center;gap:10px;margin:0 0 24px;padding:0 8px;font-size:22px;font-weight:800;color:#201a38}} .logo{{width:28px;height:28px;border-radius:8px;display:grid;place-items:center;color:#fff;background:linear-gradient(135deg,#ec4899,#8b5cf6 58%,#38bdf8)}}
.nav-item{{display:flex;align-items:center;min-height:44px;padding:0 14px;border-radius:12px;color:#3c4050;text-decoration:none;font-weight:650;margin-bottom:6px}} .nav-item:hover{{background:#f1edff}} .nav-item.active{{background:#ddd5ff;color:#201a38;box-shadow:inset 4px 0 0 #8b6cf6}}
.content{{min-width:0;min-height:100vh;padding:18px 22px 22px}} .page-panel,.page-section{{display:none}} .page-panel.active{{display:block}} .page-panel[data-page="monitor"].active{{height:100%}} .page-section.active{{display:grid}} .page-section.hero-card.active{{display:flex}}
.card{{position:relative;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px 22px;background:#fff;border:1px solid #edf0f5;border-radius:18px;padding:22px;margin-bottom:18px;box-shadow:var(--shadow)}} .card::before{{content:"";position:absolute;inset:0 0 auto;height:3px;background:linear-gradient(90deg,#8b6cf6,#60a5fa,transparent);opacity:.55}} .hero-card{{justify-content:space-between;align-items:center}} h2{{grid-column:1/-1;margin:0 0 4px;font-size:18px}} .section-sub{{margin:0;color:var(--muted);font-size:13px}}
.field{{display:grid;grid-template-columns:160px minmax(160px,1fr);gap:6px 14px;align-items:center;min-height:74px;padding:12px 14px;border:1px solid var(--line);border-radius:14px;background:#fff}} .field p{{margin:0;color:var(--muted);font-size:12px}} label{{font-weight:650}} input[type=text],input[type=password],input[type=number],select{{width:100%;padding:11px 12px;border:1px solid #cbd7e5;border-radius:12px;outline:none;background:#fff;color:var(--text);font-size:14px}} input[type=checkbox]{{width:16px;height:16px;accent-color:var(--primary)}} .password-wrap{{position:relative}} .password-wrap input{{padding-right:50px}} .eye-btn{{position:absolute;right:7px;top:50%;transform:translateY(-50%);width:34px;min-width:34px;height:34px;padding:0;border-radius:11px;background:#e2e8f0;border:1px solid #94a3b8;color:#334155;box-shadow:0 3px 10px rgba(15,23,42,.12)}} .eye-btn::before{{content:"";position:absolute;left:8px;top:12px;width:16px;height:9px;border:2px solid #334155;border-radius:18px 18px 12px 12px;transform:rotate(-6deg)}} .eye-btn::after{{content:"";position:absolute;left:14px;top:15px;width:6px;height:6px;border-radius:50%;background:#2563eb}} .eye-btn.is-visible{{background:#dbeafe;border-color:#60a5fa}} .eye-btn.is-visible::before{{border-color:#1d4ed8}} .eye-btn.is-visible::after{{left:8px;top:16px;width:19px;height:2px;border-radius:2px;background:#1d4ed8;transform:rotate(-42deg)}}
.switch{{display:inline-flex;width:46px;height:26px;border-radius:999px;background:#cbd5e1;position:relative}} .switch input{{opacity:0}} .switch span{{position:absolute;width:20px;height:20px;left:3px;top:3px;border-radius:50%;background:white;box-shadow:0 2px 8px rgba(15,23,42,.22)}} .switch:has(input:checked){{background:linear-gradient(135deg,#8b6cf6,#5b8cff)}} .switch:has(input:checked) span{{transform:translateX(20px)}}
.checks{{display:flex;gap:14px;flex-wrap:wrap}} .check-tile{{display:inline-flex;align-items:center;gap:9px;padding:13px 16px;border-radius:14px;background:#f8fafc;border:1px solid var(--line);cursor:pointer}} .check-tile:has(input:checked){{background:#efeaff;border-color:#a78bfa;color:#4c1d95}}
.actions{{display:flex;justify-content:flex-end;gap:10px;flex-wrap:wrap;position:sticky;bottom:0;margin-top:20px;padding:14px;border-radius:18px;background:rgba(255,255,255,.9);border:1px solid rgba(255,255,255,.9);box-shadow:var(--shadow);backdrop-filter:blur(12px)}} .action-group,.toolbar-right,.toolbar-left{{display:flex;align-items:center;gap:10px;flex-wrap:wrap}}
button,.button{{border:0;border-radius:12px;padding:11px 16px;background:#f1f3f8;color:#263449;min-width:94px;justify-content:center;white-space:nowrap;cursor:pointer;text-decoration:none;display:inline-flex;align-items:center;font-size:14px;font-weight:650;transition:background .18s ease,color .18s ease,box-shadow .18s ease,transform .18s ease,opacity .18s ease}} button:disabled,.button.disabled{{cursor:not-allowed;opacity:.72}} .btn-save,.btn-log{{background:#ede9fe;color:#5b21b6}} .btn-run{{background:#dcfce7;color:#047857}} .btn-run.is-running{{background:linear-gradient(135deg,#ef4444,#f97316);color:#fff;box-shadow:0 10px 26px rgba(239,68,68,.24)}} .btn-run.is-starting{{background:linear-gradient(135deg,#60a5fa,#8b5cf6);color:#fff;box-shadow:0 10px 26px rgba(99,102,241,.25)}} .btn-danger{{background:#fee2e2;color:#b91c1c}} .btn-danger.is-ready{{background:#ef4444;color:#fff;box-shadow:0 10px 26px rgba(239,68,68,.22)}} .btn-test{{background:#e0f2fe;color:#0369a1}} .btn-view{{background:#f1f5f9;color:#334155}}
.toolbar-card{{display:flex;align-items:center;justify-content:space-between;gap:14px;margin-bottom:16px}} .toolbar-card::before{{display:none}} .coin-tabs{{display:inline-flex;gap:8px;padding:4px;border-radius:14px;background:#f1f5f9;border:1px solid #e2e8f0}} .coin-tab.active{{background:#8b6cf6;color:#fff}} .empty-coin{{display:inline-flex;align-items:center;padding:0 12px;color:#64748b;font-size:13px;font-weight:650}}
	.market-card{{background:#242424;border:1px solid #3a3a3a;border-radius:18px;margin:0;padding:18px 20px 16px;color:#f8fafc;box-shadow:0 12px 34px rgba(15,23,42,.16);overflow:hidden;height:calc(100vh - 40px);display:flex;flex-direction:column}} .monitor-card{{height:calc(100vh - 188px);min-height:520px}} .market-head{{display:flex;justify-content:space-between;align-items:flex-start;gap:14px;margin-bottom:12px}} .market-title{{font-size:18px;font-weight:800;margin-bottom:4px}} .market-sub{{color:#a3a3a3;font-size:12px}} .market-price{{text-align:right}} .market-price strong{{display:block;font-size:24px;line-height:1.1}} .market-price span{{font-size:13px;color:#94a3b8}} .market-price.up span{{color:#fb7185}} .market-price.down span{{color:#22c55e}} .market-canvas-wrap{{position:relative;flex:1;min-height:0;border-top:1px solid #393939;background:linear-gradient(rgba(255,255,255,.055) 1px,transparent 1px),linear-gradient(90deg,rgba(255,255,255,.035) 1px,transparent 1px);background-size:100% 72px,72px 100%}} canvas{{width:100%;height:100%;display:block;cursor:grab}} canvas.dragging{{cursor:grabbing}} .market-loading{{position:absolute;inset:0;display:grid;place-items:center;color:#a3a3a3;pointer-events:none}} .snapshot-panel{{position:absolute;left:16px;top:16px;z-index:2;min-width:260px;max-width:min(500px,calc(100% - 32px));max-height:calc(100% - 32px);overflow:auto;padding:10px 12px;border-radius:12px;background:rgba(15,23,42,.62);border:1px solid rgba(148,163,184,.20);box-shadow:0 12px 28px rgba(0,0,0,.18);backdrop-filter:blur(8px);font-size:12px;line-height:1.35;color:#dbeafe;pointer-events:none}} .snapshot-panel strong{{display:block;color:#fff;font-size:13px;margin-bottom:5px}} .snapshot-grid{{display:grid;grid-template-columns:68px minmax(0,1fr);gap:3px 8px;align-items:start}} .snapshot-grid span{{color:#9ca3af}} .snapshot-grid b{{font-weight:700;color:#e5e7eb;word-break:break-word}} .market-time-range{{margin-top:10px;padding-top:10px;border-top:1px solid rgba(255,255,255,.08);color:#a3a3a3;font-size:12px;display:flex;justify-content:flex-end;gap:10px;flex-wrap:wrap}} #monitorPaperAccount.paper-up{{color:#4ade80}} #monitorPaperAccount.paper-down{{color:#fb7185}}
	.log-window{{width:100%;min-height:220px;height:34vh;max-height:46vh;resize:vertical;border:1px solid #dbe4ef;border-radius:16px;padding:16px;background:#0f172a;color:#d1fae5;font-family:Consolas,"Courier New",monospace;font-size:13px;line-height:1.55;white-space:pre}} .log-window-console{{background:#111827;color:#e5e7eb}} .log-panel{{display:block;margin-bottom:14px}} .log-panel h3{{margin:0 0 6px;font-size:16px;color:#111827}} .log-panel p{{margin:0 0 10px;color:#64748b;font-size:12px}} .notice{{background:#ecfdf5;color:#065f46;border:1px solid #a7f3d0;padding:13px 15px;border-radius:14px;margin-bottom:16px}} .notice-error{{background:#fee2e2;color:#991b1b;border-color:#fecaca}}
	.help-panel{{display:grid;grid-template-columns:1fr;gap:16px}} .help-card{{display:block;line-height:1.68}} .help-card::before{{display:none}} .help-card h3{{margin:18px 0 8px;font-size:16px;color:#111827}} .help-card h3:first-child{{margin-top:0}} .help-card p{{margin:6px 0;color:#475569;font-size:13px}} .help-grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}} .help-item{{border:1px solid #e2e8f0;border-radius:12px;padding:12px;background:#f8fafc}} .help-item strong{{display:block;margin-bottom:4px;color:#1f2937}} .help-list{{margin:8px 0 0;padding-left:18px;color:#475569;font-size:13px}} .help-list li{{margin:4px 0}} .help-table{{width:100%;border-collapse:collapse;margin-top:10px;font-size:13px}} .help-table th,.help-table td{{border:1px solid #e2e8f0;padding:9px 10px;text-align:left;vertical-align:top}} .help-table th{{background:#f1f5f9;color:#334155}} .help-note{{border-left:4px solid #8b6cf6;background:#f5f3ff;padding:10px 12px;border-radius:10px;color:#4c1d95;font-size:13px;margin-top:10px}} .flow-chain{{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:12px 0 4px}} .flow-chain span{{display:inline-flex;align-items:center;padding:8px 12px;border-radius:999px;background:#eef2ff;border:1px solid #c7d2fe;color:#3730a3;font-size:12px;font-weight:650}} .flow-chain span:not(:last-child)::after{{content:"→";margin-left:8px;color:#94a3b8;font-weight:400}} code{{background:#eef2ff;color:#4338ca;border-radius:6px;padding:1px 5px}}
	.accuracy-card{{display:block}} .accuracy-controls{{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:10px 0 12px}} .accuracy-controls select{{width:auto;min-width:150px}} .accuracy-controls .btn-save{{min-width:88px}} .accuracy-summary{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:12px}} .accuracy-summary div{{border:1px solid #e2e8f0;border-radius:12px;padding:10px;background:#f8fafc}} .accuracy-summary div.accuracy-primary{{border-color:#93c5fd;background:#eff6ff}} .accuracy-summary div.accuracy-paper-primary{{border-color:#86efac;background:#ecfdf5}} .accuracy-summary span{{display:block;color:#64748b;font-size:12px}} .accuracy-summary b{{display:block;margin-top:3px;font-size:18px;color:#111827}} .accuracy-canvas-wrap{{position:relative;height:320px;border:1px solid #e2e8f0;border-radius:14px;background:#0f172a;overflow:hidden}} .accuracy-canvas-wrap canvas{{width:100%;height:100%;cursor:grab}} .accuracy-canvas-wrap canvas.dragging{{cursor:grabbing}} .accuracy-point-panel{{position:absolute;left:12px;top:12px;z-index:2;min-width:240px;max-width:min(420px,calc(100% - 24px));max-height:calc(100% - 24px);overflow:auto;padding:10px 12px;border-radius:12px;background:rgba(15,23,42,.78);border:1px solid rgba(148,163,184,.28);box-shadow:0 12px 28px rgba(0,0,0,.22);backdrop-filter:blur(8px);font-size:12px;line-height:1.35;color:#dbeafe;pointer-events:none}} .accuracy-point-panel strong{{display:block;color:#fff;font-size:13px;margin-bottom:5px}} .accuracy-point-panel .snapshot-grid{{display:grid;grid-template-columns:72px minmax(0,1fr);gap:3px 8px;align-items:start}} .accuracy-point-panel .snapshot-grid span{{color:#9ca3af}} .accuracy-point-panel .snapshot-grid b{{font-weight:700;color:#e5e7eb;word-break:break-word}} .accuracy-note{{margin:10px 0 0;color:#64748b;font-size:12px}}
	@media(max-width:860px){{.app{{grid-template-columns:1fr}}.sidebar{{position:relative;height:auto}}.card{{grid-template-columns:1fr}}.field{{grid-template-columns:1fr}}}}
	</style></head><body><div class="app"><aside class="sidebar"><div class="brand"><span class="logo">O</span><span>OKX AI</span></div>
	<a class="nav-item active" href="#monitor" data-page-link="monitor">监控</a><a class="nav-item" href="#config" data-page-link="config">配置</a><a class="nav-item" href="#logs" data-page-link="logs">日志</a><a class="nav-item" href="#tests" data-page-link="tests">测试</a><a class="nav-item" href="#design" data-page-link="design">流程设计</a><a class="nav-item" href="#help" data-page-link="help">帮助</a><a class="nav-item" href="#settings" data-page-link="settings">设置</a>
</aside><div class="content"><main>
{notice_html}
<form class="config-form" method="post" action="/save#config">{''.join(rows)}<div class="actions config-actions" data-page-actions="config"><div class="action-group"><button class="action-control btn-save" type="button" id="saveConfigBtn">另存为配置</button><button class="action-control btn-save" type="button" id="importConfigBtn">导入配置</button><a class="button action-control btn-save" href="/config-json#config">查看配置</a></div></div></form>
<form class="settings-form" method="post" action="/save-auth#settings"><section class="card page-panel" data-page="settings"><h2>登录账号</h2><div class="field"><label>用户名</label><div><input type="text" name="auth_username" value="{esc(auth.get("username","admin"))}"><p>Web控制台登录用户名。</p></div></div><div class="field"><label>新密码</label><div><div class="password-wrap"><input type="password" name="auth_password" placeholder="留空则不修改"><button class="eye-btn" type="button" data-toggle-password aria-label="显示或隐藏密码"></button></div><p>建议首次部署后立即修改默认密码。</p></div></div></section><div class="actions settings-actions" data-page-actions="settings"><div class="action-group"><button class="action-control btn-save" type="submit">保存账号密码</button><a class="button action-control btn-view" href="/logout">切换账号</a><a class="button action-control btn-danger" href="/logout">退出登录</a></div></div></form>
<div class="page-panel active" data-page="monitor"><section class="card toolbar-card"><div><h2>实时监控</h2><p class="section-sub">未启动时显示虚拟行情；启动后读取真实日志。下方<strong>模拟账户</strong>按 final_direction 满仓跟单（会话 $10,000 重置），非真实成交。</p></div><div class="toolbar-right"><div class="coin-tabs">{monitor_tabs}</div><button class="button btn-run action-control" type="button" id="monitorToggleBtn">开始监控</button></div></section><section class="market-card monitor-card"><div class="market-head"><div><div class="market-title" id="monitorTitle">{esc(monitor_initial or "未配置币种")} 实时走势</div><div class="market-sub" id="monitorMeta">{esc("虚拟行情预览 · 启动监控后自动切换真实数据" if monitor_initial else "请先在配置页选择监控币种")}</div></div><div class="market-price" id="monitorPrice"><strong>--</strong><span>生成模拟行情</span></div></div><div class="market-canvas-wrap"><canvas id="monitorChart"></canvas><div class="market-loading" id="monitorLoading">正在生成虚拟走势...</div><div class="snapshot-panel" id="snapshotPanel"><strong>Snapshot</strong><div class="snapshot-grid"><span>价格</span><b>--</b><span>时间</span><b>--</b><span>评分</span><b>--</b><span>方向</span><b>--</b></div></div></div><div class="market-time-range"><span id="monitorUptime">已监控：未启动</span><span id="monitorPaperAccount">模拟账户：--</span><span id="monitorPointCount">数据点：0</span></div></section></div>
	<div class="page-panel" data-page="logs"><section class="card toolbar-card"><div><h2>实时日志</h2><p class="section-sub">默认关闭写入以节省资源。打开「写入分析日志」后，监控进程才会输出 JSON 与控制台摘要；下方窗口仅显示本次启动后的内容。</p></div><div class="toolbar-right"><label class="switch" title="写入分析日志"><input type="checkbox" id="analysisLogSwitch" {"checked" if config.get("analysis_log_enabled") else ""}><span></span></label><span class="section-sub" id="analysisLogSwitchHint" style="margin:0;">{"已开启" if config.get("analysis_log_enabled") else "已关闭"} · 保存后需重启监控</span><button class="button btn-log" type="button" id="refreshLogBtn">刷新全部</button><button class="button btn-log" type="button" id="openLogDirBtn">打开日志目录</button></div></section><section class="card" style="display:block;"><div class="log-panel"><h3>JSON 分析日志</h3><p>Web 图表/压测依赖的完整分析记录，默认保存：{esc(MONITOR_JSON_LOG_FILE)}</p><textarea class="log-window" id="logWindow" readonly>正在加载日志...</textarea><div class="toolbar-card" style="margin:12px 0 0;box-shadow:none;"><div><p class="section-sub" id="saveLogHint">可另存为 .jsonl 文件，便于回放与统计。</p></div><div class="toolbar-right"><button class="button btn-log" type="button" id="clearLogBtn">清除窗口</button><button class="btn-save" type="button" id="saveLogBtn">另存为文件</button></div></div></div><div class="log-panel"><h3>控制台日志</h3><p>监控进程精简调试输出（信号摘要、推送结果、错误）；详细重试日志需设置 CONSOLE_VERBOSE=1，默认保存：{esc(MONITOR_PROCESS_LOG_FILE)}</p><textarea class="log-window log-window-console" id="consoleLogWindow" readonly>正在加载控制台日志...</textarea><div class="toolbar-card" style="margin:12px 0 0;box-shadow:none;"><div><p class="section-sub" id="saveConsoleLogHint">可另存为 .log 文件，便于快速排查信号与推送。</p></div><div class="toolbar-right"><button class="button btn-log" type="button" id="clearConsoleLogBtn">清除窗口</button><button class="btn-save" type="button" id="saveConsoleLogBtn">另存为文件</button></div></div></div></section></div>
	<div class="page-panel" data-page="tests"><section class="card toolbar-card"><div><h2>连通性测试</h2><p class="section-sub">测试 AI 接口和微信推送（Server酱）配置是否可用。点击后会先保存当前配置页中的密钥，再发起测试；<strong>测试微信推送</strong>会发送与真实监控相同结构的格式预览（模拟 AI 全字段示例）。</p></div><div class="toolbar-right"><button class="button action-control btn-test" type="button" id="testAiBtn">测试AI</button><button class="button action-control btn-test" type="button" id="testPushBtn">测试微信推送</button></div></section><div id="connectivityTestNotice" class="notice" hidden></div><section class="card accuracy-card"><h2>实时预测压测</h2><p class="section-sub">顶部<strong>模拟账户</strong>按 final_direction 从 $10,000 满仓跟单（方向变才换仓）；绿线为权益曲线。下方为分层预测准确度；短线建议验证窗 15–20 分钟。</p><div class="accuracy-controls"><select id="accuracyInst"><option value="BTC-USDT-SWAP">BTC-USDT-SWAP</option><option value="ETH-USDT-SWAP">ETH-USDT-SWAP</option></select><select id="accuracyHorizon"><option value="5">5秒 · 轮询级</option><option value="15">15秒</option><option value="30">30秒</option><option value="60">1分钟</option><option value="180">3分钟</option><option value="300">5分钟</option><option value="900">15分钟 · 短线推荐</option><option value="1200">20分钟 · 短线结构</option></select><select id="accuracyScope"><option value="session">本次启动后</option><option value="replay">回放会话</option><option value="all">全部历史日志</option></select><select id="accuracyRetentionHours" title="结合配置页轮询间隔计算图表最多保留多少点"><option value="1">保留1小时</option><option value="2">保留2小时</option><option value="4">保留4小时</option><option value="8">保留8小时</option><option value="12" selected>保留12小时</option><option value="24">保留24小时</option><option value="48">保留48小时</option></select><button class="btn-test" type="button" id="accuracyRefreshBtn">刷新压测</button><button class="btn-save" type="button" id="accuracyExportBtn">导出图表</button><button class="btn-save" type="button" id="accuracyImportBtn">导入图表</button><button class="btn-test" type="button" id="accuracyLiveBtn" style="display:none">返回实时</button><input type="file" id="accuracyImportInput" accept=".json,application/json" hidden></div><div class="accuracy-summary" id="accuracySummary"><div><span>可靠性等级</span><b>--</b></div><div><span>已验证/日志</span><b>--</b></div><div><span>决策合理率</span><b>--</b></div><div><span>相对观望基准</span><b>--</b></div></div><div class="accuracy-canvas-wrap"><canvas id="accuracyChart"></canvas><div class="accuracy-point-panel" id="accuracyPointPanel" hidden></div></div><p class="accuracy-note" id="accuracyNote">观望：后续波动未超阈值即合理（错失机会记红点）。交易：验证窗内价格朝做多/做空方向走即方向命中；入场/止盈/止损仅在较长验证窗下有参考价值。</p></section><section class="card"><h2>离线回放压测</h2><p class="section-sub">配置页勾选「录制回放数据集」并运行监控，每轮原始输入写入 {esc(REPLAY_DATASET_FILE)}；停止监控后点击下方「开始回放」启动子进程，结果写入 {esc(REPLAY_ANALYSIS_LOG_FILE)}，再选上方「回放会话」查看压测曲线。</p><div class="replay-status" id="replayDatasetInfo">正在加载数据集状态...</div><div class="field"><label>回放间隔(秒)</label><div><input type="number" id="replayInterval" value="0" min="0" max="120" step="0.1"><p>0 表示尽快跑完；大于 0 可在回放过程中观察压测曲线刷新。</p></div></div><div class="field"><label>控制</label><div><div class="toolbar-right" style="justify-content:flex-start;gap:8px;"><button class="btn-test" type="button" id="replayStartBtn">开始回放</button><button class="btn-danger action-control" type="button" id="replayStopBtn">停止回放</button><button class="button btn-log" type="button" id="replayRefreshBtn">刷新状态</button></div><p id="replayStatusText">等待加载...</p></div></div></section></div>
	<div class="page-panel" data-page="help"><section class="card toolbar-card"><div><h2>帮助</h2><p class="section-sub">系统架构、指标含义、界面字段、推送规则与压测说明。详细流程见「流程设计」页。</p></div><div class="toolbar-right"><a class="button btn-view" href="#design">查看流程设计</a></div></section><div class="help-panel">
	<section class="card help-card"><h2>系统架构与决策边界</h2>
	<p>每轮监控按「采集 → 检测 → 本地评分 → AI 触发 →（可选）AI 分析 → merge → 跟踪 → 推送 → 写日志」执行。三层职责如下：</p>
	<table class="help-table"><thead><tr><th>层级</th><th>做什么</th><th>看什么字段</th></tr></thead><tbody>
	<tr><td>本地规则</td><td>发现异常信号、给出参考方向/分数/价位、决定是否调用 AI（L0–L3）</td><td><code>signals</code>、<code>score</code>、<code>local_trigger</code></td></tr>
	<tr><td>AI</td><td>L2/L3 被触发时做深分析；本地分仅作参考</td><td><code>analysis</code></td></tr>
	<tr><td>最终结论</td><td>推送、Web 图表、压测、信号跟踪的统一依据</td><td><code>final_decision</code></td></tr>
	</tbody></table>
	<ul class="help-list"><li>L0/L1 不调 AI；L1 最多推 watch，不推 trade。</li><li>AI 返回有效 JSON 时，<code>decision_source=ai</code>；已调 AI 但失败时为 <code>local_fallback</code>。</li><li>推送读 <code>final_decision.push_recommendation</code> + <code>confidence</code>；trade/watch/spike 通道默认全开，Web 只需配 push_score 与是否启用推送。</li></ul>
	<div class="help-note">监控页 Snapshot 与压测曲线已通过 <code>final_decision</code> 展示方向与置信度；日志里仍保留 <code>score</code> 便于对比 AI 与本地差异。</div>
	</section>
	<section class="card help-card"><h2>采集参数、计算指标与评分产出</h2>
	<h3>采集的数据</h3><div class="help-grid">
	<div class="help-item"><strong>行情与K线</strong><p>只监控 BTC-USDT-SWAP、ETH-USDT-SWAP。每轮采集 ticker、买卖一档、1m/3m/5m/15m/1H/4H K线；每个周期请求最近 <code>200</code> 根，字段包括时间、开高低收、成交量、是否收盘。</p></div>
	<div class="help-item"><strong>合约资金数据</strong><p>采集 Open Interest、资金费率、5m账户多空比。OI和资金费率按约 <code>60s</code> 一个有效样本保存，保留约 <code>3小时</code>，用于计算15分钟变化和资金状态。</p></div>
	<div class="help-item"><strong>盘口数据</strong><p>采集前20档订单簿，计算 top5/top20 买卖量、盘口不平衡、价差百分比。盘口缓存约5秒，只作为入场确认和风险修正，不单独决定方向。</p></div>
	<div class="help-item"><strong>运行内统计</strong><p>成交量倍数、ATR百分比、盘口不平衡按约60秒采样，保留约3小时，用分位数生成动态阈值；信号结算样本写入 <code>build/runtime_logs/signal_performance.jsonl</code>。</p></div>
	</div>
	<h3>计算的技术指标</h3><table class="help-table"><thead><tr><th>类别</th><th>指标</th><th>用途</th></tr></thead><tbody>
	<tr><td>趋势</td><td>EMA9/20/60/120、MA120、结构高低点、ADX/+DI/-DI</td><td>判断趋势排列、趋势强度、短中周期是否共振，以及是否处于震荡弱趋势。</td></tr>
	<tr><td>动量</td><td>RSI6/14/24、MACD、KDJ、K线实体占比、RSI背离</td><td>判断动能是否增强、过热、衰减或背离；KDJ用于短线入场时机确认。</td></tr>
	<tr><td>波动</td><td>ATR、ATR%、布林带、布林带宽度</td><td>识别高波动、低波动、挤压蓄势；入场区、止损、止盈根据ATR和结构位生成。</td></tr>
	<tr><td>量价</td><td>已收盘1m放量倍数、成交量方向、近5根量能趋势</td><td>判断突破或回踩是否有成交量确认，避免只看价格方向。</td></tr>
	<tr><td>合约资金</td><td>OI 15m变化、资金费率、资金费率变化、多空比</td><td>判断新增仓、平仓、拥挤、过热和反身性风险。</td></tr>
	<tr><td>数据质量</td><td>确认K线数量、EMA120/MACD/ADX/RSI ready</td><td>用于判断当前周期指标是否足够可靠；15m确认K线少于35根会降低趋势和动量评分。</td></tr>
	</tbody></table>
	<h3>评分体系（本地参考分 score）</h3><p>按配置页<strong>策略周期</strong>走 <code>_raw_direction_for_mode</code>，再经 guard、入场质量降级，最后由<strong>当前策略的 strategy_view</strong> 覆盖 <code>final_direction</code>（无 direction_guard 时）。观察分 <code>raw_total_score</code> 按 strategy_mode 的 score_weights 加权；交易分仅在最终方向为做多/做空时有值。推送以 merge 后的 <code>final_decision</code> 为准。</p>
	<ul class="help-list"><li><code>market_regime_score</code>：趋势、震荡、挤压、高波动等市场状态。</li><li><code>trend_score</code>：EMA/ADX/结构突破/多周期一致性。</li><li><code>momentum_score</code>：RSI、MACD、KDJ、背离和动能。</li><li><code>volume_price_score</code>：放量、量价方向、突破量能确认。</li><li><code>derivatives_score</code>：OI+价格组合、资金费率、多空拥挤。</li><li><code>orderbook_score</code>：top5/top20盘口支持和价差风险。</li><li><code>entry_quality_score</code>：价格距离EMA/ATR、入场区和等待确认。</li><li><code>risk_control_score</code>：资金费率、拥挤、高波动、背离、数据质量等风险控制。</li></ul>
	<div class="help-note">可靠性判断：指标越多不是越可靠，关键看数据质量、周期共振、量价确认、资金数据是否同向，以及是否触达入场区。系统给出的是观察和风险提示，不是自动交易指令；OI/资金费率刚启动不足15分钟时会降低变化类信号权重。<strong>15m 方向以 trend_profiles 为准</strong>，急涨时 profile 转 <code>up</code> 往往晚于 1m/5m。</div>
	</section>
	<section class="card help-card"><h2>策略周期与确认严格度（配置页）</h2>
	<p>两项配置正交：<strong>策略周期</strong>决定看哪些 K 线、如何给方向；<strong>确认严格度</strong>决定动量阈值高低、降级分数门槛与部分 guard 规则。日志里三种 <code>strategy_views</code> 每轮都会算，但只有当前选中的 mode 会覆盖 <code>final_direction</code>。</p>
	<h3>策略周期</h3><table class="help-table"><thead><tr><th>模式</th><th>时间尺度</th><th>给做多的典型条件（概要）</th></tr></thead><tbody>
	<tr><td>超短线 scalp</td><td>5–10 分钟 / 持仓 3–15 分钟</td><td>5m 或 10m 脉冲；1m/3m/5m 投票；15m profile 强反向时不追小脉冲</td></tr>
	<tr><td>短线 short（推荐）</td><td>20 分钟结构 / 持仓 15 分钟–数小时</td><td>5m+15m profile 均 up；或 20m 延伸且 15m 已 up；标准模式不靠单 pressure</td></tr>
	<tr><td>中线 swing</td><td>30–60 分钟+ / 持仓数小时–数天</td><td>1H+4H 同向；或 1H+15m 转多且 4H 不空；30–60m 动量（15m 可 mixed）</td></tr>
	</tbody></table>
	<h3>确认严格度</h3><table class="help-table"><thead><tr><th>档位</th><th>适用场景</th><th>要点</th></tr></thead><tbody>
	<tr><td>保守</td><td>宁可错过、少假信号</td><td>动量阈值 ×1.15；保留方向需更高分；短线 neutral 要 5m+15m 双票</td></tr>
	<tr><td>标准</td><td>默认平衡</td><td>短线 wait_confirmation 约 ≥60 分可保留方向；neutral 要 1 票</td></tr>
	<tr><td>激进</td><td>更早给方向</td><td>阈值更低；短线可凭短窗 pressure 给方向；scalp guard 最松</td></tr>
	</tbody></table>
	<div class="help-note">急拉行情：15m profile 未转 <code>up</code> 时，短线与中线都会长时间观望——这是结构确认设计，不是 bug。要吃 5–10 分钟脉冲请选<strong>超短线</strong>作主策略。</div>
	</section>
	<section class="card help-card"><h2>模拟跟单账户（监控页 + 压测页）</h2>
	<ul class="help-list">
	<li>每次<strong>开始监控</strong>、<strong>离线回放</strong>或压测范围切换时，虚拟账户从该范围内首条日志起算 <strong>$10,000</strong>。</li>
	<li>按 <code>final_direction</code> 满仓跟单：做多/做空持仓，观望为空仓；仅在<strong>方向变化</strong>时换仓。</li>
	<li><strong>测试页</strong>：顶部绿底「模拟账户」卡片 + 图表<strong>绿线</strong>为权益曲线；监控页底部也会显示当前权益。</li>
	<li>1x、不计手续费；非真实成交，仅用于感受策略表现。</li>
	</ul>
	</section>
	<section class="card help-card"><h2>压测图表说明（测试页）</h2>
	<ul class="help-list">
	<li><strong>模拟账户($10k)</strong>：绿底卡片与绿线权益曲线，按 final_direction 跟单。</li>
	<li><strong>综合预测准确度</strong>：观望 + 交易方向命中；图表绿/红点与此一致。</li>
	<li><strong>蓝线</strong>价格、<strong>黄线</strong>预测方向累计；长时段顶部显示抽稀信息。</li>
	</ul>
	</section>
	<section class="card help-card"><h2>界面字段、推送与日志解读</h2>
	<h3>走势图 Snapshot（读 final_decision）</h3><table class="help-table"><thead><tr><th>字段</th><th>含义</th><th>解读方式</th></tr></thead><tbody>
	<tr><td>方向</td><td><code>final_decision.direction</code></td><td>权威结论；本地 <code>raw_direction → final_direction</code> 仅作对比参考。</td></tr>
	<tr><td>分数/置信度</td><td><code>final_decision.confidence</code></td><td>推送与压测用的置信度；本地 raw/final_trade_score 在 JSON 的 score 字段中。</td></tr>
	<tr><td>决策来源</td><td><code>decision_source</code></td><td><code>ai</code> / <code>local</code> / <code>local_fallback</code>，表示 final_decision 如何产生。</td></tr>
	<tr><td>触发等级</td><td><code>trigger_level</code>（L0–L3）</td><td>本轮 AI 触发级别；L2/L3 才可能调用 AI。</td></tr>
	<tr><td>市场/策略</td><td><code>market_regime / strategy_template</code></td><td>识别趋势、震荡、挤压或高波动，匹配策略模板。</td></tr>
	<tr><td>风险/动作</td><td><code>risk_level / trade_action_level</code></td><td>风险描述与是否适合执行；高风险不等于一定反向。</td></tr>
	<tr><td>入场质量</td><td><code>entry_plan.quality / entry_quality_score</code></td><td>入场区是否有效；震荡、高波动、离 EMA 过远会降低质量。</td></tr>
	<tr><td>数据质量</td><td>15m 确认 K 线数量与 ready 状态</td><td>指标不足时系统偏向观望。</td></tr>
	<tr><td>预热</td><td><code>oi_warmup_ready / funding_warmup_ready</code></td><td>OI/资金费率变化需满 15 分钟窗口。</td></tr>
	<tr><td>OI / 资金费率 / 多空比</td><td>合约资金数据</td><td>判断新增仓、平仓、拥挤与过热；需结合价格方向理解。</td></tr>
	<tr><td>放量 / ATR / 盘口</td><td>量价与波动确认</td><td>辅助确认，不单独定方向；动态阈值来自近 3 小时采样分位数。</td></tr>
	<tr><td>分层</td><td>八层评分摘要</td><td>定位本地 score 来源；AI 决策时仅作参考。</td></tr>
	</tbody></table>
	<h3>推送类型（读 final_decision.push_recommendation）</h3><div class="help-grid">
	<div class="help-item"><strong>trade</strong><p>方向为做多/做空且 confidence ≥ push_score。正文含入场、止损、止盈。</p></div>
	<div class="help-item"><strong>watch</strong><p>风险提示型推送；门槛为 watch_push_score（低于 trade）。</p></div>
	<div class="help-item"><strong>spike</strong><p>L3 急速异动；门槛为 spike_push_score（通常最低）。</p></div>
	</div>
	<p>三类推送通道默认全开；实际是否发出取决于 final_decision、push_score、冷却期，以及是否启用微信推送。</p>
	<h3>日志字段</h3><ul class="help-list">
	<li><strong>JSON 分析日志</strong>（<code>okx_signal_analysis.jsonl</code>）：完整结构化数据，含 score、local_trigger、analysis、final_decision。需在配置页或日志页开启「写入分析日志」并重启监控。</li>
	<li><strong>控制台日志</strong>（<code>signal_monitor_console.log</code>）：每行以 <code>【AI分析】/【本地规则】/【本地兜底】</code> 开头，含 AI 触发原因、AI 结论或本地结论、推送结果；设 <code>CONSOLE_VERBOSE=1</code> 可看重试/冷却等 debug。</li>
	</ul>
	<h3>入场、止损、止盈与追踪</h3><ul class="help-list"><li>入场区由 ATR、结构位、EMA/VWAP 锚点生成。</li><li>止损参考结构失效位 + ATR 缓冲；止盈按风险距离生成两档（约 1R/2R）。</li><li>跟踪基于 final_decision：先等触达入场区，再统计 MFE/MAE，结算写入 signal_performance.jsonl。</li><li>追踪成交价保守估算：做多按入场区上沿，做空按下沿。</li></ul>
	<h3>测试与压测</h3><ul class="help-list">
	<li><strong>实时预测压测</strong>（测试页）：按 final_decision 方向验证后续短窗价格表现；范围可选「本次启动后 / 全部历史日志」。</li>
	<li><strong>离线回放压测</strong>：配置页勾选「录制回放数据集」→ 监控运行写入 <code>replay_dataset.jsonl</code> → 停止后在测试页启动回放子进程，走完整 <code>_process_inst</code> 链路（不访问 OKX、不推送），结果写入 <code>replay_analysis.jsonl</code>；压测范围选「回放会话」。</li>
	<li>监控与离线回放互斥；重新启动监控且仍勾选录制时会清空旧数据集。</li>
	</ul>
	<h3>本地文件位置</h3><ul class="help-list"><li>运行日志：<code>build/runtime_logs</code>；默认配置模板：<code>config</code>；本机密钥与登录：<code>local_state</code>。</li><li><code>build</code> 与 <code>local_state</code> 为运行时/私密目录，不应提交版本库。</li></ul>
	<h3>常见误读</h3><ul class="help-list"><li>本地观察分高 ≠ 一定推送；最终以 final_decision.push_recommendation 为准。</li><li>L1 弱信号不调 AI，也不会推 trade。</li><li>AI 与本地方向不一致时，以 final_decision 为准（AI 有效时 decision_source=ai）。</li><li>观望时 entry/stop/TP 为 <code>-</code> 是正常结果。</li><li>压测图<strong>红点</strong>在观望+大涨时只表示「观望错失机会」，不是做空信号。</li><li><code>score.trends["15m"]</code> 与 <code>trend_profiles["15m"].trend</code> 可能不一致；策略方向以后者为准。</li><li>日志里 scalp 视图做多但 final 仍观望：主策略不是超短线时，scalp 仅参考不覆盖。</li><li>盘口、OI、资金费率需结合价格与多周期结构理解，不宜单指标下结论。</li></ul>
	</section></div></div>
	{pipeline_design_html()}
	</main></div></div>
<script>
const SUGGESTED_PUSH_SCORES = {json.dumps(SUGGESTED_PUSH_SCORES, ensure_ascii=False)};
const RISK_PREFERENCE_LABELS = {json.dumps(RISK_PREFERENCE_LABELS, ensure_ascii=False)};
function currentRiskPreference() {{
  const el = document.querySelector('.config-form select[name="risk_preference"]');
  return el && el.value ? el.value : 'standard';
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
    textEl.textContent = '当前严格度「' + label + '」建议 trade ' + suggested.push_score
      + ' · watch ' + suggested.watch_push_score + ' · spike ' + suggested.spike_push_score
      + '。' + aiNote + ' watch/spike 应低于 trade，避免噪音推送。';
  }}
}}
function applySuggestedPushScores() {{
  const form = document.querySelector('.config-form');
  if (!form) return;
  const suggested = SUGGESTED_PUSH_SCORES[currentRiskPreference()] || SUGGESTED_PUSH_SCORES.standard;
  const tradeEl = form.querySelector('[name="push_score"]');
  const watchEl = form.querySelector('[name="watch_push_score"]');
  const spikeEl = form.querySelector('[name="spike_push_score"]');
  if (tradeEl) tradeEl.value = String(suggested.push_score);
  if (watchEl) watchEl.value = String(suggested.watch_push_score);
  if (spikeEl) spikeEl.value = String(suggested.spike_push_score);
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
  if (page === 'logs') {{ refreshLogs(false); refreshConsoleLogs(false); }}
  if (page === 'monitor') fetchMonitor(false);
  if (page === 'tests') {{
    refreshReplayInfo({{ lite: false }}).then(function(info) {{
      fetchAccuracy({{ resetView: true }});
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
  if (payload.inst_ids) refreshMonitorTabs(payload.inst_ids);
  return payload;
}}
let autoSaveTimer = null;
const configForm = document.querySelector('.config-form');
if (configForm) {{
  configForm.addEventListener('input', function() {{
    clearTimeout(autoSaveTimer);
    autoSaveTimer = setTimeout(function() {{ autoSaveConfig().catch(function() {{}}); }}, 450);
  }});
  configForm.addEventListener('change', function(event) {{
    if (event.target && event.target.name === 'analysis_log_enabled') {{
      analysisLogEnabled = !!event.target.checked;
      syncAnalysisLogUi();
    }}
    clearTimeout(autoSaveTimer);
    autoSaveTimer = setTimeout(function() {{ autoSaveConfig().catch(function() {{}}); }}, 120);
  }});
}}
document.querySelectorAll('[data-toggle-password]').forEach(function(btn) {{
  btn.addEventListener('click', function() {{
    const input = btn.parentElement.querySelector('input');
    if (!input) return;
    input.type = input.type === 'password' ? 'text' : 'password';
    btn.classList.toggle('is-visible', input.type === 'text');
  }});
}});
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
	function renderReplayDatasetInfo(info){{const box=document.getElementById('replayDatasetInfo');if(!box)return;const lines=[];lines.push('数据集: '+(info.exists?info.frame_count+' 帧 · '+((info.inst_ids||[]).join(', ')||'--'):'尚未录制'));if(info.exists){{lines.push('路径: '+(info.path||'--'));if(info.recorded_at)lines.push('录制 meta: '+info.recorded_at);if(info.interval_seconds)lines.push('录制间隔: '+info.interval_seconds+' 秒');}}if(info.replay_running&&info.analysis_log_bytes)lines.push('回放日志: 约 '+Math.max(1,Math.round(info.analysis_log_bytes/1024))+' KB');else if(info.analysis_log_lines)lines.push('上次回放分析日志: '+info.analysis_log_lines+' 行');lines.push('录制开关: '+(info.record_enabled?'已勾选':'未勾选')+' · 监控: '+(info.monitor_running?'运行中':'未运行'));box.textContent=lines.join('\\n');}}
	function renderReplayStatus(info){{const text=document.getElementById('replayStatusText');if(!text)return;const st=info&&info.replay_status?info.replay_status:{{}};let msg=(st.text||'--')+(st.started_at?' · 开始 '+st.started_at:'')+(st.elapsed_seconds!=null?' · 已运行 '+st.elapsed_seconds+' 秒':'');if(info&&info.replay_running&&info.analysis_log_bytes)msg+=' · 日志约 '+Math.max(1,Math.round(info.analysis_log_bytes/1024))+' KB';else if(!info.replay_running&&info.analysis_log_lines)msg+=' · 分析日志 '+info.analysis_log_lines+' 行';text.textContent=msg;}}
	function setReplayStartButtonState(mode){{const btn=document.getElementById('replayStartBtn');if(!btn)return;if(mode==='running'){{btn.disabled=false;btn.textContent='回放中...';btn.classList.add('is-starting');}}else if(mode==='starting'){{btn.disabled=true;btn.textContent='启动中...';btn.classList.add('is-starting');}}else{{btn.disabled=false;btn.textContent='开始回放';btn.classList.remove('is-starting');}}}}
	async function refreshReplayInfo(options){{options=options||{{}};const lite=options.lite!==false;try{{const url='/api/replay-dataset'+(lite?'?lite=1':'');const r=await fetch(url,{{cache:'no-store'}}),p=await r.json();if(!r.ok||p.ok===false)throw new Error(p.error||'加载失败');renderReplayDatasetInfo(p);renderReplayStatus(p);if(!lite&&syncAccuracyScopeWithReplay(p))fetchAccuracy({{resetView:true}});if(!lite&&p.replay_running)startReplayProgress();return p;}}catch(e){{const box=document.getElementById('replayDatasetInfo');if(box)box.textContent='加载回放状态失败：'+e;renderReplayStatus({{replay_status:{{text:String(e)}}}});return null;}}}}
	let replayProgressTimer=null,replayFinishedNotified=false,fetchAccuracyInFlight=null;
	function stopReplayProgress(){{stopReplayAccuracyPoll();if(replayProgressTimer){{clearInterval(replayProgressTimer);replayProgressTimer=null;}}}}
	function startReplayProgress(){{stopReplayProgress();replayFinishedNotified=false;switchAccuracyToReplaySession();replayProgressTimer=setInterval(async function(){{if(currentPage()!=='tests')return;const info=await refreshReplayInfo({{lite:true}});if(!info)return;setReplayStartButtonState(info.replay_running?'running':'idle');if(info.replay_running){{fetchAccuracy({{resetView:false}});return;}}stopReplayProgress();fetchAccuracy({{resetView:true}});if(!replayFinishedNotified){{replayFinishedNotified=true;const lines=info.analysis_log_lines!=null?info.analysis_log_lines:'--';alert('回放完成 · 分析日志 '+lines+' 行 · 上方压测已切到「回放会话」');}}}},2500);}}
	async function startReplayRun(){{const intervalEl=document.getElementById('replayInterval'),interval=Number(intervalEl&&intervalEl.value),statusEl=document.getElementById('replayStatusText');setReplayStartButtonState('starting');if(statusEl)statusEl.textContent='正在启动回放，请稍候...';try{{const r=await fetch('/api/replay-start',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{interval:Number.isFinite(interval)?interval:0}}),cache:'no-store'}});let p=null;const ct=(r.headers.get('content-type')||'').toLowerCase();if(ct.includes('application/json')){{p=await r.json();}}else{{throw new Error('服务器返回异常页面（HTTP '+r.status+'），请刷新并重新登录后再试');}}if(!r.ok||p.ok===false)throw new Error(p.error||p.message||'启动失败');renderReplayDatasetInfo(p);renderReplayStatus(p);if(statusEl)statusEl.textContent=p.message||'回放已启动';setReplayStartButtonState('running');fetchAccuracy({{resetView:true}});startReplayProgress();}}catch(e){{setReplayStartButtonState('idle');if(statusEl)statusEl.textContent='启动失败：'+e;alert('启动回放失败：'+e);}}}}
	async function stopReplayRun(){{const btn=document.getElementById('replayStopBtn');if(btn)btn.disabled=true;try{{const r=await fetch('/api/replay-stop',{{method:'POST',cache:'no-store'}}),p=await r.json();if(!r.ok||p.ok===false)throw new Error(p.error||p.message||'停止失败');stopReplayProgress();setReplayStartButtonState('idle');await refreshReplayInfo({{lite:false}});fetchAccuracy({{resetView:true}});alert(p.message||'回放已停止');}}catch(e){{alert('停止回放失败：'+e);}}finally{{if(btn)btn.disabled=false;}}}}
	const replayStartBtn=document.getElementById('replayStartBtn'),replayStopBtn=document.getElementById('replayStopBtn'),replayRefreshBtn=document.getElementById('replayRefreshBtn');
	if(replayStartBtn)replayStartBtn.addEventListener('click',startReplayRun);
	if(replayStopBtn)replayStopBtn.addEventListener('click',stopReplayRun);
	if(replayRefreshBtn)replayRefreshBtn.addEventListener('click',refreshReplayInfo);
	const accuracyRefreshBtn=document.getElementById('accuracyRefreshBtn');
	let monitorIntervalSeconds={int(config.get("interval", 5))};
	const accuracyView={{points:[],start:0,end:1,yZoom:1,yPan:0,priceRg:1,drag:null,followLatest:true,selectedKey:''}};
	let accuracyPlotPoints=[];
	let accuracyQueryKey='',accuracyLivePayload=null,accuracyImportedMode=false,accuracyImportedLabel='',accuracyScopeSyncing=false,replayAccuracyPollTimer=null;
	function stopReplayAccuracyPoll(){{if(replayAccuracyPollTimer){{clearInterval(replayAccuracyPollTimer);replayAccuracyPollTimer=null;}}}}
	async function isReplayRunning(){{try{{const p=await fetch('/api/replay-status',{{cache:'no-store'}}).then(r=>r.json());return!!(p&&p.replay_running);}}catch(e){{return false;}}}}
	function syncReplayAccuracyPoll(){{const scopeEl=document.getElementById('accuracyScope');if(!scopeEl||scopeEl.value!=='replay'||currentPage()!=='tests'){{stopReplayProgress();return;}}isReplayRunning().then(function(running){{if(running)startReplayProgress();else stopReplayProgress();}});}}
	function configuredMonitorInterval(){{const field=document.querySelector('input[name="interval"]');const n=Number(field&&field.value);return Number.isFinite(n)&&n>=1?n:monitorIntervalSeconds;}}
	function accuracyRetentionHours(){{const n=Number((document.getElementById('accuracyRetentionHours')||{{}}).value);return Number.isFinite(n)&&n>0?n:12;}}
	function accuracyQuerySignature(){{return [(document.getElementById('accuracyInst')||{{}}).value||'BTC-USDT-SWAP',(document.getElementById('accuracyHorizon')||{{}}).value||'5',(document.getElementById('accuracyScope')||{{}}).value||'session',accuracyRetentionHours(),configuredMonitorInterval()].join('|');}}
	function setAccuracyImportedMode(active,label){{accuracyImportedMode=!!active;accuracyImportedLabel=label||'';const liveBtn=document.getElementById('accuracyLiveBtn');if(liveBtn)liveBtn.style.display=active?'inline-flex':'none';}}
	function normalizeAccuracyBundle(raw){{if(!raw||typeof raw!=='object')throw new Error('无效JSON文件');const points=Array.isArray(raw.points)?raw.points:[];const clean=points.filter(o=>Number.isFinite(Number(o&&o.price)));if(!clean.length)throw new Error('文件中没有可用的图表点位');return{{name:raw.name||'OKX_Accuracy_Chart',version:raw.version||'1.0',exported_at:raw.exported_at||'',inst_id:raw.inst_id||'',horizon_seconds:Number(raw.horizon_seconds)||5,scope:raw.scope||'imported',retention_hours:Number(raw.retention_hours)||accuracyRetentionHours(),interval_seconds:Number(raw.interval_seconds)||configuredMonitorInterval(),max_points:Number(raw.max_points)||clean.length,start_at:raw.start_at||'',summary:(raw.summary&&typeof raw.summary==='object')?raw.summary:{{}},points:clean}};}}
	function buildAccuracyExportBundle(){{if(!accuracyLivePayload||!accuracyView.points.length)throw new Error('暂无图表数据，请先刷新压测');const inst=(document.getElementById('accuracyInst')||{{}}).value||accuracyLivePayload.inst_id||'BTC-USDT-SWAP',h=(document.getElementById('accuracyHorizon')||{{}}).value||String(accuracyLivePayload.horizon_seconds||5),scope=(document.getElementById('accuracyScope')||{{}}).value||accuracyLivePayload.scope||'session',retention=accuracyRetentionHours(),interval=configuredMonitorInterval();return{{name:'OKX_Accuracy_Chart',version:'1.0',exported_at:new Date().toISOString().slice(0,19).replace('T',' '),inst_id:inst,horizon_seconds:Number(h)||5,scope:scope,retention_hours:retention,interval_seconds:interval,max_points:accuracyLivePayload.max_points||accuracyView.points.length,start_at:accuracyLivePayload.start_at||'',summary:accuracyLivePayload.summary||{{}},points:accuracyView.points.slice()}};}}
	function accuracyExportFileName(){{const d=new Date(),p=n=>String(n).padStart(2,'0');return 'okx_accuracy_chart_'+d.getFullYear()+p(d.getMonth()+1)+p(d.getDate())+'_'+p(d.getHours())+p(d.getMinutes())+p(d.getSeconds())+'.json';}}
	async function saveJsonFile(text,name,description){{if(window.showSaveFilePicker){{const handle=await showSaveFilePicker({{suggestedName:name,types:[{{description:description,accept:{{'application/json':['.json']}}}}]}});const writable=await handle.createWritable();await writable.write(text);await writable.close();return handle.name||name;}}const blob=new Blob([text],{{type:'application/json;charset=utf-8'}}),url=URL.createObjectURL(blob),link=document.createElement('a');link.href=url;link.download=name;link.click();URL.revokeObjectURL(url);return name;}}
	function applyImportedAccuracyBundle(bundle){{setAccuracyImportedMode(true,bundle.exported_at||bundle.start_at||'导入快照');accuracyLivePayload=Object.assign({{ok:true}},bundle);if(bundle.inst_id){{const instEl=document.getElementById('accuracyInst');if(instEl)instEl.value=bundle.inst_id;}}if(bundle.horizon_seconds){{const hEl=document.getElementById('accuracyHorizon');if(hEl)hEl.value=String(bundle.horizon_seconds);}}if(bundle.scope){{const sEl=document.getElementById('accuracyScope');if(sEl&&['session','all','imported'].indexOf(bundle.scope)>=0)sEl.value=bundle.scope==='imported'?'session':bundle.scope;}}if(bundle.retention_hours){{const rEl=document.getElementById('accuracyRetentionHours');if(rEl)rEl.value=String(bundle.retention_hours);}}accuracyQueryKey=accuracyQuerySignature();updateAccuracySummary(bundle.summary||{{}});syncAccuracyPoints(bundle.points||[],{{resetView:true}});redrawAccuracyChart();const note=document.getElementById('accuracyNote');if(note)note.textContent='导入快照 · '+accuracyImportedLabel+' · '+bundle.inst_id+' · '+bundle.points.length+' 点 · 窗口 '+bundle.horizon_seconds+' 秒 · 双击图表重置缩放 · 点「返回实时」恢复自动刷新';}}
	async function exportAccuracyChart(){{try{{const bundle=buildAccuracyExportBundle(),text=JSON.stringify(bundle,null,2),name=await saveJsonFile(text,accuracyExportFileName(),'OKX压测图表');const note=document.getElementById('accuracyNote');if(note)note.textContent='已导出 '+name+' · '+bundle.points.length+' 点 · 可在测试页「导入图表」回放';}}catch(e){{alert('导出图表失败：'+e);}}}}
	async function pickAccuracyImportFile(){{if(window.showOpenFilePicker){{const handles=await showOpenFilePicker({{multiple:false,types:[{{description:'OKX压测图表',accept:{{'application/json':['.json']}}}}]}});return await handles[0].getFile();}}return await new Promise(resolve=>{{const input=document.getElementById('accuracyImportInput');if(!input){{resolve(null);return;}}input.onchange=()=>resolve(input.files&&input.files[0]?input.files[0]:null);input.value='';input.click();}});}}
	async function importAccuracyChart(){{try{{const file=await pickAccuracyImportFile();if(!file)return;const bundle=normalizeAccuracyBundle(JSON.parse(await file.text()));applyImportedAccuracyBundle(bundle);}}catch(e){{alert('导入图表失败：'+e);}}}}
	function exitAccuracyImportedMode(){{setAccuracyImportedMode(false,'');fetchAccuracy({{resetView:true}});}}
	function clamp(v,a,b){{return Math.max(a,Math.min(b,v));}}
	function accuracyDirectionValue(o){{const v=String((o&&o.direction)||'');if(v==='\\u505a\\u591a')return 1;if(v==='\\u505a\\u7a7a')return -1;return 0;}}
	function resetAccuracyView(){{accuracyView.start=0;accuracyView.end=1;accuracyView.yZoom=1;accuracyView.yPan=0;accuracyView.priceRg=1;accuracyView.followLatest=true;accuracyView.selectedKey='';updateAccuracyPointPanel(null);}}
	function accuracyPointHtml(o){{if(!o)return '';const hit=o.hit?'合理':'不合理',dir=o.direction||'--',actual=o.actual_direction||'--',ret=o.return_pct!=null?fmt(o.return_pct,3)+'%':'--',outcome=o.outcome_type||'--',paper=o.paper_equity!=null?('$'+fmt(o.paper_equity,0)+' / '+fmtSignedPct(o.paper_pnl_pct,2)+' / '+(o.paper_position||'--')):'--';return '<strong>选中压测点</strong><div class="snapshot-grid"><span>时间</span><b>'+escHtml(o.time||'--')+'</b><span>价格</span><b>'+fmt(o.price,2)+'</b><span>预测</span><b>'+dir+'</b><span>实际</span><b>'+actual+'</b><span>模拟账户</span><b>'+paper+'</b><span>后续价</span><b>'+(o.future_price!=null?fmt(o.future_price,2):'--')+'</b><span>涨跌</span><b>'+ret+'</b><span>判定</span><b>'+hit+'</b><span>类型</span><b>'+escHtml(outcome)+'</b><span>累计准确</span><b>'+(o.accuracy_pct!=null?fmt(o.accuracy_pct,1)+'%':'--')+'</b></div>';}}
	function escHtml(v){{return String(v==null?'':v).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}}
	function updateAccuracyPointPanel(point){{const panel=document.getElementById('accuracyPointPanel');if(!panel)return;if(!point){{panel.hidden=true;panel.innerHTML='';return;}}panel.hidden=false;panel.innerHTML=accuracyPointHtml(point);}}
	function drawAccuracyLeftAxis(ctx,pad,W,H,mn,mx){{ctx.fillStyle='rgba(226,232,240,.82)';ctx.font='12px Segoe UI, Microsoft YaHei';ctx.textAlign='right';ctx.textBaseline='middle';const ch=H-pad.t-pad.b;for(let i=0;i<=4;i++){{const value=mx-(mx-mn)*i/4,y=pad.t+ch*i/4;ctx.fillText(fmt(value,2),pad.l-8,y);}}}}
	function drawAccuracyRightAxis(ctx,pad,W,H,mn,mx){{ctx.fillStyle='rgba(251,191,36,.82)';ctx.font='12px Segoe UI, Microsoft YaHei';ctx.textAlign='left';ctx.textBaseline='middle';const ch=H-pad.t-pad.b;for(let i=0;i<=4;i++){{const value=mx-(mx-mn)*i/4,y=pad.t+ch*i/4;ctx.fillText(fmt(value,0),W-pad.r+6,y);}}}}
	function drawAccuracyCrosshair(ctx,pad,W,H,plot){{const pt=plot.point||{{}};ctx.save();ctx.strokeStyle='rgba(226,232,240,.72)';ctx.setLineDash([5,5]);ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(plot.x,pad.t);ctx.lineTo(plot.x,H-pad.b);ctx.stroke();ctx.beginPath();ctx.moveTo(pad.l,plot.yPrice);ctx.lineTo(W-pad.r,plot.yPrice);ctx.stroke();ctx.strokeStyle='rgba(251,191,36,.55)';ctx.beginPath();ctx.moveTo(pad.l,plot.yPred);ctx.lineTo(W-pad.r,plot.yPred);ctx.stroke();ctx.setLineDash([]);ctx.font='12px Segoe UI, Microsoft YaHei';ctx.fillStyle='rgba(96,165,250,.95)';ctx.textAlign='right';ctx.textBaseline='middle';ctx.fillText(fmt(pt.price,2),pad.l-8,plot.yPrice);ctx.fillStyle='rgba(251,191,36,.95)';ctx.textAlign='left';ctx.fillText(fmt(plot.predVal!=null?plot.predVal:accuracyDirectionValue(pt),0),W-pad.r+6,plot.yPred);ctx.fillStyle='rgba(229,231,235,.95)';ctx.textAlign='center';ctx.textBaseline='top';ctx.fillText(shortTime(pt.time||''),plot.x,H-pad.b+4);ctx.fillStyle='#60a5fa';ctx.beginPath();ctx.arc(plot.x,plot.yPrice,5,0,Math.PI*2);ctx.fill();ctx.fillStyle='#fbbf24';ctx.beginPath();ctx.arc(plot.x,plot.yPred,4.5,0,Math.PI*2);ctx.fill();ctx.restore();}}
	function selectAccuracyPoint(event,strict){{const c=document.getElementById('accuracyChart');if(!c||!accuracyPlotPoints.length)return;const rect=c.getBoundingClientRect(),x=event.clientX-rect.left,y=event.clientY-rect.top;let best=null,bestDist=Infinity;accuracyPlotPoints.forEach(o=>{{const dPrice=Math.hypot(o.x-x,o.yPrice-y),dPred=Math.hypot(o.x-x,o.yPred-y),dist=Math.min(dPrice,dPred);if(dist<bestDist){{best=o;bestDist=dist;}}}});const limit=strict?36:30;if(best&&bestDist<limit){{accuracyView.selectedKey=best.point.time||'';updateAccuracyPointPanel(best.point);drawAccuracyChart();}}else if(!strict){{accuracyView.selectedKey='';updateAccuracyPointPanel(null);drawAccuracyChart();}}}}
	function visibleAccuracyPoints(){{const pts=accuracyView.points||[];if(pts.length<=1)return pts;const n=pts.length,span=Math.max(0.001,accuracyView.end-accuracyView.start),a=Math.floor(accuracyView.start*(n-1)),b=Math.min(n,Math.max(a+2,Math.ceil((accuracyView.start+span)*(n-1))+1));return pts.slice(a,b);}}
	function accuracyLinePointBudget(cw,count){{if(count<=160)return count;const cap=Math.max(120,Math.floor(cw*1.2));return Math.min(count,cap);}}
	function accuracyShowPointMarkers(cw,count){{if(count<=80)return true;const budget=accuracyLinePointBudget(cw,count);return count<=Math.min(100,budget);}}
	function decimateAccuracySeries(points,predVals,maxPoints){{if(points.length<=maxPoints)return{{points:points,predVals:predVals}};const idx=new Set([0,points.length-1]);const buckets=Math.max(1,Math.floor((maxPoints-2)/2));for(let b=0;b<buckets;b++){{const start=Math.floor(b*(points.length-1)/buckets),end=Math.min(points.length-1,Math.floor((b+1)*(points.length-1)/buckets));if(start>=end){{idx.add(start);continue;}}let minI=start,maxI=start;for(let i=start;i<=end;i++){{const p=Number(points[i].price);if(p<Number(points[minI].price))minI=i;if(p>Number(points[maxI].price))maxI=i;}}idx.add(minI);idx.add(maxI);}}let order=Array.from(idx).sort((a,b)=>a-b);if(order.length>maxPoints){{const slim=[];for(let i=0;i<maxPoints;i++)slim.push(order[Math.round(i*(order.length-1)/(maxPoints-1))]);order=slim;}}return{{points:order.map(i=>points[i]),predVals:order.map(i=>predVals[i])}};}}
	function accuracyMinTimeSpan(){{const n=(accuracyView.points||[]).length;return n<=1?1:Math.max(0.02,Math.min(1,36/Math.max(36,n)));}}
	function accuracySpanHours(points){{if(!points||points.length<2)return 0;const a=parsePointTime(points[0].time).getTime(),b=parsePointTime(points[points.length-1].time).getTime();return Math.abs(b-a)/3600000;}}
	function accuracyTimeLabel(t,spanHours){{if(spanHours>=4)return compactTime(t);return shortTime(t);}}
	function syncAccuracyPoints(points,options){{const opts=options||{{}},clean=(points||[]).filter(o=>Number.isFinite(Number(o&&o.price))),hadFullView=accuracyView.start<=0.001&&accuracyView.end>=0.999;if(opts.resetView){{accuracyView.points=clean;resetAccuracyView();return;}}const followLatest=accuracyView.followLatest!==false&&accuracyView.end>=0.995;accuracyView.points=clean;if(clean.length>2&&(followLatest||hadFullView)){{accuracyView.start=0;accuracyView.end=1;accuracyView.followLatest=true;}}else if(followLatest&&clean.length>2){{const span=Math.max(0.001,accuracyView.end-accuracyView.start);accuracyView.end=1;accuracyView.start=Math.max(0,1-span);}}}}
	function redrawAccuracyChart(){{drawAccuracyChart();}}
	async function fetchAccuracy(options){{options=options||{{}};if(fetchAccuracyInFlight&&!options.force)return fetchAccuracyInFlight;const run=async()=>{{const canvas=document.getElementById('accuracyChart');if(!canvas)return;const resetView=!!options.resetView||accuracyQuerySignature()!==accuracyQueryKey;if(accuracyImportedMode&&!options.resetView)return;if(options.resetView)setAccuracyImportedMode(false,'');const inst=(document.getElementById('accuracyInst')||{{}}).value||'BTC-USDT-SWAP',h=(document.getElementById('accuracyHorizon')||{{}}).value||'5',scope=(document.getElementById('accuracyScope')||{{}}).value||'session',retention=accuracyRetentionHours(),interval=configuredMonitorInterval(),note=document.getElementById('accuracyNote');const queryKey=accuracyQuerySignature();accuracyQueryKey=queryKey;try{{if(note&&resetView)note.textContent='正在统计实时预测压测...';const qs='inst_id='+encodeURIComponent(inst)+'&horizon='+encodeURIComponent(h)+'&scope='+encodeURIComponent(scope)+'&retention_hours='+encodeURIComponent(retention)+'&interval_seconds='+encodeURIComponent(interval);const r=await fetch('/api/accuracy-data?'+qs,{{cache:'no-store'}}),p=await r.json();if(!r.ok||p.ok===false)throw new Error(p.error||'统计失败');accuracyLivePayload=p;const s=p.summary||{{}};updateAccuracySummary(s);syncAccuracyPoints(p.points||[],{{resetView:resetView}});redrawAccuracyChart();if(p.replay_pending){{if(note)note.textContent=p.hint||'请先点击下方「开始回放」；切换「回放会话」不会自动启动回放。';return;}}const maxPts=p.max_points||0,intervalSec=p.interval_seconds||interval,retainH=p.retention_hours||retention;let noteExtra='';if(p.scope==='replay'&&(s.total??0)===0&&((s.raw_log_total??0)>0||(s.pending_total??0)>0))noteExtra=' · 回放日志已有数据，验证窗口成熟后曲线才会加点';else if(p.scope==='replay'&&(s.raw_log_total??0)===0)noteExtra=' · 回放进行中，请稍候';else if(p.scope==='session'&&(s.raw_log_total??0)===0)noteExtra=' · 监控刚启动，请等待几轮轮询';else if((s.pending_total??0)>0&&(s.next_pending_seconds??0)>0)noteExtra=' · 还有 '+s.pending_total+' 条待验证，最近约 '+s.next_pending_seconds+' 秒后可加点';else if((s.pending_total??0)>0)noteExtra=' · 还有 '+s.pending_total+' 条待验证，点「刷新压测」即可尝试更新';if(note)note.textContent=(p.scope==='replay'?'回放会话':p.scope==='session'?'本次启动后':'全部历史')+' · 模拟 '+formatPaperSummary(s)+' · 综合 '+fmt(s.prediction_accuracy_pct!=null?s.prediction_accuracy_pct:s.decision_accuracy_pct,1)+'% · 窗口 '+formatHorizonLabel(p.horizon_seconds||h)+((p.time_start&&p.time_end)?(' · 范围 '+compactTime(p.time_start)+' ~ '+compactTime(p.time_end)):'')+' · '+(p.chart_points||accuracyView.points.length||0)+'点 · 双击重置缩放'+noteExtra;}}catch(e){{updateAccuracySummary({{}});syncAccuracyPoints([],{{resetView:true}});redrawAccuracyChart();if(note)note.textContent='预测压测统计失败：'+e;}}}};fetchAccuracyInFlight=run();try{{await fetchAccuracyInFlight;}}finally{{fetchAccuracyInFlight=null;}}}}
	function switchAccuracyToLiveSession(){{const scopeEl=document.getElementById('accuracyScope');if(scopeEl&&scopeEl.value!=='session'){{accuracyScopeSyncing=true;scopeEl.value='session';accuracyScopeSyncing=false;}}setAccuracyImportedMode(false,'');accuracyQueryKey='';fetchAccuracy({{resetView:true}});}}
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
	function updateAccuracySummary(s){{const box=document.getElementById('accuracySummary');if(!box)return;s=s||{{}};const pred=s.prediction_accuracy_pct!=null?s.prediction_accuracy_pct:s.decision_accuracy_pct,tradeTotal=s.trade_signal_total??0,tradeDir=s.trade_direction_accuracy_pct,tradePlan=s.trade_signal_accuracy_pct,watchTotal=s.watch_total??0,watchPct=s.watch_reasonable_pct,watchMiss=s.watch_missed_pct,resolved=s.trade_resolved_total??0,pending=s.pending_total??0,nextPending=s.next_pending_seconds??0,execLine=tradeTotal>=5?(fmt(s.entry_touch_pct,1)+'% / '+fmt(s.no_fill_pct,1)+'%'):'样本不足',paperPts=s.paper_log_points??0,verifiedLine=(s.total??0)+' / '+(s.raw_log_total??s.total??0)+(pending>0?(' · '+pending+'待'):'');const vals=[['模拟账户($10k)',formatPaperSummary(s)+' / '+(s.paper_trade_count??0)+'笔 · '+paperPts+'轮','paper'],['综合预测准确度',pred!=null?fmt(pred,1)+'% / '+(s.decision_total??s.total??0):'--','primary'],['可靠性等级',(s.reliability_level||'--')+' / '+(s.reliability_score!=null?fmt(s.reliability_score,1):'--')],['已验证/日志',verifiedLine],['待验证',pending>0?(nextPending>0?('约'+nextPending+'秒后下一条'):'窗口已够，刷新即验证'):'--'],['观望准确度',watchPct!=null?fmt(watchPct,1)+'% / '+watchTotal:'--'],['观望错失率',watchMiss!=null?fmt(watchMiss,1)+'% / '+watchTotal:'--'],['交易方向命中',tradeDir!=null?fmt(tradeDir,1)+'% / '+tradeTotal:'--'],['交易执行合理率',tradePlan!=null?fmt(tradePlan,1)+'% / '+tradeTotal:'--'],['验证窗口',formatHorizonLabel(s.horizon_seconds)+' · 阈值 '+fmt(s.threshold_pct,3)+'%'],['入场触达/未成交',execLine]];if(resolved>=3)vals.push(['止盈/止损/胜率',fmt(s.take_profit_pct,1)+'% / '+fmt(s.stop_hit_pct,1)+'% / '+fmt(s.trade_win_rate_pct,1)+'% ('+resolved+')']);box.innerHTML=vals.map(v=>'<div class="'+(v[2]==='paper'?'accuracy-paper-primary':(v[2]==='primary'?'accuracy-primary':''))+'"><span>'+v[0]+'</span><b>'+v[1]+'</b></div>').join('');}}
	function drawAccuracyChart(points){{if(Array.isArray(points))syncAccuracyPoints(points,{{resetView:true}});const c=document.getElementById('accuracyChart');if(!c)return;accuracyPlotPoints=[];const d=window.devicePixelRatio||1,r=c.getBoundingClientRect();c.width=Math.max(1,r.width*d);c.height=Math.max(1,r.height*d);const ctx=c.getContext('2d');ctx.setTransform(d,0,0,d,0,0);const W=r.width,H=r.height,pad={{l:58,r:52,t:24,b:40}},cw=W-pad.l-pad.r,ch=H-pad.t-pad.b;ctx.clearRect(0,0,W,H);ctx.fillStyle='#0f172a';ctx.fillRect(0,0,W,H);let clean=visibleAccuracyPoints();if(!clean.length){{updateAccuracyPointPanel(null);ctx.fillStyle='rgba(203,213,225,.82)';ctx.textAlign='center';ctx.textBaseline='middle';ctx.font='13px Segoe UI, Microsoft YaHei';const scopeNow=(document.getElementById('accuracyScope')||{{}}).value||'session',emptyMsg=scopeNow==='replay'?'回放会话暂无数据：请先点击下方「开始回放」，切换范围不会自动启动回放':(scopeNow==='all'?'全部历史暂无已验证点：请确认日志中有该币种数据':'暂无可验证样本：live 监控请启动后选「本次启动后」；离线回放请点「开始回放」后选「回放会话」');ctx.fillText(emptyMsg,W/2,H/2);return;}}const visibleCount=clean.length,totalCount=(accuracyView.points||[]).length;if(clean.length===1)clean=[clean[0],Object.assign({{}},clean[0])];const priceVals=clean.map(o=>Number(o.price)),mn=Math.min(...priceVals),mx=Math.max(...priceVals),basePriceRg=Math.max((mx||1)*0.001,(mx-mn)*1.16,0.01);let pred=0;const predVals=clean.map(o=>{{pred+=accuracyDirectionValue(o);return pred;}}),pmn=Math.min(...predVals),pmx=Math.max(...predVals),basePredRg=Math.max(1,(pmx-pmn)*1.16,1);const yZoom=Math.max(0.35,accuracyView.yZoom||1),yPan=accuracyView.yPan||0,normTop=0.5+yPan+0.5/yZoom,normBottom=0.5+yPan-0.5/yZoom,priceSpan=Math.max(0.000001,basePriceRg),predSpan=Math.max(0.000001,basePredRg),priceAxisBottom=mn+normBottom*priceSpan,priceAxisTop=mn+normTop*priceSpan,predAxisBottom=pmn+normBottom*predSpan,predAxisTop=pmn+normTop*predSpan;accuracyView.priceRg=priceSpan/yZoom;const pricePlotRg=Math.max(0.000001,priceAxisTop-priceAxisBottom),predPlotRg=Math.max(0.000001,predAxisTop-predAxisBottom),priceFlat=Math.abs(mx-mn)<1e-9,predFlat=Math.abs(pmx-pmn)<1e-9,bothFlat=priceFlat&&predFlat,priceY=o=>{{let norm=priceFlat?0.5:(Number(o.price)-priceAxisBottom)/pricePlotRg;return pad.t+ch-norm*ch-(bothFlat?16:0);}},predValY=val=>{{let norm=predFlat?0.5:(val-predAxisBottom)/predPlotRg;return pad.t+ch-norm*ch+(bothFlat?16:0);}},stepFull=cw/Math.max(1,clean.length-1),xAtFull=i=>pad.l+stepFull*i;const drawBudget=accuracyLinePointBudget(cw,clean.length),showMarkers=accuracyShowPointMarkers(cw,clean.length),decimated=decimateAccuracySeries(clean,predVals,drawBudget),drawPts=decimated.points,drawPred=decimated.predVals,stepDraw=cw/Math.max(1,drawPts.length-1),xAtDraw=i=>pad.l+stepDraw*i,denseMode=drawPts.length<clean.length,priceLineWidth=denseMode?1.5:2.6,predLineWidth=denseMode?1.1:2;ctx.strokeStyle='rgba(148,163,184,.22)';ctx.lineWidth=1;for(let i=0;i<=4;i++){{const y=pad.t+ch*i/4;ctx.beginPath();ctx.moveTo(pad.l,y);ctx.lineTo(W-pad.r,y);ctx.stroke();}}drawAccuracyLeftAxis(ctx,pad,W,H,priceAxisBottom,priceAxisTop);drawAccuracyRightAxis(ctx,pad,W,H,predAxisBottom,predAxisTop);drawAccuracyTimeAxis(ctx,W,H,pad,clean,cw);ctx.beginPath();drawPts.forEach((o,i)=>{{const px=xAtDraw(i),py=predValY(drawPred[i]);if(i===0)ctx.moveTo(px,py);else ctx.lineTo(px,py);}});ctx.strokeStyle='rgba(251,191,36,.92)';ctx.lineWidth=predLineWidth;ctx.stroke();ctx.beginPath();drawPts.forEach((o,i)=>{{const px=xAtDraw(i),py=priceY(o);if(i===0)ctx.moveTo(px,py);else ctx.lineTo(px,py);}});ctx.strokeStyle='rgba(96,165,250,.95)';ctx.lineWidth=priceLineWidth;ctx.stroke();const paperEq=clean.map(o=>Number(o.paper_equity)).filter(v=>Number.isFinite(v));if(paperEq.length>=2){{const pMn=Math.min(...paperEq),pMx=Math.max(...paperEq),pRg=Math.max(0.01,pMx-pMn),paperY=v=>pad.t+ch-((v-pMn)/pRg)*ch;let started=false;ctx.beginPath();clean.forEach((o,i)=>{{const pe=Number(o.paper_equity);if(!Number.isFinite(pe))return;const px=xAtFull(i),py=paperY(pe);if(!started){{ctx.moveTo(px,py);started=true;}}else ctx.lineTo(px,py);}});if(started){{ctx.strokeStyle='rgba(74,222,128,.92)';ctx.lineWidth=denseMode?1.2:2;ctx.stroke();}}}}const markerPriceRadius=showMarkers?(visibleCount>160?1.6:2.8):0,markerPredRadius=showMarkers?(visibleCount>160?1.4:2.4):0;clean.forEach((o,i)=>{{const px=xAtFull(i),pyPrice=priceY(o),pyPred=predValY(predVals[i]),selected=(o.time||'')===accuracyView.selectedKey;accuracyPlotPoints.push({{x:px,yPrice:pyPrice,yPred:pyPred,predVal:predVals[i],point:o}});if(!showMarkers&&!selected)return;const pr=selected?4.2:(markerPriceRadius||2.8),qr=selected?3.8:(markerPredRadius||2.4);ctx.lineWidth=selected?1.5:1;ctx.strokeStyle='rgba(15,23,42,.85)';ctx.fillStyle=selected?'#22d3ee':(o.hit?'#34d399':'#fb7185');ctx.beginPath();ctx.arc(px,pyPrice,pr,0,Math.PI*2);ctx.fill();ctx.stroke();ctx.strokeStyle='rgba(15,23,42,.85)';ctx.fillStyle=selected?'#fde047':'#fbbf24';ctx.beginPath();ctx.arc(px,pyPred,qr,0,Math.PI*2);ctx.fill();ctx.stroke();}});let selectedPlot=null;if(accuracyView.selectedKey){{selectedPlot=accuracyPlotPoints.find(o=>(o.point.time||'')===accuracyView.selectedKey)||null;if(selectedPlot){{drawAccuracyCrosshair(ctx,pad,W,H,selectedPlot);updateAccuracyPointPanel(selectedPlot.point);}}else{{accuracyView.selectedKey='';updateAccuracyPointPanel(null);}}}}else{{updateAccuracyPointPanel(null);}}ctx.fillStyle='rgba(226,232,240,.92)';ctx.font='12px Segoe UI, Microsoft YaHei';ctx.textAlign='left';ctx.textBaseline='top';ctx.fillText('蓝线：价格',pad.l,pad.t+4);ctx.fillStyle='#4ade80';ctx.fillText('绿线：模拟账户',pad.l+72,pad.t+4);ctx.fillStyle='#fbbf24';ctx.fillText('黄线：预测方向',pad.l+168,pad.t+4);ctx.fillStyle=denseMode?'#fcd34d':'rgba(203,213,225,.72)';ctx.fillText(' · 绘制 '+drawPts.length+'/'+visibleCount+(denseMode?' 已抽稀':' 全量'),pad.l+248,pad.t+4);ctx.textAlign='right';ctx.fillStyle='rgba(203,213,225,.68)';ctx.textBaseline='top';ctx.fillText('总计 '+totalCount+' 点 · 滚轮缩放 · 拖动平移 · 点击选点',W-pad.r,H-26);}}
	function setupAccuracyChartInteractions(){{const c=document.getElementById('accuracyChart');if(!c||c.dataset.panZoomBound)return;c.dataset.panZoomBound='1';c.addEventListener('wheel',e=>{{if(!accuracyView.points.length)return;e.preventDefault();const rect=c.getBoundingClientRect(),mx=(e.clientX-rect.left)/Math.max(1,rect.width),factor=e.deltaY>0?1.18:0.85,n=accuracyView.points.length;if(e.shiftKey||n<=2){{accuracyView.yZoom=clamp(accuracyView.yZoom/factor,0.35,12);}}else{{const span=accuracyView.end-accuracyView.start,newSpan=clamp(span*factor,accuracyMinTimeSpan(),1),anchor=accuracyView.start+span*mx;accuracyView.start=clamp(anchor-newSpan*mx,0,1-newSpan);accuracyView.end=accuracyView.start+newSpan;}}accuracyView.followLatest=accuracyView.end>=0.995;drawAccuracyChart();}},{{passive:false}});c.addEventListener('mousedown',e=>{{accuracyView.drag={{x:e.clientX,y:e.clientY,start:accuracyView.start,end:accuracyView.end,yPan:accuracyView.yPan,moved:false}};c.classList.add('dragging');}});window.addEventListener('mousemove',e=>{{const g=accuracyView.drag;if(!g)return;const rect=c.getBoundingClientRect(),dx=(e.clientX-g.x)/Math.max(1,rect.width),dy=(e.clientY-g.y)/Math.max(1,rect.height),span=g.end-g.start;if(Math.abs(e.clientX-g.x)>3||Math.abs(e.clientY-g.y)>3)g.moved=true;let ns=clamp(g.start-dx*span,0,1-span),ne=ns+span;accuracyView.start=ns;accuracyView.end=ne;accuracyView.yPan=clamp(g.yPan+dy*1.35,-3,3);accuracyView.followLatest=accuracyView.end>=0.995;drawAccuracyChart();}});window.addEventListener('mouseup',()=>{{if(accuracyView.drag){{setTimeout(function(){{accuracyView.drag=null;}},0);}}c.classList.remove('dragging');}});c.addEventListener('click',e=>{{if(accuracyView.drag&&accuracyView.drag.moved)return;selectAccuracyPoint(e,false);}});c.addEventListener('dblclick',e=>{{if(accuracyView.drag&&accuracyView.drag.moved)return;resetAccuracyView();drawAccuracyChart();}});}}
	setupAccuracyChartInteractions();
	let virtualTick=0;
let configuredMonitorInsts={json.dumps(selected_instruments, ensure_ascii=False)};
let monitorInst={json.dumps(monitor_initial, ensure_ascii=False)}, monitorPayload=null, monitorLiveMode=false;
let monitorSeriesByInst={{}}, monitorLastTickerAt=0;
let virtualSeriesByInst={{}};
let monitorViewStart=0, monitorViewEnd=1;
let monitorVisiblePoints=[], monitorPlotPoints=[], monitorSelectedKey='', monitorLatestPoint=null;
let monitorYZoom=1, monitorYPan=0, monitorYRange=1, monitorDrag=null;
let monitorStartedAt='', monitorElapsedSeconds=0, monitorStatusRunning=false, monitorWasRunning=false;
function refreshMonitorTabs(insts){{if(!Array.isArray(insts))return;configuredMonitorInsts=insts;const box=document.querySelector('.coin-tabs');if(!box)return;if(!configuredMonitorInsts.length){{monitorInst='';box.innerHTML='<span class="empty-coin">请先在配置页选择监控币种</span>';clearChartMessage('请先在配置页选择监控币种');return;}}if(configuredMonitorInsts.indexOf(monitorInst)<0)monitorInst=configuredMonitorInsts[0];box.innerHTML=configuredMonitorInsts.map(inst=>'<button class="button coin-tab '+(inst===monitorInst?'active':'')+'" type="button" data-monitor-inst="'+inst+'">'+inst+'</button>').join('');bindMonitorTabs();}}
function bindMonitorTabs(){{document.querySelectorAll('[data-monitor-inst]').forEach(b=>b.onclick=()=>{{const next=b.getAttribute('data-monitor-inst');if(configuredMonitorInsts.indexOf(next)<0)return;monitorInst=next;monitorViewStart=0;monitorViewEnd=1;monitorYZoom=1;monitorYPan=0;monitorSelectedKey='';monitorLastTickerAt=0;document.querySelectorAll('[data-monitor-inst]').forEach(o=>o.classList.remove('active'));b.classList.add('active');bootstrapMonitorChart();}});}}
function virtualBase(){{return monitorInst==='ETH-USDT-SWAP'?3200:63000;}}
function gen(n,base){{let a=[],v=base,now=Date.now();const step=Math.max(.18,base*.000055);for(let i=0;i<n;i++){{v+=(Math.random()-.5)*step+Math.sin((i+virtualTick)/18)*step*.18;a.push({{time:new Date(now-(n-i-1)*1000).toLocaleString(),price:v,kind:'virtual'}});}}return a;}}
function nextVirtualSeries(){{if(!monitorInst)return [];virtualTick++;let series=virtualSeriesByInst[monitorInst]||[];if(series.length<2){{series=gen(260,virtualBase());}}else{{const last=Number(series[series.length-1].price)||virtualBase(),step=Math.max(.18,virtualBase()*.00007),drift=Math.sin(virtualTick/16)*step*.2,price=Math.max(.01,last+(Math.random()-.5)*step+drift);series=[...series.slice(-259),{{time:new Date().toLocaleString(),price:price,kind:'virtual'}}];}}virtualSeriesByInst[monitorInst]=series;return series;}}
function visiblePoints(points){{if(!points||points.length<2)return points||[];const s=Math.max(0,Math.floor(monitorViewStart*(points.length-1))),e=Math.min(points.length,Math.ceil(monitorViewEnd*(points.length-1))+1);return points.slice(s,Math.max(s+2,e));}}
function fmt(v,d){{const n=Number(v);return Number.isFinite(n)?n.toFixed(d):'--';}}
function fmtPct(v){{const n=Number(v);return Number.isFinite(n)?n.toFixed(4)+'%':'--';}}
function shortTime(t){{if(!t)return '--';const s=String(t);return s.length>=16?s.slice(11,16):s;}}
function compactTime(t){{if(!t)return '--';const s=String(t);return s.length>=16?s.slice(5,16):s;}}
function displayValue(v){{return v===undefined||v===null||v===''?'--':v;}}
function compactList(v,limit){{if(!Array.isArray(v)||!v.length)return '--';return v.slice(0,limit||3).join(' / ')+(v.length>(limit||3)?' ...':'');}}
function fmtScore(v){{const n=Number(v);return Number.isFinite(n)?String(Math.round(n)):'--';}}
function fmtSignedPct(v,d){{const n=Number(v);return Number.isFinite(n)?(n>=0?'+':'')+n.toFixed(d)+'%':'--';}}
function fmtBool(v){{return v===true?'是':(v===false?'否':'--');}}
function summarizeLayers(layers){{if(!layers||typeof layers!=='object')return '--';const keys=['market_regime_score','trend_score','momentum_score','volume_price_score','derivatives_score','orderbook_score','entry_quality_score','risk_control_score'];return keys.filter(k=>layers[k]!==undefined&&layers[k]!==null).map(k=>k.replace('_score','').replace('market_regime','状态').replace('volume_price','量价').replace('entry_quality','入场').replace('risk_control','风控').replace('orderbook','盘口').replace('derivatives','合约').replace('momentum','动量').replace('trend','趋势')+':'+fmtScore(layers[k])).join(' / ')||'--';}}
function formatDuration(seconds){{let s=Math.max(0,Math.floor(Number(seconds)||0)),h=Math.floor(s/3600),m=Math.floor((s%3600)/60),sec=s%60,pad=n=>String(n).padStart(2,'0');return h>0?h+'小时 '+pad(m)+'分 '+pad(sec)+'秒':pad(m)+':'+pad(sec);}}
function updateMonitorUptime(status){{const el=document.getElementById('monitorUptime');if(status){{monitorStatusRunning=!!status.running;monitorStartedAt=status.started_at||'';monitorElapsedSeconds=Number(status.elapsed_seconds)||0;}}if(!el)return;if(monitorStatusRunning)el.textContent='已监控：'+formatDuration(monitorElapsedSeconds);else el.textContent=monitorStartedAt?'已停止：'+formatDuration(monitorElapsedSeconds):'已监控：未启动';}}
function formatPaperAccount(paper){{if(!paper||paper.equity==null)return '--';const eq=Number(paper.equity),pnl=Number(paper.pnl_usd)||0,pct=Number(paper.pnl_pct)||0,sign=pnl>=0?'+':'';return '$'+fmt(eq,0)+' ('+sign+fmt(pnl,0)+' / '+sign+fmt(pct,2)+'%) · '+(paper.position_label||paper.position||'--');}}
function updatePaperAccount(paper){{const el=document.getElementById('monitorPaperAccount');if(!el)return;if(!paper||paper.equity==null){{el.textContent='模拟账户：--';el.classList.remove('paper-up','paper-down');return;}}const pnl=Number(paper.pnl_usd)||0;el.textContent='模拟账户 '+formatPaperAccount(paper);el.classList.remove('paper-up','paper-down');el.classList.add(pnl>=0?'paper-up':'paper-down');}}
function updateChartFooter(points){{const c=document.getElementById('monitorPointCount');if(!c)return;c.textContent='数据点：'+((points&&points.length)||0);}}
function parsePointTime(t){{const s=String(t||'');const m=s.match(/^(\\d{{4}})-(\\d{{2}})-(\\d{{2}})[ T](\\d{{2}}):(\\d{{2}})(?::(\\d{{2}}))?/);if(m)return new Date(Number(m[1]),Number(m[2])-1,Number(m[3]),Number(m[4]),Number(m[5]),Number(m[6]||0));const d=new Date(s);return Number.isFinite(d.getTime())?d:new Date();}}
function formatPointTime(d){{const pad=n=>String(n).padStart(2,'0');return d.getFullYear()+'-'+pad(d.getMonth()+1)+'-'+pad(d.getDate())+' '+pad(d.getHours())+':'+pad(d.getMinutes())+':'+pad(d.getSeconds());}}
function bucket1mTime(t){{const d=parsePointTime(t);d.setSeconds(0,0);return formatPointTime(d);}}
function normalizeClientPoint(point,fallbackKind){{if(!point)return null;const price=Number(point.price);if(!Number.isFinite(price))return null;const normalized=Object.assign({{}},point);normalized.kind=normalized.kind||fallbackKind||'realtime';normalized.time=String(point.time||new Date().toLocaleString());if(normalized.kind!=='virtual')normalized.time=bucket1mTime(normalized.time);normalized.price=price;return normalized;}}
function mergeClientPoints(existing,incoming,maxPoints){{const merged=[],indexByTime={{}};[...(existing||[]),...(incoming||[])].forEach(point=>{{const normalized=normalizeClientPoint(point,'realtime');if(!normalized)return;const key=normalized.time||'';if(key&&Object.prototype.hasOwnProperty.call(indexByTime,key)){{merged[indexByTime[key]]=Object.assign({{}},merged[indexByTime[key]],normalized);return;}}if(key)indexByTime[key]=merged.length;merged.push(normalized);}});merged.sort((a,b)=>parsePointTime(a.time).getTime()-parsePointTime(b.time).getTime());return merged.slice(-Math.max(2,maxPoints||20000));}}
function setMonitorSeries(points){{const series=mergeClientPoints([],points||[],20000);monitorSeriesByInst[monitorInst]=series;monitorPayload={{points:series}};return series;}}
function appendMonitorPoints(points){{const series=mergeClientPoints(monitorSeriesByInst[monitorInst]||[],points||[],20000);monitorSeriesByInst[monitorInst]=series;monitorPayload={{points:series}};return series;}}
function drawMonitorSeries(metaText){{const series=monitorSeriesByInst[monitorInst]||[];if(series.length){{drawChart('monitorChart',series,document.getElementById('monitorPrice'),document.getElementById('monitorMeta'),document.getElementById('monitorLoading'));document.getElementById('monitorTitle').textContent=monitorInst+' 实时走势';const m=document.getElementById('monitorMeta');if(m&&metaText)m.textContent=metaText;return true;}}return false;}}
	function snapshotHtml(point,title){{if(!point)return '<strong>Snapshot</strong><div class="snapshot-grid"><span>价格</span><b>--</b><span>时间</span><b>--</b><span>评分</span><b>--</b><span>方向</span><b>--</b></div>';const hasMetrics=point.raw_total_score!==undefined&&point.raw_total_score!==null,kind=point.kind==='history'?(hasMetrics?'历史K线+日志指标':'历史K线'):'实时快照',dir=(point.raw_direction&&point.final_direction&&point.raw_direction!==point.final_direction)?(point.raw_direction+' → '+point.final_direction):(point.final_direction||point.direction||'--'),scoreText='综合 '+fmtScore(point.score)+' / 观察 '+fmtScore(point.raw_total_score)+' / 交易 '+fmtScore(point.final_trade_score),riskText=(point.market_risk_level||point.risk_level||'--')+' / '+(point.trade_action_level||'--'),lsAvail=point.long_short_available===false?'不可用':'可用',longShort=fmt(Number(point.long_ratio)*100,1)+'/'+fmt(Number(point.short_ratio)*100,1)+'% ('+lsAvail+')',warmup='OI '+fmtBool(point.oi_warmup_ready)+' / 费率 '+fmtBool(point.funding_warmup_ready),volumeText=fmt(point.volume_multiplier,2)+'x / 阈值 '+fmt(point.volume_threshold_used,2)+'x / '+displayValue(point.volume_direction)+'/'+displayValue(point.volume_trend),bookText=fmtSignedPct(Number(point.order_book_imbalance)*100,1)+' / top5 '+fmtSignedPct(Number(point.order_book_imbalance_5)*100,1)+' / spread '+fmt(point.spread_pct,4)+'%',qualityText=(point.data_quality_reliable===true?'可靠':(point.data_quality_reliable===false?'不足':'--'))+' / 15m确认K '+displayValue(point.data_quality_count),signals=compactList(point.signals,4),waitFor=compactList(point.wait_for,3);return '<strong>'+title+' · '+kind+'</strong><div class="snapshot-grid"><span>时间</span><b>'+compactTime(point.time)+'</b><span>价格</span><b>'+fmt(point.price,2)+'</b><span>分数</span><b>'+scoreText+'</b><span>方向</span><b>'+dir+'</b><span>模拟账户</span><b>'+(point.paper_equity!=null?formatPaperAccount({{equity:point.paper_equity,pnl_usd:point.paper_pnl_usd,pnl_pct:point.paper_pnl_pct,position_label:point.paper_position}}):'--')+'</b><span>市场/策略</span><b>'+displayValue(point.market_regime)+' / '+displayValue(point.strategy_template)+'</b><span>风险/动作</span><b>'+riskText+'</b><span>入场质量</span><b>'+displayValue(point.entry_quality)+' / '+fmtScore(point.entry_quality_score)+'</b><span>风控分</span><b>'+fmtScore(point.risk_control_score)+'</b><span>入场</span><b>'+displayValue(point.entry)+'</b><span>止损/止盈</span><b>'+displayValue(point.stop_loss)+' / '+displayValue(point.take_profit)+'</b><span>等待条件</span><b>'+waitFor+'</b><span>信号</span><b>'+signals+'</b><span>数据质量</span><b>'+qualityText+'</b><span>预热</span><b>'+warmup+'</b><span>OI</span><b>'+fmt(point.open_interest,2)+'</b><span>OI 15m</span><b>'+fmtPct(point.oi_change_pct_15m)+'</b><span>资金费率</span><b>'+fmt(point.funding_rate,6)+' / 变化 '+fmt(point.funding_change,6)+'</b><span>多头/空头</span><b>'+longShort+'</b><span>放量</span><b>'+volumeText+'</b><span>ATR 15m</span><b>'+fmt(point.atr_pct_15m,4)+'% / '+displayValue(point.volatility_regime)+'</b><span>盘口</span><b>'+bookText+'</b><span>分层</span><b>'+summarizeLayers(point.layer_scores)+'</b></div>';}}
function updateSnapshotPanel(point,title){{const panel=document.getElementById('snapshotPanel');if(panel)panel.innerHTML=snapshotHtml(point,title||'Snapshot');}}
function drawTimeAxis(x,W,H,p,points,cw){{if(!points||points.length<2)return;const maxLabels=Math.max(2,Math.min(8,Math.floor(W/150))),step=Math.max(1,Math.floor((points.length-1)/(maxLabels-1)));x.fillStyle='rgba(229,231,235,.9)';x.font='12px Segoe UI, Microsoft YaHei, Arial';x.textAlign='center';x.textBaseline='top';for(let i=0;i<points.length;i+=step){{const px=p.l+cw*i/(points.length-1);x.fillText(shortTime(points[i].time),px,H-24);}}const lastIndex=points.length-1;if((lastIndex%step)!==0){{x.fillText(shortTime(points[lastIndex].time),W-p.r,H-24);}}}}
function drawAccuracyTimeAxis(x,W,H,p,points,cw){{if(!points||points.length<2)return;const spanHours=accuracySpanHours(points),maxLabels=Math.max(2,Math.min(10,Math.floor(W/110))),step=Math.max(1,Math.floor((points.length-1)/(maxLabels-1)));x.fillStyle='rgba(229,231,235,.9)';x.font='12px Segoe UI, Microsoft YaHei, Arial';x.textAlign='center';x.textBaseline='top';for(let i=0;i<points.length;i+=step){{const px=p.l+cw*i/(points.length-1);x.fillText(accuracyTimeLabel(points[i].time,spanHours),px,H-24);}}const lastIndex=points.length-1;if((lastIndex%step)!==0){{x.fillText(accuracyTimeLabel(points[lastIndex].time,spanHours),W-p.r,H-24);}}}}
function drawPriceAxis(x,W,H,p,mn,mx){{x.fillStyle='rgba(229,231,235,.82)';x.font='12px Segoe UI, Microsoft YaHei, Arial';x.textAlign='right';x.textBaseline='middle';for(let i=0;i<=4;i++){{const value=mx-(mx-mn)*i/4,y=p.t+(H-p.t-p.b)*i/4;x.fillText(value.toFixed(2),W-8,y);}}}}
function clearChartMessage(text){{const c=document.getElementById('monitorChart'),l=document.getElementById('monitorLoading'),p=document.getElementById('monitorPrice'),m=document.getElementById('monitorMeta'),t=document.getElementById('monitorTitle');monitorSelectedKey='';monitorVisiblePoints=[];monitorPlotPoints=[];monitorLatestPoint=null;if(c){{const r=c.getBoundingClientRect(),x=c.getContext('2d');c.width=Math.max(1,r.width);c.height=Math.max(1,r.height);x.clearRect(0,0,r.width,r.height);}}if(l){{l.style.display='grid';l.textContent=text;}}if(p)p.innerHTML='<strong>--</strong><span>无数据</span>';if(m)m.textContent=text;if(t)t.textContent='未配置币种';updateChartFooter([]);updatePaperAccount(null);updateSnapshotPanel(null,'Snapshot');}}
function drawChart(id,points,priceBox,metaBox,loading){{const c=document.getElementById(id);if(!c||!points||points.length<1)return;if(points.length===1)points=[points[0],{{time:points[0].time,price:points[0].price}}];monitorLatestPoint=points[points.length-1];points=visiblePoints(points);monitorVisiblePoints=points;updateChartFooter(points);if(loading)loading.style.display='none';const d=window.devicePixelRatio||1,r=c.getBoundingClientRect();c.width=r.width*d;c.height=r.height*d;const x=c.getContext('2d');x.setTransform(d,0,0,d,0,0);const W=r.width,H=r.height,p={{l:34,r:74,t:18,b:58}},cw=W-p.l-p.r,ch=H-p.t-p.b,prices=points.map(q=>q.price);let mn=Math.min(...prices),mx=Math.max(...prices);const rawCenter=(mn+mx)/2,baseRg=Math.max(.01,(mx-mn)*1.16),center=rawCenter+monitorYPan,rg=baseRg/monitorYZoom;monitorYRange=rg;mn=center-rg/2;mx=center+rg/2;monitorPlotPoints=[];x.clearRect(0,0,W,H);x.strokeStyle='rgba(255,255,255,.12)';for(let i=0;i<=4;i++){{const y=p.t+ch*i/4;x.beginPath();x.moveTo(p.l,y);x.lineTo(W-p.r,y);x.stroke();}}x.beginPath();points.forEach((q,i)=>{{const px=p.l+cw*i/(points.length-1),py=p.t+ch-((q.price-mn)/rg)*ch;monitorPlotPoints.push({{x:px,y:py,point:q}});if(i===0)x.moveTo(px,py);else x.lineTo(px,py);}});const up=points[points.length-1].price>=points[0].price;x.strokeStyle=up?'#fb7185':'#22c55e';x.lineWidth=2;x.stroke();x.fillStyle=up?'rgba(251,113,133,.10)':'rgba(34,197,94,.08)';x.lineTo(W-p.r,H-p.b);x.lineTo(p.l,H-p.b);x.closePath();x.fill();drawTimeAxis(x,W,H,p,points,cw);drawPriceAxis(x,W,H,p,mn,mx);if(monitorSelectedKey){{const hit=monitorPlotPoints.find(o=>(o.point.time||'')===monitorSelectedKey);if(hit){{x.strokeStyle='rgba(255,255,255,.62)';x.setLineDash([4,5]);x.beginPath();x.moveTo(hit.x,p.t);x.lineTo(hit.x,H-p.b);x.stroke();x.beginPath();x.moveTo(p.l,hit.y);x.lineTo(W-p.r,hit.y);x.stroke();x.setLineDash([]);x.fillStyle='#60a5fa';x.beginPath();x.arc(hit.x,hit.y,5,0,7);x.fill();updateSnapshotPanel(hit.point,'选中点');}}else{{monitorSelectedKey='';updateSnapshotPanel(monitorLatestPoint,'最新快照');}}}}if(priceBox){{const first=points[0].price,last=points[points.length-1].price,chg=last-first,pct=first?chg/first*100:0;priceBox.classList.toggle('up',chg>=0);priceBox.classList.toggle('down',chg<0);priceBox.innerHTML='<strong>'+last.toFixed(2)+'</strong><span>'+(chg>=0?'+':'')+chg.toFixed(2)+' / '+(pct>=0?'+':'')+pct.toFixed(2)+'%</span>';}}if(!monitorSelectedKey)updateSnapshotPanel(monitorLatestPoint,'最新快照');if(metaBox)metaBox.textContent='更新：'+new Date().toLocaleTimeString();}}
function drawVirtualMonitor(){{if(!monitorInst){{clearChartMessage('请先在配置页选择监控币种');return;}}drawChart('monitorChart',nextVirtualSeries(),document.getElementById('monitorPrice'),document.getElementById('monitorMeta'),document.getElementById('monitorLoading'));document.getElementById('monitorTitle').textContent=monitorInst+' 虚拟走势';const m=document.getElementById('monitorMeta');if(m)m.textContent='虚拟行情预览 · 点击开始监控后切换真实数据';}}
function setMonitorButtonState(state,text){{const btn=document.getElementById('monitorToggleBtn'),meta=document.getElementById('monitorMeta');if(!btn)return;btn.classList.remove('is-running','is-starting');btn.disabled=false;if(state==='starting'){{btn.classList.add('is-starting');btn.textContent='启动中...';btn.disabled=true;if(meta)meta.textContent=text||'正在启动监控进程...';}}else if(state==='running'){{btn.classList.add('is-running');btn.textContent='停止监控';if(meta&&text)meta.textContent=text;}}else if(state==='stopping'){{btn.classList.add('is-starting');btn.textContent='停止中...';btn.disabled=true;if(meta)meta.textContent=text||'正在停止监控进程...';}}else{{btn.textContent='开始监控';if(meta&&text)meta.textContent=text;}}}}
async function syncMonitorStatus(){{try{{const r=await fetch('/api/status',{{cache:'no-store'}}),p=await r.json();updateMonitorUptime(p);setMonitorButtonState(p.running?'running':'stopped',p.text||'');if(p.running&&!monitorWasRunning&&typeof switchAccuracyToLiveSession==='function')switchAccuracyToLiveSession();monitorWasRunning=!!p.running;return p;}}catch(e){{return null;}}}}
function showRealtimeWaiting(clearChart){{if(!monitorInst){{clearChartMessage('请先在配置页选择监控币种');return;}}monitorLiveMode=true;const c=document.getElementById('monitorChart'),l=document.getElementById('monitorLoading'),m=document.getElementById('monitorMeta'),t=document.getElementById('monitorTitle'),p=document.getElementById('monitorPrice');if(clearChart&&c){{const r=c.getBoundingClientRect(),x=c.getContext('2d');c.width=Math.max(1,r.width);c.height=Math.max(1,r.height);x.clearRect(0,0,r.width,r.height);updateChartFooter([]);}}if(l){{l.style.display=clearChart?'grid':'none';l.textContent='监控已启动，正在等待真实价格数据...';}}if(m)m.textContent=clearChart?'实时监控已启动 · 等待第一条价格数据':'已获取最新价 · 等待完整分析数据';if(t)t.textContent=monitorInst+' 实时走势';if(clearChart&&p)p.innerHTML='<strong>--</strong><span>等待真实数据</span>';}}
async function fetchMonitor(){{if(!monitorInst){{clearChartMessage('请先在配置页选择监控币种');return null;}}try{{const r=await fetch('/api/monitor-data?inst_id='+encodeURIComponent(monitorInst),{{cache:'no-store'}}),p=await r.json();updatePaperAccount(p.paper_account||null);if(!r.ok||p.ok===false){{clearChartMessage(p.error||'当前币种未配置，不能读取监控数据');return p;}}if(!p.running){{monitorLiveMode=false;monitorSeriesByInst[monitorInst]=[];updatePaperAccount(null);drawVirtualMonitor();return p;}}monitorLiveMode=true;if(p.points&&p.points.length>0){{setMonitorSeries(p.points);const hasChart=p.source==='web-chart'||p.source==='signal-monitor-chart';document.getElementById('monitorTitle').textContent=monitorInst+(hasChart?' 1m K线走势':' 实时走势');const meta=p.source==='web-chart'?'Web获取1m K线 · 指标读取okx_signal_monitor.py日志':(p.source==='signal-monitor-chart'?'okx_signal_monitor.py 1m K线兜底 · 指标读取日志':'读取okx_signal_monitor.py实时日志 · 等待K线');drawMonitorSeries(meta+' · '+new Date().toLocaleTimeString());}}else if(!drawMonitorSeries('保留最近走势 · 等待K线/日志')){{showRealtimeWaiting(false);}}return p;}}catch(e){{if(monitorLiveMode){{if(!drawMonitorSeries('保留最近走势 · 等待K线/日志'))showRealtimeWaiting(false);}}else drawVirtualMonitor();return null;}}}}
function sleep(ms){{return new Promise(resolve=>setTimeout(resolve,ms));}}
async function bootstrapMonitorChart(){{monitorLastTickerAt=0;for(let i=0;i<16;i++){{const payload=await fetchMonitor();if(payload&&(payload.source==='web-chart'||payload.source==='signal-monitor-chart')&&payload.points&&payload.points.length>0)break;await sleep(i<4?500:1000);}}}}
function redrawMonitorCached(){{if(drawMonitorSeries())return;const series=virtualSeriesByInst[monitorInst]||[];if(series.length){{drawChart('monitorChart',series,document.getElementById('monitorPrice'),document.getElementById('monitorMeta'),document.getElementById('monitorLoading'));document.getElementById('monitorTitle').textContent=monitorInst+' 虚拟走势';}}else{{drawVirtualMonitor();}}}}
bindMonitorTabs();
const monitorToggleBtn=document.getElementById('monitorToggleBtn');
if(monitorToggleBtn){{monitorToggleBtn.addEventListener('click',async()=>{{const status=await syncMonitorStatus();if(status&&status.running){{setMonitorButtonState('stopping','正在停止监控进程...');try{{await fetch('/stop#monitor',{{cache:'no-store'}});}}catch(e){{}}await syncMonitorStatus();monitorLiveMode=false;monitorViewStart=0;monitorViewEnd=1;monitorYZoom=1;monitorYPan=0;monitorSeriesByInst[monitorInst]=[];setMonitorButtonState('stopped','监控已停止，当前显示虚拟行情');drawVirtualMonitor();return;}}if(!monitorInst){{clearChartMessage('请先在配置页选择监控币种');return;}}setMonitorButtonState('starting','正在保存配置并启动监控...');monitorYPan=0;showRealtimeWaiting(true);try{{await autoSaveConfig();await fetch('/start#monitor',{{cache:'no-store'}});await syncMonitorStatus();switchAccuracyToLiveSession();bootstrapMonitorChart();}}catch(e){{setMonitorButtonState('stopped','启动监控失败');const l=document.getElementById('monitorLoading');if(l)l.textContent='启动监控失败：'+e;}}}});}}
let logCleared=false,consoleLogCleared=false;
let analysisLogEnabled={json.dumps(bool(config.get("analysis_log_enabled")))};
function syncAnalysisLogUi(){{const sw=document.getElementById('analysisLogSwitch'),hint=document.getElementById('analysisLogSwitchHint'),cfg=document.querySelector('.config-form input[name="analysis_log_enabled"]');if(sw)sw.checked=!!analysisLogEnabled;if(cfg)cfg.checked=!!analysisLogEnabled;if(hint)hint.textContent=(analysisLogEnabled?'已开启':'已关闭')+' · 保存后需重启监控';}}
async function setAnalysisLogEnabled(enabled){{analysisLogEnabled=!!enabled;syncAnalysisLogUi();try{{await autoSaveConfig();refreshLogs(true);refreshConsoleLogs(true);}}catch(e){{analysisLogEnabled=!enabled;syncAnalysisLogUi();alert('保存日志开关失败：'+e);}}}}
async function refreshLogs(force){{const box=document.getElementById('logWindow');if(!box)return;if(logCleared&&!force)return;try{{const r=await fetch('/api/logs',{{cache:'no-store'}}),p=await r.json();if(typeof p.enabled==='boolean'){{analysisLogEnabled=p.enabled;syncAnalysisLogUi();}}logCleared=false;box.value=p.text||'暂无日志。';box.scrollTop=box.scrollHeight;}}catch(e){{box.value='日志读取失败：'+e;}}}}
async function refreshConsoleLogs(force){{const box=document.getElementById('consoleLogWindow');if(!box)return;if(consoleLogCleared&&!force)return;try{{const r=await fetch('/api/console-logs',{{cache:'no-store'}}),p=await r.json();if(typeof p.enabled==='boolean'){{analysisLogEnabled=p.enabled;syncAnalysisLogUi();}}consoleLogCleared=false;box.value=p.text||'暂无控制台日志。';box.scrollTop=box.scrollHeight;}}catch(e){{box.value='控制台日志读取失败：'+e;}}}}
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
async function runConnectivityTest(kind) {{
  const aiBtn = document.getElementById('testAiBtn');
  const pushBtn = document.getElementById('testPushBtn');
  const btn = kind === 'ai' ? aiBtn : pushBtn;
  const label = kind === 'ai' ? '测试AI' : '测试微信推送';
  if (btn) {{
    btn.disabled = true;
    btn.textContent = '测试中...';
  }}
  showConnectivityNotice('正在保存配置并测试 ' + label + '...', true);
  try {{
    await autoSaveConfig();
    const url = kind === 'ai' ? '/api/test-ai' : '/api/test-push';
    const response = await fetch(url, {{ cache: 'no-store' }});
    const payload = await response.json();
    const message = payload.message || payload.error || '未知结果';
    showConnectivityNotice(message, !!payload.ok);
    if (!response.ok || payload.ok === false) alert(message);
  }} catch (error) {{
    const message = label + '失败：' + error;
    showConnectivityNotice(message, false);
    alert(message);
  }} finally {{
    if (btn) {{
      btn.disabled = false;
      btn.textContent = label;
    }}
  }}
}}
const testAiBtn = document.getElementById('testAiBtn');
if (testAiBtn) {{
  testAiBtn.addEventListener('click', function() {{ runConnectivityTest('ai'); }});
}}
const testPushBtn = document.getElementById('testPushBtn');
if (testPushBtn) {{
  testPushBtn.addEventListener('click', function() {{ runConnectivityTest('push'); }});
}}
const analysisLogSwitch=document.getElementById('analysisLogSwitch');
if(analysisLogSwitch){{analysisLogSwitch.addEventListener('change',function(){{setAnalysisLogEnabled(analysisLogSwitch.checked);}});}}
syncAnalysisLogUi();
const refreshLogBtn = document.getElementById('refreshLogBtn');
if (refreshLogBtn) refreshLogBtn.addEventListener('click', function() {{ refreshLogs(true); refreshConsoleLogs(true); }});
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
if(monitorCanvas){{monitorCanvas.addEventListener('wheel',function(event){{event.preventDefault();const rect=monitorCanvas.getBoundingClientRect(),focus=Math.min(1,Math.max(0,(event.clientX-rect.left)/Math.max(1,rect.width))),span=monitorViewEnd-monitorViewStart,zoom=(event.deltaY<0?0.82:1.22),newSpan=Math.min(1,Math.max(.06,span*zoom)),center=monitorViewStart+span*focus;let ns=center-newSpan*focus,ne=ns+newSpan;if(ns<0){{ne-=ns;ns=0;}}if(ne>1){{ns-=ne-1;ne=1;}}monitorViewStart=Math.max(0,ns);monitorViewEnd=Math.min(1,ne);monitorYZoom=Math.max(.45,Math.min(10,monitorYZoom*(event.deltaY<0?1.14:.88)));redrawMonitorCached();}},{{passive:false}});monitorCanvas.addEventListener('mousedown',function(event){{monitorDrag={{x:event.clientX,y:event.clientY,start:monitorViewStart,end:monitorViewEnd,yPan:monitorYPan,yRange:monitorYRange,moved:false}};monitorCanvas.classList.add('dragging');}});window.addEventListener('mousemove',function(event){{if(!monitorDrag)return;const rect=monitorCanvas.getBoundingClientRect(),span=monitorDrag.end-monitorDrag.start,dx=(event.clientX-monitorDrag.x)/Math.max(1,rect.width),dy=(event.clientY-monitorDrag.y)/Math.max(1,rect.height);if(Math.abs(event.clientX-monitorDrag.x)>3||Math.abs(event.clientY-monitorDrag.y)>3)monitorDrag.moved=true;let ns=monitorDrag.start-dx*span,ne=monitorDrag.end-dx*span;if(ns<0){{ne-=ns;ns=0;}}if(ne>1){{ns-=ne-1;ne=1;}}monitorViewStart=Math.max(0,ns);monitorViewEnd=Math.min(1,ne);monitorYPan=monitorDrag.yPan+dy*Math.max(.01,monitorDrag.yRange||monitorYRange);redrawMonitorCached();}});window.addEventListener('mouseup',function(){{if(monitorDrag){{setTimeout(function(){{monitorDrag=null;}},0);}}monitorCanvas.classList.remove('dragging');}});function selectNearestPoint(event,strict){{if(!monitorPlotPoints.length)return;const rect=monitorCanvas.getBoundingClientRect(),x=event.clientX-rect.left,y=event.clientY-rect.top;let best=null,bestDist=Infinity;monitorPlotPoints.forEach(o=>{{const dx=o.x-x,dy=o.y-y,dist=Math.sqrt(dx*dx+dy*dy);if(dist<bestDist){{best=o;bestDist=dist;}}}});if(best&&bestDist<(strict?42:34)){{monitorSelectedKey=best.point.time||'';updateSnapshotPanel(best.point,'选中点');redrawMonitorCached();}}else if(!strict){{monitorSelectedKey='';updateSnapshotPanel(monitorLatestPoint,'最新快照');redrawMonitorCached();}}}}monitorCanvas.addEventListener('click',function(event){{if(monitorDrag&&monitorDrag.moved)return;selectNearestPoint(event,false);}});monitorCanvas.addEventListener('dblclick',function(event){{selectNearestPoint(event,true);}});}}
window.addEventListener('resize',()=>{{if(currentPage()==='monitor')redrawMonitorCached();else if(currentPage()==='tests')redrawAccuracyChart();}});
setInterval(async()=>{{if(currentPage()!=='monitor')return;const status=await syncMonitorStatus();if(status&&status.running){{monitorLiveMode=true;const now=Date.now();if(now-monitorLastTickerAt>=5000){{monitorLastTickerAt=now;fetchMonitor();}}}}else{{monitorLiveMode=false;drawVirtualMonitor();}}}},1000);
setInterval(()=>{{if(currentPage()==='tests'){{refreshReplayInfo().then(function(){{fetchAccuracy({{resetView:false}});}});}}}},3000);
setInterval(()=>{{if(currentPage()==='logs'&&analysisLogEnabled){{refreshLogs(false);refreshConsoleLogs(false);}}}},3000);window.addEventListener('hashchange',()=>showPage(currentPage()));syncMonitorStatus();showPage(currentPage());
</script></body></html>"""
    return body.encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def is_authenticated(self) -> bool:
        return parse_cookies(self.headers.get("Cookie", "")).get("okx_ai_session") in SESSIONS

    def send_html(self, content: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def send_json(self, payload: Dict[str, Any], status: int = 200) -> None:
        content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
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
            self.send_html(render_login())
            return
        if path == "/logout":
            token = parse_cookies(self.headers.get("Cookie", "")).get("okx_ai_session")
            if token in SESSIONS:
                SESSIONS.remove(token)
            self.redirect("/login", "okx_ai_session=; Path=/; Max-Age=0; HttpOnly")
            return
        if not self.require_auth(path):
            return
        if path == "/":
            self.send_html(render_page())
        elif path == "/api/status":
            self.send_json(monitor_status())
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
            self.send_json({
                "text": monitor_log_text(),
                "running": bool(monitor_status()["running"]),
                "enabled": analysis_log_enabled(),
                "default_path": str(MONITOR_JSON_LOG_FILE),
                "start_at": MONITOR_LOG_START_AT,
            })
        elif path == "/api/console-logs":
            self.send_json({
                "text": monitor_console_log_text(),
                "running": bool(monitor_status()["running"]),
                "enabled": analysis_log_enabled(),
                "default_path": str(MONITOR_PROCESS_LOG_FILE),
                "start_at": MONITOR_LOG_START_AT,
            })
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
            retention_hours = float(params.get("retention_hours", ["12"])[0])
            interval_seconds = int(params.get("interval_seconds", [str(load_config().get("interval", 5))])[0])
            max_points = accuracy_chart_max_points(retention_hours, interval_seconds)
            if "max_points" in params:
                max_points = max(100, min(50000, int(params.get("max_points", [str(max_points)])[0])))
            try:
                self.send_json(
                    accuracy_report(
                        inst_id,
                        horizon,
                        max_points=max_points,
                        scope=scope,
                        retention_hours=retention_hours,
                        interval_seconds=interval_seconds,
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
        elif path == "/config-json":
            content = f"<pre>{esc(active_config_file().read_text(encoding='utf-8-sig'))}</pre>".encode("utf-8")
            self.send_html(content)
        elif path == "/start":
            self.send_html(render_page(start_monitor()))
        elif path == "/stop":
            self.send_html(render_page(stop_monitor()))
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
        if path == "/api/config/import":
            if not self.require_auth(path):
                return
            try:
                import_config_bundle(json.loads(raw or "{}"))
                self.send_json({"ok": True})
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, status=400)
            return
        form = {key: values if len(values) > 1 else values[0] for key, values in urllib.parse.parse_qs(raw).items()}
        if path == "/login":
            auth = load_auth()
            if form.get("username") == auth.get("username") and form.get("password") == auth.get("password"):
                token = secrets.token_urlsafe(32)
                SESSIONS.add(token)
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
        if path == "/api/config/save":
            try:
                saved_path = update_from_form(form)
                self.send_json({"ok": True, "path": str(saved_path), "inst_ids": configured_instruments()})
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, status=400)
            return
        if path == "/api/replay-start":
            try:
                payload = json.loads(raw or "{}")
                replay_interval = float(payload.get("interval", 0))
                message = start_replay(replay_interval)
                ok = "已启动" in message
                body = {"ok": ok, "message": message, **replay_dataset_info(lite=True)}
                if not ok:
                    body["error"] = message
                self.send_json(body, status=200 if ok else 400)
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, status=400)
            return
        if path == "/api/replay-stop":
            try:
                message = stop_replay()
                ok = "已停止" in message or "未运行" in message
                self.send_json({"ok": ok, "message": message, **replay_dataset_info(lite=True)}, status=200 if ok else 400)
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, status=400)
            return
        if path == "/save-auth":
            auth = load_auth()
            username = str(form.get("auth_username", auth.get("username", "admin"))).strip() or "admin"
            password = str(form.get("auth_password", "")).strip() or auth.get("password", "admin123")
            save_auth(username, password)
            self.send_html(render_page())
            return
        try:
            env_path = update_from_form(form)
        except Exception as exc:
            self.send_html(render_page(f"保存失败：{exc}"))
            return
        if path == "/save-and-run":
            self.send_html(render_page(f"配置已保存，{start_monitor()}。"))
        else:
            note = "" if env_path == PORTABLE_ENV_FILE else f" 密钥已保存到用户目录：{env_path}"
            self.send_html(render_page(f"配置已保存。{note}"))


def main() -> int:
    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError as exc:
        print(f"配置页面启动失败: {exc}")
        print(f"可能是端口 {PORT} 被占用。可以设置 WEB_CONTROL_PANEL_PORT 后重试。")
        return 1
    url = f"http://{HOST}:{PORT}"
    print(f"配置页面已启动: {url}")
    print("如果浏览器没有自动打开，请手动访问该地址；按 Ctrl+C 停止。")
    if HOST in ("127.0.0.1", "localhost"):
        threading = __import__("threading")
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n配置页面已停止。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
