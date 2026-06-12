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
    from okx_signal_monitor import OkxAiShortTermAssistant, RuntimeConfig, SignalConfig, trend_profile_from_candles
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
    RuntimeConfig = signal_monitor_module.RuntimeConfig
    SignalConfig = signal_monitor_module.SignalConfig
    trend_profile_from_candles = signal_monitor_module.trend_profile_from_candles

BUILD_DIR = SCRIPT_DIR / "build"
LOCAL_STATE_DIR = BUILD_DIR / "local_state"
ASSETS_DIR = SCRIPT_DIR / "web_assets"
LOG_DIR = BUILD_DIR / "runtime_logs"
CONFIG_FILE = LOCAL_STATE_DIR / "trading_assistant_config.json"
ENV_FILE = LOCAL_STATE_DIR / "api_secrets.env"
AUTH_FILE = LOCAL_STATE_DIR / "web_console_auth.json"
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


CONFIG_FIELDS = [
    ("基础运行", "interval", "number", "轮询间隔(秒)", "默认5秒执行一轮监控。"),
    ("基础运行", "runtime", "number", "运行时长(秒)", "0表示一直运行，300表示运行5分钟。"),
    ("基础运行", "flag", "choice", "OKX环境", "0正式环境，1模拟盘。"),
    ("AI与推送", "ai_enabled", "checkbox", "启用AI分析", "触发信号后调用AI。"),
    ("AI与推送", "dry_run_ai", "checkbox", "AI dry-run", "只生成AI请求数据，不真实调用AI。"),
    ("AI与推送", "push_enabled", "checkbox", "启用微信推送", "满足信号和评分条件时发送微信机器人推送。"),
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
    ("WECHAT_WEBHOOK_URL", "微信机器人Webhook", "微信机器人地址。"),
]


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def active_config_file() -> Path:
    return USER_CONFIG_FILE if USER_CONFIG_FILE.exists() else CONFIG_FILE


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8-sig"))


def load_config() -> Dict[str, Any]:
    return load_json(active_config_file(), {})


def configured_instruments() -> List[str]:
    inst_ids = load_config().get("inst_ids", [])
    if isinstance(inst_ids, str):
        inst_ids = [inst_ids]
    return [inst for inst in inst_ids if inst in SUPPORTED_INSTRUMENTS]


def save_config(config: Dict[str, Any]) -> Path:
    text = json.dumps(config, indent=2, ensure_ascii=False) + "\n"
    try:
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(text, encoding="utf-8")
        if USER_CONFIG_FILE.exists():
            USER_CONFIG_FILE.write_text(text, encoding="utf-8")
        return CONFIG_FILE
    except PermissionError:
        USER_STATE_DIR.mkdir(parents=True, exist_ok=True)
        USER_CONFIG_FILE.write_text(text, encoding="utf-8")
        return USER_CONFIG_FILE


def load_auth() -> Dict[str, str]:
    for path in (USER_AUTH_FILE, AUTH_FILE):
        if path.exists():
            return load_json(path, {"username": "admin", "password": "admin123"})
    save_auth("admin", "admin123")
    return {"username": "admin", "password": "admin123"}


def save_auth(username: str, password: str) -> Path:
    data = {"username": username.strip() or "admin", "password": password.strip() or "admin123"}
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    try:
        AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
        AUTH_FILE.write_text(text, encoding="utf-8")
        return AUTH_FILE
    except PermissionError:
        USER_STATE_DIR.mkdir(parents=True, exist_ok=True)
        USER_AUTH_FILE.write_text(text, encoding="utf-8")
        return USER_AUTH_FILE


def load_env() -> Dict[str, str]:
    env: Dict[str, str] = {}
    for path in (ENV_FILE, USER_ENV_FILE):
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def save_env(env: Dict[str, str]) -> Path:
    lines = [
        "# OKX AI短线助手环境变量配置",
        "",
        f'OPENAI_API_KEY="{env.get("OPENAI_API_KEY", "")}"',
        f'AI_MODEL="{env.get("AI_MODEL", "gpt-5.5")}"',
        f'WECHAT_WEBHOOK_URL="{env.get("WECHAT_WEBHOOK_URL", "")}"',
        "",
    ]
    text = "\n".join(lines)
    try:
        ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
        ENV_FILE.write_text(text, encoding="utf-8")
        return ENV_FILE
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
        elif kind == "choice":
            config[key] = str(form.get(key, "0"))
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
    ]
    if config.get("ai_enabled"):
        args.append("--ai")
    if config.get("dry_run_ai"):
        args.append("--dry-run-ai")
    if config.get("push_enabled"):
        args.append("--push")
    return args


def monitor_status() -> Dict[str, Any]:
    global MONITOR_PROCESS
    if MONITOR_PROCESS is None:
        return {"running": False, "text": "未启动", "started_at": ""}
    code = MONITOR_PROCESS.poll()
    if code is None:
        return {"running": True, "text": f"运行中 PID={MONITOR_PROCESS.pid}", "started_at": MONITOR_STARTED_AT}
    return {"running": False, "text": f"已停止，退出码={code}", "started_at": MONITOR_STARTED_AT}


def start_monitor() -> str:
    global MONITOR_PROCESS, MONITOR_STARTED_AT, MONITOR_LOG_START_AT
    status = monitor_status()
    if status["running"]:
        return status["text"]
    if not configured_instruments():
        return "请先在配置页至少选择一个监控币种。"
    MONITOR_STARTED_AT = time.strftime("%Y-%m-%d %H:%M:%S")
    MONITOR_LOG_START_AT = MONITOR_STARTED_AT
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
    global MONITOR_PROCESS
    status = monitor_status()
    if not status["running"]:
        return "监控未运行。"
    MONITOR_PROCESS.terminate()
    try:
        MONITOR_PROCESS.wait(timeout=8)
    except subprocess.TimeoutExpired:
        MONITOR_PROCESS.kill()
        MONITOR_PROCESS.wait(timeout=5)
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
    url = build_child_env().get("WECHAT_WEBHOOK_URL", "")
    if not url:
        return "推送测试失败：未配置微信机器人Webhook。"
    try:
        result = post_json(url, {"msgtype": "text", "text": {"content": "[OKX AI短线助手] 推送测试成功。"}})
        return f"微信机器人成功：{result[:160]}"
    except Exception as exc:
        return f"微信机器人失败：{exc}"


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


def get_history_candles_before(inst_id: str, bar: str, at_time: datetime, limit: int = 120) -> List[Dict[str, Any]]:
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


def get_future_1m_candles(inst_id: str, at_time: datetime, minutes: int = 25) -> List[Dict[str, Any]]:
    end_time = datetime.fromtimestamp(at_time.timestamp() + minutes * 60)
    rows = get_history_candles_before(inst_id, "1m", end_time, limit=max(30, minutes + 8))
    selected = []
    for row in rows:
        try:
            row_time = parse_history_time(str(row.get("time", "")))
        except Exception:
            continue
        if at_time < row_time <= end_time:
            selected.append(row)
    selected.sort(key=lambda item: item.get("time", ""))
    return selected


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
    future = get_future_1m_candles(inst_id, at_time, 25)
    outcome = evaluate_history_outcome(snapshot, score, future)
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
    rows.append('<section class="card page-section" data-page="config"><h2>AI密钥与微信机器人</h2>')
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
	.market-card{{background:#242424;border:1px solid #3a3a3a;border-radius:18px;margin:0;padding:18px 20px 16px;color:#f8fafc;box-shadow:0 12px 34px rgba(15,23,42,.16);overflow:hidden;height:calc(100vh - 40px);display:flex;flex-direction:column}} .monitor-card{{height:calc(100vh - 188px);min-height:520px}} .market-head{{display:flex;justify-content:space-between;align-items:flex-start;gap:14px;margin-bottom:12px}} .market-title{{font-size:18px;font-weight:800;margin-bottom:4px}} .market-sub{{color:#a3a3a3;font-size:12px}} .market-price{{text-align:right}} .market-price strong{{display:block;font-size:24px;line-height:1.1}} .market-price span{{font-size:13px;color:#94a3b8}} .market-price.up span{{color:#22c55e}} .market-price.down span{{color:#fb7185}} .market-canvas-wrap{{position:relative;flex:1;min-height:0;border-top:1px solid #393939;background:linear-gradient(rgba(255,255,255,.055) 1px,transparent 1px),linear-gradient(90deg,rgba(255,255,255,.035) 1px,transparent 1px);background-size:100% 72px,72px 100%}} canvas{{width:100%;height:100%;display:block;cursor:grab}} canvas.dragging{{cursor:grabbing}} .market-loading{{position:absolute;inset:0;display:grid;place-items:center;color:#a3a3a3;pointer-events:none}} .snapshot-panel{{position:absolute;left:16px;top:16px;z-index:2;min-width:300px;max-width:min(560px,calc(100% - 32px));max-height:calc(100% - 32px);overflow:auto;padding:12px 14px;border-radius:12px;background:rgba(15,23,42,.62);border:1px solid rgba(148,163,184,.20);box-shadow:0 12px 28px rgba(0,0,0,.18);backdrop-filter:blur(8px);font-size:12px;color:#dbeafe;pointer-events:none}} .snapshot-panel strong{{display:block;color:#fff;font-size:13px;margin-bottom:6px}} .snapshot-grid{{display:grid;grid-template-columns:minmax(76px,.8fr) minmax(96px,1.2fr);gap:4px 12px;align-items:start}} .snapshot-grid span{{color:#9ca3af}} .snapshot-grid b{{font-weight:700;color:#e5e7eb;word-break:break-word}} .market-time-range{{margin-top:10px;padding-top:10px;border-top:1px solid rgba(255,255,255,.08);color:#a3a3a3;font-size:12px;display:flex;justify-content:flex-end;gap:10px;flex-wrap:wrap}}
	.log-window{{width:100%;min-height:calc(100vh - 250px);resize:vertical;border:1px solid #dbe4ef;border-radius:16px;padding:16px;background:#0f172a;color:#d1fae5;font-family:Consolas,"Courier New",monospace;font-size:13px;line-height:1.55;white-space:pre}} .notice{{background:#ecfdf5;color:#065f46;border:1px solid #a7f3d0;padding:13px 15px;border-radius:14px;margin-bottom:16px}}
	.help-panel{{display:grid;grid-template-columns:1fr;gap:16px}} .help-card{{display:block;line-height:1.68}} .help-card::before{{display:none}} .help-card h3{{margin:18px 0 8px;font-size:16px;color:#111827}} .help-card h3:first-child{{margin-top:0}} .help-card p{{margin:6px 0;color:#475569;font-size:13px}} .help-grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}} .help-item{{border:1px solid #e2e8f0;border-radius:12px;padding:12px;background:#f8fafc}} .help-item strong{{display:block;margin-bottom:4px;color:#1f2937}} .help-list{{margin:8px 0 0;padding-left:18px;color:#475569;font-size:13px}} .help-list li{{margin:4px 0}} .help-table{{width:100%;border-collapse:collapse;margin-top:10px;font-size:13px}} .help-table th,.help-table td{{border:1px solid #e2e8f0;padding:9px 10px;text-align:left;vertical-align:top}} .help-table th{{background:#f1f5f9;color:#334155}} .help-note{{border-left:4px solid #8b6cf6;background:#f5f3ff;padding:10px 12px;border-radius:10px;color:#4c1d95;font-size:13px;margin-top:10px}} code{{background:#eef2ff;color:#4338ca;border-radius:6px;padding:1px 5px}}
	.history-result{{grid-column:1/-1;display:block;border:1px solid #e2e8f0;border-radius:14px;background:#f8fafc;padding:14px;min-height:86px;white-space:pre-wrap;font-family:Consolas,"Courier New",monospace;font-size:13px;color:#334155}}
	@media(max-width:860px){{.app{{grid-template-columns:1fr}}.sidebar{{position:relative;height:auto}}.card{{grid-template-columns:1fr}}.field{{grid-template-columns:1fr}}}}
	</style></head><body><div class="app"><aside class="sidebar"><div class="brand"><span class="logo">O</span><span>OKX AI</span></div>
	<a class="nav-item active" href="#monitor" data-page-link="monitor">监控</a><a class="nav-item" href="#config" data-page-link="config">配置</a><a class="nav-item" href="#logs" data-page-link="logs">日志</a><a class="nav-item" href="#tests" data-page-link="tests">测试</a><a class="nav-item" href="#help" data-page-link="help">帮助</a><a class="nav-item" href="#settings" data-page-link="settings">设置</a>
</aside><div class="content"><main>
<form class="config-form" method="post" action="/save#config">{''.join(rows)}<div class="actions config-actions" data-page-actions="config"><div class="action-group"><button class="action-control btn-save" type="button" id="saveConfigBtn">另存为配置</button><button class="action-control btn-save" type="button" id="importConfigBtn">导入配置</button><a class="button action-control btn-save" href="/config-json#config">查看配置</a></div></div></form>
<form class="settings-form" method="post" action="/save-auth#settings"><section class="card page-panel" data-page="settings"><h2>登录账号</h2><div class="field"><label>用户名</label><div><input type="text" name="auth_username" value="{esc(auth.get("username","admin"))}"><p>Web控制台登录用户名。</p></div></div><div class="field"><label>新密码</label><div><div class="password-wrap"><input type="password" name="auth_password" placeholder="留空则不修改"><button class="eye-btn" type="button" data-toggle-password aria-label="显示或隐藏密码"></button></div><p>建议首次部署后立即修改默认密码。</p></div></div></section><div class="actions settings-actions" data-page-actions="settings"><div class="action-group"><button class="action-control btn-save" type="submit">保存账号密码</button><a class="button action-control btn-view" href="/logout">切换账号</a><a class="button action-control btn-danger" href="/logout">退出登录</a></div></div></form>
<div class="page-panel active" data-page="monitor"><section class="card toolbar-card"><div><h2>实时监控</h2><p class="section-sub">未启动时显示虚拟行情；启动后读取真实监控日志，鼠标滚轮可缩放，拖动可平移。</p></div><div class="toolbar-right"><div class="coin-tabs">{monitor_tabs}</div><button class="button btn-run action-control" type="button" id="monitorToggleBtn">开始监控</button></div></section><section class="market-card monitor-card"><div class="market-head"><div><div class="market-title" id="monitorTitle">{esc(monitor_initial or "未配置币种")} 实时走势</div><div class="market-sub" id="monitorMeta">{esc("虚拟行情预览 · 启动监控后自动切换真实数据" if monitor_initial else "请先在配置页选择监控币种")}</div></div><div class="market-price" id="monitorPrice"><strong>--</strong><span>生成模拟行情</span></div></div><div class="market-canvas-wrap"><canvas id="monitorChart"></canvas><div class="market-loading" id="monitorLoading">正在生成虚拟走势...</div><div class="snapshot-panel" id="snapshotPanel"><strong>Snapshot</strong><div class="snapshot-grid"><span>价格</span><b>--</b><span>时间</span><b>--</b><span>评分</span><b>--</b><span>方向</span><b>--</b></div></div></div><div class="market-time-range"><span id="monitorPointCount">数据点：0</span></div></section></div>
	<div class="page-panel" data-page="logs"><section class="card toolbar-card"><div><h2>实时日志</h2><p class="section-sub">仅显示本次启动监控后的JSON分析日志。默认保存：{esc(MONITOR_JSON_LOG_FILE)}</p></div><div class="toolbar-right"><button class="button btn-log" type="button" id="refreshLogBtn">刷新日志</button><button class="button btn-log" type="button" id="openLogDirBtn">打开日志目录</button><button class="button btn-log" type="button" id="clearLogBtn">清除窗口</button></div></section><section class="card" style="display:block;"><textarea class="log-window" id="logWindow" readonly>正在加载日志...</textarea><div class="toolbar-card" style="margin:14px 0 0;box-shadow:none;"><div><h2>保存日志</h2><p class="section-sub" id="saveLogHint">点击后弹出文件另存为窗口，可手动选择位置并输入文件名。</p></div><button class="btn-save" type="button" id="saveLogBtn">另存为日志文件</button></div></section></div>
	<div class="page-panel" data-page="tests"><section class="card toolbar-card"><div><h2>连通性测试</h2><p class="section-sub">测试AI接口和微信机器人推送配置是否可用。</p></div><div class="toolbar-right"><a class="button action-control btn-test" href="/test-ai#tests">测试AI</a><a class="button action-control btn-test" href="/test-push#tests">测试微信推送</a></div></section><section class="card"><h2>近期历史回放测试</h2><div class="field"><label>币种</label><div><select id="historyInst"><option value="BTC-USDT-SWAP">BTC-USDT-SWAP</option><option value="ETH-USDT-SWAP">ETH-USDT-SWAP</option></select><p>建议选择已经在监控页启用并运行过的币种。</p></div></div><div class="field"><label>历史时间</label><div><input type="datetime-local" id="historyTime"><p>请选择当前时间前5到90分钟内的时间点，推荐15到30分钟前。</p></div></div><div class="field"><label>执行</label><div><button class="btn-test" type="button" id="historyTestBtn">运行回放</button><p>优先使用当时监控日志；没有日志时用近期K线重建，盘口/OI等会降级。</p></div></div><div class="history-result" id="historyResult">等待运行近期历史回放测试。</div></section></div>
	<div class="page-panel" data-page="help"><section class="card toolbar-card"><div><h2>帮助</h2><p class="section-sub">指标采集、计算逻辑、评分体系、界面字段和推送内容说明。</p></div></section><div class="help-panel">
	<section class="card help-card"><h2>采集参数、计算指标与评分产出</h2>
	<h3>采集的数据</h3><div class="help-grid">
	<div class="help-item"><strong>行情与K线</strong><p>采集 BTC-USDT-SWAP、ETH-USDT-SWAP 的 ticker、盘口买卖一档、1m/3m/5m/15m/1H/4H K线。K线字段包括时间、开高低收、成交量、是否收盘。</p></div>
	<div class="help-item"><strong>合约资金数据</strong><p>采集 Open Interest、资金费率、5m账户多空比。OI和资金费率会保存本地短周期历史，用于计算15分钟变化。</p></div>
	<div class="help-item"><strong>盘口数据</strong><p>采集前20档订单簿，计算 top5/top20 买卖量、盘口不平衡、价差百分比。盘口只作为短线确认，不单独决定方向。</p></div>
	<div class="help-item"><strong>运行内统计</strong><p>保存成交量倍数、ATR百分比、盘口不平衡的近期样本，用分位数生成动态阈值；信号结算样本写入 <code>build/runtime_logs/signal_performance.jsonl</code>。</p></div>
	</div>
	<h3>计算的技术指标</h3><table class="help-table"><thead><tr><th>类别</th><th>指标</th><th>用途</th></tr></thead><tbody>
	<tr><td>趋势</td><td>EMA9/20/60/120、MA120、结构高低点、ADX/+DI/-DI</td><td>判断趋势排列、趋势强度、短中周期是否共振，以及是否处于震荡弱趋势。</td></tr>
	<tr><td>动量</td><td>RSI6/14/24、MACD、KDJ、K线实体占比、RSI背离</td><td>判断动能是否增强、过热、衰减或背离；KDJ用于短线入场时机确认。</td></tr>
	<tr><td>波动</td><td>ATR、ATR%、布林带、布林带宽度</td><td>识别高波动、低波动、挤压蓄势；入场区、止损、止盈根据ATR和结构位生成。</td></tr>
	<tr><td>量价</td><td>已收盘1m放量倍数、成交量方向、近5根量能趋势</td><td>判断突破或回踩是否有成交量确认，避免只看价格方向。</td></tr>
	<tr><td>合约资金</td><td>OI 15m变化、资金费率、资金费率变化、多空比</td><td>判断新增仓、平仓、拥挤、过热和反身性风险。</td></tr>
	</tbody></table>
	<h3>评分体系</h3><p>系统先生成 <code>raw_direction</code>，再根据入场质量决定 <code>final_direction</code>。分数分为观察分和交易分：观察分用于判断市场是否值得关注，交易分用于判断是否适合执行。</p>
	<ul class="help-list"><li><code>market_regime_score</code>：趋势、震荡、挤压、高波动等市场状态。</li><li><code>trend_score</code>：EMA/ADX/结构突破/多周期一致性。</li><li><code>momentum_score</code>：RSI、MACD、KDJ、背离和动能。</li><li><code>volume_price_score</code>：放量、量价方向、突破量能确认。</li><li><code>derivatives_score</code>：OI+价格组合、资金费率、多空拥挤。</li><li><code>orderbook_score</code>：top5/top20盘口支持和价差风险。</li><li><code>entry_quality_score</code>：价格距离EMA/ATR、入场区和等待确认。</li><li><code>risk_control_score</code>：资金费率、拥挤、高波动、背离、数据质量等风险控制。</li></ul>
	<div class="help-note">可靠性判断：指标越多不是越可靠，关键看数据质量、周期共振、量价确认、资金数据是否同向，以及是否触达入场区。系统给出的是观察和风险提示，不是自动交易指令。</div>
	</section>
	<section class="card help-card"><h2>界面字段与推送内容解读</h2>
	<h3>走势图 Snapshot</h3><table class="help-table"><thead><tr><th>字段</th><th>含义</th><th>解读方式</th></tr></thead><tbody>
	<tr><td>观察/交易分</td><td><code>raw_total_score / final_trade_score</code></td><td>观察分高说明市场值得关注；交易分为0通常表示最终观望，等待入场条件或风险降低。</td></tr>
	<tr><td>方向</td><td><code>raw_direction → final_direction</code></td><td>原始方向来自市场偏向；最终方向会被入场质量、风险和等待确认降级。</td></tr>
	<tr><td>市场风险</td><td><code>market_risk_level</code></td><td>描述行情本身是否过热、拥挤、背离或冲突。高风险不等于一定反向。</td></tr>
	<tr><td>交易动作</td><td><code>trade_action_level</code></td><td>描述当前是否适合执行：观望、等待确认、可关注、不建议。</td></tr>
	<tr><td>OI / OI 15m</td><td>当前持仓量和15分钟变化</td><td>价格上涨+OI上涨偏新增仓推动；价格上涨+OI下降偏空头回补，追多质量下降。下跌同理反向理解。</td></tr>
	<tr><td>资金费率</td><td>永续合约资金费率</td><td>绝对值过高说明单边拥挤，追单风险升高。</td></tr>
	<tr><td>多头/空头</td><td>账户多空比例换算</td><td>极端比例代表拥挤风险，不直接代表马上反转。</td></tr>
	<tr><td>放量倍数</td><td>最近已收盘1m成交量 / 前20根均量</td><td>放量表示活跃度提高，必须结合方向、结构、OI和盘口判断。</td></tr>
	</tbody></table>
	<h3>推送类型</h3><div class="help-grid"><div class="help-item"><strong>trade</strong><p>最终方向为做多或做空，且交易分达到推送阈值。重点看入场区、止损、止盈、失效条件。</p></div><div class="help-item"><strong>watch</strong><p>最终可能仍是观望，但观察分高且出现风险或异常信号，例如资金费率过热、RSI极端、布林挤压、多空拥挤。</p></div></div>
	<h3>入场、止损、止盈与追踪</h3><ul class="help-list"><li>入场区由ATR、结构位、EMA/VWAP近似锚点生成，不是固定百分比。</li><li>止损优先参考结构失效位，并加ATR缓冲。</li><li>止盈按风险距离生成两档，偏向1R/2R思路。</li><li>在线追踪先等待价格触达入场区；触达判断优先使用最新1m K线 high/low。</li><li>追踪成交价是保守估算：做多按入场区上沿，做空按入场区下沿，并记录 <code>fill_assumption</code>。</li></ul>
	<h3>常见误读</h3><ul class="help-list"><li>观察分高不代表必须交易，可能只是风险异常值得关注。</li><li>市场风险低不代表一定盈利，只代表当前拥挤/过热/冲突较少。</li><li>盘口不平衡可能是假挂单，当前只作为小权重确认。</li><li>AI分析会审计本地规则，但数据不足、预热不足或信号冲突时应优先观望。</li></ul>
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
  if (page === 'logs') refreshLogs(false);
  if (page === 'monitor') fetchMonitor(false);
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
	if(historyTestBtn){{historyTestBtn.addEventListener('click',async function(){{const box=document.getElementById('historyResult'),inst=document.getElementById('historyInst').value,at=document.getElementById('historyTime').value;if(box)box.textContent='正在回放 '+inst+' @ '+at+' ...';historyTestBtn.disabled=true;try{{const response=await fetch('/api/history-test',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{inst_id:inst,time:at}}),cache:'no-store'}}),payload=await response.json();if(!response.ok||payload.ok===false)throw new Error(payload.error||'回放失败');const s=payload.score||{{}},o=payload.outcome||{{}},v=payload.verdict||{{}},src=payload.source||{{}};if(box)box.textContent=['来源: '+(src.source||'--')+(src.nearest_log_delta_seconds!=null?' / 日志偏差 '+src.nearest_log_delta_seconds+'s':''),'方向: '+(s.raw_direction||'--')+' -> '+(s.final_direction||s.direction||'--'),'观察/交易分: '+(s.raw_total_score??'--')+' / '+(s.final_trade_score??'--'),'市场风险/交易动作: '+(s.market_risk_level||s.risk_level||'--')+' / '+(s.trade_action_level||'--'),'入场: '+(s.entry||'-'),'止损: '+(s.stop_loss||'-'),'止盈: '+(s.take_profit||'-'),'后续K线点数: '+(o.future_points??0),'入场触达: '+(o.entry_touched?'是':'否')+' / 假设成交价 '+(o.entry_price_assumed||'--'),'止损触发: '+(o.stop_hit?'是':'否'),'止盈命中: '+JSON.stringify(o.take_profit_hits||[]),'MFE/MAE: '+fmt(o.mfe_pct,3)+'% / '+fmt(o.mae_pct,3)+'%','5m/15m/20m: '+JSON.stringify(o.returns||{{}}),'结论: '+(v.direction_result||'--')+'，'+(v.entry_result||'--')+'，'+(v.risk_result||'--'),'备注: '+((v.notes||[]).join('；')||'无')].join('\\n');}}catch(error){{if(box)box.textContent='回放失败：'+error;}}finally{{historyTestBtn.disabled=false;}}}});}}
	let virtualTick=0;
let configuredMonitorInsts={json.dumps(selected_instruments, ensure_ascii=False)};
let monitorInst={json.dumps(monitor_initial, ensure_ascii=False)}, monitorPayload=null, monitorLiveMode=false;
let monitorSeriesByInst={{}}, monitorLastTickerAt=0;
let virtualSeriesByInst={{}};
let monitorViewStart=0, monitorViewEnd=1;
let monitorVisiblePoints=[], monitorPlotPoints=[], monitorSelectedKey='', monitorLatestPoint=null;
let monitorYZoom=1, monitorYPan=0, monitorYRange=1, monitorDrag=null;
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
function drawChart(id,points,priceBox,metaBox,loading){{const c=document.getElementById(id);if(!c||!points||points.length<1)return;if(points.length===1)points=[points[0],{{time:points[0].time,price:points[0].price}}];monitorLatestPoint=points[points.length-1];points=visiblePoints(points);monitorVisiblePoints=points;updateChartFooter(points);if(loading)loading.style.display='none';const d=window.devicePixelRatio||1,r=c.getBoundingClientRect();c.width=r.width*d;c.height=r.height*d;const x=c.getContext('2d');x.setTransform(d,0,0,d,0,0);const W=r.width,H=r.height,p={{l:34,r:74,t:18,b:58}},cw=W-p.l-p.r,ch=H-p.t-p.b,prices=points.map(q=>q.price);let mn=Math.min(...prices),mx=Math.max(...prices);const rawCenter=(mn+mx)/2,baseRg=Math.max(.01,(mx-mn)*1.16),center=rawCenter+monitorYPan,rg=baseRg/monitorYZoom;monitorYRange=rg;mn=center-rg/2;mx=center+rg/2;monitorPlotPoints=[];x.clearRect(0,0,W,H);x.strokeStyle='rgba(255,255,255,.12)';for(let i=0;i<=4;i++){{const y=p.t+ch*i/4;x.beginPath();x.moveTo(p.l,y);x.lineTo(W-p.r,y);x.stroke();}}x.beginPath();points.forEach((q,i)=>{{const px=p.l+cw*i/(points.length-1),py=p.t+ch-((q.price-mn)/rg)*ch;monitorPlotPoints.push({{x:px,y:py,point:q}});if(i===0)x.moveTo(px,py);else x.lineTo(px,py);}});const up=points[points.length-1].price>=points[0].price;x.strokeStyle=up?'#22c55e':'#fb7185';x.lineWidth=2;x.stroke();x.fillStyle=up?'rgba(34,197,94,.08)':'rgba(251,113,133,.10)';x.lineTo(W-p.r,H-p.b);x.lineTo(p.l,H-p.b);x.closePath();x.fill();drawTimeAxis(x,W,H,p,points,cw);drawPriceAxis(x,W,H,p,mn,mx);if(monitorSelectedKey){{const hit=monitorPlotPoints.find(o=>(o.point.time||'')===monitorSelectedKey);if(hit){{x.strokeStyle='rgba(255,255,255,.62)';x.setLineDash([4,5]);x.beginPath();x.moveTo(hit.x,p.t);x.lineTo(hit.x,H-p.b);x.stroke();x.beginPath();x.moveTo(p.l,hit.y);x.lineTo(W-p.r,hit.y);x.stroke();x.setLineDash([]);x.fillStyle='#60a5fa';x.beginPath();x.arc(hit.x,hit.y,5,0,7);x.fill();updateSnapshotPanel(hit.point,'选中点');}}else{{monitorSelectedKey='';updateSnapshotPanel(monitorLatestPoint,'最新快照');}}}}if(priceBox){{const first=points[0].price,last=points[points.length-1].price,chg=last-first,pct=first?chg/first*100:0;priceBox.classList.toggle('up',chg>=0);priceBox.classList.toggle('down',chg<0);priceBox.innerHTML='<strong>'+last.toFixed(2)+'</strong><span>'+(chg>=0?'+':'')+chg.toFixed(2)+' / '+(pct>=0?'+':'')+pct.toFixed(2)+'%</span>';}}if(!monitorSelectedKey)updateSnapshotPanel(monitorLatestPoint,'最新快照');if(metaBox)metaBox.textContent='更新：'+new Date().toLocaleTimeString();}}
function drawVirtualMonitor(){{if(!monitorInst){{clearChartMessage('请先在配置页选择监控币种');return;}}drawChart('monitorChart',nextVirtualSeries(),document.getElementById('monitorPrice'),document.getElementById('monitorMeta'),document.getElementById('monitorLoading'));document.getElementById('monitorTitle').textContent=monitorInst+' 虚拟走势';const m=document.getElementById('monitorMeta');if(m)m.textContent='虚拟行情预览 · 点击开始监控后切换真实数据';}}
function setMonitorButtonState(state,text){{const btn=document.getElementById('monitorToggleBtn'),meta=document.getElementById('monitorMeta');if(!btn)return;btn.classList.remove('is-running','is-starting');btn.disabled=false;if(state==='starting'){{btn.classList.add('is-starting');btn.textContent='启动中...';btn.disabled=true;if(meta)meta.textContent=text||'正在启动监控进程...';}}else if(state==='running'){{btn.classList.add('is-running');btn.textContent='停止监控';if(meta&&text)meta.textContent=text;}}else if(state==='stopping'){{btn.classList.add('is-starting');btn.textContent='停止中...';btn.disabled=true;if(meta)meta.textContent=text||'正在停止监控进程...';}}else{{btn.textContent='开始监控';if(meta&&text)meta.textContent=text;}}}}
async function syncMonitorStatus(){{try{{const r=await fetch('/api/status',{{cache:'no-store'}}),p=await r.json();setMonitorButtonState(p.running?'running':'stopped',p.text||'');return p;}}catch(e){{return null;}}}}
function showRealtimeWaiting(clearChart){{if(!monitorInst){{clearChartMessage('请先在配置页选择监控币种');return;}}monitorLiveMode=true;const c=document.getElementById('monitorChart'),l=document.getElementById('monitorLoading'),m=document.getElementById('monitorMeta'),t=document.getElementById('monitorTitle'),p=document.getElementById('monitorPrice');if(clearChart&&c){{const r=c.getBoundingClientRect(),x=c.getContext('2d');c.width=Math.max(1,r.width);c.height=Math.max(1,r.height);x.clearRect(0,0,r.width,r.height);updateChartFooter([]);}}if(l){{l.style.display=clearChart?'grid':'none';l.textContent='监控已启动，正在等待真实价格数据...';}}if(m)m.textContent=clearChart?'实时监控已启动 · 等待第一条价格数据':'已获取最新价 · 等待完整分析数据';if(t)t.textContent=monitorInst+' 实时走势';if(clearChart&&p)p.innerHTML='<strong>--</strong><span>等待真实数据</span>';}}
async function fetchMonitor(){{if(!monitorInst){{clearChartMessage('请先在配置页选择监控币种');return null;}}try{{const r=await fetch('/api/monitor-data?inst_id='+encodeURIComponent(monitorInst),{{cache:'no-store'}}),p=await r.json();if(!r.ok||p.ok===false){{clearChartMessage(p.error||'当前币种未配置，不能读取监控数据');return p;}}if(!p.running){{monitorLiveMode=false;monitorSeriesByInst[monitorInst]=[];drawVirtualMonitor();return p;}}monitorLiveMode=true;if(p.points&&p.points.length>0){{setMonitorSeries(p.points);const hasChart=p.source==='web-chart'||p.source==='signal-monitor-chart';document.getElementById('monitorTitle').textContent=monitorInst+(hasChart?' 1m K线走势':' 实时走势');const meta=p.source==='web-chart'?'Web获取1m K线 · 指标读取okx_signal_monitor.py日志':(p.source==='signal-monitor-chart'?'okx_signal_monitor.py 1m K线兜底 · 指标读取日志':'读取okx_signal_monitor.py实时日志 · 等待K线');drawMonitorSeries(meta+' · '+new Date().toLocaleTimeString());}}else if(!drawMonitorSeries('保留最近走势 · 等待K线/日志')){{showRealtimeWaiting(false);}}return p;}}catch(e){{if(monitorLiveMode){{if(!drawMonitorSeries('保留最近走势 · 等待K线/日志'))showRealtimeWaiting(false);}}else drawVirtualMonitor();return null;}}}}
function sleep(ms){{return new Promise(resolve=>setTimeout(resolve,ms));}}
async function bootstrapMonitorChart(){{monitorLastTickerAt=0;for(let i=0;i<16;i++){{const payload=await fetchMonitor();if(payload&&(payload.source==='web-chart'||payload.source==='signal-monitor-chart')&&payload.points&&payload.points.length>0)break;await sleep(i<4?500:1000);}}}}
function redrawMonitorCached(){{if(drawMonitorSeries())return;const series=virtualSeriesByInst[monitorInst]||[];if(series.length){{drawChart('monitorChart',series,document.getElementById('monitorPrice'),document.getElementById('monitorMeta'),document.getElementById('monitorLoading'));document.getElementById('monitorTitle').textContent=monitorInst+' 虚拟走势';}}else{{drawVirtualMonitor();}}}}
bindMonitorTabs();
const monitorToggleBtn=document.getElementById('monitorToggleBtn');
if(monitorToggleBtn){{monitorToggleBtn.addEventListener('click',async()=>{{const status=await syncMonitorStatus();if(status&&status.running){{setMonitorButtonState('stopping','正在停止监控进程...');try{{await fetch('/stop#monitor',{{cache:'no-store'}});}}catch(e){{}}monitorLiveMode=false;monitorViewStart=0;monitorViewEnd=1;monitorYZoom=1;monitorYPan=0;monitorSeriesByInst[monitorInst]=[];setMonitorButtonState('stopped','监控已停止，当前显示虚拟行情');drawVirtualMonitor();return;}}if(!monitorInst){{clearChartMessage('请先在配置页选择监控币种');return;}}setMonitorButtonState('starting','正在保存配置并启动监控...');monitorYPan=0;showRealtimeWaiting(true);try{{await autoSaveConfig();await fetch('/start#monitor',{{cache:'no-store'}});await syncMonitorStatus();bootstrapMonitorChart();}}catch(e){{setMonitorButtonState('stopped','启动监控失败');const l=document.getElementById('monitorLoading');if(l)l.textContent='启动监控失败：'+e;}}}});}}
let logCleared=false;async function refreshLogs(force){{const box=document.getElementById('logWindow');if(!box)return;if(logCleared&&!force)return;try{{const r=await fetch('/api/logs',{{cache:'no-store'}}),p=await r.json();logCleared=false;box.value=p.text||'暂无日志。';box.scrollTop=box.scrollHeight;}}catch(e){{box.value='日志读取失败：'+e;}}}}
const refreshLogBtn = document.getElementById('refreshLogBtn');
if (refreshLogBtn) refreshLogBtn.addEventListener('click', function() {{ refreshLogs(true); }});
const clearLogBtn = document.getElementById('clearLogBtn');
if (clearLogBtn) {{
  clearLogBtn.addEventListener('click', function() {{
    const box = document.getElementById('logWindow');
    if (box) box.value = '';
    logCleared = true;
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
const saveLogBtn = document.getElementById('saveLogBtn');
if (saveLogBtn) {{
  saveLogBtn.addEventListener('click', async function() {{
    try {{
      const response = await fetch('/api/logs', {{ cache: 'no-store' }});
      const payload = await response.json();
      const text = payload.text || '';
      const hint = document.getElementById('saveLogHint');
      if (window.showSaveFilePicker) {{
        const handle = await showSaveFilePicker({{
          suggestedName: 'okx_signal_analysis.jsonl',
          types: [{{ description: '日志文件', accept: {{ 'text/plain': ['.log', '.txt'] }} }}]
        }});
        const writable = await handle.createWritable();
        await writable.write(text);
        await writable.close();
        if (hint) hint.textContent = '已另存为：' + (handle.name || '用户选择的日志文件') + '。浏览器安全限制不会暴露完整本地路径。';
      }} else {{
        const blob = new Blob([text], {{ type: 'text/plain;charset=utf-8' }});
        const url = URL.createObjectURL(blob);
        const link = document.createElement('a');
        link.href = url;
        link.download = 'okx_signal_analysis.jsonl';
        link.click();
        URL.revokeObjectURL(url);
        if (hint) hint.textContent = '已触发下载：okx_signal_analysis.jsonl。';
      }}
    }} catch (error) {{
      alert('保存日志失败：' + error);
    }}
  }});
}}
const monitorCanvas=document.getElementById('monitorChart');
if(monitorCanvas){{monitorCanvas.addEventListener('wheel',function(event){{event.preventDefault();const rect=monitorCanvas.getBoundingClientRect(),focus=Math.min(1,Math.max(0,(event.clientX-rect.left)/Math.max(1,rect.width))),span=monitorViewEnd-monitorViewStart,zoom=(event.deltaY<0?0.82:1.22),newSpan=Math.min(1,Math.max(.06,span*zoom)),center=monitorViewStart+span*focus;let ns=center-newSpan*focus,ne=ns+newSpan;if(ns<0){{ne-=ns;ns=0;}}if(ne>1){{ns-=ne-1;ne=1;}}monitorViewStart=Math.max(0,ns);monitorViewEnd=Math.min(1,ne);monitorYZoom=Math.max(.45,Math.min(10,monitorYZoom*(event.deltaY<0?1.14:.88)));redrawMonitorCached();}},{{passive:false}});monitorCanvas.addEventListener('mousedown',function(event){{monitorDrag={{x:event.clientX,y:event.clientY,start:monitorViewStart,end:monitorViewEnd,yPan:monitorYPan,yRange:monitorYRange,moved:false}};monitorCanvas.classList.add('dragging');}});window.addEventListener('mousemove',function(event){{if(!monitorDrag)return;const rect=monitorCanvas.getBoundingClientRect(),span=monitorDrag.end-monitorDrag.start,dx=(event.clientX-monitorDrag.x)/Math.max(1,rect.width),dy=(event.clientY-monitorDrag.y)/Math.max(1,rect.height);if(Math.abs(event.clientX-monitorDrag.x)>3||Math.abs(event.clientY-monitorDrag.y)>3)monitorDrag.moved=true;let ns=monitorDrag.start-dx*span,ne=monitorDrag.end-dx*span;if(ns<0){{ne-=ns;ns=0;}}if(ne>1){{ns-=ne-1;ne=1;}}monitorViewStart=Math.max(0,ns);monitorViewEnd=Math.min(1,ne);monitorYPan=monitorDrag.yPan+dy*Math.max(.01,monitorDrag.yRange||monitorYRange);redrawMonitorCached();}});window.addEventListener('mouseup',function(){{if(monitorDrag){{setTimeout(function(){{monitorDrag=null;}},0);}}monitorCanvas.classList.remove('dragging');}});function selectNearestPoint(event,strict){{if(!monitorPlotPoints.length)return;const rect=monitorCanvas.getBoundingClientRect(),x=event.clientX-rect.left,y=event.clientY-rect.top;let best=null,bestDist=Infinity;monitorPlotPoints.forEach(o=>{{const dx=o.x-x,dy=o.y-y,dist=Math.sqrt(dx*dx+dy*dy);if(dist<bestDist){{best=o;bestDist=dist;}}}});if(best&&bestDist<(strict?42:34)){{monitorSelectedKey=best.point.time||'';updateSnapshotPanel(best.point,'选中点');redrawMonitorCached();}}else if(!strict){{monitorSelectedKey='';updateSnapshotPanel(monitorLatestPoint,'最新快照');redrawMonitorCached();}}}}monitorCanvas.addEventListener('click',function(event){{if(monitorDrag&&monitorDrag.moved)return;selectNearestPoint(event,false);}});monitorCanvas.addEventListener('dblclick',function(event){{selectNearestPoint(event,true);}});}}
window.addEventListener('resize',()=>{{if(currentPage()==='monitor')redrawMonitorCached();}});
setInterval(async()=>{{if(currentPage()!=='monitor')return;const status=await syncMonitorStatus();if(status&&status.running){{monitorLiveMode=true;const now=Date.now();if(now-monitorLastTickerAt>=5000){{monitorLastTickerAt=now;fetchMonitor();}}}}else{{monitorLiveMode=false;drawVirtualMonitor();}}}},1000);
setInterval(()=>{{if(currentPage()==='logs')refreshLogs(false);}},3000);window.addEventListener('hashchange',()=>showPage(currentPage()));syncMonitorStatus();showPage(currentPage());
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
            note = "" if env_path == ENV_FILE else f" 密钥已保存到用户目录：{env_path}"
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
