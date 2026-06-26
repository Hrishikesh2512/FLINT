"""Minimal Gemini connectivity check.

Loads the key the same way the app does, so no secret lives in source:
  1. the GEMINI_API_KEY environment variable, if set, otherwise
  2. config/api_keys.json (gitignored), via memory.config_manager.

Run: python test.py
"""
import os
import sys

import google.generativeai as genai

from memory.config_manager import get_gemini_key

API_KEY = os.environ.get("GEMINI_API_KEY") or get_gemini_key()

if not API_KEY:
    sys.exit(
        "No Gemini API key found. Set GEMINI_API_KEY or add it to "
        "config/api_keys.json (see Configuration in the README)."
    )

genai.configure(api_key=API_KEY)

model = genai.GenerativeModel("gemini-2.0-flash")

response = model.generate_content("hello")

print(response.text)
