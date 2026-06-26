import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from web_control_panel import (  # noqa: E402
    diagnostic_replay_log_has_data,
    iter_diagnostic_log_entries,
    render_accuracy_chart_svg,
)


class DiagnosticExportTests(unittest.TestCase):
    def test_render_accuracy_chart_svg_empty(self):
        svg = render_accuracy_chart_svg({"inst_id": "ETH-USDT-SWAP", "scope": "session", "points": []})
        self.assertIn("暂无压测点", svg)
        self.assertTrue(svg.startswith("<?xml"))

    def test_render_accuracy_chart_svg_with_points(self):
        points = [
            {"time": "2026-01-01 10:00:00", "price": 3200, "direction": "做多", "raw_direction": "做多"},
            {"time": "2026-01-01 10:01:00", "price": 3210, "direction": "做空", "raw_direction": "做多"},
        ]
        svg = render_accuracy_chart_svg(
            {
                "inst_id": "ETH-USDT-SWAP",
                "scope": "session",
                "horizon_seconds": 900,
                "summary": {"total": 2, "prediction_accuracy_pct": 50},
                "points": points,
            }
        )
        self.assertIn("polyline", svg)
        self.assertIn("ETH-USDT-SWAP", svg)

    def test_iter_diagnostic_log_entries_returns_list(self):
        entries = iter_diagnostic_log_entries()
        self.assertIsInstance(entries, list)
        for archive_name, path, max_bytes, prefer_tail in entries:
            self.assertTrue(archive_name)
            self.assertIsInstance(path, Path)
            self.assertGreater(max_bytes, 0)

    def test_diagnostic_replay_log_has_data_is_bool(self):
        self.assertIsInstance(diagnostic_replay_log_has_data(), bool)


if __name__ == "__main__":
    unittest.main()
