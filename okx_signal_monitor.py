#!/usr/bin/env python3
"""
OKX AI short-term trading assistant V1.

Scope:
    - Monitor OKX USDT perpetual swaps (default BTC/ETH; configurable via Web or --inst-ids).
    - Provide analysis and suggestions only.
    - No auto order, no martingale, no grid.

Optional AI:
    pip install openai
    export OPENAI_API_KEY="..."
    export AI_MODEL="gpt-5.5"

Optional push:
    export WECHAT_SEND_KEY="..."
    # Server酱 SendKey，可在 Web 控制面板配置

Production tuning:
    export RETRY_TIMES=3
    export PUSH_COOLDOWN_SECONDS=900
    export LOG_MAX_BYTES=524288000
    export LOG_TOTAL_MAX_BYTES=1572864000
    export VOLUME_MULTIPLIER=2.0
    export OI_CHANGE_PCT_15M=5.0
    export AI_REQUEST_TIMEOUT=30
    export AI_CIRCUIT_FAIL_THRESHOLD=3
    export AI_CIRCUIT_COOLDOWN_SECONDS=120
    export AI_PROBE_INTERVAL_SECONDS=60
    export AI_ABNORMAL_ALERT_SECONDS=300
    export AI_ABNORMAL_ALERT_COOLDOWN_SECONDS=3600
    export AI_CALL_MIN_INTERVAL_SECONDS=60
    export CONSOLE_VERBOSE=1
"""

import argparse
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Set, Tuple

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from runtime_identity import format_runtime_identity
from monitor_config_summary import build_effective_config_lines, build_log_config_snapshot

try:
    import okx.MarketData as MarketData
    import okx.PublicData as PublicData
except ImportError:
    MarketData = None
    PublicData = None

# 默认快捷合约；Web 控制台与 --inst-ids 可配置其他 OKX USDT 永续。
PRESET_INSTRUMENTS = ("BTC-USDT-SWAP", "ETH-USDT-SWAP")
SUPPORTED_INSTRUMENTS = PRESET_INSTRUMENTS
INST_ID_PATTERN = re.compile(r"^[A-Z0-9]+-[A-Z0-9]+-SWAP$")

# 多周期K线用于判断短线趋势结构：
# 1m/3m负责入场节奏，5m/15m负责短线方向，1H/4H负责上级环境。
# 这里保留1m、5m、15m、1H这些旧字段，同时新增3m和4H，保证历史日志、AI prompt和Web展示兼容。
BAR_CHANNELS = ("1m", "3m", "5m", "15m", "1H", "4H", "1D", "1W")
KLINE_LIMIT = 200
DEFAULT_INTERVAL_SECONDS = 5
DEFAULT_PUSH_SCORE = 80
DEFAULT_AI_MODEL = "gpt-5.5"
OKX_BASE_URL = "https://www.okx.com"
DEFAULT_RETRY_TIMES = 3
DEFAULT_RETRY_BACKOFF_SECONDS = 1.5
DEFAULT_OKX_CIRCUIT_FAIL_THRESHOLD = 5
DEFAULT_OKX_CIRCUIT_COOLDOWN_SECONDS = 90
DEFAULT_REPLAY_DATASET_MAX_BYTES = 500 * 1024 * 1024
DEFAULT_REPLAY_DATASET_TOTAL_MAX_BYTES = 1500 * 1024 * 1024
DEFAULT_AI_REQUEST_TIMEOUT = 30.0
DEFAULT_AI_PROBE_TIMEOUT = 10.0
DEFAULT_AI_CIRCUIT_FAIL_THRESHOLD = 3
DEFAULT_AI_CIRCUIT_COOLDOWN_SECONDS = 120
DEFAULT_AI_PROBE_INTERVAL_SECONDS = 60
DEFAULT_AI_ABNORMAL_ALERT_SECONDS = 300
DEFAULT_AI_ABNORMAL_ALERT_COOLDOWN_SECONDS = 3600
DEFAULT_AI_RATE_LIMIT_BACKOFF_SECONDS = 30.0
DEFAULT_PUSH_COOLDOWN_SECONDS = 900
DEFAULT_SPIKE_PUSH_COOLDOWN_SECONDS = 900
DEFAULT_WATCH_PUSH_COOLDOWN_SECONDS = 900
DEFAULT_REVERSE_TRADE_COOLDOWN_SECONDS = 300
DEFAULT_FORECAST_PUSH_COOLDOWN_SECONDS = 1800
DEFAULT_WECHAT_MIN_INTERVAL_SECONDS = 600
WECHAT_PUSH_KIND_PRIORITY = ("trade", "spike", "forecast")
WECHAT_WATCH_AI_MIN_MARGIN = 5
WECHAT_SPIKE_LOCAL_MIN_MARGIN = 10
WECHAT_FORECAST_MIN_MARGIN = 7
WECHAT_TRADE_MIN_MARGIN = 3
WECHAT_FORECAST_HIGH_PROB_MARGIN = 12
CONFIDENCE_HUG_MARGIN = 3
DEFAULT_AI_CALL_MIN_INTERVAL_SECONDS = 60
# 500MB；12h 写入量约 177MB，留足余量避免过早轮转导致 Web 图表/压测丢数据。
DEFAULT_LOG_MAX_BYTES = 500 * 1024 * 1024
# 默认总容量 1.5GB（约 3 个 500MB 分卷）；超过后删除最旧分卷。
DEFAULT_LOG_TOTAL_MAX_BYTES = 1500 * 1024 * 1024
MIN_LOG_MAX_BYTES = 50 * 1024 * 1024
WECHAT_PUSH_MAX_DESP = 28000
TRADE_TRIGGER_SIGNALS = frozenset(
    {"volume_spike", "structure_break", "oi_change", "order_book_imbalance", "macd_momentum_change"}
)
WATCH_TRIGGER_SIGNALS = frozenset(
    {"funding_hot", "rsi_extreme", "rsi_divergence", "boll_squeeze", "long_short_extreme", "funding_fast_change"}
)
WARMUP_MINUTES = 15
HISTORY_RETENTION_MINUTES = 1440
METRIC_SAMPLE_INTERVAL_SECONDS = 60

# 资金/OI/动态阈值按约1分钟保存一个有效样本，180分钟约180个点。
# maxlen多留余量，兼容用户把轮询间隔调低、未来把部分指标改为更高频采样的情况。
METRIC_HISTORY_MAXLEN = HISTORY_RETENTION_MINUTES * 3
MARKET_HISTORY_RESTORE_MAX_BYTES = 48 * 1024 * 1024
SNAPSHOT_PARALLEL_WORKERS = 4

# 不同类型数据使用不同缓存时间，降低OKX REST请求量，减少限频风险。
# ticker仍保持接近5秒刷新；K线、OI、资金费率、多空比可以低频更新。
CACHE_TTL_SECONDS = {
    "ticker": 5,
    "candles": 15,
    "open_interest": 60,
    "funding_rate": 60,
    "long_short_ratio": 60,
    "order_book": 5,
}

# 日志使用JSON Lines格式，一行一条分析记录，便于后续导入数据库或做回测统计。
# 默认保存到build/runtime_logs目录，便于把运行时日志和工程源码分离。
LOG_DIR = Path(__file__).resolve().parent / "build" / "runtime_logs"
LOG_FILE = LOG_DIR / "okx_signal_analysis.jsonl"
REPLAY_DATASET_FILE = LOG_DIR / "replay_dataset.jsonl"
REPLAY_LOG_FILE = LOG_DIR / "replay_analysis.jsonl"
REPLAY_DATASET_VERSION = "1.1"
SIGNAL_PERFORMANCE_FILE = LOG_DIR / "signal_performance.jsonl"
SIGNAL_PERFORMANCE_MAX_BYTES = 10 * 1024 * 1024
SIGNAL_PERFORMANCE_LOAD_BYTES = 2 * 1024 * 1024
AI_TOKEN_STATS_FILE = LOG_DIR / "ai_session_tokens.json"
PAPER_INITIAL_CAPITAL = 10000.0
PAPER_ACCOUNT_FILE = LOG_DIR / "paper_account.json"
CALIBRATION_STATE_FILE = LOG_DIR / "calibration_state.json"
FORECAST_PERFORMANCE_FILE = LOG_DIR / "forecast_performance.jsonl"
DECISION_CALIBRATION_FILE = LOG_DIR / "decision_calibration.jsonl"
CALIBRATION_PERFORMANCE_MAX_BYTES = 10 * 1024 * 1024


def default_calibration_state() -> Dict[str, Any]:
    return {"updated_at": "", "buckets": {}, "pending_forecasts": []}


def load_calibration_state(path: Path = CALIBRATION_STATE_FILE) -> Dict[str, Any]:
    if not path.exists():
        return default_calibration_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return default_calibration_state()
    if not isinstance(data, dict):
        return default_calibration_state()
    buckets = data.get("buckets")
    if not isinstance(buckets, dict):
        buckets = {}
    pending_forecasts = data.get("pending_forecasts")
    if not isinstance(pending_forecasts, list):
        pending_forecasts = []
    pending_forecasts = [
        item
        for item in pending_forecasts[-400:]
        if isinstance(item, dict) and int(item.get("forecast_version", 0) or 0) == 2
    ]
    return {
        "updated_at": str(data.get("updated_at") or ""),
        "buckets": buckets,
        "pending_forecasts": pending_forecasts,
    }


def save_calibration_state(state: Dict[str, Any], path: Path = CALIBRATION_STATE_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = default_calibration_state()
    payload["updated_at"] = now_text()
    buckets = state.get("buckets") if isinstance(state.get("buckets"), dict) else {}
    payload["buckets"] = buckets
    pending_forecasts = state.get("pending_forecasts")
    payload["pending_forecasts"] = (
        [item for item in pending_forecasts[-400:] if isinstance(item, dict)]
        if isinstance(pending_forecasts, list)
        else []
    )
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def calibration_bucket_stats(buckets: Dict[str, Any], key: str) -> Dict[str, Any]:
    stats = buckets.get(key) if isinstance(buckets.get(key), dict) else {}
    total = max(0, int(stats.get("total", 0) or 0))
    hits = max(0, int(stats.get("hits", 0) or 0))
    return {
        "total": total,
        "hits": hits,
        "structure_hits": max(0, int(stats.get("structure_hits", 0) or 0)),
        "partial_structure_hits": max(0, int(stats.get("partial_structure_hits", 0) or 0)),
        "price_hits": max(0, int(stats.get("price_hits", 0) or 0)),
        "sum_move_pct": float(stats.get("sum_move_pct", 0.0) or 0.0),
        "sum_brier": float(stats.get("sum_brier", 0.0) or 0.0),
        "disabled": bool(stats.get("disabled", False)),
        "hit_rate": safe_div(hits, total, 0.0),
        "brier_score": safe_div(float(stats.get("sum_brier", 0.0) or 0.0), total, 0.0),
    }


def append_calibration_performance(path: Path, item: Dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size >= CALIBRATION_PERFORMANCE_MAX_BYTES:
            backup = path.with_suffix(path.suffix + ".1")
            if backup.exists():
                backup.unlink()
            path.replace(backup)
        with path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(item, ensure_ascii=False) + "\n")
    except OSError as exc:
        console_debug(f"calibration performance log failed: {exc}")


def default_ai_token_stats(started_at: str = "") -> Dict[str, Any]:
    return {
        "started_at": started_at or now_text(),
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "calls": 0,
    }


def load_ai_token_stats(path: Path = AI_TOKEN_STATS_FILE) -> Dict[str, Any]:
    if not path.exists():
        return default_ai_token_stats()
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return default_ai_token_stats()
    if not isinstance(data, dict):
        return default_ai_token_stats()
    stats = default_ai_token_stats(str(data.get("started_at") or ""))
    for key in ("prompt_tokens", "completion_tokens", "total_tokens", "calls"):
        try:
            stats[key] = max(0, int(data.get(key, 0)))
        except (TypeError, ValueError):
            stats[key] = 0
    if stats["total_tokens"] <= 0:
        stats["total_tokens"] = stats["prompt_tokens"] + stats["completion_tokens"]
    return stats


def save_ai_token_stats(stats: Dict[str, Any], path: Path = AI_TOKEN_STATS_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = default_ai_token_stats(str(stats.get("started_at") or ""))
    for key in ("prompt_tokens", "completion_tokens", "total_tokens", "calls"):
        try:
            payload[key] = max(0, int(stats.get(key, 0)))
        except (TypeError, ValueError):
            payload[key] = 0
    if payload["total_tokens"] <= 0:
        payload["total_tokens"] = payload["prompt_tokens"] + payload["completion_tokens"]
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def reset_ai_token_stats(path: Path = AI_TOKEN_STATS_FILE, started_at: str = "") -> Dict[str, Any]:
    stats = default_ai_token_stats(started_at or now_text())
    save_ai_token_stats(stats, path)
    return stats


def accumulate_ai_token_stats(usage: Optional[Dict[str, int]], path: Path = AI_TOKEN_STATS_FILE) -> Dict[str, Any]:
    if not usage:
        return load_ai_token_stats(path)
    prompt = max(0, int(usage.get("prompt_tokens") or 0))
    completion = max(0, int(usage.get("completion_tokens") or 0))
    total = max(0, int(usage.get("total_tokens") or 0))
    if total <= 0:
        total = prompt + completion
    if total <= 0 and prompt <= 0 and completion <= 0:
        return load_ai_token_stats(path)
    stats = load_ai_token_stats(path)
    stats["prompt_tokens"] = int(stats.get("prompt_tokens", 0)) + prompt
    stats["completion_tokens"] = int(stats.get("completion_tokens", 0)) + completion
    stats["total_tokens"] = int(stats.get("total_tokens", 0)) + total
    stats["calls"] = int(stats.get("calls", 0)) + 1
    save_ai_token_stats(stats, path)
    return stats


def analysis_log_backup_path(active_path: Path, index: int) -> Path:
    return active_path.with_suffix(active_path.suffix + f".{index}")


def list_analysis_log_segments(active_path: Path) -> List[Path]:
    """Return log segments oldest-first: [.N, ..., .1, active]."""
    backups: List[Path] = []
    index = 1
    while True:
        backup = analysis_log_backup_path(active_path, index)
        if backup.exists():
            backups.append(backup)
            index += 1
        else:
            break
    backups.reverse()
    if active_path.exists():
        return backups + [active_path]
    return backups


def analysis_log_total_bytes(active_path: Path) -> int:
    total = 0
    for path in list_analysis_log_segments(active_path):
        try:
            total += path.stat().st_size
        except OSError:
            continue
    return total


def _shift_analysis_log_backups(active_path: Path) -> None:
    index = 1
    while analysis_log_backup_path(active_path, index).exists():
        index += 1
    for slot in range(index - 1, 0, -1):
        src = analysis_log_backup_path(active_path, slot)
        dst = analysis_log_backup_path(active_path, slot + 1)
        if dst.exists():
            dst.unlink()
        src.replace(dst)


def _rotate_analysis_log_active(active_path: Path) -> None:
    active_path.parent.mkdir(parents=True, exist_ok=True)
    backup1 = analysis_log_backup_path(active_path, 1)
    if backup1.exists():
        _shift_analysis_log_backups(active_path)
    if active_path.exists():
        active_path.replace(backup1)


def _prune_analysis_log_segments(active_path: Path, log_total_max_bytes: int) -> None:
    while analysis_log_total_bytes(active_path) > log_total_max_bytes:
        segments = list_analysis_log_segments(active_path)
        if not segments:
            return
        oldest = segments[0]
        if len(segments) == 1 and oldest.resolve() == active_path.resolve():
            return
        try:
            oldest.unlink()
        except OSError as exc:
            console_debug(f"log prune failed: {exc}")
            return


def rotate_analysis_log_if_needed(
    active_path: Path,
    log_max_bytes: int,
    log_total_max_bytes: int,
) -> None:
    log_max_bytes = max(int(log_max_bytes), MIN_LOG_MAX_BYTES)
    log_total_max_bytes = max(int(log_total_max_bytes), log_max_bytes)
    try:
        active_path.parent.mkdir(parents=True, exist_ok=True)
        if active_path.exists() and active_path.stat().st_size >= log_max_bytes:
            _rotate_analysis_log_active(active_path)
        _prune_analysis_log_segments(active_path, log_total_max_bytes)
    except Exception as exc:
        console_debug(f"log rotation failed: {exc}")


def replay_dataset_backup_path(active_path: Path, index: int) -> Path:
    return active_path.with_name(f"{active_path.name}.{index}")


def list_replay_dataset_segments(active_path: Path) -> List[Path]:
    backups: List[Path] = []
    index = 1
    while True:
        backup = replay_dataset_backup_path(active_path, index)
        if backup.exists():
            backups.append(backup)
            index += 1
        else:
            break
    backups.reverse()
    if active_path.exists():
        return backups + [active_path]
    return backups


def replay_dataset_total_bytes(active_path: Path) -> int:
    total = 0
    for path in list_replay_dataset_segments(active_path):
        try:
            total += path.stat().st_size
        except OSError:
            continue
    return total


def _shift_replay_dataset_backups(active_path: Path) -> None:
    index = 1
    while replay_dataset_backup_path(active_path, index).exists():
        index += 1
    for slot in range(index - 1, 0, -1):
        src = replay_dataset_backup_path(active_path, slot)
        dst = replay_dataset_backup_path(active_path, slot + 1)
        if dst.exists():
            dst.unlink()
        src.replace(dst)


def rotate_replay_dataset_if_needed(
    active_path: Path,
    max_bytes: int = DEFAULT_REPLAY_DATASET_MAX_BYTES,
    total_max_bytes: int = DEFAULT_REPLAY_DATASET_TOTAL_MAX_BYTES,
) -> None:
    max_bytes = max(int(max_bytes), 50 * 1024 * 1024)
    total_max_bytes = max(int(total_max_bytes), max_bytes)
    try:
        active_path.parent.mkdir(parents=True, exist_ok=True)
        if active_path.exists() and active_path.stat().st_size >= max_bytes:
            backup1 = replay_dataset_backup_path(active_path, 1)
            if backup1.exists():
                _shift_replay_dataset_backups(active_path)
            active_path.replace(backup1)
        while replay_dataset_total_bytes(active_path) > total_max_bytes:
            segments = list_replay_dataset_segments(active_path)
            if not segments:
                return
            oldest = segments[0]
            if len(segments) == 1 and oldest.resolve() == active_path.resolve():
                return
            oldest.unlink()
    except Exception as exc:
        console_debug(f"replay dataset rotation failed: {exc}")


def tail_analysis_log_text(active_path: Path, max_bytes: int) -> str:
    if max_bytes <= 0 or not active_path.parent.exists():
        return ""
    segments = list_analysis_log_segments(active_path)
    if not segments:
        return ""
    remaining = max_bytes
    chunks: List[str] = []
    for path in reversed(segments):
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size <= remaining:
            chunks.append(path.read_text(encoding="utf-8", errors="replace"))
            remaining -= size
            if remaining <= 0:
                break
            continue
        with path.open("rb") as file:
            file.seek(size - remaining)
            chunks.append(file.read().decode("utf-8", errors="replace"))
        remaining = 0
        break
    chunks.reverse()
    return "".join(chunks)


def iter_analysis_log_lines(
    active_path: Path,
    *,
    read_full: bool = False,
    max_tail_bytes: int = None,
):
    if max_tail_bytes is None:
        max_tail_bytes = DEFAULT_LOG_TOTAL_MAX_BYTES
    if not read_full:
        for line in tail_analysis_log_text(active_path, max_tail_bytes).splitlines():
            stripped = line.strip()
            if stripped:
                yield stripped
        return
    for path in list_analysis_log_segments(active_path):
        try:
            with path.open("r", encoding="utf-8-sig") as file:
                for line in file:
                    stripped = line.strip()
                    if stripped:
                        yield stripped
        except OSError:
            continue


def console_verbose_enabled() -> bool:
    return os.getenv("CONSOLE_VERBOSE", "").strip().lower() in {"1", "true", "yes", "on"}


def console_debug(message: str) -> None:
    if console_verbose_enabled():
        print(message, flush=True)


def console_info(message: str) -> None:
    print(message, flush=True)


def console_warn(message: str) -> None:
    print(message, flush=True)


def analysis_console_excerpt(analysis: Dict[str, Any], score: Dict[str, Any], limit: int = 96) -> str:
    content = analysis.get("content")
    if isinstance(content, str):
        text = " ".join(content.split())
        if text:
            return text[:limit] + ("..." if len(text) > limit else "")
    if isinstance(content, dict):
        direction = content.get("direction") or score.get("direction", "-")
        entry = content.get("entry")
        parts = [f"dir={direction}"]
        if entry and entry not in ("", "-"):
            parts.append(f"entry={entry}")
        return " ".join(parts)
    provider = analysis.get("provider")
    return str(provider) if provider else "-"


DECISION_SOURCE_LABELS = {
    "ai": "AI前瞻",
    "local": "本地筛查",
    "local_screening": "本地筛查",
    "local_fallback": "本地兜底",
    "structure_forecast": "结构演变",
}


def decision_source_prefix(final_decision: Dict[str, Any]) -> str:
    source = str(final_decision.get("decision_source", "local") or "local")
    label = DECISION_SOURCE_LABELS.get(source, source)
    return f"【{label}】"


def format_ai_call_status(trigger: Dict[str, Any], ai_enabled: bool) -> str:
    level = str(trigger.get("level", "L0"))
    reasons = trigger.get("reasons") if isinstance(trigger.get("reasons"), list) else []
    reason_text = ",".join(str(item) for item in reasons[:4]) or "-"

    if trigger.get("ai_invoked"):
        return f"trigger={level} ai=已调用 原因={reason_text}"

    if trigger.get("should_call_ai"):
        return f"trigger={level} ai=待调用 原因={reason_text}"

    if level in ("L0", "L1"):
        return f"trigger={level} ai=未调用({level}不调AI) 信号={reason_text}"

    if not ai_enabled:
        return f"trigger={level} ai=未调用(ai_disabled) 触发条件={reason_text}"

    if level == "L2":
        return f"trigger={level} ai=未调用(同指纹冷却) 触发条件={reason_text}"

    return f"trigger={level} ai=未调用 触发条件={reason_text}"


def _clip_console_text(text: Any, limit: int = 160) -> str:
    cleaned = " ".join(str(text or "").split())
    if not cleaned:
        return ""
    return cleaned[:limit] + ("..." if len(cleaned) > limit else "")


def format_ai_analysis_lines(
    analysis: Dict[str, Any],
    final_decision: Dict[str, Any],
) -> List[str]:
    lines: List[str] = []
    if not analysis:
        return lines

    source = str(final_decision.get("decision_source", "") or "")
    parsed = analysis.get("parsed") if isinstance(analysis.get("parsed"), dict) else {}

    if source == "ai" and analysis.get("valid_json") and parsed:
        forward = parsed.get("forward_view") if isinstance(parsed.get("forward_view"), dict) else {}
        parts = [
            f"前瞻={forward.get('direction', parsed.get('direction', '-'))}",
            f"P={forward.get('probability', parsed.get('confidence', '-'))}",
            f"窗口={forward.get('horizon_minutes', '-')}m",
            f"推送={final_decision.get('push_recommendation', 'none')}",
        ]
        for key, label in (
            ("entry", "入场"),
            ("stop_loss", "止损"),
            ("take_profit", "止盈"),
            ("risk_level", "风险"),
        ):
            value = final_decision.get(key) or parsed.get(key)
            if value not in (None, "", "-"):
                parts.append(f"{label}={value}")
        lines.append(f"  AI前瞻: {' | '.join(parts)}")

        if forward.get("invalidation") not in (None, "", "-"):
            lines.append(f"  失效条件: {_clip_console_text(forward.get('invalidation'), 120)}")

        audit = parsed_data_quality(parsed)
        audit_overall = audit.get("overall")
        if audit_overall:
            lines.append(f"  数据质量: {audit_overall}")

        trend = parsed.get("trend") if isinstance(parsed.get("trend"), dict) else {}
        if trend.get("summary"):
            lines.append(f"  现状回顾: {_clip_console_text(trend.get('summary'), 160)}")

        reasons = parsed.get("reasons")
        if isinstance(reasons, list) and reasons:
            lines.append(f"  前瞻理由: {_clip_console_text('; '.join(str(item) for item in reasons[:3]), 180)}")

        suggestion = forward.get("summary") or parsed.get("suggestion") or parsed_analysis_note(parsed)
        if suggestion:
            lines.append(f"  操作计划: {_clip_console_text(suggestion, 180)}")
        return lines

    if source == "local_fallback" or analysis.get("error") or analysis.get("validation_errors"):
        fail_parts: List[str] = []
        if analysis.get("validation_errors"):
            fail_parts.append(
                "校验失败: "
                + ", ".join(str(item) for item in (analysis.get("validation_errors") or [])[:3])
            )
        if analysis.get("error"):
            fail_parts.append(f"错误={analysis.get('error')}")
        content = analysis.get("content")
        if isinstance(content, str) and content.strip():
            fail_parts.append(_clip_console_text(content, 140))
        if fail_parts:
            lines.append("  AI失败: " + " | ".join(fail_parts))

    return lines


def format_local_decision_line(final_decision: Dict[str, Any]) -> str:
    source = str(final_decision.get("decision_source", "local_screening") or "local_screening")
    label = "本地兜底" if source == "local_fallback" else "本地筛查"
    screening = final_decision.get("local_screening") if isinstance(final_decision.get("local_screening"), dict) else {}
    local_bias = screening.get("local_bias") or final_decision.get("local_bias") or final_decision.get("direction", "-")
    parts = [
        f"结构偏向={local_bias}",
        f"观察分={final_decision.get('confidence', '-')}",
        f"推送={final_decision.get('push_recommendation', 'none')}",
    ]
    summary = final_decision.get("summary") or screening.get("summary")
    if summary:
        parts.append(f"回顾={_clip_console_text(summary, 80)}")
    return f"  {label}: " + " | ".join(parts)


STRATEGY_PROFILES = {
    "scalp": {
        "label": "超短线",
        "primary_bars": ["1m", "3m", "5m"],
        "confirm_bars": ["15m"],
        "background_bars": ["1H"],
        "ignore_bars": ["4H"],
        "score_weights": {
            "trend": 0.7,
            "momentum": 1.4,
            "volume_price": 1.5,
            "orderbook": 1.3,
            "derivatives": 0.7,
            "higher_timeframe": 0.5,
            "risk_control": 1.1,
        },
        "entry_style": "momentum",
        "holding_time": "3-15分钟",
    },
    "short": {
        "label": "短线",
        "primary_bars": ["5m", "15m"],
        "confirm_bars": ["1H"],
        "background_bars": ["4H"],
        "ignore_bars": [],
        "score_weights": {
            "trend": 1.2,
            "momentum": 1.15,
            "volume_price": 0.95,
            "derivatives": 1.1,
            "orderbook": 0.75,
            "higher_timeframe": 1.35,
            "risk_control": 1.0,
        },
        "entry_style": "pullback_or_breakout",
        "holding_time": "15分钟-数小时",
    },
    "swing": {
        "label": "中线",
        "primary_bars": ["1H", "4H"],
        "confirm_bars": ["15m"],
        "background_bars": [],
        "ignore_bars": ["1m"],
        "score_weights": {
            "trend": 1.55,
            "momentum": 1.05,
            "volume_price": 0.65,
            "derivatives": 1.1,
            "orderbook": 0.25,
            "higher_timeframe": 1.5,
            "risk_control": 1.2,
        },
        "entry_style": "trend_structure",
        "holding_time": "数小时-数天",
    },
    "long": {
        "label": "长线",
        "primary_bars": ["1D", "1W"],
        "confirm_bars": ["4H"],
        "background_bars": ["1H"],
        "ignore_bars": ["1m", "3m", "5m"],
        "score_weights": {
            "trend": 1.6,
            "momentum": 0.55,
            "volume_price": 0.45,
            "derivatives": 0.9,
            "orderbook": 0.1,
            "risk_control": 1.45,
        },
        "entry_style": "macro_trend_structure",
        "holding_time": "数天-数周",
    },
}

# 价格领先方向层级：不因质量/结构滞后把快速做空降级为观望。
FAST_PRICE_DIRECTION_TIERS = frozenset({
    "intrabar_crash",
    "intrabar_drop",
    "price_leading",
    "price_pullback",
    "swing_exit_short",
    "swing_exit_watch",
    "swing_entry_long",
    "swing_entry_watch",
})

TREND_PROFILE_PARAMS: Dict[str, Dict[str, Any]] = {
    "1m": {"ema": (6, 13, 34, 89), "slope_bars": 5, "slope_floor_pct": 0.04, "atr_floor_pct": 0.035, "structure_lookback": 15},
    "3m": {"ema": (8, 21, 55, 120), "slope_bars": 5, "slope_floor_pct": 0.05, "atr_floor_pct": 0.05, "structure_lookback": 18},
    "5m": {"ema": (9, 20, 60, 120), "slope_bars": 4, "slope_floor_pct": 0.06, "atr_floor_pct": 0.06, "structure_lookback": 20},
    "15m": {"ema": (9, 20, 60, 120), "slope_bars": 4, "slope_floor_pct": 0.08, "atr_floor_pct": 0.08, "structure_lookback": 20},
    "1H": {"ema": (9, 21, 55, 120), "slope_bars": 3, "slope_floor_pct": 0.09, "atr_floor_pct": 0.10, "structure_lookback": 24},
    "4H": {"ema": (8, 21, 55, 120), "slope_bars": 3, "slope_floor_pct": 0.16, "atr_floor_pct": 0.15, "structure_lookback": 30},
    "1D": {"ema": (8, 21, 55, 120), "slope_bars": 3, "slope_floor_pct": 0.45, "atr_floor_pct": 0.35, "structure_lookback": 40},
    "1W": {"ema": (6, 13, 34, 89), "slope_bars": 2, "slope_floor_pct": 0.90, "atr_floor_pct": 0.70, "structure_lookback": 52},
}

# AI payload：按策略裁剪 K 线根数；信号/触发可强制纳入周期。
AI_SIGNAL_BAR_MAP = {
    "volume_spike": ("1m",),
    "structure_break": ("5m", "15m"),
    "boll_squeeze": ("15m",),
    "rsi_divergence": ("15m",),
    "rsi_extreme": ("15m",),
    "macd_momentum_change": ("15m",),
    "oi_change": (),
    "funding_hot": (),
    "funding_fast_change": (),
    "long_short_extreme": (),
    "order_book_imbalance": (),
}
AI_CANDLE_LIMITS: Dict[str, Dict[str, int]] = {
    "scalp": {"1m": 30, "3m": 28, "5m": 20, "15m": 16, "1H": 12},
    "short": {"3m": 16, "5m": 20, "15m": 16, "1H": 12, "4H": 12},
    "swing": {"15m": 16, "1H": 16, "4H": 16},
    "long": {"1H": 12, "4H": 20, "1D": 40, "1W": 30},
}
AI_SIGNAL_CANDLE_LIMITS = {"1m": 20, "3m": 16, "5m": 20, "15m": 16, "1H": 12, "4H": 12, "1D": 20, "1W": 16}
AI_HISTORY_LIMITS = {"scalp": 90, "short": 120, "swing": 150, "long": 180}
AI_DATA_QUALITY_UNTRUSTED = frozenset({"不可信", "数据不足", "insufficient"})


def compact_background_profile(profile: Dict[str, Any]) -> Dict[str, Any]:
    if not profile:
        return {"note": "profile_only", "trend": "unknown"}
    rsi = profile.get("rsi") if isinstance(profile.get("rsi"), dict) else {}
    adx = profile.get("adx") if isinstance(profile.get("adx"), dict) else {}
    return {
        "note": "profile_only",
        "trend": profile.get("trend", "unknown"),
        "breakout": profile.get("breakout", "none"),
        "rsi_14": rsi.get("14"),
        "adx": adx.get("adx"),
        "ema_slope_pct": profile.get("ema_slope_pct"),
    }


def compact_ai_bar_profile(profile: Dict[str, Any]) -> Dict[str, Any]:
    """Precomputed neutral indicators for bars with raw candles — not trade conclusions."""
    if not profile:
        return {"note": "empty"}
    rsi = profile.get("rsi") if isinstance(profile.get("rsi"), dict) else {}
    macd = profile.get("macd") if isinstance(profile.get("macd"), dict) else {}
    boll = profile.get("boll") if isinstance(profile.get("boll"), dict) else {}
    adx = profile.get("adx") if isinstance(profile.get("adx"), dict) else {}
    data_quality = profile.get("data_quality") if isinstance(profile.get("data_quality"), dict) else {}
    return {
        "bar": profile.get("bar"),
        "trend": profile.get("trend", "unknown"),
        "breakout": profile.get("breakout", "none"),
        "divergence": profile.get("divergence", "none"),
        "rsi_14": rsi.get("14"),
        "macd_hist": macd.get("hist"),
        "macd_hist_slope": macd.get("hist_slope"),
        "atr_pct": profile.get("atr_pct"),
        "boll_bandwidth_pct": boll.get("bandwidth_pct"),
        "boll_position": boll.get("position"),
        "adx": adx.get("adx"),
        "ema_slope_pct": profile.get("ema_slope_pct"),
        "recent_high": profile.get("recent_high"),
        "recent_low": profile.get("recent_low"),
        "indicator_ready": data_quality.get("is_reliable"),
    }


def compact_ai_market_context(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """Factual market state for AI payload — no local score/direction conclusions."""
    context = snapshot.get("market_context", {}) if isinstance(snapshot.get("market_context"), dict) else {}
    payload = {
        "regime": context.get("regime"),
        "recent_price_pressure": context.get("recent_price_pressure", "neutral"),
        "oi_price_state": context.get("oi_price_state"),
        "order_book_bias": context.get("order_book_bias"),
        "volume_threshold_used": context.get("volume_threshold_used"),
        "strategy_template": context.get("strategy_template"),
    }
    warnings = context.get("warnings")
    if isinstance(warnings, list) and warnings:
        payload["warnings"] = [str(item) for item in warnings[:6]]
    return payload


def parsed_data_quality(parsed: Dict[str, Any]) -> Dict[str, Any]:
    data_quality = parsed.get("data_quality")
    if isinstance(data_quality, dict):
        return data_quality
    rule_audit = parsed.get("rule_audit")
    if isinstance(rule_audit, dict):
        return rule_audit
    return {}


def parsed_analysis_note(parsed: Dict[str, Any]) -> str:
    for key in ("analysis_note", "score_comment"):
        value = parsed.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def data_quality_untrusted(parsed: Dict[str, Any]) -> bool:
    overall = str(parsed_data_quality(parsed).get("overall", "") or "")
    return overall in AI_DATA_QUALITY_UNTRUSTED or overall in {"不可信", "insufficient"}


def normalize_forward_view(raw: Any, *, default_horizon: int = 15) -> Dict[str, Any]:
    default_horizon = max(5, min(10080, int(default_horizon or 15)))
    if not isinstance(raw, dict):
        raw = {}
    horizon = raw.get("horizon_minutes", default_horizon)
    try:
        horizon = max(5, min(10080, int(round(float(horizon)))))
    except (TypeError, ValueError):
        horizon = default_horizon

    direction = str(raw.get("direction", "观望") or "观望")
    if direction not in ("做多", "做空", "观望"):
        direction = "观望"

    probability = raw.get("probability", 0)
    try:
        probability = max(0, min(100, int(round(float(probability)))))
    except (TypeError, ValueError):
        probability = 0

    entry_plan_raw = raw.get("entry_plan")
    entry_plan: Dict[str, Any] = {}
    if isinstance(entry_plan_raw, dict):
        for key in ("entry", "stop_loss", "take_profit"):
            value = entry_plan_raw.get(key)
            entry_plan[key] = "-" if value in (None, "") else str(value)

    scenarios: List[Dict[str, Any]] = []
    raw_scenarios = raw.get("scenarios")
    if isinstance(raw_scenarios, list):
        for item in raw_scenarios[:4]:
            if not isinstance(item, dict):
                continue
            scenario_dir = str(item.get("direction", "观望") or "观望")
            if scenario_dir not in ("做多", "做空", "观望"):
                scenario_dir = "观望"
            try:
                scenario_prob = max(0, min(100, int(round(float(item.get("probability", 0))))))
            except (TypeError, ValueError):
                scenario_prob = 0
            label = str(item.get("label", "") or "").strip()
            if not label:
                continue
            scenarios.append(
                {
                    "label": label,
                    "direction": scenario_dir,
                    "probability": scenario_prob,
                }
            )

    return {
        "horizon_minutes": horizon,
        "direction": direction,
        "probability": probability,
        "summary": str(raw.get("summary", "") or "").strip(),
        "invalidation": str(raw.get("invalidation", "-") or "-").strip() or "-",
        "entry_plan": entry_plan,
        "scenarios": scenarios,
    }


def apply_forward_view_to_parsed(parsed: Dict[str, Any]) -> Dict[str, Any]:
    forward = normalize_forward_view(
        parsed.get("forward_view"),
        default_horizon=int(parsed.get("forward_horizon_default", 15) or 15),
    )
    parsed["forward_view"] = forward
    if forward.get("direction") in ("做多", "做空", "观望"):
        parsed["direction"] = forward["direction"]
    entry_plan = forward.get("entry_plan") if isinstance(forward.get("entry_plan"), dict) else {}
    for key in ("entry", "stop_loss", "take_profit"):
        value = entry_plan.get(key)
        if value not in (None, "", "-"):
            parsed[key] = value
    if forward.get("summary"):
        parsed["suggestion"] = forward["summary"]
    if forward.get("probability"):
        parsed["confidence"] = max(int(parsed.get("confidence", 0) or 0), int(forward["probability"]))
    return parsed


def normalize_ai_parsed(parsed: Optional[Dict[str, Any]], score: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    if not isinstance(parsed, dict):
        return None
    score = score or {}
    normalized = dict(parsed)

    if not normalized.get("analysis_note") and normalized.get("score_comment"):
        normalized["analysis_note"] = normalized["score_comment"]

    if not isinstance(normalized.get("data_quality"), dict):
        if isinstance(normalized.get("rule_audit"), dict):
            audit = dict(normalized["rule_audit"])
            overall = str(audit.get("overall", "") or "").strip()
            legacy_overall = {
                "规则结果可信": "充足",
                "可信": "充足",
                "部分可信": "部分可用",
                "不可信": "数据不足",
            }
            if overall in legacy_overall:
                audit["overall"] = legacy_overall[overall]
            elif not overall:
                audit["overall"] = "部分可用"
            if not isinstance(audit.get("warnings"), list):
                audit["warnings"] = [str(audit["warnings"])] if audit.get("warnings") else []
            normalized["data_quality"] = audit

    data_quality = normalized.get("data_quality")
    if not isinstance(data_quality, dict):
        normalized["data_quality"] = {"overall": "部分可用", "warnings": []}
    else:
        if not str(data_quality.get("overall", "") or "").strip():
            data_quality["overall"] = "部分可用"
        if not isinstance(data_quality.get("warnings"), list):
            data_quality["warnings"] = [str(data_quality["warnings"])] if data_quality.get("warnings") else []

    confidence = normalized.get("confidence")
    if confidence is None:
        direction = normalized.get("direction", "观望")
        if direction in ("做多", "做空"):
            confidence = score.get("final_trade_score", score.get("raw_total_score", 0))
        else:
            confidence = score.get("raw_total_score", 0)
    if isinstance(confidence, str):
        try:
            confidence = int(round(float(confidence.strip())))
        except ValueError:
            confidence = 0
    elif isinstance(confidence, float):
        confidence = int(round(confidence))
    elif not isinstance(confidence, int):
        confidence = 0
    normalized["confidence"] = max(0, min(100, int(confidence or 0)))

    push_rec = str(normalized.get("push_recommendation", "") or "").strip().lower()
    if push_rec not in ("none", "watch", "trade", "spike"):
        push_rec = "none"
    normalized["push_recommendation"] = push_rec

    trend = normalized.get("trend")
    if isinstance(trend, str):
        normalized["trend"] = {"summary": trend, "timeframes": {}, "conflict": ""}
    elif not isinstance(trend, dict):
        normalized["trend"] = {"summary": "", "timeframes": {}, "conflict": ""}
    else:
        trend.setdefault("summary", "")
        if not isinstance(trend.get("timeframes"), dict):
            trend["timeframes"] = {}
        trend.setdefault("conflict", "")

    reasons = normalized.get("reasons")
    if isinstance(reasons, str):
        normalized["reasons"] = [reasons] if reasons.strip() else []
    elif not isinstance(reasons, list):
        normalized["reasons"] = []

    if normalized.get("direction") not in ("做多", "做空", "观望"):
        normalized["direction"] = "观望"
    if normalized.get("risk_level") not in ("低", "中", "高"):
        normalized["risk_level"] = "中"

    for key in ("entry", "stop_loss", "take_profit"):
        value = normalized.get(key)
        if value in (None, ""):
            normalized[key] = "-"

    normalized.setdefault("risk", "")
    normalized.setdefault("suggestion", "")

    default_horizon = 15
    if score:
        cfg = score.get("structure_forecast") if isinstance(score.get("structure_forecast"), dict) else {}
        if cfg.get("horizon_minutes"):
            default_horizon = max(5, int(cfg.get("horizon_minutes") or 15))
    normalized["forward_horizon_default"] = default_horizon
    if isinstance(normalized.get("forward_view"), dict) or "forward_view" in normalized:
        apply_forward_view_to_parsed(normalized)
    return normalized


@dataclass
class SignalConfig:
    # 当前1m成交量超过最近20根1m均量的倍数，超过即认为放量。
    volume_multiplier: float = 2.0

    # 15分钟内OI变化超过该百分比，认为合约持仓量发生异动。
    oi_change_pct_15m: float = 5.0

    # 资金费率绝对值过高时，说明市场单边拥挤，可能存在回调或逼空风险。
    funding_abs_threshold: float = 0.0008

    # 资金费率短时间快速变化，也会作为资金情绪异常信号。
    funding_change_threshold: float = 0.0003

    # 多头或空头占比超过75%，认为市场情绪极端。
    long_short_extreme: float = 0.75

    strategy_mode: str = "short"
    risk_preference: str = "standard"
    signal_trade_enabled: bool = True
    signal_watch_enabled: bool = True
    signal_spike_enabled: bool = True
    ai_output_style: str = "steady"
    allow_scalp_trade: bool = False
    allow_counter_4h_scalp: bool = False
    allow_oi_divergence_momentum: bool = False
    scalp_move_pct_5m: float = 0.22
    scalp_move_pct_10m: float = 0.35
    watch_push_score: int = DEFAULT_PUSH_SCORE
    spike_push_score: int = 62
    ai_conflict_guard: bool = True
    l3_local_spike_push: bool = False
    l2_require_volume_or_structure: bool = True
    signal_forecast_enabled: bool = True
    forecast_push_score: int = 58
    forecast_horizon_minutes: int = 15
    calibration_enabled: bool = True
    calibration_min_samples: int = 8
    calibration_blend_weight: float = 0.65
    calibration_disable_below_hit_rate: float = 0.38
    calibration_save_interval_seconds: int = 60
    paper_follow_ai_only: bool = True
    paper_fee_bps: float = 5.0
    forward_require_forecast_alignment: bool = True
    replay_ai_cache_enabled: bool = True

    # 网络请求失败后的重试次数，解决偶发超时、临时DNS异常等问题。
    retry_times: int = DEFAULT_RETRY_TIMES

    # 重试退避基础秒数，第N次失败会等待 retry_backoff * N 秒。
    retry_backoff: float = DEFAULT_RETRY_BACKOFF_SECONDS

    # 同币种、同方向、同类信号的推送冷却时间，避免重复轰炸。
    push_cooldown_seconds: int = DEFAULT_PUSH_COOLDOWN_SECONDS
    spike_push_cooldown_seconds: int = DEFAULT_SPIKE_PUSH_COOLDOWN_SECONDS
    watch_push_cooldown_seconds: int = DEFAULT_WATCH_PUSH_COOLDOWN_SECONDS
    reverse_trade_cooldown_seconds: int = DEFAULT_REVERSE_TRADE_COOLDOWN_SECONDS
    forecast_push_cooldown_seconds: int = DEFAULT_FORECAST_PUSH_COOLDOWN_SECONDS

    # 单个日志文件最大字节数，超过后轮转成 .1 文件。
    log_max_bytes: int = DEFAULT_LOG_MAX_BYTES

    # 全部分卷合计上限，超过后删除最旧分卷。
    log_total_max_bytes: int = DEFAULT_LOG_TOTAL_MAX_BYTES

    # 是否写入 JSON 分析日志与每轮控制台摘要；回放模式不受此开关影响。
    analysis_log_enabled: bool = True


@dataclass
class RuntimeConfig:
    retry_times: int = DEFAULT_RETRY_TIMES
    retry_backoff: float = DEFAULT_RETRY_BACKOFF_SECONDS
    push_cooldown_seconds: int = DEFAULT_PUSH_COOLDOWN_SECONDS
    spike_push_cooldown_seconds: int = DEFAULT_SPIKE_PUSH_COOLDOWN_SECONDS
    watch_push_cooldown_seconds: int = DEFAULT_WATCH_PUSH_COOLDOWN_SECONDS
    reverse_trade_cooldown_seconds: int = DEFAULT_REVERSE_TRADE_COOLDOWN_SECONDS
    forecast_push_cooldown_seconds: int = DEFAULT_FORECAST_PUSH_COOLDOWN_SECONDS
    log_max_bytes: int = DEFAULT_LOG_MAX_BYTES
    log_total_max_bytes: int = DEFAULT_LOG_TOTAL_MAX_BYTES
    analysis_log_enabled: bool = True


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def parse_time_text(value: str) -> datetime:
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise ValueError(f"unsupported time format: {value}")


def deep_copy_json(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False))


def load_replay_dataset(path: Path) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    if not path.exists():
        raise FileNotFoundError(f"replay dataset not found: {path}")
    meta: Dict[str, Any] = {}
    frames: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line:
            continue
        item = json.loads(line)
        if item.get("type") == "meta":
            meta = item
        elif item.get("type") == "frame":
            frames.append(item)
    if not frames:
        raise ValueError("replay dataset has no frames")
    required = ("time", "inst_id", "ticker", "candles", "open_interest", "funding_rate", "long_short_ratio", "order_book")
    for index, frame in enumerate(frames):
        missing = [key for key in required if key not in frame]
        if missing:
            raise ValueError(f"replay frame #{index + 1} missing fields: {', '.join(missing)}")
        for bar in BAR_CHANNELS[:6]:
            if bar not in frame["candles"]:
                raise ValueError(f"replay frame #{index + 1} missing candles.{bar}")
        for bar in BAR_CHANNELS:
            frame["candles"].setdefault(bar, [])
    frames.sort(key=lambda frame: (frame.get("time", ""), frame.get("inst_id", "")))
    return meta, frames


def replay_dataset_stats(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {"exists": False, "path": str(path), "frame_count": 0, "inst_ids": [], "interval_seconds": 0}
    meta: Dict[str, Any] = {}
    frame_count = 0
    inst_ids = set()
    with path.open("r", encoding="utf-8-sig") as file:
        for line in file:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            item_type = item.get("type")
            if item_type == "meta":
                meta = item
            elif item_type == "frame":
                frame_count += 1
                inst_id = str(item.get("inst_id", "") or "")
                if inst_id:
                    inst_ids.add(inst_id)
    if frame_count <= 0:
        return {"exists": False, "path": str(path), "frame_count": 0, "inst_ids": [], "interval_seconds": 0}
    return {
        "exists": True,
        "path": str(path),
        "frame_count": frame_count,
        "inst_ids": sorted(inst_ids),
        "interval_seconds": int(meta.get("interval_seconds") or 0),
        "recorded_at": str(meta.get("recorded_at") or ""),
        "version": str(meta.get("version") or REPLAY_DATASET_VERSION),
    }


def ms_to_text(ts_ms: Any) -> str:
    try:
        return datetime.fromtimestamp(int(ts_ms) / 1000).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return "-"


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_div(numerator: float, denominator: float, default: float = 0.0) -> float:
    # 金融行情里经常会遇到空数据、0成交量或接口缺字段。
    # 所有比值统一走safe_div，避免单个异常值让整轮run_once失败。
    return numerator / denominator if denominator else default


def pct_change(new_value: float, old_value: float) -> float:
    # 百分比变化统一用这个函数，old_value<=0时不强行计算，避免OI等指标刚启动时误报。
    return safe_div(new_value - old_value, old_value) * 100 if old_value > 0 else 0.0


def percentile(values: List[float], rank: float, default: float = 0.0) -> float:
    # 轻量分位数函数，不引入numpy/pandas，适合交付到只有标准库的Windows环境。
    # rank取0-1，例如0.85表示85分位，用于把固定阈值升级成“相对近期市场状态”的动态阈值。
    clean = sorted(value for value in values if isinstance(value, (int, float)))
    if not clean:
        return default
    if len(clean) == 1:
        return clean[0]
    pos = max(0.0, min(1.0, rank)) * (len(clean) - 1)
    lower = int(pos)
    upper = min(lower + 1, len(clean) - 1)
    weight = pos - lower
    return clean[lower] * (1 - weight) + clean[upper] * weight


def confirmed_candles(candles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    # OKX返回的第一根通常是正在形成的K线，confirmed="0"。
    # 趋势、ATR、均量尽量使用已收盘K线，避免1m开盘几秒的半成品数据误导判断。
    confirmed = [item for item in candles if str(item.get("confirmed", "1")) == "1"]
    return confirmed or candles


def tactical_candles(candles: List[Dict[str, Any]], live_price: float = 0.0) -> List[Dict[str, Any]]:
    """方向判断用：保留未收盘K线，并把最新价写入正在形成的K线。

    已收盘K线算出来的 EMA/MACD 在 15m 周期内会滞后一整根K线；
    这里用实时价刷新当前根，让盘中下跌不必等到 15m 收盘才反映到趋势里。
    量价/校准仍走 confirmed_candles，两者分工不同。
    """
    if not candles:
        return []
    rows = [dict(item) for item in candles]
    if live_price <= 0:
        return rows
    latest = dict(rows[0])
    latest["close"] = live_price
    latest["high"] = max(to_float(latest.get("high")), live_price)
    low = to_float(latest.get("low"))
    latest["low"] = min(low, live_price) if low > 0 else live_price
    rows[0] = latest
    return rows


def ema(values: List[float], period: int) -> float:
    # 指数均线用于判断趋势方向和斜率。数据不足时用已有数据计算，宁可粗略也不返回异常。
    clean = [value for value in values if isinstance(value, (int, float))]
    if not clean:
        return 0.0
    k = 2 / (period + 1)
    result = clean[-1]
    for value in reversed(clean[:-1]):
        result = value * k + result * (1 - k)
    return result


def ema_series(values: List[float], period: int) -> List[float]:
    # 返回“最新在前”的EMA序列。输入也是OKX K线常见的最新在前顺序。
    # 先从最旧数据开始推导，再反转回来，这样MACD等指标能拿到当前值和上一根值。
    clean = [value for value in values if isinstance(value, (int, float))]
    if not clean:
        return []
    chronological = list(reversed(clean))
    k = 2 / (period + 1)
    result = []
    current = chronological[0]
    for value in chronological:
        current = value * k + current * (1 - k)
        result.append(current)
    return list(reversed(result))


def wilder_smooth(values: List[float], period: int) -> List[float]:
    # Wilder平滑用于RSI/ADX/ATR等经典指标，和多数交易软件的默认算法更接近。
    clean = [value for value in values if isinstance(value, (int, float))]
    if not clean:
        return []
    chronological = list(reversed(clean))
    result = []
    current = sum(chronological[:period]) / min(period, len(chronological))
    for index, value in enumerate(chronological):
        if index < period:
            current = sum(chronological[: index + 1]) / (index + 1)
        else:
            current = (current * (period - 1) + value) / period
        result.append(current)
    return list(reversed(result))


def sma(values: List[float], period: int) -> float:
    # 简单均线响应更慢，适合做大级别过滤。短线判断仍以EMA为主。
    clean = [value for value in values if isinstance(value, (int, float))]
    if not clean:
        return 0.0
    sample = clean[:period]
    return sum(sample) / len(sample) if sample else 0.0


def rsi(values: List[float], period: int = 14) -> float:
    # RSI衡量上涨/下跌平均力度。这里使用Wilder平滑，更接近OKX/TradingView常见口径。
    clean = [value for value in values if isinstance(value, (int, float))]
    if len(clean) < period + 1:
        return 50.0
    gains = []
    losses = []
    for index in range(len(clean) - 1):
        delta = clean[index] - clean[index + 1]
        if delta >= 0:
            gains.append(delta)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(abs(delta))
    avg_gain = wilder_smooth(gains, period)[0] if gains else 0.0
    avg_loss = wilder_smooth(losses, period)[0] if losses else 0.0
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def macd(values: List[float], fast: int = 12, slow: int = 26, signal: int = 9) -> Dict[str, float]:
    # MACD用于判断趋势动量是否增强或衰减。使用EMA序列计算DIF/DEA，比单点轻量近似更稳定。
    clean = [value for value in values if isinstance(value, (int, float))]
    if len(clean) < slow + signal:
        return {"dif": 0.0, "dea": 0.0, "hist": 0.0, "hist_slope": 0.0}
    fast_series = ema_series(clean, fast)
    slow_series = ema_series(clean, slow)
    size = min(len(fast_series), len(slow_series))
    dif_series = [fast_series[index] - slow_series[index] for index in range(size)]
    current_dif = dif_series[0]
    dea_series = ema_series(dif_series, signal)
    current_dea = dea_series[0] if dea_series else 0.0
    previous_hist = dif_series[1] - dea_series[1] if len(dif_series) > 1 and len(dea_series) > 1 else 0.0
    hist = current_dif - current_dea
    return {
        "dif": current_dif,
        "dea": current_dea,
        "hist": hist,
        "hist_slope": hist - previous_hist,
    }


def kdj(candles: List[Dict[str, Any]], period: int = 9) -> Dict[str, float]:
    # KDJ对短线拐点很敏感，适合做入场时机确认；但高位/低位钝化很常见，不能单独决定方向。
    rows = confirmed_candles(candles)
    if len(rows) < 2:
        return {"k": 50.0, "d": 50.0, "j": 50.0}
    k_value = 50.0
    d_value = 50.0
    for index in range(min(len(rows), period + 20) - 1, -1, -1):
        sample = rows[index:index + period]
        if not sample:
            continue
        high = max(to_float(item.get("high")) for item in sample)
        low = min(to_float(item.get("low")) for item in sample)
        close = to_float(rows[index].get("close"))
        rsv = safe_div(close - low, high - low, 0.5) * 100
        k_value = k_value * 2 / 3 + rsv / 3
        d_value = d_value * 2 / 3 + k_value / 3
    return {"k": k_value, "d": d_value, "j": 3 * k_value - 2 * d_value}


def bollinger(values: List[float], period: int = 20, width: float = 2.0) -> Dict[str, float]:
    # 布林带用于区分震荡、挤压和突破。带宽越窄，越可能处于蓄势；突破后回到带内通常是假突破风险。
    sample = [value for value in values[:period] if isinstance(value, (int, float))]
    if not sample:
        return {"mid": 0.0, "upper": 0.0, "lower": 0.0, "bandwidth_pct": 0.0, "position": 0.5}
    mid = sum(sample) / len(sample)
    variance = sum((value - mid) ** 2 for value in sample) / len(sample)
    std = variance ** 0.5
    upper = mid + width * std
    lower = mid - width * std
    latest = sample[0]
    return {
        "mid": mid,
        "upper": upper,
        "lower": lower,
        "bandwidth_pct": safe_div(upper - lower, mid) * 100,
        "position": safe_div(latest - lower, upper - lower, 0.5),
    }


def adx(candles: List[Dict[str, Any]], period: int = 14) -> Dict[str, float]:
    # ADX用于判断“有没有趋势”，+DI/-DI用于判断趋势方向。
    # 这是避免震荡行情里追涨杀跌的关键过滤器之一。
    rows = confirmed_candles(candles)
    if len(rows) < period * 2:
        return {"adx": 0.0, "plus_di": 0.0, "minus_di": 0.0}
    plus_dm = []
    minus_dm = []
    true_ranges = []
    for index in range(len(rows) - 1):
        current = rows[index]
        previous = rows[index + 1]
        high = to_float(current.get("high"))
        low = to_float(current.get("low"))
        prev_high = to_float(previous.get("high"))
        prev_low = to_float(previous.get("low"))
        prev_close = to_float(previous.get("close"))
        up_move = high - prev_high
        down_move = prev_low - low
        plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0.0)
        minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0.0)
        true_ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    tr_smooth = wilder_smooth(true_ranges, period)
    plus_smooth = wilder_smooth(plus_dm, period)
    minus_smooth = wilder_smooth(minus_dm, period)
    if not tr_smooth:
        return {"adx": 0.0, "plus_di": 0.0, "minus_di": 0.0}
    plus_di_series = [safe_div(plus_smooth[index], tr_smooth[index]) * 100 for index in range(min(len(plus_smooth), len(tr_smooth)))]
    minus_di_series = [safe_div(minus_smooth[index], tr_smooth[index]) * 100 for index in range(min(len(minus_smooth), len(tr_smooth)))]
    dx_series = [
        safe_div(abs(plus_di_series[index] - minus_di_series[index]), plus_di_series[index] + minus_di_series[index]) * 100
        for index in range(min(len(plus_di_series), len(minus_di_series)))
    ]
    adx_series = wilder_smooth(dx_series, period)
    return {
        "adx": adx_series[0] if adx_series else 0.0,
        "plus_di": plus_di_series[0] if plus_di_series else 0.0,
        "minus_di": minus_di_series[0] if minus_di_series else 0.0,
    }


def atr(candles: List[Dict[str, Any]], period: int = 14) -> float:
    # ATR衡量真实波动。入场区、止损、止盈都应该跟随波动率，而不是写死固定百分比。
    rows = confirmed_candles(candles)
    if len(rows) < 2:
        return 0.0
    true_ranges = []
    for index, item in enumerate(rows[: period + 1]):
        high = to_float(item.get("high"))
        low = to_float(item.get("low"))
        prev_close = to_float(rows[index + 1].get("close")) if index + 1 < len(rows) else to_float(item.get("close"))
        true_ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    return sum(true_ranges[:period]) / min(period, len(true_ranges)) if true_ranges else 0.0


def candle_range(item: Dict[str, Any]) -> float:
    return max(0.0, to_float(item.get("high")) - to_float(item.get("low")))


def candle_body_ratio(item: Dict[str, Any]) -> float:
    # 实体占比越高，说明该根K线方向表达更明确；长上下影线则更容易是假突破或扫损。
    body = abs(to_float(item.get("close")) - to_float(item.get("open")))
    return safe_div(body, candle_range(item))


def structure_points(candles: List[Dict[str, Any]], lookback: int = 20) -> Dict[str, float]:
    # 结构高低点用于生成止损/失效位。用已确认K线，避免当前未收盘K线不断移动。
    rows = confirmed_candles(candles)[:lookback]
    highs = [to_float(item.get("high")) for item in rows]
    lows = [to_float(item.get("low")) for item in rows]
    return {
        "recent_high": max(highs) if highs else 0.0,
        "recent_low": min(lows) if lows else 0.0,
    }


def okx_data(response: Dict[str, Any]) -> List[Any]:
    # OKX SDK返回一般是 {"code": "0", "data": [...], "msg": ""}。
    # 这里统一抽取data，避免每个调用点重复判断code和data类型。
    if not isinstance(response, dict):
        return []
    if response.get("code") not in (None, "0"):
        return []
    data = response.get("data") or []
    return data if isinstance(data, list) else []


def retry_call(label: str, func: Any, retry_times: int, retry_backoff: float) -> Any:
    # 所有外部网络调用统一走这里，避免单次网络抖动导致整轮分析失败。
    last_error: Optional[Exception] = None
    retry_times = max(1, min(int(retry_times), 5))
    retry_backoff = max(0.1, min(float(retry_backoff), 5.0))
    for attempt in range(1, retry_times + 1):
        try:
            return func()
        except Exception as exc:
            last_error = exc
            if attempt >= retry_times:
                break
            sleep_seconds = min(retry_backoff * attempt, 15.0)
            console_debug(f"[{now_text()}] {label} failed, retry {attempt}/{retry_times}: {exc}")
            time.sleep(sleep_seconds)
    raise RuntimeError(f"{label} failed after {retry_times} retries: {last_error}")


_okx_circuit_open_until = 0.0
_okx_consecutive_failures = 0
_okx_circuit_lock = threading.Lock()


def _check_okx_circuit() -> None:
    if time.time() < _okx_circuit_open_until:
        remaining = max(1, int(_okx_circuit_open_until - time.time()))
        raise RuntimeError(f"OKX circuit open, retry after {remaining}s")


def _record_okx_success() -> None:
    global _okx_consecutive_failures
    with _okx_circuit_lock:
        _okx_consecutive_failures = 0


def _record_okx_failure() -> None:
    global _okx_consecutive_failures, _okx_circuit_open_until
    threshold = max(2, env_int("OKX_CIRCUIT_FAIL_THRESHOLD", DEFAULT_OKX_CIRCUIT_FAIL_THRESHOLD))
    cooldown = max(30, env_int("OKX_CIRCUIT_COOLDOWN_SECONDS", DEFAULT_OKX_CIRCUIT_COOLDOWN_SECONDS))
    with _okx_circuit_lock:
        _okx_consecutive_failures += 1
        if _okx_consecutive_failures >= threshold:
            _okx_circuit_open_until = time.time() + cooldown
            _okx_consecutive_failures = 0
            console_warn(f"[{now_text()}] OKX circuit opened for {cooldown}s after repeated failures")


def okx_retry_call(label: str, func: Any, retry_times: int, retry_backoff: float) -> Any:
    _check_okx_circuit()
    try:
        result = retry_call(label, func, retry_times, retry_backoff)
        _record_okx_success()
        return result
    except Exception:
        _record_okx_failure()
        raise


RETRYABLE_AI_ERROR_NAMES = {
    "APIConnectionError",
    "APITimeoutError",
    "InternalServerError",
    "RateLimitError",
    "TimeoutError",
    "ConnectionError",
    "ConnectError",
    "ReadTimeout",
    "WriteTimeout",
    "RemoteProtocolError",
}
NON_RETRYABLE_AI_STATUS = {400, 401, 403, 404, 422}


def ai_error_status_code(exc: Exception) -> Optional[int]:
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    response = getattr(exc, "response", None)
    if response is not None:
        response_status = getattr(response, "status_code", None)
        if isinstance(response_status, int):
            return response_status
    return None


def is_auth_ai_error(exc: Exception) -> bool:
    status = ai_error_status_code(exc)
    return status in (401, 403)


def is_rate_limit_ai_error(exc: Exception) -> bool:
    if exc.__class__.__name__ == "RateLimitError":
        return True
    return ai_error_status_code(exc) == 429


def is_connection_ai_error(exc: Exception) -> bool:
    if exc.__class__.__name__ in {"APIConnectionError", "APITimeoutError", "ConnectionError", "ConnectError"}:
        return True
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    return False


def is_retryable_ai_error(exc: Exception) -> bool:
    if is_auth_ai_error(exc):
        return False
    status = ai_error_status_code(exc)
    if status is not None:
        if status in NON_RETRYABLE_AI_STATUS:
            return False
        if status in (429, 500, 502, 503, 504):
            return True
    return exc.__class__.__name__ in RETRYABLE_AI_ERROR_NAMES


def http_get_json(
    path: str,
    params: Dict[str, str],
    retry_times: int = DEFAULT_RETRY_TIMES,
    retry_backoff: float = DEFAULT_RETRY_BACKOFF_SECONDS,
) -> Dict[str, Any]:
    # 标准库REST兜底：当python-okx未安装或SDK调用失败时，仍可继续采集公共行情。
    def request() -> Dict[str, Any]:
        query = urllib.parse.urlencode(params)
        url = f"{OKX_BASE_URL}{path}?{query}"
        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "okx-ai-short-term-assistant/1.0",
            },
        )
        try:
            response = urllib.request.urlopen(req, timeout=10)
        except urllib.error.HTTPError as exc:
            # Rubik统计接口在部分网络/IP/地区会返回403。多空比是辅助数据，
            # 403时直接降级为空数据，不影响价格、K线、OI、资金费率等主流程。
            if exc.code == 403:
                return {"code": "403", "data": [], "msg": "Forbidden"}
            raise
        with response:
            return json.loads(response.read().decode("utf-8"))

    return okx_retry_call(path, request, retry_times, retry_backoff)


def okx_public_get(
    path: str,
    params: Dict[str, str],
    retry_times: int = DEFAULT_RETRY_TIMES,
    retry_backoff: float = DEFAULT_RETRY_BACKOFF_SECONDS,
) -> Dict[str, Any]:
    return http_get_json(path, params, retry_times, retry_backoff)


def http_post_json(
    url: str,
    payload: Dict[str, Any],
    retry_times: int = DEFAULT_RETRY_TIMES,
    retry_backoff: float = DEFAULT_RETRY_BACKOFF_SECONDS,
) -> None:
    # Telegram、企业微信、微信机器人都可以用JSON POST形式推送。
    def request() -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            response.read()

    retry_call("push-webhook", request, retry_times, retry_backoff)


def extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    # AI有时会在JSON外包一层说明文字或Markdown代码块。
    # 这里尽量提取第一个完整JSON对象，解析失败则交给本地规则兜底。
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.startswith("json"):
            cleaned = cleaned[4:].strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def candle_to_dict(raw: List[Any]) -> Dict[str, Any]:
    # OKX K线原始格式是数组：
    # [ts, open, high, low, close, vol, volCcy, volCcyQuote, confirm]
    # 转成dict后，后面的趋势判断、AI输入和日志都会更清晰。
    return {
        "time": ms_to_text(raw[0]) if len(raw) > 0 else "-",
        "open": to_float(raw[1]) if len(raw) > 1 else 0.0,
        "high": to_float(raw[2]) if len(raw) > 2 else 0.0,
        "low": to_float(raw[3]) if len(raw) > 3 else 0.0,
        "close": to_float(raw[4]) if len(raw) > 4 else 0.0,
        "volume": to_float(raw[5]) if len(raw) > 5 else 0.0,
        "confirmed": str(raw[8]) if len(raw) > 8 else "0",
    }


def trend_from_candles(
    candles: List[Dict[str, Any]],
    lookback: int = 5,
    *,
    tactical: bool = False,
    live_price: float = 0.0,
) -> str:
    # 兼容旧字段的轻量趋势判断：比较最近收盘价和lookback窗口最后一根收盘价。
    # 新版评分不再只依赖它，而是由trend_profile_from_candles计算EMA、ATR、结构位和K线质量。
    if len(candles) < 2:
        return "unknown"
    if tactical and live_price > 0:
        sample = tactical_candles(candles, live_price)[:lookback]
    else:
        sample = confirmed_candles(candles)[:lookback]
    latest = sample[0]["close"]
    oldest = sample[-1]["close"]
    if latest > oldest:
        return "up"
    if latest < oldest:
        return "down"
    return "flat"


def trend_profile_from_candles(
    candles: List[Dict[str, Any]],
    bar: str = "15m",
    *,
    tactical: bool = False,
    live_price: float = 0.0,
) -> Dict[str, Any]:
    # 单周期趋势画像。这里故意不只返回up/down，而是把“趋势、波动、结构、K线质量”都拆开。
    # 原因是短线交易里，同样是上涨，可能是稳定趋势、放量突破、尾端加速或高波动震荡，入场方式完全不同。
    rows = tactical_candles(candles, live_price) if tactical else confirmed_candles(candles)
    closes = [to_float(item.get("close")) for item in rows]
    params = TREND_PROFILE_PARAMS.get(bar, TREND_PROFILE_PARAMS["15m"])
    ema_periods = tuple(params["ema"])
    fast_period, base_period, slow_period, anchor_period = ema_periods
    slope_bars = max(1, int(params["slope_bars"]))
    structure_lookback = max(5, int(params["structure_lookback"]))

    # 判断K线质量，从这组k线中计算出来在指标是否可靠
    data_quality = {
        "confirmed_count": len(confirmed_candles(candles)),
        "analysis_count": len(rows),
        "includes_forming_bar": tactical,
        "ema120_ready": len(rows) >= 120,    # 兼容旧字段
        "anchor_ema_ready": len(rows) >= anchor_period,
        "macd_ready": len(rows) >= 35,
        "adx_ready": len(rows) >= 28,
        "rsi_ready": len(rows) >= 25,
        "is_reliable": len(rows) >= 35,
    }
    if len(closes) < 5:
        return {
            "trend": "unknown",
            "bar": bar,
            "profile_params": dict(params),
            "data_quality": data_quality,
            "ema_fast": 0.0,
            "ema_slow": 0.0,
            "ema": {"9": 0.0, "20": 0.0, "60": 0.0, "120": 0.0},
            "ma": {"120": 0.0},
            "ema_slope_pct": 0.0,
            "atr": 0.0,
            "atr_pct": 0.0,
            "body_ratio": 0.0,
            "recent_high": 0.0,
            "recent_low": 0.0,
            "breakout": "none",
            "rsi": {"6": 50.0, "14": 50.0, "24": 50.0},
            "macd": {"dif": 0.0, "dea": 0.0, "hist": 0.0, "hist_slope": 0.0},
            "kdj": {"k": 50.0, "d": 50.0, "j": 50.0},
            "boll": {"mid": 0.0, "upper": 0.0, "lower": 0.0, "bandwidth_pct": 0.0, "position": 0.5},
            "adx": {"adx": 0.0, "plus_di": 0.0, "minus_di": 0.0},
            "distance_to_ema20_atr": 0.0,
            "divergence": "none",
        }

    latest = closes[0]
    previous = closes[min(slope_bars, len(closes) - 1)]

    # 计算各个EMA
    ema_fastest = ema(closes[: max(40, fast_period * 3)], fast_period)
    fast = ema(closes[: max(80, base_period * 3)], base_period)
    slow = ema(closes[: max(120, slow_period * 2)], slow_period)
    ema_anchor = ema(closes[: max(120, anchor_period)], anchor_period)
    ma_anchor = sma(closes[: max(120, anchor_period)], anchor_period)

    # 平均真实波动幅度，判断市场是否平静
    atr_value = atr(rows, 14)

    # 最近20k线的价格最高点/最低点
    points = structure_points(rows[1:], structure_lookback)
    recent_high = points["recent_high"]
    recent_low = points["recent_low"]

    # 最近5根k线的涨跌百分比
    slope_pct = pct_change(latest, previous)

    # k线实体占比
    body_ratio = candle_body_ratio(rows[0])

    rsi_values = {"6": rsi(closes, 6), "14": rsi(closes, 14), "24": rsi(closes, 24)}
    macd_values = macd(closes)
    kdj_values = kdj(rows)
    boll_values = bollinger(closes)
    adx_values = adx(rows)
    divergence = detect_rsi_divergence(rows, rsi_values["14"])

    # 当前价格是否突破最近的结构高点/低点
    breakout = "none"
    if recent_high and latest > recent_high:
        breakout = "up"
    elif recent_low and latest < recent_low:
        breakout = "down"

    # 趋势判断同时看均线排列和短期斜率，降低“只比较两根收盘价”的噪声。
    slope_floor_pct = to_float(params.get("slope_floor_pct"), 0.08)
    atr_floor_pct = to_float(params.get("atr_floor_pct"), 0.08)
    atr_pct = safe_div(atr_value, latest) * 100
    if latest > ema_fastest > fast > slow and slope_pct > slope_floor_pct:
        trend = "up"
    elif latest < ema_fastest < fast < slow and slope_pct < -slope_floor_pct:
        trend = "down"
    elif adx_values["adx"] < 18 or abs(slope_pct) < slope_floor_pct or atr_pct < atr_floor_pct:
        trend = "range"
    else:
        trend = "mixed"

    if trend in ("mixed", "range"):
        indicator_hint = indicator_direction_scores(
            {
                "trend": trend,
                "ema_fast": fast,
                "ema_slow": slow,
                "ema_slope_pct": slope_pct,
                "rsi": rsi_values,
                "macd": macd_values,
                "kdj": kdj_values,
                "boll": boll_values,
                "adx": adx_values,
                "divergence": divergence,
                "data_quality": data_quality,
            }
        )
        plus_di = to_float(adx_values.get("plus_di"))
        minus_di = to_float(adx_values.get("minus_di"))
        hist = to_float(macd_values.get("hist"))
        if (
            indicator_hint["net"] >= 22
            and adx_values["adx"] >= 20
            and plus_di > minus_di
            and hist > 0
        ):
            trend = "up"
        elif (
            indicator_hint["net"] <= -22
            and adx_values["adx"] >= 20
            and minus_di > plus_di
            and hist < 0
        ):
            trend = "down"

    return {
        "trend": trend,
        "bar": bar,
        "profile_params": dict(params),
        "data_quality": data_quality,
        "ema_fast": fast,
        "ema_slow": slow,
        "ema": {
            str(fast_period): ema_fastest,
            str(base_period): fast,
            str(slow_period): slow,
            str(anchor_period): ema_anchor,
        },
        "ma": {str(anchor_period): ma_anchor},
        "ema_slope_pct": slope_pct,
        "atr": atr_value,
        "atr_pct": atr_pct,
        "body_ratio": body_ratio,
        "recent_high": recent_high,
        "recent_low": recent_low,
        "breakout": breakout,
        "rsi": rsi_values,
        "macd": macd_values,
        "kdj": kdj_values,
        "boll": boll_values,
        "adx": adx_values,
        "distance_to_ema20_atr": safe_div(latest - fast, atr_value),
        "distance_to_base_ema_atr": safe_div(latest - fast, atr_value),
        "divergence": divergence,
    }


def detect_rsi_divergence(candles: List[Dict[str, Any]], current_rsi: float) -> str:
    # 简化版RSI背离检测：用最近高低点和当前RSI比较，给风险层做提示。
    # 它不是严格的波峰波谷算法，但足够提示“价格创新高/新低时动量没有跟上”。
    rows = confirmed_candles(candles)
    if len(rows) < 16:
        return "none"
    closes = [to_float(item.get("close")) for item in rows]
    latest = closes[0]
    previous_window = closes[5:16]
    if not previous_window:
        return "none"
    previous_high = max(previous_window)
    previous_low = min(previous_window)
    previous_rsi = rsi(closes[5:30], 14)
    if latest > previous_high and current_rsi < previous_rsi - 3:
        return "bearish"
    if latest < previous_low and current_rsi > previous_rsi + 3:
        return "bullish"
    return "none"


def indicator_direction_scores(profile: Dict[str, Any]) -> Dict[str, Any]:
    """单周期 EMA/RSI/MACD/KDJ/BOLL/ADX 综合方向评分，用于降低纯价格确认的滞后。"""
    if not profile or profile.get("trend") == "unknown":
        return {"long": 0.0, "short": 0.0, "net": 0.0, "strength": 0, "direction": "neutral"}

    long_pts = 0.0
    short_pts = 0.0
    data_quality = profile.get("data_quality", {}) if isinstance(profile.get("data_quality"), dict) else {}
    reliable = bool(data_quality.get("is_reliable", False))

    trend = str(profile.get("trend", "mixed") or "mixed")
    ema_slope = to_float(profile.get("ema_slope_pct"))
    if trend == "up":
        long_pts += 14
    elif trend == "down":
        short_pts += 14
    elif ema_slope > 0.05:
        long_pts += 6
    elif ema_slope < -0.05:
        short_pts += 6

    ema_fast = to_float(profile.get("ema_fast"))
    ema_slow = to_float(profile.get("ema_slow"))
    if trend not in ("up", "down"):
        if ema_fast > ema_slow > 0:
            long_pts += 5
        elif 0 < ema_fast < ema_slow:
            short_pts += 5

    macd_values = profile.get("macd", {}) if isinstance(profile.get("macd"), dict) else {}
    hist = to_float(macd_values.get("hist"))
    hist_slope = to_float(macd_values.get("hist_slope"))
    dif = to_float(macd_values.get("dif"))
    dea = to_float(macd_values.get("dea"))
    if hist > 0:
        long_pts += 6
        if hist_slope >= 0:
            long_pts += 6
    elif hist < 0:
        short_pts += 6
        if hist_slope <= 0:
            short_pts += 6
    if dif > dea:
        long_pts += 4
    elif dif < dea:
        short_pts += 4

    rsi_values = profile.get("rsi", {}) if isinstance(profile.get("rsi"), dict) else {}
    rsi_14 = to_float(rsi_values.get("14"), 50.0)
    if rsi_14 >= 55:
        long_pts += min(8.0, (rsi_14 - 52.0) * 0.35)
    elif rsi_14 <= 45:
        short_pts += min(8.0, (48.0 - rsi_14) * 0.35)

    kdj_values = profile.get("kdj", {}) if isinstance(profile.get("kdj"), dict) else {}
    k_val = to_float(kdj_values.get("k"), 50.0)
    d_val = to_float(kdj_values.get("d"), 50.0)
    if k_val > d_val:
        long_pts += 6
    elif k_val < d_val:
        short_pts += 6

    adx_values = profile.get("adx", {}) if isinstance(profile.get("adx"), dict) else {}
    adx_val = to_float(adx_values.get("adx"))
    plus_di = to_float(adx_values.get("plus_di"))
    minus_di = to_float(adx_values.get("minus_di"))
    if adx_val >= 16:
        adx_weight = min(1.2, adx_val / 25.0)
        if plus_di > minus_di:
            long_pts += 10.0 * adx_weight
        elif minus_di > plus_di:
            short_pts += 10.0 * adx_weight

    boll_values = profile.get("boll", {}) if isinstance(profile.get("boll"), dict) else {}
    boll_pos = to_float(boll_values.get("position"), 0.5)
    bandwidth = to_float(boll_values.get("bandwidth_pct"))
    if bandwidth >= 0.08:
        if boll_pos >= 0.55:
            long_pts += 5
        elif boll_pos <= 0.45:
            short_pts += 5

    divergence = str(profile.get("divergence", "none") or "none")
    if divergence == "bearish":
        long_pts -= 6
        short_pts += 3
    elif divergence == "bullish":
        short_pts -= 6
        long_pts += 3

    if not reliable:
        long_pts *= 0.65
        short_pts *= 0.65

    net = long_pts - short_pts
    strength = int(round(max(long_pts, short_pts)))
    if net >= 18:
        direction = "long"
    elif net <= -18:
        direction = "short"
    else:
        direction = "neutral"
    return {
        "long": round(long_pts, 2),
        "short": round(short_pts, 2),
        "net": round(net, 2),
        "strength": strength,
        "direction": direction,
    }


def indicator_direction_consensus(
    profiles: Dict[str, Dict[str, Any]],
    bar_weights: Dict[str, float],
) -> Dict[str, Any]:
    long_weighted = 0.0
    short_weighted = 0.0
    total_weight = 0.0
    bars: Dict[str, Dict[str, Any]] = {}
    for bar, weight in bar_weights.items():
        w = max(0.0, to_float(weight, 0.0))
        if w <= 0:
            continue
        scores = indicator_direction_scores(profiles.get(bar, {}))
        bars[bar] = scores
        long_weighted += scores["long"] * w
        short_weighted += scores["short"] * w
        total_weight += w
    if total_weight <= 0:
        return {
            "long": 0.0,
            "short": 0.0,
            "net": 0.0,
            "strength": 0,
            "direction": "neutral",
            "bars": bars,
            "total_weight": 0.0,
        }
    net = (long_weighted - short_weighted) / total_weight
    strength = int(round(max(long_weighted, short_weighted) / total_weight))
    if net >= 18:
        direction = "long"
    elif net <= -18:
        direction = "short"
    else:
        direction = "neutral"
    return {
        "long": round(long_weighted / total_weight, 2),
        "short": round(short_weighted / total_weight, 2),
        "net": round(net, 2),
        "strength": strength,
        "direction": direction,
        "bars": bars,
        "total_weight": round(total_weight, 2),
    }


def symbol_ccy(inst_id: str) -> str:
    return inst_id.split("-")[0]


def clip_push_text(value: Any, limit: int = 200) -> str:
    text = "" if value is None else str(value).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def display_push_value(value: Any, fallback: Any = None) -> str:
    text = "" if value is None else str(value).strip()
    if text in ("", "-", "None"):
        fallback_text = "" if fallback is None else str(fallback).strip()
        if fallback_text and fallback_text not in ("-", "None"):
            return fallback_text
        return "-"
    return text


SIGNAL_TYPE_LABELS = {
    "volume_spike": "放量",
    "structure_break": "结构突破",
    "boll_squeeze": "布林挤压",
    "rsi_divergence": "RSI背离",
    "rsi_extreme": "RSI极端",
    "macd_momentum_change": "MACD动量",
    "oi_change": "OI异动",
    "funding_hot": "资金费率过热",
    "funding_fast_change": "费率快变",
    "long_short_extreme": "多空极端",
    "order_book_imbalance": "盘口失衡",
}

TRIGGER_REASON_LABELS = {
    "trade_signal": "交易类信号",
    "multi_signal": "多信号共振",
    "sentiment_leading": "情绪领先结构",
    "sentiment_structure_conflict": "情绪与结构冲突",
    "sentiment_signals": "衍生品情绪",
    "raw_score_high": "本地高分",
    "multi_watch": "多观察类信号",
    "scalp_spike": "超短线异动",
    "funding_extreme": "资金费率极端",
}

RISK_PREFERENCE_LABELS = {
    "conservative": "保守",
    "standard": "标准",
    "aggressive": "激进",
}

PUSH_KIND_LABELS = {
    "trade": "结构单",
    "watch": "观察提醒",
    "spike": "急变提醒",
    "forecast": "结构演变",
}

FORECAST_SCENARIO_LABELS = {
    "scalp_transition_up": "3m领先→5m确认多",
    "scalp_transition_down": "3m领先→5m确认空",
    "scalp_momentum_lead_up": "超短动量领先(多)",
    "scalp_momentum_lead_down": "超短动量领先(空)",
    "scalp_compression_release_up": "5m压缩释放(多)",
    "scalp_compression_release_down": "5m压缩释放(空)",
    "short_transition_up": "5m领先→15m确认多",
    "short_transition_down": "5m领先→15m确认空",
    "short_momentum_lead_up": "短线动量领先(多)",
    "short_momentum_lead_down": "短线动量领先(空)",
    "short_compression_release_up": "15m压缩释放(多)",
    "short_compression_release_down": "15m压缩释放(空)",
    "swing_structure_up": "15m领先→1H确认多",
    "swing_structure_down": "15m领先→1H确认空",
    "long_structure_up": "4H领先→1D确认多",
    "long_structure_down": "4H领先→1D确认空",
    "mixed_to_up": "15m mixed→偏多",
    "mixed_to_down": "15m mixed→偏空",
    "profile_lag_up": "15m滞后跟随上涨",
    "profile_lag_down": "15m滞后跟随下跌",
    "developing_momentum_up": "动量延伸酝酿(多)",
    "developing_momentum_down": "动量延伸酝酿(空)",
    "squeeze_release_up": "压缩后偏多释放",
    "squeeze_release_down": "压缩后偏空释放",
    "structure_near_up": "5m/15m即将共振多",
    "structure_near_down": "5m/15m即将共振空",
}

DECISION_SOURCE_LABELS = {
    "ai": "AI前瞻",
    "local": "本地筛查",
    "local_screening": "本地筛查",
    "local_fallback": "本地兜底",
    "structure_forecast": "结构演变",
}


def compact_candles(candles: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    # 给AI看的K线证据：保留最近limit根的时间、OHLC、成交量、是否确认。
    # 这样AI可以独立复核趋势和放量，而不是只相信程序给出的结论。
    return [
        {
            "time": item.get("time"),
            "open": item.get("open"),
            "high": item.get("high"),
            "low": item.get("low"),
            "close": item.get("close"),
            "volume": item.get("volume"),
            "confirmed": item.get("confirmed"),
        }
        for item in candles[:limit]
    ]


def history_tail(history: Deque[Tuple[float, float]], limit: int) -> List[Dict[str, Any]]:
    # 给AI看的时间序列证据，用于复核OI和资金费率变化是否可信。
    return [
        {
            "time": datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S"),
            "value": value,
        }
        for ts, value in list(history)[-limit:]
    ]


def tail_file_text(path: Path, max_bytes: int) -> str:
    if not path.exists():
        return ""
    size = path.stat().st_size
    with path.open("rb") as file:
        if size > max_bytes:
            file.seek(size - max_bytes)
        return file.read().decode("utf-8", errors="replace")


def env_float(name: str, default: float) -> float:
    return to_float(os.getenv(name), default)


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def format_duration_zh(seconds: float) -> str:
    total = int(max(0, seconds))
    if total < 60:
        return f"{total} 秒"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes} 分 {secs} 秒" if secs else f"{minutes} 分钟"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} 小时 {minutes} 分" if minutes else f"{hours} 小时"
    days, hours = divmod(hours, 24)
    return f"{days} 天 {hours} 小时" if hours else f"{days} 天"


AI_ABNORMAL_KIND_LABELS = {
    "request_failed": "AI 请求失败",
    "circuit_open": "AI 熔断中",
    "probe_failed": "AI 探活失败",
    "config_missing": "AI 密钥未配置",
    "package_missing": "openai 包未安装",
}


class OkxAiShortTermAssistant:
    """OKX短线助手主类。

    流程：采集 -> 本地触发预筛 -> 按需 AI 深分析 -> merge final_decision -> 推送/跟踪/写日志。
    本地 score 仅作触发参考与 AI 不可用时的 fallback；推送与 Web 展示以 final_decision 为准。
    """

    def __init__(
        self,
        instruments: List[str],
        interval: int,
        flag: str,
        ai_enabled: bool,
        push_enabled: bool,
        push_score: int,
        short_push_score: int,
        dry_run_ai: bool,
        config: SignalConfig,
        runtime_config: RuntimeConfig,
    ) -> None:
        self.instruments = instruments
        self.interval = interval
        self.flag = flag
        self.ai_enabled = ai_enabled
        self.push_enabled = push_enabled
        self.push_score = push_score
        self.short_push_score = short_push_score
        self.dry_run_ai = dry_run_ai
        self.config = config
        self.runtime_config = runtime_config

        # 优先使用OKX官方python-okx SDK；未安装时自动使用标准库REST兜底。
        # 这样交付时可以选择“SDK稳定优先”，也可以在极简环境里先跑通。
        self.market_api = MarketData.MarketAPI(flag=flag) if MarketData else None
        self.public_api = PublicData.PublicAPI(flag=flag) if PublicData else None

        # OI、资金费率和动态阈值需要看“时间窗口”，不是看轮询次数。
        # 旧版5秒轮询每次都写样本，会把60秒缓存数据重复写12次，分位数会被重复值稀释。
        # 现在统一按约1分钟记录一个有效样本，并保留约3小时窗口：
        # 1. 15m变化判断有足够完整的前后数据；
        # 2. 动态阈值不只受最近二三十分钟影响；
        # 3. 仍然保持内存很小，适合Windows本地长期运行。
        self.oi_history: Dict[str, Deque[Tuple[float, float]]] = defaultdict(lambda: deque(maxlen=METRIC_HISTORY_MAXLEN))
        self.funding_history: Dict[str, Deque[Tuple[float, float]]] = defaultdict(lambda: deque(maxlen=METRIC_HISTORY_MAXLEN))
        self.volume_multiplier_history: Dict[str, Deque[Tuple[float, float]]] = defaultdict(lambda: deque(maxlen=METRIC_HISTORY_MAXLEN))
        self.atr_pct_history: Dict[str, Deque[Tuple[float, float]]] = defaultdict(lambda: deque(maxlen=METRIC_HISTORY_MAXLEN))
        self.book_imbalance_history: Dict[str, Deque[Tuple[float, float]]] = defaultdict(lambda: deque(maxlen=METRIC_HISTORY_MAXLEN))

        # REST缓存用于降低请求量。例如OI、资金费率、多空比不必每5秒都请求。
        # key -> (timestamp, value)
        self.cache: Dict[str, Tuple[float, Any]] = {}
        self.cache_lock = threading.Lock()
        # 数据源短时异常时只允许回退到最近有效值，并明确标记 stale，避免把0写入历史。
        self.last_valid_market_data: Dict[str, Tuple[float, Any]] = {}
        self._market_history_restored = False

        # 推送冷却状态。key 为 push_kind:inst_id:direction，避免信号组合变化绕过冷却。
        self.last_push_at: Dict[str, float] = {}
        self.last_wechat_push_at: Dict[str, float] = {}

        # 最近一次 trade 推送方向，用于短窗内禁止反向 trade。
        self.last_trade_push_at: Dict[str, Tuple[str, float]] = {}

        # 在线信号追踪状态。它不是下单，也不改变推送逻辑，只把系统给出的观察信号当作样本，
        # 在后续5m/15m/1H用真实价格结算表现，逐步积累胜率、平均收益、最大顺向/逆向波动。
        self.pending_signal_reviews: List[Dict[str, Any]] = []
        self.signal_performance: Dict[str, Dict[str, Any]] = {}
        self.last_signal_track_at: Dict[str, float] = {}
        self._load_signal_performance()

        self.calibration_state: Dict[str, Any] = load_calibration_state()
        restored_forecasts = self.calibration_state.get("pending_forecasts")
        self.pending_forecast_reviews: List[Dict[str, Any]] = (
            [dict(item) for item in restored_forecasts if isinstance(item, dict)]
            if isinstance(restored_forecasts, list)
            else []
        )
        self.pending_decision_reviews: List[Dict[str, Any]] = []
        self._calibration_dirty = False
        self._last_calibration_save_at = 0.0
        self.last_forecast_track_at: Dict[str, float] = {}

        # 方向跟单模拟账户：监控/回放会话开始时重置为 $10,000，按 final_direction 满仓跟单。
        self.paper_session_started_at = ""
        self.paper_accounts: Dict[str, Dict[str, Any]] = {}

        # 中线方向记忆：上一轮 final_direction，用于持多后按跌幅转空/观望。
        self._direction_memory: Dict[str, str] = {}

        # AI 连接状态：请求重试 + client 重建 + 熔断探活。
        self._ai_client: Any = None
        self._ai_client_config: Tuple[str, str] = ("", "")
        self.ai_fail_streak = 0
        self.ai_circuit_open_until = 0.0
        self.ai_last_probe_at = 0.0
        self.ai_abnormal_since = 0.0
        self.ai_abnormal_kind = ""
        self.ai_last_failure_reason = ""
        self.ai_abnormal_alert_at = 0.0
        self._last_runtime_cache_prune_at = 0.0
        self.last_ai_call_at: Dict[str, float] = {}
        self.last_ai_fingerprint: Dict[str, str] = {}
        self.replay_ai_cache: Dict[str, Dict[str, Any]] = {}

        # 回放/录制：录制保存 collect_snapshot 原始输入；回放时注入同一套 collect/analyze 链路。
        self.replay_mode = False
        self.replay_log_file = LOG_FILE
        self.record_replay_file: Optional[Path] = None
        self.replay_frame: Optional[Dict[str, Any]] = None
        self.replay_now_ts: Optional[float] = None
        self._record_replay_meta_written = False

    def _now_ts(self) -> float:
        if self.replay_now_ts is not None:
            return self.replay_now_ts
        return time.time()

    def _now_text(self) -> str:
        if self.replay_now_ts is not None:
            return datetime.fromtimestamp(self.replay_now_ts).strftime("%Y-%m-%d %H:%M:%S")
        return now_text()

    def _set_replay_clock(self, time_text: str) -> None:
        self.replay_now_ts = parse_time_text(time_text).timestamp()

    def _replay_source(self, inst_id: str) -> Optional[Dict[str, Any]]:
        frame = self.replay_frame
        if not frame or frame.get("inst_id") != inst_id:
            return None
        return frame

    def _ensure_replay_meta(self) -> None:
        if not self.record_replay_file or self._record_replay_meta_written:
            return
        self.record_replay_file.parent.mkdir(parents=True, exist_ok=True)
        if self.record_replay_file.exists() and self.record_replay_file.stat().st_size > 0:
            self._record_replay_meta_written = True
            return
        meta = {
            "type": "meta",
            "version": REPLAY_DATASET_VERSION,
            "recorded_at": now_text(),
            "interval_seconds": self.interval,
            "inst_ids": list(self.instruments),
            "source": "live-collect_snapshot",
        }
        with self.record_replay_file.open("w", encoding="utf-8") as file:
            file.write(json.dumps(meta, ensure_ascii=False) + "\n")
        self._record_replay_meta_written = True

    def _append_replay_frame(
        self,
        inst_id: str,
        ticker: Dict[str, float],
        candles: Dict[str, List[Dict[str, Any]]],
        open_interest: float,
        funding_rate: float,
        long_short: Dict[str, float],
        order_book: Dict[str, Any],
        source_meta: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> None:
        if not self.record_replay_file:
            return
        rotate_replay_dataset_if_needed(self.record_replay_file)
        try:
            if not self.record_replay_file.exists() or self.record_replay_file.stat().st_size == 0:
                self._record_replay_meta_written = False
        except OSError:
            self._record_replay_meta_written = False
        self._ensure_replay_meta()
        frame = {
            "type": "frame",
            "time": self._now_text(),
            "inst_id": inst_id,
            "ticker": deep_copy_json(ticker),
            "candles": deep_copy_json(candles),
            "open_interest": open_interest,
            "funding_rate": funding_rate,
            "long_short_ratio": deep_copy_json(long_short),
            "order_book": deep_copy_json(order_book),
            "data_sources": deep_copy_json(source_meta or {}),
        }
        with self.record_replay_file.open("a", encoding="utf-8") as file:
            file.write(json.dumps(frame, ensure_ascii=False) + "\n")

    def _cache_saved_at(self, cache_key: str) -> float:
        with self.cache_lock:
            cached = self.cache.get(cache_key)
        return float(cached[0]) if cached else 0.0

    def _collect_market_source(
        self,
        *,
        source_key: str,
        cache_key: str,
        loader: Any,
        validator: Any,
        stale_after_seconds: float,
    ) -> Tuple[Any, Dict[str, Any]]:
        started = self._now_ts()
        value: Any = None
        error = ""
        try:
            value = loader()
        except Exception as exc:
            error = str(exc)

        valid = False
        if not error:
            try:
                valid = bool(validator(value))
            except Exception as exc:
                error = f"validation failed: {exc}"

        observed_at = self._cache_saved_at(cache_key) if not self.replay_mode else self._now_ts()
        if observed_at <= 0:
            observed_at = self._now_ts()
        cache_hit = bool(not self.replay_mode and observed_at < started - 0.001)
        fallback = False

        if valid:
            self.last_valid_market_data[source_key] = (observed_at, deep_copy_json(value))
        else:
            previous = self.last_valid_market_data.get(source_key)
            if previous and self._now_ts() - previous[0] <= stale_after_seconds:
                observed_at, value = previous[0], deep_copy_json(previous[1])
                valid = True
                fallback = True

        age_seconds = max(0.0, self._now_ts() - observed_at)
        available = bool(valid)
        stale = fallback or age_seconds > stale_after_seconds
        meta = {
            "available": available,
            "fresh": available and not stale,
            "stale": stale,
            "fallback": fallback,
            "cache_hit": cache_hit,
            "observed_at": datetime.fromtimestamp(observed_at).strftime("%Y-%m-%d %H:%M:%S"),
            "age_seconds": round(age_seconds, 3),
            "latency_ms": round(max(0.0, self._now_ts() - started) * 1000, 1),
            "error": error,
        }
        return value, meta

    def _snapshot_quality(
        self,
        source_meta: Dict[str, Dict[str, Any]],
        *,
        started_at: float,
        finished_at: float,
    ) -> Dict[str, Any]:
        critical_by_mode = {
            "scalp": ("ticker", "candles.1m", "candles.3m", "candles.5m"),
            "short": ("ticker", "candles.1m", "candles.5m", "candles.15m"),
            "swing": ("ticker", "candles.15m", "candles.1H", "candles.4H"),
            "long": ("ticker", "candles.4H", "candles.1D", "candles.1W"),
        }
        critical = critical_by_mode.get(self._strategy_mode(), critical_by_mode["short"])
        strategy = self._strategy_profile()
        relevant_bars = set(strategy.get("primary_bars", ())) | set(strategy.get("confirm_bars", ())) | set(strategy.get("background_bars", ()))
        relevant_keys = {
            key
            for key in source_meta
            if not key.startswith("candles.") or key.split(".", 1)[1] in relevant_bars
        }
        relevant_keys.update(critical)
        unavailable = sorted(key for key, item in source_meta.items() if key in relevant_keys and not item.get("available"))
        stale = sorted(key for key, item in source_meta.items() if key in relevant_keys and item.get("stale"))
        critical_missing = [key for key in critical if key in unavailable]
        ages = [
            to_float(item.get("age_seconds"))
            for key, item in source_meta.items()
            if key in relevant_keys and item.get("available")
        ]
        max_age = max(ages) if ages else 0.0
        observed = []
        for key, item in source_meta.items():
            if key not in relevant_keys:
                continue
            if not item.get("available"):
                continue
            try:
                observed.append(parse_time_text(str(item.get("observed_at"))).timestamp())
            except Exception:
                continue
        skew_seconds = max(observed) - min(observed) if len(observed) >= 2 else 0.0
        if critical_missing:
            overall = "insufficient"
        elif unavailable or stale or skew_seconds > 75:
            overall = "partial"
        else:
            overall = "sufficient"
        warnings = []
        if critical_missing:
            warnings.append(f"critical_missing={','.join(critical_missing)}")
        if unavailable:
            warnings.append(f"unavailable={','.join(unavailable)}")
        if stale:
            warnings.append(f"stale={','.join(stale)}")
        if skew_seconds > 75:
            warnings.append(f"source_time_skew={skew_seconds:.1f}s")
        return {
            "overall": overall,
            "is_reliable": overall == "sufficient",
            "critical_missing": critical_missing,
            "unavailable_sources": unavailable,
            "stale_sources": stale,
            "max_source_age_seconds": round(max_age, 3),
            "source_time_skew_seconds": round(skew_seconds, 3),
            "collection_duration_ms": round(max(0.0, finished_at - started_at) * 1000, 1),
            "warnings": warnings,
        }

    def collect_snapshot(self, inst_id: str) -> Dict[str, Any]:
        collection_started_at = self._now_ts()
        specs: Dict[str, Tuple[str, Any, Any, float]] = {
            "ticker": (
                f"ticker:{inst_id}",
                lambda: self._get_ticker(inst_id),
                lambda value: isinstance(value, dict) and to_float(value.get("last")) > 0,
                20.0,
            ),
            "open_interest": (
                f"open_interest:{inst_id}",
                lambda: self._get_open_interest(inst_id),
                lambda value: to_float(value) > 0,
                180.0,
            ),
            "funding_rate": (
                f"funding_rate:{inst_id}",
                lambda: self._get_funding_rate(inst_id),
                lambda value: isinstance(value, (int, float)),
                180.0,
            ),
            "long_short_ratio": (
                f"long_short_ratio:{inst_id}",
                lambda: self._get_long_short_ratio(inst_id),
                lambda value: isinstance(value, dict) and value.get("available"),
                180.0,
            ),
            "order_book": (
                f"order_book:{inst_id}",
                lambda: self._get_order_book(inst_id),
                lambda value: isinstance(value, dict) and value.get("available"),
                20.0,
            ),
        }
        for bar in BAR_CHANNELS:
            specs[f"candles.{bar}"] = (
                f"candles:{inst_id}:{bar}",
                lambda bar=bar: self._get_candles(inst_id, bar),
                lambda value: isinstance(value, list) and len(confirmed_candles(value)) >= 2,
                90.0,
            )

        collected: Dict[str, Any] = {}
        source_meta: Dict[str, Dict[str, Any]] = {}
        worker_count = max(1, min(SNAPSHOT_PARALLEL_WORKERS, len(specs)))
        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="okx-snapshot") as pool:
            futures = {
                pool.submit(
                    self._collect_market_source,
                    source_key=f"{inst_id}:{source_name}",
                    cache_key=cache_key,
                    loader=loader,
                    validator=validator,
                    stale_after_seconds=stale_after,
                ): source_name
                for source_name, (cache_key, loader, validator, stale_after) in specs.items()
            }
            for future in as_completed(futures):
                source_name = futures[future]
                value, meta = future.result()
                collected[source_name] = value
                source_meta[source_name] = meta

        replay_source = self._replay_source(inst_id)
        if replay_source and isinstance(replay_source.get("data_sources"), dict):
            recorded_meta = replay_source["data_sources"]
            for source_name, meta in recorded_meta.items():
                if source_name in source_meta and isinstance(meta, dict):
                    source_meta[source_name] = deep_copy_json(meta)

        ticker = collected.get("ticker") if isinstance(collected.get("ticker"), dict) else {}
        candles = {
            bar: collected.get(f"candles.{bar}") if isinstance(collected.get(f"candles.{bar}"), list) else []
            for bar in BAR_CHANNELS
        }
        open_interest = to_float(collected.get("open_interest"))
        funding_rate = to_float(collected.get("funding_rate"))
        long_short = (
            collected.get("long_short_ratio")
            if isinstance(collected.get("long_short_ratio"), dict)
            else {"long_short_ratio": 0.0, "long_ratio": 0.0, "short_ratio": 0.0, "available": False}
        )
        order_book = (
            collected.get("order_book")
            if isinstance(collected.get("order_book"), dict)
            else {"available": False}
        )
        if not source_meta.get("long_short_ratio", {}).get("fresh"):
            long_short = dict(long_short)
            long_short["available"] = False
        if not source_meta.get("order_book", {}).get("fresh"):
            order_book = dict(order_book)
            order_book["available"] = False
        context_bars = self._strategy_context_bars()
        volume_bar = str(context_bars.get("volume", "1m"))
        volume = self._volume_stats(candles.get(volume_bar, []), volume_bar)
        collection_finished_at = self._now_ts()
        snapshot_quality = self._snapshot_quality(
            source_meta,
            started_at=collection_started_at,
            finished_at=collection_finished_at,
        )

        if self.record_replay_file and not self.replay_mode:
            self._append_replay_frame(
                inst_id,
                ticker,
                candles,
                open_interest,
                funding_rate,
                long_short,
                order_book,
                source_meta=source_meta,
            )

        # 记录当前OI和资金费率。API本身60秒缓存，按分钟写样本即可，避免重复采样污染15m变化和分位数。
        if source_meta.get("open_interest", {}).get("fresh") and open_interest > 0:
            self._remember_metric(self.oi_history[inst_id], open_interest, METRIC_SAMPLE_INTERVAL_SECONDS)
        if source_meta.get("funding_rate", {}).get("fresh"):
            self._remember_metric(self.funding_history[inst_id], funding_rate, METRIC_SAMPLE_INTERVAL_SECONDS)

        oi_source_fresh = bool(source_meta.get("open_interest", {}).get("fresh"))
        funding_source_fresh = bool(source_meta.get("funding_rate", {}).get("fresh"))
        oi_change_pct_15m = self._change_pct_last_minutes(self.oi_history[inst_id], 15) if oi_source_fresh else 0.0
        funding_change_15m = self._change_last_minutes(self.funding_history[inst_id], 15) if funding_source_fresh else 0.0
        derivative_window_minutes = self._strategy_derivative_window_minutes()
        oi_change_pct_strategy = (
            self._change_pct_last_minutes(self.oi_history[inst_id], derivative_window_minutes)
            if oi_source_fresh
            else 0.0
        )
        funding_change_strategy = (
            self._change_last_minutes(self.funding_history[inst_id], derivative_window_minutes)
            if funding_source_fresh
            else 0.0
        )

        # 计算K线走势：confirmed 用于稳定指标；live 用于方向判断（含未收盘K线+最新价）。
        price = to_float(ticker.get("last"))
        profiles = {bar: trend_profile_from_candles(rows, bar) for bar, rows in candles.items()}
        tactical_bars = self._tactical_profile_bars()
        profiles_live = {
            bar: (
                trend_profile_from_candles(rows, bar, tactical=True, live_price=price)
                if bar in tactical_bars
                else profiles.get(bar, {})
            )
            for bar, rows in candles.items()
        }

        # 计算波动强度：高中低，便于后续判断止损止盈区间
        volatility = self._volatility_context(inst_id, profiles)

        # 动态阈值调整：每个币种根据自己的历史来调整阈值
        dynamic_thresholds = self._dynamic_thresholds(
            inst_id,
            volume_bar,
            str(context_bars.get("regime", "15m")),
        )
        market_context = self._market_context(
            price=price,
            candles=candles,
            profiles=profiles,
            profiles_live=profiles_live,
            volume=volume,
            open_interest=open_interest,
            oi_change_pct_15m=oi_change_pct_15m,
            funding_rate=funding_rate,
            funding_change_15m=funding_change_15m,
            long_short=long_short,
            order_book=order_book,
            volatility=volatility,
            dynamic_thresholds=dynamic_thresholds,
            oi_change_pct_strategy=oi_change_pct_strategy,
            funding_change_strategy=funding_change_strategy,
            derivative_window_minutes=derivative_window_minutes,
        )
        market_context["snapshot_quality"] = snapshot_quality.get("overall")
        if snapshot_quality.get("warnings"):
            market_context.setdefault("warnings", []).extend(
                f"快照质量：{warning}" for warning in snapshot_quality["warnings"]
            )

        if source_meta.get(f"candles.{volume_bar}", {}).get("fresh") and volume["average_20"] > 0:
            self._remember_metric(
                self.volume_multiplier_history[self._strategy_metric_key(inst_id, volume_bar)],
                volume["multiplier"],
                METRIC_SAMPLE_INTERVAL_SECONDS,
            )
        volatility_bar = str(volatility.get("bar", "15m"))
        if source_meta.get(f"candles.{volatility_bar}", {}).get("fresh") and volatility["atr_pct"] > 0:
            self._remember_metric(
                self.atr_pct_history[self._strategy_metric_key(inst_id, volatility_bar)],
                volatility["atr_pct"],
                METRIC_SAMPLE_INTERVAL_SECONDS,
            )
        if source_meta.get("order_book", {}).get("fresh"):
            self._remember_metric(self.book_imbalance_history[inst_id], order_book.get("imbalance", 0.0), METRIC_SAMPLE_INTERVAL_SECONDS)

        return {
            "time": self._now_text(),
            "inst_id": inst_id,
            "price": ticker.get("last", 0.0),
            "best_bid": ticker.get("bid_px", 0.0),
            "best_ask": ticker.get("ask_px", 0.0),
            "candles": candles,
            "volume": volume,
            "open_interest": open_interest,
            # 获取这15m内的OI变化率，计算百分比，就是当前oi / 15m前的oi 的百分比
            "oi_change_pct_15m": oi_change_pct_15m,
            "oi_change_pct_strategy": oi_change_pct_strategy,
            "derivative_window_minutes": derivative_window_minutes,
            # 数据是否满足15min的要求，预热是否完成
            "oi_warmup_ready": oi_source_fresh and self._history_ready(self.oi_history[inst_id], WARMUP_MINUTES),
            "oi_strategy_warmup_ready": (
                oi_source_fresh
                and self._history_ready(self.oi_history[inst_id], derivative_window_minutes)
            ),
            "funding_rate": funding_rate,
            # 资金费率相对于15Min前的变化量，就是当前资金费率 - 15Min前的资金费率
            "funding_change": funding_change_15m,
            "funding_change_strategy": funding_change_strategy,
            "funding_warmup_ready": funding_source_fresh and self._history_ready(self.funding_history[inst_id], WARMUP_MINUTES),
            "funding_strategy_warmup_ready": (
                funding_source_fresh
                and self._history_ready(self.funding_history[inst_id], derivative_window_minutes)
            ),
            "long_short_ratio": long_short,
            "order_book": order_book,
            "trend_profiles": profiles,
            "trend_profiles_live": profiles_live,
            "volatility": volatility,
            "dynamic_thresholds": dynamic_thresholds,
            "data_sources": source_meta,
            "snapshot_quality": snapshot_quality,

            # 参考阈值：当动态阈值采样不够时的参考
            "instrument_profile": self._instrument_profile(inst_id),
            "market_context": market_context,
        }

    def detect_signals(self, snapshot: Dict[str, Any]) -> List[Dict[str, Any]]:
        # 信号检测模块只判断“是否值得进一步分析”，不直接决定下单方向。
        # 触发信号后，系统才会把结构化数据交给AI分析，从而降低AI调用成本。
        signals = []
        volume = snapshot["volume"]
        long_ratio = snapshot["long_short_ratio"].get("long_ratio", 0.0)
        short_ratio = snapshot["long_short_ratio"].get("short_ratio", 0.0)
        funding_rate = snapshot["funding_rate"]
        funding_change = to_float(snapshot.get("funding_change_strategy", snapshot.get("funding_change")))
        oi_change = to_float(snapshot.get("oi_change_pct_strategy", snapshot.get("oi_change_pct_15m")))
        dynamic = snapshot.get("dynamic_thresholds", {})
        context = snapshot.get("market_context", {})
        profiles = snapshot.get("trend_profiles", {})
        order_book = snapshot.get("order_book", {})
        sources = snapshot.get("data_sources", {}) if isinstance(snapshot.get("data_sources"), dict) else {}

        def source_fresh(name: str) -> bool:
            meta = sources.get(name)
            # 兼容旧日志/测试快照：没有元数据时沿用原行为。
            return True if not isinstance(meta, dict) else bool(meta.get("fresh"))

        # 放量阈值采用“用户配置”和“近期分位数”里的较高者。
        # 这样低波动环境仍尊重用户阈值，高波动环境则自动抬高门槛，减少普通波动被误判成强信号。
        volume_threshold = max(self.config.volume_multiplier, dynamic.get("volume_multiplier_p85", 0.0))
        volume_bar = str(volume.get("source_bar", "1m") or "1m")
        if source_fresh(f"candles.{volume_bar}") and volume["multiplier"] >= volume_threshold:
            # 放量只表示交易活跃度提高，不直接代表涨跌方向，必须结合K线结构、OI和盘口。
            signals.append({
                "type": "volume_spike",
                "desc": f"confirmed {volume_bar} volume multiplier {volume['multiplier']:.2f}x >= {volume_threshold:.2f}x",
                "strength": "high" if volume["multiplier"] >= dynamic.get("volume_multiplier_p95", volume_threshold * 1.5) else "normal",
            })

        # 结构突破比单纯up/down更有交易意义，但缺少放量或盘口配合时容易是假突破。
        score_bars = self._strategy_score_bars()
        signal_primary_bar = score_bars["primary"]
        signal_entry_bar = score_bars["entry"]
        primary_profile = profiles.get(signal_primary_bar, {})
        entry_profile = profiles.get(signal_entry_bar, {})
        breakout_15m = primary_profile.get("breakout")
        breakout_5m = entry_profile.get("breakout")
        rsi_15m = to_float(primary_profile.get("rsi", {}).get("14"), 50.0)
        macd_15m = primary_profile.get("macd", {})
        boll_15m = primary_profile.get("boll", {})
        adx_15m = to_float(primary_profile.get("adx", {}).get("adx"))
        if (
            (source_fresh(f"candles.{signal_primary_bar}") and breakout_15m in ("up", "down"))
            or (source_fresh(f"candles.{signal_entry_bar}") and breakout_5m in ("up", "down"))
        ):
            signals.append({
                "type": "structure_break",
                "desc": f"structure breakout {signal_entry_bar}={breakout_5m} {signal_primary_bar}={breakout_15m}",
                "direction_hint": "做多" if "up" in (breakout_5m, breakout_15m) else "做空",
            })

        if source_fresh(f"candles.{signal_primary_bar}") and context.get("regime") == "squeeze":
            signals.append({
                "type": "boll_squeeze",
                "desc": f"15m boll squeeze bandwidth={to_float(boll_15m.get('bandwidth_pct')):.4f}% adx={adx_15m:.2f}",
            })

        if source_fresh(f"candles.{signal_primary_bar}") and primary_profile.get("divergence") in ("bearish", "bullish"):
            signals.append({
                "type": "rsi_divergence",
                "desc": f"{signal_primary_bar} RSI {primary_profile.get('divergence')} divergence",
            })

        if source_fresh(f"candles.{signal_primary_bar}") and (rsi_15m >= 80 or rsi_15m <= 20):
            signals.append({
                "type": "rsi_extreme",
                "desc": f"15m RSI extreme {rsi_15m:.2f}",
            })

        if source_fresh(f"candles.{signal_primary_bar}") and abs(to_float(macd_15m.get("hist_slope"))) > abs(to_float(macd_15m.get("hist"))) * 0.25 and abs(to_float(macd_15m.get("hist"))) > 0:
            signals.append({
                "type": "macd_momentum_change",
                "desc": f"15m MACD hist={to_float(macd_15m.get('hist')):.4f} slope={to_float(macd_15m.get('hist_slope')):.4f}",
            })

        oi_ready = bool(snapshot.get("oi_strategy_warmup_ready", snapshot.get("oi_warmup_ready")))
        funding_ready = bool(snapshot.get("funding_strategy_warmup_ready", snapshot.get("funding_warmup_ready")))
        if source_fresh("open_interest") and oi_ready and abs(oi_change) >= self.config.oi_change_pct_15m:
            # 持仓率判断：OI变化表示合约持仓量变化，配合价格可以判断新开仓或平仓压力。
            signals.append({
                "type": "oi_change",
                "desc": (
                    f"{snapshot.get('derivative_window_minutes', 15)}m OI change "
                    f"{oi_change:.2f}% ({context.get('oi_price_state', 'unknown')})"
                ),
            })

        if source_fresh("funding_rate") and abs(funding_rate) >= self.config.funding_abs_threshold:
            # 当前资金费率判断：资金费率过热说明多空某一侧过于拥挤，追单风险会提高。
            signals.append({
                "type": "funding_hot",
                "desc": f"funding rate {funding_rate:.6f}",
            })

        if source_fresh("funding_rate") and funding_ready and abs(funding_change) >= self.config.funding_change_threshold:
            # 当前资金费率变化量判断：资金费率快速变化代表市场情绪在短时间内切换。
            signals.append({
                "type": "funding_fast_change",
                "desc": (
                    f"{snapshot.get('derivative_window_minutes', 15)}m funding change "
                    f"{funding_change:.6f}"
                ),
            })

        if source_fresh("long_short_ratio") and long_ratio >= self.config.long_short_extreme:
            # 多头占比过高，继续做多的拥挤风险会提高。
            signals.append({
                "type": "long_short_extreme",
                "desc": f"long ratio {long_ratio:.2%}",
            })
        elif source_fresh("long_short_ratio") and short_ratio >= self.config.long_short_extreme:
            # 空头占比过高，继续做空的拥挤风险会提高。
            signals.append({
                "type": "long_short_extreme",
                "desc": f"short ratio {short_ratio:.2%}",
            })

        # 盘口不平衡只做短线确认。买盘大于卖盘并不保证上涨，但可以提高突破/回踩的入场质量。
        if source_fresh("order_book") and order_book.get("available") and abs(order_book.get("imbalance", 0.0)) >= max(0.35, dynamic.get("book_imbalance_p85", 0.35)):
            signals.append({
                "type": "order_book_imbalance",
                "desc": f"top20 book imbalance {order_book.get('imbalance', 0.0):.2f} ({context.get('order_book_bias', 'neutral')})",
            })

        return signals

    def score_snapshot(self, snapshot: Dict[str, Any], signals: List[Dict[str, Any]]) -> Dict[str, Any]:
        # 评分系统满分100分。新版不再把“多周期多数上涨/下跌”直接当交易方向，
        # 而是先识别市场状态，再给出方向倾向、入场质量、失效条件。
        # 评分仍然只用于“提醒强弱”和“是否推送”，不是自动交易指令。

        # 趋势判断：短周期用盘中实时价，避免等 15m 收盘才翻转。
        live_price = to_float(snapshot.get("price"))
        candles_map = snapshot.get("candles", {})
        tactical_bars = {"1m", "3m", "5m", "15m"}
        trends = {
            bar: trend_from_candles(
                candles_map.get(bar, []),
                tactical=(bar in tactical_bars and live_price > 0),
                live_price=live_price,
            )
            for bar in ("1m", "3m", "5m", "15m", "1H", "4H", "1D", "1W")
        }

        profiles = self._direction_profiles(snapshot)
        inst_id = str(snapshot.get("inst_id", "") or "")
        prior_direction = self._prior_direction(inst_id)
        context = dict(snapshot.get("market_context", {}))
        context["prior_direction"] = prior_direction
        if not isinstance(context.get("indicator_consensus"), dict):
            context["indicator_consensus"] = self._htf_indicator_consensus(profiles)
        if not isinstance(context.get("indicator_alignment"), dict):
            context["indicator_alignment"] = self._htf_indicator_alignment_flags(profiles)
        sentiment_meta = self._sentiment_direction_meta(snapshot, context)
        context["sentiment_meta"] = sentiment_meta
        snapshot["market_context"] = context
        volatility = snapshot.get("volatility", {})
        signal_types = {item["type"] for item in signals}
        strategy_profile = self._strategy_profile()
        raw_direction, direction_tier, direction_summary = self._raw_direction_meta_for_mode(snapshot, context)
        layer_scores = self._layer_scores(snapshot, signals, raw_direction, trends)
        trend_score = min(50, layer_scores["trend_score"] + layer_scores["momentum_score"])
        capital_score = min(30, layer_scores["volume_price_score"] + layer_scores["derivatives_score"] + layer_scores["orderbook_score"])
        risk_control_score = min(100, int(round(layer_scores["risk_control_score"] / 14 * 100)))
        entry_quality_score = min(100, int(round(layer_scores["entry_quality_score"] / 14 * 100)))
        directional_points = sum(
            layer_scores[key]
            for key in (
                "market_regime_score",
                "trend_score",
                "momentum_score",
                "volume_price_score",
                "derivatives_score",
                "orderbook_score",
            )
        )
        direction_score = max(0, min(100, int(round(directional_points / 74 * 100))))
        execution_score = entry_quality_score
        risk_score = risk_control_score
        raw_total_score = max(
            0,
            min(100, int(round(direction_score * 0.65 + execution_score * 0.25 + risk_score * 0.10))),
        )

        # 价位建议基于ATR、结构高低点和EMA/VWAP近似，不再使用固定百分比。
        # 如果市场状态不清晰、入场质量差，会返回观望和明确等待条件。
        final_direction = raw_direction
        direction_guard = self._direction_guard(raw_direction, context)
        if direction_guard:
            final_direction = "\u89c2\u671b"
        entry_plan = self._strategy_entry_plan(snapshot, final_direction)
        sentiment_led = (
            raw_direction in ("做多", "做空")
            and sentiment_meta.get("direction") == raw_direction
            and int(sentiment_meta.get("strength", 0) or 0) >= 3
        )
        downgraded_by_quality = self._should_downgrade_direction(
            direction_guard,
            entry_plan,
            direction_score,
            sentiment_led=sentiment_led,
        )
        fast_price_led = (
            direction_tier in FAST_PRICE_DIRECTION_TIERS
            and raw_direction in ("做多", "做空")
            and (
                (raw_direction == "做空" and context.get("recent_price_pressure") == "down")
                or (raw_direction == "做多" and context.get("recent_price_pressure") == "up")
            )
        )
        if downgraded_by_quality and fast_price_led:
            downgraded_by_quality = False
        if downgraded_by_quality:
            final_direction = "观望"
            entry_plan = self._strategy_entry_plan(snapshot, final_direction)
        final_trade_score = (
            max(0, min(100, int(round(direction_score * 0.70 + execution_score * 0.30))))
            if final_direction in ("做多", "做空")
            else 0
        )

        score = {
            "trend_score": trend_score,
            "capital_score": capital_score,
            # risk_score保留给旧Web/日志兼容；新版请优先看risk_control_score和entry_quality_score。
            "risk_score": risk_score,
            "risk_control_score": risk_control_score,
            "entry_quality_score": entry_quality_score,
            "direction_score": direction_score,
            "execution_score": execution_score,
            "raw_total_score": raw_total_score,
            "final_trade_score": final_trade_score,
            "total_score": final_trade_score if final_direction == "观望" else raw_total_score,
            "layer_scores": layer_scores,
            "raw_direction": raw_direction,
            "final_direction": final_direction,
            "direction": final_direction,
            "direction_tier": direction_tier,
            "direction_summary": direction_summary,
            "entry": entry_plan["entry"],
            "stop_loss": entry_plan["stop_loss"],
            "take_profit": entry_plan["take_profit"],

            # 风险等级：高、中、低
            "market_risk_level": self._market_risk_level(risk_score, signals),
            "trade_action_level": self._trade_action_level(final_trade_score, final_direction, entry_plan),
            # risk_level保留兼容旧Web/日志；新版请优先看market_risk_level/trade_action_level。
            "risk_level": self._market_risk_level(risk_score, signals),
            "trends": trends,
            "market_regime": context.get("regime", "unknown"),
            "bias": context.get("bias", "neutral"),
            "structural_bias": context.get("structural_bias", context.get("bias", "neutral")),
            "trend_phase": context.get("trend_phase", "transition"),
            "snapshot_quality": snapshot.get("snapshot_quality", {}),
            "entry_plan": entry_plan,
            "direction_guard": direction_guard,
            "direction_downgraded": downgraded_by_quality,
            "confidence": direction_score,
            "sentiment_meta": sentiment_meta,
            "strategy_mode": strategy_profile["mode"],
            "strategy_label": strategy_profile["label"],
            "risk_preference": self._risk_preference(),
            "ai_output_style": self._ai_output_style(),
        }
        strategy_views = self._strategy_views(snapshot, signals, score)
        score["strategy_views"] = strategy_views
        selected = strategy_views.get(strategy_profile["mode"], {})
        score["selected_strategy_view"] = selected
        if selected and strategy_profile["mode"] == self._strategy_mode():
            self._apply_selected_strategy_view(score, strategy_profile, selected)
        score["structure_forecast"] = self._finalize_structure_forecast(
            snapshot,
            self._evaluate_structure_evolution(snapshot, signals, score),
        )
        self._remember_direction(inst_id, final_direction)
        score["prior_direction"] = prior_direction
        return score

    def _layer_scores(
        self,
        snapshot: Dict[str, Any],
        signals: List[Dict[str, Any]],
        direction: str,
        trends: Dict[str, str],
    ) -> Dict[str, int]:
        # 七层评分用于替代旧版“趋势/资金/风险”三段式。
        # 每一层只处理自己负责的问题，避免一个指标在多个地方重复加分导致假自信。
        profiles = self._direction_profiles(snapshot)
        context = snapshot.get("market_context", {})
        volume = snapshot.get("volume", {})
        order_book = snapshot.get("order_book", {})
        long_short = snapshot.get("long_short_ratio", {})
        signal_types = {item["type"] for item in signals}
        funding = to_float(snapshot.get("funding_rate"))
        oi_change = to_float(snapshot.get("oi_change_pct_strategy", snapshot.get("oi_change_pct_15m")))
        recent_pressure = context.get("recent_price_pressure", "neutral")
        snapshot_quality = snapshot.get("snapshot_quality", {}) if isinstance(snapshot.get("snapshot_quality"), dict) else {}
        quality_overall = str(snapshot_quality.get("overall", "sufficient") or "sufficient")
        data_sources = snapshot.get("data_sources", {}) if isinstance(snapshot.get("data_sources"), dict) else {}
        indicator_consensus = context.get("indicator_consensus") if isinstance(context.get("indicator_consensus"), dict) else {}
        alignment = context.get("indicator_alignment") if isinstance(context.get("indicator_alignment"), dict) else {}
        pressure_against_direction = (
            (direction == "\u505a\u591a" and recent_pressure == "down")
            or (direction == "\u505a\u7a7a" and recent_pressure == "up")
        )

        weights = self._strategy_profile().get("score_weights", {})

        # 1. 市场状态层：先回答“现在适不适合交易”。趋势市给基础分，震荡/高波动降低基础分。
        market_regime_score = 6
        if context.get("regime") == "trend_up":
            market_regime_score += 8 if direction == "做多" else (-4 if direction == "做空" else 1)
        elif context.get("regime") == "trend_down":
            market_regime_score += 8 if direction == "做空" else (-4 if direction == "做多" else 1)
        elif context.get("regime") == "squeeze":
            market_regime_score += 4
        elif context.get("regime") in ("range", "mixed"):
            market_regime_score -= 2
        elif context.get("regime") == "high_volatility":
            market_regime_score -= 4
        if quality_overall == "partial":
            market_regime_score -= 2
        elif quality_overall == "insufficient":
            market_regime_score -= 6

        # 2. 趋势层：EMA排列、ADX方向、结构高低点和多周期一致性。
        trend_score = 8
        score_bars = self._strategy_score_bars()
        profile_primary = profiles.get(score_bars["primary"], {})
        profile_higher = profiles.get(score_bars["higher"], {})
        profile_background = profiles.get(score_bars["background"], {})
        profile_momentum = profiles.get(score_bars["momentum"], profile_primary)
        profile_entry = profiles.get(score_bars["entry"], {})
        profile_htf = profiles.get("1H", {}) if self._strategy_mode() in ("short", "swing") else {}
        data_quality = profile_primary.get("data_quality", {})
        adx_primary = profile_primary.get("adx", {})
        if profile_primary.get("trend") == profile_higher.get("trend") and profile_primary.get("trend") in ("up", "down"):
            trend_score += 5
        if direction == "做多" and to_float(adx_primary.get("plus_di")) > to_float(adx_primary.get("minus_di")) and to_float(adx_primary.get("adx")) >= 20:
            trend_score += 4
        if direction == "做空" and to_float(adx_primary.get("minus_di")) > to_float(adx_primary.get("plus_di")) and to_float(adx_primary.get("adx")) >= 20:
            trend_score += 4
        if "structure_break" in signal_types:
            trend_score += 3
        if context.get("regime") in ("range", "mixed") or to_float(adx_primary.get("adx")) < 16:
            trend_score -= 4
        if not data_quality.get("is_reliable", False):
            trend_score -= 3
        if pressure_against_direction and not self._htf_indicator_supports(
            indicator_consensus, direction, alignment=alignment, min_net=24.0,
        ):
            trend_score -= 5
        phase = str(context.get("trend_phase", "transition") or "transition")
        if direction == "做多" and phase == "trend_accelerating_up":
            trend_score += 2
        elif direction == "做空" and phase == "trend_accelerating_down":
            trend_score += 2
        elif direction == "做多" and phase in ("reversal_candidate_down", "breakout_attempt_down"):
            trend_score -= 3
        elif direction == "做空" and phase in ("reversal_candidate_up", "breakout_attempt_up"):
            trend_score -= 3

        ht_factor = weights.get("higher_timeframe", 1.0)
        if ht_factor and direction in ("做多", "做空"):
            trend_1h = profile_higher.get("trend")
            trend_4h = profile_background.get("trend")
            ht_adjust = 0
            if direction == "做多":
                if trend_1h == "up":
                    ht_adjust += 3
                if trend_4h == "up":
                    ht_adjust += 2
                if trend_1h == "down":
                    ht_adjust -= 4
                if trend_4h == "down":
                    ht_adjust -= 2
            elif direction == "做空":
                if trend_1h == "down":
                    ht_adjust += 3
                if trend_4h == "down":
                    ht_adjust += 2
                if trend_1h == "up":
                    ht_adjust -= 4
                if trend_4h == "up":
                    ht_adjust -= 2
            trend_score += int(round(ht_adjust * ht_factor))

        indicator_net = to_float(indicator_consensus.get("net"))
        if direction == "做多" and indicator_net >= 20 and alignment.get("long"):
            trend_score += min(4, int(round(indicator_net * 0.15)))
            momentum_score_bonus = min(3, int(round(indicator_net * 0.12)))
        elif direction == "做空" and indicator_net <= -20 and alignment.get("short"):
            trend_score += min(4, int(round(abs(indicator_net) * 0.15)))
            momentum_score_bonus = min(3, int(round(abs(indicator_net) * 0.12)))
        else:
            momentum_score_bonus = 0

        # 3. 动量层：RSI、MACD、KDJ和K线实体质量，判断趋势有没有“油门”。
        momentum_score = 6 + momentum_score_bonus
        if profile_htf:
            htf_scores = indicator_direction_scores(profile_htf)
            if direction == "做多" and htf_scores["net"] >= 16:
                momentum_score += 1
            elif direction == "做空" and htf_scores["net"] <= -16:
                momentum_score += 1
        rsi_14 = to_float(profile_momentum.get("rsi", {}).get("14"), 50.0)
        macd_values = profile_momentum.get("macd", {})
        kdj_values = profile_entry.get("kdj", {})
        if direction == "做多" and 50 <= rsi_14 <= 72:
            momentum_score += 3
        if direction == "做空" and 28 <= rsi_14 <= 50:
            momentum_score += 3
        if direction == "做多" and to_float(macd_values.get("hist")) > 0 and to_float(macd_values.get("hist_slope")) >= 0:
            momentum_score += 4
        if direction == "做空" and to_float(macd_values.get("hist")) < 0 and to_float(macd_values.get("hist_slope")) <= 0:
            momentum_score += 4
        if direction == "做多" and to_float(kdj_values.get("k")) > to_float(kdj_values.get("d")):
            momentum_score += 2
        if direction == "做空" and to_float(kdj_values.get("k")) < to_float(kdj_values.get("d")):
            momentum_score += 2
        if rsi_14 > 80 or rsi_14 < 20:
            momentum_score -= 4
        if profile_momentum.get("divergence") in ("bearish", "bullish"):
            momentum_score -= 3
        if not data_quality.get("macd_ready", False) or not data_quality.get("rsi_ready", False):
            momentum_score -= 2

        # 4. 量价层：成交量方向、趋势、分位阈值和突破确认。
        volume_price_score = 5
        if "volume_spike" in signal_types:
            volume_price_score += 5
        if direction == "做多" and volume.get("direction") == "up":
            volume_price_score += 2
        if direction == "做空" and volume.get("direction") == "down":
            volume_price_score += 2
        if volume.get("trend") == "rising":
            volume_price_score += 2
        if "structure_break" in signal_types and "volume_spike" not in signal_types:
            volume_price_score -= 3

        # 5. 合约资金层：OI+价格组合、资金费率、多空拥挤。
        derivatives_score = 6
        oi_state = context.get("oi_price_state")
        if direction == "做多" and oi_state == "price_up_oi_up_new_longs_or_short_pressure":
            derivatives_score += 4
        elif direction == "做空" and oi_state == "price_down_oi_up_new_shorts_or_long_pressure":
            derivatives_score += 4
        elif oi_state in ("price_up_oi_down_short_covering", "price_down_oi_down_long_deleveraging"):
            derivatives_score -= 2
        if abs(oi_change) >= 2 and snapshot.get("oi_strategy_warmup_ready", snapshot.get("oi_warmup_ready")):
            derivatives_score += 2
        sentiment = context.get("sentiment_meta") if isinstance(context.get("sentiment_meta"), dict) else {}
        if snapshot.get("funding_strategy_warmup_ready", snapshot.get("funding_warmup_ready")) and abs(
            funding_change := to_float(snapshot.get("funding_change_strategy", snapshot.get("funding_change")))
        ) >= self.config.funding_change_threshold:
            if direction == "做多" and funding_change < 0:
                derivatives_score += 2
            elif direction == "做空" and funding_change > 0:
                derivatives_score += 2
        if abs(funding) >= self.config.funding_abs_threshold:
            derivatives_score -= 3
        if max(long_short.get("long_ratio", 0.0), long_short.get("short_ratio", 0.0)) >= self.config.long_short_extreme:
            if sentiment.get("direction") != direction:
                derivatives_score -= 3
            else:
                derivatives_score -= 1
        if isinstance(data_sources.get("open_interest"), dict) and not data_sources["open_interest"].get("fresh"):
            derivatives_score -= 2
        if isinstance(data_sources.get("funding_rate"), dict) and not data_sources["funding_rate"].get("fresh"):
            derivatives_score -= 2

        # 6. 盘口层：top5和top20同时支持时才显著加分，价差过宽则扣分。
        orderbook_score = 4
        if order_book.get("available"):
            if direction == "做多" and context.get("order_book_bias") == "bid_support":
                orderbook_score += 3
            if direction == "做空" and context.get("order_book_bias") == "ask_pressure":
                orderbook_score += 3
            if abs(order_book.get("imbalance_5", 0.0)) > abs(order_book.get("imbalance", 0.0)) * 1.5:
                orderbook_score -= 1
            if order_book.get("spread_pct", 0.0) > 0.03:
                orderbook_score -= 2
        if isinstance(data_sources.get("order_book"), dict) and not data_sources["order_book"].get("fresh"):
            orderbook_score -= 2

        # 7. 入场质量层：距离EMA20是否过远、ATR止损空间是否可控、市场状态是否需要等待确认。
        entry_quality_score = 8
        distance_atr = abs(to_float(profile_primary.get("distance_to_ema20_atr")))
        if distance_atr <= 1.2:
            entry_quality_score += 4
        elif distance_atr >= 2.2:
            entry_quality_score -= 5
        if context.get("regime") in ("squeeze", "range", "mixed", "high_volatility"):
            entry_quality_score -= 3
        if direction == "观望":
            entry_quality_score -= 4
        if pressure_against_direction:
            entry_quality_score -= 4
        if quality_overall == "partial":
            entry_quality_score -= 2
        elif quality_overall == "insufficient":
            entry_quality_score -= 6

        # 8. 风险控制层：专门表达“风险是否可控”，避免旧risk_score被入场质量混用。
        risk_control_score = 10
        if abs(funding) >= self.config.funding_abs_threshold:
            risk_control_score -= 3
        if max(long_short.get("long_ratio", 0.0), long_short.get("short_ratio", 0.0)) >= self.config.long_short_extreme:
            risk_control_score -= 2
        if context.get("regime") == "high_volatility":
            risk_control_score -= 3
        if profile_momentum.get("divergence") in ("bearish", "bullish"):
            risk_control_score -= 2
        if not data_quality.get("is_reliable", False):
            risk_control_score -= 2
        if quality_overall == "partial":
            risk_control_score -= 2
        elif quality_overall == "insufficient":
            risk_control_score -= 5

        weighted_scores = {
            "market_regime_score": market_regime_score,
            "trend_score": trend_score * weights.get("trend", 1.0),
            "momentum_score": momentum_score * weights.get("momentum", 1.0),
            "volume_price_score": volume_price_score * weights.get("volume_price", 1.0),
            "derivatives_score": derivatives_score * weights.get("derivatives", 1.0),
            "orderbook_score": orderbook_score * weights.get("orderbook", 1.0),
            "entry_quality_score": entry_quality_score,
            "risk_control_score": risk_control_score * weights.get("risk_control", 1.0),
        }
        return {
            "market_regime_score": max(0, min(12, int(round(weighted_scores["market_regime_score"])))),
            "trend_score": max(0, min(16, int(round(weighted_scores["trend_score"])))),
            "momentum_score": max(0, min(12, int(round(weighted_scores["momentum_score"])))),
            "volume_price_score": max(0, min(12, int(round(weighted_scores["volume_price_score"])))),
            "derivatives_score": max(0, min(14, int(round(weighted_scores["derivatives_score"])))),
            "orderbook_score": max(0, min(8, int(round(weighted_scores["orderbook_score"])))),
            "entry_quality_score": max(0, min(14, int(round(weighted_scores["entry_quality_score"])))),
            "risk_control_score": max(0, min(14, int(round(weighted_scores["risk_control_score"])))),
        }

    def _strategy_mode(self) -> str:
        mode = str(getattr(self.config, "strategy_mode", "short") or "short").lower()
        return mode if mode in STRATEGY_PROFILES else "short"

    def _tactical_profile_bars(self) -> Set[str]:
        """参与实时战术画像的周期：方向判断读 live，不等到大周期收盘。"""
        mode = self._strategy_mode()
        if mode == "swing":
            return {"1m", "3m", "5m", "15m", "1H", "4H"}
        if mode == "long":
            return {"4H", "1D", "1W"}
        return {"1m", "3m", "5m", "15m", "1H"}

    def _direction_profiles(self, snapshot: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        live = snapshot.get("trend_profiles_live")
        if isinstance(live, dict) and live:
            return live
        confirmed = snapshot.get("trend_profiles", {})
        return confirmed if isinstance(confirmed, dict) else {}

    def _risk_preference(self) -> str:
        risk = str(getattr(self.config, "risk_preference", "standard") or "standard").lower()
        return risk if risk in ("conservative", "standard", "aggressive") else "standard"

    def _ai_output_style(self) -> str:
        style = str(getattr(self.config, "ai_output_style", "steady") or "steady").lower()
        return style if style in ("steady", "momentum", "trend") else "steady"

    def _risk_adjustment(self) -> float:
        return {"conservative": 1.15, "standard": 1.0, "aggressive": 0.9}.get(self._risk_preference(), 1.0)

    def _direction_confirm_score_floor(self) -> int:
        """风险偏好决定「等待确认」时保留做多/做空所需的最低观察分。"""
        base = {"scalp": 55, "short": 60, "swing": 64, "long": 68}.get(self._strategy_mode(), 60)
        adjustment = {"conservative": 6, "standard": 0, "aggressive": -5}.get(self._risk_preference(), 0)
        return max(45, min(85, base + adjustment))

    def _scalp_move_thresholds(self) -> Tuple[float, float]:
        factor = {"conservative": 1.15, "standard": 1.0, "aggressive": 0.82}.get(self._risk_preference(), 1.0)
        threshold_5m = max(to_float(getattr(self.config, "scalp_move_pct_5m", 0.22)) * factor, 0.01)
        threshold_10m = max(to_float(getattr(self.config, "scalp_move_pct_10m", 0.35)) * factor, 0.01)
        return threshold_5m, threshold_10m

    def _mode_allows_scalp_trade(self) -> bool:
        return self._strategy_mode() == "scalp" or bool(getattr(self.config, "allow_scalp_trade", False))

    def _scalp_raw_direction(self, snapshot: Dict[str, Any], context: Dict[str, Any]) -> str:
        candles = snapshot.get("candles", {})
        profiles = snapshot.get("trend_profiles", {})
        threshold_5m, threshold_10m = self._scalp_move_thresholds()
        move_5m = self._recent_move_pct(candles.get("1m", []), 5)
        move_10m = self._recent_move_pct(candles.get("1m", []), 10)
        drawdown_10m = self._recent_drawdown_pct(candles.get("1m", []), 10)
        drawdown_15m = self._recent_drawdown_pct(candles.get("1m", []), 15)
        rebound_10m = self._recent_rebound_pct(candles.get("1m", []), 10)
        rebound_15m = self._recent_rebound_pct(candles.get("1m", []), 15)
        trend_votes = [profiles.get(bar, {}).get("trend") for bar in ("1m", "3m", "5m")]
        up_votes = trend_votes.count("up")
        down_votes = trend_votes.count("down")
        recent_pressure = context.get("recent_price_pressure", "neutral")
        long_breakout = move_5m >= threshold_5m or move_10m >= threshold_10m or up_votes >= 2
        short_breakdown = move_5m <= -threshold_5m or move_10m <= -threshold_10m or down_votes >= 2
        long_rebound = recent_pressure != "down" and up_votes >= 1 and (
            rebound_10m >= threshold_5m * 0.55 or rebound_15m >= threshold_10m * 0.50
        )
        short_rollover = recent_pressure != "up" and down_votes >= 1 and (
            drawdown_10m <= -threshold_5m * 0.55 or drawdown_15m <= -threshold_10m * 0.50
        )
        long_strength = max(move_5m, move_10m, rebound_10m, rebound_15m)
        short_strength = max(-move_5m, -move_10m, -drawdown_10m, -drawdown_15m)
        direction = "观望"
        if long_breakout or long_rebound:
            direction = "做多"
        if (short_breakdown or short_rollover) and short_strength >= long_strength * 0.85:
            direction = "做空"
        trend_15m = profiles.get("15m", {}).get("trend")
        if (
            direction == "做多"
            and trend_15m == "down"
            and move_5m < threshold_5m * 1.35
            and rebound_10m < threshold_5m * 0.55
        ):
            direction = "观望"
        if (
            direction == "做空"
            and trend_15m == "up"
            and move_5m > -threshold_5m * 1.35
            and drawdown_10m > -threshold_5m * 0.55
        ):
            direction = "观望"
        return direction

    def _htf_indicator_bar_weights(self) -> Dict[str, float]:
        mode = self._strategy_mode()
        if mode == "swing":
            return {"1H": 1.5, "4H": 1.8}
        if mode == "long":
            return {"4H": 1.4, "1D": 1.8, "1W": 1.2}
        return {"1H": 1.4, "4H": 1.7}

    def _htf_indicator_consensus(self, profiles: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        return indicator_direction_consensus(profiles, self._htf_indicator_bar_weights())

    def _htf_indicator_bar_alignment(
        self,
        profiles: Dict[str, Dict[str, Any]],
        direction: str,
        *,
        min_bar_net: float = 10.0,
    ) -> bool:
        expected = "long" if direction == "做多" else "short"
        matched = 0
        required = 0
        for bar in self._htf_indicator_bar_weights():
            required += 1
            scores = indicator_direction_scores(profiles.get(bar, {}))
            if scores.get("direction") == expected and abs(to_float(scores.get("net"))) >= min_bar_net:
                matched += 1
        return required > 0 and matched == required

    def _htf_indicator_alignment_flags(self, profiles: Dict[str, Dict[str, Any]]) -> Dict[str, bool]:
        return {
            "long": self._htf_indicator_bar_alignment(profiles, "做多"),
            "short": self._htf_indicator_bar_alignment(profiles, "做空"),
        }

    def _htf_indicator_supports(
        self,
        consensus: Dict[str, Any],
        direction: str,
        *,
        alignment: Optional[Dict[str, bool]] = None,
        min_net: float = 20.0,
    ) -> bool:
        key = "long" if direction == "做多" else "short"
        if alignment is not None and not alignment.get(key):
            return False
        if consensus.get("direction") != key:
            return False
        net = to_float(consensus.get("net"))
        if direction == "做多":
            return net >= min_net
        if direction == "做空":
            return net <= -min_net
        return False

    def _strategy_price_moves(self, snapshot: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, float]:
        candles = snapshot.get("candles", {})
        price = to_float(snapshot.get("price"))
        moves = context.get("recent_move_pct") if isinstance(context.get("recent_move_pct"), dict) else {}
        mode = self._strategy_mode()
        if mode == "swing":
            live = price if price > 0 else 0.0
            return {
                "fast": self._recent_move_pct(candles.get("1m", []), 15, live_price=live),
                "medium": self._recent_move_pct(candles.get("1m", []), 30, live_price=live),
                "slow": self._recent_move_pct(candles.get("1m", []), 45, live_price=live),
            }
        live = price if price > 0 else 0.0
        if live > 0:
            return {
                "fast": self._recent_move_pct(candles.get("1m", []), 3, live_price=live),
                "medium": self._recent_move_pct(candles.get("1m", []), 8, live_price=live),
                "slow": self._recent_move_pct(candles.get("1m", []), 15, live_price=live),
            }
        return {
            "fast": to_float(moves.get("5m"), self._recent_move_pct(candles.get("1m", []), 5)),
            "medium": to_float(moves.get("10m"), self._recent_move_pct(candles.get("1m", []), 10)),
            "slow": to_float(moves.get("15m"), self._recent_move_pct(candles.get("1m", []), 15)),
        }

    def _price_move_floors(self, snapshot: Dict[str, Any]) -> Tuple[float, float]:
        """返回 (short_floor, long_floor)；做空门槛 deliberately 低于做多。"""
        profiles = self._direction_profiles(snapshot)
        factor = self._risk_adjustment()
        if self._strategy_mode() == "swing":
            atr_pct = max(to_float(profiles.get("1H", {}).get("atr_pct")), 0.15)
            short_floor = max(0.10, atr_pct * 0.22) * factor
            long_floor = max(0.18, atr_pct * 0.42) * factor
        else:
            atr_pct = max(to_float(profiles.get("15m", {}).get("atr_pct")), 0.08)
            short_floor = max(0.06, atr_pct * 0.20) * factor
            long_floor = max(0.12, atr_pct * 0.40) * factor
        return short_floor, long_floor

    def _intrabar_move_direction_meta(
        self,
        snapshot: Dict[str, Any],
        context: Dict[str, Any],
    ) -> Optional[Tuple[str, str, str]]:
        """1m 盘中急跌/急涨检测：不等待 15m K 线收盘。"""
        if self._strategy_mode() == "long":
            return None

        price = to_float(snapshot.get("price"))
        if price <= 0:
            return None

        candles_1m = snapshot.get("candles", {}).get("1m", [])
        if len(candles_1m) < 3:
            return None

        pressure = context.get("recent_price_pressure", "neutral")
        short_floor, long_floor = self._price_move_floors(snapshot)
        move_1 = self._recent_move_pct(candles_1m, 1, live_price=price)
        move_3 = self._recent_move_pct(candles_1m, 3, live_price=price)
        move_5 = self._recent_move_pct(candles_1m, 5, live_price=price)
        move_8 = self._recent_move_pct(candles_1m, 8, live_price=price)
        drawdown_5 = self._recent_drawdown_pct(candles_1m, 5, live_price=price)
        drawdown_10 = self._recent_drawdown_pct(candles_1m, 10, live_price=price)
        worst = min(move_1, move_3, move_5, move_8, drawdown_5, drawdown_10)

        prior = str(
            context.get("prior_direction")
            or self._prior_direction(str(snapshot.get("inst_id", "") or ""))
            or "观望"
        )
        profiles = self._direction_profiles(snapshot)
        trend_5m = profiles.get("5m", {}).get("trend")
        trend_15m = profiles.get("15m", {}).get("trend")

        crash_line = -short_floor * 0.28
        drop_line = -short_floor * 0.42
        if self._strategy_mode() == "swing" and prior == "做多":
            watch_drop, short_drop, crash_drop = self._swing_long_to_short_lines(snapshot)
            drop_mag = abs(min(worst, drawdown_5, drawdown_10, 0.0))
            if not self._swing_15m_bearish_aligned(profiles):
                if drop_mag < crash_drop * 1.20:
                    return None
                crash_line = -crash_drop * 1.20
                drop_line = -crash_drop * 1.20
            else:
                if drop_mag < short_drop * 1.02:
                    return None
                crash_line = -crash_drop * 1.05
                drop_line = -short_drop * 1.12

        if worst <= crash_line:
            return (
                "做空",
                "intrabar_crash",
                f"1m盘中急跌约{abs(worst):.2f}%（未等15m收盘），按下跌处理。",
            )
        if pressure == "down" and worst <= drop_line:
            return (
                "做空",
                "intrabar_drop",
                f"盘中回落约{abs(worst):.2f}%，价格领先于{str(trend_15m or '滞后')}结构。",
            )
        if pressure == "down" and worst <= drop_line * 0.82 and trend_5m != "up":
            return (
                "做空",
                "intrabar_drop",
                f"短窗转弱约{abs(worst):.2f}%且5m未确认向上。",
            )

        rebound_5 = self._recent_rebound_pct(candles_1m, 5, live_price=price)
        best = max(move_1, move_3, move_5, rebound_5)
        rally_line = long_floor * 1.05
        if self._strategy_mode() == "swing" and prior in ("做空", "观望"):
            watch_rise, long_rise, surge_rise = self._swing_short_to_long_lines(snapshot)
            rise_mag = max(best, rebound_5, 0.0)
            if not self._swing_15m_bullish_aligned(profiles):
                if rise_mag < surge_rise * 1.20:
                    return None
                rally_line = surge_rise * 1.20
            else:
                if rise_mag < long_rise * 1.02:
                    return None
                rally_line = long_rise * 1.12
        if pressure == "up" and best >= rally_line:
            return (
                "做多",
                "intrabar_rally",
                f"盘中上冲约{best:.2f}%，价格领先于结构。",
            )
        return None

    def _raw_direction_meta_for_mode(
        self,
        snapshot: Dict[str, Any],
        context: Dict[str, Any],
    ) -> Tuple[str, str, str]:
        mode = self._strategy_mode()
        prior = str(
            context.get("prior_direction")
            or self._prior_direction(str(snapshot.get("inst_id", "") or ""))
            or "观望"
        )
        context["prior_direction"] = prior
        profiles = self._direction_profiles(snapshot)

        if mode == "swing" and prior == "做多":
            exit_meta = self._swing_exit_from_long_meta(snapshot, context, profiles)
            if exit_meta:
                return exit_meta

        if mode == "swing" and prior in ("做空", "观望"):
            entry_meta = self._swing_entry_to_long_meta(snapshot, context, profiles, prior)
            if entry_meta:
                return entry_meta

        fast = self._intrabar_move_direction_meta(snapshot, context)
        if fast:
            direction, tier, summary = fast
            if (
                mode == "swing"
                and prior == "做多"
                and direction == "做空"
                and not self._swing_drop_supports_short(snapshot, profiles, context, for_intrabar=True)
            ):
                pass
            elif (
                mode == "swing"
                and prior in ("做空", "观望")
                and direction == "做多"
                and not self._swing_rise_supports_long(snapshot, profiles, context, for_intrabar=True)
            ):
                pass
            else:
                return fast
        if mode == "scalp":
            direction = self._scalp_raw_direction(snapshot, context)
            return direction, "scalp", ""
        if mode == "swing":
            return self._swing_direction_meta(snapshot, context)
        if mode == "long":
            return self._long_direction_meta(snapshot, context)
        return self._short_direction_meta(snapshot, context)

    def _price_led_direction_meta(
        self,
        snapshot: Dict[str, Any],
        context: Dict[str, Any],
    ) -> Optional[Tuple[str, str, str]]:
        """价格已走出的方向优先于滞后指标，修正「跌了仍做多/观望」。"""
        pressure = context.get("recent_price_pressure", "neutral")
        if pressure not in ("up", "down"):
            return None

        moves = self._strategy_price_moves(snapshot, context)
        short_floor, long_floor = self._price_move_floors(snapshot)
        bearish_move = min(moves["fast"], moves["medium"], moves["slow"])
        bullish_move = max(moves["fast"], moves["medium"], moves["slow"])
        profiles = self._direction_profiles(snapshot)
        score_bars = self._strategy_score_bars()
        trend_primary = profiles.get(score_bars["primary"], {}).get("trend")
        trend_entry = profiles.get(score_bars["entry"], {}).get("trend")
        structural = str(context.get("structural_bias", context.get("bias", "neutral")) or "neutral")
        swing_tight = self._strategy_mode() == "swing"
        short_mult = 1.35 if swing_tight else 1.0
        long_mult = 1.25 if swing_tight else 1.0

        if pressure == "down":
            if bearish_move <= -short_floor * 0.32 * short_mult:
                return (
                    "做空",
                    "price_leading",
                    f"短窗压力向下且跌幅约{abs(bearish_move):.2f}%，价格领先于滞后指标。",
                )
            if structural == "long" and bearish_move <= -short_floor * 0.22 * short_mult:
                return (
                    "做空",
                    "price_pullback",
                    "大周期结构仍偏多，但价格已回落，按短窗下跌跟踪。",
                )
            if bearish_move <= -short_floor * 0.55 * short_mult and trend_entry != "up":
                return (
                    "做空",
                    "price_leading",
                    f"价格走弱约{abs(bearish_move):.2f}%且{score_bars['entry']}未确认向上。",
                )

        if pressure == "up":
            if bullish_move >= long_floor * long_mult:
                return (
                    "做多",
                    "price_leading",
                    f"短窗压力向上且涨幅约{bullish_move:.2f}%，价格领先于滞后指标。",
                )
            if bullish_move >= long_floor * 0.90 * long_mult and trend_entry in ("up", "mixed") and trend_primary != "down":
                return (
                    "做多",
                    "price_leading",
                    f"价格上涨约{bullish_move:.2f}%且{score_bars['entry']}配合。",
                )
        return None

    def _prior_direction(self, inst_id: str) -> str:
        prior = self._direction_memory.get(inst_id, "观望")
        return prior if prior in ("做多", "做空", "观望") else "观望"

    def _remember_direction(self, inst_id: str, direction: str) -> None:
        if inst_id and direction in ("做多", "做空", "观望"):
            self._direction_memory[inst_id] = direction

    def _swing_live_move_metrics(self, snapshot: Dict[str, Any]) -> Dict[str, float]:
        live = to_float(snapshot.get("price"))
        candles = snapshot.get("candles", {}).get("1m", [])
        live_price = live if live > 0 else 0.0
        move_10m = self._recent_move_pct(candles, 10, live_price=live_price)
        move_15m = self._recent_move_pct(candles, 15, live_price=live_price)
        move_30m = self._recent_move_pct(candles, 30, live_price=live_price)
        drawdown_8 = self._recent_drawdown_pct(candles, 8, live_price=live_price)
        drawdown_15 = self._recent_drawdown_pct(candles, 15, live_price=live_price)
        rebound_8 = self._recent_rebound_pct(candles, 8, live_price=live_price)
        rebound_15 = self._recent_rebound_pct(candles, 15, live_price=live_price)
        worst = min(move_10m, move_15m, move_30m, drawdown_8, drawdown_15, 0.0)
        best = max(move_10m, move_15m, move_30m, rebound_8, rebound_15, 0.0)
        return {
            "move_10m": move_10m,
            "move_15m": move_15m,
            "move_30m": move_30m,
            "drawdown_8": drawdown_8,
            "drawdown_15": drawdown_15,
            "rebound_8": rebound_8,
            "rebound_15": rebound_15,
            "drop_pct": abs(worst),
            "rise_pct": best,
        }

    def _swing_live_drop_metrics(self, snapshot: Dict[str, Any]) -> Dict[str, float]:
        metrics = self._swing_live_move_metrics(snapshot)
        return {**metrics, "drop_pct": metrics["drop_pct"]}

    def _swing_long_to_short_lines(self, snapshot: Dict[str, Any]) -> Tuple[float, float, float]:
        """持多后转空/观望的跌幅门槛（正数，单位 %）。"""
        short_floor, _ = self._price_move_floors(snapshot)
        threshold_30m, _ = self._swing_move_thresholds(snapshot)
        noise_cap = max(short_floor * 2.35, threshold_30m * 0.78, 0.22)
        watch_drop = noise_cap * 0.92
        short_drop = max(short_floor * 0.68, threshold_30m * 0.88)
        crash_drop = max(short_floor * 0.95, threshold_30m * 1.15)
        return watch_drop, short_drop, crash_drop

    def _swing_short_to_long_lines(self, snapshot: Dict[str, Any]) -> Tuple[float, float, float]:
        """持空/观望后做多的涨幅门槛（正数，单位 %），与跌幅门槛对称。"""
        _, long_floor = self._price_move_floors(snapshot)
        threshold_30m, _ = self._swing_move_thresholds(snapshot)
        noise_cap = max(long_floor * 2.1, threshold_30m * 0.78, 0.22)
        watch_rise = noise_cap * 0.92
        long_rise = max(long_floor * 0.62, threshold_30m * 0.88)
        surge_rise = max(long_floor * 0.92, threshold_30m * 1.15)
        return watch_rise, long_rise, surge_rise

    def _swing_15m_bullish_aligned(self, profiles: Dict[str, Dict[str, Any]]) -> bool:
        trend_15m = profiles.get("15m", {}).get("trend")
        if trend_15m == "up":
            return True
        scores_15m = indicator_direction_scores(profiles.get("15m", {}))
        ema_slope = to_float(profiles.get("15m", {}).get("ema_slope_pct"))
        hist_slope = to_float(profiles.get("15m", {}).get("macd", {}).get("hist_slope"))
        if scores_15m.get("direction") == "long" and abs(to_float(scores_15m.get("net"))) >= 22:
            return True
        if ema_slope > 0.08 and hist_slope > 0.02:
            return True
        return False

    def _swing_15m_bearish_aligned(self, profiles: Dict[str, Dict[str, Any]]) -> bool:
        trend_15m = profiles.get("15m", {}).get("trend")
        if trend_15m == "down":
            return True
        scores_15m = indicator_direction_scores(profiles.get("15m", {}))
        ema_slope = to_float(profiles.get("15m", {}).get("ema_slope_pct"))
        hist_slope = to_float(profiles.get("15m", {}).get("macd", {}).get("hist_slope"))
        if scores_15m.get("direction") == "short" and abs(to_float(scores_15m.get("net"))) >= 22:
            return True
        if ema_slope < -0.08 and hist_slope < -0.02:
            return True
        return False

    def _swing_rise_supports_long(
        self,
        snapshot: Dict[str, Any],
        profiles: Dict[str, Dict[str, Any]],
        context: Dict[str, Any],
        *,
        for_intrabar: bool = False,
    ) -> bool:
        """15m 未同向看多时，需达到合理涨幅才允许做多（过滤低位小振荡）。"""
        metrics = self._swing_live_move_metrics(snapshot)
        rise_pct = metrics["rise_pct"]
        watch_rise, long_rise, surge_rise = self._swing_short_to_long_lines(snapshot)
        pressure = context.get("recent_price_pressure", "neutral")
        scores_15m = indicator_direction_scores(profiles.get("15m", {}))
        ema_slope = to_float(profiles.get("15m", {}).get("ema_slope_pct"))
        soft_bullish = (
            to_float(scores_15m.get("net")) >= 12
            or ema_slope > 0.04
            or self._swing_15m_bullish_aligned(profiles)
        )

        if self._swing_15m_bullish_aligned(profiles):
            required = long_rise * (0.82 if for_intrabar else 0.92)
            return rise_pct >= required and pressure == "up"

        if rise_pct >= surge_rise * 1.18 and pressure == "up":
            return True
        if rise_pct >= long_rise * 1.28 and pressure == "up" and soft_bullish:
            return True
        return False

    def _swing_entry_to_long_meta(
        self,
        snapshot: Dict[str, Any],
        context: Dict[str, Any],
        profiles: Dict[str, Dict[str, Any]],
        prior: str,
    ) -> Optional[Tuple[str, str, str]]:
        """持空/观望后做多：15m 同向可较快转多；否则按涨幅比例区分观望/做多。"""
        if prior == "做多":
            return None

        metrics = self._swing_live_move_metrics(snapshot)
        rise_pct = metrics["rise_pct"]
        watch_rise, long_rise, surge_rise = self._swing_short_to_long_lines(snapshot)
        pressure = context.get("recent_price_pressure", "neutral")
        aligned_15m = self._swing_15m_bullish_aligned(profiles)
        scores_15m = indicator_direction_scores(profiles.get("15m", {}))
        ema_slope = to_float(profiles.get("15m", {}).get("ema_slope_pct"))
        hist_slope = to_float(profiles.get("15m", {}).get("macd", {}).get("hist_slope"))
        soft_bullish = (
            to_float(scores_15m.get("net")) >= 12
            or ema_slope > 0.04
            or (hist_slope > 0.02 and to_float(profiles.get("15m", {}).get("macd", {}).get("hist")) >= 0)
        )

        if rise_pct < watch_rise * 0.80:
            return None

        if profiles.get("4H", {}).get("trend") == "down":
            return None
        if profiles.get("1H", {}).get("trend") == "down" and profiles.get("15m", {}).get("trend") == "down":
            return None

        if aligned_15m:
            if rise_pct >= surge_rise * 1.05 and pressure == "up":
                return (
                    "做多",
                    "swing_entry_long",
                    f"15m已转强且涨幅约{rise_pct:.2f}%，转多。",
                )
            if soft_bullish and rise_pct >= long_rise * 1.02 and pressure == "up":
                return (
                    "做多",
                    "swing_entry_long",
                    f"15m转强且涨幅约{rise_pct:.2f}%，转多。",
                )
            if pressure == "up" and rise_pct >= watch_rise * 1.12:
                return (
                    "观望",
                    "swing_entry_watch",
                    f"15m转强中，涨幅约{rise_pct:.2f}%，先观望。",
                )
            return None

        if rise_pct >= surge_rise * 1.20 and pressure == "up":
            return (
                "做多",
                "swing_entry_long",
                f"15m未同向但涨幅约{rise_pct:.2f}%达强门槛，转多。",
            )
        if rise_pct >= long_rise * 1.28 and pressure == "up" and soft_bullish:
            return (
                "做多",
                "swing_entry_long",
                f"15m未同向但涨幅约{rise_pct:.2f}%且压力转强，转多。",
            )
        if rise_pct >= watch_rise * 1.12:
            return (
                "观望",
                "swing_entry_watch",
                f"涨幅约{rise_pct:.2f}%未达做多门槛，先观望。",
            )
        return None

    def _swing_drop_supports_short(
        self,
        snapshot: Dict[str, Any],
        profiles: Dict[str, Dict[str, Any]],
        context: Dict[str, Any],
        *,
        for_intrabar: bool = False,
    ) -> bool:
        """15m 未同向看空时，需达到合理跌幅才允许做空（过滤高位小振荡）。"""
        metrics = self._swing_live_drop_metrics(snapshot)
        drop_pct = metrics["drop_pct"]
        watch_drop, short_drop, crash_drop = self._swing_long_to_short_lines(snapshot)
        pressure = context.get("recent_price_pressure", "neutral")
        scores_15m = indicator_direction_scores(profiles.get("15m", {}))
        ema_slope = to_float(profiles.get("15m", {}).get("ema_slope_pct"))
        soft_bearish = (
            to_float(scores_15m.get("net")) <= -12
            or ema_slope < -0.04
            or self._swing_15m_bearish_aligned(profiles)
        )

        if self._swing_15m_bearish_aligned(profiles):
            required = short_drop * (0.82 if for_intrabar else 0.92)
            return drop_pct >= required and pressure == "down"

        if drop_pct >= crash_drop * 1.18 and pressure == "down":
            return True
        if drop_pct >= short_drop * 1.28 and pressure == "down" and soft_bearish:
            return True
        return False

    def _swing_exit_from_long_meta(
        self,
        snapshot: Dict[str, Any],
        context: Dict[str, Any],
        profiles: Dict[str, Dict[str, Any]],
    ) -> Optional[Tuple[str, str, str]]:
        """持多后退出：15m 同向看空可较快转空；否则按跌幅比例区分观望/做空。"""
        metrics = self._swing_live_drop_metrics(snapshot)
        drop_pct = metrics["drop_pct"]
        watch_drop, short_drop, crash_drop = self._swing_long_to_short_lines(snapshot)
        pressure = context.get("recent_price_pressure", "neutral")
        aligned_15m = self._swing_15m_bearish_aligned(profiles)
        scores_15m = indicator_direction_scores(profiles.get("15m", {}))
        ema_slope = to_float(profiles.get("15m", {}).get("ema_slope_pct"))
        hist_slope = to_float(profiles.get("15m", {}).get("macd", {}).get("hist_slope"))
        soft_bearish = (
            to_float(scores_15m.get("net")) <= -12
            or ema_slope < -0.04
            or (hist_slope < -0.02 and to_float(profiles.get("15m", {}).get("macd", {}).get("hist")) <= 0)
        )

        if drop_pct < watch_drop * 0.80:
            return None

        if aligned_15m:
            if drop_pct >= crash_drop * 1.05 and pressure == "down":
                return (
                    "做空",
                    "swing_exit_short",
                    f"15m已转弱且跌幅约{drop_pct:.2f}%，多单退出做空。",
                )
            if soft_bearish and drop_pct >= short_drop * 1.02 and pressure == "down":
                return (
                    "做空",
                    "swing_exit_short",
                    f"15m转弱且跌幅约{drop_pct:.2f}%，多单退出做空。",
                )
            if pressure == "down" and drop_pct >= watch_drop * 1.12:
                return (
                    "观望",
                    "swing_exit_watch",
                    f"15m转弱中，跌幅约{drop_pct:.2f}%，先退出观望。",
                )
            return None

        if drop_pct >= crash_drop * 1.20 and pressure == "down":
            return (
                "做空",
                "swing_exit_short",
                f"15m未同向但跌幅约{drop_pct:.2f}%达强门槛，转空。",
            )
        if drop_pct >= short_drop * 1.28 and pressure == "down" and soft_bearish:
            return (
                "做空",
                "swing_exit_short",
                f"15m未同向但跌幅约{drop_pct:.2f}%且压力转弱，转空。",
            )
        if drop_pct >= watch_drop * 1.12:
            return (
                "观望",
                "swing_exit_watch",
                f"高位回落约{drop_pct:.2f}%未达做空门槛，先观望。",
            )
        return None

    def _swing_move_thresholds(self, snapshot: Dict[str, Any]) -> Tuple[float, float]:
        """中线动量阈值：30/60 分钟涨跌幅，结合 1H ATR 与风险偏好。"""
        profiles = snapshot.get("trend_profiles", {})
        atr_pct_1h = max(to_float(profiles.get("1H", {}).get("atr_pct")), 0.20)
        factor = self._risk_adjustment()
        threshold_30m = max(0.18, atr_pct_1h * 0.35) * factor
        threshold_60m = max(0.28, atr_pct_1h * 0.60) * factor
        return threshold_30m, threshold_60m

    def _swing_direction_meta(self, snapshot: Dict[str, Any], context: Dict[str, Any]) -> Tuple[str, str, str]:
        profiles = self._direction_profiles(snapshot)
        candles = snapshot.get("candles", {})
        trend_1h = profiles.get("1H", {}).get("trend")
        trend_4h = profiles.get("4H", {}).get("trend")
        trend_15m = profiles.get("15m", {}).get("trend")
        pressure = context.get("recent_price_pressure", "neutral")
        prior = str(
            context.get("prior_direction")
            or self._prior_direction(str(snapshot.get("inst_id", "") or ""))
            or "观望"
        )

        price_led = self._price_led_direction_meta(snapshot, context)
        if price_led:
            direction, tier, summary = price_led
            if direction == "做空" and prior == "做多":
                if self._swing_drop_supports_short(snapshot, profiles, context):
                    return price_led
                metrics = self._swing_live_drop_metrics(snapshot)
                watch_drop, _, _ = self._swing_long_to_short_lines(snapshot)
                if metrics["drop_pct"] >= watch_drop * 0.92:
                    return (
                        "观望",
                        "swing_exit_watch",
                        f"高位小回落约{metrics['drop_pct']:.2f}%，15m未确认，先观望。",
                    )
            elif direction == "做多" and prior in ("做空", "观望"):
                if self._swing_rise_supports_long(snapshot, profiles, context):
                    return price_led
                metrics = self._swing_live_move_metrics(snapshot)
                watch_rise, _, _ = self._swing_short_to_long_lines(snapshot)
                if metrics["rise_pct"] >= watch_rise * 0.92:
                    return (
                        "观望",
                        "swing_entry_watch",
                        f"低位小反弹约{metrics['rise_pct']:.2f}%，15m未确认，先观望。",
                    )
            else:
                return price_led

        threshold_30m, threshold_60m = self._swing_move_thresholds(snapshot)
        if trend_1h == trend_4h == "up":
            if pressure == "down":
                live = to_float(snapshot.get("price"))
                move_30m = self._recent_move_pct(
                    candles.get("1m", []), 30, live_price=live if live > 0 else 0.0,
                )
                short_floor, _ = self._price_move_floors(snapshot)
                drop_pct = abs(min(move_30m, 0.0))
                _, short_drop, crash_drop = self._swing_long_to_short_lines(snapshot)
                can_short = (
                    move_30m <= -short_floor * 0.55
                    and pressure == "down"
                    and (
                        (self._swing_15m_bearish_aligned(profiles) and drop_pct >= short_drop * 1.05)
                        or drop_pct >= crash_drop * 1.20
                    )
                )
                if can_short:
                    return (
                        "做空",
                        "price_pullback",
                        f"1H/4H仍偏多但短窗回落约{abs(move_30m):.2f}%，按下跌处理。",
                    )
                return "观望", "pullback", "1H/4H结构仍偏多但短窗回落，等待企稳。"
            if not self._swing_15m_bullish_aligned(profiles):
                metrics = self._swing_live_move_metrics(snapshot)
                _, long_rise, surge_rise = self._swing_short_to_long_lines(snapshot)
                live = to_float(snapshot.get("price"))
                move_30m = self._recent_move_pct(
                    candles.get("1m", []), 30, live_price=live if live > 0 else 0.0,
                )
                rise_pct = max(metrics["rise_pct"], move_30m, 0.0)
                if rise_pct >= surge_rise * 1.15 and pressure == "up":
                    return (
                        "做多",
                        "swing_entry_long",
                        f"1H/4H偏多且涨幅约{rise_pct:.2f}%，15m未同向仍跟多。",
                    )
                if rise_pct >= long_rise * 1.20 and pressure == "up":
                    return "观望", "long_wait", "大周期偏多但15m/涨幅未充分确认，暂观望。"
            if self._swing_15m_bullish_aligned(profiles) and pressure != "down":
                return "做多", "aligned", "1H/4H趋势同向偏多，15m已确认，中线关注结构回踩后的延伸机会。"
            return "观望", "long_wait", "1H/4H偏多但15m未确认，暂观望。"
        if trend_1h == trend_4h == "down":
            if pressure == "up":
                live = to_float(snapshot.get("price"))
                move_30m = self._recent_move_pct(
                    candles.get("1m", []), 30, live_price=live if live > 0 else 0.0,
                )
                _, long_floor = self._price_move_floors(snapshot)
                rise_pct = max(move_30m, 0.0)
                _, long_rise, surge_rise = self._swing_short_to_long_lines(snapshot)
                can_long = (
                    move_30m >= long_floor * 0.55
                    and pressure == "up"
                    and (
                        (self._swing_15m_bullish_aligned(profiles) and rise_pct >= long_rise * 1.05)
                        or rise_pct >= surge_rise * 1.20
                    )
                )
                if can_long:
                    return (
                        "做多",
                        "price_rebound",
                        f"1H/4H仍偏空但短窗反弹约{move_30m:.2f}%，按反弹处理。",
                    )
                return "观望", "rebound", "1H/4H结构仍偏空但短窗反弹，等待回落。"
            if self._swing_15m_bearish_aligned(profiles) and pressure != "up":
                return "做空", "aligned", "1H/4H趋势同向偏空，15m已确认，中线关注反抽不过后的延伸机会。"
            return "观望", "short_wait", "1H/4H偏空但15m未确认，暂观望。"

        threshold_30m, threshold_60m = self._swing_move_thresholds(snapshot)
        live = to_float(snapshot.get("price"))
        move_30m = self._recent_move_pct(candles.get("1m", []), 30, live_price=live if live > 0 else 0.0)
        move_60m = self._recent_move_pct(candles.get("1m", []), 60, live_price=live if live > 0 else 0.0)
        pressure = context.get("recent_price_pressure", "neutral")
        trade_up = int(context.get("trade_up", 0) or 0)
        trade_down = int(context.get("trade_down", 0) or 0)
        bias = context.get("bias", "neutral")
        regime = context.get("regime", "")

        watch_rise, long_rise, surge_rise = self._swing_short_to_long_lines(snapshot)
        metrics = self._swing_live_move_metrics(snapshot)
        rise_pct = metrics["rise_pct"]

        long_structural = (
            trend_1h in ("up", "mixed")
            and trend_4h != "down"
            and pressure != "down"
            and (
                (
                    trend_15m == "up"
                    and (trade_up >= 1 or pressure == "up" or bias == "long")
                )
                or (
                    trend_15m != "down"
                    and rise_pct >= long_rise * 1.18
                    and pressure == "up"
                )
            )
        )
        short_structural = (
            trend_1h in ("down", "mixed")
            and trend_15m in ("down", "mixed")
            and trend_4h != "up"
            and (trade_down >= 1 or pressure == "down" or bias == "short")
        )
        long_move = max(move_30m, move_60m)
        short_move = abs(min(move_30m, move_60m))

        watch_drop, short_drop, crash_drop = self._swing_long_to_short_lines(snapshot)
        drop_pct = metrics.get("drop_pct", short_move)

        if short_structural:
            if short_move >= threshold_30m * 0.88 or drop_pct >= crash_drop * 1.05:
                return (
                    "做空",
                    "developing",
                    f"1H/15m转空且30-60分钟跌幅约{short_move:.2f}%，4H仍在同步中。",
                )
            if pressure == "down" and short_move >= threshold_30m * 0.62:
                return "做空", "developing", "1H领先、15m确认转空，4H仍在同步中，关注延伸而非追极致。"
            return "观望", "short_wait", "结构偏空但跌幅未达确认门槛，继续观察。"

        if long_structural:
            if long_move >= threshold_30m * 1.05 or rise_pct >= long_rise * 1.12:
                return (
                    "做多",
                    "developing",
                    f"1H/15m转多且30-60分钟涨幅约{max(long_move, rise_pct):.2f}%，4H仍在同步中。",
                )
            if rise_pct >= watch_rise * 1.12 and self._swing_15m_bullish_aligned(profiles):
                return "做多", "developing", "1H领先、15m确认转多，4H仍在同步中，关注延伸而非追极致。"
            if rise_pct >= surge_rise * 1.12 and pressure == "up":
                return (
                    "做多",
                    "developing",
                    f"涨幅约{rise_pct:.2f}%达门槛，15m滞后但价格领先。",
                )
            return "观望", "long_wait", "结构偏多但涨幅未达确认门槛，继续观察。"

        long_momentum = (
            (
                long_move >= threshold_30m * 1.05
                or rise_pct >= long_rise * 1.22
            )
            and trend_1h != "down"
            and (trend_15m in ("up", "mixed") or rise_pct >= surge_rise * 1.12)
            and pressure != "down"
            and (bias == "long" or regime in ("trend_up", "mixed") or rise_pct >= surge_rise * 1.08)
        )
        short_momentum = (
            (short_move >= threshold_30m * 0.88 or drop_pct >= short_drop * 1.12)
            and trend_1h != "up"
            and trend_15m in ("down", "mixed")
            and pressure != "up"
            and (bias == "short" or regime in ("trend_down", "mixed"))
        )
        if long_momentum and long_move >= threshold_60m * 0.92:
            return "做多", "momentum", f"30-60分钟强势上行约{long_move:.2f}%，按中线动量跟踪。"
        if short_momentum and short_move >= threshold_60m * 0.92:
            return "做空", "momentum", f"30-60分钟强势下行约{short_move:.2f}%，按中线动量跟踪。"
        return self._merge_sentiment_direction_lead(
            snapshot,
            context,
            "观望",
            "neutral",
            "1H/4H/15m尚未形成可跟踪的中线结构，继续观察。",
        )

    def _swing_raw_direction(self, snapshot: Dict[str, Any], context: Dict[str, Any]) -> str:
        return self._swing_direction_meta(snapshot, context)[0]

    def _long_direction_meta(self, snapshot: Dict[str, Any], context: Dict[str, Any]) -> Tuple[str, str, str]:
        profiles = self._direction_profiles(snapshot)
        trend_4h = profiles.get("4H", {}).get("trend")
        trend_1d = profiles.get("1D", {}).get("trend")
        trend_1w = profiles.get("1W", {}).get("trend")
        pressure = context.get("recent_price_pressure", "neutral")
        if trend_1d == trend_1w == "up" and trend_4h != "down":
            return "做多", "aligned", "1D/1W主趋势同向偏多，4H未反向，按长线结构跟踪。"
        if trend_1d == trend_1w == "down" and trend_4h != "up":
            return "做空", "aligned", "1D/1W主趋势同向偏空，4H未反向，按长线结构跟踪。"
        if trend_1d == "up" and trend_1w != "down" and trend_4h == "up" and pressure != "down":
            return "做多", "developing", "1D趋势偏多、4H确认，1W仍在同步中。"
        if trend_1d == "down" and trend_1w != "up" and trend_4h == "down" and pressure != "up":
            return "做空", "developing", "1D趋势偏空、4H确认，1W仍在同步中。"
        return "观望", "neutral", "4H/1D/1W尚未形成可跟踪的长线同向结构。"

    def _long_raw_direction(self, snapshot: Dict[str, Any], context: Dict[str, Any]) -> str:
        return self._long_direction_meta(snapshot, context)[0]

    def _long_levels(self, snapshot: Dict[str, Any], direction: str) -> Dict[str, str]:
        price = to_float(snapshot.get("price"))
        profiles = snapshot.get("trend_profiles", {})
        atr_1d = to_float(profiles.get("1D", {}).get("atr")) or price * 0.025
        stop_gap = max(atr_1d * 0.8, price * 0.012)
        target_gap = max(atr_1d * 1.4, price * 0.025)
        if direction == "做多":
            return {
                "entry": f"{price - stop_gap * 0.35:.2f} - {price + stop_gap * 0.15:.2f}",
                "stop_loss": f"{price - stop_gap:.2f}",
                "take_profit": f"{price + target_gap:.2f} / {price + target_gap * 2:.2f}",
            }
        if direction == "做空":
            return {
                "entry": f"{price - stop_gap * 0.15:.2f} - {price + stop_gap * 0.35:.2f}",
                "stop_loss": f"{price + stop_gap:.2f}",
                "take_profit": f"{price - target_gap:.2f} / {price - target_gap * 2:.2f}",
            }
        return {"entry": "-", "stop_loss": "-", "take_profit": "-"}

    def _strategy_entry_plan(self, snapshot: Dict[str, Any], direction: str) -> Dict[str, Any]:
        mode = self._strategy_mode()
        if direction not in ("做多", "做空") or mode == "short":
            return self._suggest_levels(snapshot, direction)
        if mode == "scalp":
            levels = self._scalp_levels(snapshot, direction)
            invalidation = "5m脉冲反向或短窗压力反转"
        elif mode == "swing":
            levels = self._swing_levels(snapshot, direction)
            invalidation = "1H结构反向且4H确认失效"
        else:
            levels = self._long_levels(snapshot, direction)
            invalidation = "1D结构反向且1W背景不再支持"
        return {
            "quality": "confirmed",
            **levels,
            "invalidation": invalidation,
            "wait_for": [],
        }

    def _swing_levels(self, snapshot: Dict[str, Any], direction: str) -> Dict[str, str]:
        price = to_float(snapshot.get("price"))
        profiles = snapshot.get("trend_profiles", {})
        atr_1h = to_float(profiles.get("1H", {}).get("atr")) or price * 0.006
        stop_gap = max(atr_1h * 0.55, price * 0.002)
        target_gap = max(atr_1h * 0.95, price * 0.0035)
        if direction == "做多":
            return {
                "entry": f"{price - stop_gap * 0.35:.2f} - {price + stop_gap * 0.25:.2f}",
                "stop_loss": f"{price - stop_gap:.2f}",
                "take_profit": f"{price + target_gap:.2f} / {price + target_gap * 1.8:.2f}",
            }
        if direction == "做空":
            return {
                "entry": f"{price - stop_gap * 0.25:.2f} - {price + stop_gap * 0.35:.2f}",
                "stop_loss": f"{price + stop_gap:.2f}",
                "take_profit": f"{price - target_gap:.2f} / {price - target_gap * 1.8:.2f}",
            }
        return {"entry": "-", "stop_loss": "-", "take_profit": "-"}

    def _sentiment_direction_meta(self, snapshot: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        """合约/盘口/拥挤度合成的情绪方向提示，用于价格结构尚未确认时的领先判断。"""
        funding = to_float(snapshot.get("funding_rate"))
        funding_change = to_float(snapshot.get("funding_change_strategy", snapshot.get("funding_change")))
        long_short = snapshot.get("long_short_ratio", {})
        long_ratio = to_float(long_short.get("long_ratio"))
        short_ratio = to_float(long_short.get("short_ratio"))
        oi_state = str(context.get("oi_price_state") or "")
        order_book_bias = str(context.get("order_book_bias") or "neutral")
        price_change_15m = to_float(context.get("price_change_strategy", context.get("price_change_15m")))
        oi_change = to_float(snapshot.get("oi_change_pct_strategy", snapshot.get("oi_change_pct_15m")))
        mode = self._strategy_mode()
        long_points = 0
        short_points = 0
        factors: List[str] = []

        if oi_state == "price_up_oi_up_new_longs_or_short_pressure":
            long_points += 2
            factors.append("价涨OI增")
        elif oi_state == "price_down_oi_up_new_shorts_or_long_pressure":
            short_points += 2
            factors.append("价跌OI增")
        elif oi_state == "price_up_oi_down_short_covering":
            long_points += 1
            factors.append("空头回补")
        elif oi_state == "price_down_oi_down_long_deleveraging":
            short_points += 1
            factors.append("多头去杠杆")

        if snapshot.get("oi_strategy_warmup_ready", snapshot.get("oi_warmup_ready")) and abs(oi_change) >= 2:
            if price_change_15m > 0.05:
                long_points += 1
                factors.append("OI配合上涨")
            elif price_change_15m < -0.05:
                short_points += 1
                factors.append("OI配合下跌")

        if snapshot.get("funding_strategy_warmup_ready", snapshot.get("funding_warmup_ready")) and abs(funding_change) >= self.config.funding_change_threshold:
            if funding_change > 0:
                short_points += 1
                factors.append("费率上行")
            else:
                long_points += 1
                factors.append("费率下行")

        if long_ratio >= self.config.long_short_extreme:
            short_points += 1
            factors.append("多头拥挤")
        elif short_ratio >= self.config.long_short_extreme:
            long_points += 1
            factors.append("空头拥挤")

        if mode in ("scalp", "short") and order_book_bias == "bid_support":
            long_points += 1
            factors.append("盘口买单")
        elif mode in ("scalp", "short") and order_book_bias == "ask_pressure":
            short_points += 1
            factors.append("盘口卖压")

        direction = "观望"
        strength = 0
        if long_points >= 3 and long_points >= short_points + 1:
            direction = "做多"
            strength = long_points
        elif short_points >= 3 and short_points >= long_points + 1:
            direction = "做空"
            strength = short_points

        return {
            "direction": direction,
            "strength": strength,
            "long_points": long_points,
            "short_points": short_points,
            "factors": factors,
            "summary": "、".join(factors[:4]) if factors else "情绪中性",
        }

    def _merge_sentiment_direction_lead(
        self,
        snapshot: Dict[str, Any],
        context: Dict[str, Any],
        price_direction: str,
        price_tier: str,
        price_summary: str,
    ) -> Tuple[str, str, str]:
        """价格路径为观望时，允许足够强的情绪信号给出领先方向。"""
        if price_direction in ("做多", "做空") and price_tier not in ("neutral",):
            return price_direction, price_tier, price_summary

        pressure = context.get("recent_price_pressure", "neutral")
        if price_direction == "观望" and pressure == "down":
            price_led = self._price_led_direction_meta(snapshot, context)
            if price_led and price_led[0] == "做空":
                return price_led

        sentiment = context.get("sentiment_meta")
        if not isinstance(sentiment, dict):
            sentiment = self._sentiment_direction_meta(snapshot, context)
        if sentiment.get("direction") not in ("做多", "做空"):
            return price_direction, price_tier, price_summary

        risk = self._risk_preference()
        min_strength = {"conservative": 4, "standard": 3, "aggressive": 2}.get(risk, 3)
        if int(sentiment.get("strength", 0) or 0) < min_strength:
            return price_direction, price_tier, price_summary

        profiles = snapshot.get("trend_profiles", {})
        score_bars = self._strategy_score_bars()
        trend_15m = profiles.get(score_bars["primary"], {}).get("trend")
        trend_5m = profiles.get(score_bars["entry"], {}).get("trend")
        sent_dir = str(sentiment.get("direction"))
        if sent_dir == "做多" and ((trend_15m == "down" and trend_5m == "down") or pressure == "down"):
            return price_direction, price_tier, price_summary
        if sent_dir == "做空" and ((trend_15m == "up" and trend_5m == "up") or pressure == "up"):
            return price_direction, price_tier, price_summary

        if price_direction == "观望" or price_tier == "neutral":
            summary = f"合约/情绪领先：{sentiment.get('summary', '情绪转强')}"
            return sent_dir, "sentiment", summary
        return price_direction, price_tier, price_summary

    def _short_move_thresholds(self, snapshot: Dict[str, Any]) -> Tuple[float, float]:
        """短线动量阈值：10/20 分钟涨跌幅，明显高于超短线 5/10 分钟脉冲。"""
        volatility = snapshot.get("volatility", {})
        atr_pct_15m = max(to_float(volatility.get("atr_pct_15m")), 0.10)
        factor = self._risk_adjustment()
        threshold_10m = max(0.12, atr_pct_15m * 0.40) * factor
        threshold_20m = max(0.20, atr_pct_15m * 0.62) * factor
        return threshold_10m, threshold_20m

    def _short_direction_meta(self, snapshot: Dict[str, Any], context: Dict[str, Any]) -> Tuple[str, str, str]:
        profiles = self._direction_profiles(snapshot)
        candles = snapshot.get("candles", {})
        trend_5m = profiles.get("5m", {}).get("trend")
        trend_15m = profiles.get("15m", {}).get("trend")
        trend_1h = profiles.get("1H", {}).get("trend")
        trend_4h = profiles.get("4H", {}).get("trend")
        bias = context.get("bias", "neutral")
        pressure = context.get("recent_price_pressure", "neutral")
        trade_up = int(context.get("trade_up", 0) or 0)
        trade_down = int(context.get("trade_down", 0) or 0)
        threshold_10m, threshold_20m = self._short_move_thresholds(snapshot)
        price = to_float(snapshot.get("price"))
        live = price if price > 0 else 0.0
        move_10m = self._recent_move_pct(candles.get("1m", []), 10, live_price=live)
        move_20m = self._recent_move_pct(candles.get("1m", []), 20, live_price=live)
        price_led = self._price_led_direction_meta(snapshot, context)
        if price_led:
            return price_led

        indicator = context.get("indicator_consensus") if isinstance(context.get("indicator_consensus"), dict) else {}
        alignment = context.get("indicator_alignment") if isinstance(context.get("indicator_alignment"), dict) else {}
        indicator_long = self._htf_indicator_supports(indicator, "做多", alignment=alignment, min_net=22.0)
        indicator_short = self._htf_indicator_supports(indicator, "做空", alignment=alignment, min_net=22.0)

        if pressure == "down":
            if (
                move_20m <= -threshold_10m * 0.55
                and trend_1h != "up"
                and trend_15m != "up"
            ):
                return "做空", "price_leading", f"短窗下跌约{abs(move_20m):.2f}%，优先跟踪回落。"
            if indicator_short and trend_1h in ("down", "mixed") and trend_4h in ("down", "mixed"):
                return "做空", "indicator_htf", "1H/4H指标同向偏空且短窗转弱，提前跟随中线结构。"
            if short_structural := (
                trend_5m in ("down", "mixed")
                and trend_15m in ("down", "mixed")
                and (trade_down >= 1 or move_20m <= -threshold_10m * 0.45)
            ):
                return "做空", "structure", "5m/15m结构转弱且短窗压力向下，关注延续。"

        if pressure == "up":
            if indicator_long and trend_1h in ("up", "mixed") and trend_4h in ("up", "mixed"):
                return "做多", "indicator_htf", "1H/4H指标同向偏多且短窗未转弱，提前跟随中线结构。"

        if (
            indicator_long
            and trend_1h in ("up", "mixed")
            and trend_4h in ("up", "mixed")
            and pressure == "up"
        ):
            return "做多", "indicator_htf", "1H/4H指标同向偏多且短窗配合，提前跟随中线结构。"

        if bias == "long" and pressure != "down" and (trade_up >= 1 or trend_15m == "up"):
            return "做多", "bias", "市场偏多且5m/15m未背离，短线跟随结构。"
        if bias == "short" and pressure != "up" and (trade_down >= 1 or trend_15m == "down"):
            return "做空", "bias", "市场偏空且5m/15m未背离，短线跟随结构。"

        long_structural = trend_5m == "up" and trend_15m == "up" and pressure != "down" and (
            trade_up >= 2 or (trade_up >= 1 and pressure == "up")
        )
        short_structural = trend_5m in ("down", "mixed") and trend_15m in ("down", "mixed") and (
            trade_down >= 2 or (trade_down >= 1 and pressure == "down") or pressure == "down"
        )
        if long_structural:
            return "做多", "structure", "5m/15m结构同向转多，关注延续或回踩再入。"
        if short_structural:
            return "做空", "structure", "5m/15m结构同向转空，关注延续或反抽再入。"

        long_developing_momentum = (
            pressure == "up"
            and trend_5m in ("up", "mixed")
            and trend_1h != "down"
            and move_20m >= threshold_10m * 0.85
            and (move_10m >= threshold_10m * 0.55 or trend_15m in ("up", "mixed"))
        )
        short_developing_momentum = (
            pressure == "down"
            and trend_5m in ("down", "mixed")
            and trend_1h != "up"
            and move_20m <= -threshold_10m * 0.70
            and (move_10m <= -threshold_10m * 0.40 or trend_15m in ("down", "mixed"))
        )
        if long_developing_momentum:
            return "做多", "developing_momentum", "短窗压力向上，5m已转强且20m延伸，1H未强烈反向。"
        if short_developing_momentum:
            return "做空", "developing_momentum", "短窗压力向下，5m已转弱且20m延伸，1H未强烈反向。"

        long_developing = (
            trend_5m == "up"
            and trend_15m == "up"
            and trend_1h != "down"
            and move_20m >= threshold_10m
        )
        short_developing = (
            trend_5m == "down"
            and trend_15m == "down"
            and trend_1h != "up"
            and move_20m <= -threshold_10m
        )
        if long_developing:
            return "做多", "developing", "5m/15m已同向，20分钟涨幅确认结构延伸。"
        if short_developing:
            return "做空", "developing", "5m/15m已同向，20分钟跌幅确认结构延伸。"

        long_momentum = (
            move_20m >= threshold_20m
            and trend_15m == "up"
            and trend_5m in ("up", "mixed")
            and trend_1h != "down"
            and pressure != "down"
        )
        short_momentum = (
            move_20m <= -threshold_20m * 0.85
            and trend_15m in ("down", "mixed")
            and trend_5m in ("down", "mixed")
            and trend_1h != "up"
            and pressure != "up"
        )
        if long_momentum:
            return "做多", "momentum", f"20分钟涨幅约{move_20m:.2f}%，15m结构已确认。"
        if short_momentum:
            return "做空", "momentum", f"20分钟跌幅约{abs(move_20m):.2f}%，15m结构已确认。"

        risk = self._risk_preference()
        if risk == "aggressive":
            if pressure == "up" and trade_up >= 1 and trend_5m != "down" and trend_15m != "down":
                return "做多", "pressure", "激进模式：短窗压力偏多且15m未反向。"
            if pressure == "down" and trade_down >= 1 and trend_5m != "up" and trend_15m != "up":
                return "做空", "pressure", "激进模式：短窗压力偏空且15m未反向。"
        return self._merge_sentiment_direction_lead(
            snapshot,
            context,
            "观望",
            "neutral",
            "5m/15m尚未同向确认，等待结构或20分钟延伸。",
        )

    def _short_raw_direction(self, snapshot: Dict[str, Any], context: Dict[str, Any]) -> str:
        return self._short_direction_meta(snapshot, context)[0]

    def _effective_forecast_horizon(self) -> int:
        minimum = {"scalp": 10, "short": 15, "swing": 240, "long": 2880}.get(self._strategy_mode(), 15)
        return max(minimum, int(self.config.forecast_horizon_minutes or minimum))

    def _forecast_structure_bar(self) -> str:
        return self._forecast_timeframe_spec()["target"]

    def _forecast_timeframe_spec(self) -> Dict[str, str]:
        return {
            "scalp": {"lead": "3m", "target": "5m", "background": "15m"},
            "short": {"lead": "5m", "target": "15m", "background": "1H"},
            "swing": {"lead": "15m", "target": "1H", "background": "4H"},
            "long": {"lead": "4H", "target": "1D", "background": "1W"},
        }.get(
            self._strategy_mode(),
            {"lead": "5m", "target": "15m", "background": "1H"},
        )

    def _empty_structure_forecast(self) -> Dict[str, Any]:
        return {
            "active": False,
            "direction": "观望",
            "probability": 0,
            "horizon_minutes": self._effective_forecast_horizon(),
            "lead_bar": self._forecast_timeframe_spec()["lead"],
            "structure_bar": self._forecast_timeframe_spec()["target"],
            "background_bar": self._forecast_timeframe_spec()["background"],
            "phase": "none",
            "from_state": "-",
            "to_state": "-",
            "scenario": "none",
            "evidence": [],
            "invalidation": "-",
            "summary": "",
        }

    def _calibration_buckets(self) -> Dict[str, Any]:
        buckets = self.calibration_state.get("buckets")
        return buckets if isinstance(buckets, dict) else {}

    def _forecast_calibration_key(self, inst_id: str, scenario: str, direction: str, regime: str) -> str:
        return (
            f"forecast:v2:{self._strategy_mode()}:{self._effective_forecast_horizon()}m:"
            f"{inst_id}:{scenario}:{direction}:{regime or 'unknown'}"
        )

    def _decision_calibration_key(
        self,
        inst_id: str,
        decision_source: str,
        push_kind: str,
        direction: str,
        regime: str,
    ) -> str:
        return (
            f"decision:{self._strategy_mode()}:{self._effective_forecast_horizon()}m:"
            f"{inst_id}:{decision_source}:{push_kind}:{direction}:{regime or 'unknown'}"
        )

    def _maybe_save_calibration_state(self) -> None:
        if not self._calibration_dirty:
            return
        interval = max(15, int(self.config.calibration_save_interval_seconds))
        if time.time() - self._last_calibration_save_at < interval:
            return
        save_calibration_state(self.calibration_state)
        self._calibration_dirty = False
        self._last_calibration_save_at = time.time()

    def _record_calibration_bucket(
        self,
        bucket_key: str,
        *,
        structure_hit: bool,
        price_hit: bool,
        move_pct: float,
        auto_disable_below: float,
        min_samples_to_disable: int,
        partial_structure_hit: bool = False,
        predicted_probability: Optional[float] = None,
    ) -> Dict[str, Any]:
        buckets = self._calibration_buckets()
        stats = dict(buckets.get(bucket_key) or {})
        stats["total"] = int(stats.get("total", 0) or 0) + 1
        overall_hit = bool(structure_hit or price_hit)
        if overall_hit:
            stats["hits"] = int(stats.get("hits", 0) or 0) + 1
        if structure_hit:
            stats["structure_hits"] = int(stats.get("structure_hits", 0) or 0) + 1
        if partial_structure_hit:
            stats["partial_structure_hits"] = int(stats.get("partial_structure_hits", 0) or 0) + 1
        if price_hit:
            stats["price_hits"] = int(stats.get("price_hits", 0) or 0) + 1
        stats["sum_move_pct"] = float(stats.get("sum_move_pct", 0.0) or 0.0) + float(move_pct)
        if predicted_probability is not None:
            probability = max(0.0, min(1.0, float(predicted_probability) / 100.0))
            outcome = 1.0 if overall_hit else 0.0
            stats["sum_brier"] = float(stats.get("sum_brier", 0.0) or 0.0) + (probability - outcome) ** 2
        total = int(stats["total"])
        hit_rate = safe_div(int(stats.get("hits", 0) or 0), total, 0.0)
        if total >= min_samples_to_disable and hit_rate < auto_disable_below:
            stats["disabled"] = True
        elif total >= min_samples_to_disable and hit_rate >= max(auto_disable_below + 0.12, 0.5):
            stats["disabled"] = False
        buckets[bucket_key] = stats
        self.calibration_state["buckets"] = buckets
        self._calibration_dirty = True
        self._maybe_save_calibration_state()
        return calibration_bucket_stats(buckets, bucket_key)

    def _calibrated_score(self, heuristic: int, bucket_key: str) -> Tuple[int, Dict[str, Any]]:
        heuristic = max(0, min(100, int(heuristic)))
        if not self.config.calibration_enabled:
            return heuristic, {"samples": 0, "hit_rate": 0.0, "disabled": False}

        meta = calibration_bucket_stats(self._calibration_buckets(), bucket_key)
        samples = int(meta["total"])
        min_samples = max(3, int(self.config.calibration_min_samples))
        if samples < min_samples:
            shrink = max(0.72, 0.92 - (min_samples - samples) * 0.02)
            adjusted = int(round(heuristic * shrink + 50 * (1 - shrink)))
            return max(0, min(88, adjusted)), meta

        # Beta(2, 2) smoothing keeps small buckets from jumping to 0%/100%.
        empirical = int(round((int(meta["hits"]) + 2) / float(samples + 4) * 100))
        blend = max(0.0, min(1.0, float(self.config.calibration_blend_weight)))
        if samples < min_samples * 2:
            blend *= samples / float(min_samples * 2)
        blended = int(round(heuristic * (1 - blend) + empirical * blend))
        if samples >= min_samples and float(meta.get("brier_score", 0.0) or 0.0) > 0.28:
            blended -= 4
        return max(0, min(88, blended)), meta

    def _effective_forecast_threshold(self, bucket_key: str) -> int:
        base = max(0, min(100, int(self.config.forecast_push_score)))
        if not self.config.calibration_enabled:
            return base
        meta = calibration_bucket_stats(self._calibration_buckets(), bucket_key)
        samples = int(meta["total"])
        min_samples = max(3, int(self.config.calibration_min_samples))
        if samples < min_samples:
            return min(95, base + 2)
        hit_rate = float(meta["hit_rate"])
        if hit_rate >= 0.62:
            return max(45, base - 4)
        if hit_rate >= 0.52:
            return max(45, base - 1)
        if hit_rate < 0.42:
            return min(95, base + 8)
        if hit_rate < float(self.config.calibration_disable_below_hit_rate):
            return min(95, base + 5)
        return base

    def _finalize_structure_forecast(
        self,
        snapshot: Dict[str, Any],
        forecast: Dict[str, Any],
    ) -> Dict[str, Any]:
        if not forecast.get("active"):
            return forecast
        regime = str(snapshot.get("market_context", {}).get("regime", "unknown") or "unknown")
        inst_id = str(snapshot.get("inst_id", "") or "")
        scenario = str(forecast.get("scenario", "none") or "none")
        direction = str(forecast.get("direction", "观望") or "观望")
        bucket_key = self._forecast_calibration_key(inst_id, scenario, direction, regime)
        meta = calibration_bucket_stats(self._calibration_buckets(), bucket_key)
        raw_probability = int(forecast.get("probability", 0) or 0)
        calibrated, cal_meta = self._calibrated_score(raw_probability, bucket_key)
        threshold = self._effective_forecast_threshold(bucket_key)
        scenario_enabled = not bool(meta.get("disabled"))
        result = dict(forecast)
        result["raw_probability"] = raw_probability
        result["calibrated_probability"] = calibrated
        result["probability"] = calibrated
        result["calibration_key"] = bucket_key
        result["calibration_samples"] = int(cal_meta.get("total", 0) or 0)
        result["calibration_hit_rate"] = round(float(cal_meta.get("hit_rate", 0.0) or 0.0), 4)
        result["calibration_brier_score"] = round(float(cal_meta.get("brier_score", 0.0) or 0.0), 4)
        result["calibration_status"] = (
            "ready"
            if int(cal_meta.get("total", 0) or 0) >= int(self.config.calibration_min_samples)
            else "warming_up"
        )
        result["effective_push_threshold"] = threshold
        result["scenario_enabled"] = scenario_enabled
        result["active"] = scenario_enabled and calibrated >= max(45, threshold - 8)
        if not scenario_enabled:
            result["summary"] = f"{result.get('summary', '')}（历史命中率偏低，已自动降权）"
        elif cal_meta.get("total", 0) >= self.config.calibration_min_samples:
            result["summary"] = (
                f"{result.get('summary', '')} "
                f"[校准P={calibrated} 历史{int(round(float(cal_meta.get('hit_rate', 0)) * 100))}% n={cal_meta.get('total', 0)}]"
            ).strip()
        return result

    def _directional_move_pct(self, open_price: float, current_price: float, direction: str) -> float:
        if open_price <= 0 or current_price <= 0:
            return 0.0
        move = pct_change(current_price, open_price)
        return move if direction == "做多" else -move

    def _price_hit_threshold_pct(self, snapshot: Dict[str, Any]) -> float:
        profiles = snapshot.get("trend_profiles", {})
        price = to_float(snapshot.get("price"))
        structure_bar = self._forecast_structure_bar()
        atr_value = to_float(profiles.get(structure_bar, {}).get("atr")) or price * 0.004
        floor, cap, factor = {
            "scalp": (0.04, 0.35, 0.30),
            "short": (0.05, 0.45, 0.35),
            "swing": (0.15, 1.50, 0.45),
            "long": (0.60, 6.00, 0.55),
        }.get(self._strategy_mode(), (0.05, 0.45, 0.35))
        return max(floor, min(cap, (atr_value / max(price, 1e-9)) * 100 * factor))

    def _structure_direction_hit(
        self,
        direction: str,
        open_structure_trend: str,
        current_structure_trend: str,
    ) -> bool:
        """Only count an actual target-state confirmation as a structure hit."""
        if direction == "做多":
            return current_structure_trend == "up" and open_structure_trend != "up"
        if direction == "做空":
            return current_structure_trend == "down" and open_structure_trend != "down"
        return False

    def _structure_direction_improved(
        self,
        direction: str,
        open_structure_trend: str,
        current_structure_trend: str,
    ) -> bool:
        """Track partial progress for diagnostics without treating it as a forecast hit."""
        rank_up = {"down": 0, "range": 1, "flat": 1, "mixed": 2, "up": 3}
        rank_down = {"up": 0, "range": 1, "flat": 1, "mixed": 2, "down": 3}
        ranks = rank_up if direction == "做多" else rank_down if direction == "做空" else {}
        if not ranks:
            return False
        return ranks.get(current_structure_trend, 1) > ranks.get(open_structure_trend, 1)

    def update_forecast_tracking(self, snapshot: Dict[str, Any], score: Dict[str, Any]) -> Dict[str, Any]:
        if not self.config.calibration_enabled:
            return {"opened": [], "closed": [], "pending_count": len(self.pending_forecast_reviews)}

        now_ts = self._now_ts()
        inst_id = snapshot.get("inst_id", "")
        price = to_float(snapshot.get("price"))
        profiles = snapshot.get("trend_profiles", {})
        structure_bar = self._forecast_structure_bar()
        current_structure_trend = str(profiles.get(structure_bar, {}).get("trend", "mixed") or "mixed")
        closed: List[Dict[str, Any]] = []
        still_pending: List[Dict[str, Any]] = []

        for item in self.pending_forecast_reviews:
            if item.get("inst_id") != inst_id:
                still_pending.append(item)
                continue

            direction = str(item.get("direction", "观望") or "观望")
            open_price = to_float(item.get("open_price"))
            move_pct = self._directional_move_pct(open_price, price, direction)
            threshold = to_float(item.get("price_hit_threshold_pct"), 0.08)
            item_structure_bar = str(item.get("structure_bar", "15m") or "15m")
            item_current_trend = str(profiles.get(item_structure_bar, {}).get("trend", "mixed") or "mixed")
            structure_hit = self._structure_direction_hit(
                direction,
                str(item.get("open_structure_trend", item.get("open_trend_15m", "mixed")) or "mixed"),
                item_current_trend,
            )
            partial_structure_hit = self._structure_direction_improved(
                direction,
                str(item.get("open_structure_trend", item.get("open_trend_15m", "mixed")) or "mixed"),
                item_current_trend,
            )
            item["max_favorable_move_pct"] = max(
                to_float(item.get("max_favorable_move_pct"), 0.0),
                move_pct,
            )
            item["max_adverse_move_pct"] = min(
                to_float(item.get("max_adverse_move_pct"), 0.0),
                move_pct,
            )
            item["structure_confirmed_once"] = bool(item.get("structure_confirmed_once")) or structure_hit
            item["partial_structure_seen"] = bool(item.get("partial_structure_seen")) or partial_structure_hit
            if now_ts < float(item.get("settle_ts", 0) or 0):
                still_pending.append(item)
                continue

            structure_hit = bool(item.get("structure_confirmed_once"))
            partial_structure_hit = bool(item.get("partial_structure_seen"))
            price_hit = to_float(item.get("max_favorable_move_pct")) >= threshold
            overall_hit = structure_hit or price_hit
            bucket_key = str(item.get("calibration_key", "") or "")
            if bucket_key:
                cal_meta = self._record_calibration_bucket(
                    bucket_key,
                    structure_hit=structure_hit,
                    price_hit=price_hit,
                    move_pct=move_pct,
                    auto_disable_below=float(self.config.calibration_disable_below_hit_rate),
                    min_samples_to_disable=max(12, int(self.config.calibration_min_samples) * 2),
                    partial_structure_hit=partial_structure_hit,
                    predicted_probability=to_float(item.get("calibrated_probability")),
                )
            else:
                cal_meta = {}

            settled = {
                **item,
                "state": "closed",
                "close_time": snapshot.get("time"),
                "close_price": price,
                "structure_bar": item_structure_bar,
                "close_structure_trend": item_current_trend,
                "structure_hit": structure_hit,
                "partial_structure_hit": partial_structure_hit,
                "price_hit": price_hit,
                "hit": overall_hit,
                "directional_move_pct": round(move_pct, 4),
                "max_favorable_move_pct": round(to_float(item.get("max_favorable_move_pct")), 4),
                "max_adverse_move_pct": round(to_float(item.get("max_adverse_move_pct")), 4),
                "calibration_hit_rate_after": cal_meta.get("hit_rate"),
                "calibration_total_after": cal_meta.get("total"),
            }
            closed.append(settled)
            append_calibration_performance(FORECAST_PERFORMANCE_FILE, settled)

        self.pending_forecast_reviews = still_pending

        opened: List[Dict[str, Any]] = []
        forecast = score.get("structure_forecast") if isinstance(score.get("structure_forecast"), dict) else {}
        if forecast.get("active") and str(forecast.get("scenario", "none") or "none") != "none":
            scenario = str(forecast.get("scenario", "none") or "none")
            direction = str(forecast.get("direction", "观望") or "观望")
            track_key = (
                f"{self._strategy_mode()}:{self._effective_forecast_horizon()}m:"
                f"{inst_id}:{scenario}:{direction}"
            )
            horizon_seconds = max(300, int(forecast.get("horizon_minutes", self._effective_forecast_horizon())) * 60)
            if now_ts - self.last_forecast_track_at.get(track_key, 0.0) >= horizon_seconds:
                self.last_forecast_track_at[track_key] = now_ts
                regime = str(snapshot.get("market_context", {}).get("regime", "unknown") or "unknown")
                bucket_key = self._forecast_calibration_key(inst_id, scenario, direction, regime)
                opened_item = {
                    "id": f"{int(now_ts)}:{track_key}",
                    "kind": "forecast",
                    "forecast_version": 2,
                    "strategy_mode": self._strategy_mode(),
                    "inst_id": inst_id,
                    "scenario": scenario,
                    "direction": direction,
                    "regime": regime,
                    "state": "pending",
                    "open_time": snapshot.get("time"),
                    "created_ts": now_ts,
                    "settle_ts": now_ts + horizon_seconds,
                    "horizon_minutes": int(forecast.get("horizon_minutes", self._effective_forecast_horizon())),
                    "open_price": price,
                    "structure_bar": structure_bar,
                    "open_structure_trend": current_structure_trend,
                    "open_trend_5m": str(profiles.get("5m", {}).get("trend", "mixed") or "mixed"),
                    "raw_probability": int(forecast.get("raw_probability", forecast.get("probability", 0)) or 0),
                    "calibrated_probability": int(forecast.get("calibrated_probability", forecast.get("probability", 0)) or 0),
                    "calibration_key": bucket_key,
                    "price_hit_threshold_pct": self._price_hit_threshold_pct(snapshot),
                    "max_favorable_move_pct": 0.0,
                    "max_adverse_move_pct": 0.0,
                    "structure_confirmed_once": False,
                    "partial_structure_seen": False,
                }
                opened.append(opened_item)
                self.pending_forecast_reviews.append(opened_item)

        if len(self.pending_forecast_reviews) > 400:
            self.pending_forecast_reviews = self.pending_forecast_reviews[-400:]

        self.calibration_state["pending_forecasts"] = self.pending_forecast_reviews
        self._calibration_dirty = True
        self._maybe_save_calibration_state()
        return {
            "opened": opened,
            "closed": closed,
            "pending_count": len(self.pending_forecast_reviews),
        }

    def update_decision_calibration_tracking(
        self,
        snapshot: Dict[str, Any],
        final_decision: Dict[str, Any],
        score: Dict[str, Any],
    ) -> Dict[str, Any]:
        if not self.config.calibration_enabled:
            return {"opened": [], "closed": [], "pending_count": len(self.pending_decision_reviews)}

        now_ts = self._now_ts()
        inst_id = snapshot.get("inst_id", "")
        price = to_float(snapshot.get("price"))
        profiles = snapshot.get("trend_profiles", {})
        structure_bar = self._forecast_structure_bar()
        current_structure_trend = str(profiles.get(structure_bar, {}).get("trend", "mixed") or "mixed")
        closed: List[Dict[str, Any]] = []
        still_pending: List[Dict[str, Any]] = []

        for item in self.pending_decision_reviews:
            if item.get("inst_id") != inst_id:
                still_pending.append(item)
                continue
            if now_ts < float(item.get("settle_ts", 0) or 0):
                still_pending.append(item)
                continue

            direction = str(item.get("direction", "观望") or "观望")
            open_price = to_float(item.get("open_price"))
            move_pct = self._directional_move_pct(open_price, price, direction)
            threshold = to_float(item.get("price_hit_threshold_pct"), 0.08)
            item_structure_bar = str(item.get("structure_bar", "15m") or "15m")
            item_current_trend = str(profiles.get(item_structure_bar, {}).get("trend", "mixed") or "mixed")
            structure_hit = self._structure_direction_hit(
                direction,
                str(item.get("open_structure_trend", item.get("open_trend_15m", "mixed")) or "mixed"),
                item_current_trend,
            )
            partial_structure_hit = self._structure_direction_improved(
                direction,
                str(item.get("open_structure_trend", item.get("open_trend_15m", "mixed")) or "mixed"),
                item_current_trend,
            )
            price_hit = move_pct >= threshold
            overall_hit = structure_hit or price_hit
            bucket_key = str(item.get("calibration_key", "") or "")
            if bucket_key:
                cal_meta = self._record_calibration_bucket(
                    bucket_key,
                    structure_hit=structure_hit,
                    price_hit=price_hit,
                    move_pct=move_pct,
                    auto_disable_below=float(self.config.calibration_disable_below_hit_rate),
                    min_samples_to_disable=max(12, int(self.config.calibration_min_samples) * 2),
                    partial_structure_hit=partial_structure_hit,
                    predicted_probability=to_float(item.get("confidence")),
                )
            else:
                cal_meta = {}

            settled = {
                **item,
                "state": "closed",
                "close_time": snapshot.get("time"),
                "close_price": price,
                "structure_hit": structure_hit,
                "partial_structure_hit": partial_structure_hit,
                "price_hit": price_hit,
                "hit": overall_hit,
                "directional_move_pct": round(move_pct, 4),
                "calibration_hit_rate_after": cal_meta.get("hit_rate"),
            }
            closed.append(settled)
            append_calibration_performance(DECISION_CALIBRATION_FILE, settled)

        self.pending_decision_reviews = still_pending

        opened: List[Dict[str, Any]] = []
        direction = str(final_decision.get("direction", "观望") or "观望")
        push = str(final_decision.get("push_recommendation", "none") or "none")
        source = str(final_decision.get("decision_source", "local") or "local")
        confidence = int(final_decision.get("confidence", 0) or 0)
        if direction in ("做多", "做空") and push in ("trade", "spike", "watch") and confidence >= 50:
            regime = str(snapshot.get("market_context", {}).get("regime", "unknown") or "unknown")
            push_kind = "trade" if push == "trade" else ("spike" if push == "spike" else "watch")
            track_key = f"{inst_id}:{source}:{push_kind}:{direction}:{regime}"
            if now_ts - self.last_signal_track_at.get(f"cal:{track_key}", 0.0) >= 300:
                self.last_signal_track_at[f"cal:{track_key}"] = now_ts
                bucket_key = self._decision_calibration_key(inst_id, source, push_kind, direction, regime)
                horizon_seconds = self._effective_forecast_horizon() * 60
                opened_item = {
                    "id": f"{int(now_ts)}:{track_key}",
                    "kind": "decision",
                    "inst_id": inst_id,
                    "decision_source": source,
                    "push_kind": push_kind,
                    "direction": direction,
                    "confidence": confidence,
                    "regime": regime,
                    "state": "pending",
                    "open_time": snapshot.get("time"),
                    "created_ts": now_ts,
                    "settle_ts": now_ts + horizon_seconds,
                    "open_price": price,
                    "horizon_minutes": self._effective_forecast_horizon(),
                    "structure_bar": structure_bar,
                    "open_structure_trend": current_structure_trend,
                    "calibration_key": bucket_key,
                    "price_hit_threshold_pct": self._price_hit_threshold_pct(snapshot),
                }
                opened.append(opened_item)
                self.pending_decision_reviews.append(opened_item)

        if len(self.pending_decision_reviews) > 400:
            self.pending_decision_reviews = self.pending_decision_reviews[-400:]

        return {
            "opened": opened,
            "closed": closed,
            "pending_count": len(self.pending_decision_reviews),
        }

    def _apply_ai_calibration_audit(
        self,
        audited: Dict[str, Any],
        snapshot: Dict[str, Any],
    ) -> Dict[str, Any]:
        if not self.config.calibration_enabled:
            return audited
        if str(audited.get("decision_source", "") or "") != "ai":
            return audited
        direction = str(audited.get("direction", "观望") or "观望")
        push = str(audited.get("push_recommendation", "none") or "none")
        if direction not in ("做多", "做空") or push not in ("trade", "spike"):
            return audited

        regime = str(snapshot.get("market_context", {}).get("regime", "unknown") or "unknown")
        inst_id = str(snapshot.get("inst_id", "") or "")
        bucket_key = self._decision_calibration_key(inst_id, "ai", push, direction, regime)
        meta = calibration_bucket_stats(self._calibration_buckets(), bucket_key)
        samples = int(meta.get("total", 0) or 0)
        if samples < max(6, int(self.config.calibration_min_samples)):
            return audited

        hit_rate = float(meta.get("hit_rate", 0.0) or 0.0)
        audit = dict(audited.get("post_audit") or {"action": "kept", "reasons": []})
        if hit_rate < float(self.config.calibration_disable_below_hit_rate):
            audited = dict(audited)
            audited["push_recommendation"] = self._downgrade_push_recommendation(
                push,
                int(audited.get("confidence", 0) or 0),
            )
            audit["action"] = "downgraded"
            audit.setdefault("reasons", []).append(f"ai_calibration_low_hit_rate:{hit_rate:.2f}")
            audited["post_audit"] = audit
            audited["calibration_hint"] = {
                "bucket": bucket_key,
                "hit_rate": round(hit_rate, 4),
                "samples": samples,
            }
        elif hit_rate >= 0.6 and push == "trade":
            audited = dict(audited)
            conf = int(audited.get("confidence", 0) or 0)
            threshold = self.trade_push_score(direction)
            if conf < threshold and conf >= threshold - 2:
                audited["confidence"] = min(100, threshold)
                audit.setdefault("reasons", []).append(f"ai_calibration_boost:{hit_rate:.2f}")
                audited["post_audit"] = audit
        return audited

    def _calibration_summary(self, inst_id: str) -> Dict[str, Any]:
        summary: Dict[str, Any] = {"forecast": {}, "decision": {}}
        prefix_forecast = f"forecast:v2:{self._strategy_mode()}:{self._effective_forecast_horizon()}m:"
        prefix_decision = f"decision:{self._strategy_mode()}:{self._effective_forecast_horizon()}m:"
        for key in self._calibration_buckets():
            if f":{inst_id}:" not in key:
                continue
            meta = calibration_bucket_stats(self._calibration_buckets(), key)
            if key.startswith(prefix_forecast):
                summary["forecast"][key] = meta
            elif key.startswith(prefix_decision):
                summary["decision"][key] = meta
        return summary

    def _evaluate_structure_evolution(
        self,
        snapshot: Dict[str, Any],
        signals: List[Dict[str, Any]],
        score: Dict[str, Any],
    ) -> Dict[str, Any]:
        """前瞻轨：基于策略主周期的过渡态，估计结构演变（与 trade/spike 并行）。"""
        if not self.config.signal_forecast_enabled:
            return self._empty_structure_forecast()

        mode = self._strategy_mode()
        if mode in ("swing", "long"):
            return self._evaluate_higher_timeframe_evolution(snapshot, signals, score)
        if mode not in ("short", "scalp"):
            return self._empty_structure_forecast()

        profiles = self._direction_profiles(snapshot)
        context = snapshot.get("market_context", {})
        timeframe = self._forecast_timeframe_spec()
        lead_bar = timeframe["lead"]
        target_bar = timeframe["target"]
        background_bar = timeframe["background"]
        lead_profile = profiles.get(lead_bar, {})
        target_profile = profiles.get(target_bar, {})
        lead_trend = str(lead_profile.get("trend", "mixed") or "mixed")
        target_trend = str(target_profile.get("trend", "mixed") or "mixed")
        background_trend = str(profiles.get(background_bar, {}).get("trend", "mixed") or "mixed")
        pressure = context.get("recent_price_pressure", "neutral")
        regime = context.get("regime", "unknown")
        signal_types = {item.get("type", "") for item in signals}
        pressure_moves = context.get("pressure_windows", {}).get("moves", {})
        move_values = [to_float(value) for value in pressure_moves.values()]
        fast_move = move_values[0] if move_values else 0.0
        medium_move = move_values[1] if len(move_values) > 1 else fast_move
        slow_move = move_values[-1] if move_values else medium_move
        target_atr_pct = max(0.01, to_float(target_profile.get("atr_pct"), 0.2))
        move_floor = max(0.03 if mode == "scalp" else 0.05, target_atr_pct * (0.25 if mode == "scalp" else 0.35))
        target_macd = target_profile.get("macd", {}) if isinstance(target_profile.get("macd"), dict) else {}
        macd_hist = to_float(target_macd.get("hist"))
        macd_slope = to_float(target_macd.get("hist_slope"))
        target_adx = to_float(target_profile.get("adx", {}).get("adx"))
        final_direction = score.get("final_direction", "观望")
        horizon = self._effective_forecast_horizon()

        def add_candidate(
            bucket: List[Dict[str, Any]],
            *,
            direction: str,
            scenario: str,
            phase: str,
            from_state: str,
            to_state: str,
            base_prob: int,
            summary: str,
            invalidation: str,
            evidence: List[str],
            bonuses: Optional[List[Tuple[int, bool]]] = None,
        ) -> None:
            if direction not in ("做多", "做空"):
                return
            prob = base_prob
            for delta, ok in bonuses or []:
                if ok:
                    prob += delta
            bucket.append(
                {
                    "direction": direction,
                    "probability": prob,
                    "phase": phase,
                    "from_state": from_state,
                    "to_state": to_state,
                    "scenario": scenario,
                    "summary": summary,
                    "invalidation": invalidation,
                    "evidence": evidence,
                }
            )

        candidates: List[Dict[str, Any]] = []

        if (
            final_direction != "做多"
            and background_trend != "down"
            and pressure != "down"
            and lead_trend == "up"
            and target_trend in ("mixed", "flat", "range", "down")
        ):
            add_candidate(
                candidates,
                direction="做多",
                scenario=f"{mode}_transition_up",
                phase="transition",
                from_state=target_trend,
                to_state="up",
                base_prob=54,
                summary=(
                    f"{lead_bar} 已偏多、{target_bar} 仍 {target_trend}，"
                    f"预计 {horizon}m 内 {target_bar} 确认向上。"
                ),
                invalidation=f"{lead_bar} 转弱、短窗压力转 down 或 {background_bar} 转空",
                evidence=[
                    f"{lead_bar}={lead_trend}",
                    f"{target_bar}={target_trend}",
                    f"{background_bar}={background_trend}",
                    f"pressure={pressure}",
                ],
                bonuses=[
                    (7, target_trend in ("mixed", "flat", "range")),
                    (5, medium_move >= move_floor),
                    (4, slow_move >= move_floor * 0.8),
                    (4, macd_slope > 0),
                    (4, "volume_spike" in signal_types),
                    (3, background_trend in ("up", "mixed")),
                    (3, regime in ("trend_up", "mixed")),
                ],
            )

        if (
            final_direction != "做空"
            and background_trend != "up"
            and pressure != "up"
            and lead_trend == "down"
            and target_trend in ("mixed", "flat", "range", "up")
        ):
            add_candidate(
                candidates,
                direction="做空",
                scenario=f"{mode}_transition_down",
                phase="transition",
                from_state=target_trend,
                to_state="down",
                base_prob=54,
                summary=(
                    f"{lead_bar} 已偏空、{target_bar} 仍 {target_trend}，"
                    f"预计 {horizon}m 内 {target_bar} 确认向下。"
                ),
                invalidation=f"{lead_bar} 转强、短窗压力转 up 或 {background_bar} 转多",
                evidence=[
                    f"{lead_bar}={lead_trend}",
                    f"{target_bar}={target_trend}",
                    f"{background_bar}={background_trend}",
                    f"pressure={pressure}",
                ],
                bonuses=[
                    (7, target_trend in ("mixed", "flat", "range")),
                    (5, medium_move <= -move_floor),
                    (4, slow_move <= -move_floor * 0.8),
                    (4, macd_slope < 0),
                    (4, "volume_spike" in signal_types),
                    (3, background_trend in ("down", "mixed")),
                    (3, regime in ("trend_down", "mixed")),
                ],
            )

        if final_direction != "做多" and pressure == "up" and background_trend != "down":
            add_candidate(
                candidates,
                direction="做多",
                scenario=f"{mode}_momentum_lead_up",
                phase="developing",
                from_state=target_trend,
                to_state="up",
                base_prob=52,
                summary=f"价格动量已先行，{target_bar} 仍 {target_trend}，观察结构是否滞后跟随。",
                invalidation=f"动量回吐、pressure 转 neutral/down 或 {lead_bar} 转空",
                evidence=[f"fast_move={fast_move:.3f}%", f"medium_move={medium_move:.3f}%", f"{target_bar}={target_trend}"],
                bonuses=[
                    (6, target_trend in ("mixed", "range", "flat") and lead_trend in ("up", "mixed")),
                    (6, medium_move >= move_floor),
                    (4, fast_move > 0),
                    (4, macd_slope > 0),
                    (3, "structure_break" in signal_types),
                ],
            )

        if final_direction != "做空" and pressure == "down" and background_trend != "up":
            add_candidate(
                candidates,
                direction="做空",
                scenario=f"{mode}_momentum_lead_down",
                phase="developing",
                from_state=target_trend,
                to_state="down",
                base_prob=52,
                summary=f"价格动量已先行，{target_bar} 仍 {target_trend}，观察结构是否滞后跟随。",
                invalidation=f"动量回补、pressure 转 neutral/up 或 {lead_bar} 转多",
                evidence=[f"fast_move={fast_move:.3f}%", f"medium_move={medium_move:.3f}%", f"{target_bar}={target_trend}"],
                bonuses=[
                    (6, target_trend in ("mixed", "range", "flat") and lead_trend in ("down", "mixed")),
                    (6, medium_move <= -move_floor),
                    (4, fast_move < 0),
                    (4, macd_slope < 0),
                    (3, "structure_break" in signal_types),
                ],
            )

        compression_ready = (
            regime == "squeeze"
            or "boll_squeeze" in signal_types
            or (
                regime in ("range", "mixed")
                and "structure_break" in signal_types
                and "volume_spike" in signal_types
            )
        )
        if compression_ready:
            if final_direction != "做多" and pressure != "down" and macd_hist >= 0:
                add_candidate(
                    candidates,
                    direction="做多",
                    scenario=f"{mode}_compression_release_up",
                    phase="release",
                    from_state=str(regime),
                    to_state="breakout_up",
                    base_prob=50,
                    summary=f"{target_bar} 压缩/震荡后动能修复，预测向上释放。",
                    invalidation=f"{target_bar} MACD转弱且{lead_bar}破低",
                    evidence=[f"regime={regime}", f"macd_hist={macd_hist:.4f}"],
                    bonuses=[
                        (7, "boll_squeeze" in signal_types and fast_move > 0),
                        (5, medium_move >= move_floor * 0.8),
                        (4, macd_slope > 0),
                        (4, "volume_spike" in signal_types),
                        (3, lead_trend == "up"),
                        (2, target_adx >= 18),
                    ],
                )
            if final_direction != "做空" and pressure != "up" and macd_hist <= 0:
                add_candidate(
                    candidates,
                    direction="做空",
                    scenario=f"{mode}_compression_release_down",
                    phase="release",
                    from_state=str(regime),
                    to_state="breakout_down",
                    base_prob=50,
                    summary=f"{target_bar} 压缩/震荡后动能走弱，预测向下释放。",
                    invalidation=f"{target_bar} MACD转强且{lead_bar}破高",
                    evidence=[f"regime={regime}", f"macd_hist={macd_hist:.4f}"],
                    bonuses=[
                        (7, "boll_squeeze" in signal_types and fast_move < 0),
                        (5, medium_move <= -move_floor * 0.8),
                        (4, macd_slope < 0),
                        (4, "volume_spike" in signal_types),
                        (3, lead_trend == "down"),
                        (2, target_adx >= 18),
                    ],
                )

        if not candidates:
            return self._empty_structure_forecast()

        best = max(candidates, key=lambda item: item["probability"])
        probability = max(0, min(88, int(best["probability"])))
        result = self._empty_structure_forecast()
        result.update(best)
        result["probability"] = probability
        result["horizon_minutes"] = horizon
        result["lead_bar"] = lead_bar
        result["structure_bar"] = target_bar
        result["background_bar"] = background_bar
        result["active"] = probability >= max(45, self.config.forecast_push_score - 8)
        scenario_label = FORECAST_SCENARIO_LABELS.get(best["scenario"], best["scenario"])
        if scenario_label not in result["summary"]:
            result["summary"] = f"{scenario_label}：{result['summary']}"
        return result

    def _evaluate_higher_timeframe_evolution(
        self,
        snapshot: Dict[str, Any],
        signals: List[Dict[str, Any]],
        score: Dict[str, Any],
    ) -> Dict[str, Any]:
        mode = self._strategy_mode()
        profiles = self._direction_profiles(snapshot)
        context = snapshot.get("market_context", {})
        timeframe = self._forecast_timeframe_spec()
        lead_bar = timeframe["lead"]
        target_bar = timeframe["target"]
        background_bar = timeframe["background"]
        lead = str(profiles.get(lead_bar, {}).get("trend", "mixed") or "mixed")
        target = str(profiles.get(target_bar, {}).get("trend", "mixed") or "mixed")
        background = str(profiles.get(background_bar, {}).get("trend", "mixed") or "mixed")
        pressure = context.get("recent_price_pressure", "neutral")
        signal_types = {item.get("type", "") for item in signals}
        target_profile = profiles.get(target_bar, {})
        target_macd = target_profile.get("macd", {}) if isinstance(target_profile.get("macd"), dict) else {}
        macd_hist = to_float(target_macd.get("hist"))
        macd_slope = to_float(target_macd.get("hist_slope"))
        target_adx = to_float(target_profile.get("adx", {}).get("adx"))
        horizon = self._effective_forecast_horizon()
        candidates: List[Dict[str, Any]] = []
        if lead == "up" and target in ("mixed", "range") and background != "down" and pressure != "down":
            candidates.append({
                "direction": "做多",
                "probability": min(
                    82,
                    54
                    + (7 if background == "up" else 2)
                    + (5 if macd_slope > 0 else 0)
                    + (4 if macd_hist > 0 else 0)
                    + (4 if "volume_spike" in signal_types else 0)
                    + (3 if target_adx >= 18 else 0),
                ),
                "phase": "transition",
                "from_state": target,
                "to_state": "up",
                "scenario": f"{mode}_structure_up",
                "summary": f"{lead_bar}已转多，{target_bar}仍处过渡，预计{horizon}分钟内主结构向上同步。",
                "invalidation": f"{lead_bar}转弱或{background_bar}转空",
                "evidence": [f"{lead_bar}={lead}", f"{target_bar}={target}", f"{background_bar}={background}"],
            })
        if lead == "down" and target in ("mixed", "range") and background != "up" and pressure != "up":
            candidates.append({
                "direction": "做空",
                "probability": min(
                    82,
                    54
                    + (7 if background == "down" else 2)
                    + (5 if macd_slope < 0 else 0)
                    + (4 if macd_hist < 0 else 0)
                    + (4 if "volume_spike" in signal_types else 0)
                    + (3 if target_adx >= 18 else 0),
                ),
                "phase": "transition",
                "from_state": target,
                "to_state": "down",
                "scenario": f"{mode}_structure_down",
                "summary": f"{lead_bar}已转空，{target_bar}仍处过渡，预计{horizon}分钟内主结构向下同步。",
                "invalidation": f"{lead_bar}转强或{background_bar}转多",
                "evidence": [f"{lead_bar}={lead}", f"{target_bar}={target}", f"{background_bar}={background}"],
            })
        if not candidates:
            return self._empty_structure_forecast()
        best = max(candidates, key=lambda item: item["probability"])
        result = self._empty_structure_forecast()
        result.update(best)
        result["probability"] = max(0, min(88, int(result.get("probability", 0) or 0)))
        result["active"] = (
            score.get("final_direction") != result.get("direction")
            and result["probability"] >= max(45, self.config.forecast_push_score - 8)
        )
        result["horizon_minutes"] = horizon
        result["lead_bar"] = lead_bar
        result["structure_bar"] = target_bar
        result["background_bar"] = background_bar
        scenario_label = FORECAST_SCENARIO_LABELS.get(str(result.get("scenario")), str(result.get("scenario")))
        if scenario_label not in str(result.get("summary", "")):
            result["summary"] = f"{scenario_label}：{result.get('summary', '')}"
        return result

    def _short_levels(self, snapshot: Dict[str, Any], direction: str) -> Dict[str, str]:
        price = to_float(snapshot.get("price"))
        profiles = snapshot.get("trend_profiles", {})
        atr_15m = to_float(profiles.get("15m", {}).get("atr")) or price * 0.004
        atr_5m = to_float(profiles.get("5m", {}).get("atr")) or atr_15m * 0.45
        stop_gap = max(atr_5m * 0.7, atr_15m * 0.35, price * 0.001)
        target_gap = max(atr_15m * 0.65, price * 0.0018)
        if direction == "做多":
            return {
                "entry": f"{price - stop_gap * 0.30:.2f} - {price + stop_gap * 0.22:.2f}",
                "stop_loss": f"{price - stop_gap:.2f}",
                "take_profit": f"{price + target_gap:.2f} / {price + target_gap * 1.6:.2f}",
            }
        if direction == "做空":
            return {
                "entry": f"{price - stop_gap * 0.22:.2f} - {price + stop_gap * 0.30:.2f}",
                "stop_loss": f"{price + stop_gap:.2f}",
                "take_profit": f"{price - target_gap:.2f} / {price - target_gap * 1.6:.2f}",
            }
        return {"entry": "-", "stop_loss": "-", "take_profit": "-"}

    def _raw_direction_for_mode(self, snapshot: Dict[str, Any], context: Dict[str, Any]) -> str:
        return self._raw_direction_meta_for_mode(snapshot, context)[0]

    def _should_downgrade_direction(
        self,
        direction_guard: str,
        entry_plan: Dict[str, Any],
        raw_total_score: int,
        sentiment_led: bool = False,
    ) -> bool:
        if direction_guard:
            return True
        score_floor = self._direction_confirm_score_floor()
        quality = entry_plan.get("quality", "")
        if sentiment_led and quality == "wait_confirmation":
            if raw_total_score >= score_floor:
                return False
        if quality == "no_trade":
            return True
        if quality == "wait_confirmation":
            return raw_total_score < score_floor
        return raw_total_score < max(45, score_floor - 8)

    def _apply_selected_strategy_view(self, score: Dict[str, Any], strategy_profile: Dict[str, Any], selected: Dict[str, Any]) -> None:
        if not selected or strategy_profile.get("mode") not in ("scalp", "swing", "short", "long"):
            return
        if score.get("direction_guard") or score.get("direction_downgraded"):
            return
        if score.get("final_direction") not in ("做多", "做空"):
            return
        if selected.get("direction") != score.get("final_direction"):
            return
        score["holding_time"] = selected.get("holding_time", strategy_profile.get("holding_time"))
        if selected.get("entry") not in (None, "", "-"):
            score["entry"] = selected.get("entry")
            score["stop_loss"] = selected.get("stop_loss")
            score["take_profit"] = selected.get("take_profit")
            score["entry_plan"] = {
                **score.get("entry_plan", {}),
                "entry": selected.get("entry"),
                "stop_loss": selected.get("stop_loss"),
                "take_profit": selected.get("take_profit"),
            }
        score["trade_action_level"] = self._trade_action_level(
            int(score.get("final_trade_score", 0) or 0),
            str(score.get("final_direction")),
            score.get("entry_plan", {}),
        )

    def _strategy_profile(self, mode: Optional[str] = None) -> Dict[str, Any]:
        selected = mode if mode in STRATEGY_PROFILES else self._strategy_mode()
        profile = dict(STRATEGY_PROFILES[selected])
        profile["mode"] = selected
        return profile

    def _strategy_context_bars(self) -> Dict[str, Any]:
        mode = self._strategy_mode()
        return {
            "scalp": {
                "entry": ("1m", "3m"),
                "trade": ("3m", "5m"),
                "higher": ("15m", "1H"),
                "regime": "5m",
                "momentum": "3m",
                "volume": "1m",
                "group_weights": {"entry": 0.8, "trade": 1.4, "higher": 0.6},
            },
            "short": {
                "entry": ("1m", "3m"),
                "trade": ("5m", "15m"),
                "higher": ("1H", "4H"),
                "regime": "1H",
                "momentum": "15m",
                "volume": "5m",
                "group_weights": {"entry": 0.5, "trade": 1.35, "higher": 1.25},
            },
            "swing": {
                "entry": ("15m",),
                "trade": ("1H", "4H"),
                "higher": ("1D",),
                "regime": "1H",
                "momentum": "15m",
                "volume": "1H",
                "group_weights": {"entry": 0.65, "trade": 1.8, "higher": 1.1},
            },
            "long": {
                "entry": ("4H",),
                "trade": ("1D",),
                "higher": ("1W",),
                "regime": "1D",
                "momentum": "4H",
                "volume": "1D",
                "group_weights": {"entry": 0.7, "trade": 1.6, "higher": 1.2},
            },
        }.get(mode, {})

    def _weighted_trend_votes(
        self,
        trend_votes: Dict[str, List[Any]],
        group_weights: Dict[str, float],
    ) -> Dict[str, Any]:
        weighted = {"up": 0.0, "down": 0.0, "range": 0.0, "total": 0.0}
        groups: Dict[str, Dict[str, float]] = {}
        for group in ("entry", "trade", "higher"):
            votes = trend_votes.get(group, [])
            weight = max(0.0, to_float(group_weights.get(group), 1.0))
            counts = {
                "up": float(votes.count("up")),
                "down": float(votes.count("down")),
                "range": float(sum(1 for item in votes if item in ("range", "mixed"))),
                "total": float(len(votes)),
            }
            denominator = max(1.0, counts["total"])
            groups[group] = {
                **counts,
                "up_ratio": counts["up"] / denominator,
                "down_ratio": counts["down"] / denominator,
                "range_ratio": counts["range"] / denominator,
                "weight": weight,
            }
            weighted["up"] += counts["up"] / denominator * weight
            weighted["down"] += counts["down"] / denominator * weight
            weighted["range"] += counts["range"] / denominator * weight
            weighted["total"] += weight
        total_weight = max(weighted["total"], 1e-9)
        return {
            "groups": groups,
            "weighted_up": weighted["up"],
            "weighted_down": weighted["down"],
            "weighted_range": weighted["range"],
            "up_ratio": weighted["up"] / total_weight,
            "down_ratio": weighted["down"] / total_weight,
            "range_ratio": weighted["range"] / total_weight,
            "total_weight": weighted["total"],
        }

    def _strategy_score_bars(self) -> Dict[str, str]:
        mode = self._strategy_mode()
        return {
            "scalp": {"primary": "5m", "momentum": "3m", "entry": "1m", "higher": "15m", "background": "1H"},
            "short": {"primary": "15m", "momentum": "15m", "entry": "5m", "higher": "1H", "background": "4H"},
            "swing": {"primary": "1H", "momentum": "1H", "entry": "15m", "higher": "4H", "background": "1D"},
            "long": {"primary": "1D", "momentum": "1D", "entry": "4H", "higher": "1W", "background": "4H"},
        }.get(mode, {"primary": "15m", "momentum": "15m", "entry": "5m", "higher": "1H", "background": "4H"})

    def _strategy_pressure_spec(self) -> Dict[str, Any]:
        return {
            "scalp": {"bar": "1m", "bars": (5, 10, 15, 20), "labels": ("5m", "10m", "15m", "20m")},
            "short": {"bar": "1m", "bars": (5, 10, 15, 20), "labels": ("5m", "10m", "15m", "20m")},
            "swing": {"bar": "15m", "bars": (1, 2, 3, 4), "labels": ("15m", "30m", "45m", "60m")},
            "long": {"bar": "4H", "bars": (1, 2, 3, 6), "labels": ("4H", "8H", "12H", "24H")},
        }.get(self._strategy_mode(), {"bar": "1m", "bars": (5, 10, 15, 20), "labels": ("5m", "10m", "15m", "20m")})

    def _strategy_derivative_window_minutes(self) -> int:
        return {"scalp": 15, "short": 15, "swing": 60, "long": 240}.get(self._strategy_mode(), 15)

    def _strategy_metric_key(self, inst_id: str, bar: str) -> str:
        return f"{inst_id}:{bar}"

    def _trend_phase(
        self,
        *,
        structural_bias: str,
        bias: str,
        regime: str,
        pressure: str,
        profile: Dict[str, Any],
    ) -> str:
        breakout = str(profile.get("breakout", "none") or "none")
        macd = profile.get("macd", {}) if isinstance(profile.get("macd"), dict) else {}
        hist = to_float(macd.get("hist"))
        hist_slope = to_float(macd.get("hist_slope"))
        if regime == "squeeze":
            return "compression"
        if regime == "range":
            return "range"
        if structural_bias == "long" and pressure == "down":
            return "pullback_in_uptrend"
        if structural_bias == "short" and pressure == "up":
            return "rebound_in_downtrend"
        if breakout == "up" and pressure == "up":
            return "breakout_attempt_up"
        if breakout == "down" and pressure == "down":
            return "breakout_attempt_down"
        if bias == "long":
            return "trend_accelerating_up" if hist > 0 and hist_slope > 0 else "trend_decelerating_up"
        if bias == "short":
            return "trend_accelerating_down" if hist < 0 and hist_slope < 0 else "trend_decelerating_down"
        if pressure == "up":
            return "reversal_candidate_up"
        if pressure == "down":
            return "reversal_candidate_down"
        return "transition"

    def _recent_move_pct(self, candles: List[Dict[str, Any]], bars: int, *, live_price: float = 0.0) -> float:
        rows = tactical_candles(candles, live_price) if live_price > 0 else confirmed_candles(candles)
        if len(rows) <= bars:
            return 0.0
        latest = to_float(rows[0].get("close"))
        old = to_float(rows[bars].get("close"))
        return pct_change(latest, old)

    def _recent_drawdown_pct(self, candles: List[Dict[str, Any]], bars: int, *, live_price: float = 0.0) -> float:
        rows = tactical_candles(candles, live_price) if live_price > 0 else confirmed_candles(candles)
        if len(rows) < 2:
            return 0.0
        sample = rows[: max(2, min(len(rows), bars + 1))]
        latest = to_float(sample[0].get("close"))
        recent_high = max(to_float(item.get("high")) for item in sample)
        return pct_change(latest, recent_high)

    def _recent_rebound_pct(self, candles: List[Dict[str, Any]], bars: int, *, live_price: float = 0.0) -> float:
        rows = tactical_candles(candles, live_price) if live_price > 0 else confirmed_candles(candles)
        if len(rows) < 2:
            return 0.0
        sample = rows[: max(2, min(len(rows), bars + 1))]
        latest = to_float(sample[0].get("close"))
        recent_low = min(to_float(item.get("low")) for item in sample)
        return pct_change(latest, recent_low)

    def _recent_price_pressure(self, move_5m: float, move_10m: float, move_15m: float, volatility: Dict[str, Any]) -> str:
        atr_pct = max(to_float(volatility.get("atr_pct_15m")), 0.0)
        down_hits = 0
        up_hits = 0
        thresholds = (
            (abs(move_5m), move_5m, max(0.08, atr_pct * 0.30)),
            (abs(move_10m), move_10m, max(0.14, atr_pct * 0.45)),
            (abs(move_15m), move_15m, max(0.20, atr_pct * 0.60)),
        )
        for abs_move, signed_move, threshold in thresholds:
            if abs_move < threshold:
                continue
            if signed_move < 0:
                down_hits += 1
            elif signed_move > 0:
                up_hits += 1
        if down_hits >= 2 or move_5m <= -max(0.08, atr_pct * 0.28):
            return "down"
        if up_hits >= 2 or move_5m >= max(0.12, atr_pct * 0.35):
            return "up"
        return "neutral"

    def _direction_guard(self, direction: str, context: Dict[str, Any]) -> str:
        if direction not in ("做多", "做空"):
            return ""
        if context.get("snapshot_quality") == "insufficient":
            return "snapshot_quality_insufficient"
        mode = self._strategy_mode()
        risk = self._risk_preference()
        pressure = context.get("recent_price_pressure")
        trade_up = int(context.get("trade_up", 0) or 0)
        trade_down = int(context.get("trade_down", 0) or 0)
        sentiment = context.get("sentiment_meta") if isinstance(context.get("sentiment_meta"), dict) else {}
        sentiment_support = (
            sentiment.get("direction") == direction
            and int(sentiment.get("strength", 0) or 0) >= 3
            and risk != "conservative"
        )
        if mode == "long":
            profiles = context.get("trend_votes", {})
            higher = profiles.get("higher", []) if isinstance(profiles, dict) else []
            if direction == "做多" and "down" in higher:
                return "weekly_background_blocks_long"
            if direction == "做空" and "up" in higher:
                return "weekly_background_blocks_short"
            if direction == "做多" and pressure == "down":
                return "higher_timeframe_pressure_down_blocks_long"
            if direction == "做空" and pressure == "up":
                return "higher_timeframe_pressure_up_blocks_short"
            return ""
        if mode == "scalp":
            if risk == "aggressive":
                return ""
            if direction == "做多" and pressure == "down":
                return "recent_price_pressure_down_blocks_long"
            if direction == "做空" and pressure == "up":
                return "recent_price_pressure_up_blocks_short"
            return ""
        if mode == "swing":
            bias = context.get("bias", "neutral")
            if direction == "做多" and pressure == "down" and bias != "long":
                if sentiment_support and (bias == "long" or context.get("bias_softened")):
                    return ""
                return "recent_price_pressure_down_blocks_long"
            if direction == "做空" and pressure == "up" and bias != "short":
                if sentiment_support and (bias == "short" or context.get("bias_softened")):
                    return ""
                return "recent_price_pressure_up_blocks_short"
            return ""
        if mode == "short":
            moves = context.get("recent_move_pct") or {}
            move_20m = to_float(moves.get("20m"), 0.0)
            move_15m = abs(to_float(moves.get("15m"), 0.0))
            momentum_floor = max(0.14, move_15m * 0.85)
            if direction == "做多" and move_20m >= momentum_floor:
                return ""
            if direction == "做空" and move_20m <= -momentum_floor:
                return ""
            if sentiment_support and risk == "aggressive":
                return ""
            bias = context.get("bias", "neutral")
            if direction == "做多" and pressure == "down" and bias != "long" and trade_up < 1:
                return "recent_price_pressure_down_blocks_long"
            if direction == "做空" and pressure == "up" and bias != "short" and trade_down < 1:
                return "recent_price_pressure_up_blocks_short"
            min_trade_votes = 1 if risk == "aggressive" else 2
            if direction == "做多" and pressure == "down":
                return "recent_price_pressure_down_blocks_long"
            if direction == "做空" and pressure == "up":
                return "recent_price_pressure_up_blocks_short"
            if direction == "做多" and pressure == "neutral" and trade_up < min_trade_votes:
                if sentiment_support and risk == "aggressive" and trade_up >= 1:
                    return ""
                return "neutral_price_pressure_blocks_long_without_5m_15m_alignment"
            if direction == "做空" and pressure == "neutral" and trade_down < min_trade_votes:
                if sentiment_support and risk == "aggressive" and trade_down >= 1:
                    return ""
                return "neutral_price_pressure_blocks_short_without_5m_15m_alignment"
            return ""
        if direction == "做多" and pressure == "down":
            if risk == "aggressive" and trade_up >= 1:
                return ""
            return "recent_price_pressure_down_blocks_long"
        if direction == "做空" and pressure == "up":
            if risk == "aggressive" and trade_down >= 1:
                return ""
            return "recent_price_pressure_up_blocks_short"
        min_trade_votes = 1 if risk == "aggressive" else 2
        if direction == "做多" and pressure == "neutral" and trade_up < min_trade_votes:
            return "neutral_price_pressure_blocks_long_without_5m_15m_alignment"
        if direction == "做空" and pressure == "neutral" and trade_down < min_trade_votes:
            return "neutral_price_pressure_blocks_short_without_5m_15m_alignment"
        return ""

    def _short_strategy_view(self, snapshot: Dict[str, Any], score: Dict[str, Any]) -> Dict[str, Any]:
        context = snapshot.get("market_context", {})
        direction, tier, summary = self._short_direction_meta(snapshot, context)
        short_score = int(score.get("direction_score", score.get("raw_total_score", 0)) or 0)
        levels = self._short_levels(snapshot, direction) if direction in ("做多", "做空") else {
            "entry": "-",
            "stop_loss": "-",
            "take_profit": "-",
        }
        action = score.get("trade_action_level", "观望")
        if direction in ("做多", "做空"):
            if tier == "sentiment":
                action = "情绪领先"
            else:
                action = "动量跟踪" if tier == "momentum" else "等待结构位"
        return {
            "mode": "short",
            "label": STRATEGY_PROFILES["short"]["label"],
            "direction": direction,
            "score": short_score,
            "trade_score": short_score if direction in ("做多", "做空") else 0,
            "action_level": action,
            "entry": levels.get("entry"),
            "stop_loss": levels.get("stop_loss"),
            "take_profit": levels.get("take_profit"),
            "holding_time": STRATEGY_PROFILES["short"]["holding_time"],
            "summary": summary,
            "short_tier": tier,
        }

    def _swing_strategy_view(self, snapshot: Dict[str, Any], score: Dict[str, Any]) -> Dict[str, Any]:
        context = snapshot.get("market_context", {})
        direction, tier, summary = self._swing_direction_meta(snapshot, context)
        swing_score = int(score.get("direction_score", score.get("raw_total_score", 0)) or 0)
        levels = self._swing_levels(snapshot, direction) if direction in ("做多", "做空") else {
            "entry": "-",
            "stop_loss": "-",
            "take_profit": "-",
        }
        action = "等待结构位" if direction in ("做多", "做空") else "观望"
        if tier == "momentum" and direction in ("做多", "做空"):
            action = "动量跟踪"
        return {
            "mode": "swing",
            "label": STRATEGY_PROFILES["swing"]["label"],
            "direction": direction,
            "score": swing_score,
            "trade_score": swing_score if direction in ("做多", "做空") else 0,
            "action_level": action,
            "entry": levels.get("entry"),
            "stop_loss": levels.get("stop_loss"),
            "take_profit": levels.get("take_profit"),
            "holding_time": STRATEGY_PROFILES["swing"]["holding_time"],
            "summary": summary,
            "swing_tier": tier,
        }

    def _long_strategy_view(self, snapshot: Dict[str, Any], score: Dict[str, Any]) -> Dict[str, Any]:
        direction, tier, summary = self._long_direction_meta(snapshot, snapshot.get("market_context", {}))
        long_score = int(score.get("direction_score", score.get("raw_total_score", 0)) or 0)
        levels = self._long_levels(snapshot, direction)
        return {
            "mode": "long",
            "label": STRATEGY_PROFILES["long"]["label"],
            "direction": direction,
            "score": long_score,
            "trade_score": long_score if direction in ("做多", "做空") else 0,
            "action_level": "等待日线结构位" if direction in ("做多", "做空") else "观望",
            "entry": levels["entry"],
            "stop_loss": levels["stop_loss"],
            "take_profit": levels["take_profit"],
            "holding_time": STRATEGY_PROFILES["long"]["holding_time"],
            "summary": summary,
            "long_tier": tier,
        }

    def _scalp_levels(self, snapshot: Dict[str, Any], direction: str) -> Dict[str, str]:
        price = to_float(snapshot.get("price"))
        profiles = snapshot.get("trend_profiles", {})
        atr_5m = to_float(profiles.get("5m", {}).get("atr")) or price * 0.0018
        stop_gap = max(atr_5m * 0.45, price * 0.0008)
        target_gap = max(atr_5m * 0.75, price * 0.0012)
        if direction == "做多":
            return {
                "entry": f"{price - stop_gap * 0.25:.2f} - {price + stop_gap * 0.20:.2f}",
                "stop_loss": f"{price - stop_gap:.2f}",
                "take_profit": f"{price + target_gap:.2f} / {price + target_gap * 1.7:.2f}",
            }
        if direction == "做空":
            return {
                "entry": f"{price - stop_gap * 0.20:.2f} - {price + stop_gap * 0.25:.2f}",
                "stop_loss": f"{price + stop_gap:.2f}",
                "take_profit": f"{price - target_gap:.2f} / {price - target_gap * 1.7:.2f}",
            }
        return {"entry": "-", "stop_loss": "-", "take_profit": "-"}

    def _scalp_context(self, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        candles = snapshot.get("candles", {})
        moves = [
            self._recent_move_pct(candles.get("1m", []), bars)
            for bars in (5, 10, 15)
        ]
        atr_pct_5m = to_float(snapshot.get("trend_profiles", {}).get("5m", {}).get("atr_pct"))
        pressure = self._recent_price_pressure(
            moves[0],
            moves[1],
            moves[2],
            {"atr_pct_15m": atr_pct_5m},
        )
        order_book = snapshot.get("order_book", {})
        order_book_bias = "neutral"
        if order_book.get("available"):
            combined = (to_float(order_book.get("imbalance")) + to_float(order_book.get("imbalance_5"))) / 2
            if combined >= 0.25:
                order_book_bias = "bid_support"
            elif combined <= -0.25:
                order_book_bias = "ask_pressure"
        return {
            **snapshot.get("market_context", {}),
            "recent_price_pressure": pressure,
            "tactical_pressure": pressure,
            "order_book_bias": order_book_bias,
            "oi_price_state": self._oi_price_state(
                moves[2],
                to_float(snapshot.get("oi_change_pct_15m")),
            ),
        }

    def _scalp_strategy_view(self, snapshot: Dict[str, Any], score: Dict[str, Any]) -> Dict[str, Any]:
        candles = snapshot.get("candles", {})
        profiles = snapshot.get("trend_profiles", {})
        context = self._scalp_context(snapshot)
        volume = snapshot.get("volume", {})
        order_book = snapshot.get("order_book", {})
        threshold_5m, threshold_10m = self._scalp_move_thresholds()
        move_5m = self._recent_move_pct(candles.get("1m", []), 5)
        move_10m = self._recent_move_pct(candles.get("1m", []), 10)
        drawdown_10m = self._recent_drawdown_pct(candles.get("1m", []), 10)
        drawdown_15m = self._recent_drawdown_pct(candles.get("1m", []), 15)
        rebound_10m = self._recent_rebound_pct(candles.get("1m", []), 10)
        rebound_15m = self._recent_rebound_pct(candles.get("1m", []), 15)
        direction = self._scalp_raw_direction(snapshot, context)
        trend_votes = [profiles.get(bar, {}).get("trend") for bar in ("1m", "3m", "5m")]
        up_votes = trend_votes.count("up")
        down_votes = trend_votes.count("down")
        short_rollover = direction == "做空" and down_votes >= 1
        long_rebound = direction == "做多" and up_votes >= 1

        scalp_score = 45
        if abs(move_5m) >= threshold_5m:
            scalp_score += 18
        if abs(move_10m) >= threshold_10m:
            scalp_score += 14
        if direction == "做空" and short_rollover:
            scalp_score += 10
        if direction == "做多" and long_rebound:
            scalp_score += 10
        if volume.get("multiplier", 0.0) >= max(self.config.volume_multiplier, 1.8):
            scalp_score += 12
        if direction == "做多" and context.get("order_book_bias") == "bid_support":
            scalp_score += 8
        if direction == "做空" and context.get("order_book_bias") == "ask_pressure":
            scalp_score += 8
        if order_book.get("spread_pct", 0.0) > 0.04:
            scalp_score -= 12
        if context.get("oi_price_state") in ("price_up_oi_down_short_covering", "price_down_oi_down_long_deleveraging"):
            scalp_score -= 0 if self.config.allow_oi_divergence_momentum else 6
        if profiles.get("4H", {}).get("trend") in ("up", "down") and not self.config.allow_counter_4h_scalp:
            if direction == "做多" and profiles.get("4H", {}).get("trend") == "down":
                scalp_score -= 8
            if direction == "做空" and profiles.get("4H", {}).get("trend") == "up":
                scalp_score -= 8
        scalp_score = max(0, min(100, int(round(scalp_score))))
        trade_allowed = self._mode_allows_scalp_trade()
        levels = self._scalp_levels(snapshot, direction) if trade_allowed and direction in ("做多", "做空") else {"entry": "-", "stop_loss": "-", "take_profit": "-"}
        action = "急速异动" if direction in ("做多", "做空") else "观望"
        trade_score = scalp_score if trade_allowed and direction in ("做多", "做空") else 0
        if trade_allowed and scalp_score >= self.trade_push_score(direction) and direction in ("做多", "做空"):
            action = "可短打"
        return {
            "mode": "scalp",
            "label": STRATEGY_PROFILES["scalp"]["label"],
            "direction": direction if direction in ("做多", "做空") else "观望",
            "score": scalp_score,
            "trade_score": trade_score,
            "action_level": action,
            "entry": levels["entry"],
            "stop_loss": levels["stop_loss"],
            "take_profit": levels["take_profit"],
            "holding_time": STRATEGY_PROFILES["scalp"]["holding_time"],
            "summary": f"5m涨跌{move_5m:.3f}%，10m涨跌{move_10m:.3f}%，用于捕捉1-15分钟急速波动。",
            "move_pct_5m": move_5m,
            "move_pct_10m": move_10m,
            "drawdown_pct_10m": drawdown_10m,
            "drawdown_pct_15m": drawdown_15m,
            "rebound_pct_10m": rebound_10m,
            "rebound_pct_15m": rebound_15m,
            "trade_allowed": trade_allowed,
        }

    def _strategy_views(self, snapshot: Dict[str, Any], signals: List[Dict[str, Any]], score: Dict[str, Any]) -> Dict[str, Any]:
        mode = self._strategy_mode()
        builders = {
            "scalp": self._scalp_strategy_view,
            "short": self._short_strategy_view,
            "swing": self._swing_strategy_view,
            "long": self._long_strategy_view,
        }
        views = {mode: builders[mode](snapshot, score)}
        if mode != "scalp":
            views["scalp"] = self._scalp_strategy_view(snapshot, score)
        return views

    def _ai_request_timeout(self) -> float:
        return max(5.0, env_float("AI_REQUEST_TIMEOUT", DEFAULT_AI_REQUEST_TIMEOUT))

    def _ai_probe_timeout(self) -> float:
        return max(3.0, env_float("AI_PROBE_TIMEOUT", DEFAULT_AI_PROBE_TIMEOUT))

    def _ai_circuit_fail_threshold(self) -> int:
        return max(1, env_int("AI_CIRCUIT_FAIL_THRESHOLD", DEFAULT_AI_CIRCUIT_FAIL_THRESHOLD))

    def _ai_circuit_cooldown(self) -> int:
        return max(10, env_int("AI_CIRCUIT_COOLDOWN_SECONDS", DEFAULT_AI_CIRCUIT_COOLDOWN_SECONDS))

    def _ai_probe_interval(self) -> int:
        return max(15, env_int("AI_PROBE_INTERVAL_SECONDS", DEFAULT_AI_PROBE_INTERVAL_SECONDS))

    def _ai_rate_limit_backoff(self) -> float:
        return max(5.0, env_float("AI_RATE_LIMIT_BACKOFF_SECONDS", DEFAULT_AI_RATE_LIMIT_BACKOFF_SECONDS))

    def _ai_abnormal_alert_after(self) -> int:
        return max(60, env_int("AI_ABNORMAL_ALERT_SECONDS", DEFAULT_AI_ABNORMAL_ALERT_SECONDS))

    def _ai_abnormal_alert_cooldown(self) -> int:
        return max(300, env_int("AI_ABNORMAL_ALERT_COOLDOWN_SECONDS", DEFAULT_AI_ABNORMAL_ALERT_COOLDOWN_SECONDS))

    def _mark_ai_abnormal(self, kind: str, reason: str) -> None:
        if not self.ai_enabled or self.dry_run_ai:
            return
        cleaned = clip_push_text(reason, 800)
        if not self.ai_abnormal_since:
            self.ai_abnormal_since = time.time()
        self.ai_abnormal_kind = kind or self.ai_abnormal_kind or "request_failed"
        if cleaned:
            self.ai_last_failure_reason = cleaned

    def _clear_ai_abnormal(self) -> None:
        self.ai_abnormal_since = 0.0
        self.ai_abnormal_kind = ""
        self.ai_last_failure_reason = ""

    def _check_ai_startup_config(self) -> None:
        if not self.ai_enabled or self.dry_run_ai:
            return
        try:
            from openai import OpenAI  # noqa: F401
        except ImportError:
            self._mark_ai_abnormal("package_missing", "openai package is not installed")
            return
        api_key, _, _ = self._ai_env_config()
        if not api_key:
            self._mark_ai_abnormal("config_missing", "AI_API_KEY or OPENAI_API_KEY is not configured")

    def _build_ai_abnormal_alert_content(self) -> Tuple[str, str]:
        _, base_url, model = self._ai_env_config()
        circuit_state = self._ai_circuit_state()
        circuit_labels = {"closed": "正常", "open": "熔断开启", "half_open": "探活中"}
        kind_label = AI_ABNORMAL_KIND_LABELS.get(self.ai_abnormal_kind, self.ai_abnormal_kind or "AI 异常")
        duration = format_duration_zh(time.time() - self.ai_abnormal_since) if self.ai_abnormal_since else "-"
        reason = self.ai_last_failure_reason or "未知错误"
        title = f"[AI异常] {kind_label}"
        lines = [
            f"## OKX AI 功能异常告警 · {now_text()}",
            "",
            "### 异常概况",
            f"- 异常类型：{kind_label}",
            f"- 持续时长：{duration}",
            f"- 熔断状态：{circuit_labels.get(circuit_state, circuit_state)}",
            f"- 连续失败：{self.ai_fail_streak} 次",
            f"- 当前模型：{model or '-'}",
            f"- 接口地址：{base_url or '-'}",
            "",
            "### 失败原因",
            reason,
            "",
            "### 当前影响",
            "- 监控仍在运行，已自动切换为本地规则兜底分析",
            "- AI 恢复后将自动重新启用深分析",
            "",
            "### 建议排查",
            "- 检查 AI_API_KEY / OPENAI_API_KEY 是否正确",
            "- 检查 AI_BASE_URL 与网络连通性",
            "- 查看控制台日志与 JSON 分析日志中的 analysis.error 字段",
        ]
        return title[:120], self._join_wechat_desp(lines)

    def _maybe_push_ai_abnormal_alert(self) -> None:
        if not self.ai_enabled or self.dry_run_ai or self.replay_mode:
            return
        if not self.ai_abnormal_since:
            return
        now = time.time()
        if now - self.ai_abnormal_since < self._ai_abnormal_alert_after():
            return
        if self.ai_abnormal_alert_at and now - self.ai_abnormal_alert_at < self._ai_abnormal_alert_cooldown():
            return
        send_key = os.getenv("WECHAT_SEND_KEY", "").strip()
        if not send_key:
            console_debug(f"[{now_text()}] AI abnormal alert skipped: WECHAT_SEND_KEY is not configured")
            return
        title, desp = self._build_ai_abnormal_alert_content()
        try:
            http_post_json(
                f"https://sctapi.ftqq.com/{send_key}.send",
                {"title": title, "desp": desp},
                self.runtime_config.retry_times,
                self.runtime_config.retry_backoff,
            )
            self.ai_abnormal_alert_at = now
            console_info(f"[{now_text()}] AI abnormal alert sent to WeChat")
        except Exception as exc:
            console_warn(f"[{now_text()}] AI abnormal alert failed: {exc}")

    def _ai_env_config(self) -> Tuple[str, str, str]:
        api_key = (os.getenv("AI_API_KEY") or os.getenv("OPENAI_API_KEY") or "").strip()
        base_url = os.getenv("AI_BASE_URL", "https://www.right.codes/codex/v1").strip()
        model = (os.getenv("AI_MODEL") or DEFAULT_AI_MODEL).strip()
        return api_key, base_url, model

    def _reset_ai_client(self) -> None:
        self._ai_client = None
        self._ai_client_config = ("", "")

    def _get_ai_client(self, api_key: str, base_url: str) -> Any:
        from openai import OpenAI

        config = (api_key, base_url)
        if self._ai_client is not None and self._ai_client_config == config:
            return self._ai_client

        previous = self._ai_client_config
        self._ai_client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=self._ai_request_timeout(),
            max_retries=0,
        )
        self._ai_client_config = config
        if previous != ("", "") and previous != config:
            self.ai_fail_streak = 0
            self.ai_circuit_open_until = 0.0
            console_debug(f"[{now_text()}] AI client reloaded after config change")
        return self._ai_client

    def _ai_circuit_state(self) -> str:
        threshold = self._ai_circuit_fail_threshold()
        if self.ai_fail_streak < threshold:
            return "closed"
        if time.time() < self.ai_circuit_open_until:
            return "open"
        return "half_open"

    def _record_ai_success(self) -> None:
        if self.ai_fail_streak or self.ai_circuit_open_until or self.ai_abnormal_since:
            console_info(f"[{now_text()}] AI connection recovered, circuit closed")
        self.ai_fail_streak = 0
        self.ai_circuit_open_until = 0.0
        self._clear_ai_abnormal()

    def _record_ai_failure(self, exc: Exception) -> None:
        threshold = self._ai_circuit_fail_threshold()
        if is_auth_ai_error(exc) or not is_retryable_ai_error(exc):
            self.ai_fail_streak = threshold
        else:
            self.ai_fail_streak += 1
        self._mark_ai_abnormal("request_failed", str(exc))
        if self.ai_fail_streak >= threshold:
            self.ai_circuit_open_until = time.time() + self._ai_circuit_cooldown()
            self.ai_abnormal_kind = "circuit_open"
            console_warn(
                f"[{now_text()}] AI circuit opened for {self._ai_circuit_cooldown()}s "
                f"after failure: {exc}"
            )
        self._reset_ai_client()

    def _ai_fallback_result(
        self,
        snapshot: Dict[str, Any],
        signals: List[Dict[str, Any]],
        score: Dict[str, Any],
        content: str,
        ai_status: str,
        exc: Optional[Exception] = None,
    ) -> Dict[str, Any]:
        result = {
            "provider": "local",
            "ai_status": ai_status,
            "content": content,
            "fallback": self._local_analysis(snapshot, signals, score),
        }
        if exc is not None:
            result["error"] = str(exc)
        return result

    def _extract_ai_usage(self, response: Any) -> Optional[Dict[str, int]]:
        usage = getattr(response, "usage", None)
        if usage is None:
            return None
        result: Dict[str, int] = {}
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = getattr(usage, key, None)
            if value is None and isinstance(usage, dict):
                value = usage.get(key)
            if value is None:
                continue
            try:
                result[key] = int(value)
            except (TypeError, ValueError):
                continue
        return result or None

    def _accumulate_ai_usage(self, usage: Optional[Dict[str, int]]) -> None:
        accumulate_ai_token_stats(usage)

    def _log_ai_token_usage(
        self,
        inst_id: str,
        model: str,
        usage: Optional[Dict[str, int]],
    ) -> None:
        if not usage:
            console_debug(f"[{now_text()}] {inst_id} AI tokens: usage not returned by API")
            return
        self._accumulate_ai_usage(usage)
        console_info(
            f"[{now_text()}] {inst_id} AI tokens: "
            f"prompt={usage.get('prompt_tokens', '-')} "
            f"completion={usage.get('completion_tokens', '-')} "
            f"total={usage.get('total_tokens', '-')} "
            f"model={model}"
        )

    def _chat_completion_with_retry(self, client: Any, model: str, prompt: str) -> Any:
        last_error: Optional[Exception] = None
        retry_times = self.runtime_config.retry_times
        retry_backoff = self.runtime_config.retry_backoff
        api_key, base_url, _ = self._ai_env_config()

        for attempt in range(1, retry_times + 1):
            try:
                return client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.2,
                    timeout=self._ai_request_timeout(),
                )
            except Exception as exc:
                last_error = exc
                if not is_retryable_ai_error(exc):
                    raise
                if attempt >= retry_times:
                    break
                sleep_seconds = retry_backoff * attempt
                if is_rate_limit_ai_error(exc):
                    sleep_seconds = max(sleep_seconds, self._ai_rate_limit_backoff())
                console_debug(
                    f"[{now_text()}] ai-chat failed, retry {attempt}/{retry_times}: {exc}; "
                    f"sleep {sleep_seconds:.1f}s"
                )
                if is_connection_ai_error(exc):
                    self._reset_ai_client()
                    client = self._get_ai_client(api_key, base_url)
                time.sleep(sleep_seconds)

        raise RuntimeError(f"ai-chat failed after {retry_times} retries: {last_error}")

    def _probe_ai_connection(self, model: str) -> Tuple[bool, str]:
        api_key, base_url, _ = self._ai_env_config()
        if not api_key:
            return False, "AI_API_KEY or OPENAI_API_KEY is not configured"
        try:
            client = self._get_ai_client(api_key, base_url)
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=5,
                temperature=0.0,
                timeout=self._ai_probe_timeout(),
            )
            self._accumulate_ai_usage(self._extract_ai_usage(response))
            return True, ""
        except Exception as exc:
            reason = str(exc)
            console_debug(f"[{now_text()}] AI probe failed: {exc}")
            self._reset_ai_client()
            return False, reason

    def _maybe_probe_ai_connection(self, model: str) -> bool:
        now = time.time()
        if now - self.ai_last_probe_at < self._ai_probe_interval():
            return False
        self.ai_last_probe_at = now
        console_debug(f"[{now_text()}] AI circuit probing...")
        ok, reason = self._probe_ai_connection(model)
        if ok:
            self._record_ai_success()
            return True
        self.ai_fail_streak = self._ai_circuit_fail_threshold()
        self.ai_circuit_open_until = time.time() + self._ai_circuit_cooldown()
        self._mark_ai_abnormal("probe_failed", reason or "AI probe failed")
        return False

    def _build_ai_success_result(
        self,
        base_url: str,
        model: str,
        output_text: str,
        snapshot: Dict[str, Any],
        signals: List[Dict[str, Any]],
        score: Dict[str, Any],
        usage: Optional[Dict[str, int]] = None,
    ) -> Dict[str, Any]:
        parsed_raw = extract_json_object(output_text)
        parsed = normalize_ai_parsed(parsed_raw, score)
        valid, errors = self._validate_ai_result(parsed)
        if not valid:
            detail = "; ".join(errors[:6]) if errors else "unknown"
            if parsed_raw is not None:
                console_warn(f"[{now_text()}] AI validation failed after normalize: {detail}")
            else:
                console_warn(f"[{now_text()}] AI output is not valid JSON: {detail}")
        result = {
            "provider": "deepseek" if "deepseek" in base_url else "openai",
            "model": model,
            "ai_status": "closed",
            "content": output_text,
            "parsed": parsed,
            "valid_json": valid,
            "validation_errors": errors,
            "fallback": None if valid else self._local_analysis(snapshot, signals, score),
        }
        if usage:
            result["usage"] = usage
        return result

    def _ai_call_min_interval(self) -> int:
        return max(15, env_int("AI_CALL_MIN_INTERVAL_SECONDS", DEFAULT_AI_CALL_MIN_INTERVAL_SECONDS))

    def _ai_call_dedup_allows(self, inst_id: str, fingerprint: str) -> bool:
        last_fp = self.last_ai_fingerprint.get(inst_id, "")
        last_at = self.last_ai_call_at.get(inst_id, 0.0)
        return (
            fingerprint != last_fp
            or last_at <= 0
            or self._now_ts() - last_at >= self._ai_call_min_interval()
        )

    def _structure_forecast_active(self, score: Dict[str, Any]) -> bool:
        forecast = score.get("structure_forecast") if isinstance(score.get("structure_forecast"), dict) else {}
        return bool(forecast.get("active")) and str(forecast.get("scenario", "none") or "none") not in ("none", "")

    def _signal_fingerprint(self, signals: List[Dict[str, Any]], score: Dict[str, Any]) -> str:
        signal_types = ",".join(sorted(item.get("type", "") for item in signals if item.get("type"))) or "none"
        raw_score = int(score.get("raw_total_score", 0) or 0)
        score_bucket = (raw_score // 5) * 5
        return f"{signal_types}:{score.get('direction', '观望')}:{score_bucket}"

    def evaluate_ai_trigger(
        self,
        inst_id: str,
        signals: List[Dict[str, Any]],
        score: Dict[str, Any],
        snapshot: Dict[str, Any],
    ) -> Dict[str, Any]:
        if not signals:
            return {
                "level": "L0",
                "should_call_ai": False,
                "ai_invoked": False,
                "reasons": [],
                "fingerprint": "",
                "local_hint": self.build_local_hint(score, signals),
            }

        signal_types = {item.get("type", "") for item in signals}
        reasons: List[str] = []
        scalp_view = score.get("strategy_views", {}).get("scalp", {})
        level = "L1"

        if (
            self.config.signal_spike_enabled
            and scalp_view.get("action_level") in ("急速异动", "可短打")
            and scalp_view.get("score", 0) >= self.config.spike_push_score
        ):
            level = "L3"
            reasons.append("scalp_spike")

        if "funding_hot" in signal_types and abs(to_float(snapshot.get("funding_rate"))) >= self.config.funding_abs_threshold * 1.25:
            level = "L3"
            reasons.append("funding_extreme")

        sentiment_meta = score.get("sentiment_meta") if isinstance(score.get("sentiment_meta"), dict) else {}
        if sentiment_meta.get("direction") in ("做多", "做空") and int(sentiment_meta.get("strength", 0) or 0) >= 3:
            if score.get("raw_direction") == sentiment_meta.get("direction") and level == "L1":
                level = "L2"
                reasons.append("sentiment_leading")
            elif score.get("raw_direction") in ("做多", "做空") and score.get("final_direction") == "观望":
                if level != "L3":
                    level = "L2"
                reasons.append("sentiment_structure_conflict")

        if level != "L3":
            l2_hit = False
            if len(signals) >= 2:
                l2_hit = True
                reasons.append("multi_signal")
            elif signal_types.intersection({"oi_change", "funding_fast_change", "long_short_extreme"}) and int(
                sentiment_meta.get("strength", 0) or 0
            ) >= 2:
                l2_hit = True
                reasons.append("sentiment_signals")
            elif self._trade_signals_qualify_l2(signal_types):
                l2_hit = True
                reasons.append("trade_signal")
            elif score.get("raw_total_score", 0) >= 72:
                l2_hit = True
                reasons.append("raw_score_high")
            elif len(signal_types.intersection(WATCH_TRIGGER_SIGNALS)) >= 2:
                l2_hit = True
                reasons.append("multi_watch")
            elif self._structure_forecast_active(score):
                l2_hit = True
                reasons.append("structure_forecast_active")
            if l2_hit:
                level = "L2"

        if not reasons:
            reasons = [item.get("type", "signal") for item in signals[:3]]

        fingerprint = self._signal_fingerprint(signals, score)
        should_call_ai = False
        skip_reason = ""
        if self.ai_enabled and level in ("L2", "L3"):
            qualifies = level == "L3" or self._l2_qualifies_ai_call(reasons, signal_types, score, snapshot)
            if qualifies and self._ai_call_dedup_allows(inst_id, fingerprint):
                should_call_ai = True
            elif qualifies:
                skip_reason = "fingerprint_cooldown"
            elif level == "L2":
                skip_reason = "l2_not_qualified"

        result = {
            "level": level,
            "should_call_ai": should_call_ai,
            "ai_invoked": False,
            "reasons": reasons,
            "fingerprint": fingerprint,
            "local_hint": self.build_local_hint(score, signals),
        }
        if skip_reason:
            result["skip_reason"] = skip_reason
        return result

    def build_local_hint(self, score: Dict[str, Any], signals: List[Dict[str, Any]]) -> Dict[str, Any]:
        return {
            "reference_only": True,
            "screening_only": True,
            "direction": score.get("direction", "观望"),
            "raw_direction": score.get("raw_direction"),
            "raw_total_score": score.get("raw_total_score", 0),
            "final_trade_score": score.get("final_trade_score", 0),
            "risk_level": score.get("risk_level"),
            "entry": score.get("entry"),
            "stop_loss": score.get("stop_loss"),
            "take_profit": score.get("take_profit"),
            "market_regime": score.get("market_regime"),
            "trade_action_level": score.get("trade_action_level"),
            "sentiment_direction": (score.get("sentiment_meta") or {}).get("direction"),
            "sentiment_strength": (score.get("sentiment_meta") or {}).get("strength"),
            "signal_types": [item.get("type", "") for item in signals],
        }

    def build_local_screening(
        self,
        snapshot: Dict[str, Any],
        signals: List[Dict[str, Any]],
        score: Dict[str, Any],
        trigger: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        trigger = trigger or {}
        profiles = snapshot.get("trend_profiles", {}) if isinstance(snapshot.get("trend_profiles"), dict) else {}
        context = snapshot.get("market_context", {}) if isinstance(snapshot.get("market_context"), dict) else {}
        trend_bits = [
            f"{bar}={profiles.get(bar, {}).get('trend', '-')}"
            for bar in ("5m", "15m", "1H")
            if profiles.get(bar)
        ]
        signal_rows = [
            {"type": item.get("type", ""), "desc": item.get("desc", "")}
            for item in signals[:6]
        ]
        signal_text = "、".join(
            str(item.get("desc") or item.get("type") or "")
            for item in signal_rows
            if item.get("type") or item.get("desc")
        ) or "无显著信号"
        summary = (
            f"本地已检测 {len(signals)} 个信号（{signal_text}）；"
            f"结构={score.get('market_regime', '-')}，短窗压力={context.get('recent_price_pressure', 'neutral')}；"
            f"{' / '.join(trend_bits) if trend_bits else '周期摘要不足'}"
        )
        return {
            "role": "past_summary_and_signal_filter",
            "trigger_level": trigger.get("level", "L0"),
            "trigger_reasons": trigger.get("reasons") or [],
            "summary": summary,
            "signals": signal_rows,
            "local_bias": score.get("direction", "观望"),
            "raw_direction": score.get("raw_direction"),
            "raw_total_score": score.get("raw_total_score", 0),
            "final_trade_score": score.get("final_trade_score", 0),
            "market_regime": score.get("market_regime"),
            "recent_price_pressure": context.get("recent_price_pressure", "neutral"),
            "note": "本地只做历史回顾与信号过滤，后续操作方向由 AI forward_view 给出。",
        }

    def trade_push_score(self, direction: str) -> int:
        """trade 推送门槛：做多用 push_score，做空用 short_push_score。"""
        if direction == "做空":
            return max(0, min(100, int(self.short_push_score)))
        return max(0, min(100, int(self.push_score)))

    def _scalp_view(self, score: Dict[str, Any]) -> Dict[str, Any]:
        view = score.get("strategy_views", {}).get("scalp", {})
        return view if isinstance(view, dict) else {}

    def _is_scalp_spike_active(self, scalp_view: Dict[str, Any]) -> bool:
        if not self.config.signal_spike_enabled:
            return False
        return (
            scalp_view.get("action_level") in ("急速异动", "可短打")
            and scalp_view.get("direction") in ("做多", "做空")
            and int(scalp_view.get("score", 0) or 0) >= self.config.spike_push_score
        )

    def _trade_signals_qualify_l2(self, signal_types: set) -> bool:
        if not signal_types.intersection(TRADE_TRIGGER_SIGNALS):
            return False
        if not self.config.l2_require_volume_or_structure:
            return True
        strong = {"volume_spike", "structure_break", "oi_change", "order_book_imbalance"}
        if signal_types.intersection(strong):
            return True
        if "macd_momentum_change" in signal_types and len(signal_types) >= 2:
            return True
        return False

    def _structure_forecast_direction(self, score: Dict[str, Any]) -> str:
        forecast = score.get("structure_forecast") if isinstance(score.get("structure_forecast"), dict) else {}
        if not forecast.get("active"):
            return ""
        direction = str(forecast.get("direction", "观望") or "观望")
        return direction if direction in ("做多", "做空") else ""

    def _forward_view_direction(self, final_decision: Dict[str, Any]) -> str:
        forward = final_decision.get("forward_view") if isinstance(final_decision.get("forward_view"), dict) else {}
        direction = forward.get("direction") or final_decision.get("direction", "观望")
        return str(direction or "观望")

    def _forward_forecast_aligned(self, final_decision: Dict[str, Any], score: Dict[str, Any]) -> bool:
        if not self.config.forward_require_forecast_alignment:
            return True
        forecast_dir = self._structure_forecast_direction(score)
        if not forecast_dir:
            return True
        source = str(final_decision.get("decision_source", "") or "")
        if source != "ai":
            return True
        forward_dir = self._forward_view_direction(final_decision)
        if forward_dir not in ("做多", "做空"):
            return False
        return forward_dir == forecast_dir

    def _forward_alignment_block_reason(
        self,
        final_decision: Dict[str, Any],
        score: Dict[str, Any],
        *,
        push_kind: str = "trade",
    ) -> str:
        if not self.config.forward_require_forecast_alignment:
            return ""
        forecast_dir = self._structure_forecast_direction(score)
        if not forecast_dir:
            return ""
        source = str(final_decision.get("decision_source", "") or "")
        if push_kind == "forecast" and source != "ai":
            return ""
        if push_kind == "trade" and source != "ai":
            return ""
        if self._forward_forecast_aligned(final_decision, score):
            return ""
        forward_dir = self._forward_view_direction(final_decision)
        return f"forward_forecast_mismatch({forward_dir}!={forecast_dir})"

    def _l2_qualifies_ai_call(
        self,
        trigger_reasons: List[str],
        signal_types: set,
        score: Dict[str, Any],
        snapshot: Dict[str, Any],
    ) -> bool:
        reason_set = set(trigger_reasons or [])
        if reason_set.intersection(
            {
                "multi_signal",
                "multi_watch",
                "sentiment_leading",
                "sentiment_structure_conflict",
                "structure_forecast_active",
            }
        ):
            return True
        if "raw_score_high" in reason_set:
            return True
        if self._structure_forecast_active(score):
            return True
        context = snapshot.get("market_context", {}) if isinstance(snapshot.get("market_context"), dict) else {}
        if context.get("regime") in ("trend_up", "trend_down", "squeeze"):
            if signal_types.intersection({"structure_break", "volume_spike", "oi_change", "order_book_imbalance"}):
                return True
        if signal_types == {"macd_momentum_change"}:
            return False
        if "trade_signal" in reason_set and len(signal_types) <= 1:
            raw_score = int(score.get("raw_total_score", 0) or 0)
            if signal_types == {"structure_break"} and raw_score >= 65:
                return True
            return False
        if "sentiment_signals" in reason_set:
            if len(signal_types) >= 2:
                return True
            sentiment_meta = score.get("sentiment_meta") if isinstance(score.get("sentiment_meta"), dict) else {}
            if int(sentiment_meta.get("strength", 0) or 0) >= 3:
                return True
        return False

    def _push_cooldown_seconds(self, push_kind: str) -> int:
        if push_kind == "spike":
            return max(0, int(self.runtime_config.spike_push_cooldown_seconds))
        if push_kind == "watch":
            return max(0, int(self.runtime_config.watch_push_cooldown_seconds))
        if push_kind == "forecast":
            return max(0, int(self.runtime_config.forecast_push_cooldown_seconds))
        return max(0, int(self.runtime_config.push_cooldown_seconds))

    def _effective_push_confidence(
        self,
        final_decision: Dict[str, Any],
        score: Dict[str, Any],
        push_kind: str,
    ) -> int:
        confidence = max(0, min(100, int(final_decision.get("confidence", 0) or 0)))
        if push_kind != "spike":
            return confidence
        scalp_view = self._scalp_view(score)
        if self._is_scalp_spike_active(scalp_view):
            return max(confidence, int(scalp_view.get("score", 0) or 0))
        return confidence

    def _downgrade_push_recommendation(self, current: str, confidence: int, fallback: str = "none") -> str:
        if current == "trade":
            if confidence >= self.config.watch_push_score and self.config.signal_watch_enabled:
                return "watch"
            return fallback
        if current == "spike":
            if confidence >= self.config.watch_push_score and self.config.signal_watch_enabled:
                return "watch"
            return fallback
        return current

    def _apply_decision_post_audit(
        self,
        final_decision: Dict[str, Any],
        score: Dict[str, Any],
        signals: List[Dict[str, Any]],
        trigger: Dict[str, Any],
        snapshot: Dict[str, Any],
    ) -> Dict[str, Any]:
        audited = dict(final_decision)
        audit: Dict[str, Any] = {"action": "kept", "reasons": []}
        scalp_view = self._scalp_view(score)
        scalp_active = self._is_scalp_spike_active(scalp_view)
        scalp_dir = scalp_view.get("direction")
        context = snapshot.get("market_context", {})
        pressure = context.get("recent_price_pressure", "neutral")
        trigger_reasons = trigger.get("reasons") or []

        if (
            self.config.l3_local_spike_push
            and trigger.get("level") == "L3"
            and "scalp_spike" in trigger_reasons
            and scalp_active
            and scalp_dir in ("做多", "做空")
        ):
            audited["direction"] = scalp_dir
            audited["confidence"] = max(int(audited.get("confidence", 0) or 0), int(scalp_view.get("score", 0) or 0))
            audited["push_recommendation"] = "spike"
            audit["action"] = "l3_local_spike"
            audit["reasons"].append("L3/scalp_spike_local")
            audited["post_audit"] = audit
            audited["scalp_direction"] = scalp_dir
            audited["scalp_score"] = int(scalp_view.get("score", 0) or 0)
            return audited

        direction = str(audited.get("direction", "观望") or "观望")
        push = str(audited.get("push_recommendation", "none") or "none")
        confidence = int(audited.get("confidence", 0) or 0)

        if scalp_active and scalp_dir in ("做多", "做空"):
            audited["scalp_direction"] = scalp_dir
            audited["scalp_score"] = int(scalp_view.get("score", 0) or 0)
            if direction == scalp_dir and push == "trade" and trigger.get("level") == "L3":
                audited["push_recommendation"] = "spike"
                audited["confidence"] = max(confidence, int(scalp_view.get("score", 0) or 0))
                audit["action"] = "downgraded"
                audit["reasons"].append("l3_trade_to_spike")
                push = "spike"
                confidence = int(audited.get("confidence", 0) or 0)

        if push == "spike" and scalp_active:
            audited["confidence"] = max(confidence, int(scalp_view.get("score", 0) or 0))
            confidence = int(audited.get("confidence", 0) or 0)

        if self.config.ai_conflict_guard and push in ("trade", "spike"):
            blocked = False
            if (
                scalp_active
                and scalp_dir in ("做多", "做空")
                and direction in ("做多", "做空")
                and direction != scalp_dir
            ):
                audited["push_recommendation"] = self._downgrade_push_recommendation(push, confidence)
                audit["action"] = "blocked"
                audit["reasons"].append("opposes_scalp_spike")
                blocked = True
            elif direction == "做空" and pressure == "up" and push == "trade":
                audited["push_recommendation"] = self._downgrade_push_recommendation("trade", confidence)
                audit["action"] = "blocked"
                audit["reasons"].append("pressure_up_blocks_short_trade")
                blocked = True
            elif direction == "做多" and pressure == "down" and push == "trade":
                audited["push_recommendation"] = self._downgrade_push_recommendation("trade", confidence)
                audit["action"] = "blocked"
                audit["reasons"].append("pressure_down_blocks_long_trade")
                blocked = True
            elif push == "trade" and direction in ("做多", "做空") and self._direction_guard(direction, context):
                audited["push_recommendation"] = self._downgrade_push_recommendation("trade", confidence)
                audit["action"] = "blocked"
                audit["reasons"].append(self._direction_guard(direction, context) or "direction_guard")
                blocked = True

            if not blocked and push == "trade" and direction in ("做多", "做空"):
                threshold = self.trade_push_score(direction)
                if threshold <= confidence <= threshold + CONFIDENCE_HUG_MARGIN:
                    if scalp_active and scalp_dir in ("做多", "做空") and direction != scalp_dir:
                        audited["push_recommendation"] = "none"
                        audit["action"] = "blocked"
                        audit["reasons"].append("confidence_hug_opposes_scalp")
                    elif context.get("regime") in ("range", "mixed") and context.get("bias", "neutral") == "neutral":
                        audited["push_recommendation"] = self._downgrade_push_recommendation("trade", confidence)
                        audit["action"] = "downgraded"
                        audit["reasons"].append("confidence_hug_range_neutral")

        if str(audited.get("push_recommendation", "none") or "none") == "watch" and direction == "观望":
            audit.setdefault("reasons", [])
            if "watch_no_direction" not in audit["reasons"]:
                audit["reasons"].append("watch_no_direction")

        push = str(audited.get("push_recommendation", "none") or "none")
        if push == "trade":
            align_reason = self._forward_alignment_block_reason(audited, score, push_kind="trade")
            if align_reason:
                audited["push_recommendation"] = self._downgrade_push_recommendation(
                    "trade",
                    int(audited.get("confidence", 0) or 0),
                )
                audit["action"] = "blocked"
                audit["reasons"].append(align_reason)

        audited["post_audit"] = audit
        audited = self._apply_ai_calibration_audit(audited, snapshot)
        return audited

    def _in_reverse_trade_cooldown(self, inst_id: str, direction: str) -> bool:
        if direction not in ("做多", "做空"):
            return False
        last = self.last_trade_push_at.get(inst_id)
        if not last:
            return False
        last_dir, last_at = last
        if last_dir == direction:
            return False
        cooldown = max(0, int(self.runtime_config.reverse_trade_cooldown_seconds))
        return cooldown > 0 and time.time() - last_at < cooldown

    def _local_push_recommendation(self, score: Dict[str, Any], signals: List[Dict[str, Any]]) -> str:
        kind = self._push_kind(score, signals)
        if kind == "trade":
            return "watch" if int(score.get("raw_total_score", 0) or 0) >= self.config.watch_push_score else "none"
        if kind == "spike" and not self.config.l3_local_spike_push:
            return "watch" if int(score.get("raw_total_score", 0) or 0) >= self.config.watch_push_score else "none"
        return kind or "none"

    def _derive_confidence_from_parsed(self, parsed: Dict[str, Any], score: Dict[str, Any]) -> int:
        raw_conf: Optional[int] = None
        raw_value = parsed.get("confidence")
        if isinstance(raw_value, (int, float)):
            raw_conf = max(0, min(100, int(round(raw_value))))

        forward = parsed.get("forward_view") if isinstance(parsed.get("forward_view"), dict) else {}
        forward_prob: Optional[int] = None
        forward_prob_raw = forward.get("probability")
        if isinstance(forward_prob_raw, (int, float)):
            forward_prob = max(0, min(100, int(round(forward_prob_raw))))
            raw_conf = forward_prob if raw_conf is None else max(raw_conf, forward_prob)

        direction = parsed.get("direction", "观望")
        if raw_conf is None:
            if direction in ("做多", "做空"):
                raw_conf = max(0, min(100, int(score.get("final_trade_score", score.get("raw_total_score", 0)))))
            else:
                raw_conf = max(0, min(100, int(score.get("raw_total_score", 0))))

        raw_total = int(score.get("raw_total_score", 0) or 0)
        short_view = score.get("strategy_views", {}).get("short", {})
        short_score = int(short_view.get("score", 0) or 0) if isinstance(short_view, dict) else 0
        cap = max(raw_total + 15, short_score + 8, 52)
        if isinstance(forward_prob, int):
            cap = max(cap, min(92, forward_prob + 6))
        scalp_view = self._scalp_view(score)
        if self._is_scalp_spike_active(scalp_view) and direction == scalp_view.get("direction"):
            cap = max(cap, int(scalp_view.get("score", 0) or 0))
        return max(0, min(100, min(raw_conf, cap)))

    def _derive_push_recommendation(
        self,
        parsed: Dict[str, Any],
        score: Dict[str, Any],
        signals: List[Dict[str, Any]],
        trigger: Dict[str, Any],
    ) -> str:
        explicit = str(parsed.get("push_recommendation", "") or "").strip().lower()
        if explicit in ("none", "watch", "trade", "spike"):
            if explicit == "trade":
                scalp_view = score.get("strategy_views", {}).get("scalp", {})
                ai_dir = parsed.get("direction", "观望")
                if (
                    self._is_scalp_spike_active(scalp_view)
                    and scalp_view.get("direction") in ("做多", "做空")
                    and ai_dir in ("做多", "做空")
                    and ai_dir != scalp_view.get("direction")
                    and trigger.get("level") == "L3"
                ):
                    return "spike"
            return explicit

        direction = parsed.get("direction", "观望")
        confidence = self._derive_confidence_from_parsed(parsed, score)
        audit = parsed_data_quality(parsed)
        audit_overall = str(audit.get("overall", ""))

        if audit_overall in AI_DATA_QUALITY_UNTRUSTED:
            signal_types = {item.get("type", "") for item in signals}
            if signal_types.intersection(WATCH_TRIGGER_SIGNALS) and confidence >= self.config.watch_push_score:
                return "watch"
            return "none"

        scalp_view = score.get("strategy_views", {}).get("scalp", {})
        if (
            trigger.get("level") == "L3"
            and scalp_view.get("action_level") in ("急速异动", "可短打")
            and direction in ("做多", "做空")
        ):
            return "spike"

        if direction in ("做多", "做空") and confidence >= self.trade_push_score(direction):
            return "trade"

        signal_types = {item.get("type", "") for item in signals}
        if direction == "观望" and confidence >= self.config.watch_push_score and signal_types.intersection(WATCH_TRIGGER_SIGNALS):
            return "watch"

        return "none"

    def _build_local_final_decision(
        self,
        score: Dict[str, Any],
        signals: List[Dict[str, Any]],
        trigger: Dict[str, Any],
        snapshot: Optional[Dict[str, Any]] = None,
        *,
        decision_source: str = "local_screening",
        ai_called: bool = False,
    ) -> Dict[str, Any]:
        push_recommendation = self._local_push_recommendation(score, signals)
        screening = self.build_local_screening(snapshot or {}, signals, score, trigger)
        local_bias = str(screening.get("local_bias", "观望") or "观望")
        direction = "观望"
        confidence = int(score.get("raw_total_score", 0) or 0)
        return {
            "direction": direction,
            "local_bias": local_bias,
            "local_screening": screening,
            "confidence": max(0, min(100, confidence)),
            "push_recommendation": push_recommendation,
            "entry": "-",
            "stop_loss": "-",
            "take_profit": "-",
            "risk_level": score.get("risk_level", "中"),
            "summary": screening.get("summary", "本地回顾与信号筛查"),
            "reasons": [item.get("desc", item.get("type", "")) for item in signals[:3]],
            "rule_audit": {"overall": "本地筛查", "warnings": []},
            "decision_source": decision_source,
            "ai_called": ai_called,
            "trigger_level": trigger.get("level", "L0"),
            "local_hint_direction": local_bias,
            "market_regime": score.get("market_regime"),
            "snapshot_quality": score.get("snapshot_quality", {}),
            "strategy_label": score.get("strategy_label"),
            "risk_preference": score.get("risk_preference"),
        }

    def _build_ai_final_decision(
        self,
        analysis: Dict[str, Any],
        score: Dict[str, Any],
        signals: List[Dict[str, Any]],
        trigger: Dict[str, Any],
        snapshot: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        parsed = analysis.get("parsed") if isinstance(analysis.get("parsed"), dict) else {}
        audit = parsed_data_quality(parsed)
        forward = parsed.get("forward_view") if isinstance(parsed.get("forward_view"), dict) else {}
        direction = forward.get("direction") or parsed.get("direction", "观望")
        confidence = self._derive_confidence_from_parsed(parsed, score)
        push_recommendation = self._derive_push_recommendation(parsed, score, signals, trigger)
        screening = self.build_local_screening(snapshot or {}, signals, score, trigger)

        if data_quality_untrusted(parsed):
            if push_recommendation == "trade":
                signal_types = {item.get("type", "") for item in signals}
                push_recommendation = "watch" if signal_types.intersection(WATCH_TRIGGER_SIGNALS) else "none"
            if direction in ("做多", "做空") and push_recommendation == "none":
                direction = "观望"

        if parsed.get("risk_level") == "高" and confidence < max(50, self.trade_push_score(direction) - 10) and push_recommendation == "trade":
            push_recommendation = "watch"

        entry_plan = forward.get("entry_plan") if isinstance(forward.get("entry_plan"), dict) else {}
        entry = entry_plan.get("entry") or parsed.get("entry", "-")
        stop_loss = entry_plan.get("stop_loss") or parsed.get("stop_loss", "-")
        take_profit = entry_plan.get("take_profit") or parsed.get("take_profit", "-")
        summary = forward.get("summary") or parsed.get("suggestion", "")
        reasons = parsed.get("reasons") if isinstance(parsed.get("reasons"), list) else []
        return {
            "direction": direction,
            "confidence": confidence,
            "push_recommendation": push_recommendation,
            "entry": entry,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "risk_level": parsed.get("risk_level", "中"),
            "summary": summary,
            "reasons": reasons,
            "rule_audit": audit,
            "decision_source": "ai",
            "ai_called": True,
            "trigger_level": trigger.get("level", "L0"),
            "local_hint_direction": screening.get("local_bias", score.get("direction", "观望")),
            "local_bias": screening.get("local_bias", score.get("direction", "观望")),
            "local_screening": screening,
            "forward_view": forward,
            "market_regime": score.get("market_regime"),
            "snapshot_quality": score.get("snapshot_quality", {}),
            "strategy_label": score.get("strategy_label"),
            "risk_preference": score.get("risk_preference"),
            "score_comment": parsed_analysis_note(parsed),
        }

    def merge_final_decision(
        self,
        analysis: Optional[Dict[str, Any]],
        score: Dict[str, Any],
        signals: List[Dict[str, Any]],
        trigger: Dict[str, Any],
        snapshot: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if analysis and analysis.get("valid_json") and isinstance(analysis.get("parsed"), dict):
            return self._build_ai_final_decision(analysis, score, signals, trigger, snapshot)

        source = "local_fallback" if trigger.get("ai_invoked") else "local_screening"
        ai_called = bool(trigger.get("ai_invoked"))
        return self._build_local_final_decision(
            score,
            signals,
            trigger,
            snapshot,
            decision_source=source,
            ai_called=ai_called,
        )

    def push_gate(
        self,
        final_decision: Dict[str, Any],
        signals: List[Dict[str, Any]],
        score: Optional[Dict[str, Any]] = None,
    ) -> str:
        if not signals:
            return ""

        recommendation = str(final_decision.get("push_recommendation", "none") or "none")
        if recommendation == "none":
            return ""

        score = score or {}
        confidence = self._effective_push_confidence(final_decision, score, recommendation)

        if recommendation == "spike":
            if not self.config.signal_spike_enabled:
                return ""
            if confidence < self.config.spike_push_score:
                return ""
            return "spike"

        if recommendation == "watch":
            if not self.config.signal_watch_enabled:
                return ""
            if confidence < self.config.watch_push_score:
                return ""
            if final_decision.get("decision_source") == "ai":
                return "watch"
            signal_types = {item.get("type", "") for item in signals}
            if not signal_types.intersection(WATCH_TRIGGER_SIGNALS) and final_decision.get("direction") == "观望":
                return ""
            return "watch"

        if recommendation == "trade":
            if not self.config.signal_trade_enabled:
                return ""
            if final_decision.get("direction") not in ("做多", "做空"):
                return ""
            if confidence < self.trade_push_score(final_decision.get("direction", "")):
                return ""
            if self._forward_alignment_block_reason(final_decision, score, push_kind="trade"):
                return ""
            return "trade"

        return ""

    def _forecast_push_blocked(
        self,
        forecast: Dict[str, Any],
        final_decision: Dict[str, Any],
        signals: List[Dict[str, Any]],
        score: Dict[str, Any],
        snapshot: Dict[str, Any],
    ) -> str:
        direction = str(forecast.get("direction", "观望") or "观望")
        if direction not in ("做多", "做空"):
            return "invalid_direction"

        scalp_view = self._scalp_view(score)
        if self._is_scalp_spike_active(scalp_view):
            if scalp_view.get("direction") == direction:
                return "scalp_already_covers"
            if scalp_view.get("direction") in ("做多", "做空") and direction != scalp_view.get("direction"):
                return "opposes_active_scalp"

        pressure = snapshot.get("market_context", {}).get("recent_price_pressure", "neutral")
        if direction == "做多" and pressure == "down":
            return "pressure_down"
        if direction == "做空" and pressure == "up":
            return "pressure_up"

        if final_decision.get("direction") == direction:
            confirmed_kind = self.push_gate(final_decision, signals, score)
            if confirmed_kind in ("trade", "spike"):
                return f"confirmed_{confirmed_kind}_active"
            if int(score.get("final_trade_score", 0) or 0) >= self.config.forecast_push_score:
                return "structure_already_confirmed"

        align_reason = self._forward_alignment_block_reason(final_decision, score, push_kind="forecast")
        if align_reason:
            return align_reason

        return ""

    def forecast_push_gate(
        self,
        forecast: Dict[str, Any],
        final_decision: Dict[str, Any],
        signals: List[Dict[str, Any]],
        score: Dict[str, Any],
        snapshot: Dict[str, Any],
    ) -> str:
        if not self.config.signal_forecast_enabled:
            return ""
        if not forecast.get("active"):
            return ""
        if forecast.get("scenario_enabled") is False:
            return ""
        probability = int(forecast.get("calibrated_probability", forecast.get("probability", 0)) or 0)
        threshold = int(forecast.get("effective_push_threshold", self.config.forecast_push_score) or self.config.forecast_push_score)
        if probability < threshold:
            return ""
        if self._forecast_push_blocked(forecast, final_decision, signals, score, snapshot):
            return ""
        return "forecast"

    def _push_gate_block_reason(
        self,
        final_decision: Dict[str, Any],
        signals: List[Dict[str, Any]],
        score: Dict[str, Any],
        recommendation: str,
    ) -> str:
        confidence = self._effective_push_confidence(final_decision, score, recommendation)
        if recommendation == "spike":
            if not self.config.signal_spike_enabled:
                return "spike_disabled"
            if confidence < self.config.spike_push_score:
                return f"confidence_below_threshold({confidence}<{self.config.spike_push_score})"
        elif recommendation == "watch":
            if not self.config.signal_watch_enabled:
                return "watch_disabled"
            if confidence < self.config.watch_push_score:
                return f"confidence_below_threshold({confidence}<{self.config.watch_push_score})"
            if final_decision.get("decision_source") != "ai":
                signal_types = {item.get("type", "") for item in signals}
                if not signal_types.intersection(WATCH_TRIGGER_SIGNALS) and final_decision.get("direction") == "观望":
                    return "watch_requires_ai_or_signals"
        elif recommendation == "trade":
            if not self.config.signal_trade_enabled:
                return "trade_disabled"
            if final_decision.get("direction") not in ("做多", "做空"):
                return "trade_requires_direction"
            threshold = self.trade_push_score(final_decision.get("direction", ""))
            if confidence < threshold:
                return f"confidence_below_threshold({confidence}<{threshold})"
        return "gate_blocked"

    def _confirmed_push_eval(
        self,
        snapshot: Dict[str, Any],
        signals: List[Dict[str, Any]],
        final_decision: Dict[str, Any],
        score: Dict[str, Any],
    ) -> Dict[str, Any]:
        recommendation = str(final_decision.get("push_recommendation", "none") or "none")
        base = {
            "track": "confirmed",
            "recommendation": recommendation,
            "direction": final_decision.get("direction"),
            "confidence": final_decision.get("confidence"),
        }
        if not signals:
            return {**base, "kind": "", "status": "skipped", "reason": "no_signals"}
        if recommendation == "none":
            return {**base, "kind": "", "status": "skipped", "reason": "push_recommendation_none"}

        push_kind = self.push_gate(final_decision, signals, score)
        if not push_kind:
            return {
                **base,
                "kind": recommendation,
                "status": "gate_blocked",
                "reason": self._push_gate_block_reason(final_decision, signals, score, recommendation),
            }

        if push_kind == "trade" and self._in_reverse_trade_cooldown(
            snapshot["inst_id"], final_decision.get("direction", "")
        ):
            return {**base, "kind": push_kind, "status": "blocked", "reason": "reverse_trade_cooldown"}

        push_key = self._push_key(snapshot, push_kind, str(final_decision.get("direction", "观望") or "观望"))
        if self._in_push_cooldown(push_key, push_kind):
            return {**base, "kind": push_kind, "status": "blocked", "reason": "cooldown", "push_key": push_key}

        return {
            **base,
            "kind": push_kind,
            "status": "would_push",
            "push_key": push_key,
        }

    def _push_key(self, snapshot: Dict[str, Any], push_kind: str, direction: str) -> str:
        return f"{push_kind}:{snapshot['inst_id']}:{direction or '观望'}"

    def _in_inst_wechat_cooldown(self, inst_id: str) -> bool:
        last_at = self.last_wechat_push_at.get(inst_id, 0.0)
        return self._now_ts() - last_at < DEFAULT_WECHAT_MIN_INTERVAL_SECONDS

    def _mark_wechat_push_sent(
        self,
        inst_id: str,
        push_key: str,
        push_kind: str,
        decision: Dict[str, Any],
    ) -> None:
        now = self._now_ts()
        self.last_push_at[push_key] = now
        self.last_wechat_push_at[inst_id] = now
        if push_kind == "trade" and decision.get("direction") in ("做多", "做空"):
            self.last_trade_push_at[inst_id] = (decision["direction"], now)

    def _wechat_push_block_reason(
        self,
        push_kind: str,
        decision: Dict[str, Any],
        forecast: Dict[str, Any],
        score: Dict[str, Any],
        signals: List[Dict[str, Any]],
        trigger: Dict[str, Any],
        analysis: Optional[Dict[str, Any]],
    ) -> str:
        kind = str(push_kind or "")
        level = str(trigger.get("level", "") or "")
        ai_invoked = bool(trigger.get("ai_invoked"))

        if kind == "watch":
            if str(decision.get("decision_source", "")) != "ai" or not ai_invoked:
                return "watch_wechat_requires_ai"
            if str(decision.get("push_recommendation", "")) != "watch":
                return "watch_not_ai_recommended"
            confidence = int(decision.get("confidence", 0) or 0)
            if confidence < self.config.watch_push_score + WECHAT_WATCH_AI_MIN_MARGIN:
                return f"watch_confidence_below_wechat({confidence})"
            return ""

        if kind == "spike":
            confidence = self._effective_push_confidence(decision, score, "spike")
            post_audit = decision.get("post_audit") if isinstance(decision.get("post_audit"), dict) else {}
            l3_local = post_audit.get("action") == "l3_local_spike"
            if ai_invoked and str(decision.get("push_recommendation", "")) in ("spike", "trade"):
                return ""
            if l3_local and self.config.l3_local_spike_push:
                if confidence >= self.config.spike_push_score + WECHAT_SPIKE_LOCAL_MIN_MARGIN:
                    return ""
                return f"local_spike_below_wechat({confidence})"
            if ai_invoked:
                return ""
            return "spike_requires_ai_or_strong_local"

        if kind == "trade":
            direction = str(decision.get("direction", "观望") or "观望")
            if direction not in ("做多", "做空"):
                return "trade_requires_direction"
            confidence = int(decision.get("confidence", 0) or 0)
            threshold = self.trade_push_score(direction)
            if level in ("L2", "L3"):
                return ""
            if confidence >= threshold + WECHAT_TRADE_MIN_MARGIN:
                return ""
            return f"trade_requires_l2_or_high_confidence({confidence}<{threshold}+{WECHAT_TRADE_MIN_MARGIN})"

        if kind == "forecast":
            probability = int(
                forecast.get("calibrated_probability", forecast.get("probability", 0)) or 0
            )
            threshold = int(
                forecast.get("effective_push_threshold", self.config.forecast_push_score)
                or self.config.forecast_push_score
            )
            if probability < threshold + WECHAT_FORECAST_MIN_MARGIN:
                return f"forecast_probability_below_wechat({probability}<{threshold}+{WECHAT_FORECAST_MIN_MARGIN})"
            if level in ("L2", "L3"):
                return ""
            if probability >= threshold + WECHAT_FORECAST_HIGH_PROB_MARGIN:
                return ""
            return "forecast_requires_l2_or_high_probability"

        return "unsupported_push_kind"

    def _refine_wechat_push_tracks(
        self,
        tracks: List[Dict[str, Any]],
        snapshot: Dict[str, Any],
        signals: List[Dict[str, Any]],
        final_decision: Dict[str, Any],
        score: Dict[str, Any],
        trigger: Dict[str, Any],
        analysis: Optional[Dict[str, Any]],
    ) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
        forecast = (
            score.get("structure_forecast") if isinstance(score.get("structure_forecast"), dict) else {}
        )
        refined: List[Dict[str, Any]] = []
        eligible: List[Tuple[int, str, Dict[str, Any], str]] = []

        for index, track in enumerate(tracks):
            item = dict(track)
            if item.get("status") != "would_push":
                refined.append(item)
                continue

            kind = str(item.get("kind", "") or "")
            decision = (
                self._forecast_push_decision(forecast, final_decision, trigger)
                if kind == "forecast"
                else final_decision
            )
            block = self._wechat_push_block_reason(
                kind, decision, forecast, score, signals, trigger, analysis
            )
            if block:
                item["status"] = "gate_blocked"
                item["reason"] = block
                refined.append(item)
                continue

            direction = str(
                forecast.get("direction", decision.get("direction", "观望"))
                if kind == "forecast"
                else decision.get("direction", "观望")
            )
            push_key = self._push_key(snapshot, kind, direction)
            item["push_key"] = push_key
            refined.append(item)
            eligible.append((len(refined) - 1, kind, decision, push_key))

        if any(kind in ("trade", "spike") for _, kind, _, _ in eligible):
            eligible = [row for row in eligible if row[1] != "forecast"]
            for idx, item in enumerate(refined):
                if item.get("status") == "would_push" and item.get("kind") == "forecast":
                    blocked = dict(item)
                    blocked["status"] = "gate_blocked"
                    blocked["reason"] = "wechat_superseded_by_confirmed"
                    refined[idx] = blocked

        inst_id = snapshot["inst_id"]
        inst_blocked = self._in_inst_wechat_cooldown(inst_id)
        selected: Optional[Dict[str, Any]] = None
        selected_index = -1

        if eligible and not inst_blocked:
            ordered = sorted(
                eligible,
                key=lambda row: WECHAT_PUSH_KIND_PRIORITY.index(row[1])
                if row[1] in WECHAT_PUSH_KIND_PRIORITY
                else 99,
            )
            for ref_index, kind, decision, push_key in ordered:
                if kind == "trade" and self._in_reverse_trade_cooldown(
                    inst_id, decision.get("direction", "")
                ):
                    continue
                if self._in_push_cooldown(push_key, kind):
                    continue
                selected_index = ref_index
                selected = {
                    **refined[ref_index],
                    "decision": decision,
                    "push_key": push_key,
                }
                break

        for idx, item in enumerate(refined):
            if item.get("status") != "would_push":
                continue
            if selected_index >= 0 and idx == selected_index:
                continue
            blocked = dict(item)
            if inst_blocked:
                blocked["status"] = "blocked"
                blocked["reason"] = "wechat_inst_cooldown"
            elif selected_index >= 0:
                blocked["status"] = "gate_blocked"
                blocked["reason"] = "wechat_superseded"
            else:
                blocked["status"] = "blocked"
                blocked["reason"] = "cooldown"
            refined[idx] = blocked

        return refined, selected

    def _execute_wechat_push(
        self,
        snapshot: Dict[str, Any],
        signals: List[Dict[str, Any]],
        final_decision: Dict[str, Any],
        analysis: Optional[Dict[str, Any]],
        score: Dict[str, Any],
        trigger: Dict[str, Any],
        selected: Dict[str, Any],
        *,
        dry_run_label: str = "",
    ) -> None:
        push_kind = str(selected.get("kind", "") or "")
        push_key = str(selected.get("push_key", "") or "")
        decision = selected.get("decision") if isinstance(selected.get("decision"), dict) else final_decision
        inst_id = snapshot["inst_id"]
        if not push_kind or not push_key:
            return

        if not self.push_enabled:
            label = dry_run_label or "dry-run"
            self._log_push_event(snapshot, signals, decision, push_kind, label)
            self._mark_wechat_push_sent(inst_id, push_key, push_kind, decision)
            return

        send_key = os.getenv("WECHAT_SEND_KEY", "").strip()
        if not send_key:
            console_debug(f"[{now_text()}] WeChat push skipped: WECHAT_SEND_KEY is not configured")
            self._log_push_event(snapshot, signals, decision, push_kind, "skipped(no wechat key)")
            self._mark_wechat_push_sent(inst_id, push_key, push_kind, decision)
            return

        self._push_wechat(
            send_key,
            snapshot,
            signals,
            decision,
            analysis or {},
            push_kind,
            local_score=score,
            trigger=trigger or {},
        )
        self._mark_wechat_push_sent(inst_id, push_key, push_kind, decision)

    def dispatch_wechat_push_if_needed(
        self,
        snapshot: Dict[str, Any],
        signals: List[Dict[str, Any]],
        final_decision: Dict[str, Any],
        analysis: Optional[Dict[str, Any]],
        score: Dict[str, Any],
        trigger: Optional[Dict[str, Any]] = None,
        push_analysis: Optional[Dict[str, Any]] = None,
    ) -> None:
        trigger = trigger or {}
        selected: Optional[Dict[str, Any]] = None
        if push_analysis is not None:
            would = [track for track in push_analysis.get("tracks", []) if track.get("status") == "would_push"]
            if len(would) == 1:
                track = would[0]
                forecast = (
                    score.get("structure_forecast")
                    if isinstance(score.get("structure_forecast"), dict)
                    else {}
                )
                decision = (
                    self._forecast_push_decision(forecast, final_decision, trigger)
                    if track.get("kind") == "forecast"
                    else final_decision
                )
                selected = {**track, "decision": decision}
        else:
            tracks = [
                self._confirmed_push_eval(snapshot, signals, final_decision, score),
                self._forecast_push_eval(snapshot, signals, final_decision, score),
            ]
            _, selected = self._refine_wechat_push_tracks(
                tracks, snapshot, signals, final_decision, score, trigger, analysis
            )

        if not selected:
            return

        dry_run_label = "replay-log-only" if self.replay_mode and not self.push_enabled else ""
        self._execute_wechat_push(
            snapshot,
            signals,
            final_decision,
            analysis,
            score,
            trigger,
            selected,
            dry_run_label=dry_run_label,
        )

    def _forecast_push_eval(
        self,
        snapshot: Dict[str, Any],
        signals: List[Dict[str, Any]],
        final_decision: Dict[str, Any],
        score: Dict[str, Any],
    ) -> Dict[str, Any]:
        forecast = (
            score.get("structure_forecast") if isinstance(score.get("structure_forecast"), dict) else {}
        )
        probability = int(forecast.get("calibrated_probability", forecast.get("probability", 0)) or 0)
        threshold = int(
            forecast.get("effective_push_threshold", self.config.forecast_push_score)
            or self.config.forecast_push_score
        )
        base = {
            "track": "forecast",
            "scenario": forecast.get("scenario"),
            "direction": forecast.get("direction"),
            "probability": probability,
            "threshold": threshold,
        }
        if not self.config.signal_forecast_enabled:
            return {**base, "kind": "", "status": "skipped", "reason": "forecast_disabled"}
        if not forecast.get("active"):
            return {**base, "kind": "", "status": "skipped", "reason": "forecast_inactive"}
        if forecast.get("scenario_enabled") is False:
            return {**base, "kind": "", "status": "skipped", "reason": "scenario_disabled_by_calibration"}

        block_reason = self._forecast_push_blocked(forecast, final_decision, signals, score, snapshot)
        push_kind = self.forecast_push_gate(forecast, final_decision, signals, score, snapshot)
        if not push_kind:
            if block_reason:
                return {**base, "kind": "forecast", "status": "gate_blocked", "reason": block_reason}
            if probability < threshold:
                return {
                    **base,
                    "kind": "forecast",
                    "status": "gate_blocked",
                    "reason": f"probability_below_threshold({probability}<{threshold})",
                }
            return {**base, "kind": "forecast", "status": "gate_blocked", "reason": "gate_blocked"}

        direction = str(forecast.get("direction", "观望") or "观望")
        push_key = self._push_key(snapshot, push_kind, direction)
        if self._in_push_cooldown(push_key, push_kind):
            return {**base, "kind": push_kind, "status": "blocked", "reason": "cooldown", "push_key": push_key}

        return {
            **base,
            "kind": push_kind,
            "status": "would_push",
            "push_key": push_key,
        }

    def _build_ai_analysis_summary(
        self,
        analysis: Optional[Dict[str, Any]],
        trigger: Dict[str, Any],
        final_decision: Dict[str, Any],
    ) -> Dict[str, Any]:
        summary = {
            "enabled": self.ai_enabled,
            "dry_run": self.dry_run_ai,
            "should_call": bool(trigger.get("should_call_ai")),
            "invoked": bool(trigger.get("ai_invoked")),
            "level": trigger.get("level"),
            "reasons": trigger.get("reasons"),
        }
        if not analysis:
            return summary
        parsed = analysis.get("parsed") if isinstance(analysis.get("parsed"), dict) else {}
        summary.update(
            {
                "provider": analysis.get("provider"),
                "valid_json": analysis.get("valid_json"),
                "direction": parsed.get("direction") or final_decision.get("direction"),
                "confidence": parsed.get("confidence") or final_decision.get("confidence"),
                "push_recommendation": parsed.get("push_recommendation"),
                "usage": analysis.get("usage"),
                "error": analysis.get("error") or analysis.get("ai_status"),
            }
        )
        return summary

    def _build_push_analysis(
        self,
        snapshot: Dict[str, Any],
        signals: List[Dict[str, Any]],
        final_decision: Dict[str, Any],
        score: Dict[str, Any],
        analysis: Optional[Dict[str, Any]],
        trigger: Dict[str, Any],
    ) -> Dict[str, Any]:
        tracks = [
            self._confirmed_push_eval(snapshot, signals, final_decision, score),
            self._forecast_push_eval(snapshot, signals, final_decision, score),
        ]
        tracks, selected = self._refine_wechat_push_tracks(
            tracks, snapshot, signals, final_decision, score, trigger, analysis
        )
        would_push = selected is not None
        wechat_mode = "enabled" if self.push_enabled else "log_only"
        for item in tracks:
            if item.get("status") == "would_push":
                item["wechat"] = wechat_mode
        return {
            "would_push": would_push,
            "push_enabled": self.push_enabled,
            "wechat_sent": would_push and self.push_enabled,
            "tracks": tracks,
            "ai": self._build_ai_analysis_summary(analysis, trigger, final_decision),
        }

    def _forecast_push_decision(
        self,
        forecast: Dict[str, Any],
        final_decision: Dict[str, Any],
        trigger: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        forecast_decision = dict(final_decision)
        forecast_decision.update(
            {
                "direction": forecast.get("direction", "观望"),
                "confidence": int(forecast.get("calibrated_probability", forecast.get("probability", 0)) or 0),
                "push_recommendation": "forecast",
                "summary": forecast.get("summary", ""),
                "decision_source": "structure_forecast",
                "trigger_level": trigger.get("level", "-") if trigger else "-",
            }
        )
        return forecast_decision

    def _apply_replay_push_side_effects(
        self,
        snapshot: Dict[str, Any],
        signals: List[Dict[str, Any]],
        final_decision: Dict[str, Any],
        score: Dict[str, Any],
        push_analysis: Dict[str, Any],
        trigger: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.dispatch_wechat_push_if_needed(
            snapshot,
            signals,
            final_decision,
            None,
            score,
            trigger,
            push_analysis=push_analysis,
        )

    def _log_replay_push_summary(
        self,
        snapshot: Dict[str, Any],
        final_decision: Dict[str, Any],
        push_analysis: Dict[str, Any],
    ) -> None:
        would_push = bool(push_analysis.get("would_push"))
        ai = push_analysis.get("ai") if isinstance(push_analysis.get("ai"), dict) else {}
        if not would_push and not ai.get("invoked") and not ai.get("should_call"):
            return
        track_bits = []
        for track in push_analysis.get("tracks", []):
            if not isinstance(track, dict):
                continue
            status = str(track.get("status", "") or "")
            if status == "skipped":
                continue
            kind = str(track.get("kind", "") or "-")
            reason = str(track.get("reason", "") or "")
            bit = f"{kind}:{status}"
            if reason and status != "would_push":
                bit += f"({reason})"
            track_bits.append(bit)
        console_info(
            f"[{snapshot['time']}] replay push_analysis would_push={would_push} "
            f"dir={final_decision.get('direction', '-')} conf={final_decision.get('confidence', '-')} "
            f"tracks=[{', '.join(track_bits) or '-'}] "
            f"ai_invoked={ai.get('invoked')} ai_level={ai.get('level', '-')}"
        )

    def forecast_push_if_needed(
        self,
        snapshot: Dict[str, Any],
        signals: List[Dict[str, Any]],
        final_decision: Dict[str, Any],
        analysis: Optional[Dict[str, Any]],
        local_score: Dict[str, Any],
        trigger: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.dispatch_wechat_push_if_needed(
            snapshot, signals, final_decision, analysis, local_score, trigger
        )

    def analyze_with_ai(
        self,
        snapshot: Dict[str, Any],
        signals: List[Dict[str, Any]],
        score: Dict[str, Any],
        trigger: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        # 仅在 evaluate_ai_trigger 判定 should_call_ai 后调用。
        if self.dry_run_ai:
            payload = self._ai_payload(snapshot, signals, score, trigger)
            return {
                "provider": "dry-run",
                "content": "AI dry-run enabled. Payload prepared but not sent.",
                "payload": payload,
            }

        inst_id = str(snapshot.get("inst_id", "") or "")
        fingerprint = ""
        if isinstance(trigger, dict):
            fingerprint = str(trigger.get("fingerprint", "") or "")
        if not fingerprint:
            fingerprint = self._signal_fingerprint(signals, score)
        cache_key = f"{inst_id}:{fingerprint}"
        if self.replay_mode and self.config.replay_ai_cache_enabled:
            cached = self.replay_ai_cache.get(cache_key)
            if isinstance(cached, dict):
                return deep_copy_json(cached)

        try:
            from openai import OpenAI  # noqa: F401
        except ImportError:
            self._mark_ai_abnormal("package_missing", "openai package is not installed")
            return {
                "provider": "local",
                "content": "openai package is not installed; fallback to local analysis.",
                "fallback": self._local_analysis(snapshot, signals, score),
            }

        api_key, base_url, model = self._ai_env_config()
        if not api_key:
            self._mark_ai_abnormal("config_missing", "AI_API_KEY or OPENAI_API_KEY is not configured")
            return {
                "provider": "local",
                "content": "AI_API_KEY or OPENAI_API_KEY is not configured; fallback to local analysis.",
                "fallback": self._local_analysis(snapshot, signals, score),
            }

        circuit_state = self._ai_circuit_state()
        if circuit_state == "open":
            self._maybe_probe_ai_connection(model)
            if self._ai_circuit_state() != "closed":
                self._mark_ai_abnormal(
                    "circuit_open",
                    self.ai_last_failure_reason or "AI circuit open; using local analysis until probe succeeds.",
                )
                return self._ai_fallback_result(
                    snapshot,
                    signals,
                    score,
                    "AI circuit open; using local analysis until probe succeeds.",
                    "circuit_open",
                )

        prompt = self._ai_prompt(snapshot, signals, score, trigger)
        try:
            client = self._get_ai_client(api_key, base_url)
            response = self._chat_completion_with_retry(client, model, prompt)
            output_text = response.choices[0].message.content
            usage = self._extract_ai_usage(response)
            self._log_ai_token_usage(snapshot["inst_id"], model, usage)
            self._record_ai_success()
            result = self._build_ai_success_result(
                base_url,
                model,
                output_text,
                snapshot,
                signals,
                score,
                usage=usage,
            )
            if self.replay_mode and self.config.replay_ai_cache_enabled and result.get("valid_json"):
                self.replay_ai_cache[cache_key] = deep_copy_json(result)
            return result
        except Exception as exc:
            console_warn(f"[{now_text()}] AI request failed: {exc}")
            self._record_ai_failure(exc)
            ai_status = self._ai_circuit_state()
            return self._ai_fallback_result(
                snapshot,
                signals,
                score,
                f"AI request failed: {exc}; fallback to local analysis.",
                ai_status,
                exc,
            )

    def push_if_needed(
        self,
        snapshot: Dict[str, Any],
        signals: List[Dict[str, Any]],
        final_decision: Dict[str, Any],
        analysis: Optional[Dict[str, Any]],
        local_score: Dict[str, Any],
        trigger: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.dispatch_wechat_push_if_needed(
            snapshot, signals, final_decision, analysis, local_score, trigger
        )

    def log_result(
        self,
        snapshot: Dict[str, Any],
        signals: List[Dict[str, Any]],
        score: Dict[str, Any],
        analysis: Optional[Dict[str, Any]],
        trigger: Dict[str, Any],
        final_decision: Dict[str, Any],
        push_analysis: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not self.replay_mode and not self.runtime_config.analysis_log_enabled:
            return
        # 每一轮都会写日志，即使不触发信号也记录。
        record = {
            "time": snapshot["time"],
            "inst_id": snapshot["inst_id"],
            "price": snapshot["price"],
            "chart": {
                "bar": "1m",
                "points": compact_candles(snapshot["candles"].get("1m", []), 21),
            },
            "open_interest": snapshot["open_interest"],
            "volume": snapshot["volume"],
            "oi_change_pct_15m": snapshot["oi_change_pct_15m"],
            "oi_change_pct_strategy": snapshot.get("oi_change_pct_strategy"),
            "derivative_window_minutes": snapshot.get("derivative_window_minutes", 15),
            "oi_warmup_ready": snapshot["oi_warmup_ready"],
            "oi_strategy_warmup_ready": snapshot.get("oi_strategy_warmup_ready"),
            "funding_rate": snapshot["funding_rate"],
            "funding_change": snapshot["funding_change"],
            "funding_change_strategy": snapshot.get("funding_change_strategy"),
            "funding_warmup_ready": snapshot["funding_warmup_ready"],
            "funding_strategy_warmup_ready": snapshot.get("funding_strategy_warmup_ready"),
            "long_short_ratio": snapshot["long_short_ratio"],
            "order_book": snapshot.get("order_book", {}),
            "trend_profiles": snapshot.get("trend_profiles", {}),
            "trend_profiles_live": snapshot.get("trend_profiles_live", {}),
            "volatility": snapshot.get("volatility", {}),
            "dynamic_thresholds": snapshot.get("dynamic_thresholds", {}),
            "instrument_profile": snapshot.get("instrument_profile", {}),
            "market_context": snapshot.get("market_context", {}),
            "snapshot_quality": snapshot.get("snapshot_quality", {}),
            "data_sources": snapshot.get("data_sources", {}),
            "signal_tracking": snapshot.get("signal_tracking", {}),
            "paper_account": snapshot.get("paper_account", {}),
            "signals": signals,
            "score": score,
            "local_trigger": trigger,
            "analysis": analysis,
            "final_decision": final_decision,
            "calibration_summary": self._calibration_summary(snapshot.get("inst_id", "")),
            "config_snapshot": build_log_config_snapshot(
                config=self.config,
                push_score=self.push_score,
                short_push_score=self.short_push_score,
                ai_enabled=self.ai_enabled,
                push_enabled=self.push_enabled,
            ),
        }
        if push_analysis is not None:
            record["push_analysis"] = push_analysis
        self._rotate_log_if_needed()
        log_path = self.replay_log_file if self.replay_mode else LOG_FILE
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")
            if self.replay_mode:
                file.flush()

    def _process_inst(self, inst_id: str) -> None:
        snapshot = self.collect_snapshot(inst_id)
        signals = self.detect_signals(snapshot)
        score = self.score_snapshot(snapshot, signals)
        trigger = self.evaluate_ai_trigger(inst_id, signals, score, snapshot)

        analysis: Optional[Dict[str, Any]] = None
        if trigger["should_call_ai"]:
            analysis = self.analyze_with_ai(snapshot, signals, score, trigger)
            trigger["ai_invoked"] = True
            self.last_ai_call_at[inst_id] = self._now_ts()
            self.last_ai_fingerprint[inst_id] = trigger["fingerprint"]

        final_decision = self.merge_final_decision(analysis, score, signals, trigger, snapshot)
        final_decision = self._apply_decision_post_audit(final_decision, score, signals, trigger, snapshot)
        snapshot["forecast_tracking"] = self.update_forecast_tracking(snapshot, score)
        snapshot["decision_calibration_tracking"] = self.update_decision_calibration_tracking(
            snapshot, final_decision, score
        )
        snapshot["signal_tracking"] = self.update_signal_tracking(snapshot, signals, final_decision)
        snapshot["paper_account"] = self.update_paper_account(
            inst_id,
            to_float(snapshot.get("price")),
            self._paper_direction_from_final_decision(final_decision),
        )
        self._log_console_summary(snapshot, signals, final_decision, analysis, trigger, score)
        push_analysis: Optional[Dict[str, Any]] = None
        if self.replay_mode:
            push_analysis = self._build_push_analysis(
                snapshot, signals, final_decision, score, analysis, trigger
            )
            if self.push_enabled:
                self.dispatch_wechat_push_if_needed(
                    snapshot,
                    signals,
                    final_decision,
                    analysis,
                    score,
                    trigger,
                    push_analysis=push_analysis,
                )
            else:
                self._apply_replay_push_side_effects(
                    snapshot, signals, final_decision, score, push_analysis, trigger
                )
            self._log_replay_push_summary(snapshot, final_decision, push_analysis)
        else:
            self.dispatch_wechat_push_if_needed(
                snapshot, signals, final_decision, analysis, score, trigger
            )
        self.log_result(snapshot, signals, score, analysis, trigger, final_decision, push_analysis)

    def run_replay(self, frames: List[Dict[str, Any]], replay_interval: float = 0.0) -> None:
        self.replay_mode = True
        # 回放必须只使用帧内历史逐步预热，不能混入实时日志恢复的数据。
        self._clear_market_histories()
        self._market_history_restored = False
        if self.replay_log_file == LOG_FILE:
            self.replay_log_file = REPLAY_LOG_FILE
        self.replay_log_file.parent.mkdir(parents=True, exist_ok=True)
        self.reset_paper_accounts(session_label="replay")
        self._check_ai_startup_config()
        self.log_effective_config(mode="replay")
        total = len(frames)
        ai_note = "ai=on" if self.ai_enabled else "ai=off"
        if self.ai_enabled and self.dry_run_ai:
            ai_note = "ai=dry-run"
        push_note = "push=on" if self.push_enabled else "push=log-only"
        console_info(
            f"[{now_text()}] replay start: {total} frames -> {self.replay_log_file} "
            f"{ai_note} {push_note}"
        )
        for index, frame in enumerate(frames, start=1):
            inst_id = str(frame.get("inst_id", ""))
            if inst_id not in self.instruments:
                console_debug(f"[{now_text()}] replay skip unknown inst_id={inst_id}")
                continue
            self.replay_frame = frame
            self._set_replay_clock(str(frame.get("time", "")))
            try:
                self._process_inst(inst_id)
            except Exception as exc:
                console_warn(f"[{self._now_text()}] replay frame {index}/{total} failed: {exc}")
            finally:
                self.replay_frame = None
            if replay_interval > 0 and index < total:
                time.sleep(replay_interval)
        console_info(f"[{now_text()}] replay finished: {total} frames")

    def _prune_runtime_caches(self) -> None:
        now = time.time()
        push_keep = max(3600.0, float(self.runtime_config.push_cooldown_seconds) * 2)
        for key, last_at in list(self.last_push_at.items()):
            if now - last_at > push_keep:
                self.last_push_at.pop(key, None)
        reverse_keep = max(3600.0, float(self.runtime_config.reverse_trade_cooldown_seconds) * 2)
        for inst_id, (_, last_at) in list(self.last_trade_push_at.items()):
            if now - last_at > reverse_keep:
                self.last_trade_push_at.pop(inst_id, None)
        for key, last_at in list(self.last_signal_track_at.items()):
            if now - last_at > 7200:
                self.last_signal_track_at.pop(key, None)
        for key, last_at in list(self.last_forecast_track_at.items()):
            if now - last_at > 7200:
                self.last_forecast_track_at.pop(key, None)
        if len(self.pending_forecast_reviews) > 400:
            self.pending_forecast_reviews = self.pending_forecast_reviews[-400:]
        if len(self.pending_decision_reviews) > 400:
            self.pending_decision_reviews = self.pending_decision_reviews[-400:]
        if len(self.signal_performance) > 500:
            overflow = len(self.signal_performance) - 500
            for key in list(self.signal_performance.keys())[:overflow]:
                self.signal_performance.pop(key, None)
        cache_keep = max(CACHE_TTL_SECONDS.values()) * 4
        for key, (saved_at, _) in list(self.cache.items()):
            if now - saved_at > cache_keep:
                self.cache.pop(key, None)
        if len(self.pending_signal_reviews) > 300:
            self.pending_signal_reviews = self.pending_signal_reviews[-300:]

    def run_once(self) -> None:
        # 定时执行：遍历所有支持币种，完成数据采集、阈值检测、综合评分、AI分析、微信推送、日志存储等全部功能。
        for inst_id in self.instruments:
            try:
                self._process_inst(inst_id)
            except Exception as exc:
                console_warn(f"[{self._now_text()}] {inst_id} collect/analyze failed: {exc}")
        if not self.replay_mode:
            self._maybe_push_ai_abnormal_alert()
            self._maybe_save_calibration_state()
        now = time.time()
        if now - self._last_runtime_cache_prune_at >= 300:
            self._prune_runtime_caches()
            self._last_runtime_cache_prune_at = now

    def run_forever(self, runtime: int) -> None:
        # 主循环：默认永久运行；runtime>0时，到指定秒数自动退出，用于实现定时任务。
        self._restore_market_histories_from_log()
        self.reset_paper_accounts(session_label="live")
        reset_ai_token_stats(started_at=now_text())
        self._check_ai_startup_config()
        self.log_effective_config(mode="live")
        log_note = f"json_log={LOG_FILE}" if self.runtime_config.analysis_log_enabled else "json_log=off"
        console_info(
            f"[{now_text()}] monitor start: {', '.join(self.instruments)} "
            f"interval={self.interval}s {log_note}"
        )
        started = time.time()
        while True:
            self.run_once()
            if runtime > 0 and time.time() - started >= runtime:
                console_info(f"[{now_text()}] runtime {runtime}s reached; exit.")
                break
            time.sleep(self.interval)

    def _paper_fee_rate(self) -> float:
        return max(0.0, float(self.config.paper_fee_bps)) / 10000.0

    def _apply_paper_fee(self, amount: float) -> float:
        if amount <= 0:
            return amount
        return amount * (1.0 - self._paper_fee_rate())

    def _paper_direction_from_final_decision(self, final_decision: Dict[str, Any]) -> str:
        if not self.config.paper_follow_ai_only:
            return str(final_decision.get("direction", "观望") or "观望")
        source = str(final_decision.get("decision_source", "") or "")
        if source != "ai":
            return "观望"
        forward = final_decision.get("forward_view") if isinstance(final_decision.get("forward_view"), dict) else {}
        direction = forward.get("direction") or final_decision.get("direction", "观望")
        return str(direction or "观望")

    def log_effective_config(self, mode: str = "live") -> None:
        console_info(f"[{now_text()}] runtime {format_runtime_identity(__file__)}")
        for line in build_effective_config_lines(
            mode=mode,
            instruments=self.instruments,
            interval=self.interval,
            ai_enabled=self.ai_enabled,
            push_enabled=self.push_enabled,
            dry_run_ai=self.dry_run_ai,
            config=self.config,
            push_score=self.push_score,
            short_push_score=self.short_push_score,
        ):
            console_info(f"[{now_text()}] config {line}")

    def _new_paper_state(self) -> Dict[str, Any]:
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

    def reset_paper_accounts(self, session_label: str = "live") -> None:
        self.paper_session_started_at = self._now_text()
        self.paper_accounts = {inst_id: self._new_paper_state() for inst_id in self.instruments}
        self._persist_paper_accounts(session_label=session_label)

    def _paper_position_from_direction(self, direction: str) -> str:
        if direction == "做多":
            return "long"
        if direction == "做空":
            return "short"
        return "flat"

    def _paper_position_label(self, position: str) -> str:
        return {"long": "做多", "short": "做空", "flat": "空仓"}.get(position, "空仓")

    def _mark_paper_equity(self, state: Dict[str, Any], price: float) -> None:
        position = state.get("position", "flat")
        entry_price = to_float(state.get("entry_price"), 0.0)
        basis_equity = to_float(state.get("basis_equity"), 0.0)
        if position == "long" and entry_price > 0:
            equity = basis_equity * (price / entry_price)
        elif position == "short" and entry_price > 0:
            equity = basis_equity * (1 + (entry_price - price) / entry_price)
        else:
            equity = to_float(state.get("cash"), PAPER_INITIAL_CAPITAL)
        initial = to_float(state.get("initial_capital"), PAPER_INITIAL_CAPITAL)
        state["equity"] = equity
        state["pnl_usd"] = equity - initial
        state["pnl_pct"] = (state["pnl_usd"] / initial * 100) if initial else 0.0

    def _close_paper_position(self, state: Dict[str, Any], price: float) -> None:
        position = state.get("position", "flat")
        entry_price = to_float(state.get("entry_price"), 0.0)
        basis_equity = to_float(state.get("basis_equity"), 0.0)
        if position == "long" and entry_price > 0:
            state["cash"] = self._apply_paper_fee(basis_equity * (price / entry_price))
        elif position == "short" and entry_price > 0:
            state["cash"] = self._apply_paper_fee(basis_equity * (1 + (entry_price - price) / entry_price))
        state["position"] = "flat"
        state["position_label"] = "空仓"
        state["entry_price"] = 0.0
        state["basis_equity"] = 0.0

    def _open_paper_position(self, state: Dict[str, Any], position: str, price: float, direction: str) -> None:
        state["position"] = position
        state["position_label"] = self._paper_position_label(position)
        state["entry_price"] = price
        state["basis_equity"] = self._apply_paper_fee(to_float(state.get("cash"), PAPER_INITIAL_CAPITAL))
        state["cash"] = 0.0
        state["direction"] = direction
        state["trade_count"] = int(state.get("trade_count", 0) or 0) + 1

    def update_paper_account(self, inst_id: str, price: float, direction: str) -> Dict[str, Any]:
        """按 AI 前瞻方向（可配置）满仓跟单；方向变化时换仓，观望时空仓；1x、含简易手续费。"""
        if inst_id not in self.instruments:
            return {}
        if price <= 0:
            return dict(self.paper_accounts.get(inst_id, {}))

        state = self.paper_accounts.setdefault(inst_id, self._new_paper_state())
        target = self._paper_position_from_direction(direction)
        current = state.get("position", "flat")
        if target != current:
            if current != "flat":
                self._close_paper_position(state, price)
            if target != "flat":
                self._open_paper_position(state, target, price, direction)
            else:
                state["direction"] = "观望"
                state["position_label"] = "空仓"
        else:
            state["direction"] = direction
            if target != "flat":
                state["position_label"] = self._paper_position_label(target)
        self._mark_paper_equity(state, price)
        self._persist_paper_accounts()
        return dict(state)

    def _persist_paper_accounts(self, session_label: str = "live") -> None:
        PAPER_ACCOUNT_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "session_label": session_label,
            "session_started_at": self.paper_session_started_at or self._now_text(),
            "initial_capital": PAPER_INITIAL_CAPITAL,
            "note": "方向跟单模拟，非真实成交",
            "accounts": self.paper_accounts,
        }
        with PAPER_ACCOUNT_FILE.open("w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False)

    def update_signal_tracking(
        self,
        snapshot: Dict[str, Any],
        signals: List[Dict[str, Any]],
        final_decision: Dict[str, Any],
    ) -> Dict[str, Any]:
        # 在线回测/校准的最小闭环：
        # 1. 当系统给出做多/做空且分数较高时，先登记“等待入场区触达”的样本；
        # 2. 价格触达entry_plan.entry后才进入active_review，避免把“等待回踩”误当成立刻入场；
        # 3. 后续每轮用当前价格更新最大顺向波动(MFE)和最大逆向波动(MAE)；
        # 4. 到5m、15m、1H时结算样本，并聚合到策略模板维度；
        # 4. 日志里保存结算结果，后续可以统计哪些指标组合真的有效。
        now_ts = self._now_ts()
        price = to_float(snapshot.get("price"))
        inst_id = snapshot.get("inst_id", "")
        closed = []

        still_pending = []
        for item in self.pending_signal_reviews:
            if item.get("inst_id") != inst_id:
                still_pending.append(item)
                continue
            if item.get("state") == "pending_entry":
                low, high = item.get("entry_low", 0.0), item.get("entry_high", 0.0)
                touched, touch_source = self._entry_touched(snapshot, low, high, price)
                if touched:
                    item["state"] = "active_review"
                    fill_price, fill_assumption = self._assumed_fill_price(item.get("direction", ""), low, high, price, touch_source)
                    item["entry_price"] = fill_price
                    item["fill_assumption"] = fill_assumption
                    item["entry_time"] = snapshot.get("time")
                    item["entry_ts"] = now_ts
                    item["touch_source"] = touch_source
                    item["max_favorable_pct"] = 0.0
                    item["max_adverse_pct"] = 0.0
                    still_pending.append(item)
                    continue
                if now_ts - item.get("created_ts", now_ts) >= item.get("entry_expire_seconds", 900):
                    item["state"] = "expired_unfilled"
                    item["close_price"] = price
                    item["close_time"] = snapshot.get("time")
                    item["return_pct"] = 0.0
                    item["hit"] = False
                    closed.append(item)
                    self._record_signal_performance(item)
                    self._append_signal_performance(item)
                    continue
                still_pending.append(item)
                continue

            direction_mult = 1 if item.get("direction") == "做多" else -1
            entry_price = item.get("entry_price") or price
            move_pct = pct_change(price, entry_price) * direction_mult
            item["max_favorable_pct"] = max(item.get("max_favorable_pct", move_pct), move_pct)
            item["max_adverse_pct"] = min(item.get("max_adverse_pct", move_pct), move_pct)
            if now_ts - item.get("entry_ts", item.get("created_ts", now_ts)) >= item.get("horizon_seconds", 0):
                item["state"] = "closed"
                item["close_price"] = price
                item["close_time"] = snapshot.get("time")
                item["return_pct"] = move_pct
                item["hit"] = move_pct > 0
                closed.append(item)
                self._record_signal_performance(item)
                self._append_signal_performance(item)
            else:
                still_pending.append(item)
        self.pending_signal_reviews = still_pending

        opened = []
        direction = final_decision.get("direction")
        confidence = final_decision.get("confidence", 0)
        push_recommendation = final_decision.get("push_recommendation", "none")
        if (
            direction in ("做多", "做空")
            and price > 0
            and push_recommendation in ("trade", "spike")
            and confidence >= max(70, self.trade_push_score(direction) - 10)
        ):
            signal_types = ",".join(sorted(item.get("type", "") for item in signals if item.get("type"))) or "score-only"
            strategy = snapshot.get("market_context", {}).get("strategy_template", "unknown")
            track_key = f"{inst_id}:{direction}:{strategy}:{signal_types}"
            if now_ts - self.last_signal_track_at.get(track_key, 0.0) >= 60:
                self.last_signal_track_at[track_key] = now_ts
                entry_low, entry_high = self._entry_bounds(final_decision.get("entry", ""))
                if not entry_low or not entry_high:
                    entry_low = entry_high = price
                for label, seconds in (("5m", 300), ("15m", 900), ("1H", 3600)):
                    opened.append({
                        "id": f"{int(now_ts)}:{inst_id}:{direction}:{label}",
                        "inst_id": inst_id,
                        "direction": direction,
                        "state": "pending_entry",
                        "horizon": label,
                        "horizon_seconds": seconds,
                        "entry_expire_seconds": 900,
                        "created_ts": now_ts,
                        "open_time": snapshot.get("time"),
                        "entry_price": None,
                        "entry_low": entry_low,
                        "entry_high": entry_high,
                        "planned_entry": final_decision.get("entry"),
                        "score": confidence,
                        "market_regime": final_decision.get("market_regime"),
                        "strategy_template": strategy,
                        "signal_types": signal_types,
                        "decision_source": final_decision.get("decision_source"),
                        "max_favorable_pct": 0.0,
                        "max_adverse_pct": 0.0,
                    })
                self.pending_signal_reviews.extend(opened)

        return {
            "opened": opened,
            "closed": closed,
            "pending_count": len(self.pending_signal_reviews),
            "performance_summary": self._performance_summary(inst_id),
        }

    def _entry_bounds(self, entry_text: str) -> Tuple[float, float]:
        # 从 "123.45 - 124.56" 形式的入场区间解析上下沿。解析失败时返回0，由调用方兜底。
        if not entry_text or entry_text == "-":
            return 0.0, 0.0
        parts = [part.strip() for part in str(entry_text).split("-")]
        if len(parts) < 2:
            value = to_float(parts[0])
            return value, value
        low = to_float(parts[0])
        high = to_float(parts[1])
        if low > high:
            low, high = high, low
        return low, high

    def _entry_touched(
        self,
        snapshot: Dict[str, Any],
        entry_low: float,
        entry_high: float,
        current_price: float,
    ) -> Tuple[bool, str]:
        # 入场触达优先使用最新1m K线high/low，而不是只看轮询瞬间价格。
        # 这样两次轮询之间价格短暂扫到入场区，也能被统计到。
        if not entry_low or not entry_high:
            return False, "no_entry_zone"
        candle_low, candle_high = self._latest_1m_range(snapshot)
        if candle_low and candle_high and candle_high >= entry_low and candle_low <= entry_high:
            return True, "1m_high_low"
        if entry_low <= current_price <= entry_high:
            return True, "current_price"
        return False, "not_touched"

    def _latest_1m_range(self, snapshot: Dict[str, Any]) -> Tuple[float, float]:
        rows = snapshot.get("candles", {}).get("1m", [])
        if not rows:
            return 0.0, 0.0
        # 触达判断可以使用最新K线，包括未收盘K线，因为它的high/low正好记录了轮询间隔内的扫价范围。
        latest = rows[0]
        return to_float(latest.get("low")), to_float(latest.get("high"))

    def _assumed_fill_price(
        self,
        direction: str,
        entry_low: float,
        entry_high: float,
        current_price: float,
        touch_source: str,
    ) -> Tuple[float, str]:
        # 回测成交价只是估算，不是真实订单成交价。
        # 用保守边界估算：做多按区间上沿成交，做空按区间下沿成交；当前价触达时用夹逼价。
        if not entry_low or not entry_high:
            return current_price, "current_price_no_zone"
        if touch_source == "current_price":
            return max(entry_low, min(current_price, entry_high)), "current_price_clamped"
        if direction == "做多":
            return entry_high, "conservative_long_entry_high"
        if direction == "做空":
            return entry_low, "conservative_short_entry_low"
        return (entry_low + entry_high) / 2, "midpoint_unknown_direction"

    def _record_signal_performance(self, item: Dict[str, Any]) -> None:
        # 按币种/方向/策略/周期聚合表现。后续调参时优先看这些聚合值，而不是凭单条样本感觉。
        key = f"{item.get('inst_id')}:{item.get('direction')}:{item.get('strategy_template')}:{item.get('horizon')}"
        stats = self.signal_performance.setdefault(key, {
            "count": 0,
            "filled_count": 0,
            "expired_unfilled_count": 0,
            "hit_count": 0,
            "sum_return_pct": 0.0,
            "sum_mfe_pct": 0.0,
            "sum_mae_pct": 0.0,
            "best_return_pct": None,
            "worst_return_pct": None,
        })
        stats["count"] += 1
        if item.get("state") == "expired_unfilled":
            stats["expired_unfilled_count"] += 1
            return
        ret = to_float(item.get("return_pct"))
        stats["filled_count"] += 1
        stats["hit_count"] += 1 if ret > 0 else 0
        stats["sum_return_pct"] += ret
        stats["sum_mfe_pct"] += to_float(item.get("max_favorable_pct"))
        stats["sum_mae_pct"] += to_float(item.get("max_adverse_pct"))
        stats["best_return_pct"] = ret if stats["best_return_pct"] is None else max(stats["best_return_pct"], ret)
        stats["worst_return_pct"] = ret if stats["worst_return_pct"] is None else min(stats["worst_return_pct"], ret)

    def _load_signal_performance(self) -> None:
        # 程序重启后从JSONL恢复聚合统计，让performance_summary跨运行保留。
        # 只恢复已经结算或过期的样本；pending_entry/active_review是运行态，不跨进程恢复，避免过期时间混乱。
        if not SIGNAL_PERFORMANCE_FILE.exists():
            return
        try:
            text = tail_file_text(SIGNAL_PERFORMANCE_FILE, SIGNAL_PERFORMANCE_LOAD_BYTES)
            for line in text.splitlines():
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if item.get("state") in ("closed", "expired_unfilled"):
                    self._record_signal_performance(item)
        except Exception as exc:
            console_debug(f"load signal performance failed: {exc}")

    def _append_signal_performance(self, item: Dict[str, Any]) -> None:
        # 单独持久化结算样本，避免程序重启后只剩内存统计。
        # 这份JSONL可以后续离线聚合、调权重、对比不同信号组合。
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            self._rotate_signal_performance_if_needed()
            with SIGNAL_PERFORMANCE_FILE.open("a", encoding="utf-8") as file:
                file.write(json.dumps(item, ensure_ascii=False) + "\n")
        except Exception as exc:
            console_debug(f"signal performance log failed: {exc}")

    def _rotate_signal_performance_if_needed(self) -> None:
        if not SIGNAL_PERFORMANCE_FILE.exists() or SIGNAL_PERFORMANCE_FILE.stat().st_size < SIGNAL_PERFORMANCE_MAX_BYTES:
            return
        backup = SIGNAL_PERFORMANCE_FILE.with_suffix(SIGNAL_PERFORMANCE_FILE.suffix + ".1")
        if backup.exists():
            backup.unlink()
        SIGNAL_PERFORMANCE_FILE.replace(backup)

    def _performance_summary(self, inst_id: str) -> Dict[str, Any]:
        # 返回当前币种的聚合统计摘要。样本少时只作为调试信息，样本积累后才有校准价值。
        summary = {}
        for key, stats in self.signal_performance.items():
            if not key.startswith(f"{inst_id}:"):
                continue
            count = stats.get("count", 0)
            filled_count = stats.get("filled_count", 0)
            summary[key] = {
                "count": count,
                "filled_count": filled_count,
                "expired_unfilled_count": stats.get("expired_unfilled_count", 0),
                "fill_rate": safe_div(filled_count, count),
                "hit_rate": safe_div(stats.get("hit_count", 0), filled_count),
                "avg_return_pct": safe_div(stats.get("sum_return_pct", 0.0), filled_count),
                "avg_mfe_pct": safe_div(stats.get("sum_mfe_pct", 0.0), filled_count),
                "avg_mae_pct": safe_div(stats.get("sum_mae_pct", 0.0), filled_count),
                "best_return_pct": stats.get("best_return_pct"),
                "worst_return_pct": stats.get("worst_return_pct"),
            }
        return summary

    def _cached(self, key: str, ttl_seconds: int, loader: Any) -> Any:
        # 通用缓存：未过期直接返回缓存值，过期后重新加载。
        with self.cache_lock:
            cached = self.cache.get(key)
        if cached and time.time() - cached[0] < ttl_seconds:
            return cached[1]
        value = loader()
        with self.cache_lock:
            self.cache[key] = (time.time(), value)
        return value

    def _okx_call(self, label: str, func: Any) -> Any:
        # OKX HTTP调用统一套重试，降低临时网络故障影响。
        return okx_retry_call(
            label,
            func,
            self.runtime_config.retry_times,
            self.runtime_config.retry_backoff,
        )

    def _sdk_or_rest(self, label: str, sdk_func: Any, rest_func: Any) -> Any:
        # 优先走官方SDK；如果SDK不可用或调用失败，自动降级到REST兜底。
        if sdk_func:
            try:
                return self._okx_call(f"{label}-sdk", sdk_func)
            except Exception as exc:
                console_debug(f"[{now_text()}] {label} SDK failed, fallback to REST: {exc}")
        return self._okx_call(f"{label}-rest", rest_func)

    def _get_ticker(self, inst_id: str) -> Dict[str, float]:
        frame = self._replay_source(inst_id)
        if frame:
            ticker = frame.get("ticker") if isinstance(frame.get("ticker"), dict) else {}
            return {
                "last": to_float(ticker.get("last")),
                "bid_px": to_float(ticker.get("bid_px")),
                "ask_px": to_float(ticker.get("ask_px")),
            }
        # 为避免API访问频繁，用缓存记录，在配置的有效时间段内返回缓存数据，否则才去API重新获取。
        response = self._cached(
            f"ticker:{inst_id}",
            CACHE_TTL_SECONDS["ticker"],
            lambda: self._sdk_or_rest(
                "ticker",
                (lambda: self.market_api.get_ticker(instId=inst_id)) if self.market_api else None,
                lambda: okx_public_get(
                    "/api/v5/market/ticker",
                    {"instId": inst_id},
                    self.runtime_config.retry_times,
                    self.runtime_config.retry_backoff,
                ),
            ),
        )

        # API返回结构如下：code(是否成功)、msg（描述）、data（实际数据），接口只获取data
        data = okx_data(response)
        if not data:
            raise ValueError(f"ticker data unavailable for {inst_id}")

        # API返回的是数组,你可以一次性查多个币种，一个数组返回所有结果，这里取第一个，因为只查一个。
        item = data[0] if data else {}
        result = {
            "last": to_float(item.get("last")),     #成交价
            "bid_px": to_float(item.get("bidPx")),  #买入挂单价
            "ask_px": to_float(item.get("askPx")),  #卖出挂单价
        }
        if result["last"] <= 0:
            raise ValueError(f"ticker price invalid for {inst_id}")
        return result

    def _get_candles(self, inst_id: str, bar: str) -> List[Dict[str, Any]]:
        frame = self._replay_source(inst_id)
        if frame:
            candles = frame.get("candles") if isinstance(frame.get("candles"), dict) else {}
            rows = candles.get(bar)
            return deep_copy_json(rows) if isinstance(rows, list) else []
        # 每个周期取KLINE_LIMIT根K线：当前K线 + 足够多的历史K线。
        # 200根能让EMA120/MA120在去掉未收盘K线后仍保持稳定，同时给MACD/ADX/ATR留下缓冲。
        response = self._cached(
            f"candles:{inst_id}:{bar}",
            CACHE_TTL_SECONDS["candles"],
            lambda: self._sdk_or_rest(
                f"candles-{bar}",
                (lambda: self.market_api.get_candlesticks(instId=inst_id, bar=bar, limit=str(KLINE_LIMIT))) if self.market_api else None,
                lambda: okx_public_get(
                    "/api/v5/market/candles",
                    {"instId": inst_id, "bar": bar, "limit": str(KLINE_LIMIT)},
                    self.runtime_config.retry_times,
                    self.runtime_config.retry_backoff,
                ),
            ),
        )
        data = okx_data(response)
        if not data:
            raise ValueError(f"candles data unavailable for {inst_id} {bar}")

        # API返回的是K线数组，这里通过for循环遍历生成统一结构。
        # 一个结构包含：时间戳、开盘价、最高价、最低价、收盘价、成交量、是否收盘标记（当前k线还是历史k线）
        return [candle_to_dict(row) for row in data if isinstance(row, list)]

    def _get_open_interest(self, inst_id: str) -> float:
        frame = self._replay_source(inst_id)
        if frame:
            return to_float(frame.get("open_interest"))
        # 获取合约Open Interest。OI上涨通常说明有新仓进入市场。
        response = self._cached(
            f"open_interest:{inst_id}",
            CACHE_TTL_SECONDS["open_interest"],
            lambda: self._sdk_or_rest(
                "open-interest",
                (lambda: self.public_api.get_open_interest(instType="SWAP", instId=inst_id)) if self.public_api else None,
                lambda: okx_public_get(
                    "/api/v5/public/open-interest",
                    {"instType": "SWAP", "instId": inst_id},
                    self.runtime_config.retry_times,
                    self.runtime_config.retry_backoff,
                ),
            ),
        )
        data = okx_data(response)
        if not data:
            raise ValueError(f"open interest unavailable for {inst_id}")
        item = data[0] if data else {}
        value = to_float(item.get("oi")) or to_float(item.get("oiCcy"))
        if value <= 0:
            raise ValueError(f"open interest invalid for {inst_id}")
        return value

    def _get_funding_rate(self, inst_id: str) -> float:
        frame = self._replay_source(inst_id)
        if frame:
            return to_float(frame.get("funding_rate"))
        # 获取当前资金费率。正费率通常表示多头付费给空头，负费率相反。
        response = self._cached(
            f"funding_rate:{inst_id}",
            CACHE_TTL_SECONDS["funding_rate"],
            lambda: self._sdk_or_rest(
                "funding-rate",
                (lambda: self.public_api.get_funding_rate(instId=inst_id)) if self.public_api else None,
                lambda: okx_public_get(
                    "/api/v5/public/funding-rate",
                    {"instId": inst_id},
                    self.runtime_config.retry_times,
                    self.runtime_config.retry_backoff,
                ),
            ),
        )
        data = okx_data(response)
        if not data:
            raise ValueError(f"funding rate unavailable for {inst_id}")

        # API返回的是数组，可以同时获取多个币种的资金费率，这里取第一个
        item = data[0] if data else {}
        if "fundingRate" not in item:
            raise ValueError(f"funding rate missing for {inst_id}")
        return to_float(item.get("fundingRate"))

    def _get_long_short_ratio(self, inst_id: str) -> Dict[str, float]:
        frame = self._replay_source(inst_id)
        if frame:
            payload = frame.get("long_short_ratio") if isinstance(frame.get("long_short_ratio"), dict) else {}
            ratio = to_float(payload.get("long_short_ratio"))
            long_ratio = to_float(payload.get("long_ratio"))
            short_ratio = to_float(payload.get("short_ratio"))
            available = payload.get("available")
            if available is None:
                available = ratio > 0
            return {
                "long_short_ratio": ratio,
                "long_ratio": long_ratio,
                "short_ratio": short_ratio,
                "available": bool(available),
            }
        # 获取当前5m周期内的多空比，并换算成百分比；
        try:
            response = self._cached(
                f"long_short_ratio:{inst_id}",
                CACHE_TTL_SECONDS["long_short_ratio"],
                lambda: http_get_json(
                    "/api/v5/rubik/stat/contracts/long-short-account-ratio",
                    {"ccy": symbol_ccy(inst_id), "period": "5m"},
                    self.runtime_config.retry_times,
                    self.runtime_config.retry_backoff,
                ),
            )
            data = okx_data(response)
            item = data[0] if data else []
            ratio = to_float(item[1] if len(item) > 1 else 0.0)
        except Exception:
            ratio = 0.0

        long_ratio = ratio / (ratio + 1.0) if ratio > 0 else 0.0
        short_ratio = 1.0 - long_ratio if long_ratio > 0 else 0.0
        return {
            "long_short_ratio": ratio,
            "long_ratio": long_ratio,
            "short_ratio": short_ratio,
            "available": ratio > 0,
        }

    def _get_order_book(self, inst_id: str) -> Dict[str, Any]:
        frame = self._replay_source(inst_id)
        if frame:
            payload = frame.get("order_book") if isinstance(frame.get("order_book"), dict) else {}
            return deep_copy_json(payload)
        # 获取前20档订单簿深度。短线入场时，盘口买卖量不平衡能提示“突破是否有跟随资金”。
        # 注意：订单簿是瞬时数据，会被撤单、挂单墙诱导影响，所以这里只作为辅助确认，不作为方向核心。
        try:
            response = self._cached(
                f"order_book:{inst_id}",
                CACHE_TTL_SECONDS["order_book"],
                lambda: self._sdk_or_rest(
                    "order-book",
                    (lambda: self.market_api.get_orderbook(instId=inst_id, sz="20")) if self.market_api else None,
                    lambda: okx_public_get(
                        "/api/v5/market/books",
                        {"instId": inst_id, "sz": "20"},
                        self.runtime_config.retry_times,
                        self.runtime_config.retry_backoff,
                    ),
                ),
            )
            data = okx_data(response)
            item = data[0] if data else {}
            bids = item.get("bids") if isinstance(item, dict) else []
            asks = item.get("asks") if isinstance(item, dict) else []
        except Exception:
            bids, asks = [], []

        bid_size_5 = sum(to_float(row[1]) for row in bids[:5] if isinstance(row, list) and len(row) > 1)
        ask_size_5 = sum(to_float(row[1]) for row in asks[:5] if isinstance(row, list) and len(row) > 1)
        bid_size = sum(to_float(row[1]) for row in bids if isinstance(row, list) and len(row) > 1)
        ask_size = sum(to_float(row[1]) for row in asks if isinstance(row, list) and len(row) > 1)
        best_bid = to_float(bids[0][0]) if bids and isinstance(bids[0], list) else 0.0
        best_ask = to_float(asks[0][0]) if asks and isinstance(asks[0], list) else 0.0
        imbalance = safe_div(bid_size - ask_size, bid_size + ask_size)
        imbalance_5 = safe_div(bid_size_5 - ask_size_5, bid_size_5 + ask_size_5)
        return {
            "bid_size_5": bid_size_5,
            "ask_size_5": ask_size_5,
            "bid_size_20": bid_size,
            "ask_size_20": ask_size,
            "imbalance": imbalance,
            "imbalance_5": imbalance_5,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread": best_ask - best_bid if best_bid and best_ask else 0.0,
            "spread_pct": safe_div(best_ask - best_bid, (best_ask + best_bid) / 2) * 100 if best_bid and best_ask else 0.0,
            "available": bool(bids and asks),
        }

    def _volume_stats(self, candles: List[Dict[str, Any]], source_bar: str = "1m") -> Dict[str, float]:
        # 成交量判断使用当前策略对应周期的最近已收盘K线。
        # 如果直接使用未收盘K线，开盘几秒会低估，临近收盘又可能突然误报，导致放量信号不稳定。

        # 过滤KLINE_LIMIT根中的已收盘K线
        rows = confirmed_candles(candles)

        # 取最新k线的成交量
        current = rows[0]["volume"] if rows else 0.0
        # 提取历史成交量的和
        previous = [item["volume"] for item in rows[1:21]]
        # 计算平均成交量
        average = sum(previous) / len(previous) if previous else 0.0
        # 计算放量倍数
        multiplier = current / average if average > 0 else 0.0

        latest = rows[0] if rows else {}
        # 判断最新的K线方向：阳线、阴线、平盘
        direction = "up" if to_float(latest.get("close")) > to_float(latest.get("open")) else ("down" if to_float(latest.get("close")) < to_float(latest.get("open")) else "flat")
        recent = [item["volume"] for item in rows[:5]]
        older = [item["volume"] for item in rows[5:10]]
        recent_avg = sum(recent) / len(recent) if recent else 0.0
        older_avg = sum(older) / len(older) if older else 0.0

        # 判断趋势：上涨、下跌、平盘；通过最近5根K线和前5根k线的平均成交量判断，如果变化超过15%则判断为上涨、下跌，否则平盘
        trend = "rising" if recent_avg > older_avg * 1.15 and older_avg > 0 else ("falling" if recent_avg < older_avg * 0.85 and older_avg > 0 else "flat")
        return {
            "current": current,
            "average_20": average,
            "multiplier": multiplier,
            "direction": direction,
            "trend": trend,
            "recent_avg_5": recent_avg,
            "previous_avg_5": older_avg,
            "source": f"confirmed_{source_bar}",
            "source_bar": source_bar,
        }

    def _remember_metric(self, history: Deque[Tuple[float, float]], value: float, min_interval_seconds: int = 0) -> None:
        # 保存一条时间序列采样，格式为(timestamp, value)。
        # min_interval_seconds用于避免5秒轮询把同一个60秒缓存值重复写十几次。
        # 如果距离上次采样还很近，就更新最后一个样本的值，但保留原始采样时间，不追加新点：
        # - 最新值仍然能参与当前一轮计算；
        # - 历史队列不会被重复值撑满；
        # - 15m/1H这类按时间寻找旧值的逻辑仍然准确。
        now_ts = self._now_ts()
        numeric_value = to_float(value)
        if history and min_interval_seconds > 0 and now_ts - history[-1][0] < min_interval_seconds:
            history[-1] = (history[-1][0], numeric_value)
            return
        history.append((now_ts, numeric_value))

    def _remember_metric_at(
        self,
        history: Deque[Tuple[float, float]],
        timestamp: float,
        value: float,
        min_interval_seconds: int = 0,
    ) -> None:
        numeric_value = to_float(value)
        if timestamp <= 0:
            return
        if history and timestamp < history[-1][0]:
            return
        if history and min_interval_seconds > 0 and timestamp - history[-1][0] < min_interval_seconds:
            history[-1] = (history[-1][0], numeric_value)
            return
        history.append((timestamp, numeric_value))

    def _clear_market_histories(self) -> None:
        for container in (
            self.oi_history,
            self.funding_history,
            self.volume_multiplier_history,
            self.atr_pct_history,
            self.book_imbalance_history,
        ):
            container.clear()
        self.last_valid_market_data.clear()

    def _restore_market_histories_from_log(self) -> None:
        if self._market_history_restored or self.replay_mode:
            return
        self._market_history_restored = True
        if not LOG_FILE.exists():
            return
        cutoff = self._now_ts() - HISTORY_RETENTION_MINUTES * 60
        restored = 0
        try:
            text = tail_analysis_log_text(LOG_FILE, MARKET_HISTORY_RESTORE_MAX_BYTES)
            for line in text.splitlines():
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                    timestamp = parse_time_text(str(item.get("time", ""))).timestamp()
                except Exception:
                    continue
                if timestamp < cutoff:
                    continue
                inst_id = str(item.get("inst_id", "") or "")
                if inst_id not in self.instruments:
                    continue
                oi = to_float(item.get("open_interest"))
                if oi > 0:
                    self._remember_metric_at(self.oi_history[inst_id], timestamp, oi, METRIC_SAMPLE_INTERVAL_SECONDS)
                if "funding_rate" in item and item.get("funding_rate") is not None:
                    self._remember_metric_at(
                        self.funding_history[inst_id],
                        timestamp,
                        to_float(item.get("funding_rate")),
                        METRIC_SAMPLE_INTERVAL_SECONDS,
                    )
                volume = item.get("volume") if isinstance(item.get("volume"), dict) else {}
                if to_float(volume.get("average_20")) > 0:
                    volume_bar = str(volume.get("source_bar", "1m") or "1m")
                    self._remember_metric_at(
                        self.volume_multiplier_history[self._strategy_metric_key(inst_id, volume_bar)],
                        timestamp,
                        to_float(volume.get("multiplier")),
                        METRIC_SAMPLE_INTERVAL_SECONDS,
                    )
                profiles = item.get("trend_profiles") if isinstance(item.get("trend_profiles"), dict) else {}
                regime_bar = str((item.get("market_context") or {}).get("regime_bar", "15m") or "15m")
                atr_pct = to_float(profiles.get(regime_bar, {}).get("atr_pct"))
                if atr_pct > 0:
                    self._remember_metric_at(
                        self.atr_pct_history[self._strategy_metric_key(inst_id, regime_bar)],
                        timestamp,
                        atr_pct,
                        METRIC_SAMPLE_INTERVAL_SECONDS,
                    )
                order_book = item.get("order_book") if isinstance(item.get("order_book"), dict) else {}
                if order_book.get("available"):
                    self._remember_metric_at(
                        self.book_imbalance_history[inst_id],
                        timestamp,
                        to_float(order_book.get("imbalance")),
                        METRIC_SAMPLE_INTERVAL_SECONDS,
                    )
                restored += 1
        except Exception as exc:
            console_debug(f"[{now_text()}] restore market history failed: {exc}")
            return
        if restored:
            console_info(f"[{now_text()}] restored recent market history from {restored} log snapshots")

    def _history_values(self, history: Deque[Tuple[float, float]]) -> List[float]:
        # 只取数值部分，供动态阈值和分位数计算使用。
        return [value for _, value in history]

    def _dynamic_thresholds(self, inst_id: str, volume_bar: str = "1m", atr_bar: str = "15m") -> Dict[str, Any]:
        # 动态阈值用于解决“固定阈值在不同币种、不同时段不适配”的问题。
        # 例如BTC美盘高波动时2倍放量可能很常见，低波动亚洲盘1.5倍就很值得注意。
        # 为保持兼容，最终触发仍会参考用户配置，动态阈值作为更可靠的市场自适应参考。
        profile = self._instrument_profile(inst_id)
        volume_values = self._history_values(
            self.volume_multiplier_history[self._strategy_metric_key(inst_id, volume_bar)]
        )
        atr_values = self._history_values(
            self.atr_pct_history[self._strategy_metric_key(inst_id, atr_bar)]
        )
        book_values = [abs(value) for value in self._history_values(self.book_imbalance_history[inst_id])]
        return {
            "volume_multiplier_p85": percentile(volume_values, 0.85, max(self.config.volume_multiplier, profile["volume_multiplier_floor"])),
            "volume_multiplier_p95": percentile(volume_values, 0.95, max(self.config.volume_multiplier * 1.5, profile["volume_multiplier_floor"] * 1.4)),
            "atr_pct_p80": percentile(atr_values, 0.80, profile["atr_pct_normal"]),
            "book_imbalance_p85": percentile(book_values, 0.85, 0.35),
            "instrument_profile": profile,
            "volume_bar": volume_bar,
            "atr_bar": atr_bar,
            "sample_count": {
                "volume": len(volume_values),
                "atr_pct": len(atr_values),
                "book_imbalance": len(book_values),
            },
        }

    def _instrument_profile(self, inst_id: str) -> Dict[str, float]:
        # BTC和ETH的波动、成交量节奏、盘口深度不同。这里不硬编码交易结论，只提供刚启动时的fallback参考。
        if inst_id.startswith("ETH-"):
            return {
                "volume_multiplier_floor": 2.2,
                "atr_pct_normal": 0.35,
                "max_chase_distance_atr": 1.8,
            }
        return {
            "volume_multiplier_floor": 2.0,
            "atr_pct_normal": 0.28,
            "max_chase_distance_atr": 1.6,
        }

    def _volatility_context(self, inst_id: str, profiles: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        # 波动率上下文用于决定“能不能交易”和“止损止盈该多宽”。
        # ATR%过高时，信号更容易追在尾端；ATR%过低时，容易横盘假突破。
        context_bars = self._strategy_context_bars()
        regime_bar = str(context_bars.get("regime", "15m"))
        atr_pct = to_float(profiles.get(regime_bar, {}).get("atr_pct"))
        atr_pct_15m = to_float(profiles.get("15m", {}).get("atr_pct"))
        atr_pct_1h = to_float(profiles.get("1H", {}).get("atr_pct"))
        historical = self._history_values(
            self.atr_pct_history[self._strategy_metric_key(inst_id, regime_bar)]
        )
        p80 = percentile(historical, 0.80, atr_pct)
        if historical and atr_pct >= p80 and atr_pct > 0:
            regime = "high_volatility"
        elif atr_pct < to_float(TREND_PROFILE_PARAMS.get(regime_bar, {}).get("atr_floor_pct"), 0.08):
            regime = "low_volatility"
        else:
            regime = "normal"
        return {
            "regime": regime,
            "bar": regime_bar,
            "atr": to_float(profiles.get(regime_bar, {}).get("atr")),
            "atr_pct": atr_pct,
            "atr_1m": to_float(profiles.get("1m", {}).get("atr")),
            "atr_5m": to_float(profiles.get("5m", {}).get("atr")),
            "atr_15m": to_float(profiles.get("15m", {}).get("atr")),
            "atr_pct_15m": atr_pct_15m,
            "atr_pct_1h": atr_pct_1h,
            "atr_pct_p80": p80,
        }

    def _market_context(
        self,
        price: float,
        candles: Dict[str, List[Dict[str, Any]]],
        profiles: Dict[str, Dict[str, Any]],
        volume: Dict[str, float],
        open_interest: float,
        oi_change_pct_15m: float,
        funding_rate: float,
        funding_change_15m: float,
        long_short: Dict[str, float],
        order_book: Dict[str, Any],
        volatility: Dict[str, Any],
        dynamic_thresholds: Dict[str, Any],
        oi_change_pct_strategy: Optional[float] = None,
        funding_change_strategy: Optional[float] = None,
        derivative_window_minutes: int = 15,
        profiles_live: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        # 市场上下文是新版判断的核心：先判断市场处于什么状态，再谈方向和入场。
        # 这样可以避免“放量就做多/做空”“多周期多数上涨就追多”的简单规则。
        direction_profiles = profiles_live if isinstance(profiles_live, dict) and profiles_live else profiles
        context_bars = self._strategy_context_bars()
        trend_votes = {
            group: [direction_profiles.get(bar, {}).get("trend") for bar in context_bars.get(group, ())]
            for group in ("entry", "trade", "higher")
        }
        vote_metrics = self._weighted_trend_votes(
            trend_votes,
            context_bars.get("group_weights", {}),
        )
        up_count = sum(1 for group in trend_votes.values() for item in group if item == "up")
        down_count = sum(1 for group in trend_votes.values() for item in group if item == "down")
        range_count = sum(1 for group in trend_votes.values() for item in group if item in ("range", "mixed"))

        higher_up = trend_votes["higher"].count("up")
        higher_down = trend_votes["higher"].count("down")
        trade_up = trend_votes["trade"].count("up")
        trade_down = trend_votes["trade"].count("down")
        entry_up = trend_votes["entry"].count("up")
        entry_down = trend_votes["entry"].count("down")
        regime_bar = str(context_bars.get("regime", "15m"))
        regime_profile = direction_profiles.get(regime_bar, {})
        adx_15m = to_float(regime_profile.get("adx", {}).get("adx"))
        boll_width = to_float(regime_profile.get("boll", {}).get("bandwidth_pct"))
        macd_15m = regime_profile.get("macd", {})
        rsi_15m = to_float(regime_profile.get("rsi", {}).get("14"), 50.0)
        regime_atr_pct = to_float(regime_profile.get("atr_pct"))
        squeeze = boll_width > 0 and boll_width < max(0.35, regime_atr_pct * 1.4)
        mode = self._strategy_mode()
        pressure_spec = self._strategy_pressure_spec()
        pressure_rows = candles.get(pressure_spec["bar"], [])
        live = price if price > 0 else 0.0
        pressure_moves = [
            self._recent_move_pct(pressure_rows, bars, live_price=live)
            for bars in pressure_spec["bars"]
        ]
        move_5m, move_10m, move_15m, move_20m = pressure_moves
        pressure_volatility = dict(volatility)
        pressure_volatility["atr_pct_15m"] = regime_atr_pct
        recent_price_pressure = self._recent_price_pressure(move_5m, move_10m, move_15m, pressure_volatility)

        vote_groups = vote_metrics["groups"]
        trade_metrics = vote_groups["trade"]
        entry_metrics = vote_groups["entry"]
        higher_metrics = vote_groups["higher"]
        indicator_consensus = self._htf_indicator_consensus(direction_profiles)
        indicator_alignment = self._htf_indicator_alignment_flags(direction_profiles)
        indicator_long = self._htf_indicator_supports(
            indicator_consensus, "做多", alignment=indicator_alignment, min_net=20.0,
        )
        indicator_short = self._htf_indicator_supports(
            indicator_consensus, "做空", alignment=indicator_alignment, min_net=20.0,
        )
        long_confirmed = (
            higher_metrics["up_ratio"] >= 0.5
            and vote_metrics["weighted_up"] > vote_metrics["weighted_down"]
            and (
                trade_metrics["up_ratio"] >= 0.75
                or (
                    trade_metrics["up_ratio"] >= 0.5
                    and recent_price_pressure == "up"
                    and entry_metrics["up_ratio"] >= 0.5
                )
                or (
                    indicator_long
                    and trade_metrics["up_ratio"] >= 0.5
                    and recent_price_pressure != "down"
                )
            )
        )
        short_confirmed = (
            higher_metrics["down_ratio"] >= 0.5
            and vote_metrics["weighted_down"] > vote_metrics["weighted_up"]
            and (
                trade_metrics["down_ratio"] >= 0.75
                or (
                    trade_metrics["down_ratio"] >= 0.5
                    and recent_price_pressure == "down"
                    and entry_metrics["down_ratio"] >= 0.5
                )
                or (
                    indicator_short
                    and trade_metrics["down_ratio"] >= 0.5
                    and recent_price_pressure != "up"
                )
            )
        )

        if (
            recent_price_pressure == "down"
            and trade_metrics["down_ratio"] >= 0.35
            and vote_metrics["weighted_down"] >= vote_metrics["weighted_up"] * 0.80
        ):
            short_confirmed = True

        if squeeze and adx_15m < 18:
            bias = "neutral"
            regime = "squeeze"
        elif long_confirmed:
            bias = "long"
            regime = "trend_up"
        elif short_confirmed:
            bias = "short"
            regime = "trend_down"
        elif vote_metrics["range_ratio"] >= 0.5:
            bias = "neutral"
            regime = "range"
        else:
            bias = "neutral"
            regime = "mixed"

        if volatility["regime"] == "high_volatility":
            regime = "high_volatility"
        if regime in ("trend_up", "trend_down") and adx_15m < 16:
            regime = "mixed"
            bias = "neutral"
        bias_softened = False
        structural_bias = bias
        if bias == "long" and recent_price_pressure == "down":
            regime = "mixed"
            bias_softened = True
            bias = "neutral"
        elif bias == "short" and recent_price_pressure == "up":
            regime = "mixed"
            bias_softened = True
            bias = "neutral"
        trend_phase = self._trend_phase(
            structural_bias=structural_bias,
            bias=bias,
            regime=regime,
            pressure=recent_price_pressure,
            profile=regime_profile,
        )

        recent_15m = confirmed_candles(candles.get("15m", []))
        old_close = to_float(recent_15m[min(4, len(recent_15m) - 1)].get("close")) if recent_15m else price
        price_change_15m = pct_change(price, old_close)
        price_change_strategy = pressure_moves[-1]
        effective_oi_change = (
            oi_change_pct_15m
            if oi_change_pct_strategy is None
            else to_float(oi_change_pct_strategy)
        )
        effective_funding_change = (
            funding_change_15m
            if funding_change_strategy is None
            else to_float(funding_change_strategy)
        )
        oi_price_state = self._oi_price_state(price_change_strategy, effective_oi_change)
        volume_threshold = max(self.config.volume_multiplier, dynamic_thresholds.get("volume_multiplier_p85", 0.0))
        order_book_bias = "neutral"
        if order_book.get("available") and mode in ("scalp", "short", "swing"):
            combined_imbalance = (order_book.get("imbalance", 0.0) + order_book.get("imbalance_5", 0.0)) / 2
            if combined_imbalance >= max(0.25, dynamic_thresholds.get("book_imbalance_p85", 0.35) * 0.8):
                order_book_bias = "bid_support"
            elif combined_imbalance <= -max(0.25, dynamic_thresholds.get("book_imbalance_p85", 0.35) * 0.8):
                order_book_bias = "ask_pressure"

        warnings = []
        if regime == "squeeze":
            warnings.append("布林带挤压且ADX偏弱，方向未确认，适合等待放量突破")
        if volatility["regime"] == "high_volatility":
            warnings.append("ATR处于高波动区，追单和固定止损失效风险升高")
        if adx_15m < 18 and regime not in ("squeeze", "range"):
            warnings.append("ADX偏弱，趋势有效性不足")
        if regime_profile.get("divergence") in ("bearish", "bullish"):
            warnings.append(f"{regime_bar} RSI出现{regime_profile.get('divergence')}背离")
        if bias_softened:
            warnings.append("短窗价格与结构bias反向，保留bias供情绪/结构综合判断")
        if abs(to_float(macd_15m.get("hist_slope"))) < abs(to_float(macd_15m.get("hist"))) * 0.15 and abs(to_float(macd_15m.get("hist"))) > 0:
            warnings.append("MACD柱体变化放缓，动能可能衰减")
        if rsi_15m > 78 or rsi_15m < 22:
            warnings.append(f"{regime_bar} RSI处于极端区域，追单风险升高")
        if abs(funding_rate) >= self.config.funding_abs_threshold:
            warnings.append("资金费率过热，单边拥挤风险升高")
        if max(long_short.get("long_ratio", 0.0), long_short.get("short_ratio", 0.0)) >= self.config.long_short_extreme:
            warnings.append("多空账户占比极端，需防止拥挤反转")
        if volume["multiplier"] < volume_threshold and regime_profile.get("breakout") != "none":
            warnings.append("结构突破缺少放量确认")

        return {
            "regime": regime,
            "bias": bias,
            "structural_bias": structural_bias,
            "bias_softened": bias_softened,
            "trend_votes": trend_votes,
            "trend_vote_metrics": vote_metrics,
            "up_count": up_count,
            "down_count": down_count,
            "entry_up": entry_up,
            "entry_down": entry_down,
            "trade_up": trade_up,
            "trade_down": trade_down,
            "recent_price_pressure": recent_price_pressure,
            "tactical_pressure": recent_price_pressure,
            "trend_phase": trend_phase,
            "strategy_mode": mode,
            "strategy_bars": context_bars,
            "regime_bar": regime_bar,
            "indicator_consensus": indicator_consensus,
            "indicator_alignment": indicator_alignment,
            "recent_move_pct": {
                "5m": move_5m,
                "10m": move_10m,
                "15m": move_15m,
                "20m": move_20m,
            },
            "pressure_windows": {
                "base_bar": pressure_spec["bar"],
                "labels": list(pressure_spec["labels"]),
                "bars": list(pressure_spec["bars"]),
                "moves": {
                    label: move
                    for label, move in zip(pressure_spec["labels"], pressure_moves)
                },
            },
            "price_change_15m": price_change_15m,
            "price_change_strategy": price_change_strategy,
            "derivative_window_minutes": derivative_window_minutes,
            "oi_change_pct_strategy": effective_oi_change,
            "funding_change_strategy": effective_funding_change,
            "oi_price_state": oi_price_state,
            "volume_threshold_used": volume_threshold,
            "order_book_bias": order_book_bias,
            "strategy_template": self._strategy_template(
                regime,
                bias,
                trend_phase,
                structural_bias,
            ),
            "warnings": warnings,
        }

    def _strategy_template(
        self,
        regime: str,
        bias: str,
        trend_phase: str = "transition",
        structural_bias: str = "neutral",
    ) -> str:
        # 不同市场状态应该使用不同交易模板。这个字段给AI、本地分析和后续回测归因使用。
        if trend_phase == "pullback_in_uptrend" and structural_bias == "long":
            return "bullish_pullback_wait_reclaim"
        if trend_phase == "rebound_in_downtrend" and structural_bias == "short":
            return "bearish_rebound_wait_rejection"
        if trend_phase == "breakout_attempt_up":
            return "breakout_up_wait_confirmation"
        if trend_phase == "breakout_attempt_down":
            return "breakout_down_wait_confirmation"
        if regime == "trend_up" and bias == "long":
            return "trend_pullback_long"
        if regime == "trend_down" and bias == "short":
            return "trend_pullback_short"
        if regime == "squeeze":
            return "wait_breakout_after_squeeze"
        if regime == "range":
            return "range_edge_only"
        if regime == "high_volatility":
            return "reduce_size_wait_retest"
        return "no_trade_until_alignment"

    def _oi_price_state(self, price_change_pct: float, oi_change_pct: float) -> str:
        # OI必须和价格一起看，否则“持仓增加”无法区分多空。
        if abs(oi_change_pct) < 0.5:
            return "oi_flat"
        if price_change_pct > 0 and oi_change_pct > 0:
            return "price_up_oi_up_new_longs_or_short_pressure"
        if price_change_pct > 0 and oi_change_pct < 0:
            return "price_up_oi_down_short_covering"
        if price_change_pct < 0 and oi_change_pct > 0:
            return "price_down_oi_up_new_shorts_or_long_pressure"
        if price_change_pct < 0 and oi_change_pct < 0:
            return "price_down_oi_down_long_deleveraging"
        return "mixed"

    def _change_pct_last_minutes(self, history: Deque[Tuple[float, float]], minutes: int) -> float:
        # 计算最近N分钟百分比变化，例如15分钟OI变化。
        old_value = self._old_value(history, minutes)
        if old_value <= 0 or not history:
            return 0.0
        return (history[-1][1] - old_value) / old_value * 100

    def _change_last_minutes(self, history: Deque[Tuple[float, float]], minutes: int) -> float:
        # 计算最近N分钟绝对变化，例如资金费率变化。
        old_value = self._old_value(history, minutes)
        if not history:
            return 0.0
        return history[-1][1] - old_value

    def _old_value(self, history: Deque[Tuple[float, float]], minutes: int) -> float:
        # 找到N分钟前附近的旧值。刚启动不足N分钟时，会退化为最早采样值。
        if not history:
            return 0.0
        threshold = self._now_ts() - minutes * 60
        candidate = history[0][1]
        for ts, value in history:
            if ts >= threshold:
                return candidate
            candidate = value
        return candidate

    def _history_ready(self, history: Deque[Tuple[float, float]], minutes: int) -> bool:
        # OI和资金费率变化类信号需要完整观察窗口。
        # 刚启动不足15分钟时不触发“15分钟变化”类信号，避免误报。
        if len(history) < 2:
            return False
        return history[-1][0] - history[0][0] >= minutes * 60

    def _suggest_levels(self, snapshot: Dict[str, Any], direction: str) -> Dict[str, Any]:
        # 入场计划不再用固定百分比，而是结合：
        # 1. 15m ATR：决定止损缓冲和止盈距离；
        # 2. 15m结构高低点：决定真正的失效位置；
        # 3. 1m/5m EMA和近似VWAP：决定更合理的等待回踩区；
        # 4. 市场状态和盘口：决定是允许突破入场、等待回踩，还是直接观望。
        price = to_float(snapshot.get("price"))
        context = snapshot.get("market_context", {})
        profiles = snapshot.get("trend_profiles", {})
        candles = snapshot.get("candles", {})
        order_book = snapshot.get("order_book", {})
        if price <= 0 or direction == "观望":
            return {
                "quality": "no_trade",
                "entry": "-",
                "stop_loss": "-",
                "take_profit": "-",
                "invalidation": "市场方向不清晰或风险收益比不足，等待5m/15m结构重新确认",
                "wait_for": ["5m/15m方向一致", "回踩关键均线或VWAP后重新放量", "止损空间可控"],
            }

        atr_15m = to_float(profiles.get("15m", {}).get("atr")) or price * 0.004
        atr_5m = to_float(profiles.get("5m", {}).get("atr")) or atr_15m * 0.45
        profile_15m = profiles.get("15m", {})
        profile_5m = profiles.get("5m", {})
        vwap_1m = self._vwap(candles.get("1m", []), 60)
        ema_5m = to_float(profile_5m.get("ema_fast")) or price
        buffer = max(atr_5m * 0.35, price * 0.0008)
        wait_for = []

        if direction == "做多":
            anchor = max(vwap_1m or 0.0, ema_5m, price - atr_5m)
            entry_low = min(price, anchor) - buffer * 0.35
            entry_high = min(price + buffer * 0.6, anchor + buffer)
            structural_stop = to_float(profile_15m.get("recent_low")) or price - atr_15m
            stop = min(structural_stop - atr_5m * 0.25, entry_low - atr_5m * 0.8)
            risk = max(entry_high - stop, atr_5m)
            take_1 = entry_high + risk * 1.2
            take_2 = entry_high + risk * 2.0
            invalidation = "5m收盘跌破入场区下沿且15m结构低点失守"
            if context.get("order_book_bias") != "bid_support":
                wait_for.append("盘口买盘支撑增强或回踩后主动买量放大")
            if profile_15m.get("breakout") != "up":
                wait_for.append("15m突破或回踩不破后再确认")
        else:
            anchor = min(vwap_1m or price, ema_5m, price + atr_5m)
            entry_low = max(price - buffer * 0.6, anchor - buffer)
            entry_high = max(price, anchor) + buffer * 0.35
            structural_stop = to_float(profile_15m.get("recent_high")) or price + atr_15m
            stop = max(structural_stop + atr_5m * 0.25, entry_high + atr_5m * 0.8)
            risk = max(stop - entry_low, atr_5m)
            take_1 = entry_low - risk * 1.2
            take_2 = entry_low - risk * 2.0
            invalidation = "5m收盘突破入场区上沿且15m结构高点收复"
            if context.get("order_book_bias") != "ask_pressure":
                wait_for.append("盘口卖盘压力增强或反抽后主动卖量放大")
            if profile_15m.get("breakout") != "down":
                wait_for.append("15m跌破或反抽不过后再确认")

        quality = "breakout_valid" if not wait_for and context.get("regime") in ("trend_up", "trend_down") else "wait_confirmation"
        if context.get("regime") in ("range", "mixed", "high_volatility"):
            quality = "wait_confirmation"
            wait_for.append("等待高波动/震荡状态降温，避免追单")

        return {
            "quality": quality,
            "entry": f"{entry_low:.2f} - {entry_high:.2f}",
            "stop_loss": f"{stop:.2f}",
            "take_profit": f"{take_1:.2f} / {take_2:.2f}",
            "invalidation": invalidation,
            "wait_for": wait_for or ["按计划等待回踩或突破后的二次确认，不追逐瞬时拉升/下跌"],
            "atr_reference": {
                "atr_5m": atr_5m,
                "atr_15m": atr_15m,
                "vwap_1m_approx": vwap_1m,
                "ema_5m_fast": ema_5m,
            },
        }

    def _vwap(self, candles: List[Dict[str, Any]], lookback: int = 60) -> float:
        # OKX公开K线没有逐笔主动买卖量，这里用典型价格*成交量近似VWAP。
        # 它不是交易所精确VWAP，但足以作为短线回踩/反抽的参考锚点。
        rows = confirmed_candles(candles)[:lookback]
        total_volume = sum(to_float(item.get("volume")) for item in rows)
        if total_volume <= 0:
            return 0.0
        weighted = 0.0
        for item in rows:
            typical = (to_float(item.get("high")) + to_float(item.get("low")) + to_float(item.get("close"))) / 3
            weighted += typical * to_float(item.get("volume"))
        return weighted / total_volume

    def _market_risk_level(self, total_score: int, signals: List[Dict[str, Any]]) -> str:
        # 市场风险等级：描述行情本身是否拥挤、过热、冲突，不等同于“能不能交易”。
        risk_signal_types = {item["type"] for item in signals}
        if total_score < 70 or "funding_hot" in risk_signal_types or "rsi_extreme" in risk_signal_types:
            return "高"
        if total_score < 80 or "long_short_extreme" in risk_signal_types or "rsi_divergence" in risk_signal_types or "boll_squeeze" in risk_signal_types:
            return "中"
        return "低"

    def _trade_action_level(self, final_trade_score: int, direction: str, entry_plan: Dict[str, Any]) -> str:
        # 交易动作等级：描述当前是否适合执行。观望不是“低风险”，而是“无交易动作”。
        if direction == "观望" or final_trade_score <= 0:
            return "观望"
        if entry_plan.get("quality") == "breakout_valid" and final_trade_score >= self.trade_push_score(direction):
            return "可关注"
        confirm_floor = {"scalp": 62, "short": 68, "swing": 72, "long": 76}.get(self._strategy_mode(), 68)
        if final_trade_score >= confirm_floor:
            return "等待确认"
        return "不建议"

    def _push_kind(self, score: Dict[str, Any], signals: List[Dict[str, Any]]) -> str:
        signal_types = {item.get("type") for item in signals}
        scalp_view = score.get("strategy_views", {}).get("scalp", {})
        if self.config.signal_spike_enabled and scalp_view.get("action_level") in ("急速异动", "可短打") and scalp_view.get("score", 0) >= self.config.spike_push_score:
            return "spike"
        direction = score.get("direction", "观望")
        if (
            self.config.signal_trade_enabled
            and direction in ("做多", "做空")
            and score.get("final_trade_score", 0) >= self.trade_push_score(direction)
        ):
            return "trade"
        watch_signals = {"funding_hot", "rsi_extreme", "rsi_divergence", "boll_squeeze", "long_short_extreme"}
        if self.config.signal_watch_enabled and score.get("raw_total_score", 0) >= self.config.watch_push_score and signal_types.intersection(watch_signals):
            return "watch"
        return ""

    def _ai_forced_bars(self, signals: List[Dict[str, Any]], trigger: Dict[str, Any]) -> set:
        forced: set = set()
        signal_types = {item.get("type", "") for item in signals}
        for signal_type in signal_types:
            forced.update(AI_SIGNAL_BAR_MAP.get(signal_type, ()))
        if trigger.get("level") == "L3" and "scalp_spike" in (trigger.get("reasons") or []):
            forced.update({"1m", "3m", "5m", "15m"})
        return forced

    def _ai_relevant_bars(self, signals: List[Dict[str, Any]], trigger: Optional[Dict[str, Any]] = None) -> List[str]:
        trigger = trigger or {}
        mode = self._strategy_mode()
        profile = self._strategy_profile(mode)
        forced = self._ai_forced_bars(signals, trigger)
        bars = set(profile["primary_bars"]) | set(profile["confirm_bars"]) | set(profile["background_bars"]) | forced
        ignore = set(profile.get("ignore_bars") or [])
        bars = bars - (ignore - forced)
        ordered = [bar for bar in BAR_CHANNELS if bar in bars]
        return ordered

    def _ai_candle_limit(self, mode: str, bar: str, forced_bars: set) -> int:
        limit = int(AI_CANDLE_LIMITS.get(mode, AI_CANDLE_LIMITS["short"]).get(bar, 0))
        if limit <= 0 and bar in forced_bars:
            limit = int(AI_SIGNAL_CANDLE_LIMITS.get(bar, 12))
        return max(0, limit)

    def _ai_history_limit(self) -> int:
        mode = self._strategy_mode()
        return int(AI_HISTORY_LIMITS.get(mode, AI_HISTORY_LIMITS["short"]))

    def _ai_build_candles(
        self,
        snapshot: Dict[str, Any],
        signals: List[Dict[str, Any]],
        trigger: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Dict[str, List[Dict[str, Any]]], List[str], List[str]]:
        trigger = trigger or {}
        mode = self._strategy_mode()
        forced = self._ai_forced_bars(signals, trigger)
        relevant = self._ai_relevant_bars(signals, trigger)
        candles: Dict[str, List[Dict[str, Any]]] = {}
        profile_only: List[str] = []
        all_bars = list(BAR_CHANNELS)
        profiles = snapshot.get("trend_profiles", {})
        for bar in all_bars:
            if bar not in relevant:
                continue
            limit = self._ai_candle_limit(mode, bar, forced)
            if limit > 0:
                candles[bar] = compact_candles(snapshot["candles"].get(bar, []), limit)
            elif bar in profiles:
                profile_only.append(bar)
        return candles, relevant, profile_only

    def _ai_background_profiles(
        self,
        snapshot: Dict[str, Any],
        profile_only_bars: List[str],
    ) -> Dict[str, Dict[str, Any]]:
        profiles = snapshot.get("trend_profiles", {})
        return {
            bar: compact_background_profile(profiles.get(bar, {}))
            for bar in profile_only_bars
            if bar in profiles
        }

    def _ai_bar_profiles(
        self,
        snapshot: Dict[str, Any],
        candle_bars: List[str],
    ) -> Dict[str, Dict[str, Any]]:
        profiles = snapshot.get("trend_profiles", {}) if isinstance(snapshot.get("trend_profiles"), dict) else {}
        return {
            bar: compact_ai_bar_profile(profiles.get(bar, {}))
            for bar in candle_bars
            if bar in profiles
        }

    def _ai_trigger_context(
        self,
        snapshot: Dict[str, Any],
        signals: List[Dict[str, Any]],
        trigger: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        trigger = trigger or {}
        signal_rows: List[Dict[str, Any]] = []
        for item in signals:
            row: Dict[str, Any] = {
                "type": item.get("type", ""),
                "desc": item.get("desc", ""),
            }
            hint = item.get("direction_hint")
            if hint == "做多":
                row["breakout"] = "up"
            elif hint == "做空":
                row["breakout"] = "down"
            signal_rows.append(row)
        return {
            "role": "trigger_context_only",
            "note": (
                "Explains why AI was invoked and what rule-based detectors fired. "
                "Not a trade recommendation; analyze independently from market_data."
            ),
            "level": trigger.get("level", "L0"),
            "reasons": trigger.get("reasons", []),
            "signals": signal_rows,
            "signal_evidence": self._signal_evidence(snapshot, signals),
        }

    def _validate_ai_result(self, parsed: Optional[Dict[str, Any]]) -> Tuple[bool, List[str]]:
        # AI输出必须包含固定字段；优先 data_quality，兼容旧 rule_audit。
        required = {
            "trend",
            "risk",
            "suggestion",
            "direction",
            "entry",
            "stop_loss",
            "take_profit",
            "risk_level",
            "confidence",
            "push_recommendation",
            "reasons",
            "forward_view",
        }
        if not parsed:
            return False, ["AI output is not a JSON object"]

        errors = []
        missing = sorted(required - set(parsed.keys()))
        if missing:
            errors.append(f"missing fields: {missing}")

        if parsed.get("direction") not in ("做多", "做空", "观望"):
            errors.append("direction must be 做多/做空/观望")

        if parsed.get("risk_level") not in ("低", "中", "高"):
            errors.append("risk_level must be 低/中/高")

        push_rec = parsed.get("push_recommendation")
        if push_rec not in ("none", "watch", "trade", "spike"):
            errors.append("push_recommendation must be none/watch/trade/spike")

        confidence = parsed.get("confidence")
        if not isinstance(confidence, (int, float)):
            errors.append("confidence must be a number")

        if not isinstance(parsed.get("reasons"), list):
            errors.append("reasons must be a list")

        trend = parsed.get("trend")
        if not isinstance(trend, dict):
            errors.append("trend must be an object")

        forward_view = parsed.get("forward_view")
        if not isinstance(forward_view, dict):
            errors.append("forward_view must be an object")
        else:
            if forward_view.get("direction") not in ("做多", "做空", "观望"):
                errors.append("forward_view.direction must be 做多/做空/观望")
            fv_prob = forward_view.get("probability")
            if not isinstance(fv_prob, (int, float)):
                errors.append("forward_view.probability must be a number")
            fv_horizon = forward_view.get("horizon_minutes")
            if not isinstance(fv_horizon, (int, float)):
                errors.append("forward_view.horizon_minutes must be a number")
            if not str(forward_view.get("summary", "") or "").strip():
                errors.append("forward_view.summary is required")
            if not str(forward_view.get("invalidation", "") or "").strip():
                errors.append("forward_view.invalidation is required")

        data_quality = parsed_data_quality(parsed)
        if not data_quality:
            errors.append("data_quality (or legacy rule_audit) must be an object")
        else:
            if not str(data_quality.get("overall", "") or "").strip():
                errors.append("data_quality.overall is required")
            if not isinstance(data_quality.get("warnings"), list):
                errors.append("data_quality.warnings must be a list")

        return not errors, errors

    def _ai_payload(
        self,
        snapshot: Dict[str, Any],
        signals: List[Dict[str, Any]],
        score: Dict[str, Any],
        trigger: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        # 自主分析 payload：market_data 为唯一事实来源；trigger_context 仅说明为何调用 AI。
        inst_id = snapshot["inst_id"]
        trigger = trigger or {}
        mode = self._strategy_mode()
        profile = self._strategy_profile(mode)
        candles, included_bars, profile_only_bars = self._ai_build_candles(snapshot, signals, trigger)
        history_limit = self._ai_history_limit()
        volatility = snapshot.get("volatility", {})
        candle_bars = sorted(candles.keys())
        return {
            "instrument": inst_id,
            "snapshot_time": snapshot["time"],
            "payload_meta": {
                "included_bars": included_bars,
                "raw_candle_bars": candle_bars,
                "bar_profile_bars": candle_bars,
                "profile_only_bars": profile_only_bars,
                "candle_order": "newest_first",
                "analysis_mode": "independent",
                "note": (
                    "Analyze only from market_data. candles/bar_profiles use newest_first ordering "
                    "(index 0 = latest bar). trigger_context lists detectors that fired; "
                    "it is not a trade recommendation and must not be treated as pre-computed direction."
                ),
            },
            "market_data": {
                "market": {
                    "current_price": snapshot["price"],
                    "best_bid": snapshot["best_bid"],
                    "best_ask": snapshot["best_ask"],
                },
                "candles": candles,
                "bar_profiles": self._ai_bar_profiles(snapshot, candle_bars),
                "background_profiles": self._ai_background_profiles(snapshot, profile_only_bars),
                "market_context": compact_ai_market_context(snapshot),
                "derivatives": {
                    "volume": snapshot["volume"],
                    "open_interest": snapshot["open_interest"],
                    "oi_change_pct_15m": snapshot["oi_change_pct_15m"],
                    "oi_change_pct_strategy": snapshot.get("oi_change_pct_strategy"),
                    "window_minutes": snapshot.get("derivative_window_minutes", 15),
                    "oi_warmup_ready": snapshot["oi_warmup_ready"],
                    "oi_strategy_warmup_ready": snapshot.get("oi_strategy_warmup_ready"),
                    "funding_rate": snapshot["funding_rate"],
                    "funding_change_15m": snapshot["funding_change"],
                    "funding_change_strategy": snapshot.get("funding_change_strategy"),
                    "funding_warmup_ready": snapshot["funding_warmup_ready"],
                    "funding_strategy_warmup_ready": snapshot.get("funding_strategy_warmup_ready"),
                    "long_short_ratio": snapshot["long_short_ratio"],
                    "oi_history": history_tail(self.oi_history[inst_id], history_limit),
                    "funding_history": history_tail(self.funding_history[inst_id], history_limit),
                },
                "order_book": snapshot.get("order_book", {}),
                "volatility": volatility,
                "dynamic_thresholds": snapshot.get("dynamic_thresholds", {}),
                "instrument_profile": snapshot.get("instrument_profile", {}),
                "snapshot_quality": snapshot.get("snapshot_quality", {}),
                "data_sources": snapshot.get("data_sources", {}),
            },
            "trigger_context": self._ai_trigger_context(snapshot, signals, trigger),
            "analysis_config": {
                "strategy_mode": mode,
                "strategy_label": profile.get("label", score.get("strategy_label")),
                "risk_preference": self._risk_preference(),
                "ai_output_style": self._ai_output_style(),
                "holding_time": profile.get("holding_time", ""),
                "primary_bars": profile.get("primary_bars", []),
                "confirm_bars": profile.get("confirm_bars", []),
                "background_bars": profile.get("background_bars", []),
                "push_thresholds": {
                    "trade_long": self.push_score,
                    "trade_short": self.short_push_score,
                    "watch": self.config.watch_push_score,
                    "spike": self.config.spike_push_score,
                    "forecast": self.config.forecast_push_score,
                },
                "signal_types_enabled": {
                    "trade": self.config.signal_trade_enabled,
                    "watch": self.config.signal_watch_enabled,
                    "spike": self.config.signal_spike_enabled,
                    "forecast": self.config.signal_forecast_enabled,
                },
                "reference_thresholds": {
                    "volume_multiplier": self.config.volume_multiplier,
                    "oi_change_pct_15m": self.config.oi_change_pct_15m,
                    "funding_abs_threshold": self.config.funding_abs_threshold,
                    "funding_change_threshold": self.config.funding_change_threshold,
                    "long_short_extreme": self.config.long_short_extreme,
                },
            },
        }

    def _signal_evidence(
        self,
        snapshot: Dict[str, Any],
        signals: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        # 每个信号的 current vs threshold；不含 valid_by_rule，由 AI 自行判断是否成立。
        evidence = []
        signal_types = {item["type"] for item in signals}
        profiles = snapshot.get("trend_profiles", {}) if isinstance(snapshot.get("trend_profiles"), dict) else {}
        profile_15m = profiles.get("15m", {})

        if "volume_spike" in signal_types:
            evidence.append({
                "type": "volume_spike",
                "current": snapshot["volume"]["multiplier"],
                "threshold": snapshot.get("market_context", {}).get("volume_threshold_used", self.config.volume_multiplier),
                "detail": snapshot["volume"],
            })

        if "structure_break" in signal_types:
            evidence.append({
                "type": "structure_break",
                "profiles": {
                    "5m": compact_background_profile(profiles.get("5m", {})),
                    "15m": compact_background_profile(profiles.get("15m", {})),
                },
            })

        if "boll_squeeze" in signal_types:
            boll = profile_15m.get("boll", {}) if isinstance(profile_15m.get("boll"), dict) else {}
            adx = profile_15m.get("adx", {}) if isinstance(profile_15m.get("adx"), dict) else {}
            evidence.append({
                "type": "boll_squeeze",
                "boll_bandwidth_pct": boll.get("bandwidth_pct"),
                "adx": adx.get("adx"),
                "market_regime": snapshot.get("market_context", {}).get("regime"),
            })

        if "rsi_divergence" in signal_types or "rsi_extreme" in signal_types:
            rsi = profile_15m.get("rsi", {}) if isinstance(profile_15m.get("rsi"), dict) else {}
            evidence.append({
                "type": "rsi_state",
                "rsi_14": rsi.get("14"),
                "divergence_15m": profile_15m.get("divergence"),
            })

        if "macd_momentum_change" in signal_types:
            macd = profile_15m.get("macd", {}) if isinstance(profile_15m.get("macd"), dict) else {}
            evidence.append({
                "type": "macd_momentum_change",
                "macd_hist": macd.get("hist"),
                "macd_hist_slope": macd.get("hist_slope"),
            })

        if "oi_change" in signal_types:
            oi_current = to_float(snapshot.get("oi_change_pct_strategy", snapshot.get("oi_change_pct_15m")))
            oi_ready = bool(snapshot.get("oi_strategy_warmup_ready", snapshot.get("oi_warmup_ready")))
            evidence.append({
                "type": "oi_change",
                "current": oi_current,
                "window_minutes": snapshot.get("derivative_window_minutes", 15),
                "threshold": self.config.oi_change_pct_15m,
                "warmup_ready": oi_ready,
            })

        if "funding_hot" in signal_types:
            evidence.append({
                "type": "funding_hot",
                "current": snapshot["funding_rate"],
                "threshold": self.config.funding_abs_threshold,
            })

        if "funding_fast_change" in signal_types:
            funding_current = to_float(snapshot.get("funding_change_strategy", snapshot.get("funding_change")))
            funding_ready = bool(snapshot.get("funding_strategy_warmup_ready", snapshot.get("funding_warmup_ready")))
            evidence.append({
                "type": "funding_fast_change",
                "current": funding_current,
                "window_minutes": snapshot.get("derivative_window_minutes", 15),
                "threshold": self.config.funding_change_threshold,
                "warmup_ready": funding_ready,
            })

        if "long_short_extreme" in signal_types:
            long_short = snapshot["long_short_ratio"]
            evidence.append({
                "type": "long_short_extreme",
                "current_long_ratio": long_short.get("long_ratio", 0.0),
                "current_short_ratio": long_short.get("short_ratio", 0.0),
                "threshold": self.config.long_short_extreme,
                "available": long_short.get("available", False),
            })

        if "order_book_imbalance" in signal_types:
            evidence.append({
                "type": "order_book_imbalance",
                "current": snapshot.get("order_book", {}).get("imbalance", 0.0),
                "threshold": max(0.35, snapshot.get("dynamic_thresholds", {}).get("book_imbalance_p85", 0.35)),
                "detail": snapshot.get("order_book", {}),
            })

        return evidence

    def _ai_prompt(
        self,
        snapshot: Dict[str, Any],
        signals: List[Dict[str, Any]],
        score: Dict[str, Any],
        trigger: Optional[Dict[str, Any]] = None,
    ) -> str:
        # Prompt：AI 仅从 market_data 自主分析；trigger_context 说明为何调用，不含本地结论。
        trigger = trigger or {}
        profile = self._strategy_profile()
        payload = self._ai_payload(snapshot, signals, score, trigger)
        meta = payload.get("payload_meta", {})
        config = payload.get("analysis_config", {})
        thresholds = config.get("push_thresholds", {})
        included_bars = meta.get("raw_candle_bars") or []
        profile_only_bars = meta.get("profile_only_bars") or []
        bars_text = "、".join(included_bars) or "无"
        profile_only_text = "、".join(profile_only_bars) or "无"
        trigger_ctx = payload.get("trigger_context") if isinstance(payload.get("trigger_context"), dict) else {}
        trigger_reasons_text = "、".join(str(x) for x in (trigger_ctx.get("reasons") or trigger.get("reasons") or [])) or "无"
        signal_count = len(trigger_ctx.get("signals") or signals)
        instruments_text = "、".join(self.instruments) if self.instruments else "BTC-USDT-SWAP"
        horizon = self._effective_forecast_horizon()
        market_ctx = payload.get("market_data", {}).get("market_context", {})
        if not isinstance(market_ctx, dict):
            market_ctx = compact_ai_market_context(snapshot)
        pressure = market_ctx.get("recent_price_pressure", "neutral")
        regime = market_ctx.get("regime", "-")
        return (
            f"你是欧易OKX USDT永续合约多策略分析助手，当前监控 {instruments_text}。"
            "你的输出会写入本地日志，并可能经微信推送给人工复核；"
            "字段 push_recommendation 会参与是否推送。\n\n"
            "【分工（必须遵守）】\n"
            "1. market_data 是唯一事实来源：K线、衍生品、盘口、波动率、动态阈值、数据质量均从中读取。\n"
            "2. trigger_context 仅说明「为何本轮调用你、哪些检测器触发」；"
            "signal_evidence 提供 current vs threshold 核对材料，无本地判决字段；"
            "不得把 trigger_context 或 breakout 字段当作最终操作方向。\n"
            "3. 输入中不包含本地评分、本地方向、structure_forecast 等预计算结论；"
            "你必须独立形成判断，不要假设任何隐藏答案。\n"
            "4. 你的主责是 forward_view：预测未来若干分钟内的最可能演变，并给出后续操作方向、"
            "entry/stop_loss/take_profit、失效条件。\n"
            "5. trend.summary / risk / suggestion 用于简要回顾现状与风险；"
            "direction/entry/SL/TP/confidence 必须与 forward_view 一致。\n\n"
            "【本次触发】\n"
            f"- 级别: {trigger_ctx.get('level', trigger.get('level', 'L0'))}\n"
            f"- 原因: {trigger_reasons_text}\n"
            f"- 触发信号数: {signal_count}\n"
            f"- 市场结构 regime: {regime}\n"
            f"- 主策略: {config.get('strategy_label', profile.get('label', '短线'))} "
            f"({config.get('strategy_mode', 'short')})\n"
            f"- 前瞻默认窗口: {horizon} 分钟（写入 forward_view.horizon_minutes）\n"
            f"- 推送阈值 confidence: 做多 trade≥{thresholds.get('trade_long', self.push_score)}, "
            f"做空 trade≥{thresholds.get('trade_short', self.short_push_score)}, "
            f"watch≥{thresholds.get('watch', self.config.watch_push_score)}, "
            f"spike≥{thresholds.get('spike', self.config.spike_push_score)}\n"
            f"- 原始K线周期: {bars_text}\n"
            f"- 仅 profile 摘要周期: {profile_only_text}（无 OHLC，见 background_profiles）\n"
            f"- 短窗价格压力 recent_price_pressure: {pressure}\n\n"
            "【推送语义】\n"
            "trade=结构级可执行前瞻单；spike=5m/10m 急变短打前瞻；watch=值得盯盘但方向观望。"
            "L3/scalp_spike 优先 spike，勿把急变包装成 trade。\n"
            "若 forward_view 与 recent_price_pressure 或结构突破方向严重冲突，"
            "降低 probability 并优先 watch/none；系统 post-audit 会拦截反向 trade。\n\n"
            "硬性限制：\n"
            "1. 只提供分析和风险提示，不允许表示系统会自动下单。\n"
            "2. 不允许承诺收益，不允许使用稳赚、必涨、必跌等确定性表述。\n"
            "3. 数据不足、预热未完成、周期严重冲突或风险过高时 forward_view.direction=观望。\n"
            "4. long_short_ratio.available=false 时不得编造多空比。\n"
            "5. oi/funding warmup 未完成时，降低 15 分钟变化类指标权重。\n\n"
            f"策略视角：{config.get('strategy_label')}，持仓周期约 {config.get('holding_time', '')}；"
            f"风险偏好 {config.get('risk_preference', 'standard')}。"
            "超短线重 1m/3m/5m+15m 过滤；短线重 5m/15m+1H；"
            "中线重 15m/1H/4H；长线重 4H/1D/1W。"
            "L3/spike 场景偏短线快进快出，不要包装成中线趋势。\n\n"
            "分析步骤：\n"
            "1. 读 payload_meta：candle_order=newest_first（index 0 为最新 K 线）；"
            "有 raw candles 的周期优先读 bar_profiles，仅 profile_only 的周期读 background_profiles。\n"
            "2. 读 trigger_context.signal_evidence，自行比较 current vs threshold 是否成立。\n"
            "3. trend.summary：用 1-3 句话回顾「当前已发生」的多周期结构、动量、量价（回顾，不是预测）。\n"
            "4. 结合 derivatives（OI、费率、多空比及历史）判断资金行为与拥挤风险。\n"
            "5. forward_view：预测 horizon_minutes 内最可能路径；"
            "给出 direction/probability/summary/invalidation；"
            "可选 scenarios 列出 1-2 个备选路径；"
            "entry_plan 给出可执行的 entry/stop_loss/take_profit（做多 SL 低于入场、TP 高于入场；做空相反）。\n"
            "6. 订单簿仅作入场确认，不得单独用盘口决定方向。\n"
            "7. 评估 data_quality（预热、缺失、冲突），再定 risk_level 与 confidence。"
            "confidence 应与 forward_view.probability 大体一致；"
            "仅压线过门槛且无结构确认时，应降为 watch 或 none。\n"
            "8. push_recommendation：默认 none。"
            f"trade=forward_view.direction 为做多/做空且 confidence≥对应 trade 阈值且价位可执行；"
            f"watch=forward_view.direction=观望且 confidence≥{thresholds.get('watch', self.config.watch_push_score)} 且值得提醒；"
            f"spike=L3/急速异动且 forward_view.direction 为做多/做空且 confidence≥{thresholds.get('spike', self.config.spike_push_score)}。"
            "data_quality.overall=数据不足/不可信 时通常 none，最多 watch。\n\n"
            "必须只输出一个合法 JSON 对象，不要 Markdown，不要 JSON 外文字。"
            "必填字段：trend, risk, suggestion, direction, entry, stop_loss, take_profit, "
            "risk_level, confidence, push_recommendation, data_quality, reasons, forward_view；"
            "analysis_note 可选。"
            "trend.timeframes 对无 raw candles 的周期填 profile_only 或 -，不要编造 OHLC。\n"
            "data_quality 含 overall（充足/部分可用/数据不足）与 warnings 数组。\n"
            "forward_view 必填：horizon_minutes, direction, probability, summary, invalidation；"
            "entry_plan 含 entry/stop_loss/take_profit；scenarios 可选数组。\n"
            "输出 JSON 模板（字段必须齐全，数值类型正确）：\n"
            '{"trend":{"summary":"当前5m/15m...","timeframes":{"5m":"up"},"conflict":"..."},'
            '"risk":"...","suggestion":"...","direction":"做多","entry":"68100","'
            '"stop_loss":"67950","take_profit":"68400","risk_level":"中","confidence":68,'
            '"push_recommendation":"trade","data_quality":{"overall":"部分可用","warnings":[]},'
            '"reasons":["..."],'
            '"forward_view":{"horizon_minutes":15,"direction":"做多","probability":68,'
            '"summary":"未来15m内更可能延续...","invalidation":"跌破67950",'
            '"entry_plan":{"entry":"68100","stop_loss":"67950","take_profit":"68400"},'
            '"scenarios":[{"label":"延续","direction":"做多","probability":68}]},'
            '"analysis_note":"..."}\n\n'
            f"输入数据：{json.dumps(payload, ensure_ascii=False)}"
        )

    def _local_analysis(
        self,
        snapshot: Dict[str, Any],
        signals: List[Dict[str, Any]],
        score: Dict[str, Any],
    ) -> Dict[str, Any]:
        # 本地规则分析是AI不可用时的兜底，也用于未触发信号时的简短输出。
        reasons = [item["desc"] for item in signals] or ["未触发强信号，按规则保持观察。"]
        content = {
            "trend": {
                "legacy": score["trends"],
                "market_regime": score.get("market_regime"),
                "bias": score.get("bias"),
                "profiles": snapshot.get("trend_profiles", {}),
            },
            "risk": f"风险等级：{score['risk_level']}",
            "suggestion": "仅供观察，不构成投资建议。",
            "direction": score["direction"],
            "raw_direction": score.get("raw_direction"),
            "final_direction": score.get("final_direction"),
            "entry": score["entry"],
            "stop_loss": score["stop_loss"],
            "take_profit": score["take_profit"],
            "risk_level": score["risk_level"],
            "raw_total_score": score.get("raw_total_score"),
            "final_trade_score": score.get("final_trade_score"),
            "risk_control_score": score.get("risk_control_score"),
            "entry_quality_score": score.get("entry_quality_score"),
            "entry_quality": score.get("entry_plan", {}).get("quality"),
            "invalidation": score.get("entry_plan", {}).get("invalidation"),
            "wait_for": score.get("entry_plan", {}).get("wait_for", []),
            "layer_scores": score.get("layer_scores", {}),
            "strategy_template": snapshot.get("market_context", {}).get("strategy_template"),
            "market_context": snapshot.get("market_context", {}),
            "signal_tracking": snapshot.get("signal_tracking", {}),
            "strategy_mode": score.get("strategy_mode"),
            "strategy_label": score.get("strategy_label"),
            "risk_preference": score.get("risk_preference"),
            "ai_output_style": score.get("ai_output_style"),
            "strategy_views": score.get("strategy_views", {}),
            "selected_strategy_view": score.get("selected_strategy_view", {}),
            "reasons": reasons,
        }
        return {"provider": "local-rule", "content": content}

    def _resolve_push_view(
        self,
        final_decision: Dict[str, Any],
        analysis: Optional[Dict[str, Any]],
        local_score: Dict[str, Any],
    ) -> Dict[str, Any]:
        analysis = analysis or {}
        parsed = analysis.get("parsed") if isinstance(analysis.get("parsed"), dict) else {}
        trend = parsed.get("trend") if isinstance(parsed.get("trend"), dict) else {}
        forward = parsed.get("forward_view") if isinstance(parsed.get("forward_view"), dict) else {}
        rule_audit = final_decision.get("rule_audit") if isinstance(final_decision.get("rule_audit"), dict) else {}
        reasons = final_decision.get("reasons") if isinstance(final_decision.get("reasons"), list) else []
        screening = final_decision.get("local_screening") if isinstance(final_decision.get("local_screening"), dict) else {}

        return {
            "source": final_decision.get("decision_source", analysis.get("provider", "unknown")),
            "ai_valid": bool(analysis.get("valid_json")),
            "direction": display_push_value(final_decision.get("direction"), "观望"),
            "rule_direction": screening.get("local_bias") or local_score.get("direction", "观望"),
            "forward_horizon_minutes": forward.get("horizon_minutes") or final_decision.get("forward_view", {}).get("horizon_minutes"),
            "forward_probability": forward.get("probability") or final_decision.get("forward_view", {}).get("probability"),
            "forward_summary": clip_push_text(
                forward.get("summary") or final_decision.get("forward_view", {}).get("summary"),
                240,
            ),
            "forward_invalidation": clip_push_text(
                forward.get("invalidation") or final_decision.get("forward_view", {}).get("invalidation"),
                120,
            ),
            "local_screening_summary": clip_push_text(screening.get("summary"), 180),
            "entry": display_push_value(final_decision.get("entry"), "-"),
            "stop_loss": display_push_value(final_decision.get("stop_loss"), "-"),
            "take_profit": display_push_value(final_decision.get("take_profit"), "-"),
            "risk_level": display_push_value(final_decision.get("risk_level"), "中"),
            "suggestion": clip_push_text(final_decision.get("summary"), 240),
            "risk": clip_push_text(parsed.get("risk"), 260),
            "score_comment": clip_push_text(
                final_decision.get("score_comment", parsed_analysis_note(parsed)),
                180,
            ),
            "trend_summary": clip_push_text(trend.get("summary"), 120),
            "trend_conflict": clip_push_text(trend.get("conflict"), 120),
            "rule_audit_overall": clip_push_text(rule_audit.get("overall"), 40),
            "rule_audit_warnings": [
                clip_push_text(item, 80)
                for item in (rule_audit.get("warnings") or [])[:2]
                if clip_push_text(item, 80)
            ],
            "reasons": [clip_push_text(item, 100) for item in reasons[:3] if clip_push_text(item, 100)],
            "confidence": final_decision.get("confidence", 0),
            "push_recommendation": final_decision.get("push_recommendation", "none"),
        }

    def _resolve_push_analysis(
        self,
        analysis: Dict[str, Any],
        score: Dict[str, Any],
    ) -> Dict[str, Any]:
        # 兼容旧调用：无 final_decision 时从 analysis/score 推断。
        final_decision = {
            "direction": score.get("direction", "观望"),
            "entry": score.get("entry", "-"),
            "stop_loss": score.get("stop_loss", "-"),
            "take_profit": score.get("take_profit", "-"),
            "risk_level": score.get("risk_level", "中"),
            "summary": "",
            "reasons": [],
            "rule_audit": {},
            "decision_source": analysis.get("provider", "unknown"),
            "confidence": score.get("final_trade_score", score.get("raw_total_score", 0)),
            "push_recommendation": self._local_push_recommendation(score, []),
        }
        return self._resolve_push_view(final_decision, analysis, score)

    def _signal_labels(self, signals: List[Dict[str, Any]], limit: int = 4) -> List[str]:
        labels = []
        for item in signals:
            signal_type = str(item.get("type", "")).strip()
            if not signal_type:
                continue
            labels.append(SIGNAL_TYPE_LABELS.get(signal_type, signal_type))
        return labels[:limit]

    def _label_risk_preference(self, value: Any) -> str:
        key = str(value or "standard").strip().lower()
        return RISK_PREFERENCE_LABELS.get(key, key or "标准")

    def _label_trigger_reasons(self, reasons: List[Any]) -> str:
        labels = []
        for item in reasons:
            text = str(item or "").strip()
            if not text:
                continue
            labels.append(TRIGGER_REASON_LABELS.get(text, text))
        return "、".join(labels) if labels else "-"

    def _label_decision_source(self, final_decision: Dict[str, Any], analysis: Dict[str, Any]) -> str:
        source = str(final_decision.get("decision_source", analysis.get("provider", "local")) or "local")
        return DECISION_SOURCE_LABELS.get(source, source)

    def _ai_push_was_invoked(self, analysis: Dict[str, Any], trigger: Dict[str, Any]) -> bool:
        if trigger.get("ai_invoked"):
            return True
        if not analysis:
            return False
        if str(analysis.get("content") or "").strip():
            return True
        return analysis.get("provider") not in (None, "", "local-rule")

    def _format_full_ai_push_lines(self, analysis: Dict[str, Any]) -> List[str]:
        lines: List[str] = []
        if not analysis:
            lines.append("- 本轮未返回 AI 内容")
            return lines

        provider = display_push_value(analysis.get("provider"), "-")
        model = display_push_value(analysis.get("model"), "-")
        if provider != "-" or model != "-":
            lines.append(f"- 接口：{provider} | 模型：{model}")

        valid = bool(analysis.get("valid_json"))
        lines.append(f"- JSON 校验：{'通过' if valid else '未通过'}")

        errors = analysis.get("validation_errors") if isinstance(analysis.get("validation_errors"), list) else []
        if errors:
            lines.append("- 校验问题：" + "；".join(str(item) for item in errors))

        if analysis.get("error"):
            lines.append(f"- 调用错误：{analysis.get('error')}")

        parsed = analysis.get("parsed") if isinstance(analysis.get("parsed"), dict) else {}
        if parsed:
            trend = parsed.get("trend") if isinstance(parsed.get("trend"), dict) else {}
            if trend.get("summary"):
                lines.append(f"- 趋势：{trend.get('summary')}")
            if trend.get("conflict"):
                lines.append(f"- 周期冲突：{trend.get('conflict')}")
            timeframes = trend.get("timeframes") if isinstance(trend.get("timeframes"), dict) else {}
            if timeframes:
                lines.append("- 多周期：")
                for timeframe, value in timeframes.items():
                    text = str(value or "").strip()
                    if text:
                        lines.append(f"  · {timeframe}：{text}")

            for key, label in (
                ("direction", "方向"),
                ("confidence", "置信度"),
                ("risk_level", "风险等级"),
                ("push_recommendation", "推送建议"),
                ("entry", "入场"),
                ("stop_loss", "止损"),
                ("take_profit", "止盈"),
            ):
                value = parsed.get(key)
                if value not in (None, "", "-"):
                    lines.append(f"- {label}：{value}")

            for key, label in (("risk", "风险分析"), ("suggestion", "交易建议")):
                value = str(parsed.get(key) or "").strip()
                if value:
                    lines.append(f"- {label}：{value}")

            note = parsed_analysis_note(parsed)
            if note:
                lines.append(f"- 分析说明：{note}")

            audit = parsed_data_quality(parsed)
            if audit:
                lines.append("- 数据质量：")
                if audit.get("overall"):
                    lines.append(f"  · 总体：{audit.get('overall')}")
                for warning in audit.get("warnings") or []:
                    text = str(warning or "").strip()
                    if text:
                        lines.append(f"  · 警告：{text}")

            reasons = parsed.get("reasons") if isinstance(parsed.get("reasons"), list) else []
            if reasons:
                lines.append("- 理由：")
                for index, reason in enumerate(reasons, start=1):
                    text = str(reason or "").strip()
                    if text:
                        lines.append(f"  {index}. {text}")

        raw = str(analysis.get("content") or "").strip()
        if raw:
            lines.extend(["", "**AI 原始输出**", "```", raw, "```"])

        return lines

    def _join_wechat_desp(self, lines: List[str], max_chars: int = WECHAT_PUSH_MAX_DESP) -> str:
        text = "\n".join(lines)
        if len(text) <= max_chars:
            return text
        notice = "\n\n（正文过长已截断，完整 AI 输出见 JSON 分析日志 analysis 字段）"
        keep = max(0, max_chars - len(notice))
        return text[:keep] + notice

    def _build_wechat_push_content(
        self,
        snapshot: Dict[str, Any],
        signals: List[Dict[str, Any]],
        final_decision: Dict[str, Any],
        analysis: Dict[str, Any],
        push_kind: str = "trade",
        local_score: Optional[Dict[str, Any]] = None,
        trigger: Optional[Dict[str, Any]] = None,
    ) -> Tuple[str, str]:
        local_score = local_score or {}
        trigger = trigger or {}
        view = self._resolve_push_view(final_decision, analysis, local_score)
        inst_id = snapshot["inst_id"]
        symbol = symbol_ccy(inst_id)
        price = snapshot["price"]
        signal_labels = self._signal_labels(signals)
        signal_text = "、".join(signal_labels) or "规则评分达标"
        local_raw = local_score.get("raw_total_score", local_score.get("total_score", "-"))
        local_trade = local_score.get("final_trade_score", local_score.get("total_score", "-"))
        final_confidence = view.get("confidence", final_decision.get("confidence", "-"))
        strategy_label = final_decision.get("strategy_label", local_score.get("strategy_label", "-"))
        risk_preference = self._label_risk_preference(
            final_decision.get("risk_preference", local_score.get("risk_preference", "standard"))
        )
        decision_source_label = self._label_decision_source(final_decision, analysis)
        push_kind_label = PUSH_KIND_LABELS.get(push_kind, push_kind)
        trigger_level = final_decision.get("trigger_level", trigger.get("level", "-"))
        trigger_reasons = self._label_trigger_reasons(trigger.get("reasons") if isinstance(trigger.get("reasons"), list) else [])
        market_regime = final_decision.get("market_regime", local_score.get("market_regime", "unknown"))
        market_bias = local_score.get("bias", "neutral")

        title_parts = [
            symbol,
            view["direction"],
            f"险{view['risk_level']}",
            f"{final_confidence}分",
        ]
        if signal_labels:
            title_parts.append(signal_labels[0])
        title = " ".join(part for part in title_parts if part and part != "-")
        if push_kind == "trade":
            title = f"[结构单] {title}"
        elif push_kind == "watch":
            title = f"[观察] {title}"
        elif push_kind == "spike":
            title = f"[急变] {title}"
        elif push_kind == "forecast":
            title = f"[演变] {title}"

        forecast = local_score.get("structure_forecast") if isinstance(local_score.get("structure_forecast"), dict) else {}
        lines = [
            f"## {inst_id} · {snapshot.get('time', now_text())}",
            "",
            "### 一、当前配置",
            f"- 主策略：{strategy_label} | 确认严格度：{risk_preference}",
            f"- 本次推送：{push_kind_label} | 门槛 做多 {self.push_score} · 做空 {self.short_push_score} · "
            f"watch {self.config.watch_push_score} · spike {self.config.spike_push_score} · "
            f"forecast {self.config.forecast_push_score}",
            f"- AI 分析：{'已启用' if self.ai_enabled else '未启用'} | 轮询：{self.interval}秒",
            "",
            "### 二、触发原因",
            f"- 触发等级：{trigger_level} | 触发条件：{trigger_reasons}",
            f"- 检测信号：{signal_text}",
            f"- 市场状态：{market_regime} / {market_bias}",
        ]
        signal_details = [
            clip_push_text(item.get("desc"), 100)
            for item in signals[:3]
            if clip_push_text(item.get("desc"), 100)
        ]
        if signal_details:
            lines.append("- 信号说明：" + "；".join(signal_details))

        local_direction = display_push_value(local_score.get("final_direction", local_score.get("direction")), "观望")

        if push_kind == "forecast" and forecast.get("active"):
            scenario_label = FORECAST_SCENARIO_LABELS.get(forecast.get("scenario", ""), forecast.get("scenario", "-"))
            lines.extend(
                [
                    "",
                    "### 二、结构演变预测（前瞻轨，非已确认结构）",
                    f"- 预测方向：{forecast.get('direction', '观望')} | 概率分：{forecast.get('probability', 0)}",
                    f"- 时间窗：约 {forecast.get('horizon_minutes', 15)} 分钟 | 阶段：{forecast.get('phase', '-')}",
                    f"- 场景：{scenario_label}（{forecast.get('from_state', '-')} → {forecast.get('to_state', '-')}）",
                    f"- 说明：{clip_push_text(forecast.get('summary'), 200)}",
                    f"- 失效条件：{clip_push_text(forecast.get('invalidation'), 120)}",
                ]
            )
            evidence = forecast.get("evidence") if isinstance(forecast.get("evidence"), list) else []
            if evidence:
                lines.append("- 依据：" + "；".join(str(item) for item in evidence[:5]))
            lines.extend(
                [
                    "",
                    "### 三、与确认轨关系",
                    f"- 当前本地确认方向：{local_direction}",
                    "- 演变推送仅供提前盯盘；若后续出现 spike/trade 同向推送，以确认轨为准。",
                    "- 不构成自动交易指令。",
                    "",
                    "仅供观察，不构成投资建议。",
                ]
            )
            return title[:120], self._join_wechat_desp(lines)

        ai_invoked = self._ai_push_was_invoked(analysis, trigger)
        lines.extend(["", "### 三、本地筛查与 AI 前瞻", f"- 来源：{decision_source_label}"])
        if view.get("local_screening_summary") not in (None, "", "-"):
            lines.append(f"- 本地回顾：{view['local_screening_summary']}")
        if ai_invoked:
            forward_bits = []
            if view.get("forward_horizon_minutes") not in (None, "", "-"):
                forward_bits.append(f"{view['forward_horizon_minutes']}m")
            if view.get("forward_probability") not in (None, "", "-"):
                forward_bits.append(f"P={view['forward_probability']}")
            prefix = f"AI前瞻（{' · '.join(forward_bits)}）" if forward_bits else "AI前瞻"
            if view.get("forward_summary") not in (None, "", "-"):
                lines.append(f"- {prefix}：{view['forward_summary']}")
            if view.get("forward_invalidation") not in (None, "", "-"):
                lines.append(f"- 失效条件：{view['forward_invalidation']}")
            lines.extend(["", "#### AI 完整分析", *self._format_full_ai_push_lines(analysis), "", "#### 本地结构偏向"])

        lines.append(
            f"- 本地结构偏向：{view['rule_direction']} | 观察/交易分 {local_raw}/{local_trade}"
        )
        if view["direction"] != view["rule_direction"]:
            lines.append(f"- 本地原始方向 {view['rule_direction']}（与最终结论不一致，以下以最终结论为准）")

        local_summary = clip_push_text(local_score.get("trade_action_level"), 120)
        if local_summary != "-":
            lines.append(f"- 本地动作：{local_summary}")

        selected_view = local_score.get("selected_strategy_view", {})
        if selected_view.get("summary"):
            lines.append(f"- 主策略视角：{clip_push_text(selected_view.get('summary'), 140)}")

        scalp_view = local_score.get("strategy_views", {}).get("scalp", {})
        if scalp_view.get("action_level") in ("急速异动", "可短打"):
            lines.append(
                f"- 超短线：{scalp_view.get('direction')} / {scalp_view.get('action_level')} / "
                f"{scalp_view.get('score')}分；{clip_push_text(scalp_view.get('summary'), 120)}"
            )
        if forecast.get("active") and push_kind != "forecast":
            scenario_label = FORECAST_SCENARIO_LABELS.get(forecast.get("scenario", ""), forecast.get("scenario", "-"))
            lines.append(
                f"- 结构演变：{forecast.get('direction')} P={forecast.get('probability', 0)} "
                f"({scenario_label})；{clip_push_text(forecast.get('summary'), 100)}"
            )
        post_audit = final_decision.get("post_audit") if isinstance(final_decision.get("post_audit"), dict) else {}
        if post_audit.get("action") not in (None, "", "kept"):
            audit_reasons = post_audit.get("reasons") or []
            audit_text = "、".join(str(item) for item in audit_reasons) or "-"
            lines.append(
                f"- 推送复核：{post_audit.get('action', '-')}（{audit_text}）"
            )

        if not ai_invoked and view["reasons"]:
            lines.append("- 本地依据：")
            for index, reason in enumerate(view["reasons"], start=1):
                lines.append(f"  {index}. {reason}")

        lines.append("- 交易计划（最终结论）：")
        if view["direction"] == "观望" and view["entry"] == "-" and view["stop_loss"] == "-" and view["take_profit"] == "-":
            lines.append("  - 当前建议观望，暂不给出入场/止损/止盈")
        else:
            lines.append(f"  - 入场：{view['entry']}")
            lines.append(f"  - 止损：{view['stop_loss']}")
            lines.append(f"  - 止盈：{view['take_profit']}")

        if view["suggestion"] != "-" and not ai_invoked:
            lines.append(f"- 执行建议：{view['suggestion']}")

        lines.extend([
            "",
            "### 四、最终结论",
            f"- 方向：{view['direction']} | 风险：{view['risk_level']} | 置信度：{final_confidence}",
            f"- 现价：{price} | 推送：{view.get('push_recommendation', push_kind)}",
            f"- 决策来源：{decision_source_label} | 触发等级：{trigger_level}",
        ])
        if not ai_invoked and view["trend_summary"] != "-":
            lines.append(f"- 趋势：{view['trend_summary']}")
        if not ai_invoked and view["trend_conflict"] not in ("-", "none", "无", "否"):
            lines.append(f"- 周期冲突：{view['trend_conflict']}")

        audit_bits = []
        if view["rule_audit_overall"] != "-":
            audit_bits.append(view["rule_audit_overall"])
        audit_bits.extend(view["rule_audit_warnings"])
        if audit_bits and not ai_invoked:
            lines.append(f"- 数据质量：{'；'.join(audit_bits)}")
        elif view["score_comment"] != "-" and not ai_invoked:
            lines.append(f"- 分析说明：{view['score_comment']}")

        lines.extend([
            "",
            "仅供观察，不构成投资建议。",
        ])
        return title[:120], self._join_wechat_desp(lines)

    def _format_push_message(
        self,
        snapshot: Dict[str, Any],
        signals: List[Dict[str, Any]],
        final_decision: Dict[str, Any],
        analysis: Dict[str, Any],
        push_kind: str = "trade",
        local_score: Optional[Dict[str, Any]] = None,
        trigger: Optional[Dict[str, Any]] = None,
    ) -> str:
        title, desp = self._build_wechat_push_content(
            snapshot,
            signals,
            final_decision,
            analysis,
            push_kind,
            local_score=local_score,
            trigger=trigger,
        )
        return f"[OKX AI短线助手][{push_kind}] {title}\n\n{desp}"

    def _in_push_cooldown(self, push_key: str, push_kind: str = "trade") -> bool:
        last_at = self.last_push_at.get(push_key, 0.0)
        return self._now_ts() - last_at < self._push_cooldown_seconds(push_kind)

    def _push_wechat(
        self,
        send_key: str,
        snapshot: Dict[str, Any],
        signals: List[Dict[str, Any]],
        final_decision: Dict[str, Any],
        analysis: Dict[str, Any],
        push_kind: str = "trade",
        local_score: Optional[Dict[str, Any]] = None,
        trigger: Optional[Dict[str, Any]] = None,
    ) -> None:
        title, desp = self._build_wechat_push_content(
            snapshot,
            signals,
            final_decision,
            analysis,
            push_kind,
            local_score=local_score,
            trigger=trigger,
        )
        try:
            http_post_json(
                f"https://sctapi.ftqq.com/{send_key}.send",
                {"title": title, "desp": desp},
                self.runtime_config.retry_times,
                self.runtime_config.retry_backoff,
            )
            self._log_push_event(snapshot, signals, final_decision, push_kind, "sent")
        except Exception as exc:
            self._log_push_event(snapshot, signals, final_decision, push_kind, f"failed: {exc}")

    def _rotate_log_if_needed(self) -> None:
        log_path = self.replay_log_file if self.replay_mode else LOG_FILE
        rotate_analysis_log_if_needed(
            log_path,
            self.runtime_config.log_max_bytes,
            self.runtime_config.log_total_max_bytes,
        )

    def _log_console_summary(
        self,
        snapshot: Dict[str, Any],
        signals: List[Dict[str, Any]],
        final_decision: Dict[str, Any],
        analysis: Optional[Dict[str, Any]],
        trigger: Dict[str, Any],
        score: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not self.replay_mode and not self.runtime_config.analysis_log_enabled:
            return
        if not signals and trigger.get("level") == "L0":
            return

        signal_text = ", ".join(item.get("type", item.get("desc", "?")) for item in signals) or "none"
        source_prefix = decision_source_prefix(final_decision)
        trigger_text = format_ai_call_status(trigger, self.ai_enabled)

        console_info(
            f"[{snapshot['time']}] {snapshot['inst_id']} price={snapshot['price']} "
            f"{source_prefix} {trigger_text} signals={signal_text}"
        )

        decision_source = str(final_decision.get("decision_source", "") or "")
        if decision_source == "ai":
            for line in format_ai_analysis_lines(analysis or {}, final_decision):
                console_info(line)
        elif decision_source == "local_fallback":
            for line in format_ai_analysis_lines(analysis or {}, final_decision):
                console_info(line)
            console_info(format_local_decision_line(final_decision))
        else:
            console_info(format_local_decision_line(final_decision))

        forecast = (score or {}).get("structure_forecast") if isinstance((score or {}).get("structure_forecast"), dict) else {}
        if forecast.get("active"):
            scenario = FORECAST_SCENARIO_LABELS.get(forecast.get("scenario", ""), forecast.get("scenario", "-"))
            console_info(
                f"[演变] {snapshot['inst_id']} {forecast.get('direction', '观望')} "
                f"P={forecast.get('probability', 0)} {scenario} "
                f"horizon={forecast.get('horizon_minutes', 15)}m"
            )

    def _log_push_event(
        self,
        snapshot: Dict[str, Any],
        signals: List[Dict[str, Any]],
        final_decision: Dict[str, Any],
        push_kind: str,
        status: str,
    ) -> None:
        signal_text = ",".join(item["type"] for item in signals)
        console_info(
            f"[{snapshot['time']}] {decision_source_prefix(final_decision)} push [{push_kind}] "
            f"{snapshot['inst_id']} dir={final_decision.get('direction', '-')} "
            f"conf={final_decision.get('confidence', '-')} signals={signal_text} -> {status}"
        )


def short_count_greater(trends: Dict[str, str]) -> bool:
    # 判断多周期中下跌周期是否多于上涨周期。
    return sum(1 for item in trends.values() if item == "down") > sum(1 for item in trends.values() if item == "up")


def _assistant_from_user_config(config: Dict[str, Any]) -> OkxAiShortTermAssistant:
    inst_ids = config.get("inst_ids") or list(PRESET_INSTRUMENTS)
    if isinstance(inst_ids, str):
        inst_ids = [inst_ids]
    try:
        instruments = validate_instruments(order_configured_instruments(inst_ids))
    except ValueError:
        instruments = list(PRESET_INSTRUMENTS)
    push_score = int(config.get("push_score", DEFAULT_PUSH_SCORE))
    short_push_score = int(config.get("short_push_score", push_score))
    watch_push_score = int(config.get("watch_push_score", 65))
    spike_push_score = int(config.get("spike_push_score", 62))
    return OkxAiShortTermAssistant(
        instruments=instruments,
        interval=max(int(config.get("interval", DEFAULT_INTERVAL_SECONDS)), 1),
        flag="0",
        ai_enabled=bool(config.get("ai_enabled", True)),
        push_enabled=True,
        push_score=push_score,
        short_push_score=short_push_score,
        dry_run_ai=bool(config.get("dry_run_ai", False)),
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
            watch_push_score=watch_push_score,
            spike_push_score=spike_push_score,
            ai_conflict_guard=bool(config.get("ai_conflict_guard", True)),
            l3_local_spike_push=bool(config.get("l3_local_spike_push", False)),
            l2_require_volume_or_structure=bool(config.get("l2_require_volume_or_structure", True)),
            signal_forecast_enabled=bool(config.get("signal_forecast_enabled", True)),
            forecast_push_score=int(config.get("forecast_push_score", 58)),
            forecast_horizon_minutes=int(config.get("forecast_horizon_minutes", 15)),
            calibration_enabled=bool(config.get("calibration_enabled", True)),
            calibration_min_samples=max(3, int(config.get("calibration_min_samples", 8))),
            calibration_blend_weight=max(0.0, min(1.0, float(config.get("calibration_blend_weight", 0.65)))),
            calibration_disable_below_hit_rate=max(
                0.1, min(0.9, float(config.get("calibration_disable_below_hit_rate", 0.38)))
            ),
            calibration_save_interval_seconds=max(15, int(config.get("calibration_save_interval_seconds", 60))),
            paper_follow_ai_only=bool(config.get("paper_follow_ai_only", True)),
            paper_fee_bps=max(0.0, float(config.get("paper_fee_bps", 5.0))),
            forward_require_forecast_alignment=bool(config.get("forward_require_forecast_alignment", True)),
            replay_ai_cache_enabled=bool(config.get("replay_ai_cache_enabled", True)),
        ),
        runtime_config=RuntimeConfig(
            retry_times=int(config.get("retry_times", DEFAULT_RETRY_TIMES)),
            retry_backoff=float(config.get("retry_backoff", DEFAULT_RETRY_BACKOFF_SECONDS)),
            push_cooldown_seconds=int(config.get("push_cooldown_seconds", DEFAULT_PUSH_COOLDOWN_SECONDS)),
            spike_push_cooldown_seconds=int(
                config.get("spike_push_cooldown_seconds", DEFAULT_SPIKE_PUSH_COOLDOWN_SECONDS)
            ),
            watch_push_cooldown_seconds=int(
                config.get("watch_push_cooldown_seconds", DEFAULT_WATCH_PUSH_COOLDOWN_SECONDS)
            ),
            reverse_trade_cooldown_seconds=int(
                config.get("reverse_trade_cooldown_seconds", DEFAULT_REVERSE_TRADE_COOLDOWN_SECONDS)
            ),
            forecast_push_cooldown_seconds=int(
                config.get("forecast_push_cooldown_seconds", DEFAULT_FORECAST_PUSH_COOLDOWN_SECONDS)
            ),
            log_max_bytes=max(int(config.get("log_max_bytes", DEFAULT_LOG_MAX_BYTES)), MIN_LOG_MAX_BYTES),
            log_total_max_bytes=max(
                int(config.get("log_total_max_bytes", DEFAULT_LOG_TOTAL_MAX_BYTES)),
                max(int(config.get("log_max_bytes", DEFAULT_LOG_MAX_BYTES)), MIN_LOG_MAX_BYTES),
            ),
            analysis_log_enabled=bool(config.get("analysis_log_enabled", True)),
        ),
    )


def build_wechat_push_format_preview(config: Optional[Dict[str, Any]] = None) -> Tuple[str, str]:
    """Assemble a sample WeChat push using mock AI JSON fields for layout preview."""
    config = dict(config or {})
    preview_config = {**config, "ai_enabled": True}
    assistant = _assistant_from_user_config(preview_config)
    strategy_mode = str(config.get("strategy_mode", "short"))
    strategy_label = STRATEGY_PROFILES.get(strategy_mode, STRATEGY_PROFILES["short"])["label"]
    risk_preference = str(config.get("risk_preference", "standard"))
    inst_ids = order_configured_instruments(config.get("inst_ids") or ["BTC-USDT-SWAP"])
    inst_id = inst_ids[0] if inst_ids else "BTC-USDT-SWAP"
    preview_time = now_text()

    mock_parsed: Dict[str, Any] = {
        "trend": {
            "summary": "【示例】5m/15m 结构偏多，1H 仍在箱体内震荡，尚未形成单边延伸。",
            "timeframes": {
                "1m": "短线抬升，回踩区间上沿",
                "3m": "动能修复，未出现背离",
                "5m": "profile=up，脉冲后缩量整理",
                "15m": "结构由 mixed 转 up",
                "1H": "震荡偏多，阻力尚未突破",
                "4H": "背景中性，不构成强趋势过滤",
            },
            "conflict": "1H 与 15m 方向尚未完全共振",
        },
        "risk": "【示例】资金费率略偏正、多空比均衡；波动率偏高，追价风险大于回踩确认。",
        "suggestion": "【示例】可轻仓试多，优先等回踩入场区；未放量突破前不建议追涨。",
        "direction": "做多",
        "entry": "65750 - 65780",
        "stop_loss": "65710",
        "take_profit": "65840 / 65900",
        "risk_level": "中",
        "confidence": 78,
        "push_recommendation": "trade",
        "analysis_note": "【示例】基于 5m/15m 结构与盘口自主推导价位；15m 刚转多需防假突破。",
        "data_quality": {
            "overall": "部分可用",
            "warnings": [
                "【示例】15m 趋势刚转多，需防假突破",
                "【示例】费率预热已完成，但 15m 变化幅度一般",
            ],
        },
        "reasons": [
            "【示例】top20 盘口 imbalance 0.72，买盘承接明显",
            "【示例】5m+15m profile 同步转多",
            "【示例】1m 回踩区间上沿获支撑，量价未明显背离",
        ],
        "forward_view": {
            "horizon_minutes": 15,
            "direction": "做多",
            "probability": 78,
            "summary": "【示例】未来15m 更可能延续 15m 偏多，优先回踩试多",
            "invalidation": "【示例】跌破 65710 或 5m 转 mixed/down",
            "entry_plan": {
                "entry": "65750 - 65780",
                "stop_loss": "65710",
                "take_profit": "65840 / 65900",
            },
            "scenarios": [
                {"label": "延续", "direction": "做多", "probability": 78},
                {"label": "假突破", "direction": "观望", "probability": 22},
            ],
        },
    }
    mock_raw = json.dumps(mock_parsed, ensure_ascii=False, indent=2)
    analysis: Dict[str, Any] = {
        "provider": "openai",
        "model": "preview-model",
        "valid_json": True,
        "validation_errors": [],
        "content": mock_raw,
        "parsed": mock_parsed,
    }
    signals = [
        {
            "type": "order_book_imbalance",
            "desc": "【示例】top20 book imbalance 0.72 (bid_support)",
        },
        {
            "type": "volume_spike",
            "desc": "【示例】1m closed volume 2.3x median",
        },
    ]
    local_score: Dict[str, Any] = {
        "direction": "观望",
        "final_direction": "做多",
        "raw_direction": "做多",
        "raw_total_score": 68,
        "final_trade_score": 74,
        "risk_level": "中",
        "market_regime": "high_volatility",
        "bias": "bullish",
        "strategy_label": strategy_label,
        "risk_preference": risk_preference,
        "trade_action_level": "【示例】等待确认 → 可轻仓试多",
        "entry": "65750 - 65780",
        "stop_loss": "65710",
        "take_profit": "65840 / 65900",
        "selected_strategy_view": {
            "summary": "【示例】5m+15m 同向偏多，20m 延伸尚未确认",
        },
        "strategy_views": {
            "scalp": {
                "direction": "做多",
                "action_level": "可短打",
                "score": 71,
                "summary": "【示例】5m 脉冲 0.11%，适合 1-15 分钟波动捕捉",
            }
        },
    }
    final_decision: Dict[str, Any] = {
        "direction": "做多",
        "confidence": 78,
        "push_recommendation": "trade",
        "entry": "65750 - 65780",
        "stop_loss": "65710",
        "take_profit": "65840 / 65900",
        "risk_level": "中",
        "summary": mock_parsed["suggestion"],
        "reasons": mock_parsed["reasons"],
        "rule_audit": mock_parsed["data_quality"],
        "decision_source": "ai",
        "ai_called": True,
        "trigger_level": "L2",
        "market_regime": "high_volatility",
        "strategy_label": strategy_label,
        "risk_preference": risk_preference,
        "score_comment": parsed_analysis_note(mock_parsed),
    }
    trigger = {
        "level": "L2",
        "ai_invoked": True,
        "reasons": ["trade_signal", "multi_signal"],
    }
    snapshot = {
        "inst_id": inst_id,
        "time": preview_time,
        "price": 65766.9,
    }
    return assistant._build_wechat_push_content(
        snapshot,
        signals,
        final_decision,
        analysis,
        "trade",
        local_score,
        trigger,
    )


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


def order_configured_instruments(inst_ids: Any) -> List[str]:
    normalized = parse_inst_id_tokens(inst_ids)
    if not normalized:
        return []
    selected = set(normalized)
    ordered = [inst for inst in PRESET_INSTRUMENTS if inst in selected]
    for inst_id in normalized:
        if inst_id not in ordered:
            ordered.append(inst_id)
    return ordered


def is_valid_inst_id_format(inst_id: str) -> bool:
    return bool(INST_ID_PATTERN.match(normalize_inst_id(inst_id)))


def okx_swap_instrument_exists(inst_id: str) -> bool:
    inst_id = normalize_inst_id(inst_id)
    if not is_valid_inst_id_format(inst_id):
        return False
    query = urllib.parse.urlencode({"instType": "SWAP", "instId": inst_id})
    request = urllib.request.Request(
        f"{OKX_BASE_URL}/api/v5/public/instruments?{query}",
        headers={"Accept": "application/json", "User-Agent": "okx-ai-assistant/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            payload = json.loads(response.read().decode("utf-8", errors="ignore"))
        if str(payload.get("code")) != "0":
            return False
        rows = payload.get("data") or []
        return any(str(row.get("instId", "")).upper() == inst_id for row in rows if isinstance(row, dict))
    except (OSError, urllib.error.URLError, json.JSONDecodeError, TypeError, ValueError):
        return False


def validate_instruments(instruments: List[str]) -> List[str]:
    ordered = order_configured_instruments(instruments)
    if not ordered:
        return list(PRESET_INSTRUMENTS)
    invalid_format = [inst for inst in ordered if not is_valid_inst_id_format(inst)]
    if invalid_format:
        raise ValueError(f"Invalid instrument format: {', '.join(invalid_format)}")
    invalid_okx = [inst for inst in ordered if not okx_swap_instrument_exists(inst)]
    if invalid_okx:
        raise ValueError(f"OKX swap instrument not found: {', '.join(invalid_okx)}")
    return ordered


def parse_instruments(value: str) -> List[str]:
    try:
        instruments = parse_inst_id_tokens(value)
        if not instruments:
            return list(PRESET_INSTRUMENTS)
        return validate_instruments(instruments)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def parse_args() -> argparse.Namespace:
    # 命令行参数 > 环境变量 > 程序默认。
    parser = argparse.ArgumentParser(description="OKX AI short-term trading assistant V1")
    parser.add_argument(
        "--inst-ids",
        type=parse_instruments,
        default=parse_instruments(os.getenv("OKX_INST_IDS", ",".join(PRESET_INSTRUMENTS))),
        help="Comma-separated OKX USDT swap instIds, e.g. BTC-USDT-SWAP,SOL-USDT-SWAP",
    )
    parser.add_argument("--interval", type=int, default=int(os.getenv("OKX_INTERVAL", str(DEFAULT_INTERVAL_SECONDS))))
    parser.add_argument("--runtime", type=int, default=int(os.getenv("OKX_RUNTIME", "0")))
    parser.add_argument("--flag", default=os.getenv("OKX_FLAG", "0"), choices=("0", "1"))
    parser.add_argument("--ai", action="store_true", default=os.getenv("AI_ENABLED", "0") == "1")
    parser.add_argument("--dry-run-ai", action="store_true", default=os.getenv("AI_DRY_RUN", "0") == "1")
    parser.add_argument("--push", action="store_true", default=os.getenv("PUSH_ENABLED", "0") == "1")
    parser.add_argument("--push-score", type=int, default=int(os.getenv("PUSH_SCORE", str(DEFAULT_PUSH_SCORE))))
    parser.add_argument(
        "--short-push-score",
        type=int,
        default=int(os.getenv("SHORT_PUSH_SCORE", os.getenv("PUSH_SCORE", str(DEFAULT_PUSH_SCORE)))),
        help="Trade push threshold for short direction (defaults to --push-score)",
    )
    parser.add_argument("--retry-times", type=int, default=env_int("RETRY_TIMES", DEFAULT_RETRY_TIMES))
    parser.add_argument("--retry-backoff", type=float, default=env_float("RETRY_BACKOFF_SECONDS", DEFAULT_RETRY_BACKOFF_SECONDS))
    parser.add_argument("--push-cooldown", type=int, default=env_int("PUSH_COOLDOWN_SECONDS", DEFAULT_PUSH_COOLDOWN_SECONDS))
    parser.add_argument("--log-max-bytes", type=int, default=env_int("LOG_MAX_BYTES", DEFAULT_LOG_MAX_BYTES))
    parser.add_argument(
        "--log-total-max-bytes",
        type=int,
        default=env_int("LOG_TOTAL_MAX_BYTES", DEFAULT_LOG_TOTAL_MAX_BYTES),
        help="Total size cap for all rotated analysis log segments",
    )
    parser.add_argument("--volume-multiplier", type=float, default=env_float("VOLUME_MULTIPLIER", 2.0))
    parser.add_argument("--oi-change-pct-15m", type=float, default=env_float("OI_CHANGE_PCT_15M", 5.0))
    parser.add_argument("--funding-threshold", type=float, default=env_float("FUNDING_ABS_THRESHOLD", 0.0008))
    parser.add_argument("--funding-change-threshold", type=float, default=env_float("FUNDING_CHANGE_THRESHOLD", 0.0003))
    parser.add_argument("--long-short-extreme", type=float, default=env_float("LONG_SHORT_EXTREME", 0.75))
    parser.add_argument("--strategy-mode", choices=tuple(STRATEGY_PROFILES.keys()), default=os.getenv("STRATEGY_MODE", "short"))
    parser.add_argument("--risk-preference", choices=("conservative", "standard", "aggressive"), default=os.getenv("RISK_PREFERENCE", "standard"))
    parser.add_argument("--ai-output-style", choices=("steady", "momentum", "trend"), default=os.getenv("AI_OUTPUT_STYLE", "steady"))
    parser.add_argument("--trade-signals", action="store_true", default=os.getenv("TRADE_SIGNALS_ENABLED", "1") == "1")
    parser.add_argument("--no-trade-signals", action="store_false", dest="trade_signals")
    parser.add_argument("--watch-signals", action="store_true", default=os.getenv("WATCH_SIGNALS_ENABLED", "1") == "1")
    parser.add_argument("--no-watch-signals", action="store_false", dest="watch_signals")
    parser.add_argument("--spike-alerts", action="store_true", default=os.getenv("SPIKE_ALERTS_ENABLED", "1") == "1")
    parser.add_argument("--no-spike-alerts", action="store_false", dest="spike_alerts")
    parser.add_argument("--allow-scalp-trade", action="store_true", default=os.getenv("ALLOW_SCALP_TRADE", "0") == "1")
    parser.add_argument("--allow-counter-4h-scalp", action="store_true", default=os.getenv("ALLOW_COUNTER_4H_SCALP", "0") == "1")
    parser.add_argument("--allow-oi-divergence-momentum", action="store_true", default=os.getenv("ALLOW_OI_DIVERGENCE_MOMENTUM", "0") == "1")
    parser.add_argument("--scalp-move-pct-5m", type=float, default=env_float("SCALP_MOVE_PCT_5M", 0.22))
    parser.add_argument("--scalp-move-pct-10m", type=float, default=env_float("SCALP_MOVE_PCT_10M", 0.35))
    parser.add_argument("--watch-push-score", type=int, default=env_int("WATCH_PUSH_SCORE", DEFAULT_PUSH_SCORE))
    parser.add_argument("--spike-push-score", type=int, default=env_int("SPIKE_PUSH_SCORE", 62))
    parser.add_argument(
        "--ai-conflict-guard",
        action="store_true",
        default=os.getenv("AI_CONFLICT_GUARD", "1") == "1",
        help="Block trade/spike that opposes active scalp spike or short-window pressure",
    )
    parser.add_argument("--no-ai-conflict-guard", action="store_false", dest="ai_conflict_guard")
    parser.add_argument(
        "--l3-local-spike-push",
        action="store_true",
        default=os.getenv("L3_LOCAL_SPIKE_PUSH", "1") == "1",
        help="On L3 scalp_spike, prefer local spike push without waiting for AI",
    )
    parser.add_argument("--no-l3-local-spike-push", action="store_false", dest="l3_local_spike_push")
    parser.add_argument(
        "--l2-require-volume-or-structure",
        action="store_true",
        default=os.getenv("L2_REQUIRE_VOLUME_OR_STRUCTURE", "1") == "1",
        help="L2 trade_signal requires volume/structure (macd alone needs a second signal)",
    )
    parser.add_argument("--no-l2-require-volume-or-structure", action="store_false", dest="l2_require_volume_or_structure")
    parser.add_argument(
        "--spike-push-cooldown",
        type=int,
        default=env_int("SPIKE_PUSH_COOLDOWN_SECONDS", DEFAULT_SPIKE_PUSH_COOLDOWN_SECONDS),
    )
    parser.add_argument(
        "--watch-push-cooldown",
        type=int,
        default=env_int("WATCH_PUSH_COOLDOWN_SECONDS", DEFAULT_WATCH_PUSH_COOLDOWN_SECONDS),
    )
    parser.add_argument(
        "--reverse-trade-cooldown",
        type=int,
        default=env_int("REVERSE_TRADE_COOLDOWN_SECONDS", DEFAULT_REVERSE_TRADE_COOLDOWN_SECONDS),
    )
    parser.add_argument(
        "--forecast-alerts",
        action="store_true",
        default=os.getenv("FORECAST_ALERTS_ENABLED", "1") == "1",
        help="Enable parallel structure evolution forecast track",
    )
    parser.add_argument("--no-forecast-alerts", action="store_false", dest="forecast_alerts")
    parser.add_argument("--forecast-push-score", type=int, default=env_int("FORECAST_PUSH_SCORE", 58))
    parser.add_argument("--forecast-horizon-minutes", type=int, default=env_int("FORECAST_HORIZON_MINUTES", 15))
    parser.add_argument(
        "--forecast-push-cooldown",
        type=int,
        default=env_int("FORECAST_PUSH_COOLDOWN_SECONDS", DEFAULT_FORECAST_PUSH_COOLDOWN_SECONDS),
    )
    parser.add_argument(
        "--calibration",
        action="store_true",
        default=os.getenv("CALIBRATION_ENABLED", "1") == "1",
        help="Enable online self-calibration from settled forecast/decision outcomes",
    )
    parser.add_argument("--no-calibration", action="store_false", dest="calibration")
    parser.add_argument("--calibration-min-samples", type=int, default=env_int("CALIBRATION_MIN_SAMPLES", 8))
    parser.add_argument(
        "--calibration-blend-weight",
        type=float,
        default=env_float("CALIBRATION_BLEND_WEIGHT", 0.65),
    )
    parser.add_argument(
        "--calibration-disable-below-hit-rate",
        type=float,
        default=env_float("CALIBRATION_DISABLE_BELOW_HIT_RATE", 0.38),
    )
    parser.add_argument("--record-replay", action="store_true", default=os.getenv("RECORD_REPLAY", "0") == "1")
    parser.add_argument(
        "--record-replay-file",
        default=os.getenv("RECORD_REPLAY_FILE", str(REPLAY_DATASET_FILE)),
        help="Append live collect_snapshot frames to this JSONL file",
    )
    parser.add_argument("--replay-file", default=os.getenv("REPLAY_FILE", ""), help="Replay dataset JSONL and exit")
    parser.add_argument("--replay-interval", type=float, default=env_float("REPLAY_INTERVAL", 0.0))
    parser.add_argument(
        "--replay-log-file",
        default=os.getenv("REPLAY_LOG_FILE", str(REPLAY_LOG_FILE)),
        help="Analysis JSONL output path when replaying",
    )
    parser.add_argument(
        "--analysis-log",
        action="store_true",
        default=os.getenv("ANALYSIS_LOG_ENABLED", "1") == "1",
        help="Write JSON analysis log and per-round console summaries during live monitoring",
    )
    parser.add_argument("--no-analysis-log", action="store_false", dest="analysis_log")
    parser.add_argument(
        "--paper-follow-ai-only",
        action="store_true",
        default=os.getenv("PAPER_FOLLOW_AI_ONLY", "1") == "1",
        help="Paper account follows AI forward_view only (default on)",
    )
    parser.add_argument("--no-paper-follow-ai-only", action="store_false", dest="paper_follow_ai_only")
    parser.add_argument(
        "--paper-fee-bps",
        type=float,
        default=env_float("PAPER_FEE_BPS", 5.0),
        help="Simulated round-trip fee in basis points per open/close leg",
    )
    parser.add_argument(
        "--forward-require-forecast-alignment",
        action="store_true",
        default=os.getenv("FORWARD_REQUIRE_FORECAST_ALIGNMENT", "1") == "1",
        help="Require AI forward_view to align with active structure_forecast for trade/forecast push",
    )
    parser.add_argument(
        "--no-forward-require-forecast-alignment",
        action="store_false",
        dest="forward_require_forecast_alignment",
    )
    parser.add_argument(
        "--replay-ai-cache",
        action="store_true",
        default=os.getenv("REPLAY_AI_CACHE_ENABLED", "1") == "1",
        help="Cache AI responses by inst+fingerprint during replay for deterministic regression",
    )
    parser.add_argument("--no-replay-ai-cache", action="store_false", dest="replay_ai_cache")

    # add_argument只是向实例中注册参数，parse_args才是真正解析命令行参数的地方。会优先检测py执行时有没有传入参数，没有才会使用default；
    return parser.parse_args()


def main() -> int:
    # 程序入口：解析参数，创建助手实例，进入循环。
    args = parse_args()
    assistant = OkxAiShortTermAssistant(
        instruments=args.inst_ids,
        interval=max(args.interval, 1),
        flag=args.flag,
        ai_enabled=args.ai,
        push_enabled=args.push,
        push_score=args.push_score,
        short_push_score=args.short_push_score,
        dry_run_ai=args.dry_run_ai,
        config=SignalConfig(
            volume_multiplier=args.volume_multiplier,
            oi_change_pct_15m=args.oi_change_pct_15m,
            funding_abs_threshold=args.funding_threshold,
            funding_change_threshold=args.funding_change_threshold,
            long_short_extreme=args.long_short_extreme,
            strategy_mode=args.strategy_mode,
            risk_preference=args.risk_preference,
            signal_trade_enabled=args.trade_signals,
            signal_watch_enabled=args.watch_signals,
            signal_spike_enabled=args.spike_alerts,
            ai_output_style=args.ai_output_style,
            allow_scalp_trade=args.allow_scalp_trade,
            allow_counter_4h_scalp=args.allow_counter_4h_scalp,
            allow_oi_divergence_momentum=args.allow_oi_divergence_momentum,
            scalp_move_pct_5m=args.scalp_move_pct_5m,
            scalp_move_pct_10m=args.scalp_move_pct_10m,
            watch_push_score=args.watch_push_score,
            spike_push_score=args.spike_push_score,
            ai_conflict_guard=bool(args.ai_conflict_guard),
            l3_local_spike_push=bool(args.l3_local_spike_push),
            l2_require_volume_or_structure=bool(args.l2_require_volume_or_structure),
            signal_forecast_enabled=bool(args.forecast_alerts),
            forecast_push_score=max(0, min(100, int(args.forecast_push_score))),
            forecast_horizon_minutes=max(5, int(args.forecast_horizon_minutes)),
            calibration_enabled=bool(args.calibration),
            calibration_min_samples=max(3, int(args.calibration_min_samples)),
            calibration_blend_weight=max(0.0, min(1.0, float(args.calibration_blend_weight))),
            calibration_disable_below_hit_rate=max(0.1, min(0.9, float(args.calibration_disable_below_hit_rate))),
            paper_follow_ai_only=bool(args.paper_follow_ai_only),
            paper_fee_bps=max(0.0, float(args.paper_fee_bps)),
            forward_require_forecast_alignment=bool(args.forward_require_forecast_alignment),
            replay_ai_cache_enabled=bool(args.replay_ai_cache),
        ),
        runtime_config=RuntimeConfig(
            retry_times=max(args.retry_times, 1),
            retry_backoff=max(args.retry_backoff, 0.1),
            push_cooldown_seconds=max(args.push_cooldown, 0),
            spike_push_cooldown_seconds=max(args.spike_push_cooldown, 0),
            watch_push_cooldown_seconds=max(args.watch_push_cooldown, 0),
            reverse_trade_cooldown_seconds=max(args.reverse_trade_cooldown, 0),
            forecast_push_cooldown_seconds=max(args.forecast_push_cooldown, 0),
            log_max_bytes=max(args.log_max_bytes, MIN_LOG_MAX_BYTES),
            log_total_max_bytes=max(args.log_total_max_bytes, max(args.log_max_bytes, MIN_LOG_MAX_BYTES)),
            analysis_log_enabled=bool(args.analysis_log),
        ),
    )
    if args.record_replay:
        assistant.record_replay_file = Path(args.record_replay_file)
    if args.replay_file:
        assistant.replay_log_file = Path(args.replay_log_file)
        _, frames = load_replay_dataset(Path(args.replay_file))
        try:
            assistant.run_replay(frames, max(float(args.replay_interval), 0.0))
        except KeyboardInterrupt:
            console_info("Replay stopped.")
        return 0
    try:
        assistant.run_forever(args.runtime)
    except KeyboardInterrupt:
        console_info("Stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
