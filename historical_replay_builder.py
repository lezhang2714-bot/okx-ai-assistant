#!/usr/bin/env python3
"""Build replay_dataset JSONL frames from OKX historical market data."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import okx_signal_monitor as monitor

BAR_CHANNELS = monitor.BAR_CHANNELS
KLINE_LIMIT = monitor.KLINE_LIMIT
REPLAY_DATASET_VERSION = monitor.REPLAY_DATASET_VERSION
DEFAULT_RETRY_TIMES = monitor.DEFAULT_RETRY_TIMES
DEFAULT_RETRY_BACKOFF_SECONDS = monitor.DEFAULT_RETRY_BACKOFF_SECONDS
candle_to_dict = monitor.candle_to_dict
http_get_json = monitor.http_get_json
ms_to_text = monitor.ms_to_text
okx_data = monitor.okx_data
okx_public_get = monitor.okx_public_get
symbol_ccy = monitor.symbol_ccy
to_float = monitor.to_float

REQUIRED_BARS = BAR_CHANNELS[:6]
OPTIONAL_BARS = BAR_CHANNELS[6:]
DEFAULT_OUTPUT = monitor.LOG_DIR / "replay_dataset_historical.jsonl"
MAX_FRAMES = 2000
HISTORY_CANDLE_LIMIT = 100
CANDLES_LIMIT = 300

BAR_MS: Dict[str, int] = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "1H": 3_600_000,
    "4H": 14_400_000,
    "1D": 86_400_000,
    "1W": 604_800_000,
}

ProgressCallback = Callable[[str, int, int, str], None]
CancelCheck = Callable[[], bool]


def parse_replay_time(value: str) -> datetime:
    text = str(value or "").strip().replace("T", " ")
    if text.endswith("Z"):
        text = text[:-1]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return monitor.parse_time_text(text)


def frame_times(start: datetime, end: datetime, step_seconds: int) -> List[datetime]:
    if end < start:
        raise ValueError("结束时间不能早于开始时间")
    step_seconds = max(5, int(step_seconds))
    times: List[datetime] = []
    cursor = start
    while cursor <= end:
        times.append(cursor)
        if len(times) > MAX_FRAMES:
            raise ValueError(f"帧数超过上限 {MAX_FRAMES}，请缩短时间段或增大步长")
        cursor += timedelta(seconds=step_seconds)
    if not times:
        raise ValueError("时间段为空")
    return times


def _raw_open_ms(raw: Sequence[Any]) -> int:
    try:
        return int(raw[0])
    except (TypeError, ValueError, IndexError):
        return 0


def fetch_candles(inst_id: str, bar: str, earliest_ms: int, latest_ms: int) -> List[Dict[str, Any]]:
    collected: Dict[int, Dict[str, Any]] = {}
    bar_ms = BAR_MS.get(bar, 60_000)
    history_border = int(time.time() * 1000) - (1440 - 1) * bar_ms
    cursor = latest_ms + bar_ms

    while cursor > earliest_ms:
        use_history = cursor < history_border
        path = "/api/v5/market/history-candles" if use_history else "/api/v5/market/candles"
        limit = HISTORY_CANDLE_LIMIT if use_history else CANDLES_LIMIT
        params = {
            "instId": inst_id,
            "bar": bar,
            "limit": str(limit),
            "after": str(cursor),
        }
        response = okx_public_get(path, params, DEFAULT_RETRY_TIMES, DEFAULT_RETRY_BACKOFF_SECONDS)
        rows = okx_data(response)
        if not rows:
            break
        oldest_ms = None
        for raw in rows:
            if not isinstance(raw, list):
                continue
            open_ms = _raw_open_ms(raw)
            if open_ms <= 0 or open_ms > latest_ms:
                continue
            if open_ms < earliest_ms:
                continue
            item = candle_to_dict(raw)
            item["_open_ms"] = open_ms
            collected[open_ms] = item
            oldest_ms = open_ms if oldest_ms is None else min(oldest_ms, open_ms)
        if oldest_ms is None or oldest_ms <= earliest_ms:
            break
        if oldest_ms >= cursor:
            break
        cursor = oldest_ms
    ordered = sorted(collected.values(), key=lambda item: int(item.get("_open_ms", 0)), reverse=True)
    return ordered


def slice_candles_at(candles: List[Dict[str, Any]], frame_ts_ms: int, bar: str, limit: int = KLINE_LIMIT) -> List[Dict[str, Any]]:
    bar_ms = BAR_MS.get(bar, 60_000)
    eligible = [item for item in candles if int(item.get("_open_ms", 0)) <= frame_ts_ms]
    window = eligible[:limit]
    output: List[Dict[str, Any]] = []
    for item in window:
        open_ms = int(item.get("_open_ms", 0))
        row = {key: value for key, value in item.items() if not key.startswith("_")}
        row["confirmed"] = "1" if frame_ts_ms >= open_ms + bar_ms else "0"
        output.append(row)
    return output


def _series_open_ms(row: Any) -> int:
    if isinstance(row, list) and row:
        return int(to_float(row[0]))
    if isinstance(row, dict):
        for key in ("ts", "timestamp", "time", "fundingTime"):
            if key in row:
                return int(to_float(row.get(key)))
    return 0


def _series_value(row: Any, value_index: int = 1) -> float:
    if isinstance(row, list) and len(row) > value_index:
        return to_float(row[value_index])
    if isinstance(row, dict):
        for key in ("oi", "fundingRate", "ratio"):
            if key in row:
                return to_float(row.get(key))
    return 0.0


def fetch_time_series(
    path: str,
    params: Dict[str, str],
    earliest_ms: int,
    latest_ms: int,
    *,
    use_begin_end: bool = False,
) -> List[Tuple[int, float]]:
    collected: Dict[int, float] = {}
    if use_begin_end:
        chunk_ms = 7 * 24 * 3600 * 1000
        cursor_begin = earliest_ms
        while cursor_begin <= latest_ms:
            cursor_end = min(latest_ms, cursor_begin + chunk_ms)
            query = dict(params)
            query["begin"] = str(cursor_begin)
            query["end"] = str(cursor_end)
            query["limit"] = "100"
            try:
                response = http_get_json(path, query, DEFAULT_RETRY_TIMES, DEFAULT_RETRY_BACKOFF_SECONDS)
            except Exception:
                break
            rows = okx_data(response)
            for row in rows or []:
                ts_ms = _series_open_ms(row)
                if earliest_ms <= ts_ms <= latest_ms:
                    collected[ts_ms] = _series_value(row)
            cursor_begin = cursor_end + 1
        return sorted(collected.items(), key=lambda item: item[0])

    cursor = latest_ms + 60_000
    while cursor > earliest_ms:
        query = dict(params)
        query["limit"] = "100"
        query["after"] = str(cursor)
        try:
            response = http_get_json(path, query, DEFAULT_RETRY_TIMES, DEFAULT_RETRY_BACKOFF_SECONDS)
        except Exception:
            break
        rows = okx_data(response)
        if not rows:
            break
        oldest_ms = None
        for row in rows:
            ts_ms = _series_open_ms(row)
            if ts_ms <= 0 or ts_ms > latest_ms:
                continue
            if ts_ms < earliest_ms:
                continue
            collected[ts_ms] = _series_value(row)
            oldest_ms = ts_ms if oldest_ms is None else min(oldest_ms, ts_ms)
        if oldest_ms is None or oldest_ms <= earliest_ms:
            break
        if oldest_ms >= cursor:
            break
        cursor = oldest_ms
    return sorted(collected.items(), key=lambda item: item[0])


def lookup_series(series: List[Tuple[int, float]], frame_ts_ms: int) -> float:
    value = 0.0
    for ts_ms, point in series:
        if ts_ms > frame_ts_ms:
            break
        value = point
    return value


def lookup_long_short(series: List[Tuple[int, float]], frame_ts_ms: int) -> Dict[str, Any]:
    ratio = lookup_series(series, frame_ts_ms)
    long_ratio = ratio / (ratio + 1.0) if ratio > 0 else 0.0
    short_ratio = 1.0 - long_ratio if long_ratio > 0 else 0.0
    return {
        "long_short_ratio": ratio,
        "long_ratio": long_ratio,
        "short_ratio": short_ratio,
        "available": ratio > 0,
    }


def neutral_order_book(last_price: float) -> Dict[str, Any]:
    price = max(0.0, to_float(last_price))
    return {
        "bid_size_5": 0.0,
        "ask_size_5": 0.0,
        "bid_size_20": 0.0,
        "ask_size_20": 0.0,
        "imbalance": 0.0,
        "imbalance_5": 0.0,
        "best_bid": price,
        "best_ask": price,
        "spread": 0.0,
        "spread_pct": 0.0,
        "available": False,
    }


def build_historical_replay_dataset(
    *,
    inst_id: str,
    start_time: str,
    end_time: str,
    step_seconds: int,
    output_path: Path = DEFAULT_OUTPUT,
    progress_callback: Optional[ProgressCallback] = None,
    cancel_check: Optional[CancelCheck] = None,
) -> Dict[str, Any]:
    inst_id = str(inst_id or "").strip().upper()
    if not inst_id:
        raise ValueError("inst_id 不能为空")

    start_dt = parse_replay_time(start_time)
    end_dt = parse_replay_time(end_time)
    times = frame_times(start_dt, end_dt, step_seconds)
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)
    warmup_ms = start_ms - KLINE_LIMIT * BAR_MS["4H"]

    def progress(phase: str, current: int, total: int, message: str) -> None:
        if progress_callback:
            progress_callback(phase, current, total, message)

    def cancelled() -> bool:
        return bool(cancel_check and cancel_check())

    progress("fetch", 0, len(REQUIRED_BARS) + 3, "开始拉取 OKX 历史数据")
    candle_cache: Dict[str, List[Dict[str, Any]]] = {}
    for index, bar in enumerate(REQUIRED_BARS, start=1):
        if cancelled():
            raise RuntimeError("生成已取消")
        progress("fetch", index, len(REQUIRED_BARS) + 3, f"拉取 K 线 {bar}")
        candle_cache[bar] = fetch_candles(inst_id, bar, warmup_ms, end_ms)
        if not candle_cache[bar]:
            raise ValueError(f"未获取到 {inst_id} {bar} 历史 K 线，请检查时间段或网络")

    if cancelled():
        raise RuntimeError("生成已取消")
    progress("fetch", len(REQUIRED_BARS) + 1, len(REQUIRED_BARS) + 3, "拉取 OI / 资金费率 / 多空比")
    oi_series = fetch_time_series(
        "/api/v5/rubik/stat/contracts/open-interest-history",
        {"instId": inst_id, "period": "5m"},
        warmup_ms,
        end_ms,
        use_begin_end=True,
    )
    funding_series = fetch_time_series(
        "/api/v5/public/funding-rate-history",
        {"instId": inst_id},
        warmup_ms,
        end_ms,
    )
    long_short_series = fetch_time_series(
        "/api/v5/rubik/stat/contracts/long-short-account-ratio",
        {"ccy": symbol_ccy(inst_id), "period": "5m"},
        warmup_ms,
        end_ms,
        use_begin_end=True,
    )

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    meta = {
        "type": "meta",
        "version": REPLAY_DATASET_VERSION,
        "recorded_at": monitor.now_text(),
        "interval_seconds": int(step_seconds),
        "inst_ids": [inst_id],
        "source": "historical-replay-builder",
        "range_start": start_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "range_end": end_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "frame_count": len(times),
        "notes": "K线来自 OKX 历史接口；盘口为占位，盘口失衡类信号不会触发。",
    }
    total_frames = len(times)
    with output_path.open("w", encoding="utf-8") as file:
        file.write(json.dumps(meta, ensure_ascii=False) + "\n")
        for index, frame_dt in enumerate(times, start=1):
            if cancelled():
                raise RuntimeError("生成已取消")
            progress("build", index, total_frames, f"生成帧 {index}/{total_frames}")
            frame_ts_ms = int(frame_dt.timestamp() * 1000)
            candles: Dict[str, List[Dict[str, Any]]] = {}
            for bar in REQUIRED_BARS:
                candles[bar] = slice_candles_at(candle_cache[bar], frame_ts_ms, bar)
            for bar in OPTIONAL_BARS:
                candles[bar] = []
            minute_rows = candles.get("1m") or []
            last_price = to_float(minute_rows[0].get("close")) if minute_rows else 0.0
            if last_price <= 0:
                for bar in ("5m", "15m", "1H"):
                    rows = candles.get(bar) or []
                    if rows:
                        last_price = to_float(rows[0].get("close"))
                    if last_price > 0:
                        break
            if last_price <= 0:
                continue
            frame = {
                "type": "frame",
                "time": frame_dt.strftime("%Y-%m-%d %H:%M:%S"),
                "inst_id": inst_id,
                "ticker": {"last": last_price, "bid_px": last_price, "ask_px": last_price},
                "candles": candles,
                "open_interest": lookup_series(oi_series, frame_ts_ms),
                "funding_rate": lookup_series(funding_series, frame_ts_ms),
                "long_short_ratio": lookup_long_short(long_short_series, frame_ts_ms),
                "order_book": neutral_order_book(last_price),
                "data_sources": {
                    "source": "historical-replay-builder",
                    "order_book": {"available": False, "fresh": False, "stale": True},
                    "long_short_ratio": {
                        "available": lookup_long_short(long_short_series, frame_ts_ms)["available"],
                        "fresh": True,
                        "stale": False,
                    },
                },
            }
            file.write(json.dumps(frame, ensure_ascii=False) + "\n")

    monitor.load_replay_dataset(output_path)
    progress("done", total_frames, total_frames, "生成完成")
    return {
        "ok": True,
        "path": str(output_path),
        "frame_count": total_frames,
        "inst_id": inst_id,
        "range_start": meta["range_start"],
        "range_end": meta["range_end"],
        "step_seconds": int(step_seconds),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build replay dataset JSONL from OKX historical candles.")
    parser.add_argument("--inst-id", default="ETH-USDT-SWAP")
    parser.add_argument("--start", required=True, help="YYYY-MM-DD HH:MM[:SS]")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD HH:MM[:SS]")
    parser.add_argument("--step-seconds", type=int, default=60)
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    def on_progress(phase: str, current: int, total: int, message: str) -> None:
        print(f"[{phase}] {current}/{total} {message}", flush=True)

    try:
        result = build_historical_replay_dataset(
            inst_id=args.inst_id,
            start_time=args.start,
            end_time=args.end,
            step_seconds=args.step_seconds,
            output_path=Path(args.output),
            progress_callback=on_progress,
        )
    except Exception as exc:
        print(f"failed: {exc}", flush=True)
        return 1
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
