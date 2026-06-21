import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from monitor_config_summary import (  # noqa: E402
    MONITOR_BEHAVIOR_DEFAULTS,
    config_requires_monitor_restart,
    config_value,
    paper_settings_from_log_item,
)


class ConfigAlignmentTests(unittest.TestCase):
    def test_l3_default_is_false(self):
        self.assertFalse(MONITOR_BEHAVIOR_DEFAULTS["l3_local_spike_push"])

    def test_config_value_uses_shared_default(self):
        self.assertEqual(config_value({}, "l3_local_spike_push"), False)
        self.assertEqual(config_value({"l3_local_spike_push": True}, "l3_local_spike_push"), True)

    def test_restart_detects_push_score_change(self):
        before = {"push_score": 75, "paper_follow_ai_only": True}
        after = {"push_score": 80, "paper_follow_ai_only": True}
        self.assertTrue(config_requires_monitor_restart(before, after))

    def test_paper_settings_prefers_log_snapshot(self):
        item = {
            "config_snapshot": {
                "paper_follow_ai_only": False,
                "paper_fee_bps": 8.0,
            }
        }
        settings, source = paper_settings_from_log_item(item, {"paper_follow_ai_only": True, "paper_fee_bps": 5.0})
        self.assertEqual(source, "log_snapshot")
        self.assertFalse(settings["paper_follow_ai_only"])
        self.assertEqual(settings["paper_fee_bps"], 8.0)


if __name__ == "__main__":
    unittest.main()
