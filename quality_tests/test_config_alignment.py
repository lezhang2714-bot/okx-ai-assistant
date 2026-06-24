import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from monitor_config_summary import (  # noqa: E402
    MONITOR_BEHAVIOR_DEFAULTS,
    STRATEGY_DEFAULT_ACCURACY_HORIZON_SECONDS,
    STRATEGY_DEFAULT_INTERVAL_SECONDS,
    config_requires_monitor_restart,
    config_value,
    paper_settings_from_log_item,
    recommended_accuracy_horizon_for_strategy,
    sync_strategy_bound_config,
)
from web_control_panel import normalize_config, visible_config_keys  # noqa: E402


class ConfigAlignmentTests(unittest.TestCase):
    def test_l3_default_is_false(self):
        self.assertFalse(MONITOR_BEHAVIOR_DEFAULTS["l3_local_spike_push"])

    def test_config_value_uses_shared_default(self):
        self.assertEqual(config_value({}, "l3_local_spike_push"), False)
        self.assertEqual(config_value({"l3_local_spike_push": True}, "l3_local_spike_push"), False)

    def test_signal_watch_disabled_by_default(self):
        self.assertFalse(MONITOR_BEHAVIOR_DEFAULTS["signal_watch_enabled"])

    def test_factory_default_visible_config(self):
        fresh = normalize_config({})
        visible = visible_config_keys()
        payload = {key: fresh[key] for key in visible if key in fresh}
        again = normalize_config(payload)
        self.assertEqual(again["push_score"], 70)
        self.assertEqual(again["short_push_score"], 68)
        self.assertEqual(again["strategy_mode"], "swing")
        self.assertEqual(again["risk_preference"], "aggressive")
        self.assertEqual(again["interval"], 60)
        self.assertEqual(again.get("custom_inst_ids"), [])
        self.assertEqual(again["inst_ids"], ["ETH-USDT-SWAP"])
        self.assertEqual(again["ai_periodic_interval_minutes"], 10)
        self.assertFalse(config_value(again, "signal_watch_enabled"))

    def test_restart_detects_ai_periodic_interval_change(self):
        before = {"ai_periodic_interval_minutes": 10}
        after = {"ai_periodic_interval_minutes": 15}
        self.assertTrue(config_requires_monitor_restart(before, after))

    def test_restart_detects_wechat_silence_brief_change(self):
        before = {"wechat_silence_brief_minutes": 0}
        after = {"wechat_silence_brief_minutes": 120}
        self.assertTrue(config_requires_monitor_restart(before, after))

    def test_restart_detects_push_score_change(self):
        before = {"push_score": 75, "paper_follow_ai_only": True}
        after = {"push_score": 80, "paper_follow_ai_only": True}
        self.assertTrue(config_requires_monitor_restart(before, after))

    def test_interval_follows_strategy_mode(self):
        for mode, seconds in STRATEGY_DEFAULT_INTERVAL_SECONDS.items():
            synced = sync_strategy_bound_config({"strategy_mode": mode})
            self.assertEqual(synced["interval"], seconds)
        swing = normalize_config({"strategy_mode": "swing", "interval": 5})
        self.assertEqual(swing["interval"], 60)

    def test_accuracy_horizon_follows_strategy_mode(self):
        for mode, seconds in STRATEGY_DEFAULT_ACCURACY_HORIZON_SECONDS.items():
            self.assertEqual(recommended_accuracy_horizon_for_strategy(mode), seconds)
        fresh = normalize_config({})
        self.assertEqual(recommended_accuracy_horizon_for_strategy(fresh["strategy_mode"]), 3600)

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
