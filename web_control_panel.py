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
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

try:
    from okx_signal_monitor import KLINE_LIMIT, OkxAiShortTermAssistant, RuntimeConfig, SignalConfig, trend_profile_from_candles
except ModuleNotFoundError:
    import importlib.util

    signal_monitor_path = SCRIPT_DIR / "okx_signal_monitor.py"
    spec = importlib.util.spec_from_file_location("okx_signal_monitor", signal_monitor_path)
    if spec is None or spec.loader is None:
        raise
    signal_monitor_module = importlib.util.module_from_spec(spec)
    sys.modules["okx_signal_monitor"] = signal_monitor_module
    spec.loader.exec_module(signal_monitor_module)
    OkxAiShortTermAssistant = signal_monitor_module.OkxAiShortTermAssistant
    KLINE_LIMIT = signal_monitor_module.KLINE_LIMIT
    RuntimeConfig = signal_monitor_module.RuntimeConfig
    SignalConfig = signal_monitor_module.SignalConfig
    trend_profile_from_candles = signal_monitor_module.trend_profile_from_candles

BUILD_DIR = SCRIPT_DIR / "build"
CONFIG_DIR = SCRIPT_DIR / "config"
PORTABLE_STATE_DIR = SCRIPT_DIR / "local_state"
LEGACY_STATE_DIR = BUILD_DIR / "local_state"
ASSETS_DIR = SCRIPT_DIR / "web_assets"
LOG_DIR = BUILD_DIR / "runtime_logs"
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
HOST = os.getenv("WEB_CONTROL_PANEL_HOST", "127.0.0.1")
PORT = int(os.getenv("WEB_CONTROL_PANEL_PORT", "8765"))
SUPPORTED_INSTRUMENTS = ("BTC-USDT-SWAP", "ETH-USDT-SWAP")
OKX_BASE_URL = "https://www.okx.com"

SESSIONS = set()
MONITOR_PROCESS: subprocess.Popen = None
MONITOR_STARTED_AT = ""
MONITOR_LOG_START_AT = ""
MONITOR_STOPPED_AT = ""


CONFIG_FIELDS = [
    ("基础运行", "interval", "number", "轮询间隔(秒)", "默认5秒执行一轮监控。"),
    ("基础运行", "runtime", "number", "运行时长(秒)", "0表示一直运行，300表示运行5分钟。"),
    ("基础运行", "flag", "choice", "OKX环境", "0正式环境，1模拟盘。"),
    ("策略模式", "strategy_mode", "strategy_choice", "策略周期", "超短线捕捉1-15分钟急速波动；短线关注5m/15m结构；中线关注1H/4H趋势。"),
    ("策略模式", "risk_preference", "risk_choice", "风险偏好", "保守提高确认门槛，激进更早提示机会。"),
    ("策略模式", "signal_trade_enabled", "checkbox", "交易信号", "允许推送做多/做空交易信号。"),
    ("策略模式", "signal_watch_enabled", "checkbox", "观察信号", "允许推送资金费率、背离、布林收口等观察型风险信号。"),
    ("策略模式", "signal_spike_enabled", "checkbox", "急速异动提醒", "并行捕捉5-10分钟急速涨跌，即使主策略不是超短线也会提示。"),
    ("策略模式", "ai_output_style", "ai_style_choice", "AI输出风格", "稳健确认偏保守；动量捕捉更关注短时波动；趋势跟随更关注顺势结构。"),
    ("高级策略", "allow_scalp_trade", "checkbox", "允许超短线交易建议", "关闭时超短线只做异动提醒；开启后可给入场/止损/止盈。"),
    ("高级策略", "allow_counter_4h_scalp", "checkbox", "允许逆4H短打", "仅影响超短线，开启后不因4H反向直接压低动量单。"),
    ("高级策略", "allow_oi_divergence_momentum", "checkbox", "允许OI背离动量单", "仅影响超短线，允许价涨仓减这类逼空/回补型上涨提示。"),
    ("高级策略", "scalp_move_pct_5m", "number", "5分钟急速阈值%", "最近5根已收盘1m K线涨跌幅超过该值时触发超短线动量评分。"),
    ("高级策略", "scalp_move_pct_10m", "number", "10分钟急速阈值%", "最近10根已收盘1m K线涨跌幅超过该值时触发超短线动量评分。"),
    ("AI与推送", "watch_push_score", "number", "观察/异动推送分", "观察信号和急速异动提醒达到该分数才推送。"),
    ("AI与推送", "ai_enabled", "checkbox", "启用AI分析", "触发信号后调用AI。"),
    ("AI与推送", "dry_run_ai", "checkbox", "AI dry-run", "只生成AI请求数据，不真实调用AI。"),
    ("AI与推送", "push_enabled", "checkbox", "启用微信推送", "满足信号和评分条件时通过 Server酱 推送到个人微信。"),
    ("AI与推送", "push_score", "number", "推送分数阈值", "触发信号且评分达到该值才推送。"),
    ("策略阈值", "volume_multiplier", "number", "放量倍数", "当前1m成交量超过20根均量的倍数。"),
    ("策略阈值", "oi_change_pct_15m", "number", "15分钟OI变化%", "预热满15分钟后生效。"),
    ("策略阈值", "funding_abs_threshold", "number", "资金费率过热阈值", "资金费率绝对值超过该值触发。"),
    ("策略阈值", "funding_change_threshold", "number", "资金费率变化阈值", "预热满15分钟后生效。"),
    ("策略阈值", "long_short_extreme", "number", "多空极端占比", "例如0.75表示75%。"),
    ("网络与日志", "retry_times", "number", "重试次数", "OKX/AI/推送请求失败重试次数。"),
    ("网络与日志", "retry_backoff", "number", "重试退避(秒)", "第N次失败等待 backoff * N 秒。"),
    ("网络与日志", "push_cooldown_seconds", "number", "推送冷却(秒)", "同类信号冷却期内不重复推送。"),
    ("网络与日志", "log_max_bytes", "number", "日志最大字节", "超过后轮转为.1文件。"),
]

ENV_FIELDS = [
    ("OPENAI_API_KEY", "OpenAI API Key", "AI接口密钥。"),
    ("AI_MODEL", "AI模型", "默认gpt-5.5，可改为更便宜模型。"),
    ("WECHAT_SEND_KEY", "微信推送 SendKey", "Server酱 SendKey，用于推送到个人微信。在 https://sct.ftqq.com 获取。"),
]


def default_config() -> Dict[str, Any]:
    return {
        "inst_ids": ["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
        "interval": 5,
        "runtime": 0,
        "flag": "0",
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
        "push_score": 80,
        "watch_push_score": 80,
        "volume_multiplier": 2.0,
        "oi_change_pct_15m": 5.0,
        "funding_abs_threshold": 0.0008,
        "funding_change_threshold": 0.0003,
        "long_short_extreme": 0.75,
        "retry_times": 3,
        "retry_backoff": 1.5,
        "push_cooldown_seconds": 900,
        "log_max_bytes": 10485760,
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


def load_config() -> Dict[str, Any]:
    config = default_config()
    loaded = load_json(active_config_file(), {})
    if isinstance(loaded, dict):
        config.update(loaded)
    return config


def configured_instruments() -> List[str]:
    inst_ids = load_config().get("inst_ids", [])
    if isinstance(inst_ids, str):
        inst_ids = [inst_ids]
    return [inst for inst in inst_ids if inst in SUPPORTED_INSTRUMENTS]


def save_config(config: Dict[str, Any]) -> Path:
    text = json.dumps(config, indent=2, ensure_ascii=False) + "\n"
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
        f'AI_MODEL="{env.get("AI_MODEL", "gpt-5.5")}"',
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
    return {"name": name or "OKX_AI_Config", "version": "1.0", "config": load_config(), "env": load_env()}


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
    args = [
        sys.executable,
        str(SCRIPT_DIR / "okx_signal_monitor.py"),
        "--inst-ids",
        ",".join(config.get("inst_ids", [])),
        "--interval",
        str(config.get("interval", 5)),
        "--runtime",
        str(config.get("runtime", 0)),
        "--flag",
        str(config.get("flag", "0")),
        "--push-score",
        str(config.get("push_score", 80)),
        "--retry-times",
        str(config.get("retry_times", 3)),
        "--retry-backoff",
        str(config.get("retry_backoff", 1.5)),
        "--push-cooldown",
        str(config.get("push_cooldown_seconds", 900)),
        "--log-max-bytes",
        str(config.get("log_max_bytes", 10485760)),
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
        str(config.get("watch_push_score", config.get("push_score", 80))),
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
    return args


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
        return {"running": False, "text": "未启动", "started_at": "", "elapsed_seconds": 0}
    code = MONITOR_PROCESS.poll()
    if code is None:
        return {"running": True, "text": f"运行中 PID={MONITOR_PROCESS.pid}", "started_at": MONITOR_STARTED_AT, "elapsed_seconds": elapsed_seconds}
    if not MONITOR_STOPPED_AT:
        MONITOR_STOPPED_AT = time.strftime("%Y-%m-%d %H:%M:%S")
        return monitor_status()
    return {"running": False, "text": f"已停止，退出码={code}", "started_at": MONITOR_STARTED_AT, "elapsed_seconds": elapsed_seconds}


def start_monitor() -> str:
    global MONITOR_PROCESS, MONITOR_STARTED_AT, MONITOR_LOG_START_AT, MONITOR_STOPPED_AT
    status = monitor_status()
    if status["running"]:
        return status["text"]
    if not configured_instruments():
        return "请先在配置页至少选择一个监控币种。"
    MONITOR_STARTED_AT = time.strftime("%Y-%m-%d %H:%M:%S")
    MONITOR_LOG_START_AT = MONITOR_STARTED_AT
    MONITOR_STOPPED_AT = ""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
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


def point_from_log_item(item: Dict[str, Any], price: float) -> Dict[str, Any]:
    score = item.get("score") if isinstance(item.get("score"), dict) else {}
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
    }


def test_ai_connection() -> str:
    env = build_child_env()
    api_key = env.get("OPENAI_API_KEY", "")
    model = env.get("AI_MODEL", "gpt-5.5")
    if not api_key:
        return "AI测试失败：OPENAI_API_KEY未配置。"
    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key)
        response = client.responses.create(model=model, input="请回复：AI接口连通性测试成功。")
        return f"AI测试成功：{getattr(response, 'output_text', response)}"
    except Exception as exc:
        return f"AI测试失败：{exc}"


def test_push_connection() -> str:
    send_key = build_child_env().get("WECHAT_SEND_KEY", "").strip()
    if not send_key:
        return "推送测试失败：未配置 WECHAT_SEND_KEY。"
    try:
        result = post_json(
            f"https://sctapi.ftqq.com/{send_key}.send",
            {"title": "[OKX AI短线助手] 推送测试", "desp": "微信推送配置正常。"},
        )
        return f"微信推送成功：{result[:160]}"
    except Exception as exc:
        return f"微信推送失败：{exc}"


def tail_text(path: Path, max_bytes: int = 160000) -> str:
    if not path.exists():
        return ""
    size = path.stat().st_size
    with path.open("rb") as file:
        if size > max_bytes:
            file.seek(size - max_bytes)
        return file.read().decode("utf-8", errors="replace")


def monitor_log_text() -> str:
    if not MONITOR_LOG_START_AT:
        return "等待启动监控。本窗口只显示本次启动后的JSON分析日志。"
    lines = []
    for line in tail_text(MONITOR_JSON_LOG_FILE, 40 * 1024 * 1024).splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if str(item.get("time", "")) >= MONITOR_LOG_START_AT:
            lines.append(line)
    return "\n".join(lines) or "暂无本次启动后的JSON分析日志。"


def monitor_console_log_text() -> str:
    if not MONITOR_LOG_START_AT:
        return "等待启动监控。本窗口只显示本次启动后的控制台输出。"
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
    for line in tail_text(MONITOR_JSON_LOG_FILE, 40 * 1024 * 1024).splitlines():
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
    return {
        "inst_id": inst_id,
        "running": running,
        "points": points,
        "price": last,
        "change": change,
        "change_pct": (change / first * 100) if first else 0,
        "source": source,
    }


def parse_history_time(value: str) -> datetime:
    text = value.strip().replace("T", " ")
    if len(text) == 16:
        text += ":00"
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")


def find_log_item_near(inst_id: str, target: datetime, tolerance_seconds: int = 90) -> Tuple[Dict[str, Any], int]:
    best = {}
    best_delta = 10 ** 9
    for line in tail_text(MONITOR_JSON_LOG_FILE, 40 * 1024 * 1024).splitlines():
        try:
            item = json.loads(line)
            if item.get("inst_id") != inst_id:
                continue
            item_time = parse_history_time(str(item.get("time", "")))
        except Exception:
            continue
        delta = abs(int((item_time - target).total_seconds()))
        if delta < best_delta:
            best = item
            best_delta = delta
    return (best, best_delta) if best and best_delta <= tolerance_seconds else ({}, best_delta)


def log_candles_between(inst_id: str, start_time: datetime, end_time: datetime) -> List[Dict[str, Any]]:
    # 近期历史回放优先使用本地监控日志里的1m K线。
    # 原因：用户通常验证的是当前时间前十几二十分钟，monitor日志已经保存了当时及后续每轮的chart.points。
    # 直接用日志能避免OKX history-candles分页参数、网络不可用、最新K线尚未归档等问题。
    by_minute: Dict[str, Dict[str, Any]] = {}
    for line in tail_text(MONITOR_JSON_LOG_FILE, 40 * 1024 * 1024).splitlines():
        try:
            item = json.loads(line)
            if item.get("inst_id") != inst_id:
                continue
            item_time = parse_history_time(str(item.get("time", "")))
        except Exception:
            continue
        if not (start_time < item_time <= end_time):
            continue
        chart = item.get("chart") if isinstance(item.get("chart"), dict) else {}
        rows = chart.get("points") if chart.get("bar") == "1m" and isinstance(chart.get("points"), list) else []
        for row in rows[:3]:
            if not isinstance(row, dict):
                continue
            try:
                row_time = parse_history_time(str(row.get("time", "")))
                candle = {
                    "time": row_time.strftime("%Y-%m-%d %H:%M:%S"),
                    "open": float(row.get("open")),
                    "high": float(row.get("high")),
                    "low": float(row.get("low")),
                    "close": float(row.get("close")),
                    "volume": float(row.get("volume", 0.0)),
                    "confirmed": str(row.get("confirmed", "1")),
                }
            except Exception:
                continue
            if start_time < row_time <= end_time:
                by_minute[minute_bucket_key(candle["time"])] = candle
    return [by_minute[key] for key in sorted(by_minute)]


def get_history_candles_before(inst_id: str, bar: str, at_time: datetime, limit: int = KLINE_LIMIT) -> List[Dict[str, Any]]:
    before_ms = int(at_time.timestamp() * 1000)
    query = urllib.parse.urlencode({"instId": inst_id, "bar": bar, "before": str(before_ms), "limit": str(limit)})
    request = urllib.request.Request(
        f"{OKX_BASE_URL}/api/v5/market/history-candles?{query}",
        headers={"Accept": "application/json", "User-Agent": "okx-ai-assistant-web/1.0"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        payload = json.loads(response.read().decode("utf-8", errors="ignore"))
    candles = []
    for row in payload.get("data") or []:
        if not isinstance(row, list) or len(row) < 6:
            continue
        candles.append({
            "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(row[0]) / 1000)),
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "volume": float(row[5]),
            "confirmed": str(row[8]) if len(row) > 8 else "1",
        })
    return candles


def build_history_assistant(config: Dict[str, Any]) -> OkxAiShortTermAssistant:
    return OkxAiShortTermAssistant(
        instruments=config.get("inst_ids", list(SUPPORTED_INSTRUMENTS)),
        interval=max(int(config.get("interval", 5)), 1),
        flag=str(config.get("flag", "0")),
        ai_enabled=False,
        push_enabled=False,
        push_score=int(config.get("push_score", 80)),
        dry_run_ai=False,
        config=SignalConfig(
            volume_multiplier=float(config.get("volume_multiplier", 2.0)),
            oi_change_pct_15m=float(config.get("oi_change_pct_15m", 5.0)),
            funding_abs_threshold=float(config.get("funding_abs_threshold", 0.0008)),
            funding_change_threshold=float(config.get("funding_change_threshold", 0.0003)),
            long_short_extreme=float(config.get("long_short_extreme", 0.75)),
            strategy_mode=str(config.get("strategy_mode", "short")),
            risk_preference=str(config.get("risk_preference", "standard")),
            signal_trade_enabled=bool(config.get("signal_trade_enabled", True)),
            signal_watch_enabled=bool(config.get("signal_watch_enabled", True)),
            signal_spike_enabled=bool(config.get("signal_spike_enabled", True)),
            ai_output_style=str(config.get("ai_output_style", "steady")),
            allow_scalp_trade=bool(config.get("allow_scalp_trade", False)),
            allow_counter_4h_scalp=bool(config.get("allow_counter_4h_scalp", False)),
            allow_oi_divergence_momentum=bool(config.get("allow_oi_divergence_momentum", False)),
            scalp_move_pct_5m=float(config.get("scalp_move_pct_5m", 0.22)),
            scalp_move_pct_10m=float(config.get("scalp_move_pct_10m", 0.35)),
            watch_push_score=int(config.get("watch_push_score", config.get("push_score", 80))),
        ),
        runtime_config=RuntimeConfig(),
    )


def snapshot_from_log_or_history(inst_id: str, at_time: datetime, assistant: OkxAiShortTermAssistant) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    log_item, delta = find_log_item_near(inst_id, at_time)
    if log_item:
        candles = {}
        chart = log_item.get("chart") if isinstance(log_item.get("chart"), dict) else {}
        chart_rows = chart.get("points") if isinstance(chart.get("points"), list) else []
        if chart_rows:
            # okx_signal_monitor.py 写入日志时保存的是供指标计算使用的 K 线顺序：
            # 最新 K 线在前、越往后越旧。这里回放的是同一套评分逻辑，
            # 因此必须保留原顺序，不能按 Web 画图的时间轴习惯反转。
            candles["1m"] = chart_rows
        for bar in ("3m", "5m", "15m", "1H", "4H"):
            try:
                candles[bar] = get_history_candles_before(inst_id, bar, at_time)
            except Exception:
                candles[bar] = []
        if not candles.get("1m"):
            candles["1m"] = get_history_candles_before(inst_id, "1m", at_time)
        source_meta = {"source": "signal-monitor-log", "nearest_log_delta_seconds": delta}
        base = log_item
    else:
        candles = {bar: get_history_candles_before(inst_id, bar, at_time) for bar in ("1m", "3m", "5m", "15m", "1H", "4H")}
        source_meta = {"source": "rebuilt-kline", "nearest_log_delta_seconds": delta, "data_quality": "partial"}
        base = {}

    price = float(base.get("price") or (candles.get("1m", [{}])[0].get("close") if candles.get("1m") else 0.0))
    volume = assistant._volume_stats(candles.get("1m", []))
    profiles = {bar: trend_profile_from_candles(rows) for bar, rows in candles.items()}
    volatility = assistant._volatility_context(inst_id, profiles)
    dynamic = assistant._dynamic_thresholds(inst_id)
    long_short = base.get("long_short_ratio") if isinstance(base.get("long_short_ratio"), dict) else {"available": False, "long_ratio": 0.0, "short_ratio": 0.0, "long_short_ratio": 0.0}
    order_book = base.get("order_book") if isinstance(base.get("order_book"), dict) else {"available": False, "imbalance": 0.0, "imbalance_5": 0.0, "spread_pct": 0.0}
    oi_change = float(base.get("oi_change_pct_15m") or 0.0)
    funding_change = float(base.get("funding_change") or 0.0)
    funding_rate = float(base.get("funding_rate") or 0.0)
    context = assistant._market_context(price, candles, profiles, volume, float(base.get("open_interest") or 0.0), oi_change, funding_rate, funding_change, long_short, order_book, volatility, dynamic)
    snapshot = {
        "time": at_time.strftime("%Y-%m-%d %H:%M:%S"),
        "inst_id": inst_id,
        "price": price,
        "best_bid": price,
        "best_ask": price,
        "candles": candles,
        "volume": volume,
        "open_interest": base.get("open_interest"),
        "oi_change_pct_15m": oi_change,
        "oi_warmup_ready": bool(base.get("oi_warmup_ready", False)),
        "funding_rate": funding_rate,
        "funding_change": funding_change,
        "funding_warmup_ready": bool(base.get("funding_warmup_ready", False)),
        "long_short_ratio": long_short,
        "order_book": order_book,
        "trend_profiles": profiles,
        "volatility": volatility,
        "dynamic_thresholds": dynamic,
        "instrument_profile": assistant._instrument_profile(inst_id),
        "market_context": context,
    }
    return snapshot, source_meta


def get_future_1m_candles(inst_id: str, at_time: datetime, minutes: int = 25) -> Tuple[List[Dict[str, Any]], str]:
    end_time = min(datetime.now(), datetime.fromtimestamp(at_time.timestamp() + minutes * 60))
    rows = log_candles_between(inst_id, at_time, end_time)
    if rows:
        return rows, "signal-monitor-log"
    rows = get_history_candles_before(inst_id, "1m", end_time, limit=max(80, minutes + 40))
    selected = []
    for row in rows:
        try:
            row_time = parse_history_time(str(row.get("time", "")))
        except Exception:
            continue
        if at_time < row_time <= end_time:
            selected.append(row)
    selected.sort(key=lambda item: item.get("time", ""))
    return selected, "okx-history-candles"


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


def evaluate_history_outcome(snapshot: Dict[str, Any], score: Dict[str, Any], future: List[Dict[str, Any]]) -> Dict[str, Any]:
    price = float(snapshot.get("price") or 0.0)
    direction = score.get("raw_direction") if score.get("raw_direction") in ("做多", "做空") else score.get("direction")
    entry_low, entry_high = parse_level_pair(score.get("entry"))
    stop = float(score.get("stop_loss")) if score.get("stop_loss") not in (None, "-") else 0.0
    targets = parse_targets(score.get("take_profit"))
    entry_touched = False
    entry_price = 0.0
    stop_hit = False
    tp_hits = [False for _ in targets]
    mfe = 0.0
    mae = 0.0
    returns = {}
    for index, row in enumerate(future, start=1):
        high = float(row.get("high") or 0.0)
        low = float(row.get("low") or 0.0)
        close = float(row.get("close") or 0.0)
        if not entry_touched and entry_low and entry_high and high >= entry_low and low <= entry_high:
            entry_touched = True
            entry_price = entry_high if direction == "做多" else entry_low
        ref = entry_price if entry_touched else price
        if ref:
            move_high = (high - ref) / ref * 100
            move_low = (low - ref) / ref * 100
            if direction == "做空":
                move_high, move_low = -move_low, -move_high
            mfe = max(mfe, move_high)
            mae = min(mae, move_low)
        if entry_touched and stop:
            if direction == "做多" and low <= stop:
                stop_hit = True
            if direction == "做空" and high >= stop:
                stop_hit = True
        if entry_touched:
            for target_index, target in enumerate(targets):
                if direction == "做多" and high >= target:
                    tp_hits[target_index] = True
                if direction == "做空" and low <= target:
                    tp_hits[target_index] = True
        if index in (5, 15, 20) and price:
            ret = (close - price) / price * 100
            returns[f"{index}m"] = -ret if direction == "做空" else ret
    return {
        "future_points": len(future),
        "entry_touched": entry_touched,
        "entry_price_assumed": entry_price,
        "stop_hit": stop_hit,
        "take_profit_hits": tp_hits,
        "mfe_pct": mfe,
        "mae_pct": mae,
        "returns": returns,
    }


def judge_history_reliability(score: Dict[str, Any], outcome: Dict[str, Any]) -> Dict[str, Any]:
    notes = []
    direction = score.get("raw_direction") or score.get("direction")
    ret15 = outcome.get("returns", {}).get("15m")
    direction_ok = ret15 is not None and ret15 > 0
    if score.get("final_direction") == "观望":
        notes.append("系统最终为观望，主要验证观察分和风险提示是否有意义。")
    if not outcome.get("entry_touched"):
        notes.append("后续走势未触达入场区，入场计划未成交。")
    if outcome.get("stop_hit"):
        notes.append("入场后触发止损，需结合MFE/MAE判断入场质量。")
    if any(outcome.get("take_profit_hits", [])):
        notes.append("后续触达至少一档止盈。")
    return {
        "direction": direction,
        "direction_result": "正向" if direction_ok else ("反向/无效" if ret15 is not None else "样本不足"),
        "entry_result": "触达" if outcome.get("entry_touched") else "未触达",
        "risk_result": "止损触发" if outcome.get("stop_hit") else "未先触发止损",
        "notes": notes,
    }


def run_history_test(inst_id: str, at_time_text: str) -> Dict[str, Any]:
    if inst_id not in SUPPORTED_INSTRUMENTS:
        raise ValueError("仅支持 BTC-USDT-SWAP 或 ETH-USDT-SWAP")
    at_time = parse_history_time(at_time_text)
    now = datetime.now()
    delta_minutes = (now - at_time).total_seconds() / 60
    if delta_minutes < 5 or delta_minutes > 90:
        raise ValueError("请选择当前时间前5到90分钟内的时间点。")
    assistant = build_history_assistant(load_config())
    snapshot, source_meta = snapshot_from_log_or_history(inst_id, at_time, assistant)
    signals = assistant.detect_signals(snapshot)
    score = assistant.score_snapshot(snapshot, signals)
    analysis = assistant._local_analysis(snapshot, signals, score)
    future, future_source = get_future_1m_candles(inst_id, at_time, 25)
    outcome = evaluate_history_outcome(snapshot, score, future)
    outcome["future_source"] = future_source
    verdict = judge_history_reliability(score, outcome)
    return {
        "ok": True,
        "inst_id": inst_id,
        "time": at_time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": source_meta,
        "signals": signals,
        "score": score,
        "analysis": analysis,
        "outcome": outcome,
        "verdict": verdict,
    }


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


def read_accuracy_items(inst_id: str, since_time: datetime = None) -> Tuple[List[Dict[str, Any]], List[Tuple[datetime, float]]]:
    items = []
    price_by_time: Dict[str, Tuple[datetime, float]] = {}
    for line in tail_text(MONITOR_JSON_LOG_FILE, 40 * 1024 * 1024).splitlines():
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
        items.append(item)
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
    return 0.025


def pct_rate(hit: int, total: int) -> float:
    return (hit / total * 100) if total else 0.0


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


def price_crosses_range(prev_price: float, price: float, low: float, high: float) -> bool:
    if low <= price <= high:
        return True
    if prev_price <= 0:
        return False
    return min(prev_price, price) <= high and max(prev_price, price) >= low


def evaluate_realtime_advice(item: Dict[str, Any], price: float, future_path: List[Tuple[datetime, float]], threshold_pct: float) -> Dict[str, Any]:
    score = item.get("score") if isinstance(item.get("score"), dict) else {}
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

    try:
        entry_low, entry_high = parse_level_pair(score.get("entry"))
        stop = safe_float(score.get("stop_loss"), 0.0) if score.get("stop_loss") not in (None, "-") else 0.0
        targets = parse_targets(score.get("take_profit"))
    except Exception:
        result["outcome_type"] = "trade_bad_levels"
        return result
    first_target = targets[0] if targets else 0.0
    if not entry_low or not entry_high:
        result["outcome_type"] = "trade_missing_levels"
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
        result["hit"] = True
        result["outcome_type"] = "trade_no_fill"
    elif result["trade_win"]:
        result["hit"] = True
        result["outcome_type"] = "trade_take_profit"
    elif result["stop_hit"]:
        result["hit"] = False
        result["outcome_type"] = "trade_stop_loss"
    else:
        result["hit"] = result["mfe_pct"] >= abs(result["mae_pct"])
        result["outcome_type"] = "trade_open_favorable" if result["hit"] else "trade_open_adverse"
    return result


def empty_accuracy_summary() -> Dict[str, Any]:
    return {
        "total": 0,
        "raw_log_total": 0,
        "pending_total": 0,
        "mature_rate_pct": 0.0,
        "reliability_score": 0.0,
        "reliability_level": "样本不足",
        "decision_total": 0,
        "decision_accuracy_pct": 0.0,
        "baseline_watch_pct": 0.0,
        "model_edge_pct": 0.0,
        "trade_signal_total": 0,
        "trade_signal_accuracy_pct": 0.0,
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
    }


def reliability_level(total: int, mature_rate_pct: float, edge_pct: float, trade_total: int) -> Tuple[float, str]:
    """给压测页一个直观可靠性等级。

    这个分数不是交易收益率，而是样本量、成熟率、相对基准优势和真实交易信号覆盖的综合健康度。
    压测时重点看它是否长期稳定上升，而不是单次短窗波动。
    """
    sample_score = min(35.0, total / 120.0 * 35.0)
    mature_score = min(20.0, max(0.0, mature_rate_pct) / 100.0 * 20.0)
    edge_score = max(0.0, min(30.0, (edge_pct + 5.0) / 35.0 * 30.0))
    trade_score = min(15.0, trade_total / 20.0 * 15.0)
    score = sample_score + mature_score + edge_score + trade_score
    if total < 30:
        level = "样本不足"
    elif score >= 75 and edge_pct > 8:
        level = "较可靠"
    elif score >= 55 and edge_pct > 0:
        level = "观察中"
    else:
        level = "需优化"
    return score, level


def accuracy_chart_max_points(retention_hours: float, interval_seconds: int) -> int:
    """图表保留点数 = 保留时长 / 轮询间隔，与配置页 interval 对齐。"""
    interval_seconds = max(1, int(interval_seconds or 5))
    retention_hours = max(0.5, min(168.0, float(retention_hours or 4)))
    return max(100, min(50000, int(retention_hours * 3600 / interval_seconds)))


def accuracy_report(
    inst_id: str,
    horizon_seconds: int = 5,
    max_points: int = 2400,
    scope: str = "session",
    retention_hours: float = 4.0,
    interval_seconds: int = 5,
) -> Dict[str, Any]:
    if inst_id not in SUPPORTED_INSTRUMENTS:
        raise ValueError("仅支持 BTC-USDT-SWAP 或 ETH-USDT-SWAP")
    horizon_seconds = max(5, min(3600, int(horizon_seconds or 5)))
    interval_seconds = max(1, int(interval_seconds or 5))
    retention_hours = max(0.5, min(168.0, float(retention_hours or 4)))
    max_points = max(100, min(50000, int(max_points or accuracy_chart_max_points(retention_hours, interval_seconds))))
    session_scope = scope == "session"
    since_time = parse_history_time(MONITOR_LOG_START_AT) if session_scope and MONITOR_LOG_START_AT else None
    threshold_pct = realtime_accuracy_threshold_pct(horizon_seconds)
    if session_scope and not since_time:
        return {
            "ok": True,
            "inst_id": inst_id,
            "horizon_seconds": horizon_seconds,
            "scope": "session",
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
            "log_path": str(MONITOR_JSON_LOG_FILE),
        }
    items, price_points = read_accuracy_items(inst_id, since_time)
    rows = []
    raw_log_total = 0
    pending_total = 0
    decision_total = 0
    decision_hit = 0
    trade_signal_total = 0
    trade_signal_hit = 0
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
            trade_signal_hit += 1 if advice["hit"] else 0
        else:
            watch_total += 1
            watch_hit += 1 if advice["hit"] else 0
        decision_hit_current = bool(advice["hit"])
        decision_total += 1
        decision_hit += 1 if decision_hit_current else 0
        signed_error = (pred_value - actual_value)
        signed_error_sum += signed_error
        abs_error_sum += abs(signed_error)
        rows.append({
            "time": item_time.strftime("%Y-%m-%d %H:%M:%S"),
            "future_time": advice["future_time"],
            "price": price,
            "future_price": advice["future_price"],
            "raw_direction": raw_direction,
            "final_direction": predicted,
            "actual_direction": advice["actual_direction"],
            "actual_return_pct": advice["actual_return_pct"],
            "hit": decision_hit_current,
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
        })
    total = len(rows)
    mature_rate_pct = pct_rate(total, raw_log_total)
    decision_accuracy_pct = pct_rate(decision_hit, decision_total)
    baseline_watch_pct = pct_rate(baseline_watch_hit, total)
    model_edge_pct = decision_accuracy_pct - baseline_watch_pct if total else 0.0
    reliability_score, reliability_label = reliability_level(total, mature_rate_pct, model_edge_pct, trade_signal_total)
    return {
        "ok": True,
        "inst_id": inst_id,
        "horizon_seconds": horizon_seconds,
        "scope": "session" if since_time else "all",
        "start_at": since_time.strftime("%Y-%m-%d %H:%M:%S") if since_time else "",
        "threshold_pct": threshold_pct,
        "rolling_window": window,
        "summary_scope": "cumulative",
        "summary": {
            "total": total,
            "raw_log_total": raw_log_total,
            "pending_total": pending_total,
            "mature_rate_pct": mature_rate_pct,
            "reliability_score": reliability_score,
            "reliability_level": reliability_label,
            "decision_total": decision_total,
            "decision_accuracy_pct": decision_accuracy_pct,
            "baseline_watch_pct": baseline_watch_pct,
            "model_edge_pct": model_edge_pct,
            "trade_signal_total": trade_signal_total,
            "trade_signal_accuracy_pct": pct_rate(trade_signal_hit, trade_signal_total),
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
        },
        "points": rolling[-max_points:],
        "recent": rows[-20:],
        "retention_hours": retention_hours,
        "interval_seconds": interval_seconds,
        "max_points": max_points,
        "log_path": str(MONITOR_JSON_LOG_FILE),
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
    rows.append('<section class="card page-section" data-page="config"><h2>AI密钥与微信推送</h2>')
    for key, label, help_text in ENV_FIELDS:
        value = env.get(key, "gpt-5.5" if key == "AI_MODEL" else "")
        input_type = "password" if "KEY" in key else "text"
        rows.append(f'<div class="field"><label>{esc(label)}</label><div><input type="{input_type}" name="env_{esc(key)}" value="{esc(value)}"><p>{esc(help_text)}</p></div></div>')
    rows.append("</section>")

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
	.market-card{{background:#242424;border:1px solid #3a3a3a;border-radius:18px;margin:0;padding:18px 20px 16px;color:#f8fafc;box-shadow:0 12px 34px rgba(15,23,42,.16);overflow:hidden;height:calc(100vh - 40px);display:flex;flex-direction:column}} .monitor-card{{height:calc(100vh - 188px);min-height:520px}} .market-head{{display:flex;justify-content:space-between;align-items:flex-start;gap:14px;margin-bottom:12px}} .market-title{{font-size:18px;font-weight:800;margin-bottom:4px}} .market-sub{{color:#a3a3a3;font-size:12px}} .market-price{{text-align:right}} .market-price strong{{display:block;font-size:24px;line-height:1.1}} .market-price span{{font-size:13px;color:#94a3b8}} .market-price.up span{{color:#fb7185}} .market-price.down span{{color:#22c55e}} .market-canvas-wrap{{position:relative;flex:1;min-height:0;border-top:1px solid #393939;background:linear-gradient(rgba(255,255,255,.055) 1px,transparent 1px),linear-gradient(90deg,rgba(255,255,255,.035) 1px,transparent 1px);background-size:100% 72px,72px 100%}} canvas{{width:100%;height:100%;display:block;cursor:grab}} canvas.dragging{{cursor:grabbing}} .market-loading{{position:absolute;inset:0;display:grid;place-items:center;color:#a3a3a3;pointer-events:none}} .snapshot-panel{{position:absolute;left:16px;top:16px;z-index:2;min-width:260px;max-width:min(500px,calc(100% - 32px));max-height:calc(100% - 32px);overflow:auto;padding:10px 12px;border-radius:12px;background:rgba(15,23,42,.62);border:1px solid rgba(148,163,184,.20);box-shadow:0 12px 28px rgba(0,0,0,.18);backdrop-filter:blur(8px);font-size:12px;line-height:1.35;color:#dbeafe;pointer-events:none}} .snapshot-panel strong{{display:block;color:#fff;font-size:13px;margin-bottom:5px}} .snapshot-grid{{display:grid;grid-template-columns:68px minmax(0,1fr);gap:3px 8px;align-items:start}} .snapshot-grid span{{color:#9ca3af}} .snapshot-grid b{{font-weight:700;color:#e5e7eb;word-break:break-word}} .market-time-range{{margin-top:10px;padding-top:10px;border-top:1px solid rgba(255,255,255,.08);color:#a3a3a3;font-size:12px;display:flex;justify-content:flex-end;gap:10px;flex-wrap:wrap}}
	.log-window{{width:100%;min-height:220px;height:34vh;max-height:46vh;resize:vertical;border:1px solid #dbe4ef;border-radius:16px;padding:16px;background:#0f172a;color:#d1fae5;font-family:Consolas,"Courier New",monospace;font-size:13px;line-height:1.55;white-space:pre}} .log-window-console{{background:#111827;color:#e5e7eb}} .log-panel{{display:block;margin-bottom:14px}} .log-panel h3{{margin:0 0 6px;font-size:16px;color:#111827}} .log-panel p{{margin:0 0 10px;color:#64748b;font-size:12px}} .notice{{background:#ecfdf5;color:#065f46;border:1px solid #a7f3d0;padding:13px 15px;border-radius:14px;margin-bottom:16px}}
	.help-panel{{display:grid;grid-template-columns:1fr;gap:16px}} .help-card{{display:block;line-height:1.68}} .help-card::before{{display:none}} .help-card h3{{margin:18px 0 8px;font-size:16px;color:#111827}} .help-card h3:first-child{{margin-top:0}} .help-card p{{margin:6px 0;color:#475569;font-size:13px}} .help-grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}} .help-item{{border:1px solid #e2e8f0;border-radius:12px;padding:12px;background:#f8fafc}} .help-item strong{{display:block;margin-bottom:4px;color:#1f2937}} .help-list{{margin:8px 0 0;padding-left:18px;color:#475569;font-size:13px}} .help-list li{{margin:4px 0}} .help-table{{width:100%;border-collapse:collapse;margin-top:10px;font-size:13px}} .help-table th,.help-table td{{border:1px solid #e2e8f0;padding:9px 10px;text-align:left;vertical-align:top}} .help-table th{{background:#f1f5f9;color:#334155}} .help-note{{border-left:4px solid #8b6cf6;background:#f5f3ff;padding:10px 12px;border-radius:10px;color:#4c1d95;font-size:13px;margin-top:10px}} code{{background:#eef2ff;color:#4338ca;border-radius:6px;padding:1px 5px}}
	.history-result{{grid-column:1/-1;display:block;border:1px solid #e2e8f0;border-radius:14px;background:#f8fafc;padding:14px;min-height:86px;white-space:pre-wrap;font-family:Consolas,"Courier New",monospace;font-size:13px;color:#334155}} .accuracy-card{{display:block}} .accuracy-controls{{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:10px 0 12px}} .accuracy-controls select{{width:auto;min-width:150px}} .accuracy-controls .btn-save{{min-width:88px}} .accuracy-summary{{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin-bottom:12px}} .accuracy-summary div{{border:1px solid #e2e8f0;border-radius:12px;padding:10px;background:#f8fafc}} .accuracy-summary span{{display:block;color:#64748b;font-size:12px}} .accuracy-summary b{{display:block;margin-top:3px;font-size:18px;color:#111827}} .accuracy-canvas-wrap{{position:relative;height:320px;border:1px solid #e2e8f0;border-radius:14px;background:#0f172a;overflow:hidden}} .accuracy-canvas-wrap canvas{{width:100%;height:100%;cursor:default}} .accuracy-note{{margin:10px 0 0;color:#64748b;font-size:12px}}
	@media(max-width:860px){{.app{{grid-template-columns:1fr}}.sidebar{{position:relative;height:auto}}.card{{grid-template-columns:1fr}}.field{{grid-template-columns:1fr}}}}
	</style></head><body><div class="app"><aside class="sidebar"><div class="brand"><span class="logo">O</span><span>OKX AI</span></div>
	<a class="nav-item active" href="#monitor" data-page-link="monitor">监控</a><a class="nav-item" href="#config" data-page-link="config">配置</a><a class="nav-item" href="#logs" data-page-link="logs">日志</a><a class="nav-item" href="#tests" data-page-link="tests">测试</a><a class="nav-item" href="#help" data-page-link="help">帮助</a><a class="nav-item" href="#settings" data-page-link="settings">设置</a>
</aside><div class="content"><main>
<form class="config-form" method="post" action="/save#config">{''.join(rows)}<div class="actions config-actions" data-page-actions="config"><div class="action-group"><button class="action-control btn-save" type="button" id="saveConfigBtn">另存为配置</button><button class="action-control btn-save" type="button" id="importConfigBtn">导入配置</button><a class="button action-control btn-save" href="/config-json#config">查看配置</a></div></div></form>
<form class="settings-form" method="post" action="/save-auth#settings"><section class="card page-panel" data-page="settings"><h2>登录账号</h2><div class="field"><label>用户名</label><div><input type="text" name="auth_username" value="{esc(auth.get("username","admin"))}"><p>Web控制台登录用户名。</p></div></div><div class="field"><label>新密码</label><div><div class="password-wrap"><input type="password" name="auth_password" placeholder="留空则不修改"><button class="eye-btn" type="button" data-toggle-password aria-label="显示或隐藏密码"></button></div><p>建议首次部署后立即修改默认密码。</p></div></div></section><div class="actions settings-actions" data-page-actions="settings"><div class="action-group"><button class="action-control btn-save" type="submit">保存账号密码</button><a class="button action-control btn-view" href="/logout">切换账号</a><a class="button action-control btn-danger" href="/logout">退出登录</a></div></div></form>
<div class="page-panel active" data-page="monitor"><section class="card toolbar-card"><div><h2>实时监控</h2><p class="section-sub">未启动时显示虚拟行情；启动后读取真实监控日志，鼠标滚轮可缩放，拖动可平移。</p></div><div class="toolbar-right"><div class="coin-tabs">{monitor_tabs}</div><button class="button btn-run action-control" type="button" id="monitorToggleBtn">开始监控</button></div></section><section class="market-card monitor-card"><div class="market-head"><div><div class="market-title" id="monitorTitle">{esc(monitor_initial or "未配置币种")} 实时走势</div><div class="market-sub" id="monitorMeta">{esc("虚拟行情预览 · 启动监控后自动切换真实数据" if monitor_initial else "请先在配置页选择监控币种")}</div></div><div class="market-price" id="monitorPrice"><strong>--</strong><span>生成模拟行情</span></div></div><div class="market-canvas-wrap"><canvas id="monitorChart"></canvas><div class="market-loading" id="monitorLoading">正在生成虚拟走势...</div><div class="snapshot-panel" id="snapshotPanel"><strong>Snapshot</strong><div class="snapshot-grid"><span>价格</span><b>--</b><span>时间</span><b>--</b><span>评分</span><b>--</b><span>方向</span><b>--</b></div></div></div><div class="market-time-range"><span id="monitorUptime">已监控：未启动</span><span id="monitorPointCount">数据点：0</span></div></section></div>
	<div class="page-panel" data-page="logs"><section class="card toolbar-card"><div><h2>实时日志</h2><p class="section-sub">上方为 JSON 分析日志，下方为 okx_signal_monitor.py 控制台输出；均仅显示本次启动监控后的内容。</p></div><div class="toolbar-right"><button class="button btn-log" type="button" id="refreshLogBtn">刷新全部</button><button class="button btn-log" type="button" id="openLogDirBtn">打开日志目录</button></div></section><section class="card" style="display:block;"><div class="log-panel"><h3>JSON 分析日志</h3><p>结构化分析记录，默认保存：{esc(MONITOR_JSON_LOG_FILE)}</p><textarea class="log-window" id="logWindow" readonly>正在加载日志...</textarea><div class="toolbar-card" style="margin:12px 0 0;box-shadow:none;"><div><p class="section-sub" id="saveLogHint">可另存为 .jsonl 文件，便于回放与统计。</p></div><div class="toolbar-right"><button class="button btn-log" type="button" id="clearLogBtn">清除窗口</button><button class="btn-save" type="button" id="saveLogBtn">另存为文件</button></div></div></div><div class="log-panel"><h3>控制台日志</h3><p>监控进程 print 输出与运行状态，默认保存：{esc(MONITOR_PROCESS_LOG_FILE)}</p><textarea class="log-window log-window-console" id="consoleLogWindow" readonly>正在加载控制台日志...</textarea><div class="toolbar-card" style="margin:12px 0 0;box-shadow:none;"><div><p class="section-sub" id="saveConsoleLogHint">可另存为 .log 文件，便于排查采集、AI、推送异常。</p></div><div class="toolbar-right"><button class="button btn-log" type="button" id="clearConsoleLogBtn">清除窗口</button><button class="btn-save" type="button" id="saveConsoleLogBtn">另存为文件</button></div></div></div></section></div>
	<div class="page-panel" data-page="tests"><section class="card toolbar-card"><div><h2>连通性测试</h2><p class="section-sub">测试AI接口和微信推送（Server酱）配置是否可用。</p></div><div class="toolbar-right"><a class="button action-control btn-test" href="/test-ai#tests">测试AI</a><a class="button action-control btn-test" href="/test-push#tests">测试微信推送</a></div></section><section class="card accuracy-card"><h2>实时预测压测</h2><p class="section-sub">上方是累计可靠性压测指标；走势图可导出为 JSON 文件，后续在测试页导入回放；导入后暂停自动刷新，点「返回实时」恢复。</p><div class="accuracy-controls"><select id="accuracyInst"><option value="BTC-USDT-SWAP">BTC-USDT-SWAP</option><option value="ETH-USDT-SWAP">ETH-USDT-SWAP</option></select><select id="accuracyHorizon"><option value="5">下一轮约5秒</option><option value="15">15秒</option><option value="30">30秒</option><option value="60">60秒</option><option value="180">3分钟</option></select><select id="accuracyScope"><option value="session">本次启动后</option><option value="all">全部历史日志</option></select><select id="accuracyRetentionHours" title="结合配置页轮询间隔计算图表最多保留多少点"><option value="1">保留1小时</option><option value="2">保留2小时</option><option value="4" selected>保留4小时</option><option value="8">保留8小时</option><option value="12">保留12小时</option><option value="24">保留24小时</option><option value="48">保留48小时</option></select><button class="btn-test" type="button" id="accuracyRefreshBtn">刷新压测</button><button class="btn-save" type="button" id="accuracyExportBtn">导出图表</button><button class="btn-save" type="button" id="accuracyImportBtn">导入图表</button><button class="btn-test" type="button" id="accuracyLiveBtn" style="display:none">返回实时</button><input type="file" id="accuracyImportInput" accept=".json,application/json" hidden></div><div class="accuracy-summary" id="accuracySummary"><div><span>可靠性等级</span><b>--</b></div><div><span>已验证/日志</span><b>--</b></div><div><span>决策合理率</span><b>--</b></div><div><span>相对观望基准</span><b>--</b></div></div><div class="accuracy-canvas-wrap"><canvas id="accuracyChart"></canvas></div><p class="accuracy-note" id="accuracyNote">交易信号按做多/做空方向验证；观望按后续波动是否不足以形成可交易机会验证；系统需要长期跑赢“永远观望”基准才算有可靠性。</p></section><section class="card"><h2>近期历史回放测试</h2><div class="field"><label>币种</label><div><select id="historyInst"><option value="BTC-USDT-SWAP">BTC-USDT-SWAP</option><option value="ETH-USDT-SWAP">ETH-USDT-SWAP</option></select><p>建议选择已经在监控页启用并运行过的币种。</p></div></div><div class="field"><label>历史时间</label><div><input type="datetime-local" id="historyTime"><p>请选择当前时间前5到90分钟内的时间点，推荐15到30分钟前。</p></div></div><div class="field"><label>执行</label><div><button class="btn-test" type="button" id="historyTestBtn">运行回放</button><p>优先使用当时监控日志；没有日志时用近期K线重建，盘口/OI等会降级。</p></div></div><div class="history-result" id="historyResult">等待运行近期历史回放测试。</div></section></div>
	<div class="page-panel" data-page="help"><section class="card toolbar-card"><div><h2>帮助</h2><p class="section-sub">指标采集、计算逻辑、评分体系、界面字段和推送内容说明。</p></div></section><div class="help-panel">
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
	<h3>评分体系</h3><p>系统先由市场状态生成 <code>raw_direction</code>，再根据入场质量和风险降级为 <code>final_direction</code>。分数分为综合分、观察分和交易分：观察分用于判断市场是否值得关注，交易分用于判断是否适合执行；最终观望时交易分为0。</p>
	<ul class="help-list"><li><code>market_regime_score</code>：趋势、震荡、挤压、高波动等市场状态。</li><li><code>trend_score</code>：EMA/ADX/结构突破/多周期一致性。</li><li><code>momentum_score</code>：RSI、MACD、KDJ、背离和动能。</li><li><code>volume_price_score</code>：放量、量价方向、突破量能确认。</li><li><code>derivatives_score</code>：OI+价格组合、资金费率、多空拥挤。</li><li><code>orderbook_score</code>：top5/top20盘口支持和价差风险。</li><li><code>entry_quality_score</code>：价格距离EMA/ATR、入场区和等待确认。</li><li><code>risk_control_score</code>：资金费率、拥挤、高波动、背离、数据质量等风险控制。</li></ul>
	<div class="help-note">可靠性判断：指标越多不是越可靠，关键看数据质量、周期共振、量价确认、资金数据是否同向，以及是否触达入场区。系统给出的是观察和风险提示，不是自动交易指令；OI/资金费率刚启动不足15分钟时会降低变化类信号权重。</div>
	</section>
	<section class="card help-card"><h2>界面字段与推送内容解读</h2>
	<h3>走势图 Snapshot</h3><table class="help-table"><thead><tr><th>字段</th><th>含义</th><th>解读方式</th></tr></thead><tbody>
	<tr><td>分数</td><td><code>total_score / raw_total_score / final_trade_score</code></td><td>综合分是兼容展示；观察分高说明市场值得关注；交易分为0通常表示最终观望，等待入场条件或风险降低。</td></tr>
	<tr><td>方向</td><td><code>raw_direction → final_direction</code></td><td>原始方向来自市场偏向；最终方向会被入场质量、风险和等待确认降级。</td></tr>
	<tr><td>市场/策略</td><td><code>market_regime / strategy_template</code></td><td>先识别趋势、震荡、挤压或高波动，再匹配趋势回踩、区间边缘、等待突破、降低仓位等策略模板。</td></tr>
	<tr><td>风险/动作</td><td><code>market_risk_level / trade_action_level</code></td><td>市场风险描述行情是否过热、拥挤、背离或冲突；交易动作描述当前是否适合执行。高风险不等于一定反向。</td></tr>
	<tr><td>入场质量</td><td><code>entry_plan.quality / entry_quality_score</code></td><td>表示入场区是否有效，例如突破有效、等待确认、不可交易；价格离EMA20过远、高波动或震荡会降低质量。</td></tr>
	<tr><td>数据质量</td><td>15m确认K线数量和ready状态</td><td>确认K线越少，EMA/MACD/ADX/RSI越不稳定；数据不足时系统会偏向观望。</td></tr>
	<tr><td>预热</td><td><code>oi_warmup_ready / funding_warmup_ready</code></td><td>OI和资金费率变化类指标需要完整15分钟观察窗口；未预热完成时不应过度解读变化率。</td></tr>
	<tr><td>OI / OI 15m</td><td>当前持仓量和15分钟变化</td><td>价格上涨+OI上涨偏新增仓推动；价格上涨+OI下降偏空头回补，追多质量下降。下跌同理反向理解。</td></tr>
	<tr><td>资金费率</td><td>永续合约资金费率</td><td>绝对值过高说明单边拥挤，追单风险升高。</td></tr>
	<tr><td>多头/空头</td><td>账户多空比例换算</td><td>极端比例代表拥挤风险，不直接代表马上反转。</td></tr>
	<tr><td>放量</td><td>已收盘1m成交量 / 前20根均量 / 动态阈值</td><td>放量表示活跃度提高，必须结合方向、结构、OI和盘口判断；动态阈值来自近3小时约1分钟采样的分位数。</td></tr>
	<tr><td>ATR 15m</td><td>15m ATR百分比和波动状态</td><td>用于判断高波动、低波动、止损宽度和是否容易追在尾端。</td></tr>
	<tr><td>盘口</td><td>top20/top5不平衡与价差</td><td>正值偏买盘支撑，负值偏卖盘压力；盘口变化快，只作为小权重确认。</td></tr>
	<tr><td>分层</td><td>八层评分摘要</td><td>展示状态、趋势、动量、量价、合约、盘口、入场、风控各层得分，便于定位分数来源。</td></tr>
	</tbody></table>
	<h3>推送类型</h3><div class="help-grid"><div class="help-item"><strong>trade</strong><p>最终方向为做多或做空，且交易分达到推送阈值。重点看入场区、止损、止盈、失效条件。</p></div><div class="help-item"><strong>watch</strong><p>最终可能仍是观望，但观察分高且出现风险或异常信号，例如资金费率过热、RSI极端、布林挤压、多空拥挤。</p></div></div>
	<h3>入场、止损、止盈与追踪</h3><ul class="help-list"><li>入场区由ATR、结构位、EMA/VWAP近似锚点生成，不是固定百分比。</li><li>止损优先参考结构失效位，并加ATR缓冲。</li><li>止盈按风险距离生成两档，偏向1R/2R思路。</li><li>在线追踪先等待价格触达入场区；触达判断优先使用最新1m K线 high/low。</li><li>追踪成交价是保守估算：做多按入场区上沿，做空按入场区下沿，并记录 <code>fill_assumption</code>。</li><li>近期历史回放优先使用本地 <code>build/runtime_logs/okx_signal_analysis.jsonl</code> 的后续1m K线；没有日志时才用OKX历史K线兜底。</li></ul>
	<h3>回放测试解读</h3><ul class="help-list"><li><code>来源</code> 表示评分快照来自监控日志或K线重建；日志偏差越小越接近所选时间。</li><li><code>后续数据</code> 表示验证后续走势的数据来源，优先为 <code>signal-monitor-log</code>。</li><li><code>后续K线点数</code> 为0时不具备验证意义，通常说明所选时间之后没有持续监控日志，或OKX兜底数据未取到。</li><li><code>MFE/MAE</code> 表示入场后最大顺向/逆向波动；未触达入场区时只是观察方向的价格波动。</li><li><code>5m/15m/20m</code> 用目标时间后的收盘价计算方向表现，适合验证观察方向，不等于真实成交收益。</li></ul>
	<h3>本地文件位置</h3><ul class="help-list"><li>运行日志保存在 <code>build/runtime_logs</code>，默认配置模板保存在 <code>config</code>，本机密钥和登录认证保存在 <code>local_state</code>。</li><li><code>build</code> 是运行时产物目录，<code>local_state</code> 保存本机私密状态，均不应提交。</li><li>页面资源保留在 <code>web_assets</code>，源码入口是 <code>web_control_panel.py</code> 和 <code>okx_signal_monitor.py</code>。</li></ul>
	<h3>常见误读</h3><ul class="help-list"><li>观察分高不代表必须交易，可能只是风险异常值得关注。</li><li>市场风险低不代表一定盈利，只代表当前拥挤/过热/冲突较少。</li><li>盘口不平衡可能是假挂单，当前只作为小权重确认。</li><li>最终方向为观望时，入场/止损/止盈为 <code>-</code> 是正常结果。</li><li>AI分析会审计本地规则，但数据不足、预热不足或信号冲突时应优先观望。</li></ul>
	</section></div></div>
	</main></div></div>
<script>
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
  if (page === 'tests') fetchAccuracy();
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
  configForm.addEventListener('change', function() {{
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
	const historyTimeInput=document.getElementById('historyTime');
	if(historyTimeInput&&!historyTimeInput.value){{const d=new Date(Date.now()-20*60*1000),p=n=>String(n).padStart(2,'0');historyTimeInput.value=d.getFullYear()+'-'+p(d.getMonth()+1)+'-'+p(d.getDate())+'T'+p(d.getHours())+':'+p(d.getMinutes());}}
	const historyTestBtn=document.getElementById('historyTestBtn');
	if(historyTestBtn){{historyTestBtn.addEventListener('click',async function(){{const box=document.getElementById('historyResult'),inst=document.getElementById('historyInst').value,at=document.getElementById('historyTime').value;if(box)box.textContent='正在回放 '+inst+' @ '+at+' ...';historyTestBtn.disabled=true;try{{const response=await fetch('/api/history-test',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{inst_id:inst,time:at}}),cache:'no-store'}}),payload=await response.json();if(!response.ok||payload.ok===false)throw new Error(payload.error||'回放失败');const s=payload.score||{{}},o=payload.outcome||{{}},v=payload.verdict||{{}},src=payload.source||{{}};if(box)box.textContent=['来源: '+(src.source||'--')+(src.nearest_log_delta_seconds!=null?' / 日志偏差 '+src.nearest_log_delta_seconds+'s':''),'后续数据: '+(o.future_source||'--'),'方向: '+(s.raw_direction||'--')+' -> '+(s.final_direction||s.direction||'--'),'观察/交易分: '+(s.raw_total_score??'--')+' / '+(s.final_trade_score??'--'),'市场风险/交易动作: '+(s.market_risk_level||s.risk_level||'--')+' / '+(s.trade_action_level||'--'),'入场: '+(s.entry||'-'),'止损: '+(s.stop_loss||'-'),'止盈: '+(s.take_profit||'-'),'后续K线点数: '+(o.future_points??0),'入场触达: '+(o.entry_touched?'是':'否')+' / 假设成交价 '+(o.entry_price_assumed||'--'),'止损触发: '+(o.stop_hit?'是':'否'),'止盈命中: '+JSON.stringify(o.take_profit_hits||[]),'MFE/MAE: '+fmt(o.mfe_pct,3)+'% / '+fmt(o.mae_pct,3)+'%','5m/15m/20m: '+JSON.stringify(o.returns||{{}}),'结论: '+(v.direction_result||'--')+'，'+(v.entry_result||'--')+'，'+(v.risk_result||'--'),'备注: '+((v.notes||[]).join('；')||'无')].join('\\n');}}catch(error){{if(box)box.textContent='回放失败：'+error;}}finally{{historyTestBtn.disabled=false;}}}});}}
	const accuracyRefreshBtn=document.getElementById('accuracyRefreshBtn');
	let monitorIntervalSeconds={int(config.get("interval", 5))};
	const accuracyView={{points:[],start:0,end:1,yZoom:1,yPan:0,drag:null,followLatest:true}};
	let accuracyQueryKey='',accuracyLivePayload=null,accuracyImportedMode=false,accuracyImportedLabel='';
	function configuredMonitorInterval(){{const field=document.querySelector('input[name="interval"]');const n=Number(field&&field.value);return Number.isFinite(n)&&n>=1?n:monitorIntervalSeconds;}}
	function accuracyRetentionHours(){{const n=Number((document.getElementById('accuracyRetentionHours')||{{}}).value);return Number.isFinite(n)&&n>0?n:4;}}
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
	function resetAccuracyView(){{accuracyView.start=0;accuracyView.end=1;accuracyView.yZoom=1;accuracyView.yPan=0;accuracyView.followLatest=true;}}
	function visibleAccuracyPoints(){{const pts=accuracyView.points||[];if(pts.length<=2)return pts;const n=pts.length,span=Math.max(0.001,accuracyView.end-accuracyView.start),a=Math.floor(accuracyView.start*(n-1)),b=Math.min(n,Math.max(a+2,Math.ceil((accuracyView.start+span)*(n-1))+1));return pts.slice(a,b);}}
	function syncAccuracyPoints(points,options){{const opts=options||{{}},clean=(points||[]).filter(o=>Number.isFinite(Number(o&&o.price)));if(opts.resetView){{accuracyView.points=clean;resetAccuracyView();return;}}const followLatest=accuracyView.followLatest!==false&&accuracyView.end>=0.995;accuracyView.points=clean;if(followLatest&&clean.length>2){{const span=Math.max(0.001,accuracyView.end-accuracyView.start);accuracyView.end=1;accuracyView.start=Math.max(0,1-span);}}}}
	function redrawAccuracyChart(){{drawAccuracyChart();}}
	async function fetchAccuracy(options){{const canvas=document.getElementById('accuracyChart');if(!canvas)return;const resetView=!!(options&&options.resetView)||accuracyQuerySignature()!==accuracyQueryKey;if(accuracyImportedMode&&!(options&&options.resetView))return;if(options&&options.resetView)setAccuracyImportedMode(false,'');const inst=(document.getElementById('accuracyInst')||{{}}).value||'BTC-USDT-SWAP',h=(document.getElementById('accuracyHorizon')||{{}}).value||'5',scope=(document.getElementById('accuracyScope')||{{}}).value||'session',retention=accuracyRetentionHours(),interval=configuredMonitorInterval(),note=document.getElementById('accuracyNote');const queryKey=accuracyQuerySignature();accuracyQueryKey=queryKey;try{{if(note&&resetView)note.textContent='正在统计实时预测压测...';const qs='inst_id='+encodeURIComponent(inst)+'&horizon='+encodeURIComponent(h)+'&scope='+encodeURIComponent(scope)+'&retention_hours='+encodeURIComponent(retention)+'&interval_seconds='+encodeURIComponent(interval);const r=await fetch('/api/accuracy-data?'+qs,{{cache:'no-store'}}),p=await r.json();if(!r.ok||p.ok===false)throw new Error(p.error||'统计失败');accuracyLivePayload=p;const s=p.summary||{{}};updateAccuracySummary(s);syncAccuracyPoints(p.points||[],{{resetView:resetView}});redrawAccuracyChart();const maxPts=p.max_points||0,intervalSec=p.interval_seconds||interval,retainH=p.retention_hours||retention;if(note)note.textContent=(p.scope==='session'?'本次启动后':'全部历史')+' · 等级 '+(s.reliability_level||'--')+' · 分数 '+fmt(s.reliability_score,1)+' · 已验证/日志 '+(s.total??0)+'/'+(s.raw_log_total??s.total??0)+' · 图表保留 '+retainH+' 小时 / 轮询 '+intervalSec+' 秒 / 最多 '+maxPts+' 点 · 窗口 '+(p.horizon_seconds||h)+' 秒 · 双击图表重置缩放';}}catch(e){{updateAccuracySummary({{}});syncAccuracyPoints([],{{resetView:true}});redrawAccuracyChart();if(note)note.textContent='预测压测统计失败：'+e;}}}}
	if(accuracyRefreshBtn){{accuracyRefreshBtn.addEventListener('click',()=>fetchAccuracy({{resetView:true}}));}}
	const accuracyExportBtn=document.getElementById('accuracyExportBtn'),accuracyImportBtn=document.getElementById('accuracyImportBtn'),accuracyLiveBtn=document.getElementById('accuracyLiveBtn');
	if(accuracyExportBtn)accuracyExportBtn.addEventListener('click',exportAccuracyChart);
	if(accuracyImportBtn)accuracyImportBtn.addEventListener('click',importAccuracyChart);
	if(accuracyLiveBtn)accuracyLiveBtn.addEventListener('click',exitAccuracyImportedMode);
	['accuracyInst','accuracyHorizon','accuracyScope','accuracyRetentionHours'].forEach(id=>{{const el=document.getElementById(id);if(el)el.addEventListener('change',()=>fetchAccuracy({{resetView:true}}));}});
	function updateAccuracySummary(s){{const box=document.getElementById('accuracySummary');if(!box)return;const edge=s.model_edge_pct;const edgeText=edge!=null?(edge>=0?'+':'')+fmt(edge,1)+'pct':'--';const vals=[['可靠性等级',(s.reliability_level||'--')+' / '+(s.reliability_score!=null?fmt(s.reliability_score,1):'--')],['已验证/日志',(s.total??0)+' / '+(s.raw_log_total??s.total??0)],['待验证/成熟率',(s.pending_total??0)+' / '+(s.mature_rate_pct!=null?fmt(s.mature_rate_pct,1)+'%':'--')],['决策合理率',s.decision_accuracy_pct!=null?fmt(s.decision_accuracy_pct,1)+'% / '+(s.decision_total??0):'--'],['相对观望基准',edgeText+' / 基准 '+(s.baseline_watch_pct!=null?fmt(s.baseline_watch_pct,1)+'%':'--')],['观望区间可靠率',s.watch_reasonable_pct!=null?fmt(s.watch_reasonable_pct,1)+'% / '+(s.watch_total??0):'--'],['交易建议合理率',s.trade_signal_accuracy_pct!=null?fmt(s.trade_signal_accuracy_pct,1)+'% / '+(s.trade_signal_total??0):'--'],['入场触达/未成交',fmt(s.entry_touch_pct,1)+'% / '+fmt(s.no_fill_pct,1)+'%'],['止盈/止损',fmt(s.take_profit_pct,1)+'% / '+fmt(s.stop_hit_pct,1)+'%'],['交易胜率',fmt(s.trade_win_rate_pct,1)+'% / '+(s.trade_resolved_total??0)],['MFE/MAE均值',fmt(s.avg_mfe_pct,3)+'% / '+fmt(s.avg_mae_pct,3)+'%'],['趋势倾向命中率',s.trend_bias_accuracy_pct!=null?fmt(s.trend_bias_accuracy_pct,1)+'% / '+(s.trend_bias_total??0):'--']];box.innerHTML=vals.map(v=>'<div><span>'+v[0]+'</span><b>'+v[1]+'</b></div>').join('');}}
	function drawAccuracyChart(points){{if(Array.isArray(points))syncAccuracyPoints(points,{{resetView:true}});const c=document.getElementById('accuracyChart');if(!c)return;const d=window.devicePixelRatio||1,r=c.getBoundingClientRect();c.width=Math.max(1,r.width*d);c.height=Math.max(1,r.height*d);const x=c.getContext('2d');x.setTransform(d,0,0,d,0,0);const W=r.width,H=r.height,p={{l:58,r:46,t:24,b:40}},cw=W-p.l-p.r,ch=H-p.t-p.b;x.clearRect(0,0,W,H);x.fillStyle='#0f172a';x.fillRect(0,0,W,H);x.strokeStyle='rgba(148,163,184,.22)';x.lineWidth=1;for(let i=0;i<=4;i++){{const y=p.t+ch*i/4;x.beginPath();x.moveTo(p.l,y);x.lineTo(W-p.r,y);x.stroke();}}const clean=visibleAccuracyPoints();if(!clean.length){{x.fillStyle='rgba(203,213,225,.82)';x.textAlign='center';x.textBaseline='middle';x.font='13px Segoe UI, Microsoft YaHei';x.fillText('暂无可验证样本：启动监控后等待下一轮价格日志即可出现',W/2,H/2);return;}}const priceVals=clean.map(o=>Number(o.price)),mn=Math.min(...priceVals),mx=Math.max(...priceVals),priceRange=Math.max(0.000001,mx-mn);let pred=0;const predVals=clean.map(o=>{{pred+=accuracyDirectionValue(o);return pred;}}),pmn=Math.min(...predVals),pmx=Math.max(...predVals),predRange=Math.max(1,pmx-pmn),step=cw/Math.max(1,clean.length-1),xAt=i=>p.l+step*i,yFromNorm=n=>p.t+ch*(0.5-(n-(0.5+accuracyView.yPan))*accuracyView.yZoom),priceY=o=>yFromNorm((Number(o.price)-mn)/priceRange),predY=i=>yFromNorm((predVals[i]-pmn)/predRange);function line(color,width,yFn){{x.beginPath();clean.forEach((o,i)=>{{const px=xAt(i),py=yFn(o,i);if(i===0)x.moveTo(px,py);else x.lineTo(px,py);}});x.strokeStyle=color;x.lineWidth=width;x.stroke();}}line('#60a5fa',2.2,priceY);line('#fbbf24',2.2,(o,i)=>predY(i));clean.forEach((o,i)=>{{const px=xAt(i),py=priceY(o);x.fillStyle=o.hit?'#22d3ee':'#fb7185';x.beginPath();x.arc(px,py,2.8,0,Math.PI*2);x.fill();}});x.fillStyle='rgba(226,232,240,.92)';x.font='12px Segoe UI, Microsoft YaHei';x.textAlign='left';x.textBaseline='top';x.fillText('蓝线：价格曲线',p.l,p.t+4);x.fillStyle='#fbbf24';x.fillText('黄线：预测方向曲线（做多↑ 观望→ 做空↓）',p.l+96,p.t+4);x.fillStyle='rgba(226,232,240,.78)';x.textAlign='right';x.textBaseline='middle';x.fillText(fmt(mx,2),p.l-8,p.t);x.fillText(fmt((mx+mn)/2,2),p.l-8,p.t+ch/2);x.fillText(fmt(mn,2),p.l-8,p.t+ch);x.textAlign='left';x.fillText('pred +',W-p.r+6,p.t);x.fillText('pred -',W-p.r+6,p.t+ch);x.textAlign='center';x.textBaseline='top';for(let i=0;i<clean.length;i+=Math.max(1,Math.floor(clean.length/5))){{x.fillStyle='rgba(203,213,225,.78)';x.fillText(compactTime(clean[i].time),xAt(i),H-26);}}x.textAlign='right';x.fillStyle='rgba(203,213,225,.68)';x.fillText('可见 '+clean.length+' / 总计 '+accuracyView.points.length+' 点',W-p.r,H-26);}}
	function setupAccuracyChartInteractions(){{const c=document.getElementById('accuracyChart');if(!c||c.dataset.panZoomBound)return;c.dataset.panZoomBound='1';c.addEventListener('wheel',e=>{{if(!accuracyView.points.length)return;e.preventDefault();const rect=c.getBoundingClientRect(),mx=(e.clientX-rect.left)/Math.max(1,rect.width),factor=e.deltaY>0?1.18:0.85;if(e.shiftKey){{accuracyView.yZoom=clamp(accuracyView.yZoom/factor,0.35,12);}}else{{const span=accuracyView.end-accuracyView.start,newSpan=clamp(span*factor,Math.min(1,30/Math.max(30,accuracyView.points.length)),1),anchor=accuracyView.start+span*mx;accuracyView.start=clamp(anchor-newSpan*mx,0,1-newSpan);accuracyView.end=accuracyView.start+newSpan;}}accuracyView.followLatest=accuracyView.end>=0.995;drawAccuracyChart();}},{{passive:false}});c.addEventListener('mousedown',e=>{{accuracyView.drag={{x:e.clientX,y:e.clientY,start:accuracyView.start,end:accuracyView.end,yPan:accuracyView.yPan}};c.classList.add('dragging');}});window.addEventListener('mousemove',e=>{{const g=accuracyView.drag;if(!g)return;const rect=c.getBoundingClientRect(),dx=(e.clientX-g.x)/Math.max(1,rect.width),dy=(e.clientY-g.y)/Math.max(1,rect.height),span=g.end-g.start;accuracyView.start=clamp(g.start-dx*span,0,1-span);accuracyView.end=accuracyView.start+span;accuracyView.yPan=clamp(g.yPan+dy/Math.max(0.35,accuracyView.yZoom),-2,2);accuracyView.followLatest=accuracyView.end>=0.995;drawAccuracyChart();}});window.addEventListener('mouseup',()=>{{accuracyView.drag=null;c.classList.remove('dragging');}});c.addEventListener('dblclick',()=>{{resetAccuracyView();drawAccuracyChart();}});}}
	setupAccuracyChartInteractions();
	let virtualTick=0;
let configuredMonitorInsts={json.dumps(selected_instruments, ensure_ascii=False)};
let monitorInst={json.dumps(monitor_initial, ensure_ascii=False)}, monitorPayload=null, monitorLiveMode=false;
let monitorSeriesByInst={{}}, monitorLastTickerAt=0;
let virtualSeriesByInst={{}};
let monitorViewStart=0, monitorViewEnd=1;
let monitorVisiblePoints=[], monitorPlotPoints=[], monitorSelectedKey='', monitorLatestPoint=null;
let monitorYZoom=1, monitorYPan=0, monitorYRange=1, monitorDrag=null;
let monitorStartedAt='', monitorElapsedSeconds=0, monitorStatusRunning=false;
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
function updateChartFooter(points){{const c=document.getElementById('monitorPointCount');if(!c)return;c.textContent='数据点：'+((points&&points.length)||0);}}
function parsePointTime(t){{const s=String(t||'');const m=s.match(/^(\\d{{4}})-(\\d{{2}})-(\\d{{2}})[ T](\\d{{2}}):(\\d{{2}})(?::(\\d{{2}}))?/);if(m)return new Date(Number(m[1]),Number(m[2])-1,Number(m[3]),Number(m[4]),Number(m[5]),Number(m[6]||0));const d=new Date(s);return Number.isFinite(d.getTime())?d:new Date();}}
function formatPointTime(d){{const pad=n=>String(n).padStart(2,'0');return d.getFullYear()+'-'+pad(d.getMonth()+1)+'-'+pad(d.getDate())+' '+pad(d.getHours())+':'+pad(d.getMinutes())+':'+pad(d.getSeconds());}}
function bucket1mTime(t){{const d=parsePointTime(t);d.setSeconds(0,0);return formatPointTime(d);}}
function normalizeClientPoint(point,fallbackKind){{if(!point)return null;const price=Number(point.price);if(!Number.isFinite(price))return null;const normalized=Object.assign({{}},point);normalized.kind=normalized.kind||fallbackKind||'realtime';normalized.time=String(point.time||new Date().toLocaleString());if(normalized.kind!=='virtual')normalized.time=bucket1mTime(normalized.time);normalized.price=price;return normalized;}}
function mergeClientPoints(existing,incoming,maxPoints){{const merged=[],indexByTime={{}};[...(existing||[]),...(incoming||[])].forEach(point=>{{const normalized=normalizeClientPoint(point,'realtime');if(!normalized)return;const key=normalized.time||'';if(key&&Object.prototype.hasOwnProperty.call(indexByTime,key)){{merged[indexByTime[key]]=Object.assign({{}},merged[indexByTime[key]],normalized);return;}}if(key)indexByTime[key]=merged.length;merged.push(normalized);}});merged.sort((a,b)=>parsePointTime(a.time).getTime()-parsePointTime(b.time).getTime());return merged.slice(-Math.max(2,maxPoints||20000));}}
function setMonitorSeries(points){{const series=mergeClientPoints([],points||[],20000);monitorSeriesByInst[monitorInst]=series;monitorPayload={{points:series}};return series;}}
function appendMonitorPoints(points){{const series=mergeClientPoints(monitorSeriesByInst[monitorInst]||[],points||[],20000);monitorSeriesByInst[monitorInst]=series;monitorPayload={{points:series}};return series;}}
function drawMonitorSeries(metaText){{const series=monitorSeriesByInst[monitorInst]||[];if(series.length){{drawChart('monitorChart',series,document.getElementById('monitorPrice'),document.getElementById('monitorMeta'),document.getElementById('monitorLoading'));document.getElementById('monitorTitle').textContent=monitorInst+' 实时走势';const m=document.getElementById('monitorMeta');if(m&&metaText)m.textContent=metaText;return true;}}return false;}}
	function snapshotHtml(point,title){{if(!point)return '<strong>Snapshot</strong><div class="snapshot-grid"><span>价格</span><b>--</b><span>时间</span><b>--</b><span>评分</span><b>--</b><span>方向</span><b>--</b></div>';const hasMetrics=point.raw_total_score!==undefined&&point.raw_total_score!==null,kind=point.kind==='history'?(hasMetrics?'历史K线+日志指标':'历史K线'):'实时快照',dir=(point.raw_direction&&point.final_direction&&point.raw_direction!==point.final_direction)?(point.raw_direction+' → '+point.final_direction):(point.final_direction||point.direction||'--'),scoreText='综合 '+fmtScore(point.score)+' / 观察 '+fmtScore(point.raw_total_score)+' / 交易 '+fmtScore(point.final_trade_score),riskText=(point.market_risk_level||point.risk_level||'--')+' / '+(point.trade_action_level||'--'),lsAvail=point.long_short_available===false?'不可用':'可用',longShort=fmt(Number(point.long_ratio)*100,1)+'/'+fmt(Number(point.short_ratio)*100,1)+'% ('+lsAvail+')',warmup='OI '+fmtBool(point.oi_warmup_ready)+' / 费率 '+fmtBool(point.funding_warmup_ready),volumeText=fmt(point.volume_multiplier,2)+'x / 阈值 '+fmt(point.volume_threshold_used,2)+'x / '+displayValue(point.volume_direction)+'/'+displayValue(point.volume_trend),bookText=fmtSignedPct(Number(point.order_book_imbalance)*100,1)+' / top5 '+fmtSignedPct(Number(point.order_book_imbalance_5)*100,1)+' / spread '+fmt(point.spread_pct,4)+'%',qualityText=(point.data_quality_reliable===true?'可靠':(point.data_quality_reliable===false?'不足':'--'))+' / 15m确认K '+displayValue(point.data_quality_count),signals=compactList(point.signals,4),waitFor=compactList(point.wait_for,3);return '<strong>'+title+' · '+kind+'</strong><div class="snapshot-grid"><span>时间</span><b>'+compactTime(point.time)+'</b><span>价格</span><b>'+fmt(point.price,2)+'</b><span>分数</span><b>'+scoreText+'</b><span>方向</span><b>'+dir+'</b><span>市场/策略</span><b>'+displayValue(point.market_regime)+' / '+displayValue(point.strategy_template)+'</b><span>风险/动作</span><b>'+riskText+'</b><span>入场质量</span><b>'+displayValue(point.entry_quality)+' / '+fmtScore(point.entry_quality_score)+'</b><span>风控分</span><b>'+fmtScore(point.risk_control_score)+'</b><span>入场</span><b>'+displayValue(point.entry)+'</b><span>止损/止盈</span><b>'+displayValue(point.stop_loss)+' / '+displayValue(point.take_profit)+'</b><span>等待条件</span><b>'+waitFor+'</b><span>信号</span><b>'+signals+'</b><span>数据质量</span><b>'+qualityText+'</b><span>预热</span><b>'+warmup+'</b><span>OI</span><b>'+fmt(point.open_interest,2)+'</b><span>OI 15m</span><b>'+fmtPct(point.oi_change_pct_15m)+'</b><span>资金费率</span><b>'+fmt(point.funding_rate,6)+' / 变化 '+fmt(point.funding_change,6)+'</b><span>多头/空头</span><b>'+longShort+'</b><span>放量</span><b>'+volumeText+'</b><span>ATR 15m</span><b>'+fmt(point.atr_pct_15m,4)+'% / '+displayValue(point.volatility_regime)+'</b><span>盘口</span><b>'+bookText+'</b><span>分层</span><b>'+summarizeLayers(point.layer_scores)+'</b></div>';}}
function updateSnapshotPanel(point,title){{const panel=document.getElementById('snapshotPanel');if(panel)panel.innerHTML=snapshotHtml(point,title||'Snapshot');}}
function drawTimeAxis(x,W,H,p,points,cw){{if(!points||points.length<2)return;const maxLabels=Math.max(2,Math.min(8,Math.floor(W/150))),step=Math.max(1,Math.floor((points.length-1)/(maxLabels-1)));x.fillStyle='rgba(229,231,235,.9)';x.font='12px Segoe UI, Microsoft YaHei, Arial';x.textAlign='center';x.textBaseline='top';for(let i=0;i<points.length;i+=step){{const px=p.l+cw*i/(points.length-1);x.fillText(shortTime(points[i].time),px,H-24);}}const lastIndex=points.length-1;if((lastIndex%step)!==0){{x.fillText(shortTime(points[lastIndex].time),W-p.r,H-24);}}}}
function drawPriceAxis(x,W,H,p,mn,mx){{x.fillStyle='rgba(229,231,235,.82)';x.font='12px Segoe UI, Microsoft YaHei, Arial';x.textAlign='right';x.textBaseline='middle';for(let i=0;i<=4;i++){{const value=mx-(mx-mn)*i/4,y=p.t+(H-p.t-p.b)*i/4;x.fillText(value.toFixed(2),W-8,y);}}}}
function clearChartMessage(text){{const c=document.getElementById('monitorChart'),l=document.getElementById('monitorLoading'),p=document.getElementById('monitorPrice'),m=document.getElementById('monitorMeta'),t=document.getElementById('monitorTitle');monitorSelectedKey='';monitorVisiblePoints=[];monitorPlotPoints=[];monitorLatestPoint=null;if(c){{const r=c.getBoundingClientRect(),x=c.getContext('2d');c.width=Math.max(1,r.width);c.height=Math.max(1,r.height);x.clearRect(0,0,r.width,r.height);}}if(l){{l.style.display='grid';l.textContent=text;}}if(p)p.innerHTML='<strong>--</strong><span>无数据</span>';if(m)m.textContent=text;if(t)t.textContent='未配置币种';updateChartFooter([]);updateSnapshotPanel(null,'Snapshot');}}
function drawChart(id,points,priceBox,metaBox,loading){{const c=document.getElementById(id);if(!c||!points||points.length<1)return;if(points.length===1)points=[points[0],{{time:points[0].time,price:points[0].price}}];monitorLatestPoint=points[points.length-1];points=visiblePoints(points);monitorVisiblePoints=points;updateChartFooter(points);if(loading)loading.style.display='none';const d=window.devicePixelRatio||1,r=c.getBoundingClientRect();c.width=r.width*d;c.height=r.height*d;const x=c.getContext('2d');x.setTransform(d,0,0,d,0,0);const W=r.width,H=r.height,p={{l:34,r:74,t:18,b:58}},cw=W-p.l-p.r,ch=H-p.t-p.b,prices=points.map(q=>q.price);let mn=Math.min(...prices),mx=Math.max(...prices);const rawCenter=(mn+mx)/2,baseRg=Math.max(.01,(mx-mn)*1.16),center=rawCenter+monitorYPan,rg=baseRg/monitorYZoom;monitorYRange=rg;mn=center-rg/2;mx=center+rg/2;monitorPlotPoints=[];x.clearRect(0,0,W,H);x.strokeStyle='rgba(255,255,255,.12)';for(let i=0;i<=4;i++){{const y=p.t+ch*i/4;x.beginPath();x.moveTo(p.l,y);x.lineTo(W-p.r,y);x.stroke();}}x.beginPath();points.forEach((q,i)=>{{const px=p.l+cw*i/(points.length-1),py=p.t+ch-((q.price-mn)/rg)*ch;monitorPlotPoints.push({{x:px,y:py,point:q}});if(i===0)x.moveTo(px,py);else x.lineTo(px,py);}});const up=points[points.length-1].price>=points[0].price;x.strokeStyle=up?'#fb7185':'#22c55e';x.lineWidth=2;x.stroke();x.fillStyle=up?'rgba(251,113,133,.10)':'rgba(34,197,94,.08)';x.lineTo(W-p.r,H-p.b);x.lineTo(p.l,H-p.b);x.closePath();x.fill();drawTimeAxis(x,W,H,p,points,cw);drawPriceAxis(x,W,H,p,mn,mx);if(monitorSelectedKey){{const hit=monitorPlotPoints.find(o=>(o.point.time||'')===monitorSelectedKey);if(hit){{x.strokeStyle='rgba(255,255,255,.62)';x.setLineDash([4,5]);x.beginPath();x.moveTo(hit.x,p.t);x.lineTo(hit.x,H-p.b);x.stroke();x.beginPath();x.moveTo(p.l,hit.y);x.lineTo(W-p.r,hit.y);x.stroke();x.setLineDash([]);x.fillStyle='#60a5fa';x.beginPath();x.arc(hit.x,hit.y,5,0,7);x.fill();updateSnapshotPanel(hit.point,'选中点');}}else{{monitorSelectedKey='';updateSnapshotPanel(monitorLatestPoint,'最新快照');}}}}if(priceBox){{const first=points[0].price,last=points[points.length-1].price,chg=last-first,pct=first?chg/first*100:0;priceBox.classList.toggle('up',chg>=0);priceBox.classList.toggle('down',chg<0);priceBox.innerHTML='<strong>'+last.toFixed(2)+'</strong><span>'+(chg>=0?'+':'')+chg.toFixed(2)+' / '+(pct>=0?'+':'')+pct.toFixed(2)+'%</span>';}}if(!monitorSelectedKey)updateSnapshotPanel(monitorLatestPoint,'最新快照');if(metaBox)metaBox.textContent='更新：'+new Date().toLocaleTimeString();}}
function drawVirtualMonitor(){{if(!monitorInst){{clearChartMessage('请先在配置页选择监控币种');return;}}drawChart('monitorChart',nextVirtualSeries(),document.getElementById('monitorPrice'),document.getElementById('monitorMeta'),document.getElementById('monitorLoading'));document.getElementById('monitorTitle').textContent=monitorInst+' 虚拟走势';const m=document.getElementById('monitorMeta');if(m)m.textContent='虚拟行情预览 · 点击开始监控后切换真实数据';}}
function setMonitorButtonState(state,text){{const btn=document.getElementById('monitorToggleBtn'),meta=document.getElementById('monitorMeta');if(!btn)return;btn.classList.remove('is-running','is-starting');btn.disabled=false;if(state==='starting'){{btn.classList.add('is-starting');btn.textContent='启动中...';btn.disabled=true;if(meta)meta.textContent=text||'正在启动监控进程...';}}else if(state==='running'){{btn.classList.add('is-running');btn.textContent='停止监控';if(meta&&text)meta.textContent=text;}}else if(state==='stopping'){{btn.classList.add('is-starting');btn.textContent='停止中...';btn.disabled=true;if(meta)meta.textContent=text||'正在停止监控进程...';}}else{{btn.textContent='开始监控';if(meta&&text)meta.textContent=text;}}}}
async function syncMonitorStatus(){{try{{const r=await fetch('/api/status',{{cache:'no-store'}}),p=await r.json();updateMonitorUptime(p);setMonitorButtonState(p.running?'running':'stopped',p.text||'');return p;}}catch(e){{return null;}}}}
function showRealtimeWaiting(clearChart){{if(!monitorInst){{clearChartMessage('请先在配置页选择监控币种');return;}}monitorLiveMode=true;const c=document.getElementById('monitorChart'),l=document.getElementById('monitorLoading'),m=document.getElementById('monitorMeta'),t=document.getElementById('monitorTitle'),p=document.getElementById('monitorPrice');if(clearChart&&c){{const r=c.getBoundingClientRect(),x=c.getContext('2d');c.width=Math.max(1,r.width);c.height=Math.max(1,r.height);x.clearRect(0,0,r.width,r.height);updateChartFooter([]);}}if(l){{l.style.display=clearChart?'grid':'none';l.textContent='监控已启动，正在等待真实价格数据...';}}if(m)m.textContent=clearChart?'实时监控已启动 · 等待第一条价格数据':'已获取最新价 · 等待完整分析数据';if(t)t.textContent=monitorInst+' 实时走势';if(clearChart&&p)p.innerHTML='<strong>--</strong><span>等待真实数据</span>';}}
async function fetchMonitor(){{if(!monitorInst){{clearChartMessage('请先在配置页选择监控币种');return null;}}try{{const r=await fetch('/api/monitor-data?inst_id='+encodeURIComponent(monitorInst),{{cache:'no-store'}}),p=await r.json();if(!r.ok||p.ok===false){{clearChartMessage(p.error||'当前币种未配置，不能读取监控数据');return p;}}if(!p.running){{monitorLiveMode=false;monitorSeriesByInst[monitorInst]=[];drawVirtualMonitor();return p;}}monitorLiveMode=true;if(p.points&&p.points.length>0){{setMonitorSeries(p.points);const hasChart=p.source==='web-chart'||p.source==='signal-monitor-chart';document.getElementById('monitorTitle').textContent=monitorInst+(hasChart?' 1m K线走势':' 实时走势');const meta=p.source==='web-chart'?'Web获取1m K线 · 指标读取okx_signal_monitor.py日志':(p.source==='signal-monitor-chart'?'okx_signal_monitor.py 1m K线兜底 · 指标读取日志':'读取okx_signal_monitor.py实时日志 · 等待K线');drawMonitorSeries(meta+' · '+new Date().toLocaleTimeString());}}else if(!drawMonitorSeries('保留最近走势 · 等待K线/日志')){{showRealtimeWaiting(false);}}return p;}}catch(e){{if(monitorLiveMode){{if(!drawMonitorSeries('保留最近走势 · 等待K线/日志'))showRealtimeWaiting(false);}}else drawVirtualMonitor();return null;}}}}
function sleep(ms){{return new Promise(resolve=>setTimeout(resolve,ms));}}
async function bootstrapMonitorChart(){{monitorLastTickerAt=0;for(let i=0;i<16;i++){{const payload=await fetchMonitor();if(payload&&(payload.source==='web-chart'||payload.source==='signal-monitor-chart')&&payload.points&&payload.points.length>0)break;await sleep(i<4?500:1000);}}}}
function redrawMonitorCached(){{if(drawMonitorSeries())return;const series=virtualSeriesByInst[monitorInst]||[];if(series.length){{drawChart('monitorChart',series,document.getElementById('monitorPrice'),document.getElementById('monitorMeta'),document.getElementById('monitorLoading'));document.getElementById('monitorTitle').textContent=monitorInst+' 虚拟走势';}}else{{drawVirtualMonitor();}}}}
bindMonitorTabs();
const monitorToggleBtn=document.getElementById('monitorToggleBtn');
if(monitorToggleBtn){{monitorToggleBtn.addEventListener('click',async()=>{{const status=await syncMonitorStatus();if(status&&status.running){{setMonitorButtonState('stopping','正在停止监控进程...');try{{await fetch('/stop#monitor',{{cache:'no-store'}});}}catch(e){{}}await syncMonitorStatus();monitorLiveMode=false;monitorViewStart=0;monitorViewEnd=1;monitorYZoom=1;monitorYPan=0;monitorSeriesByInst[monitorInst]=[];setMonitorButtonState('stopped','监控已停止，当前显示虚拟行情');drawVirtualMonitor();return;}}if(!monitorInst){{clearChartMessage('请先在配置页选择监控币种');return;}}setMonitorButtonState('starting','正在保存配置并启动监控...');monitorYPan=0;showRealtimeWaiting(true);try{{await autoSaveConfig();await fetch('/start#monitor',{{cache:'no-store'}});await syncMonitorStatus();bootstrapMonitorChart();}}catch(e){{setMonitorButtonState('stopped','启动监控失败');const l=document.getElementById('monitorLoading');if(l)l.textContent='启动监控失败：'+e;}}}});}}
let logCleared=false,consoleLogCleared=false;
async function refreshLogs(force){{const box=document.getElementById('logWindow');if(!box)return;if(logCleared&&!force)return;try{{const r=await fetch('/api/logs',{{cache:'no-store'}}),p=await r.json();logCleared=false;box.value=p.text||'暂无日志。';box.scrollTop=box.scrollHeight;}}catch(e){{box.value='日志读取失败：'+e;}}}}
async function refreshConsoleLogs(force){{const box=document.getElementById('consoleLogWindow');if(!box)return;if(consoleLogCleared&&!force)return;try{{const r=await fetch('/api/console-logs',{{cache:'no-store'}}),p=await r.json();consoleLogCleared=false;box.value=p.text||'暂无控制台日志。';box.scrollTop=box.scrollHeight;}}catch(e){{box.value='控制台日志读取失败：'+e;}}}}
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
setInterval(()=>{{if(currentPage()==='tests')fetchAccuracy({{resetView:false}});}},3000);
setInterval(()=>{{if(currentPage()==='logs'){{refreshLogs(false);refreshConsoleLogs(false);}}}},3000);window.addEventListener('hashchange',()=>showPage(currentPage()));syncMonitorStatus();showPage(currentPage());
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
        if not self.is_authenticated():
            self.redirect("/login")
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
            self.send_json({"text": monitor_log_text(), "running": bool(monitor_status()["running"]), "default_path": str(MONITOR_JSON_LOG_FILE), "start_at": MONITOR_LOG_START_AT})
        elif path == "/api/console-logs":
            self.send_json({
                "text": monitor_console_log_text(),
                "running": bool(monitor_status()["running"]),
                "default_path": str(MONITOR_PROCESS_LOG_FILE),
                "start_at": MONITOR_LOG_START_AT,
            })
        elif path == "/api/accuracy-data":
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            inst_id = params.get("inst_id", ["BTC-USDT-SWAP"])[0]
            horizon = int(params.get("horizon", ["5"])[0])
            scope = params.get("scope", ["session"])[0]
            retention_hours = float(params.get("retention_hours", ["4"])[0])
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
        elif path == "/config-json":
            content = f"<pre>{esc(active_config_file().read_text(encoding='utf-8-sig'))}</pre>".encode("utf-8")
            self.send_html(content)
        elif path == "/start":
            self.send_html(render_page(start_monitor()))
        elif path == "/stop":
            self.send_html(render_page(stop_monitor()))
        elif path == "/test-ai":
            self.send_html(render_page(test_ai_connection()))
        elif path == "/test-push":
            self.send_html(render_page(test_push_connection()))
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8")
        if path == "/api/config/import":
            if not self.is_authenticated():
                self.send_json({"ok": False, "error": "未登录"}, status=401)
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
        if not self.is_authenticated():
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
        if path == "/api/history-test":
            try:
                payload = json.loads(raw or "{}")
                inst_id = str(payload.get("inst_id", "BTC-USDT-SWAP")).strip() or "BTC-USDT-SWAP"
                at_time = str(payload.get("time", "")).strip()
                self.send_json(run_history_test(inst_id, at_time))
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
