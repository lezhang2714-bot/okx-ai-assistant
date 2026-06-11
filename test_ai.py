#!/usr/bin/env python3
import os
import sys
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


def main() -> int:
    load_env()
    api_key = os.getenv("AI_API_KEY") or os.getenv("OPENAI_API_KEY")
    base_url = os.getenv("AI_BASE_URL", "https://api.deepseek.com")
    model = os.getenv("AI_MODEL", "deepseek-chat")
    if not api_key:
        print("AI_API_KEY or OPENAI_API_KEY is not configured in .env.")
        return 1

    try:
        from openai import OpenAI
    except ImportError:
        print("openai package is not installed. Run ./install.sh first.")
        return 1

    client = OpenAI(api_key=api_key, base_url=base_url)
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": "请用一句中文回复：OKX AI短线助手AI接口连通性测试成功。"}],
    )
    print(response.choices[0].message.content)
    print(f"\nprovider: {'deepseek' if 'deepseek' in base_url else 'openai'}, model: {model}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
