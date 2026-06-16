import json
import tempfile
import unittest
from pathlib import Path

import okx_signal_monitor as monitor


class LogRotationTests(unittest.TestCase):
    def test_rotate_and_prune_old_segments(self):
        with tempfile.TemporaryDirectory() as tmp:
            active = Path(tmp) / "okx_signal_analysis.jsonl"
            chunk = json.dumps({"time": "2026-01-01 00:00:00", "inst_id": "BTC-USDT-SWAP"}) + "\n"
            single_limit = len(chunk) * 3
            total_limit = single_limit * 2 + 1

            active.write_text(chunk * 3, encoding="utf-8")
            monitor.rotate_analysis_log_if_needed(active, single_limit, total_limit * 10)
            self.assertFalse(active.exists())
            self.assertTrue(monitor.analysis_log_backup_path(active, 1).exists())

            active.write_text(chunk * 3, encoding="utf-8")
            monitor.rotate_analysis_log_if_needed(active, single_limit, total_limit)
            segments = monitor.list_analysis_log_segments(active)
            total = monitor.analysis_log_total_bytes(active)
            self.assertLessEqual(total, total_limit)
            self.assertGreaterEqual(len(segments), 1)

    def test_tail_reads_newest_segments(self):
        with tempfile.TemporaryDirectory() as tmp:
            active = Path(tmp) / "okx_signal_analysis.jsonl"
            older = monitor.analysis_log_backup_path(active, 1)
            older.write_text('{"time":"2026-01-01 00:00:00","n":1}\n', encoding="utf-8")
            active.write_text('{"time":"2026-01-01 00:00:01","n":2}\n', encoding="utf-8")
            text = monitor.tail_analysis_log_text(active, 4096)
            self.assertIn('"n":1', text)
            self.assertIn('"n":2', text)
            self.assertLess(text.index('"n":1'), text.index('"n":2'))


if __name__ == "__main__":
    unittest.main()
