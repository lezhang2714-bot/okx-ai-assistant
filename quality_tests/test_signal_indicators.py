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

    def test_trend_profile_parameters_follow_bar_granularity(self):
        profile_1m = trend_profile_from_candles(sample_candles(), "1m")
        profile_1d = trend_profile_from_candles(sample_candles(), "1D")
        self.assertEqual(profile_1m["bar"], "1m")
        self.assertEqual(profile_1d["bar"], "1D")
        self.assertNotEqual(profile_1m["profile_params"]["ema"], profile_1d["profile_params"]["ema"])
        self.assertLess(
            profile_1m["profile_params"]["slope_floor_pct"],
            profile_1d["profile_params"]["slope_floor_pct"],
        )

    def test_strategy_bars_and_horizons_are_isolated(self):
        assistant = assistant_for_tests()
        expected = {
            "scalp": ("5m", 15, ("3m", "5m", "15m")),
            "short": ("15m", 15, ("5m", "15m", "1H")),
            "swing": ("1H", 240, ("15m", "1H", "4H")),
            "long": ("1D", 2880, ("4H", "1D", "1W")),
        }
        keys = set()
        for mode, (primary, horizon, forecast_bars) in expected.items():
            assistant.config.strategy_mode = mode
            self.assertEqual(assistant._strategy_score_bars()["primary"], primary)
            self.assertEqual(assistant._effective_forecast_horizon(), horizon)
            spec = assistant._forecast_timeframe_spec()
            self.assertEqual(
                (spec["lead"], spec["target"], spec["background"]),
                forecast_bars,
            )
            keys.add(assistant._forecast_calibration_key("BTC-USDT-SWAP", "test", LONG, "mixed"))
        self.assertEqual(len(keys), 4)
        self.assertTrue(all(key.startswith("forecast:v2:") for key in keys))

    def test_structure_hit_requires_target_state_confirmation(self):
        assistant = assistant_for_tests()
        self.assertFalse(assistant._structure_direction_hit(LONG, "mixed", "mixed"))
        self.assertFalse(assistant._structure_direction_hit(SHORT, "mixed", "mixed"))
        self.assertTrue(assistant._structure_direction_hit(LONG, "mixed", "up"))
        self.assertTrue(assistant._structure_direction_hit(SHORT, "range", "down"))
        self.assertFalse(assistant._structure_direction_hit(LONG, "up", "up"))

    def test_partial_structure_progress_is_not_a_hit(self):
        assistant = assistant_for_tests()
        self.assertTrue(assistant._structure_direction_improved(LONG, "down", "mixed"))
        self.assertFalse(assistant._structure_direction_hit(LONG, "down", "mixed"))

    def test_calibration_tracks_partial_hits_and_probability_error_separately(self):
        assistant = assistant_for_tests()
        assistant._last_calibration_save_at = 10**20
        key = assistant._forecast_calibration_key("BTC-USDT-SWAP", "test", LONG, "mixed")
        assistant._record_calibration_bucket(
            key,
            structure_hit=False,
            partial_structure_hit=True,
            price_hit=False,
            move_pct=-0.1,
            predicted_probability=80,
            auto_disable_below=0.38,
            min_samples_to_disable=12,
        )
        stats = assistant._record_calibration_bucket(
            key,
            structure_hit=True,
            price_hit=False,
            move_pct=0.2,
            predicted_probability=60,
            auto_disable_below=0.38,
            min_samples_to_disable=12,
        )
        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["hits"], 1)
        self.assertEqual(stats["partial_structure_hits"], 1)
        self.assertAlmostEqual(stats["brier_score"], 0.4, places=6)

    def test_calibration_summary_excludes_legacy_forecast_buckets(self):
        assistant = assistant_for_tests()
        current = assistant._forecast_calibration_key("BTC-USDT-SWAP", "test", LONG, "mixed")
        assistant.calibration_state["buckets"] = {
            "forecast:BTC-USDT-SWAP:legacy:做多:mixed": {"total": 100, "hits": 99},
            current: {"total": 2, "hits": 1},
        }
        summary = assistant._calibration_summary("BTC-USDT-SWAP")
        self.assertEqual(list(summary["forecast"]), [current])

    def test_scalp_and_short_forecasts_use_different_target_bars(self):
        assistant = assistant_for_tests()
        snapshot = {
            "trend_profiles": {
                "3m": profile_with_trend("up"),
                "5m": profile_with_trend("mixed"),
                "15m": profile_with_trend("mixed"),
                "1H": profile_with_trend("mixed"),
            },
            "market_context": {
                "recent_price_pressure": "up",
                "regime": "mixed",
                "pressure_windows": {
                    "moves": {"fast": 0.12, "medium": 0.18, "slow": 0.22},
                },
            },
        }
        score = {"final_direction": WATCH}
        assistant.config.strategy_mode = "scalp"
        scalp = assistant._evaluate_structure_evolution(snapshot, [], score)
        self.assertEqual(scalp["lead_bar"], "3m")
        self.assertEqual(scalp["structure_bar"], "5m")
        self.assertTrue(scalp["scenario"].startswith("scalp_"))

        assistant.config.strategy_mode = "short"
        short = assistant._evaluate_structure_evolution(snapshot, [], score)
        self.assertEqual(short["lead_bar"], "5m")
        self.assertEqual(short["structure_bar"], "15m")
        self.assertTrue(short["scenario"].startswith("short_"))

    def test_swing_and_long_forecasts_use_their_own_structure_chain(self):
        assistant = assistant_for_tests()
        score = {"final_direction": WATCH}
        cases = {
            "swing": ("15m", "1H", "4H"),
            "long": ("4H", "1D", "1W"),
        }
        for mode, (lead, target, background) in cases.items():
            assistant.config.strategy_mode = mode
            snapshot = {
                "trend_profiles": {
                    lead: profile_with_trend("up"),
                    target: profile_with_trend("mixed"),
                    background: profile_with_trend("up"),
                },
                "market_context": {"recent_price_pressure": "neutral"},
            }
            forecast = assistant._evaluate_structure_evolution(snapshot, [], score)
            self.assertTrue(forecast["active"])
            self.assertEqual(forecast["lead_bar"], lead)
            self.assertEqual(forecast["structure_bar"], target)
            self.assertEqual(forecast["background_bar"], background)
            self.assertEqual(forecast["to_state"], "up")

    def test_long_direction_uses_daily_and_weekly_structure(self):
        assistant = assistant_for_tests()
        assistant.config.strategy_mode = "long"
        profiles = {bar: bullish_profile() for bar in ("4H", "1D", "1W")}
        direction, tier, _ = assistant._long_direction_meta(
            {"trend_profiles": profiles},
            {"recent_price_pressure": "neutral"},
        )
        self.assertEqual(direction, LONG)
        self.assertEqual(tier, "aligned")

    def test_swing_context_does_not_count_4h_twice(self):
        assistant = assistant_for_tests()
        assistant.config.strategy_mode = "swing"
        bars = assistant._strategy_context_bars()
        self.assertEqual(bars["trade"], ("1H", "4H"))
        self.assertEqual(bars["higher"], ("1D",))

    def test_long_context_uses_ratio_confirmation_with_single_trade_bar(self):
        assistant = assistant_for_tests()
        assistant.config.strategy_mode = "long"
        profiles = {
            "4H": profile_with_trend("range"),
            "1D": bullish_profile(),
            "1W": bullish_profile(),
        }
        context = assistant._market_context(
            price=100.0,
            candles={"4H": sample_candles(30), "15m": sample_candles(30)},
            profiles=profiles,
            volume={"multiplier": 1.0, "source_bar": "1D"},
            open_interest=1000.0,
            oi_change_pct_15m=0.0,
            funding_rate=0.0,
            funding_change_15m=0.0,
            long_short={"long_ratio": 0.5, "short_ratio": 0.5},
            order_book={"available": False},
            volatility={"regime": "normal", "atr_pct": 1.0, "atr_pct_15m": 0.3},
            dynamic_thresholds={"volume_multiplier_p85": 2.0},
        )
        self.assertEqual(context["structural_bias"], "long")
        self.assertEqual(context["trend_vote_metrics"]["groups"]["trade"]["up_ratio"], 1.0)

    def test_pressure_windows_report_real_strategy_timeframes(self):
        assistant = assistant_for_tests()
        assistant.config.strategy_mode = "long"
        profiles = {bar: bullish_profile() for bar in ("4H", "1D", "1W")}
        context = assistant._market_context(
            price=100.0,
            candles={"4H": sample_candles(30), "15m": sample_candles(30)},
            profiles=profiles,
            volume={"multiplier": 1.0, "source_bar": "1D"},
            open_interest=1000.0,
            oi_change_pct_15m=0.0,
            funding_rate=0.0,
            funding_change_15m=0.0,
            long_short={"long_ratio": 0.5, "short_ratio": 0.5},
            order_book={"available": False},
            volatility={"regime": "normal", "atr_pct": 1.0, "atr_pct_15m": 0.3},
            dynamic_thresholds={"volume_multiplier_p85": 2.0},
        )
        self.assertEqual(context["pressure_windows"]["base_bar"], "4H")
        self.assertEqual(context["pressure_windows"]["labels"], ["4H", "8H", "12H", "24H"])
        self.assertIn(context["trend_phase"], {
            "trend_accelerating_up",
            "trend_decelerating_up",
            "breakout_attempt_up",
        })

    def test_volume_stats_preserves_strategy_source_bar(self):
        assistant = assistant_for_tests()
        volume = assistant._volume_stats(sample_candles(30), "1D")
        self.assertEqual(volume["source_bar"], "1D")
        self.assertEqual(volume["source"], "confirmed_1D")

    def test_derivative_windows_follow_strategy(self):
        assistant = assistant_for_tests()
        expected = {"scalp": 15, "short": 15, "swing": 60, "long": 240}
        for mode, minutes in expected.items():
            assistant.config.strategy_mode = mode
            self.assertEqual(assistant._strategy_derivative_window_minutes(), minutes)

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
        self.assertEqual(context["structural_bias"], "long")
        self.assertEqual(context["trend_phase"], "pullback_in_uptrend")
        self.assertEqual(context["strategy_template"], "bullish_pullback_wait_reclaim")

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

    def test_market_source_falls_back_to_recent_valid_value_as_stale(self):
        assistant = assistant_for_tests()
        assistant.last_valid_market_data["BTC-USDT-SWAP:open_interest"] = (
            assistant._now_ts() - 30,
            1234.0,
        )
        value, meta = assistant._collect_market_source(
            source_key="BTC-USDT-SWAP:open_interest",
            cache_key="open_interest:BTC-USDT-SWAP",
            loader=lambda: 0.0,
            validator=lambda item: item > 0,
            stale_after_seconds=180,
        )
        self.assertEqual(value, 1234.0)
        self.assertTrue(meta["available"])
        self.assertTrue(meta["stale"])
        self.assertTrue(meta["fallback"])
        self.assertFalse(meta["fresh"])

    def test_snapshot_quality_marks_missing_critical_source_insufficient(self):
        assistant = assistant_for_tests()
        meta = {
            "ticker": {"available": False, "stale": False},
            "candles.1m": {"available": True, "stale": False, "age_seconds": 1, "observed_at": "2026-01-01 00:00:00"},
            "candles.5m": {"available": True, "stale": False, "age_seconds": 1, "observed_at": "2026-01-01 00:00:00"},
            "candles.15m": {"available": True, "stale": False, "age_seconds": 1, "observed_at": "2026-01-01 00:00:00"},
        }
        quality = assistant._snapshot_quality(meta, started_at=1.0, finished_at=1.2)
        self.assertEqual(quality["overall"], "insufficient")
        self.assertIn("ticker", quality["critical_missing"])

    def test_stale_oi_does_not_trigger_oi_change_signal(self):
        assistant = assistant_for_tests()
        snapshot = {
            "volume": {"multiplier": 0.0},
            "long_short_ratio": {"long_ratio": 0.5, "short_ratio": 0.5},
            "funding_rate": 0.0,
            "funding_change": 0.0,
            "oi_change_pct_15m": 99.0,
            "oi_warmup_ready": True,
            "funding_warmup_ready": True,
            "dynamic_thresholds": {},
            "market_context": {"regime": "range"},
            "trend_profiles": {"5m": {}, "15m": {"rsi": {"14": 50.0}, "macd": {}, "boll": {}, "adx": {}}},
            "order_book": {"available": False},
            "data_sources": {
                "open_interest": {"fresh": False},
                "funding_rate": {"fresh": True},
                "long_short_ratio": {"fresh": True},
                "order_book": {"fresh": True},
                "candles.1m": {"fresh": True},
                "candles.5m": {"fresh": True},
                "candles.15m": {"fresh": True},
            },
        }
        signals = assistant.detect_signals(snapshot)
        self.assertNotIn("oi_change", {item["type"] for item in signals})

    def test_insufficient_snapshot_quality_blocks_direction(self):
        assistant = assistant_for_tests()
        reason = assistant._direction_guard(
            LONG,
            {
                "snapshot_quality": "insufficient",
                "recent_price_pressure": "up",
                "trade_up": 2,
            },
        )
        self.assertEqual(reason, "snapshot_quality_insufficient")


if __name__ == "__main__":
    unittest.main()
