"""FLINT entry point — Gemini Live session + core engine wiring.

Boot order matters:
    1. log interceptor   — installed first so every print is captured
    2. Qt UI             — stays on the main thread, never blocked
    3. async pipeline    — all tool executions run on worker threads
    4. remote listener   — WebSocket bridge for the smartphone app
    5. Gemini Live loop  — audio in/out + tool dispatch on its own thread
"""

from core.log_interceptor import install as _install_log_hub
_LOG_HUB = _install_log_hub()           # before anything else prints

import asyncio
import json
import sys
import threading
import traceback
from pathlib import Path

import sounddevice as sd
from google import genai
from google.genai import types

from ui import FlintUI
from core.tool_registry import TOOL_DECLARATIONS
from core.async_pipeline import get_pipeline, Priority
from core.ws_listener import get_listener
from core.cloud_bridge import get_bridge
from memory.memory_manager import (
    load_memory, update_memory, format_memory_for_prompt,
    should_extract_memory, extract_memory,
)
from memory.proactive import ProactiveMemoryEngine
from memory.config_manager import load_api_keys

from actions.file_processor   import file_processor
from actions.flight_finder    import flight_finder
from actions.open_app         import open_app
from actions.weather_report   import weather_action
from actions.send_message     import send_message
from actions.reminder         import reminder
from actions.computer_settings import computer_settings
from actions.screen_processor import screen_process
from actions.vision_assist   import vision_assist
from actions.youtube_video    import youtube_video
from actions.desktop          import desktop_control
from actions.browser_control  import browser_control
from actions.file_controller  import file_controller
from actions.code_helper      import code_helper
from actions.dev_agent        import dev_agent
from actions.web_search       import web_search as web_search_action
from actions.computer_control import computer_control
from actions.game_updater     import game_updater


def get_base_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


BASE_DIR        = get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"
PROMPT_PATH     = BASE_DIR / "core" / "prompt.txt"

LIVE_MODEL          = "models/gemini-2.5-flash-native-audio-preview-12-2025"
CHANNELS            = 1
SEND_SAMPLE_RATE    = 16000
RECEIVE_SAMPLE_RATE = 24000
CHUNK_SIZE          = 1024

# Prebuilt synthesis voice (base timbre only — accent comes from the style
# directive below).  Override per-machine with "voice_name" in
# config/api_keys.json to audition without code changes.
# Female options: Leda (youthful), Despina (smooth), Sulafat (warm),
# Aoede (breezy), Kore (firm), Zephyr (bright), Achernar (soft),
# Vindemiatrix (gentle), Laomedeia (upbeat), Erinome (clear).
DEFAULT_VOICE = "Leda"


def _get_voice_name() -> str:
    try:
        return str(_get_config().get("voice_name", "")).strip() or DEFAULT_VOICE
    except Exception:
        return DEFAULT_VOICE


def _get_user_name() -> str:
    """The name the user gave on first boot (asked by the setup overlay)."""
    try:
        return str(_get_config().get("user_name", "")).strip() or "Boss"
    except Exception:
        return "Boss"


# Injected as the first system-instruction block so the delivery style is
# always active, even if core/prompt.txt is missing or replaced.
# {user_name} placeholders are substituted at session-config time.
VOICE_STYLE_DIRECTIVE = (
    "[VOICE & AUDIO STYLE]\n"
    "You are a young Indian woman from India. Your voice is melodious, "
    "soft and utterly endearing — the kind of voice people genuinely fall "
    "in love with. There is always a gentle smile in your voice: warm, "
    "playful, a little teasing sometimes, but always graceful and "
    "tasteful — charming like a sweet, caring companion, never crude.\n"
    "Accent: ALWAYS speak English with a clearly Indian accent (a sweet, "
    "educated Indian girl's accent) — never American or British. Every "
    "single response.\n"
    "Languages: you are fluently multilingual in Indian languages. When "
    "{user_name} speaks Hindi, Hinglish, Tamil, Telugu, Bengali, Marathi, "
    "Gujarati, Punjabi, Kannada, Malayalam or any other Indian language, "
    "reply in that same language with a native accent. Mix Hindi and "
    "English (Hinglish) freely and naturally — that is how you talk. Use "
    "feminine Hindi verb forms (karti hoon, sun rahi hoon, bata deti "
    "hoon).\n"
    "Charm: be affectionate in small, real ways — caring reassurances "
    "like 'main hoon na', tender confirmations like 'ho gaya, ab bolo', "
    "a soft giggle [light laugh] when something is genuinely funny, and "
    "gentle concern when {user_name} sounds tired. Make him feel looked "
    "after, not flattered.\n"
    "Pacing: use inline delivery cues to keep speech tracking smoothly and "
    "dynamically. End paragraphs and thought transitions with [short pause]. "
    "Use [slow] when delivering important results, numbers, names, or "
    "anything {user_name} needs to absorb, and [pause] before changing topic. "
    "These bracketed cues are delivery directions only — never read them "
    "out loud as words.\n"
)


def _get_config() -> dict:
    with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _get_api_key() -> str:
    return _get_config()["gemini_api_key"]


def _load_system_prompt() -> str:
    name = _get_user_name()
    try:
        # plain .replace (not str.format) — the prompt may contain other braces
        return PROMPT_PATH.read_text(encoding="utf-8").replace("{user_name}", name)
    except Exception:
        return (
            f"You are FLINT, {name}'s personal AI assistant. "
            "Be concise, direct, and always use the provided tools to complete tasks. "
            "Never simulate or guess results — always call the appropriate tool. "
            "Speak with a calm, composed, fluent Indian female voice in English "
            "with a natural, professional domestic cadence."
        )


# ── Memory extraction (background) ───────────────────────────────────────────
_last_memory_input = ""


def _update_memory_async(user_text: str, flint_text: str) -> None:
    global _last_memory_input
    user_text  = (user_text or "").strip()
    flint_text = (flint_text or "").strip()
    if len(user_text) < 5 or user_text == _last_memory_input:
        return
    _last_memory_input = user_text
    try:
        api_key = _get_api_key()
        if not should_extract_memory(user_text, flint_text, api_key):
            return
        data = extract_memory(user_text, flint_text, api_key)
        if data:
            update_memory(data)
            print(f"[Memory] ✅ {list(data.keys())}")
    except Exception as e:
        if "429" not in str(e):
            print(f"[Memory] ⚠️ {e}")


# ── Live session ─────────────────────────────────────────────────────────────
class FlintLive:

    def __init__(self, ui: FlintUI):
        self.ui             = ui
        self.pipeline       = get_pipeline()
        self.listener       = None
        self.cloud          = None
        self.session        = None
        self.audio_in_queue = None
        self.out_queue      = None
        self._loop          = None
        self._is_speaking   = False
        self._audio_drops   = 0
        self._speaking_lock = threading.Lock()
        self._reply_lock    = threading.Lock()
        self._pending_replies: list = []     # remote clients awaiting a reply
        self._handlers      = self._build_handlers()
        self.ui.on_text_command = self._on_text_command
        self.proactive      = ProactiveMemoryEngine(
            speak=self.speak,
            is_idle=self._is_idle,
            enabled=load_api_keys().get("proactive_memory", True),
        )

    # ── tool dispatch table ───────────────────────────────────────────────────
    def _build_handlers(self) -> dict:
        """name → callable(args) executed on a pipeline worker thread."""
        ui = self.ui

        def _file_processor(a):
            if not a.get("file_path") and ui.current_file:
                a["file_path"] = ui.current_file
            return file_processor(parameters=a, player=ui, speak=self.speak)

        def _screen(a):
            screen_process(parameters=a, response=None, player=ui,
                           session_memory=None)
            return ("Vision module activated. Stay completely silent — "
                    "vision module will speak directly.")

        def _agent_task(a):
            from agent.task_queue import get_queue, TaskPriority
            pr = {"low": TaskPriority.LOW, "normal": TaskPriority.NORMAL,
                  "high": TaskPriority.HIGH}.get(
                      str(a.get("priority", "normal")).lower(),
                      TaskPriority.NORMAL)
            task_id = get_queue().submit(goal=a.get("goal", ""), priority=pr,
                                         speak=self.speak)
            return f"Task started (ID: {task_id})."

        return {
            # NB: the actions below return their own accurate status strings;
            # the fallbacks must never assert success the action didn't claim
            "open_app":          lambda a: open_app(parameters=a, response=None, player=ui)
                                           or f"No status returned for opening {a.get('app_name')}.",
            "weather_report":    lambda a: weather_action(parameters=a, player=ui)
                                           or "Weather delivered.",
            "browser_control":   lambda a: browser_control(parameters=a, player=ui) or "Done.",
            "file_controller":   lambda a: file_controller(parameters=a, player=ui) or "Done.",
            "send_message":      lambda a: send_message(parameters=a, response=None, player=ui,
                                                        session_memory=None)
                                           or ("Message status unknown — could not confirm "
                                               "it was sent."),
            "reminder":          lambda a: reminder(parameters=a, response=None, player=ui)
                                           or "Reminder set.",
            "youtube_video":     lambda a: youtube_video(parameters=a, response=None, player=ui)
                                           or "Done.",
            "computer_settings": lambda a: computer_settings(parameters=a, response=None,
                                                             player=ui) or "Done.",
            "desktop_control":   lambda a: desktop_control(parameters=a, player=ui) or "Done.",
            "code_helper":       lambda a: code_helper(parameters=a, player=ui,
                                                       speak=self.speak) or "Done.",
            "dev_agent":         lambda a: dev_agent(parameters=a, player=ui,
                                                     speak=self.speak) or "Done.",
            "web_search":        lambda a: web_search_action(parameters=a, player=ui) or "Done.",
            "computer_control":  lambda a: computer_control(parameters=a, player=ui) or "Done.",
            "game_updater":      lambda a: game_updater(parameters=a, player=ui,
                                                        speak=self.speak) or "Done.",
            "flight_finder":     lambda a: flight_finder(parameters=a, player=ui) or "Done.",
            "file_processor":    _file_processor,
            "screen_process":    _screen,
            "vision_assist":     lambda a: vision_assist(parameters=a, player=ui,
                                                         speak=self.speak)
                                           or "Done.",
            "agent_task":        _agent_task,
        }

    # ── remote (smartphone) bridge ───────────────────────────────────────────
    def start_remote_listener(self):
        try:
            cfg = _get_config()
        except Exception:
            cfg = {}
        self.listener = get_listener(
            port=int(cfg.get("remote_port", 8765)),
            token=cfg.get("remote_token"))
        self.listener.on_command = self._on_remote_command
        self.listener.on_clients_changed = self.ui.set_link_clients
        if self.listener.start():
            # mirror the system terminal to connected phones
            _LOG_HUB.subscribe(lambda line, lvl, _st: self.listener.broadcast(
                {"type": "log", "line": line, "level": lvl}))

    # ── cloud bridge (Supabase command_queue) ────────────────────────────────
    def start_cloud_bridge(self):
        """Phase 2: listen to a Supabase table and run actions remotely.

        Runs on its own daemon thread inside CloudBridge, so the Qt frame and
        the Live session stay fully responsive. Actions get the live UI + a
        speak() hook so remote commands behave like local ones.
        """
        self.cloud = get_bridge()
        self.cloud.player = self.ui
        self.cloud.speak = self.speak
        self.cloud.start()

    def _on_remote_command(self, text: str, reply):
        print(f"[Remote] 📱 Command: {text}")
        self.ui.write_log(f"You: {text}  (remote)")
        with self._reply_lock:
            self._pending_replies.append(reply)
        self._on_text_command(text)

    def _broadcast(self, payload: dict):
        if self.listener:
            self.listener.broadcast(payload)

    def _set_state(self, state: str):
        self.ui.set_state(state)
        self._broadcast({"type": "state", "state": state})

    # ── text / speech plumbing ───────────────────────────────────────────────
    def _on_text_command(self, text: str):
        if not self._loop or not self.session:
            return
        asyncio.run_coroutine_threadsafe(
            self.session.send_client_content(
                turns={"parts": [{"text": text}]}, turn_complete=True),
            self._loop)

    def _is_idle(self) -> bool:
        """True when it is polite to speak unprompted: not muted, not talking."""
        if self.ui.muted:
            return False
        with self._speaking_lock:
            return not self._is_speaking

    def set_speaking(self, value: bool):
        with self._speaking_lock:
            self._is_speaking = value
        if value:
            self._set_state("SPEAKING")
        elif not self.ui.muted:
            self._set_state("LISTENING")

    def speak(self, text: str):
        self._on_text_command(text)

    def speak_error(self, tool_name: str, error: str):
        short = str(error)[:120]
        self.ui.write_log(f"ERR: {tool_name} — {short}")
        self.speak(f"Sir, {tool_name} encountered an error. {short}")

    # ── session config ───────────────────────────────────────────────────────
    def _build_config(self) -> types.LiveConnectConfig:
        from datetime import datetime

        mem_str    = format_memory_for_prompt(load_memory())
        sys_prompt = _load_system_prompt()
        time_str   = datetime.now().strftime("%A, %B %d, %Y — %I:%M %p")
        time_ctx   = (f"[CURRENT DATE & TIME]\nRight now it is: {time_str}\n"
                      f"Use this to calculate exact times for reminders.\n\n")

        parts = [VOICE_STYLE_DIRECTIVE.replace("{user_name}", _get_user_name()),
                 time_ctx]
        if mem_str:
            parts.append(mem_str)
        parts.append(sys_prompt)

        return types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            output_audio_transcription={},
            input_audio_transcription={},
            system_instruction="\n".join(parts),
            tools=[{"function_declarations": TOOL_DECLARATIONS}],
            session_resumption=types.SessionResumptionConfig(),
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=_get_voice_name()))),
        )

    # ── tool execution (pipeline-backed) ─────────────────────────────────────
    async def _execute_tool(self, fc) -> types.FunctionResponse:
        name = fc.name
        args = dict(fc.args or {})
        print(f"[FLINT] 🔧 {name}  {args}")
        self._set_state("THINKING")

        # save_memory is trivial — run inline, no worker round-trip
        if name == "save_memory":
            category = args.get("category", "notes")
            key, value = args.get("key", ""), args.get("value", "")
            if key and value:
                update_memory({category: {key: {"value": value}}})
                print(f"[Memory] 💾 save_memory: {category}/{key} = {value}")
            if not self.ui.muted:
                self._set_state("LISTENING")
            return types.FunctionResponse(
                id=fc.id, name=name, response={"result": "ok", "silent": True})

        if name == "shutdown_flint":
            self.ui.write_log("SYS: Shutdown requested.")
            self.speak("Goodbye, sir.")

            def _shutdown():
                import time, os
                time.sleep(1)
                os._exit(0)

            threading.Thread(target=_shutdown, daemon=True).start()
            return types.FunctionResponse(
                id=fc.id, name=name, response={"result": "Shutting down."})

        handler = self._handlers.get(name)
        if handler is None:
            result = f"Unknown tool: {name}"
        else:
            # off to a worker thread — the session loop and Qt frame stay live
            job = self.pipeline.submit(f"tool:{name}", handler, args,
                                       priority=Priority.HIGH)
            try:
                result = await asyncio.wrap_future(job.future) or "Done."
            except Exception as e:
                result = f"Tool '{name}' failed: {e}"
                traceback.print_exc()
                self.speak_error(name, e)

        if not self.ui.muted:
            self._set_state("LISTENING")
        print(f"[FLINT] 📤 {name} → {str(result)[:80]}")
        return types.FunctionResponse(
            id=fc.id, name=name, response={"result": result})

    # ── audio loops ──────────────────────────────────────────────────────────
    async def _send_realtime(self):
        while True:
            msg = await self.out_queue.get()
            await self.session.send_realtime_input(media=msg)

    def _enqueue_audio(self, msg: dict):
        """Runs on the event-loop thread via call_soon_threadsafe.

        Live audio must never block or raise: if the uplink consumer stalls
        and the queue fills, evict the oldest block so the stream always
        carries the freshest audio with bounded latency.
        """
        q = self.out_queue
        if q.full():
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                pass
            self._audio_drops += 1
            if self._audio_drops % 50 == 1:
                print(f"[FLINT] ⚠️ Audio uplink congested — dropped "
                      f"{self._audio_drops} stale mic blocks so far")
        q.put_nowait(msg)

    async def _listen_audio(self):
        print("[FLINT] 🎤 Mic started")
        loop = asyncio.get_event_loop()

        def callback(indata, frames, time_info, status):
            with self._speaking_lock:
                flint_speaking = self._is_speaking
            if not flint_speaking and not self.ui.muted:
                loop.call_soon_threadsafe(
                    self._enqueue_audio,
                    {"data": indata.tobytes(), "mime_type": "audio/pcm"})

        with sd.InputStream(
            samplerate=SEND_SAMPLE_RATE, channels=CHANNELS, dtype="int16",
            blocksize=CHUNK_SIZE, callback=callback,
        ):
            print("[FLINT] 🎤 Mic stream open")
            while True:
                await asyncio.sleep(0.1)

    async def _receive_audio(self):
        print("[FLINT] 👂 Recv started")
        out_buf, in_buf = [], []

        while True:
            async for response in self.session.receive():
                if response.data:
                    self.audio_in_queue.put_nowait(response.data)

                if response.server_content:
                    sc = response.server_content
                    if sc.output_transcription and sc.output_transcription.text:
                        self.set_speaking(True)
                        txt = sc.output_transcription.text.strip()
                        if txt:
                            out_buf.append(txt)
                    if sc.input_transcription and sc.input_transcription.text:
                        txt = sc.input_transcription.text.strip()
                        if txt:
                            in_buf.append(txt)

                    if sc.turn_complete:
                        self.set_speaking(False)
                        full_in = " ".join(in_buf).strip()
                        in_buf = []
                        if full_in:
                            self.ui.write_log(f"You: {full_in}")

                        full_out = " ".join(out_buf).strip()
                        out_buf = []
                        if full_out:
                            self.ui.write_log(f"Flint: {full_out}")
                            self._broadcast({"type": "transcript",
                                             "role": "flint", "text": full_out})
                            with self._reply_lock:
                                pending = self._pending_replies
                                self._pending_replies = []
                            for reply in pending:
                                try:
                                    reply(full_out)
                                except Exception:
                                    pass

                        if full_in and len(full_in) > 5:
                            threading.Thread(
                                target=_update_memory_async,
                                args=(full_in, full_out), daemon=True).start()

                if response.tool_call:
                    fn_responses = []
                    for fc in response.tool_call.function_calls:
                        print(f"[FLINT] 📞 {fc.name}")
                        fn_responses.append(await self._execute_tool(fc))
                    await self.session.send_tool_response(
                        function_responses=fn_responses)

    async def _play_audio(self):
        print("[FLINT] 🔊 Play started")
        stream = sd.RawOutputStream(
            samplerate=RECEIVE_SAMPLE_RATE, channels=CHANNELS,
            dtype="int16", blocksize=CHUNK_SIZE)
        stream.start()
        try:
            while True:
                chunk = await self.audio_in_queue.get()
                self.set_speaking(True)
                await asyncio.to_thread(stream.write, chunk)
        finally:
            self.set_speaking(False)
            stream.stop()
            stream.close()

    # ── main loop ────────────────────────────────────────────────────────────
    async def run(self):
        client = genai.Client(
            api_key=_get_api_key(),
            http_options={"api_version": "v1beta"})

        while True:
            try:
                print("[FLINT] 🔌 Connecting...")
                self._set_state("THINKING")
                config = self._build_config()

                async with (
                    client.aio.live.connect(model=LIVE_MODEL, config=config) as session,
                    asyncio.TaskGroup() as tg,
                ):
                    self.session        = session
                    self._loop          = asyncio.get_event_loop()
                    self.audio_in_queue = asyncio.Queue()
                    # ~2s of mic audio (32 × 64ms blocks); beyond that,
                    # _enqueue_audio drops the oldest block instead of raising
                    self.out_queue      = asyncio.Queue(maxsize=32)
                    self._audio_drops   = 0

                    print("[FLINT] ✅ Connected.")
                    self._set_state("LISTENING")
                    self.ui.write_log("SYS: FLINT online.")

                    tg.create_task(self._send_realtime())
                    tg.create_task(self._listen_audio())
                    tg.create_task(self._receive_audio())
                    tg.create_task(self._play_audio())
                    tg.create_task(self.proactive.loop())

            except Exception as e:
                print(f"[FLINT] ⚠️ {e}")
                traceback.print_exc()

            self.set_speaking(False)
            self._set_state("THINKING")
            print("[FLINT] 🔄 Reconnecting in 3s...")
            await asyncio.sleep(3)


def main():
    ui = FlintUI("face.png")
    ui.attach_pipeline(get_pipeline())

    def runner():
        ui.wait_for_api_key()
        flint = FlintLive(ui)
        flint.start_remote_listener()
        flint.start_cloud_bridge()
        try:
            asyncio.run(flint.run())
        except KeyboardInterrupt:
            print("\n🔴 Shutting down...")

    threading.Thread(target=runner, daemon=True, name="FlintLiveThread").start()
    ui.root.mainloop()


if __name__ == "__main__":
    main()
