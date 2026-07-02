import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from okx_signal_monitor import (  # noqa: E402
    OkxAiShortTermAssistant,
    RuntimeConfig,
    SignalConfig,
    apply_forward_view_to_parsed,
    build_wechat_push_format_preview,
    format_ai_suggestion,
    normalize_ai_parsed,
    push_monitor_lifecycle_briefs,
    push_wechat_shadow_copy,
    resolve_ai_suggestion,
    wechat_confidence_label,
)


LONG = "\u505a\u591a"
SHORT = "\u505a\u7a7a"
WATCH = "\u89c2\u671b"


def make_assistant(**config_overrides):
    config = SignalConfig(**config_overrides)
    return OkxAiShortTermAssistant(
        instruments=["BTC-USDT-SWAP"],
        interval=5,
        flag="0",
        ai_enabled=True,
        push_enabled=False,
        push_score=75,
        short_push_score=75,
        dry_run_ai=False,
        config=config,
        runtime_config=RuntimeConfig(),
    )


class DecisionPipelineTests(unittest.TestCase):
    def test_merge_prefers_valid_ai_forward_view(self):
        assistant = make_assistant()
        score = {
            "direction": WATCH,
            "raw_total_score": 55,
            "final_trade_score": 0,
            "risk_level": "\u4e2d",
            "strategy_views": {"scalp": {}},
        }
        trigger = {"level": "L2", "ai_invoked": True, "reasons": ["multi_signal"]}
        analysis = {
            "valid_json": True,
            "parsed": {
                "direction": LONG,
                "confidence": 78,
                "push_recommendation": "trade",
                "entry": "100",
                "stop_loss": "99",
                "take_profit": "102",
                "risk_level": "\u4e2d",
                "forward_view": {
                    "horizon_minutes": 15,
                    "direction": LONG,
                    "probability": 78,
                    "summary": "test forward",
                    "invalidation": "break 99",
                },
            },
        }
        decision = assistant.merge_final_decision(analysis, score, [{"type": "volume_spike"}], trigger, {})
        self.assertEqual(decision["decision_source"], "ai")
        self.assertEqual(decision["direction"], LONG)
        self.assertEqual(decision["forward_view"]["direction"], LONG)

    def test_ai_watch_promotes_to_trade_when_direction_and_score_qualify(self):
        assistant = make_assistant()
        score = {
            "direction": LONG,
            "raw_total_score": 36,
            "final_trade_score": 0,
            "risk_level": "\u4e2d",
            "strategy_views": {"scalp": {}},
        }
        trigger = {"level": "L2", "ai_invoked": True, "reasons": ["trade_signal"]}
        analysis = {
            "valid_json": True,
            "parsed": {
                "direction": LONG,
                "confidence": 78,
                "push_recommendation": "watch",
                "entry": "1700",
                "stop_loss": "1695",
                "take_profit": "1720",
                "risk_level": "\u4e2d",
                "forward_view": {
                    "horizon_minutes": 240,
                    "direction": LONG,
                    "probability": 78,
                    "summary": "test",
                    "invalidation": "break 1690",
                },
            },
        }
        decision = assistant.merge_final_decision(
            analysis,
            score,
            [{"type": "volume_spike"}],
            trigger,
            {},
        )
        self.assertEqual(decision["direction"], LONG)
        self.assertEqual(decision["confidence"], 78)
        self.assertEqual(decision["push_recommendation"], "trade")
        self.assertEqual(
            assistant.push_gate(decision, [{"type": "volume_spike"}], score),
            "trade",
        )

    def test_ai_watch_stays_none_when_score_below_trade_threshold(self):
        assistant = make_assistant()
        score = {
            "direction": LONG,
            "raw_total_score": 36,
            "final_trade_score": 0,
            "risk_level": "\u4e2d",
            "strategy_views": {"scalp": {}},
        }
        trigger = {"level": "L2", "ai_invoked": True, "reasons": ["trade_signal"]}
        analysis = {
            "valid_json": True,
            "parsed": {
                "direction": LONG,
                "confidence": 68,
                "push_recommendation": "watch",
                "risk_level": "\u4e2d",
                "forward_view": {"direction": LONG, "probability": 68},
            },
        }
        decision = assistant.merge_final_decision(
            analysis,
            score,
            [{"type": "volume_spike"}],
            trigger,
            {},
        )
        self.assertEqual(decision["direction"], LONG)
        self.assertEqual(decision["push_recommendation"], "none")
        self.assertEqual(assistant.push_gate(decision, [{"type": "volume_spike"}], score), "")

    def test_local_screening_without_ai(self):
        assistant = make_assistant()
        score = {
            "direction": LONG,
            "final_direction": LONG,
            "raw_total_score": 72,
            "final_trade_score": 72,
            "risk_level": "\u4e2d",
            "strategy_views": {"scalp": {}},
        }
        trigger = {"level": "L1", "ai_invoked": False, "reasons": ["funding_hot"]}
        decision = assistant.merge_final_decision(None, score, [{"type": "funding_hot"}], trigger, {})
        self.assertEqual(decision["decision_source"], "local_screening")
        self.assertEqual(decision["direction"], LONG)
        self.assertNotEqual(decision["push_recommendation"], "trade")

    def test_strong_local_decision_can_push_trade_without_ai(self):
        assistant = make_assistant()
        score = {
            "direction": LONG,
            "final_direction": LONG,
            "raw_total_score": 84,
            "final_trade_score": 82,
            "entry": "100 - 101",
            "stop_loss": "98",
            "take_profit": "104 / 106",
            "risk_level": "\u4e2d",
            "entry_plan": {"quality": "breakout_valid"},
            "strategy_views": {"scalp": {}},
        }
        trigger = {"level": "L1", "ai_invoked": False, "reasons": ["trade_signal"]}
        signals = [{"type": "structure_break", "desc": "breakout up"}]
        decision = assistant.merge_final_decision(None, score, signals, trigger, {})
        self.assertEqual(decision["direction"], LONG)
        self.assertEqual(decision["confidence"], 82)
        self.assertEqual(decision["push_recommendation"], "trade")
        self.assertEqual(decision["entry"], "100 - 101")
        self.assertEqual(decision["rule_audit"]["local_trade_threshold"], 80)
        self.assertTrue(decision["rule_audit"]["local_trade_eligible"])
        self.assertEqual(assistant.push_gate(decision, signals, score), "trade")
        self.assertEqual(
            assistant._wechat_push_block_reason("trade", decision, {}, score, signals, trigger, None),
            "trade_wechat_requires_ai_review",
        )

    def test_local_push_review_triggers_ai_call(self):
        assistant = make_assistant(strategy_mode="swing", short_push_score=70)
        snapshot = {
            "inst_id": "ETH-USDT-SWAP",
            "price": 1668.0,
            "funding_rate": 0.0001,
            "trend_profiles": {"15m": {"trend": "down"}, "1H": {"trend": "mixed"}},
            "market_context": {"regime": "trend_down", "recent_price_pressure": "down"},
        }
        score = {
            "direction": SHORT,
            "final_direction": SHORT,
            "raw_direction": SHORT,
            "raw_total_score": 76,
            "final_trade_score": 75,
            "risk_level": "\u4e2d",
            "entry_plan": {"quality": "wait_confirmation"},
            "strategy_views": {"scalp": {}},
        }
        signals = [{"type": "order_book_imbalance", "desc": "book imbalance -0.72"}]
        trigger = assistant.evaluate_ai_trigger("ETH-USDT-SWAP", signals, score, snapshot)
        self.assertTrue(trigger["should_call_ai"])
        self.assertIn("local_push_review", trigger["reasons"])

    def test_ai_failure_blocks_wechat_trade_push(self):
        assistant = make_assistant()
        score = {
            "direction": SHORT,
            "final_direction": SHORT,
            "raw_total_score": 86,
            "final_trade_score": 81,
            "risk_level": "\u4e2d",
            "entry_plan": {"quality": "breakout_valid"},
            "strategy_views": {"scalp": {}},
        }
        trigger = {"level": "L2", "ai_invoked": True, "reasons": ["local_push_review"]}
        analysis = {"valid_json": False, "error": "timeout"}
        decision = assistant.merge_final_decision(
            analysis,
            score,
            [{"type": "order_book_imbalance", "desc": "book"}],
            trigger,
            {},
        )
        self.assertEqual(decision["decision_source"], "local_fallback")
        self.assertEqual(
            assistant._wechat_push_block_reason(
                "trade", decision, {}, score, [{"type": "order_book_imbalance"}], trigger, analysis
            ),
            "trade_wechat_ai_review_failed",
        )

    def test_forecast_wechat_still_allowed_without_ai(self):
        assistant = make_assistant()
        forecast = {
            "active": True,
            "direction": SHORT,
            "probability": 72,
            "calibrated_probability": 72,
            "effective_push_threshold": 58,
        }
        score = {"structure_forecast": forecast, "strategy_views": {"scalp": {}}}
        trigger = {"level": "L1", "ai_invoked": False, "reasons": ["structure_forecast_active"]}
        decision = {"direction": WATCH, "confidence": 50, "push_recommendation": "none"}
        block = assistant._wechat_push_block_reason(
            "forecast",
            decision,
            forecast,
            score,
            [{"type": "volume_spike"}],
            trigger,
            None,
        )
        self.assertEqual(block, "")

    def test_ai_failure_uses_strong_local_fallback(self):
        assistant = make_assistant()
        score = {
            "direction": SHORT,
            "final_direction": SHORT,
            "raw_total_score": 86,
            "final_trade_score": 81,
            "risk_level": "\u4e2d",
            "entry_plan": {
                "quality": "breakout_valid",
                "entry": "100",
                "stop_loss": "102",
                "take_profit": "96",
            },
            "strategy_views": {"scalp": {}},
        }
        trigger = {"level": "L2", "ai_invoked": True, "reasons": ["multi_signal"]}
        analysis = {"valid_json": False, "error": "timeout"}
        decision = assistant.merge_final_decision(
            analysis,
            score,
            [{"type": "volume_spike", "desc": "volume"}],
            trigger,
            {},
        )
        self.assertEqual(decision["decision_source"], "local_fallback")
        self.assertEqual(decision["direction"], SHORT)
        self.assertEqual(decision["push_recommendation"], "trade")
        self.assertTrue(decision["ai_called"])

    def test_push_gate_blocks_misaligned_ai_trade(self):
        assistant = make_assistant(forward_require_forecast_alignment=True)
        final_decision = {
            "direction": LONG,
            "confidence": 80,
            "push_recommendation": "trade",
            "decision_source": "ai",
            "forward_view": {"direction": LONG, "probability": 80},
        }
        score = {
            "structure_forecast": {
                "active": True,
                "direction": SHORT,
                "probability": 70,
            },
            "strategy_views": {"scalp": {}},
        }
        signals = [{"type": "volume_spike"}]
        self.assertEqual(assistant.push_gate(final_decision, signals, score), "")

    def test_push_gate_allows_aligned_ai_trade(self):
        assistant = make_assistant(forward_require_forecast_alignment=True)
        final_decision = {
            "direction": LONG,
            "confidence": 80,
            "push_recommendation": "trade",
            "decision_source": "ai",
            "forward_view": {"direction": LONG, "probability": 80},
        }
        score = {
            "structure_forecast": {
                "active": True,
                "direction": LONG,
                "probability": 70,
            },
            "strategy_views": {"scalp": {}},
        }
        signals = [{"type": "volume_spike"}]
        self.assertEqual(assistant.push_gate(final_decision, signals, score), "trade")

    def test_l2_macd_only_does_not_qualify_ai(self):
        assistant = make_assistant()
        qualified = assistant._l2_qualifies_ai_call(
            ["trade_signal"],
            {"macd_momentum_change"},
            {"structure_forecast": {"active": False}, "strategy_views": {"scalp": {}}},
            {"market_context": {"regime": "range"}},
        )
        self.assertFalse(qualified)

    def test_l2_multi_signal_qualifies_ai(self):
        assistant = make_assistant()
        qualified = assistant._l2_qualifies_ai_call(
            ["multi_signal"],
            {"volume_spike", "structure_break"},
            {"structure_forecast": {"active": False}, "strategy_views": {"scalp": {}}},
            {"market_context": {"regime": "range"}},
        )
        self.assertTrue(qualified)

    def test_post_audit_downgrades_misaligned_trade(self):
        assistant = make_assistant(forward_require_forecast_alignment=True)
        final_decision = {
            "direction": LONG,
            "confidence": 80,
            "push_recommendation": "trade",
            "decision_source": "ai",
            "forward_view": {"direction": LONG, "probability": 80},
        }
        score = {
            "structure_forecast": {
                "active": True,
                "direction": SHORT,
                "probability": 70,
            },
            "strategy_views": {"scalp": {}},
        }
        audited = assistant._apply_decision_post_audit(
            final_decision,
            score,
            [{"type": "volume_spike"}],
            {"level": "L2", "reasons": ["multi_signal"]},
            {"market_context": {"recent_price_pressure": "neutral", "regime": "trend_up"}},
        )
        self.assertNotEqual(audited.get("push_recommendation"), "trade")

    def test_paper_direction_follows_ai_forward_only(self):
        assistant = make_assistant(paper_follow_ai_only=True)
        ai_decision = {
            "decision_source": "ai",
            "direction": LONG,
            "forward_view": {"direction": SHORT},
        }
        self.assertEqual(assistant._paper_direction_from_final_decision(ai_decision), SHORT)
        assistant_off = make_assistant(paper_follow_ai_only=False)
        self.assertEqual(assistant_off._paper_direction_from_final_decision(ai_decision), LONG)
        local_decision = {"decision_source": "local_screening", "direction": LONG}
        self.assertEqual(assistant._paper_direction_from_final_decision(local_decision), WATCH)

    def test_l2_single_structure_break_qualifies_with_score(self):
        assistant = make_assistant()
        qualified = assistant._l2_qualifies_ai_call(
            ["trade_signal"],
            {"structure_break"},
            {
                "raw_total_score": 66,
                "structure_forecast": {"active": False},
                "strategy_views": {"scalp": {}},
            },
            {"market_context": {"regime": "range"}},
        )
        self.assertTrue(qualified)
        low_score = assistant._l2_qualifies_ai_call(
            ["trade_signal"],
            {"structure_break"},
            {
                "raw_total_score": 60,
                "structure_forecast": {"active": False},
                "strategy_views": {"scalp": {}},
            },
            {"market_context": {"regime": "range"}},
        )
        self.assertFalse(low_score)

    def test_structure_forecast_active_triggers_ai(self):
        assistant = make_assistant()
        score = {
            "direction": WATCH,
            "raw_total_score": 58,
            "structure_forecast": {
                "active": True,
                "scenario": "breakout_watch",
                "direction": LONG,
            },
            "strategy_views": {"scalp": {}},
        }
        signals = [{"type": "rsi_extreme", "desc": "15m RSI extreme 82"}]
        snapshot = {"funding_rate": 0.0001, "market_context": {"regime": "range"}}
        trigger = assistant.evaluate_ai_trigger("BTC-USDT-SWAP", signals, score, snapshot)
        self.assertEqual(trigger["level"], "L2")
        self.assertIn("structure_forecast_active", trigger["reasons"])
        self.assertTrue(trigger["should_call_ai"])

    def test_swing_l2_blocks_weak_multi_signal(self):
        assistant = make_assistant(strategy_mode="swing", risk_preference="aggressive")
        score = {
            "direction": LONG,
            "raw_direction": LONG,
            "final_direction": LONG,
            "direction_score": 62,
            "raw_total_score": 65,
            "structure_forecast": {"active": False},
            "strategy_views": {"scalp": {}},
        }
        signals = [
            {"type": "macd_momentum_change", "desc": "macd"},
            {"type": "order_book_imbalance", "desc": "book"},
        ]
        trigger = assistant.evaluate_ai_trigger(
            "BTC-USDT-SWAP",
            signals,
            score,
            {
                "funding_rate": 0.0001,
                "trend_profiles": {"15m": {"trend": "up"}, "1H": {"trend": "up"}},
                "market_context": {"regime": "trend_up"},
            },
        )
        self.assertEqual(trigger["level"], "L1")
        self.assertEqual(trigger.get("candidate_level"), "L2")
        self.assertFalse(trigger["should_call_ai"])
        self.assertEqual(trigger.get("skip_reason"), "l2_not_qualified")

    def test_swing_l2_allows_mature_structure_signal(self):
        assistant = make_assistant(strategy_mode="swing", risk_preference="aggressive")
        score = {
            "direction": LONG,
            "raw_direction": LONG,
            "final_direction": LONG,
            "direction_score": 49,
            "raw_total_score": 62,
            "structure_forecast": {"active": False},
            "strategy_views": {"scalp": {}},
        }
        trigger = assistant.evaluate_ai_trigger(
            "BTC-USDT-SWAP",
            [{"type": "structure_break", "desc": "break up"}],
            score,
            {
                "funding_rate": 0.0001,
                "trend_profiles": {"15m": {"trend": "up"}, "1H": {"trend": "mixed"}},
                "market_context": {"regime": "trend_up"},
            },
        )
        self.assertEqual(trigger["level"], "L2")
        self.assertTrue(trigger["should_call_ai"])
        self.assertEqual(trigger.get("effective_ai_call_min_interval"), 300)

    def test_swing_l2_cooldown_ignores_fingerprint_changes(self):
        assistant = make_assistant(strategy_mode="swing", risk_preference="aggressive")
        inst = "BTC-USDT-SWAP"
        assistant.last_ai_fingerprint[inst] = "old:fingerprint"
        assistant.last_ai_call_at[inst] = assistant._now_ts()
        score = {
            "direction": SHORT,
            "raw_direction": SHORT,
            "final_direction": SHORT,
            "direction_score": 60,
            "raw_total_score": 64,
            "structure_forecast": {"active": False},
            "strategy_views": {"scalp": {}},
        }
        trigger = assistant.evaluate_ai_trigger(
            inst,
            [{"type": "structure_break", "desc": "break down"}],
            score,
            {
                "funding_rate": 0.0001,
                "trend_profiles": {"15m": {"trend": "down"}, "1H": {"trend": "mixed"}},
                "market_context": {"regime": "trend_down"},
            },
        )
        self.assertFalse(trigger["should_call_ai"])
        self.assertEqual(trigger.get("skip_reason"), "fingerprint_cooldown")

    def test_swing_sustained_displacement_triggers_once(self):
        assistant = make_assistant(strategy_mode="swing", risk_preference="aggressive")
        score = {
            "direction": LONG,
            "raw_direction": LONG,
            "final_direction": LONG,
            "prior_direction": LONG,
            "direction_score": 46,
            "raw_total_score": 52,
            "structure_forecast": {"active": False},
            "strategy_views": {"scalp": {}},
        }
        snapshot = {
            "funding_rate": 0.0001,
            "trend_profiles": {
                "15m": {"trend": "up"},
                "1H": {"trend": "mixed", "atr_pct": 0.80},
            },
            "market_context": {
                "regime": "mixed",
                "pressure_windows": {"moves": {"30m": 0.55, "45m": 0.72, "60m": 0.78}},
            },
        }
        first = assistant.evaluate_ai_trigger("BTC-USDT-SWAP", [], score, snapshot)
        second = assistant.evaluate_ai_trigger("BTC-USDT-SWAP", [], score, snapshot)
        self.assertEqual(first["level"], "L2")
        self.assertIn("sustained_displacement", first["reasons"])
        self.assertTrue(first["should_call_ai"])
        self.assertEqual(second["level"], "L0")

    def test_swing_high_probability_forecast_can_trigger_without_strong_signal(self):
        assistant = make_assistant(strategy_mode="swing", risk_preference="aggressive")
        score = {
            "direction": SHORT,
            "raw_direction": SHORT,
            "final_direction": SHORT,
            "prior_direction": SHORT,
            "direction_score": 46,
            "raw_total_score": 52,
            "structure_forecast": {
                "active": True,
                "scenario": "swing_structure_down",
                "direction": SHORT,
                "probability": 68,
                "effective_push_threshold": 58,
            },
            "strategy_views": {"scalp": {}},
        }
        trigger = assistant.evaluate_ai_trigger(
            "BTC-USDT-SWAP",
            [{"type": "macd_momentum_change", "desc": "macd"}],
            score,
            {
                "funding_rate": 0.0001,
                "trend_profiles": {
                    "15m": {"trend": "down"},
                    "1H": {"trend": "mixed", "atr_pct": 0.8},
                },
                "market_context": {"regime": "mixed", "pressure_windows": {"moves": {}}},
            },
        )
        self.assertEqual(trigger["level"], "L2")
        self.assertIn("high_probability_forecast", trigger["reasons"])
        self.assertTrue(trigger["should_call_ai"])

    def test_swing_direction_reversal_bypasses_cooldown_once(self):
        assistant = make_assistant(strategy_mode="swing", risk_preference="aggressive")
        inst = "BTC-USDT-SWAP"
        assistant.last_ai_call_at[inst] = assistant._now_ts()
        assistant.last_ai_direction[inst] = SHORT
        score = {
            "direction": LONG,
            "raw_direction": LONG,
            "final_direction": LONG,
            "prior_direction": SHORT,
            "direction_score": 48,
            "raw_total_score": 54,
            "structure_forecast": {"active": False},
            "strategy_views": {"scalp": {}},
        }
        trigger = assistant.evaluate_ai_trigger(
            inst,
            [{"type": "macd_momentum_change", "desc": "macd"}],
            score,
            {
                "funding_rate": 0.0001,
                "trend_profiles": {
                    "15m": {"trend": "up"},
                    "1H": {"trend": "mixed", "atr_pct": 0.8},
                },
                "market_context": {"regime": "mixed", "pressure_windows": {"moves": {}}},
            },
        )
        self.assertIn("direction_reversal", trigger["reasons"])
        self.assertTrue(trigger["should_call_ai"])

    def test_l3_respects_fingerprint_cooldown(self):
        assistant = make_assistant()
        score = {
            "direction": LONG,
            "raw_total_score": 70,
            "strategy_views": {
                "scalp": {
                    "action_level": "急速异动",
                    "score": 65,
                    "direction": LONG,
                }
            },
        }
        signals = [{"type": "volume_spike", "desc": "volume"}]
        snapshot = {"funding_rate": 0.0001, "market_context": {"regime": "trend_up"}}
        inst = "BTC-USDT-SWAP"
        assistant.last_ai_fingerprint[inst] = assistant._signal_fingerprint(signals, score)
        assistant.last_ai_call_at[inst] = assistant._now_ts()
        trigger = assistant.evaluate_ai_trigger(inst, signals, score, snapshot)
        self.assertEqual(trigger["level"], "L3")
        self.assertFalse(trigger["should_call_ai"])
        self.assertEqual(trigger.get("skip_reason"), "fingerprint_cooldown")

    def test_swing_spike_requires_two_confirmed_rounds(self):
        assistant = make_assistant(strategy_mode="swing", spike_push_score=62)
        score = {
            "direction": SHORT,
            "raw_direction": SHORT,
            "final_direction": SHORT,
            "raw_total_score": 70,
            "strategy_views": {
                "scalp": {
                    "action_level": "\u6025\u901f\u5f02\u52a8",
                    "score": 76,
                    "direction": SHORT,
                    "move_pct_5m": -0.30,
                    "move_pct_10m": -0.45,
                }
            },
        }
        signals = [{"type": "structure_break", "desc": "break down"}]
        snapshot = {
            "funding_rate": 0.0001,
            "trend_profiles": {"15m": {"trend": "down"}},
            "market_context": {"regime": "trend_down"},
        }
        first = assistant.evaluate_ai_trigger("BTC-USDT-SWAP", signals, score, snapshot)
        second = assistant.evaluate_ai_trigger("BTC-USDT-SWAP", signals, score, snapshot)
        third = assistant.evaluate_ai_trigger("BTC-USDT-SWAP", signals, score, snapshot)
        self.assertNotEqual(first["level"], "L3")
        self.assertEqual(first.get("spike_filter_reason"), "strategy_spike_confirming(1/2)")
        self.assertEqual(second["level"], "L3")
        self.assertIn("scalp_spike", second["reasons"])
        self.assertEqual(second.get("effective_spike_score"), 72)
        self.assertNotEqual(third["level"], "L3")
        self.assertEqual(third.get("spike_filter_reason"), "strategy_spike_event_active")
        score["strategy_views"]["scalp"]["score"] = 60
        assistant.evaluate_ai_trigger("BTC-USDT-SWAP", signals, score, snapshot)
        score["strategy_views"]["scalp"]["score"] = 76
        assistant.evaluate_ai_trigger("BTC-USDT-SWAP", signals, score, snapshot)
        retriggered = assistant.evaluate_ai_trigger("BTC-USDT-SWAP", signals, score, snapshot)
        self.assertEqual(retriggered["level"], "L3")

    def test_periodic_ai_review_triggers_without_signals(self):
        assistant = make_assistant(strategy_mode="swing", ai_periodic_interval_minutes=10)
        score = {"direction": WATCH, "strategy_views": {"scalp": {}}}
        snapshot = {
            "funding_rate": 0.0001,
            "trend_profiles": {},
            "market_context": {"regime": "mixed"},
        }
        trigger = assistant.evaluate_ai_trigger("BTC-USDT-SWAP", [], score, snapshot)
        self.assertTrue(trigger["should_call_ai"])
        self.assertIn("periodic_review", trigger["reasons"])
        self.assertEqual(trigger["level"], "L2")
        self.assertTrue(str(trigger.get("fingerprint", "")).startswith("periodic:"))

    def test_periodic_ai_respects_interval_after_last_call(self):
        assistant = make_assistant(strategy_mode="swing", ai_periodic_interval_minutes=10)
        score = {"direction": WATCH, "strategy_views": {"scalp": {}}}
        snapshot = {
            "funding_rate": 0.0001,
            "trend_profiles": {},
            "market_context": {"regime": "mixed"},
        }
        assistant.last_ai_periodic_call_at["BTC-USDT-SWAP"] = assistant._now_ts()
        trigger = assistant.evaluate_ai_trigger("BTC-USDT-SWAP", [], score, snapshot)
        self.assertFalse(trigger["should_call_ai"])
        self.assertEqual(trigger["level"], "L0")

    def test_periodic_ai_disabled_when_interval_zero(self):
        assistant = make_assistant(strategy_mode="swing", ai_periodic_interval_minutes=0)
        score = {"direction": WATCH, "strategy_views": {"scalp": {}}}
        snapshot = {
            "funding_rate": 0.0001,
            "trend_profiles": {},
            "market_context": {"regime": "mixed"},
        }
        trigger = assistant.evaluate_ai_trigger("BTC-USDT-SWAP", [], score, snapshot)
        self.assertFalse(trigger["should_call_ai"])
        self.assertEqual(trigger["level"], "L0")

    def test_ai_prompt_uses_strategy_horizon_and_periodic_hint(self):
        assistant = make_assistant(strategy_mode="swing", ai_periodic_interval_minutes=10)
        snapshot = {
            "inst_id": "ETH-USDT-SWAP",
            "price": 1665.3,
            "time": "2026-06-24 10:00:00",
            "funding_rate": 0.0001,
            "funding_change": 0.0,
            "funding_warmup_ready": True,
            "oi_warmup_ready": True,
            "open_interest": 1000,
            "oi_change_pct_15m": 0.0,
            "volume": {"multiplier": 1.0},
            "long_short_ratio": {"available": False},
            "candles": {},
            "trend_profiles": {},
            "market_context": {"regime": "mixed", "recent_price_pressure": "neutral"},
            "volatility": {},
            "dynamic_thresholds": {},
            "instrument_profile": {},
            "snapshot_quality": {},
            "data_sources": {},
            "best_bid": 1665.0,
            "best_ask": 1665.5,
            "order_book": {},
        }
        score = {
            "direction": WATCH,
            "strategy_views": {"scalp": {}},
            "trends": {},
            "market_regime": "mixed",
        }
        trigger = {
            "level": "L2",
            "reasons": ["periodic_review"],
            "fingerprint": "periodic:1",
        }
        prompt = assistant._ai_prompt(snapshot, [], score, trigger)
        self.assertIn("periodic_review", prompt)
        self.assertIn("horizon_minutes 默认 240", prompt)
        self.assertIn('"horizon_minutes":240', prompt)
        self.assertIn("主周期 1H、4H", prompt)
        self.assertIn("trend.timeframes 仅允许: 15m、1H、4H", prompt)
        self.assertIn('"1H":"mixed"', prompt)
        self.assertIn("1665.3", prompt)

    def test_normalize_ai_parsed_filters_extra_timeframes_for_swing(self):
        parsed = normalize_ai_parsed(
            {
                "direction": WATCH,
                "confidence": 55,
                "risk_level": "中",
                "push_recommendation": "none",
                "entry": "-",
                "stop_loss": "-",
                "take_profit": "-",
                "trend": {
                    "summary": "test",
                    "timeframes": {
                        "1m": "down",
                        "3m": "down",
                        "5m": "up",
                        "15m": "range",
                        "1H": "mixed",
                        "4H": "down",
                    },
                    "conflict": "x",
                },
                "risk": "",
                "suggestion": "",
                "reasons": [],
                "forward_view": {
                    "horizon_minutes": 240,
                    "direction": WATCH,
                    "probability": 50,
                    "summary": "s",
                    "invalidation": "i",
                },
                "data_quality": {"overall": "部分可用", "warnings": []},
            },
            {"strategy_mode": "swing"},
        )
        self.assertEqual(set(parsed["trend"]["timeframes"].keys()), {"15m", "1H", "4H"})

    def test_apply_forward_view_keeps_suggestion_separate_from_summary(self):
        parsed = apply_forward_view_to_parsed(
            {
                "direction": SHORT,
                "confidence": 62,
                "entry": "1668-1672",
                "stop_loss": "1678",
                "take_profit": "1650",
                "push_recommendation": "watch",
                "forward_view": {
                    "horizon_minutes": 240,
                    "direction": SHORT,
                    "probability": 62,
                    "summary": "未来240分钟内更可能延续震荡下行，短期反弹遇阻后重新测试1650-1655支撑区。",
                    "invalidation": "有效站上1678并15m转强",
                    "entry_plan": {
                        "entry": "1668-1672",
                        "stop_loss": "1678",
                        "take_profit": "1650",
                    },
                },
                "suggestion": (
                    "未来240分钟内更可能延续震荡下行，短期反弹遇阻后重新测试1650-1655支撑区。"
                ),
            }
        )
        self.assertNotEqual(parsed["suggestion"], parsed["forward_view"]["summary"])
        self.assertIn("操作态度", parsed["suggestion"])
        self.assertIn("价位计划", parsed["suggestion"])
        self.assertIn("1668-1672", parsed["suggestion"])

    def test_resolve_ai_suggestion_keeps_structured_ai_text(self):
        structured = (
            "操作态度：观望等待\n"
            "等待条件：15m 假突破后回落再评估\n"
            "价位计划：暂不给入场位\n"
            "失效条件：有效站上1678"
        )
        parsed = {
            "direction": WATCH,
            "push_recommendation": "watch",
            "forward_view": {
                "direction": WATCH,
                "summary": "未来240分钟内震荡下行",
                "invalidation": "有效站上1678",
            },
            "suggestion": structured,
        }
        self.assertEqual(resolve_ai_suggestion(parsed), structured)

    def test_format_ai_suggestion_builds_action_plan_for_short_bias(self):
        text = format_ai_suggestion(
            {
                "direction": SHORT,
                "push_recommendation": "watch",
                "entry": "1668-1672",
                "stop_loss": "1678",
                "take_profit": "1650",
                "forward_view": {
                    "direction": SHORT,
                    "invalidation": "有效站上1678并15m转强",
                    "entry_plan": {
                        "entry": "1668-1672",
                        "stop_loss": "1678",
                        "take_profit": "1650",
                    },
                },
            }
        )
        self.assertIn("操作态度：偏做空", text)
        self.assertIn("触发条件", text)
        self.assertIn("价位计划：入场 1668-1672", text)
        self.assertIn("失效条件：有效站上1678并15m转强", text)

    def test_vague_ai_suggestion_is_replaced_with_structured_plan(self):
        parsed = apply_forward_view_to_parsed(
            {
                "direction": SHORT,
                "confidence": 65,
                "push_recommendation": "spike",
                "entry": "1664.4",
                "stop_loss": "1672.5",
                "take_profit": "1655.0",
                "forward_view": {
                    "horizon_minutes": 240,
                    "direction": SHORT,
                    "probability": 65,
                    "summary": "未来240分钟内更可能延续震荡下行，短期反弹遇阻后重新向下测试1650-1655支撑区。",
                    "invalidation": "若价格有效突破1672.5并站稳，则空头失效",
                    "entry_plan": {
                        "entry": "1664.4",
                        "stop_loss": "1672.5",
                        "take_profit": "1655.0",
                    },
                },
                "suggestion": "当前不宜追多，等待反弹乏力后逢高做空或观望。若价格跌破1660可考虑短线追空，但需严格止损。",
            }
        )
        self.assertIn("操作态度：急变短打做空", parsed["suggestion"])
        self.assertIn("价位计划", parsed["suggestion"])

    def test_wechat_confidence_label_splits_ai_and_spike_scores(self):
        label = wechat_confidence_label(
            {"confidence": 95, "scalp_score": 95},
            {"parsed": {"confidence": 65, "forward_view": {"probability": 65}}},
            {"strategy_views": {"scalp": {"score": 95}}},
            "spike",
        )
        self.assertEqual(label, "AI65·触发95")

    def test_wechat_push_is_compact_without_raw_json(self):
        title, desp = build_wechat_push_format_preview({"strategy_mode": "swing"})
        self.assertIn("[结构单]", title)
        self.assertIn("### 结论", desp)
        self.assertIn("### 触发", desp)
        self.assertIn("### AI分析", desp)
        self.assertIn("- 建议：", desp)
        self.assertIn("操作态度：", desp)
        self.assertIn("价位计划：", desp)
        self.assertNotIn("AI 原始输出", desp)
        self.assertNotIn("一、当前配置", desp)
        self.assertNotIn("#### AI 完整分析", desp)

    def test_wechat_trade_push_keeps_long_suggestion_after_watch_ai(self):
        assistant = make_assistant()
        parsed = {
            "direction": LONG,
            "confidence": 78,
            "push_recommendation": "watch",
            "risk_level": "\u4e2d",
            "entry": "1692.50",
            "stop_loss": "1687.80",
            "take_profit": "1710.00",
            "trend": {"summary": "15m 转强", "timeframes": {"15m": "up"}, "conflict": ""},
            "risk": "波动偏高，追价需谨慎",
            "forward_view": {
                "direction": LONG,
                "probability": 78,
                "summary": "未来15m 更可能延续上行",
                "invalidation": "跌破1687.80",
                "entry_plan": {
                    "entry": "1692.50",
                    "stop_loss": "1687.80",
                    "take_profit": "1710.00",
                },
            },
        }
        final_decision = {
            "direction": LONG,
            "confidence": 78,
            "push_recommendation": "trade",
            "decision_source": "ai",
            "entry": "1692.50",
            "stop_loss": "1687.80",
            "take_profit": "1710.00",
            "risk_level": "\u4e2d",
            "strategy_label": "\u77ed\u7ebf",
            "forward_view": parsed["forward_view"],
        }
        analysis = {"valid_json": True, "parsed": parsed, "provider": "deepseek", "model": "test"}
        snapshot = {"inst_id": "ETH-USDT-SWAP", "price": 1692.5, "time": "2026-06-15 08:50:00"}
        title, desp = assistant._build_wechat_push_content(
            snapshot,
            [{"type": "volume_spike", "desc": "volume spike"}],
            final_decision,
            analysis,
            push_kind="trade",
            local_score={"raw_total_score": 70, "final_trade_score": 72, "strategy_views": {"scalp": {}}},
            trigger={"level": "L2", "ai_invoked": True, "reasons": ["multi_signal"]},
        )
        self.assertIn("- 建议：", desp)
        self.assertIn("\u53ef\u6267\u884c\u505a\u591a", desp)
        self.assertIn("1692.50", desp)
        self.assertIn("1687.80", desp)
        self.assertIn("1710.00", desp)

    def test_evaluate_ai_trigger_does_not_crash_for_local_push_review(self):
        assistant = make_assistant(short_push_score=70)
        assistant.push_enabled = True
        snapshot = {
            "inst_id": "ETH-USDT-SWAP",
            "price": 1668.0,
            "funding_rate": 0.0001,
            "trend_profiles": {"15m": {"trend": "down"}},
            "market_context": {"regime": "trend_down", "recent_price_pressure": "down"},
        }
        score = {
            "direction": SHORT,
            "final_direction": SHORT,
            "raw_direction": SHORT,
            "raw_total_score": 76,
            "final_trade_score": 75,
            "risk_level": "\u4e2d",
            "entry_plan": {"quality": "wait_confirmation"},
            "strategy_views": {"scalp": {}},
        }
        signals = [{"type": "order_book_imbalance", "desc": "book imbalance -0.72"}]
        trigger = assistant.evaluate_ai_trigger("ETH-USDT-SWAP", signals, score, snapshot)
        self.assertIn("level", trigger)
        self.assertIn("should_call_ai", trigger)

    def test_silence_brief_triggers_ai_after_wechat_silence(self):
        assistant = make_assistant(wechat_silence_brief_minutes=120)
        assistant.push_enabled = True
        inst = "ETH-USDT-SWAP"
        assistant.last_silence_brief_at[inst] = assistant._now_ts() - 121 * 60
        snapshot = {
            "inst_id": inst,
            "price": 1668.0,
            "funding_rate": 0.0001,
            "trend_profiles": {"15m": {"trend": "mixed"}, "1H": {"trend": "mixed"}},
            "market_context": {"regime": "range", "recent_price_pressure": "neutral"},
        }
        trigger = assistant.evaluate_ai_trigger(inst, [], {"strategy_views": {"scalp": {}}}, snapshot)
        self.assertTrue(trigger["should_call_ai"])
        self.assertIn("silence_brief", trigger["reasons"])

    def test_silence_brief_wechat_after_ai_epoch(self):
        assistant = make_assistant(wechat_silence_brief_minutes=60)
        assistant.push_enabled = True
        inst = "ETH-USDT-SWAP"
        assistant.last_silence_brief_at[inst] = assistant._now_ts() - 70 * 60
        cycle = assistant._silence_brief_cycle(inst)
        assistant._silence_brief_ai_epoch_done[inst] = cycle
        analysis = {
            "valid_json": True,
            "parsed": {
                "direction": WATCH,
                "confidence": 55,
                "risk_level": "\u4e2d",
                "push_recommendation": "none",
                "analysis_note": "4H\u504f\u7a7a\u30011H\u6574\u7406\uff0c\u672a\u8fbe\u7ed3\u6784\u5355\u95e8\u69db",
                "forward_view": {
                    "direction": WATCH,
                    "probability": 55,
                    "horizon_minutes": 240,
                    "summary": "震荡等待方向选择",
                    "invalidation": "突破1720或跌破1655",
                },
                "suggestion": "操作态度：观望等待\n等待条件：突破1720或跌破1655\n价位计划：暂不给入场位\n失效条件：-",
                "data_quality": {"overall": "\u5145\u8db3", "warnings": []},
            },
        }
        assistant._silence_brief_analysis_cache[inst] = (cycle, analysis)
        track = assistant._silence_brief_push_eval(
            {"inst_id": inst, "price": 1668.0, "time": "t"},
            [],
            {"direction": WATCH, "push_recommendation": "none"},
            {"strategy_views": {"scalp": {}}},
            analysis,
            {"level": "L2", "reasons": ["silence_brief"]},
        )
        self.assertEqual(track.get("status"), "would_push")
        self.assertEqual(track.get("kind"), "brief")

    def _silence_brief_ready_fixture(self, assistant, inst="BTC-USDT-SWAP"):
        assistant.push_enabled = True
        assistant.last_silence_brief_at[inst] = assistant._now_ts() - 70 * 60
        cycle = assistant._silence_brief_cycle(inst)
        assistant._silence_brief_ai_epoch_done[inst] = cycle
        analysis = {
            "valid_json": True,
            "parsed": {
                "direction": WATCH,
                "confidence": 55,
                "risk_level": "\u4e2d",
                "push_recommendation": "none",
                "analysis_note": "4H\u504f\u7a7a\u30011H\u6574\u7406\uff0c\u672a\u8fbe\u7ed3\u6784\u5355\u95e8\u69db",
                "forward_view": {
                    "direction": WATCH,
                    "probability": 55,
                    "horizon_minutes": 240,
                    "summary": "震荡等待方向选择",
                    "invalidation": "突破1720或跌破1655",
                },
                "suggestion": "操作态度：观望等待\n等待条件：突破1720或跌破1655\n价位计划：暂不给入场位\n失效条件：-",
                "data_quality": {"overall": "\u5145\u8db3", "warnings": []},
            },
        }
        assistant._silence_brief_analysis_cache[inst] = (cycle, analysis)
        snapshot = {"inst_id": inst, "price": 1668.0, "time": "t"}
        trigger = {"level": "L2", "reasons": ["silence_brief"], "ai_invoked": True}
        final_decision = {"direction": WATCH, "push_recommendation": "none", "confidence": 55}
        score = {"strategy_views": {"scalp": {}}}
        return snapshot, trigger, final_decision, score, analysis

    def test_silence_brief_included_in_push_analysis(self):
        assistant = make_assistant(wechat_silence_brief_minutes=60)
        snapshot, trigger, final_decision, score, analysis = self._silence_brief_ready_fixture(assistant)
        push_analysis = assistant._build_push_analysis(
            snapshot, [], final_decision, score, analysis, trigger
        )
        self.assertTrue(push_analysis["would_push"])
        brief_tracks = [t for t in push_analysis["tracks"] if t.get("track") == "silence_brief"]
        self.assertEqual(len(brief_tracks), 1)
        self.assertEqual(brief_tracks[0]["status"], "would_push")
        self.assertEqual(brief_tracks[0]["kind"], "brief")

    def test_silence_brief_dispatches_via_push_analysis(self):
        assistant = make_assistant(wechat_silence_brief_minutes=60)
        snapshot, trigger, final_decision, score, analysis = self._silence_brief_ready_fixture(assistant)
        push_analysis = assistant._build_push_analysis(
            snapshot, [], final_decision, score, analysis, trigger
        )
        with patch.object(assistant, "_execute_wechat_push") as mock_push:
            assistant.dispatch_wechat_push_if_needed(
                snapshot,
                [],
                final_decision,
                analysis,
                score,
                trigger,
                push_analysis=push_analysis,
            )
            mock_push.assert_called_once()
            selected = mock_push.call_args[0][6]
            self.assertEqual(selected["kind"], "brief")
            self.assertEqual(selected["decision"]["push_recommendation"], "brief")

    def test_lifecycle_brief_enabled_requires_silence_minutes(self):
        disabled = make_assistant(wechat_silence_brief_minutes=0)
        disabled.push_enabled = True
        self.assertFalse(disabled._lifecycle_brief_enabled())
        enabled = make_assistant(wechat_silence_brief_minutes=90)
        enabled.push_enabled = True
        self.assertTrue(enabled._lifecycle_brief_enabled())

    def test_lifecycle_brief_block_reason_bypasses_silence_ready(self):
        assistant = make_assistant(wechat_silence_brief_minutes=90)
        assistant.push_enabled = True
        analysis = {"valid_json": True, "parsed": {"direction": WATCH, "analysis_note": "启动观察"}}
        decision = {"inst_id": "ETH-USDT-SWAP", "lifecycle_event": "monitor_start"}
        block = assistant._wechat_push_block_reason(
            "brief",
            decision,
            {},
            {"strategy_views": {"scalp": {}}},
            [],
            {"level": "L2", "reasons": ["monitor_start"]},
            analysis,
        )
        self.assertEqual(block, "")

    def test_push_monitor_lifecycle_briefs_noop_when_disabled(self):
        with patch.object(OkxAiShortTermAssistant, "push_lifecycle_brief") as mock_push:
            push_monitor_lifecycle_briefs(
                "monitor_start",
                {"wechat_silence_brief_minutes": 0, "push_enabled": True, "ai_enabled": True},
            )
            mock_push.assert_not_called()

    def test_lifecycle_brief_does_not_block_silence_brief_epoch(self):
        assistant = make_assistant(wechat_silence_brief_minutes=60)
        assistant.push_enabled = True
        inst = "ETH-USDT-SWAP"
        past = assistant._now_ts() - 61 * 60
        assistant.last_silence_brief_at[inst] = past
        assistant._mark_wechat_push_sent(
            inst,
            "brief:eth:lifecycle:monitor_start:观望",
            "brief",
            {"lifecycle_event": "monitor_start", "direction": "观望"},
        )
        self.assertNotIn(inst, assistant._silence_brief_epoch_sent)
        assistant.last_silence_brief_at[inst] = past
        self.assertTrue(assistant._silence_brief_should_call_ai(inst))

    def test_silence_brief_marks_epoch_sent(self):
        assistant = make_assistant(wechat_silence_brief_minutes=60)
        inst = "ETH-USDT-SWAP"
        assistant.last_silence_brief_at[inst] = assistant._now_ts() - 70 * 60
        cycle = assistant._silence_brief_cycle(inst)
        assistant._mark_wechat_push_sent(
            inst,
            "brief:eth:观望",
            "brief",
            {"direction": "观望"},
        )
        self.assertEqual(assistant._silence_brief_epoch_sent.get(inst), cycle)
        self.assertGreater(assistant.last_silence_brief_at.get(inst, 0.0), 0.0)

    def test_silence_brief_schedule_independent_of_business_push(self):
        assistant = make_assistant(wechat_silence_brief_minutes=60)
        assistant.push_enabled = True
        inst = "ETH-USDT-SWAP"
        assistant.last_silence_brief_at[inst] = assistant._now_ts() - 70 * 60
        assistant.last_wechat_push_at[inst] = assistant._now_ts() - 5 * 60
        self.assertTrue(assistant._silence_brief_should_call_ai(inst))

    def test_silence_brief_repeats_after_previous_silence_brief(self):
        assistant = make_assistant(wechat_silence_brief_minutes=60)
        assistant.push_enabled = True
        inst = "ETH-USDT-SWAP"
        prior_brief = assistant._now_ts() - 130 * 60
        due_cycle = int(prior_brief + assistant._silence_brief_interval_seconds())
        assistant.last_silence_brief_at[inst] = assistant._now_ts() - 65 * 60
        assistant._silence_brief_epoch_sent[inst] = due_cycle
        self.assertTrue(assistant._silence_brief_should_call_ai(inst))

    def test_swing_spike_rejects_single_macd_without_large_move(self):
        assistant = make_assistant(strategy_mode="swing", spike_push_score=62)
        score = {
            "direction": SHORT,
            "raw_direction": SHORT,
            "final_direction": SHORT,
            "raw_total_score": 70,
            "strategy_views": {
                "scalp": {
                    "action_level": "\u6025\u901f\u5f02\u52a8",
                    "score": 90,
                    "direction": SHORT,
                    "move_pct_5m": -0.05,
                    "move_pct_10m": -0.08,
                }
            },
        }
        trigger = assistant.evaluate_ai_trigger(
            "BTC-USDT-SWAP",
            [{"type": "macd_momentum_change", "desc": "macd"}],
            score,
            {
                "funding_rate": 0.0001,
                "trend_profiles": {"15m": {"trend": "down"}},
                "market_context": {"regime": "trend_down"},
            },
        )
        self.assertNotEqual(trigger["level"], "L3")
        self.assertEqual(trigger.get("spike_filter_reason"), "strategy_spike_requires_strong_evidence")

    def test_swing_spike_wechat_requires_strategy_qualification(self):
        assistant = make_assistant(strategy_mode="swing", spike_push_score=62)
        score = {
            "final_direction": SHORT,
            "strategy_views": {
                "scalp": {
                    "action_level": "\u6025\u901f\u5f02\u52a8",
                    "score": 68,
                    "direction": SHORT,
                    "move_pct_5m": -0.30,
                    "move_pct_10m": -0.45,
                }
            },
        }
        decision = {
            "decision_source": "ai",
            "direction": SHORT,
            "confidence": 80,
            "push_recommendation": "spike",
        }
        signals = [{"type": "structure_break"}]
        reason = assistant._wechat_push_block_reason(
            "spike",
            decision,
            {},
            score,
            signals,
            {"level": "L3", "ai_invoked": True, "reasons": ["scalp_spike"]},
            {"valid_json": True},
        )
        self.assertIn("strategy_spike_score", reason)
        self.assertEqual(assistant._push_cooldown_seconds("spike"), 1800)


class PushCooldownTests(unittest.TestCase):
    def _snapshot(self, price: float = 100.0) -> dict:
        return {"inst_id": "BTC-USDT-SWAP", "price": price, "time": "2026-06-26 00:54:00"}

    def test_same_direction_short_not_bypassed_when_price_drops(self):
        assistant = make_assistant()
        assistant._set_replay_clock("2026-06-26 00:54:00")
        snapshot = self._snapshot(100.0)
        push_key = assistant._push_key(snapshot, "trade", SHORT)
        assistant._mark_wechat_push_sent(
            snapshot["inst_id"],
            push_key,
            "trade",
            {"direction": SHORT, "confidence": 78},
            snapshot,
        )
        blocked = assistant._in_push_cooldown(
            push_key,
            "trade",
            self._snapshot(97.0),
            80,
            SHORT,
        )
        self.assertTrue(blocked)

    def test_same_trend_cooldown_blocks_repeat_within_two_hours(self):
        assistant = make_assistant()
        assistant._set_replay_clock("2026-06-26 00:16:00")
        snapshot = self._snapshot(1574.81)
        push_key = assistant._push_key(snapshot, "spike", SHORT)
        assistant._mark_wechat_push_sent(
            snapshot["inst_id"],
            push_key,
            "spike",
            {"direction": SHORT, "confidence": 85},
            snapshot,
        )
        assistant._set_replay_clock("2026-06-26 00:47:35")
        blocked = assistant._in_push_cooldown(
            push_key,
            "spike",
            self._snapshot(1559.64),
            77,
            SHORT,
        )
        self.assertTrue(blocked)
        reason = assistant._push_cooldown_block_reason(
            push_key, "spike", self._snapshot(1559.64), 77, SHORT
        )
        self.assertEqual(reason, "same_trend_cooldown")

    def test_same_direction_repushed_after_trend_cooldown_elapsed(self):
        assistant = make_assistant()
        assistant._set_replay_clock("2026-06-26 00:16:00")
        snapshot = self._snapshot(100.0)
        push_key = assistant._push_key(snapshot, "trade", SHORT)
        assistant._mark_wechat_push_sent(
            snapshot["inst_id"],
            push_key,
            "trade",
            {"direction": SHORT, "confidence": 78},
            snapshot,
        )
        assistant._set_replay_clock("2026-06-26 02:30:00")
        blocked = assistant._in_push_cooldown(
            push_key,
            "trade",
            self._snapshot(97.0),
            80,
            SHORT,
        )
        self.assertFalse(blocked)

    def test_trend_leg_bounce_reset_allows_earlier_same_direction_push(self):
        assistant = make_assistant()
        assistant._set_replay_clock("2026-06-26 00:16:00")
        snapshot = self._snapshot(100.0)
        push_key = assistant._push_key(snapshot, "trade", SHORT)
        assistant._mark_wechat_push_sent(
            snapshot["inst_id"],
            push_key,
            "trade",
            {"direction": SHORT, "confidence": 80},
            snapshot,
        )
        assistant._set_replay_clock("2026-06-26 00:40:00")
        blocked = assistant._in_push_cooldown(
            push_key,
            "trade",
            self._snapshot(101.0),
            82,
            SHORT,
        )
        self.assertFalse(blocked)

    def test_repeat_short_near_local_low_blocked(self):
        assistant = make_assistant()
        assistant._set_replay_clock("2026-06-26 00:16:00")
        snapshot = {
            **self._snapshot(100.0),
            "trend_profiles": {
                "1H": {"recent_high": 110.0, "recent_low": 99.8},
                "4H": {"recent_high": 110.0, "recent_low": 99.8},
            },
        }
        push_key = assistant._push_key(snapshot, "spike", SHORT)
        assistant._mark_wechat_push_sent(
            snapshot["inst_id"],
            push_key,
            "spike",
            {"direction": SHORT, "confidence": 85},
            snapshot,
        )
        reason = assistant._repeat_direction_entry_block(
            {**snapshot, "price": 99.9},
            snapshot["inst_id"],
            SHORT,
            "spike",
        )
        self.assertEqual(reason, "repeat_short_near_local_low")

    def test_direction_change_not_blocked_by_same_direction_cooldown(self):
        assistant = make_assistant()
        assistant._set_replay_clock("2026-06-26 00:54:00")
        snapshot = self._snapshot(100.0)
        short_key = assistant._push_key(snapshot, "trade", SHORT)
        assistant._mark_wechat_push_sent(
            snapshot["inst_id"],
            short_key,
            "trade",
            {"direction": SHORT, "confidence": 80},
            snapshot,
        )
        long_key = assistant._push_key(snapshot, "trade", LONG)
        blocked = assistant._in_push_cooldown(long_key, "trade", snapshot, 82, LONG)
        self.assertFalse(blocked)

    def test_trade_and_spike_share_direction_cooldown(self):
        assistant = make_assistant()
        assistant._set_replay_clock("2026-06-26 00:54:00")
        snapshot = self._snapshot(100.0)
        trade_key = assistant._push_key(snapshot, "trade", SHORT)
        assistant._mark_wechat_push_sent(
            snapshot["inst_id"],
            trade_key,
            "trade",
            {"direction": SHORT, "confidence": 80},
            snapshot,
        )
        spike_key = assistant._push_key(snapshot, "spike", SHORT)
        blocked = assistant._in_push_cooldown(spike_key, "spike", snapshot, 81, SHORT)
        self.assertTrue(blocked)

    def test_direction_change_bypasses_inst_wechat_cooldown(self):
        assistant = make_assistant()
        assistant._set_replay_clock("2026-06-26 00:54:00")
        inst = "BTC-USDT-SWAP"
        assistant.last_wechat_push_at[inst] = assistant._now_ts()
        assistant.last_trade_push_at[inst] = (SHORT, assistant._now_ts())
        self.assertFalse(assistant._in_inst_wechat_cooldown(inst, LONG))
        self.assertTrue(assistant._in_inst_wechat_cooldown(inst, SHORT))


class SentimentSignalTests(unittest.TestCase):
    def test_sentiment_signals_single_high_strength_qualifies(self):
        assistant = make_assistant()
        qualified = assistant._l2_qualifies_ai_call(
            ["sentiment_signals"],
            {"oi_change"},
            {
                "sentiment_meta": {"direction": LONG, "strength": 3},
                "structure_forecast": {"active": False},
                "strategy_views": {"scalp": {}},
            },
            {"market_context": {"regime": "range"}},
        )
        self.assertTrue(qualified)


class AiPayloadTests(unittest.TestCase):
    def _sample_snapshot(self):
        return {
            "inst_id": "BTC-USDT-SWAP",
            "time": "2026-06-18 12:00:00",
            "price": 68000.0,
            "best_bid": 67999.0,
            "best_ask": 68001.0,
            "candles": {"5m": [], "15m": []},
            "trend_profiles": {
                "5m": {"breakout": "up", "trend": "up", "rsi": {"14": 55}, "macd": {"hist": 1.0, "hist_slope": 0.5}},
                "15m": {"breakout": "up", "trend": "up", "rsi": {"14": 58}, "macd": {"hist": 1.2, "hist_slope": 0.4}},
            },
            "volume": {"multiplier": 2.5, "source_bar": "1m"},
            "open_interest": {"value": 1000},
            "oi_change_pct_15m": 1.2,
            "oi_warmup_ready": True,
            "funding_rate": 0.0009,
            "funding_change": 0.0002,
            "funding_warmup_ready": True,
            "long_short_ratio": {"long_ratio": 0.55, "short_ratio": 0.45, "available": True},
            "order_book": {"available": True, "imbalance": 0.4},
            "volatility": {"level": "中"},
            "dynamic_thresholds": {"volume_multiplier_p85": 2.0, "book_imbalance_p85": 0.35},
            "market_context": {
                "regime": "trend_up",
                "recent_price_pressure": "up",
                "oi_price_state": "price_up_oi_up",
                "order_book_bias": "bid",
                "volume_threshold_used": 2.0,
            },
            "snapshot_quality": {"overall": "充足"},
            "data_sources": {},
        }

    def test_ai_payload_excludes_local_conclusions(self):
        assistant = make_assistant()
        snapshot = self._sample_snapshot()
        signals = [
            {"type": "structure_break", "desc": "breakout up", "direction_hint": LONG},
            {"type": "volume_spike", "desc": "volume 2.5x"},
        ]
        score = {
            "direction": LONG,
            "raw_direction": LONG,
            "raw_total_score": 78,
            "final_trade_score": 72,
            "market_regime": "trend_up",
            "strategy_views": {"scalp": {"direction": LONG, "score": 70, "action_level": "可短打"}},
            "structure_forecast": {"active": True, "direction": LONG, "probability": 65, "scenario": "breakout"},
        }
        trigger = {"level": "L2", "reasons": ["multi_signal"]}
        payload = assistant._ai_payload(snapshot, signals, score, trigger)
        payload_text = json.dumps(payload, ensure_ascii=False)
        self.assertIn("market_data", payload)
        self.assertIn("trigger_context", payload)
        self.assertIn("analysis_config", payload)
        self.assertNotIn("local_screening", payload)
        self.assertNotIn("local_reference", payload)
        for forbidden in (
            "local_bias",
            "raw_total_score",
            "final_trade_score",
            "structure_forecast",
            "scalp_view",
        ):
            self.assertNotIn(forbidden, payload_text)
        trigger_ctx = payload["trigger_context"]
        self.assertIn("signal_evidence", trigger_ctx)
        self.assertGreater(len(trigger_ctx["signal_evidence"]), 0)
        self.assertNotIn("observable_context", trigger_ctx)
        self.assertNotIn("valid_by_rule", payload_text)
        self.assertIn("market_context", payload["market_data"])
        self.assertIn("bar_profiles", payload["market_data"])
        self.assertEqual(payload["payload_meta"].get("candle_order"), "newest_first")
        self.assertEqual(payload["payload_meta"].get("analysis_mode"), "independent")

    def test_ai_trigger_context_uses_breakout_not_direction_hint(self):
        assistant = make_assistant()
        snapshot = self._sample_snapshot()
        signals = [{"type": "structure_break", "desc": "breakout", "direction_hint": LONG}]
        ctx = assistant._ai_trigger_context(snapshot, signals, {"level": "L2", "reasons": ["trade_signal"]})
        self.assertEqual(ctx["signals"][0].get("breakout"), "up")
        self.assertNotIn("structure_hint", ctx["signals"][0])
        self.assertNotIn("local_bias", ctx)
        self.assertNotIn("raw_total_score", ctx)


class AiForwardStatsTests(unittest.TestCase):
    def test_accuracy_activity_stats_counts_calls_and_tokens(self):
        from web_control_panel import accuracy_activity_stats  # noqa: WPS433

        items = [
            {
                "local_trigger": {"ai_invoked": False},
                "analysis": None,
            },
            {
                "local_trigger": {"ai_invoked": True},
                "analysis": {
                    "usage": {
                        "prompt_tokens": 100,
                        "completion_tokens": 20,
                        "total_tokens": 120,
                    }
                },
            },
            {
                "local_trigger": {"ai_invoked": True},
                "analysis": {
                    "usage": {
                        "prompt_tokens": 80,
                        "completion_tokens": 10,
                    }
                },
            },
        ]
        stats = accuracy_activity_stats(items)
        self.assertEqual(stats["analysis_total"], 3)
        self.assertEqual(stats["ai_call_total"], 2)
        self.assertEqual(stats["ai_token_total"], 210)
        self.assertEqual(stats["ai_prompt_token_total"], 180)
        self.assertEqual(stats["ai_completion_token_total"], 30)

    def test_ai_off_chart_uses_local_score_without_mutating_decision(self):
        from web_control_panel import effective_fields_from_log_item  # noqa: WPS433

        score = {
            "direction": LONG,
            "final_direction": LONG,
            "raw_direction": LONG,
            "direction_score": 73,
            "final_trade_score": 69,
            "entry": "100 - 101",
            "stop_loss": "98",
            "take_profit": "104 / 106",
            "trade_action_level": "等待确认",
        }
        final_decision = {
            "decision_source": "local_screening",
            "direction": WATCH,
            "confidence": 73,
            "entry": "-",
            "stop_loss": "-",
            "take_profit": "-",
        }
        item = {
            "config_snapshot": {"ai_enabled": False},
            "score": score,
            "final_decision": final_decision,
        }
        effective = effective_fields_from_log_item(item)
        self.assertEqual(effective["final_direction"], LONG)
        self.assertEqual(effective["entry"], "100 - 101")
        self.assertEqual(effective["analysis_mode"], "local")
        self.assertEqual(final_decision["direction"], WATCH)
        self.assertEqual(score["final_direction"], LONG)

    def test_ai_on_without_call_keeps_external_screening_watch(self):
        from web_control_panel import effective_fields_from_log_item  # noqa: WPS433

        item = {
            "config_snapshot": {"ai_enabled": True},
            "score": {"direction": LONG, "final_direction": LONG, "entry": "100"},
            "final_decision": {
                "decision_source": "local_screening",
                "direction": WATCH,
                "entry": "-",
            },
        }
        effective = effective_fields_from_log_item(item)
        self.assertEqual(effective["final_direction"], WATCH)
        self.assertEqual(effective["entry"], "-")

    def test_ai_off_paper_uses_local_direction_even_if_ai_only_enabled(self):
        from web_control_panel import paper_direction_from_log_item  # noqa: WPS433

        item = {
            "config_snapshot": {"ai_enabled": False, "paper_follow_ai_only": True},
            "score": {"direction": SHORT, "final_direction": SHORT},
            "final_decision": {"decision_source": "local_screening", "direction": WATCH},
        }
        self.assertEqual(
            paper_direction_from_log_item(item, paper_follow_ai_only=True),
            SHORT,
        )

    def test_ai_forward_skips_local_fallback_with_valid_json(self):
        from web_control_panel import ai_forward_from_log_item  # noqa: WPS433

        item = {
            "final_decision": {
                "decision_source": "local_fallback",
                "direction": LONG,
                "forward_view": {"direction": LONG, "horizon_minutes": 15, "probability": 70},
            },
            "analysis": {"valid_json": True, "parsed": {"forward_view": {"direction": LONG}}},
        }
        self.assertIsNone(ai_forward_from_log_item(item))

    def test_ai_forward_accepts_ai_decision_source(self):
        from web_control_panel import ai_forward_from_log_item  # noqa: WPS433

        item = {
            "final_decision": {
                "decision_source": "ai",
                "direction": LONG,
                "forward_view": {
                    "direction": LONG,
                    "horizon_minutes": 15,
                    "probability": 72,
                    "summary": "up",
                    "invalidation": "break",
                },
            },
            "analysis": {"valid_json": True},
        }
        forward = ai_forward_from_log_item(item)
        self.assertIsNotNone(forward)
        self.assertEqual(forward["direction"], LONG)

    def test_prediction_direction_prefers_ai_forward(self):
        from web_control_panel import prediction_direction_from_log_item  # noqa: WPS433

        item = {
            "config_snapshot": {"ai_enabled": True},
            "score": {"raw_direction": WATCH, "final_direction": WATCH},
            "final_decision": {
                "decision_source": "ai",
                "direction": WATCH,
                "forward_view": {"direction": LONG, "horizon_minutes": 15},
            },
        }
        direction, source = prediction_direction_from_log_item(item)
        self.assertEqual(direction, LONG)
        self.assertEqual(source, "ai_forward")

    def test_prediction_direction_uses_raw_when_local(self):
        from web_control_panel import prediction_direction_from_log_item  # noqa: WPS433

        item = {
            "config_snapshot": {"ai_enabled": False},
            "score": {"raw_direction": LONG, "final_direction": WATCH},
            "final_decision": {"decision_source": "local_screening", "direction": WATCH},
        }
        direction, source = prediction_direction_from_log_item(item)
        self.assertEqual(direction, LONG)
        self.assertEqual(source, "raw_direction")

    def _seed_decision_calibration_bucket(
        self,
        assistant: OkxAiShortTermAssistant,
        *,
        inst_id: str = "BTC-USDT-SWAP",
        direction: str = LONG,
        push_kind: str = "trade",
        regime: str = "high_volatility",
        total: int = 16,
        hits: int = 4,
    ) -> str:
        key = assistant._decision_calibration_key(inst_id, "ai", push_kind, direction, regime)
        assistant.calibration_state.setdefault("buckets", {})[key] = {
            "total": total,
            "hits": hits,
        }
        return key

    def test_calibration_keeps_trend_aligned_trade_when_confidence_meets_raised_threshold(self):
        assistant = make_assistant(strategy_mode="swing", push_score=65, calibration_min_samples=8)
        self._seed_decision_calibration_bucket(assistant, total=16, hits=4)
        final_decision = {
            "direction": LONG,
            "confidence": 68,
            "push_recommendation": "trade",
            "decision_source": "ai",
            "local_bias": LONG,
            "forward_view": {"direction": LONG, "probability": 68},
        }
        score = {"strategy_views": {"scalp": {}}, "final_direction": LONG}
        snapshot = {
            "inst_id": "BTC-USDT-SWAP",
            "market_context": {
                "regime": "high_volatility",
                "recent_price_pressure": "up",
                "bias": "long",
            },
        }
        audited = assistant._apply_ai_calibration_audit(final_decision, snapshot, score)
        self.assertEqual(audited.get("push_recommendation"), "trade")
        self.assertGreaterEqual(int(audited.get("calibration_effective_threshold", 0) or 0), 65)

    def test_calibration_downgrades_trade_when_confidence_below_raised_threshold(self):
        assistant = make_assistant(strategy_mode="swing", push_score=65, calibration_min_samples=8)
        self._seed_decision_calibration_bucket(assistant, total=16, hits=2)
        final_decision = {
            "direction": LONG,
            "confidence": 66,
            "push_recommendation": "trade",
            "decision_source": "ai",
            "forward_view": {"direction": LONG, "probability": 66},
        }
        score = {"strategy_views": {"scalp": {}}}
        snapshot = {
            "inst_id": "BTC-USDT-SWAP",
            "market_context": {
                "regime": "range",
                "recent_price_pressure": "neutral",
                "bias": "neutral",
            },
        }
        audited = assistant._apply_ai_calibration_audit(final_decision, snapshot, score)
        self.assertEqual(audited.get("push_recommendation"), "none")
        self.assertIn("calibration_shadow_track", audited)

    def test_replay_reset_clears_decision_calibration_buckets(self):
        assistant = make_assistant(strategy_mode="swing")
        key = self._seed_decision_calibration_bucket(assistant)
        assistant.pending_decision_reviews.append({"inst_id": "BTC-USDT-SWAP", "kind": "decision"})
        assistant._reset_replay_calibration_state(["BTC-USDT-SWAP"])
        self.assertNotIn(key, assistant.calibration_state.get("buckets", {}))
        self.assertEqual(assistant.pending_decision_reviews, [])

    def test_confidence_hug_skipped_for_trend_aligned_high_volatility(self):
        assistant = make_assistant(strategy_mode="swing", push_score=65, ai_conflict_guard=True)
        final_decision = {
            "direction": LONG,
            "confidence": 66,
            "push_recommendation": "trade",
            "decision_source": "ai",
            "local_bias": LONG,
            "forward_view": {"direction": LONG, "probability": 66},
        }
        score = {
            "structure_forecast": {"active": False},
            "strategy_views": {"scalp": {}},
            "final_direction": LONG,
        }
        audited = assistant._apply_decision_post_audit(
            final_decision,
            score,
            [{"type": "volume_spike"}],
            {"level": "L2", "reasons": ["multi_signal"]},
            {
                "inst_id": "BTC-USDT-SWAP",
                "market_context": {
                    "regime": "mixed",
                    "recent_price_pressure": "up",
                    "bias": "long",
                },
            },
        )
        self.assertEqual(audited.get("push_recommendation"), "trade")

    def test_pressure_up_allows_short_trade_during_pullback(self):
        assistant = make_assistant(strategy_mode="swing", short_push_score=65, ai_conflict_guard=True)
        final_decision = {
            "direction": SHORT,
            "confidence": 70,
            "push_recommendation": "trade",
            "decision_source": "ai",
            "forward_view": {"direction": SHORT, "probability": 70},
        }
        score = {
            "structure_forecast": {"active": False},
            "strategy_views": {"scalp": {}},
            "final_direction": SHORT,
        }
        audited = assistant._apply_decision_post_audit(
            final_decision,
            score,
            [{"type": "structure_break"}],
            {"level": "L2", "reasons": ["multi_signal"]},
            {
                "inst_id": "BTC-USDT-SWAP",
                "market_context": {
                    "regime": "trend_down",
                    "recent_price_pressure": "up",
                    "bias": "short",
                    "trade_down": 3,
                    "trade_up": 1,
                },
            },
        )
        self.assertEqual(audited.get("push_recommendation"), "trade")

    def test_late_long_entry_guard_blocks_chase_near_structural_high(self):
        assistant = make_assistant(strategy_mode="swing", late_long_entry_guard=True)
        final_decision = {
            "direction": LONG,
            "confidence": 68,
            "push_recommendation": "trade",
            "decision_source": "ai",
            "forward_view": {"direction": LONG, "probability": 68},
        }
        score = {
            "structure_forecast": {"active": False},
            "strategy_views": {"scalp": {}},
        }
        snapshot = {
            "inst_id": "ETH-USDT-SWAP",
            "price": 1840.0,
            "volatility": {"atr_pct_15m": 0.55},
            "trend_profiles": {
                "15m": {"recent_high": 1846.0},
                "1H": {"recent_high": 1848.16, "recent_low": 1712.9, "rsi": {"14": 72.0}},
                "4H": {"recent_high": 1848.16, "recent_low": 1712.9},
            },
            "candles": {"1m": []},
            "market_context": {},
        }
        audited = assistant._apply_decision_post_audit(
            final_decision,
            score,
            [{"type": "volume_spike"}],
            {"level": "L2", "reasons": ["periodic_review"]},
            {
                **snapshot,
                "market_context": {
                    "regime": "high_volatility",
                    "recent_price_pressure": "neutral",
                    "bias": "long",
                    "trend_phase": "transition",
                    "price_change_strategy": 0.73,
                    "recent_move_pct": {"20m": 0.12},
                },
            },
        )
        self.assertEqual(audited.get("push_recommendation"), "none")
        self.assertIn("late_long_entry_near_structural_high", audited.get("post_audit", {}).get("reasons", []))

    def test_late_long_entry_guard_allows_pullback_long(self):
        assistant = make_assistant(strategy_mode="swing", late_long_entry_guard=True)
        final_decision = {
            "direction": LONG,
            "confidence": 68,
            "push_recommendation": "trade",
            "decision_source": "ai",
            "local_bias": LONG,
            "forward_view": {"direction": LONG, "probability": 68},
        }
        score = {
            "structure_forecast": {"active": False},
            "strategy_views": {"scalp": {}},
            "final_direction": LONG,
        }
        audited = assistant._apply_decision_post_audit(
            final_decision,
            score,
            [{"type": "volume_spike"}],
            {"level": "L2", "reasons": ["multi_signal"]},
            {
                "inst_id": "ETH-USDT-SWAP",
                "price": 1840.0,
                "volatility": {"atr_pct_15m": 0.55},
                "trend_profiles": {
                    "1H": {"recent_high": 1848.16, "rsi": {"14": 58.0}},
                    "4H": {"recent_high": 1848.16},
                },
                "candles": {"1m": []},
                "market_context": {
                    "regime": "high_volatility",
                    "recent_price_pressure": "down",
                    "bias": "long",
                    "trend_phase": "pullback_in_uptrend",
                    "price_change_strategy": 2.4,
                    "recent_move_pct": {"20m": -0.45},
                },
            },
        )
        self.assertEqual(audited.get("push_recommendation"), "trade")

    def test_late_long_entry_guard_allows_fresh_breakout(self):
        assistant = make_assistant(strategy_mode="swing", late_long_entry_guard=True)
        reason = assistant._late_long_entry_block_reason(
            {
                "price": 1763.0,
                "volatility": {"atr_pct_15m": 0.45},
                "trend_profiles": {"1H": {"recent_high": 1765.0, "rsi": {"14": 62.0}}},
                "candles": {"1m": []},
            },
            {
                "recent_price_pressure": "up",
                "trend_phase": "breakout_attempt_up",
                "price_change_strategy": 1.2,
                "recent_move_pct": {"20m": 0.8},
            },
            {},
            {"direction": LONG},
            "spike",
        )
        self.assertEqual(reason, "")

    def test_late_long_entry_guard_exempts_early_rally(self):
        assistant = make_assistant(strategy_mode="swing", late_long_entry_guard=True)
        reason = assistant._late_long_entry_block_reason(
            {
                "price": 1749.13,
                "volatility": {"atr_pct_15m": 0.45},
                "trend_profiles": {
                    "1H": {"recent_high": 1752.0, "recent_low": 1712.9, "rsi": {"14": 66.0}},
                    "4H": {"recent_high": 1752.0, "recent_low": 1712.9},
                },
                "candles": {"1m": []},
            },
            {
                "recent_price_pressure": "up",
                "trend_phase": "trend_accelerating_up",
                "price_change_strategy": 1.8,
                "recent_move_pct": {"20m": 0.9},
            },
            {},
            {"direction": LONG},
            "trade",
        )
        self.assertEqual(reason, "")

    def test_pullback_blocks_long_trade_during_downtrend(self):
        assistant = make_assistant(strategy_mode="swing", pullback_long_entry_guard=True)
        final_decision = {
            "direction": LONG,
            "confidence": 68,
            "push_recommendation": "trade",
            "decision_source": "ai",
            "forward_view": {"direction": LONG, "probability": 68},
        }
        score = {
            "structure_forecast": {"active": False},
            "strategy_views": {"scalp": {}},
        }
        audited = assistant._apply_decision_post_audit(
            final_decision,
            score,
            [{"type": "volume_spike"}],
            {"level": "L2", "reasons": ["multi_signal"]},
            {
                "inst_id": "ETH-USDT-SWAP",
                "price": 1787.96,
                "volatility": {"atr_pct_15m": 0.45},
                "trend_profiles": {
                    "15m": {"trend": "down"},
                    "1H": {"trend": "down", "recent_high": 1848.16, "recent_low": 1712.9},
                    "4H": {"recent_high": 1848.16, "recent_low": 1712.9},
                },
                "candles": {"1m": []},
                "market_context": {
                    "regime": "high_volatility",
                    "recent_price_pressure": "down",
                    "bias": "neutral",
                    "trend_phase": "transition",
                    "trade_down": 3,
                    "trade_up": 1,
                    "price_change_strategy": -0.8,
                    "recent_move_pct": {"20m": -0.6},
                },
            },
        )
        self.assertEqual(audited.get("push_recommendation"), "none")
        self.assertIn("pullback_downtrend_blocks_long_trade", audited.get("post_audit", {}).get("reasons", []))

    def test_trade_push_confidence_uses_forward_probability_cap(self):
        assistant = make_assistant(strategy_mode="swing", push_score=65)
        final_decision = {
            "decision_source": "ai",
            "direction": LONG,
            "confidence": 79,
            "forward_view": {"direction": LONG, "probability": 62},
            "push_recommendation": "trade",
        }
        self.assertEqual(assistant._trade_push_confidence(final_decision, {}), 62)
        self.assertEqual(assistant.push_gate(final_decision, [{"type": "volume_spike"}], {}), "")

    def test_post_peak_blocks_long_after_session_rally(self):
        assistant = make_assistant(strategy_mode="swing", post_peak_long_entry_guard=True)
        eval_result = assistant._post_peak_long_entry_eval(
            {
                "price": 1830.0,
                "trend_profiles": {
                    "1H": {"recent_high": 1848.16, "recent_low": 1712.9},
                    "4H": {"recent_high": 1848.16, "recent_low": 1712.9},
                },
            },
            {
                "recent_price_pressure": "neutral",
                "trend_phase": "transition",
            },
            {"direction": LONG, "push_recommendation": "trade"},
            "trade",
        )
        self.assertTrue(eval_result.get("blocked"))
        self.assertEqual(eval_result.get("reason"), "post_peak_blocks_long_trade")

    def test_deep_pullback_blocks_long_using_session_high(self):
        assistant = make_assistant(strategy_mode="swing", pullback_long_entry_guard=True, push_score=65)
        final_decision = {
            "direction": LONG,
            "confidence": 68,
            "push_recommendation": "trade",
            "decision_source": "ai",
            "forward_view": {"direction": LONG, "probability": 68},
        }
        score = {
            "structure_forecast": {"active": False},
            "strategy_views": {"scalp": {}},
        }
        audited = assistant._apply_decision_post_audit(
            final_decision,
            score,
            [{"type": "volume_spike"}],
            {"level": "L2", "reasons": ["multi_signal"]},
            {
                "inst_id": "ETH-USDT-SWAP",
                "price": 1758.0,
                "volatility": {"atr_pct_15m": 0.45},
                "trend_profiles": {
                    "15m": {"trend": "down"},
                    "1H": {"trend": "down", "recent_high": 1848.16, "recent_low": 1750.0},
                    "4H": {"recent_high": 1848.16, "recent_low": 1712.9},
                },
                "candles": {"1m": []},
                "market_context": {
                    "regime": "mixed",
                    "recent_price_pressure": "down",
                    "bias": "neutral",
                    "trend_phase": "transition",
                    "trade_down": 2,
                    "trade_up": 1,
                    "price_change_strategy": -0.5,
                    "recent_move_pct": {"20m": -0.4},
                },
            },
        )
        self.assertEqual(audited.get("push_recommendation"), "none")
        self.assertIn("pullback_downtrend_blocks_long_trade", audited.get("post_audit", {}).get("reasons", []))
        self.assertGreaterEqual(float(audited.get("drawdown_from_high_pct", 0) or 0), 2.5)

    def test_l3_promotes_spike_from_ai_watch(self):
        assistant = make_assistant(strategy_mode="scalp", spike_push_score=70)
        final_decision = {
            "direction": LONG,
            "confidence": 72,
            "push_recommendation": "watch",
            "decision_source": "ai",
        }
        score = {
            "structure_forecast": {"active": False},
            "strategy_views": {
                "scalp": {
                    "action_level": "急速异动",
                    "direction": LONG,
                    "score": 78,
                }
            },
        }
        audited = assistant._apply_decision_post_audit(
            final_decision,
            score,
            [{"type": "volume_spike"}],
            {"level": "L3", "reasons": ["scalp_spike"]},
            {
                "inst_id": "ETH-USDT-SWAP",
                "price": 1763.0,
                "volatility": {"atr_pct_15m": 0.45},
                "trend_profiles": {"1H": {"recent_high": 1765.0, "recent_low": 1712.9}},
                "candles": {"1m": []},
                "market_context": {
                    "regime": "high_volatility",
                    "recent_price_pressure": "up",
                    "bias": "long",
                    "trend_phase": "breakout_attempt_up",
                },
            },
        )
        self.assertEqual(audited.get("push_recommendation"), "spike")
        self.assertIn("l3_scalp_spike_from_ai_watch", audited.get("post_audit", {}).get("reasons", []))

    def test_post_peak_short_favor_promotes_local_short(self):
        assistant = make_assistant(strategy_mode="swing", post_peak_short_entry_favor=True, push_score=65)
        score = {
            "final_direction": SHORT,
            "direction": SHORT,
            "final_trade_score": 40,
            "raw_total_score": 40,
            "structure_forecast": {"active": False},
            "strategy_views": {"scalp": {}},
        }
        audited = assistant._apply_decision_post_audit(
            {
                "direction": WATCH,
                "confidence": 40,
                "push_recommendation": "none",
                "decision_source": "local_screening",
            },
            score,
            [],
            {"level": "L0", "reasons": []},
            {
                "inst_id": "ETH-USDT-SWAP",
                "price": 1790.0,
                "volatility": {"atr_pct_15m": 0.45},
                "trend_profiles": {
                    "15m": {"trend": "down"},
                    "1H": {"trend": "down", "recent_high": 1848.16, "recent_low": 1712.9},
                    "4H": {"recent_high": 1848.16, "recent_low": 1712.9},
                },
                "candles": {"1m": []},
                "market_context": {
                    "regime": "mixed",
                    "recent_price_pressure": "down",
                    "trade_down": 3,
                    "trade_up": 1,
                },
            },
        )
        self.assertEqual(audited.get("direction"), SHORT)
        self.assertEqual(audited.get("push_recommendation"), "trade")
        self.assertIn("post_peak_local_short_promote", audited.get("post_audit", {}).get("reasons", []))
        self.assertTrue((audited.get("post_peak_short_favor") or {}).get("active"))

    def test_ai_trade_persist_exempts_no_signals(self):
        assistant = make_assistant(strategy_mode="swing", push_score=65)
        final_decision = {
            "direction": SHORT,
            "confidence": 66,
            "push_recommendation": "trade",
            "decision_source": "ai_persisted",
            "ai_trade_persisted": True,
            "forward_view": {"direction": SHORT, "probability": 66},
        }
        self.assertTrue(assistant._confirmed_push_exempt_no_signals(final_decision))
        track = assistant._confirmed_push_eval(
            {"inst_id": "ETH-USDT-SWAP", "price": 1765.0},
            [],
            final_decision,
            {},
        )
        self.assertNotEqual(track.get("reason"), "no_signals")
        self.assertEqual(assistant.push_gate(final_decision, [], {}), "trade")

    def test_ai_trade_decision_persists_across_local_frames(self):
        assistant = make_assistant(strategy_mode="swing", push_score=65, ai_trade_push_persist_seconds=1800)
        assistant.last_ai_trade_decision["ETH-USDT-SWAP"] = {
            "direction": SHORT,
            "push_recommendation": "trade",
            "confidence": 66,
            "forward_view": {"direction": SHORT, "probability": 66},
            "ts": assistant._now_ts(),
        }
        score = {
            "final_direction": WATCH,
            "direction": WATCH,
            "raw_total_score": 30,
            "structure_forecast": {"active": False},
            "strategy_views": {"scalp": {}},
        }
        merged = assistant.merge_final_decision(
            None,
            score,
            [],
            {"level": "L0", "reasons": []},
            {"inst_id": "ETH-USDT-SWAP", "price": 1765.0},
        )
        self.assertEqual(merged.get("direction"), SHORT)
        self.assertEqual(merged.get("push_recommendation"), "trade")
        self.assertEqual(merged.get("decision_source"), "ai_persisted")


class WechatShadowPushTests(unittest.TestCase):
    def test_shadow_skipped_without_customer_key(self):
        with patch("okx_signal_monitor.urllib.request.urlopen") as mock_open:
            push_wechat_shadow_copy("t", "d", customer_send_key="")
            mock_open.assert_not_called()

    def test_shadow_posts_when_customer_key_present(self):
        with patch("okx_signal_monitor.urllib.request.urlopen") as mock_open:
            mock_open.return_value.__enter__.return_value.read.return_value = b"ok"
            push_wechat_shadow_copy("t", "d", customer_send_key="SCT-customer-key")
            mock_open.assert_called_once()


if __name__ == "__main__":
    unittest.main()
