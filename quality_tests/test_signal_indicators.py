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


if __name__ == "__main__":
    unittest.main()
