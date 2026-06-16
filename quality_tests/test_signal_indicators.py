import math
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from okx_signal_monitor import (  # noqa: E402
    OkxAiShortTermAssistant,
    RuntimeConfig,
    SignalConfig,
    adx,
    bollinger,
    kdj,
    macd,
    rsi,
    trend_profile_from_candles,
)


LONG = "\u505a\u591a"
SHORT = "\u505a\u7a7a"
WATCH = "\u89c2\u671b"


def sample_candles(count=140):
    rows = []
    base = 100.0
    for index in range(count):
        close = base + (count - index) * 0.08 + math.sin(index / 5) * 0.2
        rows.append({
            "time": f"2026-01-01 00:{index % 60:02d}:00",
            "open": close - 0.04,
            "high": close + 0.25,
            "low": close - 0.22,
            "close": close,
            "volume": 100 + (index % 13) * 7,
            "confirmed": "1",
        })
    return rows


def falling_1m_candles(count=30):
    rows = []
    for index in range(count):
        close = 100.0 + index * 0.12
        rows.append({
            "time": f"2026-01-01 01:{index % 60:02d}:00",
            "open": close + 0.03,
            "high": close + 0.12,
            "low": close - 0.10,
            "close": close,
            "volume": 100,
            "confirmed": "1",
        })
    return rows


def rollover_1m_candles():
    closes = [100.00, 100.06, 100.11, 100.15, 100.20, 100.24, 100.21, 100.18, 100.12, 100.08, 100.02, 99.98]
    rows = []
    for index, close in enumerate(closes):
        rows.append({
            "time": f"2026-01-01 02:{index % 60:02d}:00",
            "open": close + (0.03 if index < 5 else -0.02),
            "high": close + 0.04,
            "low": close - 0.04,
            "close": close,
            "volume": 100 + index,
            "confirmed": "1",
        })
    return rows


def bullish_profile():
    return {
        "trend": "up",
        "data_quality": {
            "is_reliable": True,
            "macd_ready": True,
            "rsi_ready": True,
        },
        "ema_fast": 100.0,
        "ema_slow": 99.0,
        "atr": 0.4,
        "atr_pct": 0.3,
        "recent_high": 104.0,
        "recent_low": 96.0,
        "breakout": "none",
        "distance_to_ema20_atr": 0.5,
        "rsi": {"14": 60.0},
        "macd": {"hist": 1.0, "hist_slope": 0.2},
        "kdj": {"k": 60.0, "d": 55.0},
        "boll": {"bandwidth_pct": 1.0},
        "adx": {"adx": 25.0, "plus_di": 30.0, "minus_di": 15.0},
        "divergence": "none",
    }


def profile_with_trend(trend):
    profile = bullish_profile()
    profile["trend"] = trend
    if trend == "down":
        profile["adx"] = {"adx": 24.0, "plus_di": 12.0, "minus_di": 28.0}
        profile["macd"] = {"hist": -1.0, "hist_slope": -0.2}
        profile["kdj"] = {"k": 42.0, "d": 55.0}
    return profile


def assistant_for_tests():
    return OkxAiShortTermAssistant(
        ["BTC-USDT-SWAP"],
        5,
        "0",
        False,
        False,
        80,
        74,
        False,
        SignalConfig(),
        RuntimeConfig(),
    )


class IndicatorTests(unittest.TestCase):
    def test_indicator_outputs_are_finite(self):
        candles = sample_candles()
        closes = [item["close"] for item in candles]
        values = [
            rsi(closes, 14),
            macd(closes)["hist"],
            kdj(candles)["k"],
            bollinger(closes)["bandwidth_pct"],
            adx(candles)["adx"],
        ]
        for value in values:
            self.assertTrue(math.isfinite(value))

    def test_trend_profile_has_quality_flags(self):
        profile = trend_profile_from_candles(sample_candles())
        self.assertTrue(profile["data_quality"]["is_reliable"])
        self.assertIn(profile["trend"], {"up", "down", "range", "mixed", "unknown"})
        self.assertIn("rsi", profile)
        self.assertIn("macd", profile)
        self.assertIn("adx", profile)

    def test_entry_touch_uses_1m_high_low(self):
        assistant = OkxAiShortTermAssistant(
            ["BTC-USDT-SWAP"],
            5,
            "0",
            False,
            False,
            80,
            74,
            False,
            SignalConfig(),
            RuntimeConfig(),
        )
        snapshot = {
            "candles": {
                "1m": [{
                    "low": 99.0,
                    "high": 101.0,
                    "close": 100.5,
                    "confirmed": "0",
                }],
            },
        }
        touched, source = assistant._entry_touched(snapshot, 100.8, 101.2, 100.5)
        self.assertTrue(touched)
        self.assertEqual(source, "1m_high_low")

    def test_assumed_fill_price_is_conservative(self):
        assistant = OkxAiShortTermAssistant(
            ["BTC-USDT-SWAP"],
            5,
            "0",
            False,
            False,
            80,
            74,
            False,
            SignalConfig(),
            RuntimeConfig(),
        )
        long_price, long_assumption = assistant._assumed_fill_price("做多", 100.0, 101.0, 102.0, "1m_high_low")
        short_price, short_assumption = assistant._assumed_fill_price("做空", 100.0, 101.0, 99.0, "1m_high_low")
        self.assertEqual(long_price, 101.0)
        self.assertEqual(short_price, 100.0)
        self.assertEqual(long_assumption, "conservative_long_entry_high")
        self.assertEqual(short_assumption, "conservative_short_entry_low")

    def test_recent_down_pressure_neutralizes_long_context(self):
        assistant = assistant_for_tests()
        profiles = {bar: bullish_profile() for bar in ("1m", "3m", "5m", "15m", "1H", "4H")}
        context = assistant._market_context(
            price=100.0,
            candles={"1m": falling_1m_candles(), "15m": sample_candles(30)},
            profiles=profiles,
            volume={"multiplier": 1.0},
            open_interest=1000.0,
            oi_change_pct_15m=0.0,
            funding_rate=0.0,
            funding_change_15m=0.0,
            long_short={"long_ratio": 0.5, "short_ratio": 0.5},
            order_book={"available": False},
            volatility={"regime": "normal", "atr_pct_15m": 0.3},
            dynamic_thresholds={"volume_multiplier_p85": 2.0},
        )
        self.assertEqual(context["recent_price_pressure"], "down")
        self.assertEqual(context["bias"], "neutral")
        self.assertEqual(context["strategy_template"], "no_trade_until_alignment")

    def test_score_blocks_long_when_recent_pressure_is_down(self):
        assistant = assistant_for_tests()
        candles = {bar: sample_candles() for bar in ("1m", "3m", "5m", "15m", "1H", "4H")}
        profiles = {bar: bullish_profile() for bar in candles}
        snapshot = {
            "price": 100.0,
            "candles": candles,
            "trend_profiles": profiles,
            "market_context": {
                "bias": "long",
                "regime": "trend_up",
                "recent_price_pressure": "down",
                "order_book_bias": "bid_support",
                "oi_price_state": "price_up_oi_up_new_longs_or_short_pressure",
            },
            "volume": {"direction": "up", "trend": "rising", "multiplier": 1.0},
            "order_book": {"available": False},
            "long_short_ratio": {"long_ratio": 0.5, "short_ratio": 0.5},
            "funding_rate": 0.0,
            "oi_change_pct_15m": 0.0,
            "oi_warmup_ready": True,
        }
        score = assistant.score_snapshot(snapshot, [])
        self.assertEqual(score["raw_direction"], LONG)
        self.assertEqual(score["final_direction"], WATCH)
        self.assertEqual(score["final_trade_score"], 0)
        self.assertEqual(score["direction_guard"], "recent_price_pressure_down_blocks_long")

    def test_score_blocks_long_when_short_term_alignment_is_missing(self):
        assistant = assistant_for_tests()
        candles = {bar: sample_candles() for bar in ("1m", "3m", "5m", "15m", "1H", "4H")}
        profiles = {bar: bullish_profile() for bar in candles}
        snapshot = {
            "price": 100.0,
            "candles": candles,
            "trend_profiles": profiles,
            "market_context": {
                "bias": "long",
                "regime": "trend_up",
                "recent_price_pressure": "neutral",
                "trade_up": 1,
                "trade_down": 0,
                "order_book_bias": "bid_support",
                "oi_price_state": "price_up_oi_up_new_longs_or_short_pressure",
            },
            "volume": {"direction": "up", "trend": "rising", "multiplier": 1.0},
            "order_book": {"available": False},
            "long_short_ratio": {"long_ratio": 0.5, "short_ratio": 0.5},
            "funding_rate": 0.0,
            "oi_change_pct_15m": 0.0,
            "oi_warmup_ready": True,
        }
        score = assistant.score_snapshot(snapshot, [])
        self.assertEqual(score["raw_direction"], LONG)
        self.assertEqual(score["final_direction"], WATCH)
        self.assertEqual(score["final_trade_score"], 0)
        self.assertEqual(score["direction_guard"], "neutral_price_pressure_blocks_long_without_5m_15m_alignment")

    def test_scalp_short_detects_rollover_from_recent_high(self):
        assistant = assistant_for_tests()
        assistant.config.allow_scalp_trade = True
        candles = {bar: sample_candles() for bar in ("3m", "5m", "15m", "1H", "4H")}
        candles["1m"] = rollover_1m_candles()
        profiles = {bar: bullish_profile() for bar in candles}
        profiles["1m"] = profile_with_trend("down")
        snapshot = {
            "price": 100.0,
            "candles": candles,
            "trend_profiles": profiles,
            "market_context": {
                "recent_price_pressure": "neutral",
                "order_book_bias": "neutral",
                "oi_price_state": "oi_flat",
            },
            "volume": {"direction": "down", "trend": "flat", "multiplier": 1.0},
            "order_book": {"available": False, "spread_pct": 0.0},
        }
        view = assistant._scalp_strategy_view(snapshot, {"raw_total_score": 0})
        self.assertEqual(view["direction"], SHORT)
        self.assertLessEqual(view["drawdown_pct_10m"], -0.12)
        self.assertGreater(view["trade_score"], 0)


if __name__ == "__main__":
    unittest.main()
