"""解析 config.json，输出 monitor.py 命令行参数。由 run.sh / run.bat 调用。"""
import json
import sys
from pathlib import Path

CONFIG_PATH = Path(__file__).with_name("config.json")

with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    cfg = json.load(f)

args = []

inst_ids = cfg.get("inst_ids") or ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]
args += ["--inst-ids", ",".join(inst_ids)]

for key, option in [
    ("interval", "--interval"),
    ("runtime", "--runtime"),
    ("flag", "--flag"),
    ("push_score", "--push-score"),
    ("retry_times", "--retry-times"),
    ("retry_backoff", "--retry-backoff"),
    ("push_cooldown_seconds", "--push-cooldown"),
    ("log_max_bytes", "--log-max-bytes"),
    ("volume_multiplier", "--volume-multiplier"),
    ("oi_change_pct_15m", "--oi-change-pct-15m"),
    ("funding_abs_threshold", "--funding-threshold"),
    ("funding_change_threshold", "--funding-change-threshold"),
    ("long_short_extreme", "--long-short-extreme"),
]:
    if key in cfg:
        args += [option, str(cfg[key])]

if cfg.get("ai_enabled"):
    args.append("--ai")
if cfg.get("dry_run_ai"):
    args.append("--dry-run-ai")
if cfg.get("push_enabled"):
    args.append("--push")

print(" ".join(args))
