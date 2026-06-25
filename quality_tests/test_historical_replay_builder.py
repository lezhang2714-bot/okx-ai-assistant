import sys
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from historical_replay_builder import (  # noqa: E402
    BAR_MS,
    frame_times,
    lookup_long_short,
    lookup_series,
    neutral_order_book,
    parse_replay_time,
    slice_candles_at,
)


class HistoricalReplayBuilderTests(unittest.TestCase):
    def test_parse_replay_time_accepts_datetime_local(self) -> None:
        parsed = parse_replay_time("2026-06-01T12:30")
        self.assertEqual(parsed, datetime(2026, 6, 1, 12, 30))

    def test_frame_times_respects_step(self) -> None:
        start = datetime(2026, 6, 1, 0, 0, 0)
        end = datetime(2026, 6, 1, 0, 10, 0)
        times = frame_times(start, end, 300)
        self.assertEqual(len(times), 3)
        self.assertEqual(times[-1], end)

    def test_slice_candles_marks_confirmed_at_frame_time(self) -> None:
        open_ms = 1_700_000_000_000
        candles = [
            {
                "time": "2023-11-14 22:13:20",
                "open": 1.0,
                "high": 2.0,
                "low": 0.5,
                "close": 1.5,
                "volume": 10.0,
                "confirmed": "1",
                "_open_ms": open_ms,
            }
        ]
        frame_ts = open_ms + BAR_MS["1m"] - 1
        rows = slice_candles_at(candles, frame_ts, "1m")
        self.assertEqual(rows[0]["confirmed"], "0")
        rows_closed = slice_candles_at(candles, open_ms + BAR_MS["1m"], "1m")
        self.assertEqual(rows_closed[0]["confirmed"], "1")

    def test_lookup_series_returns_latest_before_frame(self) -> None:
        series = [(1_000, 1.0), (2_000, 2.0), (3_000, 3.0)]
        self.assertEqual(lookup_series(series, 2_500), 2.0)
        self.assertEqual(lookup_series(series, 500), 0.0)

    def test_lookup_long_short_available(self) -> None:
        payload = lookup_long_short([(1_000, 1.5)], 2_000)
        self.assertTrue(payload["available"])
        self.assertGreater(payload["long_ratio"], 0.0)

    def test_neutral_order_book(self) -> None:
        book = neutral_order_book(123.4)
        self.assertFalse(book["available"])
        self.assertEqual(book["best_bid"], 123.4)


if __name__ == "__main__":
    unittest.main()
