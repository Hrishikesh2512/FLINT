"""Cloud-native FLINT agent — Phase 4 of the cloud architecture.

A fully **decoupled**, standalone execution loop that runs with no desktop
dependencies at all: no PyQt, no ``pyautogui``, no screen coordinates tied to
this laptop. Everything lives in the cloud:

    phone (binary PCM audio)
        │  WebSocket
        ▼
    CloudAgentServer ── per client ──▶ Gemini Live API session  (voice in/out)
                                            │  tool_call: browse_web(task)
                                            ▼
                                   ComputerUseAgent  (Gemini 2.5 Computer Use)
                                            │  click_at / type_text_at / navigate …
                                            ▼
                                   RemoteBrowser  ── Playwright over CDP ──▶
                                            Cloud Sandbox Chrome (remote container)

Two Gemini models cooperate:

* **Live API** (``live_model``) owns the spoken conversation. It receives the
  user's microphone stream from the phone and speaks back. It is given a
  single function tool, ``browse_web``, so the *model* decides when a request
  needs a real browser.

* **Computer Use** (``computer_use_model``) is a separate agentic loop. When
  ``browse_web`` fires, this loop drives a remote Chrome (via Playwright's
  Chrome DevTools Protocol connection to a sandbox container) by translating
  the model's UI function calls — ``click_at``, ``type_text_at``, ``navigate``,
  ``scroll_document`` … — into Playwright operations, screenshotting after each
  step and feeding the screenshot back to the model until the task completes.

Nothing here touches the local display. The browser the model controls is the
*remote* sandbox browser reached over CDP, so the whole thing can run headless
on a server with no GUI.

Configuration (config/api_keys.json):

    gemini_api_key            (required)
    cloud_agent_host          default "0.0.0.0"
    cloud_agent_port          default 8770
    cloud_agent_token         optional shared secret; clients must auth if set
    live_model                default models/gemini-2.5-flash-native-audio-preview-12-2025
    computer_use_model        default models/gemini-2.5-computer-use-preview-10-2025
    sandbox_cdp_url           CDP endpoint of the remote Chrome,
                              e.g. "http://sandbox-host:9222"  (required for browsing)
    sandbox_viewport          [width, height]  default [1280, 800]
    computer_use_max_steps    default 30
    auto_acknowledge_safety   default false — never silently confirm a
                              model-flagged sensitive browser action

Wire protocol (mobile client ↔ server):

    client -> server
        binary frame                      raw 16 kHz mono PCM mic audio
        {"type": "auth",  "token": "..."} optional, required if a token is set
        {"type": "end"}                   end of the current spoken turn
        {"type": "text",  "text": "..."}  optional typed prompt instead of audio

    server -> client
        {"type": "hello", "auth_required": bool, "live_model": "..."}
        {"type": "auth_ok"} | {"type": "auth_failed"}
        binary frame                      raw 24 kHz mono PCM Flint audio reply
        {"type": "transcript", "role": "user"|"flint", "text": "..."}
        {"type": "tool",  "name": "browse_web", "status": "running"|"done", ...}
        {"type": "error", "message": "..."}

Run standalone:

    python -m core.cloud_agent
"""

from __future__ import annotations

import asyncio
import json
import traceback
from pathlib import Path
from typing import Awaitable, Callable

try:
    from google import genai
    from google.genai import types
    _GENAI_OK = True
except ImportError:
    _GENAI_OK = False
    genai = None          # type: ignore
    types = None          # type: ignore

try:
    import websockets
    _WS_OK = True
except ImportError:
    _WS_OK = False

try:
    from playwright.async_api import async_playwright
    _PW_OK = True
except ImportError:
    _PW_OK = False


BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"

DEFAULT_LIVE_MODEL = "models/gemini-2.5-flash-native-audio-preview-12-2025"
DEFAULT_CU_MODEL = "models/gemini-2.5-computer-use-preview-10-2025"
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8770
DEFAULT_VIEWPORT = (1280, 800)
DEFAULT_MAX_STEPS = 30

SEND_SAMPLE_RATE = 16000      # phone mic → Gemini
RECEIVE_SAMPLE_RATE = 24000   # Gemini → phone

# Normalized coordinate space the Computer Use model emits (per-axis).
COORD_SPACE = 1000


def _say(msg: str) -> None:
    """print() that survives cp1252 consoles choking on emoji."""
    try:
        print(msg)
    except UnicodeEncodeError:
        print(msg.encode("ascii", "replace").decode("ascii"))


def _load_config() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        _say(f"[CloudAgent] ⚠️ Could not read config: {e}")
        return {}


# ── the one tool the Live model is given ──────────────────────────────────────
# The Live model decides *when* a request needs a real browser; we run it.
BROWSE_WEB_DECLARATION = {
    "name": "browse_web",
    "description": (
        "Perform a task that requires visually browsing or operating a real "
        "web browser — searching the web, reading pages, filling forms, "
        "clicking through a site. Runs autonomously in a remote browser and "
        "returns a short summary of what was found or done."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "A clear, self-contained description of the "
                               "browsing task, e.g. 'find the cheapest direct "
                               "flight from Delhi to Goa next Friday'.",
            },
            "start_url": {
                "type": "string",
                "description": "Optional URL to open first. Defaults to a "
                               "search engine when omitted.",
            },
        },
        "required": ["task"],
    },
}


# ══════════════════════════════════════════════════════════════════════════════
#  Remote browser — Playwright over CDP to the sandbox container
# ══════════════════════════════════════════════════════════════════════════════
class RemoteBrowser:
    """Drives a *remote* Chrome over the DevTools Protocol.

    We never launch a local browser — we ``connect_over_cdp`` to a Chrome
    already running inside the cloud sandbox, so the model operates a browser
    that lives in the container, not on this machine.
    """

    def __init__(self, cdp_url: str, viewport: tuple[int, int]):
        self.cdp_url = cdp_url
        self.width, self.height = viewport
        self._pw = None
        self._browser = None
        self._context = None
        self.page = None

    async def __aenter__(self) -> "RemoteBrowser":
        if not _PW_OK:
            raise RuntimeError("playwright not installed (pip install playwright)")
        if not self.cdp_url:
            raise RuntimeError("sandbox_cdp_url not configured — cannot reach the remote browser")
        self._pw = await async_playwright().start()
        # Attach to the sandbox's Chrome rather than spawning one locally.
        self._browser = await self._pw.chromium.connect_over_cdp(self.cdp_url)
        self._context = (self._browser.contexts[0]
                         if self._browser.contexts
                         else await self._browser.new_context())
        self.page = (self._context.pages[0]
                     if self._context.pages
                     else await self._context.new_page())
        await self.page.set_viewport_size({"width": self.width, "height": self.height})
        return self

    async def __aexit__(self, *exc) -> None:
        # Detach but do NOT kill the sandbox browser — the container owns it.
        try:
            if self._browser:
                await self._browser.close()
        finally:
            if self._pw:
                await self._pw.stop()

    # ── coordinate helpers ────────────────────────────────────────────────────
    def _px(self, x: int, y: int) -> tuple[float, float]:
        """Normalized 0-1000 (per axis) → real viewport pixels."""
        return (x / COORD_SPACE) * self.width, (y / COORD_SPACE) * self.height

    # ── screenshot / state ────────────────────────────────────────────────────
    async def screenshot(self) -> bytes:
        return await self.page.screenshot(type="png")

    async def current_url(self) -> str:
        try:
            return self.page.url
        except Exception:
            return ""

    # ── action dispatch ───────────────────────────────────────────────────────
    async def execute(self, name: str, args: dict) -> None:
        """Translate one Computer Use action into Playwright calls."""
        page = self.page

        if name == "open_web_browser":
            return  # already attached to a live page

        if name == "navigate":
            url = args.get("url") or args.get("website") or ""
            if url:
                await page.goto(url, wait_until="domcontentloaded")
            return

        if name == "search":
            query = args.get("query", "")
            await page.goto("https://www.google.com/search?q=" +
                            query.replace(" ", "+"), wait_until="domcontentloaded")
            return

        if name in ("click_at", "hover_at"):
            px, py = self._px(args["x"], args["y"])
            await page.mouse.move(px, py)
            if name == "click_at":
                await page.mouse.click(px, py)
            return

        if name == "type_text_at":
            px, py = self._px(args["x"], args["y"])
            await page.mouse.click(px, py)
            if args.get("clear_before_typing", True):
                await page.keyboard.press("Control+A")
                await page.keyboard.press("Delete")
            await page.keyboard.type(args.get("text", ""))
            if args.get("press_enter", False):
                await page.keyboard.press("Enter")
            return

        if name == "key_combination":
            keys = args.get("keys", "")
            combo = "+".join(keys) if isinstance(keys, list) else str(keys)
            # Computer Use uses lowercase + "ctrl"; Playwright wants "Control".
            combo = (combo.replace("ctrl", "Control").replace("cmd", "Meta")
                          .replace("alt", "Alt").replace("shift", "Shift"))
            await page.keyboard.press(combo)
            return

        if name == "scroll_document":
            direction = args.get("direction", "down").lower()
            await page.keyboard.press("PageDown" if direction == "down" else "PageUp")
            return

        if name == "scroll_at":
            px, py = self._px(args["x"], args["y"])
            await page.mouse.move(px, py)
            magnitude = float(args.get("magnitude", 400))
            direction = args.get("direction", "down").lower()
            dy = magnitude if direction == "down" else -magnitude
            dx = 0
            if direction in ("left", "right"):
                dx = -magnitude if direction == "left" else magnitude
                dy = 0
            await page.mouse.wheel(dx, dy)
            return

        if name == "drag_and_drop":
            x1, y1 = self._px(args["x"], args["y"])
            x2, y2 = self._px(args["destination_x"], args["destination_y"])
            await page.mouse.move(x1, y1)
            await page.mouse.down()
            await page.mouse.move(x2, y2, steps=12)
            await page.mouse.up()
            return

        if name in ("go_back", "go_forward"):
            await (page.go_back() if name == "go_back" else page.go_forward())
            return

        if name == "wait_5_seconds":
            await asyncio.sleep(5)
            return

        _say(f"[CloudAgent] ⚠️ Unmapped browser action: {name} {args}")


# ══════════════════════════════════════════════════════════════════════════════
#  Computer Use agentic loop
# ══════════════════════════════════════════════════════════════════════════════
class ComputerUseAgent:
    """Runs a single browsing task to completion with the Computer Use model."""

    def __init__(self, client, cfg: dict,
                 notify: Callable[[dict], Awaitable[None]] | None = None):
        self.client = client
        self.model = cfg.get("computer_use_model", DEFAULT_CU_MODEL)
        self.cdp_url = cfg.get("sandbox_cdp_url", "")
        vp = cfg.get("sandbox_viewport", list(DEFAULT_VIEWPORT))
        self.viewport = (int(vp[0]), int(vp[1]))
        self.max_steps = int(cfg.get("computer_use_max_steps", DEFAULT_MAX_STEPS))
        self.auto_ack_safety = bool(cfg.get("auto_acknowledge_safety", False))
        self._notify = notify

    def _tool_config(self):
        """The computer_use tool, scoped to a browser environment."""
        # Dict form so we don't depend on an exact SDK type name across versions.
        return types.GenerateContentConfig(
            tools=[{"computer_use": {"environment": "ENVIRONMENT_BROWSER"}}],
        )

    async def run(self, task: str, start_url: str = "") -> str:
        if not self.cdp_url:
            return ("Browsing is unavailable: no sandbox_cdp_url is configured, "
                    "so there is no remote browser to drive.")

        async with RemoteBrowser(self.cdp_url, self.viewport) as browser:
            if start_url:
                await browser.execute("navigate", {"url": start_url})

            shot = await browser.screenshot()
            prompt = (
                f"Task: {task}\n\n"
                "Operate the browser to accomplish this. When finished, reply "
                "with a concise plain-text summary of the result."
            )
            contents = [types.Content(role="user", parts=[
                types.Part(text=prompt),
                types.Part.from_bytes(data=shot, mime_type="image/png"),
            ])]
            cfg = self._tool_config()

            for step in range(1, self.max_steps + 1):
                resp = await self.client.aio.models.generate_content(
                    model=self.model, contents=contents, config=cfg)

                candidate = (resp.candidates or [None])[0]
                if candidate is None or not candidate.content:
                    return "The browser agent returned no response."
                parts = candidate.content.parts or []
                contents.append(candidate.content)  # keep the model's turn in history

                calls = [p.function_call for p in parts if getattr(p, "function_call", None)]
                if not calls:
                    # No more actions → the model's text is the final answer.
                    text = " ".join(p.text for p in parts if getattr(p, "text", None)).strip()
                    return text or "Task finished."

                # Execute every requested action, then feed back fresh screenshots.
                response_parts = []
                for fc in calls:
                    name = fc.name
                    args = dict(fc.args or {})

                    if self._requires_safety_block(args):
                        msg = (f"Refused browser action '{name}': it was flagged "
                               "as sensitive and auto-acknowledge is off.")
                        _say(f"[CloudAgent] 🔒 {msg}")
                        return msg

                    try:
                        await browser.execute(name, args)
                        await self._emit({"type": "tool", "name": "browse_web",
                                          "status": "running", "step": step,
                                          "action": name})
                    except Exception as e:
                        _say(f"[CloudAgent] ⚠️ action {name} failed: {e}")

                    shot = await browser.screenshot()
                    url = await browser.current_url()
                    response_parts.append(types.Part(
                        function_response=types.FunctionResponse(
                            id=getattr(fc, "id", None),
                            name=name,
                            response={"url": url},
                        )))
                    response_parts.append(
                        types.Part.from_bytes(data=shot, mime_type="image/png"))

                contents.append(types.Content(role="user", parts=response_parts))

            return f"Stopped after {self.max_steps} browser steps without finishing."

    def _requires_safety_block(self, args: dict) -> bool:
        """True if the model flagged this action sensitive and we shouldn't auto-run it."""
        if self.auto_ack_safety:
            return False
        decision = args.get("safety_decision") or args.get("safety")
        if isinstance(decision, dict):
            return bool(decision.get("requires_confirmation"))
        return False

    async def _emit(self, payload: dict) -> None:
        if self._notify:
            try:
                await self._notify(payload)
            except Exception:
                pass


# ══════════════════════════════════════════════════════════════════════════════
#  Per-client Live bridge
# ══════════════════════════════════════════════════════════════════════════════
class LiveBridgeSession:
    """One connected phone ⇄ one Gemini Live session ⇄ browsing tool."""

    def __init__(self, ws, client, cfg: dict):
        self.ws = ws
        self.client = client
        self.cfg = cfg
        self.live_model = cfg.get("live_model", DEFAULT_LIVE_MODEL)
        self.session = None

    def _live_config(self):
        return types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            system_instruction=(
                "You are FLINT, a fast spoken assistant operated from a phone. "
                "Keep replies short and natural. Whenever a request needs you to "
                "actually look at or operate a website, call the browse_web tool "
                "and then summarise what it returns out loud."
            ),
            tools=[{"function_declarations": [BROWSE_WEB_DECLARATION]}],
        )

    async def _send_json(self, payload: dict) -> None:
        await self.ws.send(json.dumps(payload))

    # ── pumps ─────────────────────────────────────────────────────────────────
    async def _phone_to_gemini(self) -> None:
        """Forward inbound mobile frames (binary audio / JSON control) to Gemini."""
        async for message in self.ws:
            if isinstance(message, (bytes, bytearray)):
                await self.session.send_realtime_input(
                    media={"data": bytes(message),
                           "mime_type": f"audio/pcm;rate={SEND_SAMPLE_RATE}"})
                continue
            # text frame → control message
            try:
                msg = json.loads(message)
            except Exception:
                continue
            mtype = msg.get("type")
            if mtype == "end":
                await self.session.send_realtime_input(audio_stream_end=True)
            elif mtype == "text" and msg.get("text"):
                await self.session.send_client_content(
                    turns={"role": "user", "parts": [{"text": msg["text"]}]},
                    turn_complete=True)

    async def _gemini_to_phone(self) -> None:
        """Stream Gemini audio + transcripts back, and service tool calls."""
        while True:
            turn = self.session.receive()
            async for response in turn:
                if response.data:                       # 24 kHz PCM audio out
                    await self.ws.send(response.data)
                if response.text:
                    await self._send_json({"type": "transcript",
                                           "role": "flint", "text": response.text})
                tool_call = getattr(response, "tool_call", None)
                if tool_call and tool_call.function_calls:
                    await self._handle_tool_calls(tool_call.function_calls)

    async def _handle_tool_calls(self, function_calls) -> None:
        responses = []
        for fc in function_calls:
            if fc.name == "browse_web":
                args = dict(fc.args or {})
                task = args.get("task", "")
                await self._send_json({"type": "tool", "name": "browse_web",
                                       "status": "running", "task": task})
                agent = ComputerUseAgent(self.client, self.cfg, notify=self._send_json)
                try:
                    result = await agent.run(task, args.get("start_url", ""))
                except Exception as e:
                    result = f"Browsing failed: {e}"
                    _say(f"[CloudAgent] ⚠️ browse_web error: {e}")
                await self._send_json({"type": "tool", "name": "browse_web",
                                       "status": "done", "result": result})
                responses.append(types.FunctionResponse(
                    id=getattr(fc, "id", None), name=fc.name,
                    response={"result": result}))
            else:
                responses.append(types.FunctionResponse(
                    id=getattr(fc, "id", None), name=fc.name,
                    response={"error": f"unknown tool {fc.name}"}))
        if responses:
            await self.session.send_tool_response(function_responses=responses)

    # ── lifecycle ─────────────────────────────────────────────────────────────
    async def run(self) -> None:
        config = self._live_config()
        async with self.client.aio.live.connect(
                model=self.live_model, config=config) as session:
            self.session = session
            _say("[CloudAgent] ✅ Live session open for a client")
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self._phone_to_gemini())
                tg.create_task(self._gemini_to_phone())


# ══════════════════════════════════════════════════════════════════════════════
#  WebSocket server
# ══════════════════════════════════════════════════════════════════════════════
class CloudAgentServer:
    """Accepts mobile clients and bridges each to its own Live session."""

    def __init__(self, cfg: dict | None = None):
        self.cfg = cfg or _load_config()
        self.host = self.cfg.get("cloud_agent_host", DEFAULT_HOST)
        self.port = int(self.cfg.get("cloud_agent_port", DEFAULT_PORT))
        self.token = str(self.cfg.get("cloud_agent_token", "") or "")
        self.client = None

    def _build_client(self):
        api_key = self.cfg.get("gemini_api_key")
        if not api_key:
            raise RuntimeError("gemini_api_key missing in config/api_keys.json")
        return genai.Client(api_key=api_key, http_options={"api_version": "v1beta"})

    async def _authenticate(self, ws) -> bool:
        """Optional shared-token handshake before a Live session is opened."""
        await ws.send(json.dumps({
            "type": "hello",
            "auth_required": bool(self.token),
            "live_model": self.cfg.get("live_model", DEFAULT_LIVE_MODEL),
        }))
        if not self.token:
            return True
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=10)
            msg = json.loads(raw) if isinstance(raw, str) else {}
        except Exception:
            return False
        ok = msg.get("type") == "auth" and msg.get("token") == self.token
        await ws.send(json.dumps({"type": "auth_ok" if ok else "auth_failed"}))
        return ok

    async def _handler(self, ws) -> None:
        peer = getattr(ws, "remote_address", "?")
        _say(f"[CloudAgent] 🔗 client connected: {peer}")
        try:
            if not await self._authenticate(ws):
                _say(f"[CloudAgent] 🚫 auth failed: {peer}")
                return
            bridge = LiveBridgeSession(ws, self.client, self.cfg)
            await bridge.run()
        except* Exception as eg:
            for e in eg.exceptions:
                _say(f"[CloudAgent] ⚠️ client {peer}: {e}")
                traceback.print_exception(type(e), e, e.__traceback__)
        finally:
            _say(f"[CloudAgent] 🔌 client gone: {peer}")

    async def serve_forever(self) -> None:
        if not _GENAI_OK:
            raise RuntimeError("google-genai not installed (pip install google-genai)")
        if not _WS_OK:
            raise RuntimeError("websockets not installed (pip install websockets)")
        self.client = self._build_client()
        _say(f"[CloudAgent] 🎧 listening on ws://{self.host}:{self.port} "
             f"(auth {'on' if self.token else 'off'})")
        if not self.cfg.get("sandbox_cdp_url"):
            _say("[CloudAgent] ⚠️ sandbox_cdp_url not set — voice works, but "
                 "browse_web will report browsing is unavailable.")
        async with websockets.serve(self._handler, self.host, self.port,
                                    max_size=None, ping_interval=20):
            await asyncio.Future()  # run until cancelled


async def serve() -> None:
    await CloudAgentServer().serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        _say("\n[CloudAgent] ⏹️ stopped")
