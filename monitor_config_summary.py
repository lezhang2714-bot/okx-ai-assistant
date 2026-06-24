"""Shared monitor config defaults, log snapshots, and restart detection."""
from __future__ import annotations

from typing import Any, Dict, List, Set, Tuple

LOCAL_TRADE_PUSH_MARGIN = 5
SWING_SPIKE_SCORE_MARGIN = 10
LONG_SPIKE_SCORE_MARGIN = 15
SWING_SPIKE_CONFIRM_ROUNDS = 2
SWING_SPIKE_COOLDOWN_SECONDS = 1800
SWING_AI_CALL_MIN_INTERVAL_SECONDS = 300
LONG_AI_CALL_MIN_INTERVAL_SECONDS = 600
SWING_AI_SUSTAINED_REVIEW_INTERVAL_SECONDS = 300

# 轮询间隔与策略周期绑定：更慢周期不必高频 REST 轮询。
STRATEGY_DEFAULT_INTERVAL_SECONDS: Dict[str, int] = {
    "scalp": 5,
    "short": 5,
    "swing": 60,
    "long": 180,
}


def recommended_interval_for_strategy(strategy_mode: str) -> int:
    mode = str(strategy_mode or "short").strip().lower()
    return max(1, int(STRATEGY_DEFAULT_INTERVAL_SECONDS.get(mode, STRATEGY_DEFAULT_INTERVAL_SECONDS["short"])))

# 压测验证窗口与策略周期绑定：与主周期/前瞻 horizon 对齐。
STRATEGY_DEFAULT_ACCURACY_HORIZON_SECONDS: Dict[str, int] = {
    "scalp": 5,
    "short": 900,
    "swing": 3600,
    "long": 14400,
}

STRATEGY_ACCURACY_HORIZON_HINTS: Dict[str, str] = {
    "scalp": "轮询级",
    "short": "短线推荐",
    "swing": "中线推荐",
    "long": "长线推荐",
}


def recommended_accuracy_horizon_for_strategy(strategy_mode: str) -> int:
    mode = str(strategy_mode or "swing").strip().lower()
    return max(
        5,
        int(
            STRATEGY_DEFAULT_ACCURACY_HORIZON_SECONDS.get(
                mode,
                STRATEGY_DEFAULT_ACCURACY_HORIZON_SECONDS["swing"],
            )
        ),
    )

# Single source for monitor/Web CLI behavior defaults (must match SignalConfig + default_config).
MONITOR_BEHAVIOR_DEFAULTS: Dict[str, Any] = {
    "interval": 5,
    "push_score": 70,
    "short_push_score": 68,
    "watch_push_score": 65,
    "spike_push_score": 62,
    "forecast_push_score": 58,
    "forecast_horizon_minutes": 240,
    "push_cooldown_seconds": 1800,
    "spike_push_cooldown_seconds": 1200,
    "watch_push_cooldown_seconds": 1800,
    "reverse_trade_cooldown_seconds": 600,
    "forecast_push_cooldown_seconds": 3600,
    "strategy_mode": "swing",
    "risk_preference": "aggressive",
    "signal_trade_enabled": True,
    "signal_watch_enabled": False,
    "signal_spike_enabled": True,
    "signal_forecast_enabled": True,
    "ai_conflict_guard": True,
    "l3_local_spike_push": False,
    "l2_require_volume_or_structure": True,
    "calibration_enabled": True,
    "paper_follow_ai_only": True,
    "paper_fee_bps": 5.0,
    "forward_require_forecast_alignment": True,
    "replay_ai_cache_enabled": True,
    "wechat_require_ai_review": True,
    "wechat_silence_brief_minutes": 0,
    "volume_multiplier": 2.0,
    "oi_change_pct_15m": 5.0,
    "funding_abs_threshold": 0.0008,
    "funding_change_threshold": 0.0003,
    "long_short_extreme": 0.75,
    "retry_times": 3,
    "retry_backoff": 1.5,
    "calibration_min_samples": 8,
    "calibration_blend_weight": 0.65,
    "calibration_disable_below_hit_rate": 0.38,
}

MONITOR_RESTART_KEYS: Set[str] = frozenset(
    {
        "inst_ids",
        "interval",
        "strategy_mode",
        "risk_preference",
        "ai_enabled",
        "ai_periodic_interval_minutes",
        "dry_run_ai",
        "push_enabled",
        "push_score",
        "short_push_score",
        "watch_push_score",
        "spike_push_score",
        "forecast_push_score",
        "forecast_horizon_minutes",
        "push_cooldown_seconds",
        "spike_push_cooldown_seconds",
        "watch_push_cooldown_seconds",
        "reverse_trade_cooldown_seconds",
        "forecast_push_cooldown_seconds",
        "signal_trade_enabled",
        "signal_watch_enabled",
        "signal_spike_enabled",
        "signal_forecast_enabled",
        "ai_conflict_guard",
        "l3_local_spike_push",
        "l2_require_volume_or_structure",
        "calibration_enabled",
        "calibration_min_samples",
        "calibration_blend_weight",
        "calibration_disable_below_hit_rate",
        "paper_follow_ai_only",
        "paper_fee_bps",
        "forward_require_forecast_alignment",
        "wechat_require_ai_review",
        "wechat_silence_brief_minutes",
        "record_replay_enabled",
    }
)

ACCURACY_METRIC_SCOPES: Dict[str, str] = {
    "prediction_accuracy_pct": "AI开=合并final_direction；AI关=本地score.final_direction · 当前UI验证窗",
    "ai_forward_direction_accuracy_pct": "decision_source=ai 的 forward_view · 每条 horizon_minutes",
    "model_edge_pct": "综合命中率减 baseline，非 AI 前瞻 edge",
    "paper_pnl_pct": "按日志 config_snapshot 重算；无快照时用当前配置并标注",
}


# Fixed in code; not exposed on Web config UI (always applied via apply_fixed_behavior_defaults).
FIXED_BEHAVIOR_KEYS: frozenset = frozenset(
    {
        "signal_watch_enabled",
        "watch_push_score",
        "spike_push_score",
        "ai_conflict_guard",
        "l3_local_spike_push",
        "l2_require_volume_or_structure",
        "push_cooldown_seconds",
        "spike_push_cooldown_seconds",
        "watch_push_cooldown_seconds",
        "reverse_trade_cooldown_seconds",
        "signal_forecast_enabled",
        "forecast_push_score",
        "forecast_horizon_minutes",
        "forecast_push_cooldown_seconds",
        "calibration_enabled",
        "calibration_min_samples",
        "calibration_blend_weight",
        "calibration_disable_below_hit_rate",
        "paper_follow_ai_only",
        "paper_fee_bps",
        "forward_require_forecast_alignment",
        "replay_ai_cache_enabled",
        "wechat_require_ai_review",
    }
)


def apply_fixed_behavior_defaults(config: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(config)
    for key in FIXED_BEHAVIOR_KEYS:
        merged[key] = MONITOR_BEHAVIOR_DEFAULTS[key]
    return merged


def sync_strategy_bound_config(config: Dict[str, Any]) -> Dict[str, Any]:
    merged = apply_fixed_behavior_defaults(config)
    merged["interval"] = recommended_interval_for_strategy(merged.get("strategy_mode"))
    return merged


def config_value(config: Any, key: str, default: Any = None) -> Any:
    if key in FIXED_BEHAVIOR_KEYS:
        return MONITOR_BEHAVIOR_DEFAULTS[key]
    if isinstance(config, dict) and key in config:
        return config[key]
    if default is not None:
        return default
    return MONITOR_BEHAVIOR_DEFAULTS.get(key)


def config_requires_monitor_restart(before: Dict[str, Any], after: Dict[str, Any]) -> bool:
    for key in MONITOR_RESTART_KEYS:
        if before.get(key) != after.get(key):
            return True
    return False


def build_log_config_snapshot(
    *,
    config: Any,
    push_score: int,
    short_push_score: int,
    ai_enabled: bool = False,
    push_enabled: bool = False,
) -> Dict[str, Any]:
    cfg = config
    return {
        "paper_follow_ai_only": bool(getattr(cfg, "paper_follow_ai_only", True)),
        "paper_fee_bps": float(getattr(cfg, "paper_fee_bps", 5.0)),
        "forward_require_forecast_alignment": bool(getattr(cfg, "forward_require_forecast_alignment", True)),
        "replay_ai_cache_enabled": bool(getattr(cfg, "replay_ai_cache_enabled", True)),
        "l3_local_spike_push": bool(getattr(cfg, "l3_local_spike_push", False)),
        "push_score": int(push_score),
        "short_push_score": int(short_push_score),
        "watch_push_score": int(getattr(cfg, "watch_push_score", 65)),
        "spike_push_score": int(getattr(cfg, "spike_push_score", 62)),
        "swing_spike_score_margin": SWING_SPIKE_SCORE_MARGIN,
        "swing_spike_confirm_rounds": SWING_SPIKE_CONFIRM_ROUNDS,
        "swing_spike_cooldown_seconds": SWING_SPIKE_COOLDOWN_SECONDS,
        "swing_ai_call_min_interval_seconds": SWING_AI_CALL_MIN_INTERVAL_SECONDS,
        "long_ai_call_min_interval_seconds": LONG_AI_CALL_MIN_INTERVAL_SECONDS,
        "swing_ai_sustained_review_interval_seconds": SWING_AI_SUSTAINED_REVIEW_INTERVAL_SECONDS,
        "forecast_push_score": int(getattr(cfg, "forecast_push_score", 58)),
        "local_trade_push_margin": LOCAL_TRADE_PUSH_MARGIN,
        "ai_enabled": bool(ai_enabled),
        "push_enabled": bool(push_enabled),
        "ai_periodic_interval_minutes": int(getattr(cfg, "ai_periodic_interval_minutes", 0) or 0),
    }


def paper_settings_from_log_item(
    item: Dict[str, Any],
    fallback: Dict[str, Any],
) -> Tuple[Dict[str, Any], str]:
    snap = item.get("config_snapshot")
    if isinstance(snap, dict):
        return (
            {
                "paper_follow_ai_only": bool(snap.get("paper_follow_ai_only", True)),
                "paper_fee_bps": max(0.0, float(snap.get("paper_fee_bps", 5.0))),
            },
            "log_snapshot",
        )
    return (
        {
            "paper_follow_ai_only": bool(config_value(fallback, "paper_follow_ai_only", True)),
            "paper_fee_bps": max(0.0, float(config_value(fallback, "paper_fee_bps", 5.0))),
        },
        "current_config",
    )


def build_effective_config_lines(
    *,
    mode: str,
    instruments: List[str],
    interval: int,
    ai_enabled: bool,
    push_enabled: bool,
    dry_run_ai: bool,
    config: Any,
    push_score: int,
    short_push_score: int,
) -> List[str]:
    cfg = config
    inst_text = ",".join(instruments) if instruments else "-"
    return [
        f"mode={mode} inst={inst_text} interval={interval}s",
        (
            f"ai={'on' if ai_enabled else 'off'}"
            f" dry_run={'on' if dry_run_ai else 'off'}"
            f" push={'on' if push_enabled else 'off'}"
        ),
        (
            f"strategy={getattr(cfg, 'strategy_mode', '-')} "
            f"risk={getattr(cfg, 'risk_preference', '-')} "
            f"trade={getattr(cfg, 'signal_trade_enabled', True)} "
            f"watch={getattr(cfg, 'signal_watch_enabled', True)} "
            f"spike={getattr(cfg, 'signal_spike_enabled', True)} "
            f"forecast={getattr(cfg, 'signal_forecast_enabled', True)}"
        ),
        (
            f"push_scores long={push_score} short={short_push_score} "
            f"watch={getattr(cfg, 'watch_push_score', '-')} "
            f"spike={getattr(cfg, 'spike_push_score', '-')} "
            f"forecast={getattr(cfg, 'forecast_push_score', '-')} "
            f"local_trade_margin=+{LOCAL_TRADE_PUSH_MARGIN} "
            f"swing_spike=+{SWING_SPIKE_SCORE_MARGIN}/{SWING_SPIKE_CONFIRM_ROUNDS}rounds "
            f"ai_interval swing={SWING_AI_CALL_MIN_INTERVAL_SECONDS}s long={LONG_AI_CALL_MIN_INTERVAL_SECONDS}s "
            f"sustained_review={SWING_AI_SUSTAINED_REVIEW_INTERVAL_SECONDS}s "
            f"periodic={getattr(cfg, 'ai_periodic_interval_minutes', 0)}m "
            f"silence_brief={getattr(cfg, 'wechat_silence_brief_minutes', 0)}m"
        ),
        (
            f"paper_ai_only={getattr(cfg, 'paper_follow_ai_only', True)} "
            f"paper_fee_bps={getattr(cfg, 'paper_fee_bps', 5.0)} "
            f"forward_align={getattr(cfg, 'forward_require_forecast_alignment', True)} "
            f"replay_ai_cache={getattr(cfg, 'replay_ai_cache_enabled', True)}"
        ),
    ]
