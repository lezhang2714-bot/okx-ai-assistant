import json
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
            "",
        )

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


if __name__ == "__main__":
    unittest.main()
