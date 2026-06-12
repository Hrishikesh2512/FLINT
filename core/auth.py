"""Supabase Auth — per-user sign up / sign in for FLINT.

Why this exists
    FLINT is shared freely, but the owner wants to know who is using it. Every
    user creates an email + password account here. Supabase stores only a
    salted *hash* of the password — FLINT never sees, sends, or stores the
    plaintext password anywhere. The owner gets a full user list (emails,
    sign-up date, last login) in the Supabase dashboard, with zero ability to
    read anyone's password. That is the secure, standard way to "track users".

What is persisted locally
    config/session.json (gitignored) holds the access + refresh tokens and the
    user's email / id / display name — NEVER the password. On the next launch
    the saved refresh token silently restores the session, so returning users
    are not asked to log in again.

Public surface
    available()                      -> bool   (supabase installed + configured)
    sign_up(email, password, name)   -> (profile|None, error|None)
    sign_in(email, password)         -> (profile|None, error|None)
    restore_session()                -> profile|None
    current_profile()                -> profile|None
    sign_out()                       -> None

A "profile" is a plain dict: {"email", "user_id", "name"}.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

try:
    from supabase import create_client, Client
    _SUPABASE_OK = True
except Exception:                      # pragma: no cover - import guard
    _SUPABASE_OK = False
    Client = object                    # type: ignore


def _resource_dir() -> Path:
    """Read-only bundled data. In a PyInstaller one-file exe this is the
    temporary _MEIPASS extraction dir; from source it's the project root."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


def _user_dir() -> Path:
    """Per-machine writable config. Next to the .exe when frozen so it persists
    across launches (the _MEIPASS dir is wiped each run)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


CONFIG_DIR   = _user_dir() / "config"
API_FILE     = CONFIG_DIR / "api_keys.json"     # per-machine, gitignored
SESSION_FILE = CONFIG_DIR / "session.json"      # per-machine, gitignored


def _app_config_path() -> Path:
    """app_config.json: a copy next to the exe wins (lets an owner override
    without rebuilding); otherwise the bundled one shipped inside the exe."""
    override = _user_dir() / "config" / "app_config.json"
    if override.exists():
        return override
    return _resource_dir() / "config" / "app_config.json"


APP_CONFIG = _app_config_path()


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _supabase_creds() -> tuple[str, str]:
    """(url, publishable/anon key) — bundled app_config first, then api_keys."""
    app = _read_json(APP_CONFIG)
    url = str(app.get("supabase_url", "")).strip()
    key = str(app.get("supabase_anon_key", "")).strip()
    if not (url and key):
        # Fall back to the dev machine's api_keys.json.
        keys = _read_json(API_FILE)
        url = url or str(keys.get("supabase_url", "")).strip()
        key = key or str(keys.get("supabase_key", "")).strip()
    return url, key


def bundled_keys() -> dict:
    """Owner-provided API keys baked into app_config.json (may be empty)."""
    app = _read_json(APP_CONFIG)
    out = {}
    g = str(app.get("bundled_gemini_api_key", "")).strip()
    o = str(app.get("bundled_openrouter_api_key", "")).strip()
    if g:
        out["gemini_api_key"] = g
    if o:
        out["openrouter_api_key"] = o
    return out


_client: "Client | None" = None


def _get_client() -> "Client | None":
    global _client
    if not _SUPABASE_OK:
        return None
    if _client is not None:
        return _client
    url, key = _supabase_creds()
    if not (url and key):
        return None
    try:
        _client = create_client(url, key)
    except Exception as e:
        print(f"[Auth] ⚠️ Could not init Supabase client: {e}")
        _client = None
    return _client


def available() -> bool:
    return _get_client() is not None


# ── session persistence (tokens only — never the password) ───────────────────
def _save_session(profile: dict, access: str | None, refresh: str | None) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    data = dict(profile)
    if access:
        data["access_token"] = access
    if refresh:
        data["refresh_token"] = refresh
    SESSION_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _profile_from_user(user, name_hint: str = "") -> dict:
    meta = {}
    try:
        meta = dict(user.user_metadata or {})
    except Exception:
        pass
    name = (name_hint or meta.get("name") or "").strip()
    email = ""
    user_id = ""
    try:
        email = (user.email or "").strip()
        user_id = str(user.id or "")
    except Exception:
        pass
    if not name:
        name = email.split("@")[0] if email else "Boss"
    return {"email": email, "user_id": user_id, "name": name}


def _friendly(error: Exception) -> str:
    msg = str(error)
    low = msg.lower()
    if "invalid login" in low or "invalid_credentials" in low:
        return "Incorrect email or password."
    if "already registered" in low or "already been registered" in low:
        return "That email already has an account — try signing in."
    if "password should be" in low or "weak" in low:
        return "Password is too weak (use at least 6 characters)."
    if "rate limit" in low:
        return "Too many attempts — please wait a minute and retry."
    if "network" in low or "connection" in low or "timed out" in low:
        return "Network error — check your internet connection."
    if "email" in low and "confirm" in low:
        return "Please confirm your email, then sign in."
    return msg[:160] or "Authentication failed."


# ── public auth operations ───────────────────────────────────────────────────
def sign_up(email: str, password: str, name: str = "") -> tuple[dict | None, str | None]:
    client = _get_client()
    if client is None:
        return None, "Accounts are unavailable (Supabase not configured)."
    try:
        resp = client.auth.sign_up({
            "email": email.strip(),
            "password": password,
            "options": {"data": {"name": name.strip()}},
        })
    except Exception as e:
        return None, _friendly(e)

    user = getattr(resp, "user", None)
    if user is None:
        return None, "Sign up failed — please try again."

    profile = _profile_from_user(user, name)
    session = getattr(resp, "session", None)
    access = getattr(session, "access_token", None) if session else None
    refresh = getattr(session, "refresh_token", None) if session else None
    _save_session(profile, access, refresh)
    return profile, None


def sign_in(email: str, password: str) -> tuple[dict | None, str | None]:
    client = _get_client()
    if client is None:
        return None, "Accounts are unavailable (Supabase not configured)."
    try:
        resp = client.auth.sign_in_with_password({
            "email": email.strip(),
            "password": password,
        })
    except Exception as e:
        return None, _friendly(e)

    user = getattr(resp, "user", None)
    session = getattr(resp, "session", None)
    if user is None or session is None:
        return None, "Incorrect email or password."

    profile = _profile_from_user(user)
    _save_session(profile, getattr(session, "access_token", None),
                  getattr(session, "refresh_token", None))
    return profile, None


def restore_session() -> dict | None:
    """Re-establish a saved session on launch. Returns the profile or None."""
    saved = _read_json(SESSION_FILE)
    refresh = str(saved.get("refresh_token", "")).strip()
    if not refresh:
        return None
    client = _get_client()
    if client is None:
        # Offline or misconfigured: trust the cached profile so the user is
        # not locked out, but without live token verification.
        return {k: saved.get(k, "") for k in ("email", "user_id", "name")} \
            if saved.get("email") else None
    try:
        resp = client.auth.refresh_session(refresh)
        session = getattr(resp, "session", None)
        user = getattr(resp, "user", None)
        if user is not None:
            profile = _profile_from_user(user, saved.get("name", ""))
            if session is not None:
                _save_session(profile, session.access_token, session.refresh_token)
            return profile
    except Exception as e:
        print(f"[Auth] ⚠️ Session restore failed: {e}")
    # Fall back to cached identity rather than forcing re-login on a hiccup.
    return {k: saved.get(k, "") for k in ("email", "user_id", "name")} \
        if saved.get("email") else None


def current_profile() -> dict | None:
    saved = _read_json(SESSION_FILE)
    return saved if saved.get("email") else None


def sign_out() -> None:
    try:
        client = _get_client()
        if client is not None:
            client.auth.sign_out()
    except Exception:
        pass
    try:
        SESSION_FILE.unlink(missing_ok=True)
    except Exception:
        pass
