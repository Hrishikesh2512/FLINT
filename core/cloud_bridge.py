"""Supabase command bridge — Phase 2 of the cloud architecture.

Lets a remote client (web dashboard, phone, automation) drop a row into a
Supabase ``command_queue`` table and have FLINT execute the matching action
on this desktop.  The whole thing runs on its own daemon thread with its own
poll loop, so the Qt desktop frame, the Gemini Live session and the audio
loops are never blocked.

Table contract (``command_queue`` by default — override in config):

    column        meaning
    ──────        ───────────────────────────────────────────────────────────
    id            primary key (any type; used only to address the row back)
    action_name   which action to run — e.g. "open_app", "web_search"
    status        'pending' → picked up; set to 'executed' when finished
    payload       OPTIONAL json/jsonb of arguments for the action
                  (also accepted: 'params' / 'parameters' / 'args')

Optional, best-effort columns (written only if they exist on the table):

    result        short text summary of what the action returned
    error         error text when the action raised
    executed_at   ISO-8601 timestamp the row was completed

Configuration lives in config/api_keys.json:

    "supabase_url":            "https://xxxx.supabase.co"   (required)
    "supabase_key":            "service-role or anon key"   (required)
    "supabase_command_table":  "command_queue"              (optional)
    "supabase_poll_interval":  2.0                          (optional, seconds)

The action_name maps to a function inside the actions/ package.  Most action
modules expose a function with the same name as the module; the handful that
don't are listed in _ACTION_ENTRYPOINTS below.  Arguments from ``payload`` are
matched against each action's real signature, so heterogeneous action APIs all
work through one dispatcher.
"""

from __future__ import annotations

import importlib
import inspect
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

try:
    from supabase import create_client, Client
    _SUPABASE_OK = True
except ImportError:
    _SUPABASE_OK = False
    Client = Any  # type: ignore

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"

DEFAULT_TABLE = "command_queue"
DEFAULT_POLL_INTERVAL = 2.0
STATUS_PENDING = "pending"
STATUS_DONE = "executed"

# action_name -> (module under actions/, entry function inside that module).
# Only modules whose entry function differs from the module name need listing;
# everything else falls back to actions.<action_name>.<action_name>.
_ACTION_ENTRYPOINTS: dict[str, tuple[str, str]] = {
    "weather_report":  ("weather_report",  "weather_action"),
    "desktop_control": ("desktop",         "desktop_control"),
    "screen_process":  ("screen_processor", "screen_process"),
}

# argument names this bridge knows how to supply to an action function.
_PAYLOAD_KEYS = ("payload", "params", "parameters", "args")


def _say(msg: str) -> None:
    """print() that survives cp1252 consoles choking on emoji."""
    try:
        print(msg)
    except UnicodeEncodeError:
        print(msg.encode("ascii", "replace").decode("ascii"))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_config() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        _say(f"[Cloud] ⚠️ Could not read config: {e}")
        return {}


def _resolve_action(action_name: str) -> Callable | None:
    """action_name -> callable from the actions/ package, or None."""
    if not action_name:
        return None
    module_name, func_name = _ACTION_ENTRYPOINTS.get(
        action_name, (action_name, action_name))
    try:
        module = importlib.import_module(f"actions.{module_name}")
    except Exception as e:
        _say(f"[Cloud] ⚠️ No action module 'actions.{module_name}': {e}")
        return None
    func = getattr(module, func_name, None)
    if not callable(func):
        _say(f"[Cloud] ⚠️ '{module_name}' has no callable '{func_name}'")
        return None
    return func


def _extract_payload(row: dict) -> dict:
    """Pull the action arguments out of whichever column the client used."""
    for key in _PAYLOAD_KEYS:
        raw = row.get(key)
        if raw in (None, "", {}):
            continue
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                pass
    return {}


class CloudBridge:
    """Threaded Supabase ``command_queue`` listener + action dispatcher."""

    def __init__(self, url: str | None = None, key: str | None = None,
                 table: str = DEFAULT_TABLE,
                 poll_interval: float = DEFAULT_POLL_INTERVAL):
        self.url = (url or "").strip()
        self.key = (key or "").strip()
        self.table = table or DEFAULT_TABLE
        self.poll_interval = max(0.5, float(poll_interval or DEFAULT_POLL_INTERVAL))

        # Optional desktop context injected by main.py so actions that draw on
        # the UI / speak still work; defaults to headless (None) when run alone.
        self.player: Any = None
        self.speak: Callable[[str], None] | None = None

        self._client: Client | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._running = False

    # ── status ────────────────────────────────────────────────────────────────
    @property
    def available(self) -> bool:
        return _SUPABASE_OK

    @property
    def running(self) -> bool:
        return self._running

    @property
    def configured(self) -> bool:
        return bool(
            self.url and self.key
            and "YOUR-" not in self.url and "YOUR-" not in self.key
        )

    # ── lifecycle ─────────────────────────────────────────────────────────────
    def start(self) -> bool:
        if not _SUPABASE_OK:
            _say("[Cloud] ⚠️ 'supabase' package not installed — cloud bridge "
                 "disabled (pip install supabase)")
            return False
        if not self.configured:
            _say("[Cloud] ⚠️ Supabase URL/key not set in config/api_keys.json — "
                 "cloud bridge disabled")
            return False
        if self._thread and self._thread.is_alive():
            return True
        try:
            self._client = create_client(self.url, self.key)
        except Exception as e:
            _say(f"[Cloud] ❌ Could not init Supabase client: {e}")
            return False

        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="FlintCloudBridge")
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        self._running = False

    # ── poll loop (background thread) ─────────────────────────────────────────
    def _run(self) -> None:
        self._running = True
        _say(f"[Cloud] ✅ Listening on Supabase table '{self.table}' "
             f"(poll {self.poll_interval}s)")
        while not self._stop.is_set():
            try:
                rows = self._fetch_pending()
                for row in rows:
                    if self._stop.is_set():
                        break
                    self._process_row(row)
            except Exception as e:
                _say(f"[Cloud] ⚠️ Poll error: {e}")
            # Event-based sleep so stop() wakes us immediately.
            self._stop.wait(self.poll_interval)
        self._running = False
        _say("[Cloud] ⏹️ Cloud bridge stopped")

    def _fetch_pending(self) -> list[dict]:
        resp = (self._client.table(self.table)
                .select("*")
                .eq("status", STATUS_PENDING)
                .order("id")
                .execute())
        return list(resp.data or [])

    def _process_row(self, row: dict) -> None:
        row_id = row.get("id")
        action_name = str(row.get("action_name", "")).strip()
        _say(f"[Cloud] 📥 Command #{row_id}: {action_name!r}")

        func = _resolve_action(action_name)
        result_text, error_text = "", ""
        if func is None:
            error_text = f"unknown action_name: {action_name!r}"
        else:
            payload = _extract_payload(row)
            try:
                result = self._invoke(func, payload)
                result_text = "" if result is None else str(result)
                _say(f"[Cloud] ✅ #{row_id} {action_name} → "
                     f"{(result_text or 'done')[:80]}")
            except Exception as e:
                error_text = str(e)
                _say(f"[Cloud] ❌ #{row_id} {action_name} failed: {e}")

        self._mark_executed(row_id, result_text, error_text)

    def _invoke(self, func: Callable, payload: dict) -> Any:
        """Call an action, supplying only the kwargs its signature accepts."""
        available = {
            "parameters": payload,
            "player": self.player,
            "speak": self.speak,
            "response": None,
            "session_memory": None,
        }
        try:
            sig = inspect.signature(func)
            accepts_var_kw = any(
                p.kind is inspect.Parameter.VAR_KEYWORD
                for p in sig.parameters.values())
            if accepts_var_kw:
                kwargs = available
            else:
                kwargs = {k: v for k, v in available.items()
                          if k in sig.parameters}
            # An action that takes none of our known kwargs but does take a
            # single positional likely wants the payload dict itself.
            if not kwargs and payload:
                return func(payload)
        except (TypeError, ValueError):
            kwargs = {"parameters": payload, "player": self.player}
        return func(**kwargs)

    def _mark_executed(self, row_id: Any, result_text: str,
                       error_text: str) -> None:
        if row_id is None:
            return
        # Spec-guaranteed update: just the status. Done first so the row is
        # never re-picked even if the optional enrichment below fails.
        try:
            (self._client.table(self.table)
             .update({"status": STATUS_DONE})
             .eq("id", row_id)
             .execute())
        except Exception as e:
            _say(f"[Cloud] ⚠️ Could not mark #{row_id} executed: {e}")
            return

        # Best-effort enrichment — silently ignored if columns don't exist.
        extra = {"executed_at": _now_iso()}
        if result_text:
            extra["result"] = result_text[:2000]
        if error_text:
            extra["error"] = error_text[:2000]
        try:
            (self._client.table(self.table)
             .update(extra)
             .eq("id", row_id)
             .execute())
        except Exception:
            pass


# ── module-level singleton ────────────────────────────────────────────────────
_bridge: CloudBridge | None = None
_bridge_lock = threading.Lock()


def get_bridge() -> CloudBridge:
    """Build (once) the CloudBridge from config/api_keys.json."""
    global _bridge
    with _bridge_lock:
        if _bridge is None:
            cfg = _load_config()
            _bridge = CloudBridge(
                url=cfg.get("supabase_url"),
                key=cfg.get("supabase_key"),
                table=cfg.get("supabase_command_table", DEFAULT_TABLE),
                poll_interval=cfg.get("supabase_poll_interval",
                                      DEFAULT_POLL_INTERVAL),
            )
        return _bridge


if __name__ == "__main__":
    bridge = get_bridge()
    if not bridge.start():
        _say("[Cloud] Bridge did not start (see warnings above).")
        raise SystemExit(1)
    _say("[Cloud] Bridge running. Insert a row into command_queue with "
         "status='pending' and action_name set. Ctrl+C to quit.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        bridge.stop()
