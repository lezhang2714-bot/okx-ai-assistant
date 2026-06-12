#!/usr/bin/env python3
"""
OKX AI short-term trading assistant V1.

Scope:
    - Monitor BTC-USDT-SWAP and ETH-USDT-SWAP only.
    - Provide analysis and suggestions only.
    - No auto order, no martingale, no grid.

Install:
    pip install python-okx

Optional AI (OpenAI / DeepSeek):
    pip install openai
    export AI_API_KEY="..."           # 或 OPENAI_API_KEY
    export AI_BASE_URL="https://api.deepseek.com"  # DeepSeek; OpenAI留空
    export AI_MODEL="deepseek-chat"   # 或 gpt-4o

Optional push:
    export TELEGRAM_BOT_TOKEN="..."
    export TELEGRAM_CHAT_ID="..."
    export WECOM_WEBHOOK_URL="..."
    export WECHAT_SEND_KEY="..."       # Server酱 SendKey，推送到个人微信

Production tuning:
    export RETRY_TIMES=3
    export PUSH_COOLDOWN_SECONDS=900
    export LOG_MAX_BYTES=10485760
    export VOLUME_MULTIPLIER=2.0
    export OI_CHANGE_PCT_15M=5.0
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
# 1m看即时波动，5m看短线节奏，15m看交易方向，1H看上一级趋势。
BAR_CHANNELS = ("1m", "5m", "15m", "1H")
DEFAULT_INTERVAL_SECONDS = 600
DEFAULT_PUSH_SCORE = 80
DEFAULT_AI_MODEL = "deepseek-chat"
OKX_BASE_URL = "https://www.okx.com"
DEFAULT_RETRY_TIMES = 3
DEFAULT_RETRY_BACKOFF_SECONDS = 1.5
DEFAULT_PUSH_COOLDOWN_SECONDS = 900
DEFAULT_LOG_MAX_BYTES = 10 * 1024 * 1024
WARMUP_MINUTES = 15

# 不同类型数据使用不同缓存时间，降低OKX REST请求量，减少限频风险。
# ticker仍保持接近5秒刷新；K线、OI、资金费率、多空比可以低频更新。
CACHE_TTL_SECONDS = {
    "ticker": 5,
    "candles": 15,
    "open_interest": 60,
    "funding_rate": 60,
    "long_short_ratio": 60,
}

# 日志使用JSON Lines格式，一行一条分析记录，便于后续导入数据库或做回测统计。
# 默认保存到运行目录下的logs目录，便于交付后集中查看、备份和清理。
LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_FILE = LOG_DIR / "okx_ai_monitor.log"


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
    # V1使用轻量趋势判断：比较最近收盘价和lookback窗口最后一根收盘价。
    # 生产版可以替换为EMA、MACD、ATR、结构高低点等更完整的趋势模型。
    if len(candles) < 2:
        return "unknown"
    sample = candles[:lookback]
    latest = sample[0]["close"]
    oldest = sample[-1]["close"]
    if latest > oldest:
        return "up"
    if latest < oldest:
        return "down"
    return "flat"


def symbol_ccy(inst_id: str) -> str:
    return inst_id.split("-")[0]


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

        # OI和资金费率需要计算“15分钟内变化”，所以本地保存最近一段时间的采样值。
        # maxlen=240在5秒轮询下大约可保存20分钟数据。
        self.oi_history: Dict[str, Deque[Tuple[float, float]]] = defaultdict(lambda: deque(maxlen=240))
        self.funding_history: Dict[str, Deque[Tuple[float, float]]] = defaultdict(lambda: deque(maxlen=240))

        # REST缓存用于降低请求量。例如OI、资金费率、多空比不必每5秒都请求。
        # key -> (timestamp, value)
        self.cache: Dict[str, Tuple[float, Any]] = {}

        # 推送冷却状态。key通常由币种、方向、信号类型组成。
        self.last_push_at: Dict[str, float] = {}

    def collect_snapshot(self, inst_id: str) -> Dict[str, Any]:
        # 获取当前时刻的成交价、买入挂单价、卖出挂单价。
        ticker = self._get_ticker(inst_id)

        # 获取k线，返回字典：key是哪个k线周期，数据是数组，包含当前周期下的21根K线的数据结构，
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

        # 记录当前OI和资金费率，240次存储的循环队列，5s轮询可以存储大约15min。
        self._remember_metric(self.oi_history[inst_id], open_interest)
        self._remember_metric(self.funding_history[inst_id], funding_rate)
        
        return {
            "time": now_text(),
            "inst_id": inst_id,
            "price": ticker.get("last", 0.0),
            "best_bid": ticker.get("bid_px", 0.0),
            "best_ask": ticker.get("ask_px", 0.0),
            "candles": candles,
            "volume": volume,
            "open_interest": open_interest,
            # 获取这15m内的OI变化率，计算百分比，就是当前oi / 15m前的oi 的百分比
            "oi_change_pct_15m": self._change_pct_last_minutes(self.oi_history[inst_id], 15),
            # 数据是否满足15min的要求，预热是否完成
            "oi_warmup_ready": self._history_ready(self.oi_history[inst_id], WARMUP_MINUTES),
            "funding_rate": funding_rate,
            # 资金费率相对于15Min前的变化量，就是当前资金费率 - 15Min前的资金费率
            "funding_change": self._change_last_minutes(self.funding_history[inst_id], 15),
            "funding_warmup_ready": self._history_ready(self.funding_history[inst_id], WARMUP_MINUTES),
            "long_short_ratio": long_short,
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
        
        if volume["multiplier"] >= self.config.volume_multiplier:
            # 放量倍数判断：放量通常代表短时间交易活跃度提高，但方向需要结合K线和OI判断。
            signals.append({
                "type": "volume_spike",
                "desc": f"1m volume multiplier {volume['multiplier']:.2f}x",
            })

        if snapshot.get("oi_warmup_ready") and abs(oi_change) >= self.config.oi_change_pct_15m:
            # 持仓率判断：OI变化表示合约持仓量变化，配合价格可以判断新开仓或平仓压力。
            signals.append({
                "type": "oi_change",
                "desc": f"15m OI change {oi_change:.2f}%",
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

        return signals

    def score_snapshot(self, snapshot: Dict[str, Any], signals: List[Dict[str, Any]]) -> Dict[str, Any]:
        # 评分系统满分100分：
        # 趋势评分最高50分，资金评分最高30分，风险评分最高20分。
        # 评分用于“是否推送”和“建议强弱”，不是自动交易指令。

        # 趋势判断，通过当前收盘价和21条K线的最早收盘价，判断时间范围内的up/down/flat/unkonw状态
        trends = {
            "1m": trend_from_candles(snapshot["candles"]["1m"]),
            "5m": trend_from_candles(snapshot["candles"]["5m"]),
            "15m": trend_from_candles(snapshot["candles"]["15m"]),
            "1H": trend_from_candles(snapshot["candles"]["1H"]),
        }

        # 单纯依靠收盘价来判断做多/做空
        up_count = sum(1 for item in trends.values() if item == "up")
        down_count = sum(1 for item in trends.values() if item == "down")
        direction = "观望"
        if up_count >= 3:
            # 多数周期向上，方向倾向做多。
            direction = "做多"
        elif down_count >= 3:
            # 多数周期向下，方向倾向做空。
            direction = "做空"

        # 趋势评分：周期越一致，趋势分越高；15m和1H同向额外加分。
        #待优化
        trend_score = 30 + max(up_count, down_count) * 10
        if trends["15m"] == trends["1H"] and trends["15m"] != "unknown":
            trend_score += 10
        trend_score = min(trend_score, 50)

        funding = snapshot["funding_rate"]
        oi_change = snapshot["oi_change_pct_15m"]
        volume_mult = snapshot["volume"]["multiplier"]

        # 资金评分：放量、OI变化、资金费率不过热都会提高资金评分。
        capital_score = 20
        if volume_mult >= 1.5:
            capital_score += 8
        if abs(oi_change) >= 2:
            capital_score += 7
        if abs(funding) < self.config.funding_abs_threshold:
            capital_score += 5
        capital_score = min(capital_score, 30)

        risk_score = 20
        long_ratio = snapshot["long_short_ratio"].get("long_ratio", 0.0)
        short_ratio = snapshot["long_short_ratio"].get("short_ratio", 0.0)
        if abs(funding) >= self.config.funding_abs_threshold:
            # 资金费率过热，扣风险分。
            risk_score -= 8
        if max(long_ratio, short_ratio) >= self.config.long_short_extreme:#调整
            # 多空比极端，扣风险分。
            risk_score -= 6
        if direction == "做多" and short_count_greater(trends):
            risk_score -= 4
        risk_score = max(risk_score, 0)

        total = max(0, min(100, trend_score + capital_score + risk_score))#综合评分

        # V1用当前价格的固定百分比生成入场、止损、止盈。
        # 生产版建议改成基于ATR、前高前低、订单簿流动性和支撑阻力位。
        entry, stop_loss, take_profit = self._suggest_levels(snapshot["price"], direction)

        return {
            "trend_score": trend_score,
            "capital_score": capital_score,
            "risk_score": risk_score,
            "total_score": total,
            "direction": direction,
            "entry": entry,
            "stop_loss": stop_loss,
            "take_profit": take_profit,

            # 风险等级：高、中、低
            "risk_level": self._risk_level(total, signals),
            "trends": trends,
        }

    def analyze_with_ai(
        self,
        snapshot: Dict[str, Any],
        signals: List[Dict[str, Any]],
        score: Dict[str, Any],
    ) -> Dict[str, Any]:
        # AI模块只在触发信号后由run_once调用。
        # 如果没有开启AI、没有安装openai包、没有配置Key或请求失败，都会回退到本地规则分析。
        if not self.ai_enabled:
            return self._local_analysis(snapshot, signals, score)

        if self.dry_run_ai:
            # dry-run用于调试：可以看到发给AI的数据，但不会产生真实API费用。
            payload = self._ai_payload(snapshot, signals, score)
            return {
                "provider": "dry-run",
                "content": "AI dry-run enabled. Payload prepared but not sent.",
                "payload": payload,
            }
        try:
            from openai import OpenAI
        except ImportError:
            return {
                "provider": "local",
                "content": "openai package is not installed; fallback to local analysis.",
                "fallback": self._local_analysis(snapshot, signals, score),
            }

        api_key = os.getenv("AI_API_KEY","sk-aefc7a633ce3471ab1acaccaa9814ce3") or os.getenv("OPENAI_API_KEY")
        base_url = os.getenv("AI_BASE_URL", "https://api.deepseek.com")
        model = os.getenv("AI_MODEL", "deepseek-chat")

        if not api_key:
            return {
                "provider": "local",
                "content": "AI_API_KEY or OPENAI_API_KEY is not configured; fallback to local analysis.",
                "fallback": self._local_analysis(snapshot, signals, score),
            }

        client = OpenAI(api_key=api_key, base_url=base_url)
        prompt = self._ai_prompt(snapshot, signals, score)

        try:
            # 使用 Chat Completions API，兼容 OpenAI 和 DeepSeek。
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
            )
            output_text = response.choices[0].message.content
            parsed = extract_json_object(output_text)
            valid, errors = self._validate_ai_result(parsed)
            return {
                "provider": "deepseek" if "deepseek" in base_url else "openai",
                "model": model,
                "content": output_text,
                "parsed": parsed,
                "valid_json": valid,
                "validation_errors": errors,
                "fallback": None if valid else self._local_analysis(snapshot, signals, score),
            }
        except Exception as exc:
            print(f"[{now_text()}] AI analysis failed: {exc}")
            return {
                "provider": "local",
                "content": f"AI request failed: {exc}; fallback to local analysis.",
                "fallback": self._local_analysis(snapshot, signals, score),
            }

    def push_if_needed(
        self,
        snapshot: Dict[str, Any],
        signals: List[Dict[str, Any]],
        score: Dict[str, Any],
        analysis: Dict[str, Any],
    ) -> None:
        # 推送模块只关心综合评分是否达到阈值。
        # 默认80分以上推送，70分以下只记录不推送，避免提醒过多。
        # if not signals:
        #     return

        # if score["total_score"] < self.push_score:
        #     return

        push_key = self._push_key(snapshot, signals, score)
        # if self._in_push_cooldown(push_key):
        #     print(f"[{now_text()}] push skipped by cooldown: {push_key}")
        #     return

        message = self._format_push_message(snapshot, signals, score, analysis)
        print("----------------------------------------------\n")
        print(message)
        # if not self.push_enabled:
        #     self.last_push_at[push_key] = time.time()
        #     return

        self._push_telegram(message)
        self._push_webhook(os.getenv("WECOM_WEBHOOK_URL"), message)
        self._push_serverchan(os.getenv("WECHAT_SEND_KEY","SCT361954Tfk2ZEcU9hXFfFNrdwAaeSBn5"), snapshot, signals, score, analysis)
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
            "open_interest": snapshot["open_interest"],
            "oi_change_pct_15m": snapshot["oi_change_pct_15m"],
            "oi_warmup_ready": snapshot["oi_warmup_ready"],
            "funding_rate": snapshot["funding_rate"],
            "funding_change": snapshot["funding_change"],
            "funding_warmup_ready": snapshot["funding_warmup_ready"],
            "long_short_ratio": snapshot["long_short_ratio"],
            "signals": signals,
            "score": score,
            "analysis": analysis,
        }
        self._rotate_log_if_needed()
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")

    def run_once(self) -> None:
        # 定时执行：遍历所有支持币种，完成数据采集、阈值检测、综合评分、AI分析、微信推送、日志存储等全部功能。
        for inst_id in self.instruments:
            try:
                # 采集当前币种基础数据快照,包括：
                # 当前时间戳、币种、当前成交价、买入挂单价、卖出挂单价、21条k线、1m成交量、oi、oi变化率、资金费率、费率变化量、多空比
                snapshot = self.collect_snapshot(inst_id)

                # 阈值检测，通过阈值判断是否有用户关注的信号产生:信号格式为多个type和desc
                # {
                #     "type": "volume_spike",
                #     "desc": "1m volume multiplier 3.21x"
                # }
                # 放量倍数、持仓率、资金费率、资金费率变化量、多头占比、空头占比
                # 返回是一个数组，有信号变化就加入数组，没有信号产生，数组就是空
                signals = self.detect_signals(snapshot)

                # 本地综合评分，包括：各项打分、总分、建议、操作区间、风险等级、K线趋势；
                score = self.score_snapshot(snapshot, signals)

                # 只有触发信号才调用AI；否则用本地规则输出简要分析，节省AI成本。
                # analysis = self.analyze_with_ai(snapshot, signals, score) if signals else self._local_analysis(snapshot, signals, score)
                analysis = self.analyze_with_ai(snapshot, signals, score)
                # 打印终端摘要
                # self._print_console(snapshot, signals, score, analysis)
                # 推送判断与处理
                self.push_if_needed(snapshot, signals, score, analysis)
                # # 记录JSON日志
                self.log_result(snapshot, signals, score, analysis)

            except Exception as exc:
                print(f"[{now_text()}] {inst_id} collect/analyze failed: {exc}")

    def run_forever(self, runtime: int) -> None:
        # 主循环：默认永久运行；runtime>0时，到指定秒数自动退出，用于实现定时任务。
        started = time.time()
        while True:
            self.run_once()
            if runtime > 0 and time.time() - started >= runtime:
                print(f"Runtime {runtime}s reached; exit.")
                break
            time.sleep(self.interval)

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
        # 每个周期取21根K线：当前K线 + 最近20根，用于计算均量和趋势。
        response = self._cached(
            f"candles:{inst_id}:{bar}",
            CACHE_TTL_SECONDS["candles"],
            lambda: self._sdk_or_rest(
                f"candles-{bar}",
                (lambda: self.market_api.get_candlesticks(instId=inst_id, bar=bar, limit="21")) if self.market_api else None,
                lambda: okx_public_get(
                    "/api/v5/market/candles",
                    {"instId": inst_id, "bar": bar, "limit": "21"},
                    self.runtime_config.retry_times,
                    self.runtime_config.retry_backoff,
                ),
            ),
        )
        data = okx_data(response)

        # API返回的是21条k线数据，一个数组返回，这里通过for循环遍历21条生成一个返回数组结构
        # 一个结构包含：时间戳、开盘价、最高价、最低价、收盘价、成交量、是否收盘标记（当前k线还是历史k线）
        return [candle_to_dict(row) for row in data if isinstance(row, list)]

    def _get_open_interest(self, inst_id: str) -> float:
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

    def _volume_stats(self, candles_1m: List[Dict[str, Any]]) -> Dict[str, float]:
        # 当前成交量取最新1m K线成交量，均量取后面20根。
        # multiplier越大，说明当前交易活跃度相对近期越异常。
        current = candles_1m[0]["volume"] if candles_1m else 0.0
        previous = [item["volume"] for item in candles_1m[1:21]]
        average = sum(previous) / len(previous) if previous else 0.0
        multiplier = current / average if average > 0 else 0.0
        return {
            "current": current,
            "average_20": average,
            "multiplier": multiplier,
        }

    def _remember_metric(self, history: Deque[Tuple[float, float]], value: float) -> None:
        # 保存一条时间序列采样，格式为(timestamp, value)。
        history.append((time.time(), value))

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
        threshold = time.time() - minutes * 60
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

    def _suggest_levels(self, price: float, direction: str) -> Tuple[str, str, str]:
        # V1的价位建议是演示规则：
        # 做多：止损放在当前价下方，止盈放在上方。
        # 做空：止损放在当前价上方，止盈放在下方。
        if price <= 0 or direction == "观望":
            return "-", "-", "-"
        if direction == "做多":
            return (
                f"{price * 0.998:.2f} - {price * 1.002:.2f}",
                f"{price * 0.992:.2f}",
                f"{price * 1.012:.2f} / {price * 1.020:.2f}",
            )
        return (
            f"{price * 0.998:.2f} - {price * 1.002:.2f}",
            f"{price * 1.008:.2f}",
            f"{price * 0.988:.2f} / {price * 0.980:.2f}",
        )

    def _risk_level(self, total_score: int, signals: List[Dict[str, Any]]) -> str:
        # 风险等级综合评分和高风险信号。资金费率过热直接提高风险等级。
        risk_signal_types = {item["type"] for item in signals}
        if total_score < 70 or "funding_hot" in risk_signal_types:
            return "高"
        if total_score < 80 or "long_short_extreme" in risk_signal_types:
            return "中"
        return "低"

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
            "prediction",
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

        prediction = parsed.get("prediction")
        if not isinstance(prediction, dict):
            errors.append("prediction must be an object")
        else:
            for key in ("short_term", "mid_term", "target_price", "confidence",
                        "key_support", "key_resistance", "bias"):
                if key not in prediction:
                    errors.append(f"prediction missing {key}")
            if prediction.get("confidence") not in ("低", "中", "高"):
                errors.append("prediction.confidence must be 低/中/高")
            if prediction.get("bias") not in ("偏多", "偏空", "震荡"):
                errors.append("prediction.bias must be 偏多/偏空/震荡")

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
                    "5m": compact_candles(snapshot["candles"]["5m"], 12),
                    "15m": compact_candles(snapshot["candles"]["15m"], 12),
                    "1H": compact_candles(snapshot["candles"]["1H"], 12),
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
            },
            "rule_thresholds": {
                "volume_multiplier": self.config.volume_multiplier,
                "oi_change_pct_15m": self.config.oi_change_pct_15m,
                "funding_abs_threshold": self.config.funding_abs_threshold,
                "funding_change_threshold": self.config.funding_change_threshold,
                "long_short_extreme": self.config.long_short_extreme,
                "push_score": self.push_score,
            },
            "rule_outputs": {
                "signals": signals,
                "score": score,
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
                "threshold": self.config.volume_multiplier,
                "valid_by_rule": snapshot["volume"]["multiplier"] >= self.config.volume_multiplier,
                "detail": snapshot["volume"],
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
            "你是欧易OKX USDT永续合约短线交易分析助手，只分析BTC-USDT-SWAP和ETH-USDT-SWAP。"
            "你的任务是根据实时行情、多周期K线、成交量、OI、资金费率、多空比和本地规则评分，"
            "先审计程序规则结果是否可信，再输出短线交易观察建议。\n\n"
            "硬性限制：\n"
            "1. 只提供分析和风险提示，不允许表示系统会自动下单。\n"
            "2. 不允许承诺收益，不允许使用稳赚、必涨、必跌等确定性表述。\n"
            "3. 如果数据不足、信号矛盾、预热未完成或风险过高，direction必须选择观望。\n"
            "4. 如果long_short_ratio.available=false，需要说明多空比不可用，不要凭空编造多空比。\n"
            "5. 如果oi_warmup_ready=false或funding_warmup_ready=false，需要降低对15分钟变化类指标的权重。\n\n"
            "分析步骤：\n"
            "1. 规则审计：根据raw_evidence和signal_evidence，判断程序触发的signals是否有原始数据支持。\n"
            "2. 趋势：分别根据K线原始数据判断1m、5m、15m、1H方向，说明短线和上级周期是否共振。\n"
            "3. 量能：复核当前1m成交量相对20根均量是否真的放量，放量是否支持当前方向。\n"
            "4. OI：根据oi_history和oi_change_pct_15m判断OI变化是否可信，代表新增仓位、平仓或暂时无有效结论。\n"
            "5. 资金费率：根据funding_history、funding_rate、funding_change_15m判断是否过热或快速变化。\n"
            "6. 多空比：若available=true，判断是否极端；若available=false，明确说明不可用并降低该项权重。\n"
            "7. 风险：结合止损距离、资金费率、趋势冲突、信号数量、预热状态给出风险等级。\n"
            "8. 建议：方向只能是做多、做空、观望。入场、止损、止盈可参考rule_outputs.score，但可以保守修正。\n"
            "9. 预测：基于当前多周期K线结构、量价关系、OI变化和资金费率，预测未来5分钟和15-60分钟的走势方向、"
            "目标价位、关键支撑和压力位。预测必须给出置信度，置信度低时必须说明不确定因素。\n\n"
            "必须只输出一个合法JSON对象，不要输出Markdown代码块，不要输出JSON以外的解释。"
            "JSON字段必须完全包含：\n"
            "{\n"
            '  "trend": {\n'
            '    "summary": "一句话趋势结论",\n'
            '    "timeframes": {"1m": "...", "5m": "...", "15m": "...", "1H": "..."},\n'
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
            '  "prediction": {\n'
            '    "short_term": "未来5分钟走势预测及理由",\n'
            '    "mid_term": "未来15-60分钟走势预测及理由",\n'
            '    "target_price": "预测目标价位，观望时填-",\n'
            '    "confidence": "低/中/高",\n'
            '    "key_support": "当前关键支撑位",\n'
            '    "key_resistance": "当前关键压力位",\n'
            '    "bias": "偏多/偏空/震荡",\n'
            '    "uncertainty_factors": ["不确定因素1", "不确定因素2"]\n'
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
        trends = score["trends"]
        direction = score["direction"]
        price = snapshot["price"]
        volume = snapshot["volume"]
        oi_pct = snapshot["oi_change_pct_15m"]
        funding = snapshot["funding_rate"]
        bias = _trends_to_bias(trends)

        # ---- 短期预测（5分钟）：看1m趋势 + 放量情况 ----
        t1m = trends.get("1m", "unknown")
        vol_spike = volume["multiplier"] >= self.config.volume_multiplier
        if t1m == "up" and vol_spike:
            short_term = f"1m放量上涨(量比{volume['multiplier']:.1f}x)，短期惯性偏多，关注能否站稳{price:.1f}上方。"
        elif t1m == "down" and vol_spike:
            short_term = f"1m放量下跌(量比{volume['multiplier']:.1f}x)，短期惯性偏空，关注{price:.1f}支撑。"
        elif t1m == "up":
            short_term = f"1m温和上涨，量能正常，短期延续偏多但力度有限。"
        elif t1m == "down":
            short_term = f"1m温和下跌，量能正常，短期延续偏空但力度有限。"
        else:
            short_term = "1m方向不明，短期看震荡整理，等待方向选择。"

        # ---- 中期预测（15-60分钟）：看15m+1H共振 + OI + 资金费率 ----
        t15m = trends.get("15m", "unknown")
        t1h = trends.get("1H", "unknown")
        oi_ready = snapshot.get("oi_warmup_ready", False)
        fund_ready = snapshot.get("funding_warmup_ready", False)
        mid_parts = []
        if t15m == t1h and t15m in ("up", "down"):
            mid_parts.append(f"15m与1H共振{t15m}，中期趋势一致性较好")
        elif t15m != "unknown" and t1h != "unknown":
            mid_parts.append(f"15m{t15m}与1H{t1h}存在周期冲突，中期方向不确定性高")
        if oi_ready:
            if oi_pct > 0:
                mid_parts.append(f"OI 15分钟增加{oi_pct:.1f}%，有新增仓位进场")
            elif oi_pct < 0:
                mid_parts.append(f"OI 15分钟减少{abs(oi_pct):.1f}%，部分仓位离场")
        if fund_ready and abs(funding) >= self.config.funding_abs_threshold:
            side = "多头拥挤" if funding > 0 else "空头拥挤"
            mid_parts.append(f"资金费率{funding:.5f}偏{side}，追单风险提高")
        mid_term = "；".join(mid_parts) if mid_parts else "数据预热未完成，中期暂无有效预测。"

        # ---- 置信度：看趋势一致性和信号数量 ----
        up_c = sum(1 for t in trends.values() if t == "up")
        down_c = sum(1 for t in trends.values() if t == "down")
        consistent = max(up_c, down_c)
        sig_count = len(signals)
        if consistent >= 3 and sig_count >= 2:
            confidence = "中"
        elif consistent >= 3 or sig_count >= 2:
            confidence = "低"
        else:
            confidence = "低"

        # ---- 支撑/压力位：优先用止损止盈，其次从15m K线取近端高低点 ----
        support = score.get("stop_loss", "-")
        resistance = score.get("take_profit", "-")
        if support == "-" or resistance == "-":
            c15 = snapshot["candles"].get("15m", [])
            if len(c15) >= 3:
                highs = [c["high"] for c in c15[1:] if c.get("high", 0) > 0]
                lows = [c["low"] for c in c15[1:] if c.get("low", 0) > 0]
                if support == "-" and lows:
                    support = f"{min(lows):.2f}"
                if resistance == "-" and highs:
                    resistance = f"{max(highs):.2f}"

        # ---- 不确定因素 ----
        unknowns = []
        if not snapshot.get("oi_warmup_ready"):
            unknowns.append("OI预热未完成(需15分钟)")
        if not snapshot.get("funding_warmup_ready"):
            unknowns.append("资金费率预热未完成(需15分钟)")
        lr = snapshot["long_short_ratio"]
        if not lr.get("available"):
            unknowns.append("多空比接口不可用")
        if consistent < 3:
            unknowns.append("多周期方向不一致")
        if not unknowns:
            unknowns.append("无明显不确定因素")

        prediction = {
            "short_term": short_term,
            "mid_term": mid_term,
            "target_price": _extract_first_price(score.get("take_profit", "-")),
            "confidence": confidence,
            "key_support": support,
            "key_resistance": resistance,
            "bias": bias,
            "uncertainty_factors": unknowns,
        }

        content = {
            "trend": trends,
            "risk": f"风险等级：{score['risk_level']}",
            "suggestion": "仅供观察，不构成投资建议。",
            "direction": direction,
            "entry": score["entry"],
            "stop_loss": score["stop_loss"],
            "take_profit": score["take_profit"],
            "risk_level": score["risk_level"],
            "reasons": reasons,
            "prediction": prediction,
        }
        return {"provider": "local-rule", "content": content}

    def _format_push_message(
        self,
        snapshot: Dict[str, Any],
        signals: List[Dict[str, Any]],
        score: Dict[str, Any],
        analysis: Dict[str, Any],
    ) -> str:
        # 把分析结果整理成适合聊天工具阅读的文本。
        reason_text = "; ".join(item["desc"] for item in signals) or "规则评分达到推送阈值"
        return (
            f"[OKX AI短线助手]\n"
            f"币种: {snapshot['inst_id']}\n"
            f"价格: {snapshot['price']}\n"
            f"综合评分: {score['total_score']}\n"
            f"方向: {score['direction']}\n"
            f"入场位: {score['entry']}\n"
            f"止损位: {score['stop_loss']}\n"
            f"止盈位: {score['take_profit']}\n"
            f"风险等级: {score['risk_level']}\n"
            f"分析理由: {reason_text}\n"
            f"AI/规则分析: {json.dumps(analysis, ensure_ascii=False)}"
        )

    def _push_key(
        self,
        snapshot: Dict[str, Any],
        signals: List[Dict[str, Any]],
        score: Dict[str, Any],
    ) -> str:
        # 同币种、同方向、同信号组合视为同一类提醒，进入冷却窗口。
        signal_types = ",".join(sorted(item["type"] for item in signals)) or "score-only"
        return f"{snapshot['inst_id']}:{score['direction']}:{signal_types}"

    def _in_push_cooldown(self, push_key: str) -> bool:
        last_at = self.last_push_at.get(push_key, 0.0)
        return time.time() - last_at < self.runtime_config.push_cooldown_seconds

    def _push_telegram(self, message: str) -> None:
        # Telegram推送需要Bot Token和Chat ID。
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        chat_id = os.getenv("TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            return
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        try:
            http_post_json(
                url,
                {"chat_id": chat_id, "text": message},
                self.runtime_config.retry_times,
                self.runtime_config.retry_backoff,
            )
        except Exception as exc:
            print(f"Telegram push failed: {exc}")

    def _push_webhook(self, url: Optional[str], message: str) -> None:
        # 企业微信群机器人 Webhook，JSON POST 格式。
        if not url:
            return
        try:
            http_post_json(
                url,
                {"msgtype": "text", "text": {"content": message}},
                self.runtime_config.retry_times,
                self.runtime_config.retry_backoff,
            )
        except Exception as exc:
            print(f"Webhook push failed: {exc}")

    def _push_serverchan(
        self,
        send_key: Optional[str],
        snapshot: Dict[str, Any],
        signals: List[Dict[str, Any]],
        score: Dict[str, Any],
        analysis: Dict[str, Any],
    ) -> None:
        # Server酱 推送个人微信。SendKey 从 https://sct.ftqq.com 获取。
        if not send_key:
            return
        inst_id = snapshot["inst_id"]
        direction = score["direction"]
        price = snapshot["price"]
        total = score["total_score"]
        risk = score["risk_level"]
        entry = score["entry"]
        stop_loss = score["stop_loss"]
        take_profit = score["take_profit"]
        reason_text = "; ".join(item["desc"] for item in signals) or "规则评分达到推送阈值"

        # 提取 AI/规则分析摘要和预测
        provider = analysis.get("provider", "unknown")
        parsed = analysis.get("parsed") or {}
        if isinstance(parsed, dict) and parsed.get("suggestion"):
            ai_summary = f"- **{provider}建议**：{parsed['suggestion']}\n"
        elif analysis.get("fallback"):
            fb = analysis["fallback"]
            fb_content = fb.get("content", {}) if isinstance(fb, dict) else {}
            if isinstance(fb_content, dict) and fb_content.get("suggestion"):
                ai_summary = f"- **规则建议**：{fb_content['suggestion']}\n"
            else:
                ai_summary = ""
        else:
            ai_summary = ""

        # 提取预测信息
        pred = parsed.get("prediction") if isinstance(parsed, dict) else None
        if pred and isinstance(pred, dict):
            pred_text = (
                f"\n## 走势预测\n\n"
                f"- **置信度**：{pred.get('confidence', '-')}\n"
                f"- **偏向**：{pred.get('bias', '-')}\n"
                f"- **短期(5m)**：{pred.get('short_term', '-')}\n"
                f"- **中期(15-60m)**：{pred.get('mid_term', '-')}\n"
                f"- **目标价**：{pred.get('target_price', '-')}\n"
                f"- **支撑位**：{pred.get('key_support', '-')}\n"
                f"- **压力位**：{pred.get('key_resistance', '-')}\n"
            )
        else:
            pred_text = ""

        # title 手机通知栏直接显示。
        bias = pred.get("bias", "") if pred else ""
        title = f"{inst_id} {direction} {bias} 评分{total} 风险{risk}"

        # desp Markdown 格式，点开通知看到详情。
        desp = (
            f"## {inst_id} 短线分析\n\n"
            f"- **价格**：{price}\n"
            f"- **方向**：{direction}\n"
            f"- **综合评分**：{total}\n"
            f"- **入场区间**：{entry}\n"
            f"- **止损**：{stop_loss}\n"
            f"- **止盈**：{take_profit}\n"
            f"- **风险等级**：{risk}\n"
            f"- **信号**：{reason_text}\n"
            f"{ai_summary}"
            f"{pred_text}"
        )

        try:
            http_post_json(
                f"https://sctapi.ftqq.com/{send_key}.send",
                {"title": title, "desp": desp},
                self.runtime_config.retry_times,
                self.runtime_config.retry_backoff,
            )
        except Exception as exc:
            print(f"ServerChan push failed: {exc}")

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
        # if signals:
        print(f"analysis={json.dumps(analysis, ensure_ascii=False)}")


def short_count_greater(trends: Dict[str, str]) -> bool:
    # 判断多周期中下跌周期是否多于上涨周期。
    return sum(1 for item in trends.values() if item == "down") > sum(1 for item in trends.values() if item == "up")


def _trends_to_bias(trends: Dict[str, str]) -> str:
    # 根据多周期趋势判断方向偏向，供本地规则兜底使用。
    up = sum(1 for t in trends.values() if t == "up")
    down = sum(1 for t in trends.values() if t == "down")
    if up > down:
        return "偏多"
    if down > up:
        return "偏空"
    return "震荡"


def _extract_first_price(text: str) -> str:
    # 从 "78343.50 - 78657.50" 或 "79442.51 / 80070.51" 中取第一个价格，
    # 作为 target_price 的默认值。
    if not text or text == "-":
        return "-"
    return text.replace("/", "-").replace(" ", "").split("-")[0]


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
        ),
        runtime_config=RuntimeConfig(
            retry_times=max(args.retry_times, 1),
            retry_backoff=max(args.retry_backoff, 0.1),
            push_cooldown_seconds=max(args.push_cooldown, 0),
            log_max_bytes=max(args.log_max_bytes, 1024 * 1024),
        ),
    )
    try:
        assistant.run_forever(args.runtime)
    except KeyboardInterrupt:
        print("Stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
