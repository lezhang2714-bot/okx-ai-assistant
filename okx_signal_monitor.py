#!/usr/bin/env python3
"""
OKX AI short-term trading assistant V1.

Scope:
    - Monitor BTC-USDT-SWAP and ETH-USDT-SWAP only.
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
    export LOG_MAX_BYTES=10485760
    export VOLUME_MULTIPLIER=2.0
    export OI_CHANGE_PCT_15M=5.0
    export AI_REQUEST_TIMEOUT=30
    export AI_CIRCUIT_FAIL_THRESHOLD=3
    export AI_CIRCUIT_COOLDOWN_SECONDS=120
    export AI_PROBE_INTERVAL_SECONDS=60
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple

try:
    import okx.MarketData as MarketData
    import okx.PublicData as PublicData
except ImportError:
    MarketData = None
    PublicData = None

# AI短线助手V1的固定范围：只做BTC/ETH USDT永续，不做现货和其他币种。
SUPPORTED_INSTRUMENTS = ("BTC-USDT-SWAP", "ETH-USDT-SWAP")

# 多周期K线用于判断短线趋势结构：
# 1m/3m负责入场节奏，5m/15m负责短线方向，1H/4H负责上级环境。
# 这里保留1m、5m、15m、1H这些旧字段，同时新增3m和4H，保证历史日志、AI prompt和Web展示兼容。
BAR_CHANNELS = ("1m", "3m", "5m", "15m", "1H", "4H")
KLINE_LIMIT = 200
DEFAULT_INTERVAL_SECONDS = 5
DEFAULT_PUSH_SCORE = 80
DEFAULT_AI_MODEL = "gpt-5.5"
OKX_BASE_URL = "https://www.okx.com"
DEFAULT_RETRY_TIMES = 3
DEFAULT_RETRY_BACKOFF_SECONDS = 1.5
DEFAULT_AI_REQUEST_TIMEOUT = 30.0
DEFAULT_AI_PROBE_TIMEOUT = 10.0
DEFAULT_AI_CIRCUIT_FAIL_THRESHOLD = 3
DEFAULT_AI_CIRCUIT_COOLDOWN_SECONDS = 120
DEFAULT_AI_PROBE_INTERVAL_SECONDS = 60
DEFAULT_AI_RATE_LIMIT_BACKOFF_SECONDS = 30.0
DEFAULT_PUSH_COOLDOWN_SECONDS = 900
DEFAULT_LOG_MAX_BYTES = 10 * 1024 * 1024
WARMUP_MINUTES = 15
HISTORY_RETENTION_MINUTES = 180
METRIC_SAMPLE_INTERVAL_SECONDS = 60

# 资金/OI/动态阈值按约1分钟保存一个有效样本，180分钟约180个点。
# maxlen多留余量，兼容用户把轮询间隔调低、未来把部分指标改为更高频采样的情况。
METRIC_HISTORY_MAXLEN = HISTORY_RETENTION_MINUTES * 3

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
REPLAY_DATASET_VERSION = "1.0"
SIGNAL_PERFORMANCE_FILE = LOG_DIR / "signal_performance.jsonl"
SIGNAL_PERFORMANCE_MAX_BYTES = 10 * 1024 * 1024
SIGNAL_PERFORMANCE_LOAD_BYTES = 2 * 1024 * 1024


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
            "trend": 1.0,
            "momentum": 1.0,
            "volume_price": 1.0,
            "derivatives": 1.0,
            "orderbook": 0.8,
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
            "trend": 1.4,
            "momentum": 0.7,
            "volume_price": 0.7,
            "derivatives": 1.2,
            "orderbook": 0.3,
            "risk_control": 1.3,
        },
        "entry_style": "trend_structure",
        "holding_time": "数小时-数天",
    },
}


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


@dataclass
class RuntimeConfig:
    # 网络请求失败后的重试次数，解决偶发超时、临时DNS异常等问题。
    retry_times: int = DEFAULT_RETRY_TIMES

    # 重试退避基础秒数，第N次失败会等待 retry_backoff * N 秒。
    retry_backoff: float = DEFAULT_RETRY_BACKOFF_SECONDS

    # 同币种、同方向、同类信号的推送冷却时间，避免重复轰炸。
    push_cooldown_seconds: int = DEFAULT_PUSH_COOLDOWN_SECONDS

    # 单个日志文件最大字节数，超过后轮转成 .1 文件。
    log_max_bytes: int = DEFAULT_LOG_MAX_BYTES


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
        for bar in BAR_CHANNELS:
            if bar not in frame["candles"]:
                raise ValueError(f"replay frame #{index + 1} missing candles.{bar}")
    frames.sort(key=lambda frame: (frame.get("time", ""), frame.get("inst_id", "")))
    return meta, frames


def replay_dataset_stats(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {"exists": False, "path": str(path), "frame_count": 0, "inst_ids": [], "interval_seconds": 0}
    meta, frames = load_replay_dataset(path)
    inst_ids = sorted({str(frame.get("inst_id", "")) for frame in frames if frame.get("inst_id")})
    return {
        "exists": True,
        "path": str(path),
        "frame_count": len(frames),
        "inst_ids": inst_ids,
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
    for attempt in range(1, retry_times + 1):
        try:
            return func()
        except Exception as exc:
            last_error = exc
            if attempt >= retry_times:
                break
            sleep_seconds = retry_backoff * attempt
            print(f"[{now_text()}] {label} failed, retry {attempt}/{retry_times}: {exc}")
            time.sleep(sleep_seconds)
    raise RuntimeError(f"{label} failed after {retry_times} retries: {last_error}")


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

    return retry_call(path, request, retry_times, retry_backoff)


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


def trend_from_candles(candles: List[Dict[str, Any]], lookback: int = 5) -> str:
    # 兼容旧字段的轻量趋势判断：比较最近收盘价和lookback窗口最后一根收盘价。
    # 新版评分不再只依赖它，而是由trend_profile_from_candles计算EMA、ATR、结构位和K线质量。
    if len(candles) < 2:
        return "unknown"
    sample = confirmed_candles(candles)[:lookback]
    latest = sample[0]["close"]
    oldest = sample[-1]["close"]
    if latest > oldest:
        return "up"
    if latest < oldest:
        return "down"
    return "flat"


def trend_profile_from_candles(candles: List[Dict[str, Any]]) -> Dict[str, Any]:
    # 单周期趋势画像。这里故意不只返回up/down，而是把“趋势、波动、结构、K线质量”都拆开。
    # 原因是短线交易里，同样是上涨，可能是稳定趋势、放量突破、尾端加速或高波动震荡，入场方式完全不同。
    rows = confirmed_candles(candles)
    closes = [to_float(item.get("close")) for item in rows]

    # 判断K线质量，从这组k线中计算出来在指标是否可靠
    data_quality = {
        "confirmed_count": len(rows),        # k线个数
        "ema120_ready": len(rows) >= 120,    # EMA120是否可靠
        "macd_ready": len(rows) >= 35,
        "adx_ready": len(rows) >= 28,
        "rsi_ready": len(rows) >= 25,
        "is_reliable": len(rows) >= 35,
    }
    if len(closes) < 5:
        return {
            "trend": "unknown",
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
    previous = closes[min(4, len(closes) - 1)]

    # 计算各个EMA
    ema9 = ema(closes[:40], 9)
    fast = ema(closes[:80], 20)
    slow = ema(closes[:120], 60)
    ema120 = ema(closes[:120], 120)
    ma120 = sma(closes[:120], 120)

    # 平均真实波动幅度，判断市场是否平静
    atr_value = atr(rows, 14)

    # 最近20k线的价格最高点/最低点
    points = structure_points(rows[1:], 20)
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
    if latest > ema9 > fast > slow and slope_pct > 0:
        trend = "up"
    elif latest < ema9 < fast < slow and slope_pct < 0:
        trend = "down"
    elif adx_values["adx"] < 18 or abs(slope_pct) < 0.08 or safe_div(atr_value, latest) * 100 < 0.08:
        trend = "range"
    else:
        trend = "mixed"

    return {
        "trend": trend,
        "data_quality": data_quality,
        "ema_fast": fast,
        "ema_slow": slow,
        "ema": {"9": ema9, "20": fast, "60": slow, "120": ema120},
        "ma": {"120": ma120},
        "ema_slope_pct": slope_pct,
        "atr": atr_value,
        "atr_pct": safe_div(atr_value, latest) * 100,
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


class OkxAiShortTermAssistant:
    """OKX短线助手主类。

    这个类串起完整流程：
    采集OKX数据 -> 检测异常信号 -> 规则评分 -> 可选AI分析 -> 可选推送 -> 写日志。
    """

    def __init__(
        self,
        instruments: List[str],
        interval: int,
        flag: str,
        ai_enabled: bool,
        push_enabled: bool,
        push_score: int,
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

        # 推送冷却状态。key通常由币种、方向、信号类型组成。
        self.last_push_at: Dict[str, float] = {}

        # 在线信号追踪状态。它不是下单，也不改变推送逻辑，只把系统给出的观察信号当作样本，
        # 在后续5m/15m/1H用真实价格结算表现，逐步积累胜率、平均收益、最大顺向/逆向波动。
        self.pending_signal_reviews: List[Dict[str, Any]] = []
        self.signal_performance: Dict[str, Dict[str, Any]] = {}
        self.last_signal_track_at: Dict[str, float] = {}
        self._load_signal_performance()

        # AI 连接状态：请求重试 + client 重建 + 熔断探活。
        self._ai_client: Any = None
        self._ai_client_config: Tuple[str, str] = ("", "")
        self.ai_fail_streak = 0
        self.ai_circuit_open_until = 0.0
        self.ai_last_probe_at = 0.0

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
    ) -> None:
        if not self.record_replay_file:
            return
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
        }
        with self.record_replay_file.open("a", encoding="utf-8") as file:
            file.write(json.dumps(frame, ensure_ascii=False) + "\n")

    def collect_snapshot(self, inst_id: str) -> Dict[str, Any]:
        # 获取当前时刻的成交价、买入挂单价、卖出挂单价。
        ticker = self._get_ticker(inst_id)

        # 获取k线，返回字典：key是哪个k线周期，数据是数组，包含当前周期下最近KLINE_LIMIT根K线的数据结构，
        # 一个结构包含：时间戳、开盘价、最高价、最低价、收盘价、成交量、是否收盘标记（当前k线还是历史k线）
        candles = {bar: self._get_candles(inst_id, bar) for bar in BAR_CHANNELS}

        # 参照1m周期k线计算成交量数据；返回一个结构，包含当前成交量、历史20根K线的平均成交量、放量倍数（当前/平均）。
        volume = self._volume_stats(candles["1m"])

        # 获取当前合约的OI持仓量。返回的就是一个OI值
        open_interest = self._get_open_interest(inst_id)

        # 获取当前合约的资金费率。返回的就是一个资金费率值
        funding_rate = self._get_funding_rate(inst_id)

        # 获取当前5m周期内的多空比，并换算成百分比；返回一个结构，包含：多空比、多头百分比、空头百分比、是否有效
        long_short = self._get_long_short_ratio(inst_id)

        # 获取订单簿前20档深度，用于判断短线买卖盘压力。
        # 订单簿变化很快，所以只作为入场确认和风险修正，不单独决定方向。
        order_book = self._get_order_book(inst_id)

        if self.record_replay_file and not self.replay_mode:
            self._append_replay_frame(inst_id, ticker, candles, open_interest, funding_rate, long_short, order_book)

        # 记录当前OI和资金费率。API本身60秒缓存，按分钟写样本即可，避免重复采样污染15m变化和分位数。
        self._remember_metric(self.oi_history[inst_id], open_interest, METRIC_SAMPLE_INTERVAL_SECONDS)
        self._remember_metric(self.funding_history[inst_id], funding_rate, METRIC_SAMPLE_INTERVAL_SECONDS)

        oi_change_pct_15m = self._change_pct_last_minutes(self.oi_history[inst_id], 15)
        funding_change_15m = self._change_last_minutes(self.funding_history[inst_id], 15)

        # 计算K线走势
        profiles = {bar: trend_profile_from_candles(rows) for bar, rows in candles.items()}

        # 计算波动强度：高中低，便于后续判断止损止盈区间
        volatility = self._volatility_context(inst_id, profiles)

        # 动态阈值调整：每个币种根据自己的历史来调整阈值
        dynamic_thresholds = self._dynamic_thresholds(inst_id)
        market_context = self._market_context(
            price=ticker.get("last", 0.0),
            candles=candles,
            profiles=profiles,
            volume=volume,
            open_interest=open_interest,
            oi_change_pct_15m=oi_change_pct_15m,
            funding_rate=funding_rate,
            funding_change_15m=funding_change_15m,
            long_short=long_short,
            order_book=order_book,
            volatility=volatility,
            dynamic_thresholds=dynamic_thresholds,
        )

        self._remember_metric(self.volume_multiplier_history[inst_id], volume["multiplier"], METRIC_SAMPLE_INTERVAL_SECONDS)
        self._remember_metric(self.atr_pct_history[inst_id], volatility["atr_pct_15m"], METRIC_SAMPLE_INTERVAL_SECONDS)
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
            # 数据是否满足15min的要求，预热是否完成
            "oi_warmup_ready": self._history_ready(self.oi_history[inst_id], WARMUP_MINUTES),
            "funding_rate": funding_rate,
            # 资金费率相对于15Min前的变化量，就是当前资金费率 - 15Min前的资金费率
            "funding_change": funding_change_15m,
            "funding_warmup_ready": self._history_ready(self.funding_history[inst_id], WARMUP_MINUTES),
            "long_short_ratio": long_short,
            "order_book": order_book,
            "trend_profiles": profiles,
            "volatility": volatility,
            "dynamic_thresholds": dynamic_thresholds,

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
        funding_change = snapshot["funding_change"]
        oi_change = snapshot["oi_change_pct_15m"]
        dynamic = snapshot.get("dynamic_thresholds", {})
        context = snapshot.get("market_context", {})
        profiles = snapshot.get("trend_profiles", {})
        order_book = snapshot.get("order_book", {})

        # 放量阈值采用“用户配置”和“近期分位数”里的较高者。
        # 这样低波动环境仍尊重用户阈值，高波动环境则自动抬高门槛，减少普通波动被误判成强信号。
        volume_threshold = max(self.config.volume_multiplier, dynamic.get("volume_multiplier_p85", 0.0))
        if volume["multiplier"] >= volume_threshold:
            # 放量只表示交易活跃度提高，不直接代表涨跌方向，必须结合K线结构、OI和盘口。
            signals.append({
                "type": "volume_spike",
                "desc": f"confirmed 1m volume multiplier {volume['multiplier']:.2f}x >= {volume_threshold:.2f}x",
                "strength": "high" if volume["multiplier"] >= dynamic.get("volume_multiplier_p95", volume_threshold * 1.5) else "normal",
            })

        # 结构突破比单纯up/down更有交易意义，但缺少放量或盘口配合时容易是假突破。
        breakout_15m = profiles.get("15m", {}).get("breakout")
        breakout_5m = profiles.get("5m", {}).get("breakout")
        rsi_15m = to_float(profiles.get("15m", {}).get("rsi", {}).get("14"), 50.0)
        macd_15m = profiles.get("15m", {}).get("macd", {})
        boll_15m = profiles.get("15m", {}).get("boll", {})
        adx_15m = to_float(profiles.get("15m", {}).get("adx", {}).get("adx"))
        if breakout_15m in ("up", "down") or breakout_5m in ("up", "down"):
            signals.append({
                "type": "structure_break",
                "desc": f"structure breakout 5m={breakout_5m} 15m={breakout_15m}",
                "direction_hint": "做多" if "up" in (breakout_5m, breakout_15m) else "做空",
            })

        if context.get("regime") == "squeeze":
            signals.append({
                "type": "boll_squeeze",
                "desc": f"15m boll squeeze bandwidth={to_float(boll_15m.get('bandwidth_pct')):.4f}% adx={adx_15m:.2f}",
            })

        if profiles.get("15m", {}).get("divergence") in ("bearish", "bullish"):
            signals.append({
                "type": "rsi_divergence",
                "desc": f"15m RSI {profiles.get('15m', {}).get('divergence')} divergence",
            })

        if rsi_15m >= 80 or rsi_15m <= 20:
            signals.append({
                "type": "rsi_extreme",
                "desc": f"15m RSI extreme {rsi_15m:.2f}",
            })

        if abs(to_float(macd_15m.get("hist_slope"))) > abs(to_float(macd_15m.get("hist"))) * 0.25 and abs(to_float(macd_15m.get("hist"))) > 0:
            signals.append({
                "type": "macd_momentum_change",
                "desc": f"15m MACD hist={to_float(macd_15m.get('hist')):.4f} slope={to_float(macd_15m.get('hist_slope')):.4f}",
            })

        if snapshot.get("oi_warmup_ready") and abs(oi_change) >= self.config.oi_change_pct_15m:
            # 持仓率判断：OI变化表示合约持仓量变化，配合价格可以判断新开仓或平仓压力。
            signals.append({
                "type": "oi_change",
                "desc": f"15m OI change {oi_change:.2f}% ({context.get('oi_price_state', 'unknown')})",
            })

        if abs(funding_rate) >= self.config.funding_abs_threshold:
            # 当前资金费率判断：资金费率过热说明多空某一侧过于拥挤，追单风险会提高。
            signals.append({
                "type": "funding_hot",
                "desc": f"funding rate {funding_rate:.6f}",
            })

        if snapshot.get("funding_warmup_ready") and abs(funding_change) >= self.config.funding_change_threshold:
            # 当前资金费率变化量判断：资金费率快速变化代表市场情绪在短时间内切换。
            signals.append({
                "type": "funding_fast_change",
                "desc": f"15m funding change {funding_change:.6f}",
            })

        if long_ratio >= self.config.long_short_extreme:
            # 多头占比过高，继续做多的拥挤风险会提高。
            signals.append({
                "type": "long_short_extreme",
                "desc": f"long ratio {long_ratio:.2%}",
            })
        elif short_ratio >= self.config.long_short_extreme:
            # 空头占比过高，继续做空的拥挤风险会提高。
            signals.append({
                "type": "long_short_extreme",
                "desc": f"short ratio {short_ratio:.2%}",
            })

        # 盘口不平衡只做短线确认。买盘大于卖盘并不保证上涨，但可以提高突破/回踩的入场质量。
        if order_book.get("available") and abs(order_book.get("imbalance", 0.0)) >= max(0.35, dynamic.get("book_imbalance_p85", 0.35)):
            signals.append({
                "type": "order_book_imbalance",
                "desc": f"top20 book imbalance {order_book.get('imbalance', 0.0):.2f} ({context.get('order_book_bias', 'neutral')})",
            })

        return signals

    def score_snapshot(self, snapshot: Dict[str, Any], signals: List[Dict[str, Any]]) -> Dict[str, Any]:
        # 评分系统满分100分。新版不再把“多周期多数上涨/下跌”直接当交易方向，
        # 而是先识别市场状态，再给出方向倾向、入场质量、失效条件。
        # 评分仍然只用于“提醒强弱”和“是否推送”，不是自动交易指令。

        # 趋势判断，通过当前收盘价和21条K线的最早收盘价，判断时间范围内的up/down/flat/unkonw状态
        trends = {
            "1m": trend_from_candles(snapshot["candles"]["1m"]),
            "3m": trend_from_candles(snapshot["candles"].get("3m", [])),
            "5m": trend_from_candles(snapshot["candles"]["5m"]),
            "15m": trend_from_candles(snapshot["candles"]["15m"]),
            "1H": trend_from_candles(snapshot["candles"]["1H"]),
            "4H": trend_from_candles(snapshot["candles"].get("4H", [])),
        }

        profiles = snapshot.get("trend_profiles", {})
        context = snapshot.get("market_context", {})
        volatility = snapshot.get("volatility", {})
        signal_types = {item["type"] for item in signals}
        raw_direction = {"long": "做多", "short": "做空"}.get(context.get("bias"), "观望")

        strategy_profile = self._strategy_profile()
        layer_scores = self._layer_scores(snapshot, signals, raw_direction, trends)
        trend_score = min(50, layer_scores["trend_score"] + layer_scores["momentum_score"])
        capital_score = min(30, layer_scores["volume_price_score"] + layer_scores["derivatives_score"] + layer_scores["orderbook_score"])
        risk_control_score = min(20, layer_scores["risk_control_score"])
        entry_quality_score = min(20, layer_scores["entry_quality_score"])
        raw_total_score = max(0, min(100, sum(layer_scores.values())))

        # 价位建议基于ATR、结构高低点和EMA/VWAP近似，不再使用固定百分比。
        # 如果市场状态不清晰、入场质量差，会返回观望和明确等待条件。
        final_direction = raw_direction
        direction_guard = self._direction_guard(raw_direction, context)
        if direction_guard:
            final_direction = "\u89c2\u671b"
        entry_plan = self._suggest_levels(snapshot, final_direction)
        if not direction_guard and entry_plan["quality"] in ("no_trade", "wait_confirmation") and raw_total_score < 88:
            final_direction = "观望"
            entry_plan = self._suggest_levels(snapshot, final_direction)
        final_trade_score = raw_total_score if final_direction in ("做多", "做空") else 0

        score = {
            "trend_score": trend_score,
            "capital_score": capital_score,
            # risk_score保留给旧Web/日志兼容；新版请优先看risk_control_score和entry_quality_score。
            "risk_score": risk_control_score,
            "risk_control_score": risk_control_score,
            "entry_quality_score": entry_quality_score,
            "raw_total_score": raw_total_score,
            "final_trade_score": final_trade_score,
            "total_score": final_trade_score if final_direction == "观望" else raw_total_score,
            "layer_scores": layer_scores,
            "raw_direction": raw_direction,
            "final_direction": final_direction,
            "direction": final_direction,
            "entry": entry_plan["entry"],
            "stop_loss": entry_plan["stop_loss"],
            "take_profit": entry_plan["take_profit"],

            # 风险等级：高、中、低
            "market_risk_level": self._market_risk_level(raw_total_score, signals),
            "trade_action_level": self._trade_action_level(final_trade_score, final_direction, entry_plan),
            # risk_level保留兼容旧Web/日志；新版请优先看market_risk_level/trade_action_level。
            "risk_level": self._market_risk_level(raw_total_score, signals),
            "trends": trends,
            "market_regime": context.get("regime", "unknown"),
            "bias": context.get("bias", "neutral"),
            "entry_plan": entry_plan,
            "direction_guard": direction_guard,
            "confidence": raw_total_score,
            "strategy_mode": strategy_profile["mode"],
            "strategy_label": strategy_profile["label"],
            "risk_preference": self._risk_preference(),
            "ai_output_style": self._ai_output_style(),
        }
        strategy_views = self._strategy_views(snapshot, signals, score)
        score["strategy_views"] = strategy_views
        selected = strategy_views.get(strategy_profile["mode"], {})
        score["selected_strategy_view"] = selected
        if selected:
            score["trade_action_level"] = selected.get("action_level", score["trade_action_level"])
            score["holding_time"] = selected.get("holding_time", strategy_profile.get("holding_time"))
            if strategy_profile["mode"] == "scalp" and selected.get("trade_allowed"):
                score["direction"] = selected.get("direction", score["direction"])
                score["final_direction"] = score["direction"]
                if selected.get("entry") not in (None, ""):
                    score["entry"] = selected.get("entry")
                    score["stop_loss"] = selected.get("stop_loss")
                    score["take_profit"] = selected.get("take_profit")
                score["final_trade_score"] = selected.get("trade_score", score["final_trade_score"])
                score["total_score"] = selected.get("score", score["total_score"])
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
        profiles = snapshot.get("trend_profiles", {})
        context = snapshot.get("market_context", {})
        volume = snapshot.get("volume", {})
        order_book = snapshot.get("order_book", {})
        long_short = snapshot.get("long_short_ratio", {})
        signal_types = {item["type"] for item in signals}
        funding = to_float(snapshot.get("funding_rate"))
        oi_change = to_float(snapshot.get("oi_change_pct_15m"))
        recent_pressure = context.get("recent_price_pressure", "neutral")
        pressure_against_direction = (
            (direction == "\u505a\u591a" and recent_pressure == "down")
            or (direction == "\u505a\u7a7a" and recent_pressure == "up")
        )

        # 1. 市场状态层：先回答“现在适不适合交易”。趋势市给基础分，震荡/高波动降低基础分。
        market_regime_score = 6
        if context.get("regime") in ("trend_up", "trend_down"):
            market_regime_score += 8
        elif context.get("regime") == "squeeze":
            market_regime_score += 4
        elif context.get("regime") in ("range", "mixed"):
            market_regime_score -= 2
        elif context.get("regime") == "high_volatility":
            market_regime_score -= 4

        # 2. 趋势层：EMA排列、ADX方向、结构高低点和多周期一致性。
        trend_score = 8
        profile_15m = profiles.get("15m", {})
        profile_1h = profiles.get("1H", {})
        data_quality = profile_15m.get("data_quality", {})
        adx_15m = profile_15m.get("adx", {})
        if profile_15m.get("trend") == profile_1h.get("trend") and profile_15m.get("trend") in ("up", "down"):
            trend_score += 5
        if direction == "做多" and to_float(adx_15m.get("plus_di")) > to_float(adx_15m.get("minus_di")) and to_float(adx_15m.get("adx")) >= 20:
            trend_score += 4
        if direction == "做空" and to_float(adx_15m.get("minus_di")) > to_float(adx_15m.get("plus_di")) and to_float(adx_15m.get("adx")) >= 20:
            trend_score += 4
        if "structure_break" in signal_types:
            trend_score += 3
        if context.get("regime") in ("range", "mixed") or to_float(adx_15m.get("adx")) < 16:
            trend_score -= 4
        if not data_quality.get("is_reliable", False):
            trend_score -= 3
        if pressure_against_direction:
            trend_score -= 5

        # 3. 动量层：RSI、MACD、KDJ和K线实体质量，判断趋势有没有“油门”。
        momentum_score = 6
        rsi_14 = to_float(profile_15m.get("rsi", {}).get("14"), 50.0)
        macd_values = profile_15m.get("macd", {})
        kdj_values = profiles.get("5m", {}).get("kdj", {})
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
        if profile_15m.get("divergence") in ("bearish", "bullish"):
            momentum_score -= 3
        if not data_quality.get("macd_ready", False) or not data_quality.get("rsi_ready", False):
            momentum_score -= 2
        if pressure_against_direction:
            momentum_score -= 4

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
        if pressure_against_direction:
            volume_price_score -= 2

        # 5. 合约资金层：OI+价格组合、资金费率、多空拥挤。
        derivatives_score = 6
        oi_state = context.get("oi_price_state")
        if direction == "做多" and oi_state == "price_up_oi_up_new_longs_or_short_pressure":
            derivatives_score += 4
        elif direction == "做空" and oi_state == "price_down_oi_up_new_shorts_or_long_pressure":
            derivatives_score += 4
        elif oi_state in ("price_up_oi_down_short_covering", "price_down_oi_down_long_deleveraging"):
            derivatives_score -= 2
        if abs(oi_change) >= 2 and snapshot.get("oi_warmup_ready"):
            derivatives_score += 2
        if abs(funding) >= self.config.funding_abs_threshold:
            derivatives_score -= 4
        if max(long_short.get("long_ratio", 0.0), long_short.get("short_ratio", 0.0)) >= self.config.long_short_extreme:
            derivatives_score -= 3

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

        # 7. 入场质量层：距离EMA20是否过远、ATR止损空间是否可控、市场状态是否需要等待确认。
        entry_quality_score = 8
        distance_atr = abs(to_float(profile_15m.get("distance_to_ema20_atr")))
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

        # 8. 风险控制层：专门表达“风险是否可控”，避免旧risk_score被入场质量混用。
        risk_control_score = 10
        if abs(funding) >= self.config.funding_abs_threshold:
            risk_control_score -= 3
        if max(long_short.get("long_ratio", 0.0), long_short.get("short_ratio", 0.0)) >= self.config.long_short_extreme:
            risk_control_score -= 2
        if context.get("regime") == "high_volatility":
            risk_control_score -= 3
        if profile_15m.get("divergence") in ("bearish", "bullish"):
            risk_control_score -= 2
        if not data_quality.get("is_reliable", False):
            risk_control_score -= 2
        if pressure_against_direction:
            risk_control_score -= 2

        weights = self._strategy_profile().get("score_weights", {})
        risk_factor = self._risk_adjustment()
        weighted_scores = {
            "market_regime_score": market_regime_score,
            "trend_score": trend_score * weights.get("trend", 1.0),
            "momentum_score": momentum_score * weights.get("momentum", 1.0),
            "volume_price_score": volume_price_score * weights.get("volume_price", 1.0),
            "derivatives_score": derivatives_score * weights.get("derivatives", 1.0),
            "orderbook_score": orderbook_score * weights.get("orderbook", 1.0),
            "entry_quality_score": entry_quality_score,
            "risk_control_score": risk_control_score * weights.get("risk_control", 1.0) * risk_factor,
        }
        return {
            "market_regime_score": max(0, min(12, int(round(weighted_scores["market_regime_score"])))),
            "trend_score": max(0, min(16, int(round(weighted_scores["trend_score"])))),
            "momentum_score": max(0, min(12, int(round(weighted_scores["momentum_score"])))),
            "volume_price_score": max(0, min(12, int(round(weighted_scores["volume_price_score"])))),
            "derivatives_score": max(0, min(12, int(round(weighted_scores["derivatives_score"])))),
            "orderbook_score": max(0, min(8, int(round(weighted_scores["orderbook_score"])))),
            "entry_quality_score": max(0, min(14, int(round(weighted_scores["entry_quality_score"])))),
            "risk_control_score": max(0, min(14, int(round(weighted_scores["risk_control_score"])))),
        }

    def _strategy_mode(self) -> str:
        mode = str(getattr(self.config, "strategy_mode", "short") or "short").lower()
        return mode if mode in STRATEGY_PROFILES else "short"

    def _risk_preference(self) -> str:
        risk = str(getattr(self.config, "risk_preference", "standard") or "standard").lower()
        return risk if risk in ("conservative", "standard", "aggressive") else "standard"

    def _ai_output_style(self) -> str:
        style = str(getattr(self.config, "ai_output_style", "steady") or "steady").lower()
        return style if style in ("steady", "momentum", "trend") else "steady"

    def _risk_adjustment(self) -> float:
        return {"conservative": 1.15, "standard": 1.0, "aggressive": 0.9}.get(self._risk_preference(), 1.0)

    def _strategy_profile(self, mode: Optional[str] = None) -> Dict[str, Any]:
        selected = mode if mode in STRATEGY_PROFILES else self._strategy_mode()
        profile = dict(STRATEGY_PROFILES[selected])
        profile["mode"] = selected
        return profile

    def _recent_move_pct(self, candles: List[Dict[str, Any]], bars: int) -> float:
        rows = confirmed_candles(candles)
        if len(rows) <= bars:
            return 0.0
        latest = to_float(rows[0].get("close"))
        old = to_float(rows[bars].get("close"))
        return pct_change(latest, old)

    def _recent_drawdown_pct(self, candles: List[Dict[str, Any]], bars: int) -> float:
        rows = confirmed_candles(candles)
        if len(rows) < 2:
            return 0.0
        sample = rows[: max(2, min(len(rows), bars + 1))]
        latest = to_float(sample[0].get("close"))
        recent_high = max(to_float(item.get("high")) for item in sample)
        return pct_change(latest, recent_high)

    def _recent_rebound_pct(self, candles: List[Dict[str, Any]], bars: int) -> float:
        rows = confirmed_candles(candles)
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
        if down_hits >= 2 or move_5m <= -max(0.12, atr_pct * 0.35):
            return "down"
        if up_hits >= 2 or move_5m >= max(0.12, atr_pct * 0.35):
            return "up"
        return "neutral"

    def _direction_guard(self, direction: str, context: Dict[str, Any]) -> str:
        pressure = context.get("recent_price_pressure")
        if direction == "做多" and pressure == "down":
            return "recent_price_pressure_down_blocks_long"
        if direction == "做空" and pressure == "up":
            return "recent_price_pressure_up_blocks_short"
        trade_up = int(context.get("trade_up", 0) or 0)
        trade_down = int(context.get("trade_down", 0) or 0)
        if direction == "做多" and pressure == "neutral" and trade_up < 2:
            return "neutral_price_pressure_blocks_long_without_5m_15m_alignment"
        if direction == "做空" and pressure == "neutral" and trade_down < 2:
            return "neutral_price_pressure_blocks_short_without_5m_15m_alignment"
        return ""

    def _short_strategy_view(self, score: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "mode": "short",
            "label": STRATEGY_PROFILES["short"]["label"],
            "direction": score.get("direction", "观望"),
            "score": score.get("raw_total_score", 0),
            "trade_score": score.get("final_trade_score", 0),
            "action_level": score.get("trade_action_level", "观望"),
            "entry": score.get("entry"),
            "stop_loss": score.get("stop_loss"),
            "take_profit": score.get("take_profit"),
            "holding_time": STRATEGY_PROFILES["short"]["holding_time"],
            "summary": "关注5m/15m结构与1H确认，适合等待突破回踩或二次确认。",
        }

    def _swing_strategy_view(self, snapshot: Dict[str, Any], score: Dict[str, Any]) -> Dict[str, Any]:
        profiles = snapshot.get("trend_profiles", {})
        trend_1h = profiles.get("1H", {}).get("trend")
        trend_4h = profiles.get("4H", {}).get("trend")
        if trend_1h == trend_4h == "up":
            direction = "做多"
            summary = "1H/4H趋势同向偏多，中线只关注结构回踩后的低频机会。"
        elif trend_1h == trend_4h == "down":
            direction = "做空"
            summary = "1H/4H趋势同向偏空，中线只关注反抽不过后的低频机会。"
        else:
            direction = "观望"
            summary = "1H/4H结构未共振，中线以关键支撑阻力观察为主。"
        swing_score = score.get("raw_total_score", 0)
        if direction == "观望":
            swing_score = min(swing_score, 72)
        return {
            "mode": "swing",
            "label": STRATEGY_PROFILES["swing"]["label"],
            "direction": direction,
            "score": swing_score,
            "trade_score": swing_score if direction in ("做多", "做空") else 0,
            "action_level": "等待结构位" if direction in ("做多", "做空") else "观望",
            "entry": "-",
            "stop_loss": "-",
            "take_profit": "-",
            "holding_time": STRATEGY_PROFILES["swing"]["holding_time"],
            "summary": summary,
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

    def _scalp_strategy_view(self, snapshot: Dict[str, Any], score: Dict[str, Any]) -> Dict[str, Any]:
        candles = snapshot.get("candles", {})
        profiles = snapshot.get("trend_profiles", {})
        context = snapshot.get("market_context", {})
        volume = snapshot.get("volume", {})
        order_book = snapshot.get("order_book", {})
        move_5m = self._recent_move_pct(candles.get("1m", []), 5)
        move_10m = self._recent_move_pct(candles.get("1m", []), 10)
        drawdown_10m = self._recent_drawdown_pct(candles.get("1m", []), 10)
        drawdown_15m = self._recent_drawdown_pct(candles.get("1m", []), 15)
        rebound_10m = self._recent_rebound_pct(candles.get("1m", []), 10)
        rebound_15m = self._recent_rebound_pct(candles.get("1m", []), 15)
        threshold_5m = max(to_float(getattr(self.config, "scalp_move_pct_5m", 0.22)), 0.01)
        threshold_10m = max(to_float(getattr(self.config, "scalp_move_pct_10m", 0.35)), 0.01)
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
        direction = "\u89c2\u671b"
        if long_breakout or long_rebound:
            direction = "\u505a\u591a"
        if (short_breakdown or short_rollover) and short_strength >= long_strength * 0.85:
            direction = "\u505a\u7a7a"

        scalp_score = 45
        if abs(move_5m) >= threshold_5m:
            scalp_score += 18
        if abs(move_10m) >= threshold_10m:
            scalp_score += 14
        if direction == "\u505a\u7a7a" and short_rollover:
            scalp_score += 10
        if direction == "\u505a\u591a" and long_rebound:
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
        levels = self._scalp_levels(snapshot, direction) if self.config.allow_scalp_trade and direction in ("做多", "做空") else {"entry": "-", "stop_loss": "-", "take_profit": "-"}
        action = "急速异动" if direction in ("做多", "做空") else "观望"
        trade_score = scalp_score if self.config.allow_scalp_trade and direction in ("做多", "做空") else 0
        if self.config.allow_scalp_trade and scalp_score >= self.push_score and direction in ("做多", "做空"):
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
            "trade_allowed": bool(self.config.allow_scalp_trade),
        }

    def _strategy_views(self, snapshot: Dict[str, Any], signals: List[Dict[str, Any]], score: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "scalp": self._scalp_strategy_view(snapshot, score),
            "short": self._short_strategy_view(score),
            "swing": self._swing_strategy_view(snapshot, score),
        }

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
            print(f"[{now_text()}] AI client reloaded after config change")
        return self._ai_client

    def _ai_circuit_state(self) -> str:
        threshold = self._ai_circuit_fail_threshold()
        if self.ai_fail_streak < threshold:
            return "closed"
        if time.time() < self.ai_circuit_open_until:
            return "open"
        return "half_open"

    def _record_ai_success(self) -> None:
        if self.ai_fail_streak or self.ai_circuit_open_until:
            print(f"[{now_text()}] AI connection recovered, circuit closed")
        self.ai_fail_streak = 0
        self.ai_circuit_open_until = 0.0

    def _record_ai_failure(self, exc: Exception) -> None:
        threshold = self._ai_circuit_fail_threshold()
        if is_auth_ai_error(exc) or not is_retryable_ai_error(exc):
            self.ai_fail_streak = threshold
        else:
            self.ai_fail_streak += 1
        if self.ai_fail_streak >= threshold:
            self.ai_circuit_open_until = time.time() + self._ai_circuit_cooldown()
            print(
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
                print(
                    f"[{now_text()}] ai-chat failed, retry {attempt}/{retry_times}: {exc}; "
                    f"sleep {sleep_seconds:.1f}s"
                )
                if is_connection_ai_error(exc):
                    self._reset_ai_client()
                    client = self._get_ai_client(api_key, base_url)
                time.sleep(sleep_seconds)

        raise RuntimeError(f"ai-chat failed after {retry_times} retries: {last_error}")

    def _probe_ai_connection(self, model: str) -> bool:
        api_key, base_url, _ = self._ai_env_config()
        if not api_key:
            return False
        try:
            client = self._get_ai_client(api_key, base_url)
            client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=5,
                temperature=0.0,
                timeout=self._ai_probe_timeout(),
            )
            return True
        except Exception as exc:
            print(f"[{now_text()}] AI probe failed: {exc}")
            self._reset_ai_client()
            return False

    def _maybe_probe_ai_connection(self, model: str) -> bool:
        now = time.time()
        if now - self.ai_last_probe_at < self._ai_probe_interval():
            return False
        self.ai_last_probe_at = now
        print(f"[{now_text()}] AI circuit probing...")
        if self._probe_ai_connection(model):
            self._record_ai_success()
            return True
        self.ai_fail_streak = self._ai_circuit_fail_threshold()
        self.ai_circuit_open_until = time.time() + self._ai_circuit_cooldown()
        return False

    def _build_ai_success_result(
        self,
        base_url: str,
        model: str,
        output_text: str,
        snapshot: Dict[str, Any],
        signals: List[Dict[str, Any]],
        score: Dict[str, Any],
    ) -> Dict[str, Any]:
        parsed = extract_json_object(output_text)
        valid, errors = self._validate_ai_result(parsed)
        return {
            "provider": "deepseek" if "deepseek" in base_url else "openai",
            "model": model,
            "ai_status": "closed",
            "content": output_text,
            "parsed": parsed,
            "valid_json": valid,
            "validation_errors": errors,
            "fallback": None if valid else self._local_analysis(snapshot, signals, score),
        }

    def analyze_with_ai(
        self,
        snapshot: Dict[str, Any],
        signals: List[Dict[str, Any]],
        score: Dict[str, Any],
    ) -> Dict[str, Any]:
        # AI模块只在触发信号后由run_once调用。
        # 三层稳健机制：请求重试、client重建、熔断后轻量探活自动恢复。
        if not self.ai_enabled:
            return self._local_analysis(snapshot, signals, score)

        if self.dry_run_ai:
            payload = self._ai_payload(snapshot, signals, score)
            return {
                "provider": "dry-run",
                "content": "AI dry-run enabled. Payload prepared but not sent.",
                "payload": payload,
            }

        try:
            from openai import OpenAI  # noqa: F401
        except ImportError:
            return {
                "provider": "local",
                "content": "openai package is not installed; fallback to local analysis.",
                "fallback": self._local_analysis(snapshot, signals, score),
            }

        api_key, base_url, model = self._ai_env_config()
        if not api_key:
            return {
                "provider": "local",
                "content": "AI_API_KEY or OPENAI_API_KEY is not configured; fallback to local analysis.",
                "fallback": self._local_analysis(snapshot, signals, score),
            }

        circuit_state = self._ai_circuit_state()
        if circuit_state == "open":
            self._maybe_probe_ai_connection(model)
            if self._ai_circuit_state() != "closed":
                return self._ai_fallback_result(
                    snapshot,
                    signals,
                    score,
                    "AI circuit open; using local analysis until probe succeeds.",
                    "circuit_open",
                )

        prompt = self._ai_prompt(snapshot, signals, score)
        try:
            client = self._get_ai_client(api_key, base_url)
            response = self._chat_completion_with_retry(client, model, prompt)
            output_text = response.choices[0].message.content
            self._record_ai_success()
            return self._build_ai_success_result(base_url, model, output_text, snapshot, signals, score)
        except Exception as exc:
            print(f"[{now_text()}] AI request failed: {exc}")
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
        score: Dict[str, Any],
        analysis: Dict[str, Any],
    ) -> None:
        # 推送拆成两类：
        # 1. trade：最终方向可交易，看final_trade_score；
        # 2. watch：最终观望但原始观察分高或风险异常，看raw_total_score和风险信号。
        # 这样不会因为final_trade_score=0漏掉资金费率过热、RSI极端、挤压突破等重要观察提醒。
        if not signals:
            return

        push_kind = self._push_kind(score, signals)
        if not push_kind:
            return

        push_key = self._push_key(snapshot, signals, score)
        if self._in_push_cooldown(push_key):
            print(f"[{now_text()}] push skipped by cooldown: {push_key}")
            return

        message = self._format_push_message(snapshot, signals, score, analysis, push_kind)
        print(message)
        if not self.push_enabled:
            self.last_push_at[push_key] = time.time()
            return

        send_key = os.getenv("WECHAT_SEND_KEY", "").strip()
        if not send_key:
            print(f"[{now_text()}] WeChat push skipped: WECHAT_SEND_KEY is not configured")
            self.last_push_at[push_key] = time.time()
            return

        self._push_wechat(send_key, snapshot, signals, score, analysis, push_kind)
        self.last_push_at[push_key] = time.time()

    def log_result(
        self,
        snapshot: Dict[str, Any],
        signals: List[Dict[str, Any]],
        score: Dict[str, Any],
        analysis: Dict[str, Any],
    ) -> None:
        # 每一轮都会写日志，即使不触发信号也记录。
        # 这些日志后续可以用于统计信号质量、优化阈值、做复盘。
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
            "oi_warmup_ready": snapshot["oi_warmup_ready"],
            "funding_rate": snapshot["funding_rate"],
            "funding_change": snapshot["funding_change"],
            "funding_warmup_ready": snapshot["funding_warmup_ready"],
            "long_short_ratio": snapshot["long_short_ratio"],
            "order_book": snapshot.get("order_book", {}),
            "trend_profiles": snapshot.get("trend_profiles", {}),
            "volatility": snapshot.get("volatility", {}),
            "dynamic_thresholds": snapshot.get("dynamic_thresholds", {}),
            "instrument_profile": snapshot.get("instrument_profile", {}),
            "market_context": snapshot.get("market_context", {}),
            "signal_tracking": snapshot.get("signal_tracking", {}),
            "signals": signals,
            "score": score,
            "analysis": analysis,
        }
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
        snapshot["signal_tracking"] = self.update_signal_tracking(snapshot, signals, score)
        analysis = self.analyze_with_ai(snapshot, signals, score) if signals else self._local_analysis(snapshot, signals, score)
        print(f"[{snapshot['time']}] {inst_id} analysis by {analysis.get('provider')}: {analysis.get('content')}")
        if not self.replay_mode:
            self.push_if_needed(snapshot, signals, score, analysis)
        self.log_result(snapshot, signals, score, analysis)

    def run_replay(self, frames: List[Dict[str, Any]], replay_interval: float = 0.0) -> None:
        self.replay_mode = True
        if self.replay_log_file == LOG_FILE:
            self.replay_log_file = REPLAY_LOG_FILE
        self.replay_log_file.parent.mkdir(parents=True, exist_ok=True)
        total = len(frames)
        print(f"[{now_text()}] replay start: {total} frames -> {self.replay_log_file}")
        for index, frame in enumerate(frames, start=1):
            inst_id = str(frame.get("inst_id", ""))
            if inst_id not in self.instruments:
                print(f"[{now_text()}] replay skip unknown inst_id={inst_id}")
                continue
            self.replay_frame = frame
            self._set_replay_clock(str(frame.get("time", "")))
            try:
                self._process_inst(inst_id)
            except Exception as exc:
                print(f"[{self._now_text()}] replay frame {index}/{total} failed: {exc}")
            finally:
                self.replay_frame = None
            if replay_interval > 0 and index < total:
                time.sleep(replay_interval)
        print(f"[{now_text()}] replay finished: {total} frames")

    def run_once(self) -> None:
        # 定时执行：遍历所有支持币种，完成数据采集、阈值检测、综合评分、AI分析、微信推送、日志存储等全部功能。
        for inst_id in self.instruments:
            try:
                self._process_inst(inst_id)
            except Exception as exc:
                print(f"[{self._now_text()}] {inst_id} collect/analyze failed: {exc}")

    def run_forever(self, runtime: int) -> None:
        # 主循环：默认永久运行；runtime>0时，到指定秒数自动退出，用于实现定时任务。
        started = time.time()
        while True:
            self.run_once()
            if runtime > 0 and time.time() - started >= runtime:
                print(f"Runtime {runtime}s reached; exit.")
                break
            time.sleep(self.interval)

    def update_signal_tracking(
        self,
        snapshot: Dict[str, Any],
        signals: List[Dict[str, Any]],
        score: Dict[str, Any],
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
        direction = score.get("direction")
        if direction in ("做多", "做空") and price > 0 and (signals or score.get("total_score", 0) >= self.push_score):
            signal_types = ",".join(sorted(item.get("type", "") for item in signals if item.get("type"))) or "score-only"
            strategy = snapshot.get("market_context", {}).get("strategy_template", "unknown")
            track_key = f"{inst_id}:{direction}:{strategy}:{signal_types}"
            # 同一类信号最多每60秒登记一次，避免5秒轮询制造大量高度重复样本。
            if now_ts - self.last_signal_track_at.get(track_key, 0.0) >= 60:
                self.last_signal_track_at[track_key] = now_ts
                entry_low, entry_high = self._entry_bounds(score.get("entry_plan", {}).get("entry", ""))
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
                        "planned_entry": score.get("entry"),
                        "score": score.get("total_score"),
                        "market_regime": score.get("market_regime"),
                        "strategy_template": strategy,
                        "signal_types": signal_types,
                        "layer_scores": score.get("layer_scores", {}),
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
            print(f"load signal performance failed: {exc}")

    def _append_signal_performance(self, item: Dict[str, Any]) -> None:
        # 单独持久化结算样本，避免程序重启后只剩内存统计。
        # 这份JSONL可以后续离线聚合、调权重、对比不同信号组合。
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            self._rotate_signal_performance_if_needed()
            with SIGNAL_PERFORMANCE_FILE.open("a", encoding="utf-8") as file:
                file.write(json.dumps(item, ensure_ascii=False) + "\n")
        except Exception as exc:
            print(f"signal performance log failed: {exc}")

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
        cached = self.cache.get(key)
        if cached and time.time() - cached[0] < ttl_seconds:
            return cached[1]
        value = loader()
        self.cache[key] = (time.time(), value)
        return value

    def _okx_call(self, label: str, func: Any) -> Any:
        # OKX HTTP调用统一套重试，降低临时网络故障影响。
        return retry_call(
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
                print(f"[{now_text()}] {label} SDK failed, fallback to REST: {exc}")
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

        # API返回的是数组,你可以一次性查多个币种，一个数组返回所有结果，这里取第一个，因为只查一个。
        item = data[0] if data else {}
        return {
            "last": to_float(item.get("last")),     #成交价
            "bid_px": to_float(item.get("bidPx")),  #买入挂单价
            "ask_px": to_float(item.get("askPx")),  #卖出挂单价
        }

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
        item = data[0] if data else {}
        return to_float(item.get("oi")) or to_float(item.get("oiCcy"))

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

        # API返回的是数组，可以同时获取多个币种的资金费率，这里取第一个
        item = data[0] if data else {}
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

    def _volume_stats(self, candles_1m: List[Dict[str, Any]]) -> Dict[str, float]:
        # 成交量判断改为优先使用“最近已收盘1m K线”。
        # 如果直接使用未收盘K线，开盘几秒会低估，临近收盘又可能突然误报，导致放量信号不稳定。

        # 过滤KLINE_LIMIT根中的已收盘K线
        rows = confirmed_candles(candles_1m)

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
            "source": "confirmed_1m",
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

    def _history_values(self, history: Deque[Tuple[float, float]]) -> List[float]:
        # 只取数值部分，供动态阈值和分位数计算使用。
        return [value for _, value in history]

    def _dynamic_thresholds(self, inst_id: str) -> Dict[str, Any]:
        # 动态阈值用于解决“固定阈值在不同币种、不同时段不适配”的问题。
        # 例如BTC美盘高波动时2倍放量可能很常见，低波动亚洲盘1.5倍就很值得注意。
        # 为保持兼容，最终触发仍会参考用户配置，动态阈值作为更可靠的市场自适应参考。
        profile = self._instrument_profile(inst_id)
        volume_values = self._history_values(self.volume_multiplier_history[inst_id])
        atr_values = self._history_values(self.atr_pct_history[inst_id])
        book_values = [abs(value) for value in self._history_values(self.book_imbalance_history[inst_id])]
        return {
            "volume_multiplier_p85": percentile(volume_values, 0.85, max(self.config.volume_multiplier, profile["volume_multiplier_floor"])),
            "volume_multiplier_p95": percentile(volume_values, 0.95, max(self.config.volume_multiplier * 1.5, profile["volume_multiplier_floor"] * 1.4)),
            "atr_pct_p80": percentile(atr_values, 0.80, profile["atr_pct_normal"]),
            "book_imbalance_p85": percentile(book_values, 0.85, 0.35),
            "instrument_profile": profile,
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
        atr_pct_15m = to_float(profiles.get("15m", {}).get("atr_pct"))
        atr_pct_1h = to_float(profiles.get("1H", {}).get("atr_pct"))
        historical = self._history_values(self.atr_pct_history[inst_id])
        p80 = percentile(historical, 0.80, atr_pct_15m)
        if historical and atr_pct_15m >= p80 and atr_pct_15m > 0:
            regime = "high_volatility"
        elif atr_pct_15m < 0.08:
            regime = "low_volatility"
        else:
            regime = "normal"
        return {
            "regime": regime,
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
    ) -> Dict[str, Any]:
        # 市场上下文是新版判断的核心：先判断市场处于什么状态，再谈方向和入场。
        # 这样可以避免“放量就做多/做空”“多周期多数上涨就追多”的简单规则。
        trend_votes = {
            "entry": [profiles.get("1m", {}).get("trend"), profiles.get("3m", {}).get("trend")],
            "trade": [profiles.get("5m", {}).get("trend"), profiles.get("15m", {}).get("trend")],
            "higher": [profiles.get("1H", {}).get("trend"), profiles.get("4H", {}).get("trend")],
        }
        up_count = sum(1 for group in trend_votes.values() for item in group if item == "up")
        down_count = sum(1 for group in trend_votes.values() for item in group if item == "down")
        range_count = sum(1 for group in trend_votes.values() for item in group if item in ("range", "mixed"))

        higher_up = trend_votes["higher"].count("up")
        higher_down = trend_votes["higher"].count("down")
        trade_up = trend_votes["trade"].count("up")
        trade_down = trend_votes["trade"].count("down")
        entry_up = trend_votes["entry"].count("up")
        entry_down = trend_votes["entry"].count("down")
        adx_15m = to_float(profiles.get("15m", {}).get("adx", {}).get("adx"))
        boll_width = to_float(profiles.get("15m", {}).get("boll", {}).get("bandwidth_pct"))
        macd_15m = profiles.get("15m", {}).get("macd", {})
        rsi_15m = to_float(profiles.get("15m", {}).get("rsi", {}).get("14"), 50.0)
        squeeze = boll_width > 0 and boll_width < max(0.35, volatility.get("atr_pct_15m", 0.0) * 1.4)
        move_5m = self._recent_move_pct(candles.get("1m", []), 5)
        move_10m = self._recent_move_pct(candles.get("1m", []), 10)
        move_15m = self._recent_move_pct(candles.get("1m", []), 15)
        recent_price_pressure = self._recent_price_pressure(move_5m, move_10m, move_15m, volatility)

        long_confirmed = higher_up >= 1 and up_count > down_count and (
            trade_up >= 2 or (trade_up >= 1 and recent_price_pressure == "up" and entry_up >= 1)
        )
        short_confirmed = higher_down >= 1 and down_count > up_count and (
            trade_down >= 2 or (trade_down >= 1 and recent_price_pressure == "down" and entry_down >= 1)
        )

        if squeeze and adx_15m < 18:
            bias = "neutral"
            regime = "squeeze"
        elif long_confirmed:
            bias = "long"
            regime = "trend_up"
        elif short_confirmed:
            bias = "short"
            regime = "trend_down"
        elif range_count >= 3:
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
        if bias == "long" and recent_price_pressure == "down":
            regime = "mixed"
            bias = "neutral"
        elif bias == "short" and recent_price_pressure == "up":
            regime = "mixed"
            bias = "neutral"

        recent_15m = confirmed_candles(candles.get("15m", []))
        old_close = to_float(recent_15m[min(4, len(recent_15m) - 1)].get("close")) if recent_15m else price
        price_change_15m = pct_change(price, old_close)
        oi_price_state = self._oi_price_state(price_change_15m, oi_change_pct_15m)
        volume_threshold = max(self.config.volume_multiplier, dynamic_thresholds.get("volume_multiplier_p85", 0.0))
        order_book_bias = "neutral"
        if order_book.get("available"):
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
        if profiles.get("15m", {}).get("divergence") in ("bearish", "bullish"):
            warnings.append(f"15m RSI出现{profiles.get('15m', {}).get('divergence')}背离")
        if abs(to_float(macd_15m.get("hist_slope"))) < abs(to_float(macd_15m.get("hist"))) * 0.15 and abs(to_float(macd_15m.get("hist"))) > 0:
            warnings.append("MACD柱体变化放缓，动能可能衰减")
        if rsi_15m > 78 or rsi_15m < 22:
            warnings.append("15m RSI处于极端区域，追单风险升高")
        if abs(funding_rate) >= self.config.funding_abs_threshold:
            warnings.append("资金费率过热，单边拥挤风险升高")
        if max(long_short.get("long_ratio", 0.0), long_short.get("short_ratio", 0.0)) >= self.config.long_short_extreme:
            warnings.append("多空账户占比极端，需防止拥挤反转")
        if volume["multiplier"] < volume_threshold and profiles.get("15m", {}).get("breakout") != "none":
            warnings.append("结构突破缺少放量确认")

        return {
            "regime": regime,
            "bias": bias,
            "trend_votes": trend_votes,
            "up_count": up_count,
            "down_count": down_count,
            "entry_up": entry_up,
            "entry_down": entry_down,
            "trade_up": trade_up,
            "trade_down": trade_down,
            "recent_price_pressure": recent_price_pressure,
            "recent_move_pct": {
                "5m": move_5m,
                "10m": move_10m,
                "15m": move_15m,
            },
            "price_change_15m": price_change_15m,
            "oi_price_state": oi_price_state,
            "volume_threshold_used": volume_threshold,
            "order_book_bias": order_book_bias,
            "strategy_template": self._strategy_template(regime, bias),
            "warnings": warnings,
        }

    def _strategy_template(self, regime: str, bias: str) -> str:
        # 不同市场状态应该使用不同交易模板。这个字段给AI、本地分析和后续回测归因使用。
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
        if entry_plan.get("quality") == "breakout_valid" and final_trade_score >= self.push_score:
            return "可关注"
        if final_trade_score >= 70:
            return "等待确认"
        return "不建议"

    def _push_kind(self, score: Dict[str, Any], signals: List[Dict[str, Any]]) -> str:
        signal_types = {item.get("type") for item in signals}
        scalp_view = score.get("strategy_views", {}).get("scalp", {})
        if self.config.signal_spike_enabled and scalp_view.get("action_level") in ("急速异动", "可短打") and scalp_view.get("score", 0) >= self.config.watch_push_score:
            return "spike"
        if self.config.signal_trade_enabled and score.get("final_trade_score", 0) >= self.push_score and score.get("direction") in ("做多", "做空"):
            return "trade"
        watch_signals = {"funding_hot", "rsi_extreme", "rsi_divergence", "boll_squeeze", "long_short_extreme"}
        if self.config.signal_watch_enabled and score.get("raw_total_score", 0) >= self.config.watch_push_score and signal_types.intersection(watch_signals):
            return "watch"
        return ""

    def _validate_ai_result(self, parsed: Optional[Dict[str, Any]]) -> Tuple[bool, List[str]]:
        # AI输出必须包含固定字段，否则不直接信任，转用本地规则兜底。
        required = {
            "trend",
            "risk",
            "suggestion",
            "direction",
            "entry",
            "stop_loss",
            "take_profit",
            "risk_level",
            "score_comment",
            "rule_audit",
            "reasons",
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

        if not isinstance(parsed.get("reasons"), list):
            errors.append("reasons must be a list")

        rule_audit = parsed.get("rule_audit")
        if not isinstance(rule_audit, dict):
            errors.append("rule_audit must be an object")
        else:
            for key in ("overall", "score_consistency", "warnings"):
                if key not in rule_audit:
                    errors.append(f"rule_audit missing {key}")

        return not errors, errors

    def _ai_payload(
        self,
        snapshot: Dict[str, Any],
        signals: List[Dict[str, Any]],
        score: Dict[str, Any],
    ) -> Dict[str, Any]:
        # 发送给AI的数据分成三层：
        # 1. raw_evidence：原始证据，让AI能复核趋势、放量、OI/funding变化。
        # 2. computed_indicators：程序已经算好的指标。
        # 3. rule_outputs：程序触发的信号和评分，让AI审查是否可信。
        inst_id = snapshot["inst_id"]
        return {
            "instrument": inst_id,
            "snapshot_time": snapshot["time"],
            "raw_evidence": {
                "market": {
                    "current_price": snapshot["price"],
                    "best_bid": snapshot["best_bid"],
                    "best_ask": snapshot["best_ask"],
                },
                "candles": {
                    "1m": compact_candles(snapshot["candles"]["1m"], 21),
                    "3m": compact_candles(snapshot["candles"].get("3m", []), 40),
                    "5m": compact_candles(snapshot["candles"]["5m"], 12),
                    "15m": compact_candles(snapshot["candles"]["15m"], 12),
                    "1H": compact_candles(snapshot["candles"]["1H"], 12),
                    "4H": compact_candles(snapshot["candles"].get("4H", []), 12),
                },
                "oi_history": history_tail(self.oi_history[inst_id], 180),
                "funding_history": history_tail(self.funding_history[inst_id], 180),
            },
            "computed_indicators": {
                "volume": snapshot["volume"],
                "open_interest": snapshot["open_interest"],
                "oi_change_pct_15m": snapshot["oi_change_pct_15m"],
                "oi_warmup_ready": snapshot["oi_warmup_ready"],
                "funding_rate": snapshot["funding_rate"],
                "funding_change_15m": snapshot["funding_change"],
                "funding_warmup_ready": snapshot["funding_warmup_ready"],
                "long_short_ratio": snapshot["long_short_ratio"],
                "order_book": snapshot.get("order_book", {}),
                "trend_profiles": snapshot.get("trend_profiles", {}),
                "volatility": snapshot.get("volatility", {}),
                "dynamic_thresholds": snapshot.get("dynamic_thresholds", {}),
                "instrument_profile": snapshot.get("instrument_profile", {}),
                "market_context": snapshot.get("market_context", {}),
                "signal_tracking": snapshot.get("signal_tracking", {}),
            },
            "rule_thresholds": {
                "volume_multiplier": self.config.volume_multiplier,
                "oi_change_pct_15m": self.config.oi_change_pct_15m,
                "funding_abs_threshold": self.config.funding_abs_threshold,
                "funding_change_threshold": self.config.funding_change_threshold,
                "long_short_extreme": self.config.long_short_extreme,
                "push_score": self.push_score,
                "watch_push_score": self.config.watch_push_score,
                "strategy_mode": self._strategy_mode(),
                "risk_preference": self._risk_preference(),
                "signal_types_enabled": {
                    "trade": self.config.signal_trade_enabled,
                    "watch": self.config.signal_watch_enabled,
                    "spike": self.config.signal_spike_enabled,
                },
            },
            "rule_outputs": {
                "signals": signals,
                "score": score,
                "direction_audit": {
                    "raw_direction": score.get("raw_direction"),
                    "final_direction": score.get("final_direction"),
                    "downgraded_to_wait": score.get("raw_direction") != score.get("final_direction"),
                },
                "signal_evidence": self._signal_evidence(snapshot, signals),
            },
        }

    def _signal_evidence(
        self,
        snapshot: Dict[str, Any],
        signals: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        # 把每个信号的“当前值 vs 阈值”写清楚，AI可以据此审查信号是否合理。
        evidence = []
        signal_types = {item["type"] for item in signals}

        if "volume_spike" in signal_types:
            evidence.append({
                "type": "volume_spike",
                "current": snapshot["volume"]["multiplier"],
                "threshold": snapshot.get("market_context", {}).get("volume_threshold_used", self.config.volume_multiplier),
                "valid_by_rule": snapshot["volume"]["multiplier"] >= snapshot.get("market_context", {}).get("volume_threshold_used", self.config.volume_multiplier),
                "detail": snapshot["volume"],
            })

        if "structure_break" in signal_types:
            evidence.append({
                "type": "structure_break",
                "profiles": {
                    "5m": snapshot.get("trend_profiles", {}).get("5m", {}),
                    "15m": snapshot.get("trend_profiles", {}).get("15m", {}),
                },
                "valid_by_rule": snapshot.get("trend_profiles", {}).get("5m", {}).get("breakout") in ("up", "down")
                or snapshot.get("trend_profiles", {}).get("15m", {}).get("breakout") in ("up", "down"),
            })

        if "boll_squeeze" in signal_types:
            evidence.append({
                "type": "boll_squeeze",
                "boll_15m": snapshot.get("trend_profiles", {}).get("15m", {}).get("boll", {}),
                "adx_15m": snapshot.get("trend_profiles", {}).get("15m", {}).get("adx", {}),
                "valid_by_rule": snapshot.get("market_context", {}).get("regime") == "squeeze",
            })

        if "rsi_divergence" in signal_types or "rsi_extreme" in signal_types:
            evidence.append({
                "type": "rsi_state",
                "rsi_15m": snapshot.get("trend_profiles", {}).get("15m", {}).get("rsi", {}),
                "divergence_15m": snapshot.get("trend_profiles", {}).get("15m", {}).get("divergence"),
                "valid_by_rule": True,
            })

        if "macd_momentum_change" in signal_types:
            evidence.append({
                "type": "macd_momentum_change",
                "macd_15m": snapshot.get("trend_profiles", {}).get("15m", {}).get("macd", {}),
                "valid_by_rule": True,
            })

        if "oi_change" in signal_types:
            evidence.append({
                "type": "oi_change",
                "current": snapshot["oi_change_pct_15m"],
                "threshold": self.config.oi_change_pct_15m,
                "warmup_ready": snapshot["oi_warmup_ready"],
                "valid_by_rule": snapshot["oi_warmup_ready"] and abs(snapshot["oi_change_pct_15m"]) >= self.config.oi_change_pct_15m,
            })

        if "funding_hot" in signal_types:
            evidence.append({
                "type": "funding_hot",
                "current": snapshot["funding_rate"],
                "threshold": self.config.funding_abs_threshold,
                "valid_by_rule": abs(snapshot["funding_rate"]) >= self.config.funding_abs_threshold,
            })

        if "funding_fast_change" in signal_types:
            evidence.append({
                "type": "funding_fast_change",
                "current": snapshot["funding_change"],
                "threshold": self.config.funding_change_threshold,
                "warmup_ready": snapshot["funding_warmup_ready"],
                "valid_by_rule": snapshot["funding_warmup_ready"] and abs(snapshot["funding_change"]) >= self.config.funding_change_threshold,
            })

        if "long_short_extreme" in signal_types:
            long_short = snapshot["long_short_ratio"]
            evidence.append({
                "type": "long_short_extreme",
                "current_long_ratio": long_short.get("long_ratio", 0.0),
                "current_short_ratio": long_short.get("short_ratio", 0.0),
                "threshold": self.config.long_short_extreme,
                "available": long_short.get("available", False),
                "valid_by_rule": long_short.get("available", False) and max(
                    long_short.get("long_ratio", 0.0),
                    long_short.get("short_ratio", 0.0),
                ) >= self.config.long_short_extreme,
            })

        if "order_book_imbalance" in signal_types:
            evidence.append({
                "type": "order_book_imbalance",
                "current": snapshot.get("order_book", {}).get("imbalance", 0.0),
                "threshold": max(0.35, snapshot.get("dynamic_thresholds", {}).get("book_imbalance_p85", 0.35)),
                "detail": snapshot.get("order_book", {}),
                "valid_by_rule": snapshot.get("order_book", {}).get("available", False),
            })

        return evidence

    def _ai_prompt(
        self,
        snapshot: Dict[str, Any],
        signals: List[Dict[str, Any]],
        score: Dict[str, Any],
    ) -> str:
        # Prompt明确约束AI：只做分析，不自动下单，不承诺收益，并要求固定字段。
        payload = self._ai_payload(snapshot, signals, score)
        return (
            "你是欧易OKX USDT永续合约多策略分析助手，只分析BTC-USDT-SWAP和ETH-USDT-SWAP。"
            # "你的任务是根据实时行情、多周期K线、EMA/MA/RSI/MACD/KDJ/BOLL/ADX/ATR结构画像、成交量、OI、资金费率、多空比、订单簿和本地规则评分，"
            "你的任务是根据实时行情、多周期K线、EMA/MA/RSI/MACD/KDJ/BOLL/ADX/ATR结构画像、成交量、OI、资金费率、多空比、订单簿，"
            ""
            # "先审计程序规则结果是否可信，再输出短线交易观察建议。\n\n"
            "直接忽略本地规则，独立根据原始数据和规则触发的信号来判断市场状态和交易机会。\n\n"
            "硬性限制：\n"
            "1. 只提供分析和风险提示，不允许表示系统会自动下单。\n"
            "2. 不允许承诺收益，不允许使用稳赚、必涨、必跌等确定性表述。\n"
            "3. 如果数据不足、信号矛盾、预热未完成或风险过高，direction必须选择观望。\n"
            "4. 如果long_short_ratio.available=false，需要说明多空比不可用，不要凭空编造多空比。\n"
            "5. 如果oi_warmup_ready=false或funding_warmup_ready=false，需要降低对15分钟变化类指标的权重。\n\n"
            f"策略要求：当前主策略为{score.get('strategy_label')}，风险偏好为{score.get('risk_preference')}，AI输出风格为{score.get('ai_output_style')}。"
            "必须优先按照rule_outputs.score.selected_strategy_view给出主结论，同时参考strategy_views里的超短线、短线、中线并行视角解释差异。"
            "超短线只看1m/3m/5m与15m过滤，4H只做背景；短线看5m/15m和1H确认；中线看1H/4H结构。"
            "如果超短线出现急速异动但主策略不是超短线，应说明这是短打机会或风险提示，不要把它包装成中线趋势。\n\n"
            "分析步骤：\n"
            "1. 规则审计：根据raw_evidence和signal_evidence，判断程序触发的signals是否有原始数据支持。\n"
            "2. 市场状态：优先参考market_context.regime/bias，区分趋势、震荡、高波动、混合状态。\n"
            "3. 趋势：根据K线原始数据、EMA9/20/60/120、MA120、ADX和结构高低点判断1m、3m、5m、15m、1H、4H方向。\n"
            "4. 动量：用RSI、MACD柱体变化、KDJ和K线实体质量判断是否过热、背离或动能衰减。\n"
            "5. 量能：复核已收盘1m成交量、量价方向、动态分位数和突破放量确认。\n"
            "6. 合约资金：根据OI+价格组合、资金费率、多空比判断新增仓、平仓和拥挤风险。\n"
            "7. 订单簿：只作为入场确认，不允许单独用盘口不平衡决定方向；注意top5/top20分歧和价差。\n"
            "8. 回测反馈：signal_tracking只是在线复盘统计，样本少时不能过度依赖。\n"
            "9. 风险：结合ATR止损距离、资金费率、趋势冲突、信号数量、预热状态给出风险等级。\n"
            "10. 建议：方向只能是做多、做空、观望。入场、止损、止盈优先参考rule_outputs.score.entry_plan，并可保守修正。\n\n"
            "必须只输出一个合法JSON对象，不要输出Markdown代码块，不要输出JSON以外的解释。"
            "JSON字段必须完全包含：\n"
            "{\n"
            '  "trend": {\n'
            '    "summary": "一句话趋势结论",\n'
            '    "timeframes": {"1m": "...", "3m": "...", "5m": "...", "15m": "...", "1H": "...", "4H": "..."},\n'
            '    "conflict": "是否存在周期冲突"\n'
            "  },\n"
            '  "risk": "风险分析，说明资金费率/OI/多空比/预热状态",\n'
            '  "suggestion": "简短交易建议和执行注意事项",\n'
            '  "direction": "做多/做空/观望",\n'
            '  "entry": "建议入场区间，观望时填-",\n'
            '  "stop_loss": "建议止损，观望时填-",\n'
            '  "take_profit": "建议止盈，观望时填-",\n'
            '  "risk_level": "低/中/高",\n'
            '  "score_comment": "对本地综合评分是否可信的说明",\n'
            '  "rule_audit": {\n'
            '    "overall": "规则结果可信/部分可信/不可信",\n'
            '    "volume_signal_valid": true,\n'
            '    "oi_signal_valid": true,\n'
            '    "funding_signal_valid": true,\n'
            '    "long_short_signal_valid": true,\n'
            '    "score_consistency": "综合评分与原始证据是否一致",\n'
            '    "warnings": ["规则审计警告"]\n'
            "  },\n"
            '  "reasons": ["理由1", "理由2", "理由3"]\n'
            "}\n\n"
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

    def _resolve_push_analysis(
        self,
        analysis: Dict[str, Any],
        score: Dict[str, Any],
    ) -> Dict[str, Any]:
        # 推送优先使用 AI 有效 JSON；无效时回退 fallback/local-rule，再补 score 字段。
        payload: Optional[Dict[str, Any]] = None
        if analysis.get("valid_json") and isinstance(analysis.get("parsed"), dict):
            payload = analysis["parsed"]
        elif isinstance(analysis.get("fallback"), dict) and isinstance(analysis["fallback"].get("content"), dict):
            payload = analysis["fallback"]["content"]
        elif analysis.get("provider") == "local-rule" and isinstance(analysis.get("content"), dict):
            payload = analysis["content"]

        trend = payload.get("trend") if isinstance(payload, dict) and isinstance(payload.get("trend"), dict) else {}
        rule_audit = payload.get("rule_audit") if isinstance(payload, dict) and isinstance(payload.get("rule_audit"), dict) else {}
        reasons = payload.get("reasons") if isinstance(payload, dict) and isinstance(payload.get("reasons"), list) else []

        def pick(field: str, score_key: Optional[str] = None) -> Any:
            score_key = score_key or field
            if isinstance(payload, dict):
                value = payload.get(field)
                if value not in (None, ""):
                    return value
            return score.get(score_key)

        direction = display_push_value(pick("direction", "direction"), score.get("direction", "观望"))
        return {
            "source": analysis.get("provider", "unknown"),
            "ai_valid": bool(analysis.get("valid_json")),
            "direction": direction,
            "rule_direction": score.get("direction", "观望"),
            "entry": display_push_value(pick("entry", "entry"), score.get("entry")),
            "stop_loss": display_push_value(pick("stop_loss", "stop_loss"), score.get("stop_loss")),
            "take_profit": display_push_value(pick("take_profit", "take_profit"), score.get("take_profit")),
            "risk_level": display_push_value(pick("risk_level", "risk_level"), score.get("risk_level")),
            "suggestion": clip_push_text(pick("suggestion"), 240),
            "risk": clip_push_text(pick("risk"), 260),
            "score_comment": clip_push_text(pick("score_comment"), 180),
            "trend_summary": clip_push_text(trend.get("summary"), 120),
            "trend_conflict": clip_push_text(trend.get("conflict"), 120),
            "rule_audit_overall": clip_push_text(rule_audit.get("overall"), 40),
            "rule_audit_warnings": [
                clip_push_text(item, 80)
                for item in (rule_audit.get("warnings") or [])[:2]
                if clip_push_text(item, 80)
            ],
            "reasons": [clip_push_text(item, 100) for item in reasons[:3] if clip_push_text(item, 100)],
        }

    def _signal_labels(self, signals: List[Dict[str, Any]], limit: int = 4) -> List[str]:
        labels = []
        for item in signals:
            signal_type = str(item.get("type", "")).strip()
            if not signal_type:
                continue
            labels.append(SIGNAL_TYPE_LABELS.get(signal_type, signal_type))
        return labels[:limit]

    def _build_wechat_push_content(
        self,
        snapshot: Dict[str, Any],
        signals: List[Dict[str, Any]],
        score: Dict[str, Any],
        analysis: Dict[str, Any],
        push_kind: str = "trade",
    ) -> Tuple[str, str]:
        view = self._resolve_push_analysis(analysis, score)
        inst_id = snapshot["inst_id"]
        symbol = symbol_ccy(inst_id)
        price = snapshot["price"]
        signal_labels = self._signal_labels(signals)
        signal_text = "、".join(signal_labels) or "规则评分达标"
        raw_score = score.get("raw_total_score", score.get("total_score", "-"))
        trade_score = score.get("final_trade_score", score.get("total_score", "-"))

        title_parts = [
            symbol,
            view["direction"],
            f"险{view['risk_level']}",
            f"{raw_score}分",
        ]
        if signal_labels:
            title_parts.append(signal_labels[0])
        title = " ".join(part for part in title_parts if part and part != "-")
        if push_kind == "watch":
            title = f"[观察] {title}"
        elif push_kind == "spike":
            title = f"[急速异动] {title}"

        lines = [
            f"## {inst_id} · {snapshot.get('time', now_text())}",
            "",
            f"**AI结论**：{view['direction']} | 风险 {view['risk_level']} | 来源 {view['source']}",
            f"**价格**：{price} | 观察分/交易分 {raw_score}/{trade_score}",
            f"**主策略**：{score.get('strategy_label', '-')} | 风险偏好 {score.get('risk_preference', '-')}",
        ]
        selected_view = score.get("selected_strategy_view", {})
        if selected_view.get("summary"):
            lines.append(f"**主策略视角**：{selected_view.get('summary')}")
        scalp_view = score.get("strategy_views", {}).get("scalp", {})
        if scalp_view.get("action_level") in ("急速异动", "可短打"):
            lines.append(f"**超短线提醒**：{scalp_view.get('direction')} / {scalp_view.get('action_level')} / {scalp_view.get('score')}分；{scalp_view.get('summary')}")
        if view["direction"] != view["rule_direction"]:
            lines.append(f"**规则方向**：{view['rule_direction']}（与AI不一致，推送以AI为准）")
        if view["trend_summary"] != "-":
            lines.append(f"**趋势**：{view['trend_summary']}")
        if view["trend_conflict"] not in ("-", "none", "无", "否"):
            lines.append(f"**周期冲突**：{view['trend_conflict']}")

        lines.extend(["", "**交易计划**"])
        if view["direction"] == "观望" and view["entry"] == "-" and view["stop_loss"] == "-" and view["take_profit"] == "-":
            lines.append("- 当前建议观望，暂不给出入场/止损/止盈")
        else:
            lines.append(f"- 入场：{view['entry']}")
            lines.append(f"- 止损：{view['stop_loss']}")
            lines.append(f"- 止盈：{view['take_profit']}")

        if view["suggestion"] != "-":
            lines.extend(["", f"**执行建议**：{view['suggestion']}"])

        if view["reasons"]:
            lines.extend(["", "**核心依据**"])
            for index, reason in enumerate(view["reasons"], start=1):
                lines.append(f"{index}. {reason}")

        if view["risk"] != "-":
            lines.extend(["", f"**风险提示**：{view['risk']}"])

        if view["score_comment"] != "-":
            lines.append(f"**评分说明**：{view['score_comment']}")

        audit_bits = []
        if view["rule_audit_overall"] != "-":
            audit_bits.append(view["rule_audit_overall"])
        audit_bits.extend(view["rule_audit_warnings"])
        if audit_bits:
            lines.extend(["", f"**规则审计**：{'；'.join(audit_bits)}"])

        lines.extend([
            "",
            f"**触发信号**：{signal_text}",
            f"**市场状态**：{score.get('market_regime', 'unknown')} / {score.get('bias', 'neutral')}",
            f"**交易动作**：{score.get('trade_action_level', '-')}",
            "",
            "仅供观察，不构成投资建议。",
        ])
        return title[:120], "\n".join(lines)

    def _format_push_message(
        self,
        snapshot: Dict[str, Any],
        signals: List[Dict[str, Any]],
        score: Dict[str, Any],
        analysis: Dict[str, Any],
        push_kind: str = "trade",
    ) -> str:
        title, desp = self._build_wechat_push_content(snapshot, signals, score, analysis, push_kind)
        return f"[OKX AI短线助手][{push_kind}] {title}\n\n{desp}"

    def _push_key(
        self,
        snapshot: Dict[str, Any],
        signals: List[Dict[str, Any]],
        score: Dict[str, Any],
    ) -> str:
        # 同币种、同方向、同信号组合视为同一类提醒，进入冷却窗口。
        signal_types = ",".join(sorted(item["type"] for item in signals)) or "score-only"
        push_kind = self._push_kind(score, signals) or "none"
        return f"{push_kind}:{snapshot['inst_id']}:{score['direction']}:{signal_types}"

    def _in_push_cooldown(self, push_key: str) -> bool:
        last_at = self.last_push_at.get(push_key, 0.0)
        return time.time() - last_at < self.runtime_config.push_cooldown_seconds

    def _push_wechat(
        self,
        send_key: str,
        snapshot: Dict[str, Any],
        signals: List[Dict[str, Any]],
        score: Dict[str, Any],
        analysis: Dict[str, Any],
        push_kind: str = "trade",
    ) -> None:
        # Server酱推送至个人微信。正文优先展示 AI 分析结论，规则分数仅作参考。
        title, desp = self._build_wechat_push_content(snapshot, signals, score, analysis, push_kind)
        try:
            http_post_json(
                f"https://sctapi.ftqq.com/{send_key}.send",
                {"title": title, "desp": desp},
                self.runtime_config.retry_times,
                self.runtime_config.retry_backoff,
            )
        except Exception as exc:
            print(f"WeChat push failed: {exc}")

    def _rotate_log_if_needed(self) -> None:
        # 简单日志轮转：超过大小就把当前日志替换为 .1。
        # 生产版可以接入logrotate或保留多份历史文件。
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            if not LOG_FILE.exists() or LOG_FILE.stat().st_size < self.runtime_config.log_max_bytes:
                return
            backup = LOG_FILE.with_suffix(LOG_FILE.suffix + ".1")
            if backup.exists():
                backup.unlink()
            LOG_FILE.replace(backup)
        except Exception as exc:
            print(f"log rotation failed: {exc}")

    def _print_console(
        self,
        snapshot: Dict[str, Any],
        signals: List[Dict[str, Any]],
        score: Dict[str, Any],
        analysis: Dict[str, Any],
    ) -> None:
        # 控制台输出每轮核心摘要，方便命令行观察运行效果。
        signal_text = ", ".join(item["desc"] for item in signals) if signals else "none"
        print(
            f"[{snapshot['time']}] {snapshot['inst_id']} "
            f"price={snapshot['price']} score={score['total_score']} "
            f"dir={score['direction']} risk={score['risk_level']} signals={signal_text}"
        )
        if signals:
            print(f"analysis={json.dumps(analysis, ensure_ascii=False)}")


def short_count_greater(trends: Dict[str, str]) -> bool:
    # 判断多周期中下跌周期是否多于上涨周期。
    return sum(1 for item in trends.values() if item == "down") > sum(1 for item in trends.values() if item == "up")


def parse_instruments(value: str) -> List[str]:
    # V1只允许客户需求中的两个永续合约，避免误传现货或其他合约。
    instruments = [item.strip().upper() for item in value.split(",") if item.strip()]
    unsupported = [item for item in instruments if item not in SUPPORTED_INSTRUMENTS]
    if unsupported:
        raise argparse.ArgumentTypeError(f"Unsupported instruments: {unsupported}. V1 only supports {SUPPORTED_INSTRUMENTS}")
    return instruments or list(SUPPORTED_INSTRUMENTS)


def parse_args() -> argparse.Namespace:
    # 命令行参数 > 环境变量 > 程序默认。
    parser = argparse.ArgumentParser(description="OKX AI short-term trading assistant V1")
    parser.add_argument(
        "--inst-ids",
        type=parse_instruments,
        default=parse_instruments(os.getenv("OKX_INST_IDS", ",".join(SUPPORTED_INSTRUMENTS))),
        help="Comma-separated instruments. V1: BTC-USDT-SWAP,ETH-USDT-SWAP",
    )
    parser.add_argument("--interval", type=int, default=int(os.getenv("OKX_INTERVAL", str(DEFAULT_INTERVAL_SECONDS))))
    parser.add_argument("--runtime", type=int, default=int(os.getenv("OKX_RUNTIME", "0")))
    parser.add_argument("--flag", default=os.getenv("OKX_FLAG", "0"), choices=("0", "1"))
    parser.add_argument("--ai", action="store_true", default=os.getenv("AI_ENABLED", "0") == "1")
    parser.add_argument("--dry-run-ai", action="store_true", default=os.getenv("AI_DRY_RUN", "0") == "1")
    parser.add_argument("--push", action="store_true", default=os.getenv("PUSH_ENABLED", "0") == "1")
    parser.add_argument("--push-score", type=int, default=int(os.getenv("PUSH_SCORE", str(DEFAULT_PUSH_SCORE))))
    parser.add_argument("--retry-times", type=int, default=env_int("RETRY_TIMES", DEFAULT_RETRY_TIMES))
    parser.add_argument("--retry-backoff", type=float, default=env_float("RETRY_BACKOFF_SECONDS", DEFAULT_RETRY_BACKOFF_SECONDS))
    parser.add_argument("--push-cooldown", type=int, default=env_int("PUSH_COOLDOWN_SECONDS", DEFAULT_PUSH_COOLDOWN_SECONDS))
    parser.add_argument("--log-max-bytes", type=int, default=env_int("LOG_MAX_BYTES", DEFAULT_LOG_MAX_BYTES))
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
        ),
        runtime_config=RuntimeConfig(
            retry_times=max(args.retry_times, 1),
            retry_backoff=max(args.retry_backoff, 0.1),
            push_cooldown_seconds=max(args.push_cooldown, 0),
            log_max_bytes=max(args.log_max_bytes, 1024 * 1024),
        ),
    )
    if args.record_replay:
        assistant.record_replay_file = Path(args.record_replay_file)
    if args.replay_file:
        assistant.replay_log_file = Path(args.replay_log_file)
        assistant.push_enabled = False
        _, frames = load_replay_dataset(Path(args.replay_file))
        try:
            assistant.run_replay(frames, max(float(args.replay_interval), 0.0))
        except KeyboardInterrupt:
            print("Replay stopped.")
        return 0
    try:
        assistant.run_forever(args.runtime)
    except KeyboardInterrupt:
        print("Stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
