#!/usr/bin/env python3
import json
import os
import sys
import urllib.request
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
ENV_FILE = SCRIPT_DIR / ".env"


def load_env() -> None:
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def post_json(url: str, payload: dict) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as response:
        print(response.read().decode("utf-8", errors="ignore"))


def main() -> int:
    load_env()
    message = "[OKX AI短线助手] 推送测试成功。"
    sent = False

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if token and chat_id:
        print("[push-test] telegram")
        post_json(f"https://api.telegram.org/bot{token}/sendMessage", {"chat_id": chat_id, "text": message})
        sent = True

    for name in ("WECOM_WEBHOOK_URL", "WECHAT_WEBHOOK_URL"):
        url = os.getenv(name)
        if url:
            print(f"[push-test] {name}")
            post_json(url, {"msgtype": "text", "text": {"content": message}})
            sent = True

    if not sent:
        print("No push channel configured. Fill TELEGRAM_* or WECOM_WEBHOOK_URL/WECHAT_WEBHOOK_URL in .env.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
