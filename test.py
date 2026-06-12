"""Quick Gemini connectivity check.

Reads the key from the GEMINI_API_KEY env var or config/api_keys.json — never
hardcoded — so this file is safe to commit. Run:  python test.py
"""

import json
import os
from pathlib import Path


def _load_key() -> str:
    env = os.environ.get("GEMINI_API_KEY")
    if env:
        return env.strip()
    cfg = Path(__file__).resolve().parent / "config" / "api_keys.json"
    try:
        return str(json.loads(cfg.read_text(encoding="utf-8")).get("gemini_api_key", "")).strip()
    except Exception:
        return ""


def main() -> None:
    key = _load_key()
    if not key:
        raise SystemExit(
            "No Gemini key found. Set GEMINI_API_KEY or fill config/api_keys.json.")
    from google import genai
    client = genai.Client(api_key=key)
    resp = client.models.generate_content(
        model="gemini-2.0-flash", contents="hello")
    print(resp.text)


if __name__ == "__main__":
    main()
