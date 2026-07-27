"""The voice loop: sleep-listening for the wake word, then a live session.

    ┌────────────┐  wake word   ┌──────────────┐  silence / goodbye
    │ WAKE       │ ───────────► │ CONVERSATION │ ─────────────────┐
    │ (oww ~3%   │   (chime)    │ (Gemini Live │                  │
    │  CPU)      │ ◄─────────── │  + tools)    │ ◄────────────────┘
    └────────────┘              └──────────────┘

Reliability model (learned on real hardware): the Bluetooth headset can
drop at any moment and never stores its bond; audio streams die silently
when the device vanishes. So the orchestrator wraps one full lifecycle —
connect headset → pin mic profile → open streams → chime → listen — and
any starvation or error tears the whole thing down and starts the cycle
again. Timers keep working across all of it.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time

from flint_core.memory import MemoryStore
from venom.audio.devices import current_devices
from venom.audio.streams import MicStream, SpeakerStream, chime
from venom.config import VenomConfig
from venom.live import LiveSession
from venom.tools_pi import TimerBoard, build_pi_registry
from venom.wake import WakeWordDetector

log = logging.getLogger("venom.voice")

# Wake-phase frame starvation: mic callbacks deliver ~15 frames/s; this many
# consecutive empty seconds means the stream is dead or the headset is gone.
STARVATION_SECONDS = 12
# Frames that keep arriving but are all exactly zero: the capture path was
# silently rerouted (observed live: headset drops mid-lifecycle and PipeWire
# falls back to the built-in jack's sink monitor — Venom sits deaf forever).
SILENCE_REBUILD_SECONDS = 20
# The wake button toggles (wake ⇄ end conversation). A second press this soon
# after waking is almost always impatience during the connect gap, not a
# request to sleep — observed live on a cold (no pre-warm) start: press,
# two silent seconds, press again, conversation killed before her first word.
WAKE_TOGGLE_GRACE_SECONDS = 6.0


class StreamsDied(Exception):
    """Audio stopped flowing — rebuild the whole audio lifecycle."""


class FocusRequested(Exception):
    """External Bluetooth audio started mid-wake — drop the pre-warmed
    session (radio quiet) and re-enter the loop via the focus path."""


class SilenceTracker:
    """Detects a dead capture path: a real microphone always carries a noise
    floor, so sustained bit-exact silence means nobody is actually listening."""

    def __init__(self, limit_seconds: float = SILENCE_REBUILD_SECONDS,
                 sample_rate: int = 16000):
        self._limit = limit_seconds
        self._rate = sample_rate
        self._silent = 0.0

    def update(self, frame: bytes) -> bool:
        """Feed one mic frame; True when the silence limit is crossed."""
        if any(frame):
            self._silent = 0.0
        else:
            self._silent += len(frame) / 2 / self._rate
        return self._silent >= self._limit


class VoiceOrchestrator:
    def __init__(self, config: VenomConfig, activity=None):
        from collections import deque

        self.config = config
        # FIXED (Fix 2): shared VoiceActivity flag (from the supervisor). While
        # a conversation is live we flip session_active True so the brain
        # switcher won't probe or flap mid-sentence. Optional so the voice loop
        # still runs standalone (tests, dev) with a private stand-in.
        if activity is None:
            from venom.supervisor import VoiceActivity

            activity = VoiceActivity()
        self.activity = activity
        # Consecutive pre-warm failures, for a gentle retry backoff (see
        # _wait_for_wake): a one-off blip recovers fast, a persistent outage
        # backs off instead of hammering the socket every 2s.
        self._prewarm_fails = 0
        self.state = "starting"
        # Web console: prompts in, transcript out (thread-safe via event loop).
        self.inbox: asyncio.Queue[str] = asyncio.Queue()
        self.transcript = deque(maxlen=60)
        self.memory = MemoryStore(config.memory_path)
        self.timers = TimerBoard()
        # Persistent productivity stores live beside memory in the state dir.
        from venom.stores import (ConnectionStore, ConversationLog,
                                  FavouritesStore, ListStore, NoteStore,
                                  ReminderStore)

        state_dir = config.memory_path.parent
        self.reminders = ReminderStore(state_dir / "reminders.json")
        self.notes = NoteStore(state_dir / "notes.json")
        self.lists = ListStore(state_dir / "lists.json")
        # Favourite songs + their offline copies — for long, signal-less rides.
        self.favourites = FavouritesStore(state_dir / "favourites.json")
        # People she knows — numbers, nicknames, socials, interests — used to
        # contact them and to recall who they are.
        self.connections = ConnectionStore(state_dir / "connections.json")
        from venom.session import SessionState

        self.session = SessionState(state_dir / "session.json")
        # What you two actually said, across sessions and reboots — rendered
        # into every new session's prompt so she picks up where you left off.
        self.convlog = ConversationLog(state_dir / "conversations.json")
        # Reminders that fired while asleep, awaiting spoken announcement.
        self.pending_reminders: list[str] = []
        # Approximate location (network geo) — warm the cache off-thread so the
        # first conversation has it without blocking on the lookup.
        from venom.location import LocationProvider

        self.location = LocationProvider()
        self.location.warm()
        from venom.music import MusicPlayer

        self.music = MusicPlayer(favourites=self.favourites,
                                 offline_dir=state_dir / "music")
        from venom.chess_game import ChessGame

        self.chess = ChessGame()
        from venom.notifications import NotificationHub

        # Incoming WhatsApp is delivered locally by the bridge over loopback
        # (no public ntfy round-trip). Enabled whenever WhatsApp is.
        self.notifications = NotificationHub(
            is_dnd=lambda: self._dnd, on_arrival=self._note_whatsapp,
            enabled=config.whatsapp.enabled)
        # Senders of WhatsApp messages that arrived while idle, awaiting a
        # proactive spoken announcement. Filled from the notification thread,
        # drained by the wake loop — hence the lock.
        self._pending_announcements: list[str] = []
        self._ann_lock = threading.Lock()
        self._opening_announcement: str | None = None
        # Ambient awareness: the loop that lets her open a conversation on her
        # own (see venom/ambient.py). Built in run(), where there's a loop.
        self.ambient = None
        self._ambient_task: asyncio.Task | None = None
        # Bluetooth receive: laptop/phone audio into the earphone. A process-
        # wide singleton — orchestrators are rebuilt after crashes, and two
        # receivers would bridge every stream twice (doubled audio).
        self.btreceiver = None
        if config.audio.receiver:
            from venom.audio.receiver import shared_receiver

            self.btreceiver = shared_receiver(
                headset_mac=config.audio.bluetooth_mac,
                headset_name=config.audio.bluetooth_name,
                repin=self._repin_defaults)
        # Calendar (secret iCal URL): agenda tools + proactive lead-time
        # chimes through the same pending-reminder path reminders use.
        self.calwatch = None
        if config.calendar.ready:
            from venom.gcal import CalendarFeed, CalendarWatcher

            self.calwatch = CalendarWatcher(
                CalendarFeed(config.calendar.ical_url),
                lead_minutes=config.calendar.lead_minutes,
                refresh_minutes=config.calendar.refresh_minutes)
        # Gmail over IMAP (app password), strictly read-only.
        self.mailbox = None
        if config.mail.ready:
            from venom.gmail import Mailbox

            self.mailbox = Mailbox(config.mail)
        # Self-hosted WhatsApp send, via the Baileys bridge on localhost.
        self.whatsapp = None
        if config.whatsapp.ready:
            from venom.whatsapp import WhatsAppClient

            self.whatsapp = WhatsAppClient(config.whatsapp)
        # Emergency SOS: its own contact book, alerted over the same WhatsApp
        # bridge. Built whenever WhatsApp is, so contacts can be set up (and
        # the drill run) long before anyone needs it.
        self.sos = None
        if self.whatsapp is not None:
            from venom.sos import build_sos

            self.sos = build_sos(state_dir, self.whatsapp,
                                 location=self.location,
                                 connections=self.connections,
                                 user_name=config.voice.user_name)
        # Tuya / Smart Life smart bulbs, driven locally over the LAN.
        self.lights = None
        if config.lights.ready:
            from venom.lights import LightsController

            self.lights = LightsController(config.lights.registry_path)
        self.registry = build_pi_registry(config, self.memory, self.timers,
                                          music=self.music,
                                          reminders=self.reminders,
                                          notes=self.notes, lists=self.lists,
                                          location=self.location,
                                          chess=self.chess,
                                          notifications=self.notifications,
                                          receiver=self.btreceiver,
                                          calendar=self.calwatch,
                                          mailbox=self.mailbox,
                                          whatsapp=self.whatsapp,
                                          connections=self.connections,
                                          lights=self.lights,
                                          sos=self.sos)
        self._detector: WakeWordDetector | None = None
        # True while we've paused our own music for a live conversation, so we
        # only resume what *we* paused (not a track the user paused by hand).
        self._music_ducked = False
        # Physical buttons (set from the event loop, read by the wake loop):
        #   _manual_wake — the headset button asks to start a conversation.
        #   _dnd         — Do-Not-Disturb: ignore wake word + headset button and
        #                  hold proactive timer/reminder chimes until toggled off.
        #   _speaker     — the current lifecycle's speaker, so button handlers
        #                  can chime; None between lifecycles.
        self._manual_wake = asyncio.Event()
        self._dnd = False
        self._speaker: SpeakerStream | None = None
        # The live conversation, set while one is active — so a wake-button
        # press mid-reply becomes a barge-in (interrupt) instead of a queued
        # wake. None between conversations.
        self._session: LiveSession | None = None
        # When the current conversation began — presses inside the grace
        # window never end it (see WAKE_TOGGLE_GRACE_SECONDS).
        self._conversation_started = 0.0

    async def run(self) -> None:
        # The wake model takes minutes to load from slow flash — load it in
        # parallel with the (equally slow) first headset hunt. It loads once
        # and survives audio lifecycle rebuilds.
        self._detector = WakeWordDetector(self.config.voice.wake_word,
                                          self.config.voice.wake_threshold)
        self._detector_ready = asyncio.create_task(
            asyncio.to_thread(self._detector.load))

        from venom.buttons import watch_buttons

        # Keep a reference: asyncio only holds tasks weakly, and a
        # garbage-collected watcher means the buttons silently die.
        self._buttons_task = asyncio.create_task(watch_buttons(
            on_wake=self._on_wake_button,
            on_dnd=self._on_dnd_button,
            dnd_code=self.config.buttons.dnd_code,
            wake_code=self.config.buttons.wake_code))

        # Phone notifications (WhatsApp): chime on arrival, read on demand.
        self.notifications.start()

        # Bluetooth receive: bridge laptop/phone audio into the earphone.
        if self.btreceiver is not None:
            self.btreceiver.start()

        # Calendar feed refresh + proactive event alerts.
        if self.calwatch is not None:
            self.calwatch.start()

        # Ambient awareness — she watches the world and speaks first when it's
        # worth it. Keep a reference (asyncio holds tasks weakly).
        self._ambient_task = self._start_ambient()

        first_cycle = True
        died_streak = 0
        while True:
            started = asyncio.get_event_loop().time()
            try:
                await self._audio_lifecycle(first_cycle)
            except StreamsDied as exc:
                log.warning("audio lifecycle ended: %s — rebuilding", exc)
                # A lifecycle that survived a while, then died, is the normal
                # headset-drop dance. One that dies within seconds, over and
                # over, means the audio path is gone (earphone unplugged /
                # fried) and Venom is deaf AND mute — the one failure it
                # cannot announce through the headset. Tell the phone instead.
                ran_for = asyncio.get_event_loop().time() - started
                died_streak = died_streak + 1 if ran_for < 120 else 1
                if died_streak == 5:
                    self._alert_audio_dead(str(exc))
            except Exception:
                log.exception("audio lifecycle crashed — rebuilding")
            first_cycle = False
            self.state = "reconnecting"
            await asyncio.sleep(3)

    def _repin_defaults(self) -> None:
        """Re-assert PipeWire defaults on Venom's own headset. Called by the
        Bluetooth receiver the moment an external device starts streaming,
        so a connecting laptop can never disturb the Pi's audio path.
        Blocking (subprocess) — runs on the receiver's thread, not the loop."""
        if self.config.audio.use_bluetooth:
            from venom.audio.routing import pin_bluetooth_audio

            pin_bluetooth_audio(mac=self.config.audio.bluetooth_mac)
        else:
            from venom.audio.routing import pin_usb_audio

            pin_usb_audio()

    def _alert_audio_dead(self, reason: str) -> None:
        """Push a one-time ntfy alert to the phone: the audio path is dead and
        the user would otherwise only notice Venom by her silence."""
        topic = self.config.phone.ntfy_topic
        if not topic:
            return
        from venom.phone import push_alert

        asyncio.get_event_loop().run_in_executor(
            None, push_alert, self.config.phone.ntfy_server, topic,
            f"Audio keeps failing ({reason}). Check the earphone — unplug "
            f"and replug it, or reboot me.", "Venom lost its voice")

    # ── one full audio lifecycle: headset → streams → listen loop ────────────
    async def _audio_lifecycle(self, first_cycle: bool) -> None:
        loop = asyncio.get_running_loop()

        if self.config.audio.use_bluetooth:
            from venom.audio.routing import pin_bluetooth_audio
            from venom.btaudio import BluetoothHeadset

            self.state = "connecting bluetooth headset"
            headset = BluetoothHeadset(self.config.audio.bluetooth_mac,
                                       self.config.audio.bluetooth_name)
            while not await asyncio.to_thread(headset.wait_for_connection):
                log.warning("headset not connected — put it in pairing mode; retrying")
                self.state = "waiting for headset (pairing mode)"
                await asyncio.sleep(10)

            # The mic only exists in the HFP profile — pin it every connect.
            # A lifecycle without a microphone is useless (the wake loop would
            # sit deaf on the sink monitor), so failure here restarts the cycle.
            self.state = "activating headset microphone"
            if not await asyncio.to_thread(pin_bluetooth_audio, 3.0, 6,
                                           self.config.audio.bluetooth_mac):
                raise StreamsDied("headset connected but no microphone appeared")
        else:
            # USB (or default) path: make the USB earphone PipeWire's default
            # so the resampling 'pipewire' device routes to it — re-asserted
            # every lifecycle, so a reconnecting Bluetooth device can't keep it.
            from venom.audio.routing import pin_usb_audio

            self.state = "selecting usb audio"
            # Wait for the earphone to actually be present (a USB mic source)
            # before opening streams. On an unplug/replug the USB node
            # re-enumerates; opening against the vanished device — or whatever
            # default PipeWire fell back to — left Venom deaf until a reboot.
            # Looping here means a replug reconnects on its own, in-session.
            waited = 0
            while not await asyncio.to_thread(pin_usb_audio):
                if waited == 0:
                    log.warning("USB earphone not present — waiting for a (re)connect")
                self.state = "waiting for usb earphone"
                if waited == 8:  # ~half a minute gone — nudge the phone once
                    self._alert_audio_dead("earphone disconnected")
                waited += 1
                await asyncio.sleep(3)
            if waited:
                log.info("USB earphone (re)connected — resuming")

        # Streams only make sense once the wake model can consume them.
        self.state = "loading wake model"
        try:
            await self._detector_ready
        except Exception as exc:
            # Awaiting a failed task re-raises forever — without a fresh load
            # task a one-off model-load hiccup would brick voice until the
            # process restarts. Re-arm the load and let the lifecycle retry.
            self._detector_ready = asyncio.create_task(
                asyncio.to_thread(self._detector.load))
            raise StreamsDied(f"wake model failed to load: {exc}") from exc

        pick = current_devices(bluetooth=self.config.audio.use_bluetooth)
        log.info("audio devices — mic: %s, speaker: %s", pick.input_name, pick.output_name)

        suppressor = None
        if self.config.audio.noise_suppression:
            from venom.audio.denoise import NoiseSuppressor

            suppressor = NoiseSuppressor()

        speaker = SpeakerStream(pick)
        mic = MicStream(pick, loop, suppressor=suppressor)
        speaker.start()
        mic.start()
        self._speaker = speaker  # let button handlers chime this lifecycle
        try:
            chime(speaker)  # audible on every (re)connect: "Venom hears you"
            while True:
                # Bluetooth focus: while the laptop/phone streams audio into
                # the earphone, the Pi's single radio belongs to that stream.
                # No pre-warmed session (Gemini idle-closes it every few
                # minutes and each re-warm is a Wi-Fi burst — audible as a
                # stutter in the music) and no wake word. The wake button or
                # a console prompt breaks in (cold session, ~4s to first
                # reply); the stream ending resumes the normal cycle.
                cold_wake = False
                if self._bt_focused():
                    self.state = "bluetooth audio"
                    log.info("bluetooth focus: external audio streaming — "
                             "wake word off, no pre-warm (button breaks in)")
                    if not await self._focus_phase(mic, speaker):
                        continue  # stream ended — back to the pre-warm cycle
                    cold_wake = True
                # Pre-warm: open the Gemini Live session (socket + big-prompt
                # prefill, the ~4-5s cold cost) NOW, while we listen for the wake
                # word. It sits idle, off the mic, until we activate it — so the
                # first reply after "Hey Jarvis" is the warm ~1s path every time.
                session = self._build_session(mic, speaker)
                warm_task = asyncio.create_task(session.run())
                if not cold_wake:
                    self.state = "wake"
                    try:
                        if not await self._wait_for_wake(mic, speaker, warm_task):
                            continue  # warm session dropped — spin a new one
                    except FocusRequested:
                        continue  # loop top re-enters via the focus path
                self.state = "conversation"
                # Chime FIRST — the instant "I heard you" — before the slower
                # bookkeeping (pausing music can take a beat), so waking never
                # feels laggy even when the rest of the setup takes a moment.
                chime(speaker)
                chime(speaker, frequency=1320.0)
                # The music and the mic share one headset, so anything playing
                # bleeds into the mic — Gemini never hears a clean end-of-speech
                # and never replies. Pause our own player for the whole turn;
                # the finally below resumes it when we go back to sleep.
                self._duck_music()
                # Same physics for the laptop/phone streaming into the
                # earphone: AVRCP-pause it for the conversation (observed
                # live — with music bleeding in she never answered).
                if self.btreceiver is not None:
                    self.btreceiver.hold_streams()
                self._prepare_opening(session)
                session.activate()
                self._conversation_started = time.monotonic()
                self._session = session  # a wake press now barges in, not wakes
                # FIXED (Fix 2): conversation is now live and speaking — freeze
                # the brain switcher until we're back to wake/pre-warm.
                self.activity.session_active = True
                # Gate the mic to true silence between/after words *only* while
                # talking, so Gemini's own turn detector hears a clean end-of-
                # speech and replies promptly (a body-mic's noise floor otherwise
                # reads as "still talking" and it waits). Off during wake, where
                # pure zeros would look like a dead capture path (see
                # SilenceTracker) and trigger needless stream rebuilds.
                if suppressor is not None:
                    suppressor.gate = True
                try:
                    await warm_task
                except Exception:
                    log.exception("live session ended with error")
                    chime(speaker, frequency=330.0, duration=0.4)
                    await asyncio.sleep(2)
                finally:
                    # FIXED (Fix 2): conversation over — let the brain switcher
                    # resume evaluating in the gap before the next session.
                    self._session = None
                    self.activity.session_active = False
                    if suppressor is not None:
                        suppressor.gate = False
                    self._unduck_music()
                    if self.btreceiver is not None:
                        self.btreceiver.release_streams()
                    self._drain(mic)
                self._detector.reset()
                log.info("back to wake listening")
        finally:
            self._speaker = None
            mic.stop()
            speaker.stop()

    def _bt_focused(self) -> bool:
        """True while an external device (laptop/phone) is actively streaming
        Bluetooth audio into the earphone and focus mode is enabled. Keys off
        the incoming stream only — Venom's own music player is irrelevant."""
        return (self.btreceiver is not None
                and self.config.audio.receiver_focus
                and self.btreceiver.is_streaming)

    def _note_whatsapp(self, sender: str) -> None:
        """Notification-thread hook: a WhatsApp arrived. Queue the sender for a
        proactive spoken announcement (picked up by the idle wake loop). Held
        under DND — the hub already suppresses the chime then too."""
        if self._dnd:
            return
        with self._ann_lock:
            self._pending_announcements.append(sender or "Someone")

    def _take_announcement(self) -> str | None:
        """Drain queued WhatsApp senders into one opening instruction, or None.
        Bursts collapse into a single announcement so she doesn't chatter."""
        with self._ann_lock:
            if not self._pending_announcements:
                return None
            senders = self._pending_announcements[:]
            self._pending_announcements.clear()
        seen: list[str] = []
        for s in senders:
            if s not in seen:
                seen.append(s)
        if len(seen) == 1:
            who = seen[0]
        elif len(seen) == 2:
            who = f"{seen[0]} and {seen[1]}"
        else:
            who = f"{seen[0]}, {seen[1]} and {len(seen) - 2} others"
        one = len(senders) == 1
        return (
            f"[Proactive] The user just received "
            f"{'a WhatsApp message' if one else 'WhatsApp messages'} from {who}. "
            f"Open the conversation by telling them now, in ONE short warm "
            f"Hinglish sentence — e.g. 'Sir, {who} ne WhatsApp pe message kiya "
            f"hai' — and ask if they'd like it read out. Do NOT read the message "
            f"content yet; wait for them to say yes.")

    # ── ambient awareness (she speaks first) ─────────────────────────────────
    def _start_ambient(self) -> asyncio.Task | None:
        """Launch the ambient loop, if enabled. Additive: any failure here
        leaves a perfectly good reactive assistant."""
        if not self.config.ambient.enabled:
            return None
        try:
            from venom.ambient import AmbientLoop

            self.ambient = AmbientLoop(
                self.config,
                self.config.memory_path.parent / "ambient.json",
                speak=self.queue_proactive,
                is_busy=self._ambient_busy,
                session=self.session,
                calendar=self.calwatch,
                mailbox=self.mailbox,
                memory=self.memory,
                location=self.location,
                reminders=self.reminders)
        except Exception:
            log.exception("ambient loop failed to start — continuing without it")
            return None
        log.info("ambient awareness on (tick %.0fs, quiet %02d:00-%02d:00)",
                 self.config.ambient.tick_seconds,
                 self.config.ambient.quiet_start_hour,
                 self.config.ambient.quiet_end_hour)
        return asyncio.create_task(self.ambient.run())

    def shutdown(self) -> None:
        """Stop the background tasks this orchestrator owns.

        The voice loop rebuilds the orchestrator after a crash; an ambient
        loop left running on the discarded one would keep queueing nudges
        nobody reads — and burn their once-only keys doing it.
        """
        if self._ambient_task is not None:
            self._ambient_task.cancel()
            self._ambient_task = None

    def _ambient_busy(self) -> bool:
        """True whenever she must not open her mouth unprompted: DND, a live
        conversation, an already-queued opening, or someone else's audio
        streaming through the earphone."""
        return (self._dnd
                or self._session is not None
                or self._opening_announcement is not None
                or self._bt_focused())

    def queue_proactive(self, instruction: str) -> None:
        """Ambient loop -> wake loop: open the next session with this.

        Deliberately the same door the WhatsApp announcement uses, so every
        unprompted conversation Venom starts goes through one code path.
        """
        self._opening_announcement = instruction

    def _announce_due_alerts(self, speaker: SpeakerStream) -> None:
        """Chime for timers/reminders that fire while asleep. Do-Not-Disturb
        holds them: left unpopped so they announce the moment DND ends."""
        if self._dnd:
            return
        for timer in self.timers.pop_due():
            chime(speaker)
            chime(speaker, frequency=1100.0)
            self.timers.add(0, f"(already finished) {timer.label}")
            log.info("timer fired while asleep: %s", timer.label)
        for reminder in self.reminders.pop_due():
            chime(speaker)
            chime(speaker, frequency=880.0)
            self.pending_reminders.append(reminder["text"])
            log.info("reminder fired while asleep: %s", reminder["text"])
        if self.calwatch is not None:
            for alert in self.calwatch.pop_due():
                chime(speaker)
                chime(speaker, frequency=740.0)
                self.pending_reminders.append(f"Calendar: {alert}")
                log.info("calendar alert: %s", alert)

    async def _focus_phase(self, mic: MicStream, speaker: SpeakerStream) -> bool:
        """Hold radio-quiet while the laptop/phone streams audio. True → the
        user broke in (wake button or console prompt): converse NOW on a
        cold session. False → the stream ended: resume the pre-warm cycle.
        Timers/reminders still chime; stream health is still watched."""
        starved = 0.0
        silence = SilenceTracker()
        self._manual_wake.clear()
        while True:
            if not self._bt_focused():
                log.info("bluetooth focus: stream ended — back to wake listening")
                return False
            self._announce_due_alerts(speaker)
            if not self.inbox.empty():
                log.info("bluetooth focus: console prompt — waking (cold)")
                self._drain(mic)
                return True
            if self._manual_wake.is_set():
                self._manual_wake.clear()
                if not self._dnd:
                    log.info("bluetooth focus: wake button — waking (cold)")
                    self._drain(mic)
                    return True
            # The mic keeps running (its capture path must stay provably
            # alive), but nothing leaves the device — no wake model feed,
            # no uplink. Same starvation/dead-capture guards as wake.
            try:
                frame = await asyncio.wait_for(mic.frames.get(), timeout=1.0)
                starved = 0.0
            except TimeoutError:
                starved += 1.0
                if starved >= STARVATION_SECONDS:
                    raise StreamsDied(
                        f"no mic audio for {STARVATION_SECONDS}s (headset gone?)"
                    ) from None
                continue
            chunks = [frame]
            while True:
                try:
                    chunks.append(mic.frames.get_nowait())
                except asyncio.QueueEmpty:
                    break
            if silence.update(b"".join(chunks)):
                raise StreamsDied(
                    f"mic delivered pure digital silence for "
                    f"{SILENCE_REBUILD_SECONDS}s (capture path rerouted?)"
                )

    async def _wake_phase(self, mic: MicStream, speaker: SpeakerStream) -> None:
        starved = 0.0
        silence = SilenceTracker()
        self._manual_wake.clear()  # ignore any press queued from a past cycle
        while True:
            # A stream that starts mid-wake flips us into focus: leave the
            # wake loop so the caller re-enters via the focus path (and
            # cancels the pre-warmed session = radio quiet).
            if self._bt_focused():
                raise FocusRequested()
            self._announce_due_alerts(speaker)
            # A WhatsApp arrived while asleep — wake ourselves to announce who
            # messaged (unless DND, which holds it queued until DND lifts).
            if not self._dnd:
                ann = self._take_announcement()
                if ann is not None:
                    self._opening_announcement = ann
                    log.info("whatsapp arrived while asleep — announcing sender")
                    self._drain(mic)
                    return
                # The ambient loop decided something is worth saying — wake
                # ourselves and lead with it, exactly like a WhatsApp arrival.
                if self._opening_announcement is not None:
                    log.info("ambient nudge queued — waking to speak first")
                    self._drain(mic)
                    return
            if not self.inbox.empty():
                log.info("console prompt while asleep — starting a session")
                self._drain(mic)
                return  # the session's housekeeping delivers the prompt
            # Headset button: an explicit wake, checked every loop (frames arrive
            # ~15x/s) so it feels instant. Ignored under DND.
            if self._manual_wake.is_set():
                self._manual_wake.clear()
                if not self._dnd:
                    log.info("headset button — waking")
                    self._drain(mic)
                    return
            try:
                frame = await asyncio.wait_for(mic.frames.get(), timeout=1.0)
                starved = 0.0
            except TimeoutError:
                starved += 1.0
                if starved >= STARVATION_SECONDS:
                    raise StreamsDied(
                        f"no mic audio for {STARVATION_SECONDS}s (headset gone?)"
                    ) from None
                continue
            # Feed the whole backlog in one pass. One-frame-per-loop (each
            # with its own to_thread hop) drains slower than frames arrive
            # whenever the CPU is busy, so the wake check fell seconds behind
            # live audio — the classic "lagging wake word". Batching catches
            # up to real time every iteration; the silence tracker must see
            # the same batch, or it undercounts dead air by whatever the
            # backlog swallowed.
            chunks = [frame]
            while True:
                try:
                    chunks.append(mic.frames.get_nowait())
                except asyncio.QueueEmpty:
                    break
            batch = b"".join(chunks)
            if silence.update(batch):
                raise StreamsDied(
                    f"mic delivered pure digital silence for "
                    f"{SILENCE_REBUILD_SECONDS}s (capture path rerouted?)"
                )
            if not self._dnd:
                if await asyncio.to_thread(self._detector.feed, batch):
                    log.info("wake word detected")
                    self._drain(mic)
                    return

    # ── physical button handlers (called on the event loop) ──────────────────
    def _on_wake_button(self) -> None:
        """Wake button (headset or shutter-2) — a toggle: it wakes her when
        she's asleep, and ends the conversation (back to sleep) when one is
        already live. Ignored under DND."""
        if self._dnd:
            log.info("wake button ignored — DND is on")
            return
        session = self._session
        if session is not None and not session.ended:
            if (time.monotonic() - self._conversation_started
                    < WAKE_TOGGLE_GRACE_SECONDS):
                log.info("wake button — ignored (conversation just started; "
                         "she's still connecting/listening)")
                return
            log.info("wake button — ending conversation (sleep)")
            session.request_stop()
            return
        self._manual_wake.set()

    def _on_dnd_button(self) -> None:
        """Shutter button 1: toggle Do-Not-Disturb with a distinct two-tone
        chime — falling when going quiet, rising when coming back."""
        self._dnd = not self._dnd
        log.info("DND %s (shutter button)", "on" if self._dnd else "off")
        sp = self._speaker
        if sp is None:
            return
        if self._dnd:                       # entering: high → low (falling)
            chime(sp, frequency=587.0)
            chime(sp, frequency=440.0)
        else:                               # leaving: low → high (rising)
            chime(sp, frequency=440.0)
            chime(sp, frequency=587.0)

    def _duck_music(self) -> None:
        """Pause our own music while a conversation is live so the shared-headset
        mic hears you cleanly. The player tracks whose pause it was, so an
        explicit user pause during the conversation is never resumed over."""
        try:
            if self.music.duck():
                self._music_ducked = True
                log.info("paused music for the conversation")
        except Exception:
            log.exception("could not pause music for the conversation")

    def _unduck_music(self) -> None:
        """Resume music we paused for the conversation, back to sleep."""
        if not self._music_ducked:
            return
        self._music_ducked = False
        try:
            if self.music.unduck():
                log.info("resumed music")
            else:
                log.info("music left paused (user's explicit pause wins)")
        except Exception:
            log.exception("could not resume music after the conversation")

    def _build_session(self, mic: MicStream, speaker: SpeakerStream) -> LiveSession:
        """A pre-warmable session; the opening briefing is decided at wake."""
        return LiveSession(self.config, self.registry, self.memory,
                           self.timers, mic.frames, speaker,
                           inbox=self.inbox, transcript=self.transcript,
                           reminders=self.reminders,
                           pending_reminders=self.pending_reminders,
                           location=self.location, opening=None,
                           convlog=self.convlog)

    def _prepare_opening(self, session: LiveSession) -> None:
        """At the moment of waking, decide whether to lead with a briefing."""
        # A queued WhatsApp announcement takes priority over the morning brief:
        # she woke *because* of the message, so lead with who it's from.
        if self._opening_announcement:
            session._opening = self._opening_announcement
            self._opening_announcement = None
            self.session.mark_interaction()
            log.info("delivering whatsapp announcement")
            return
        if self.session.should_brief():
            from venom.tools_pi import build_briefing

            session._opening = build_briefing(self.memory, self.timers,
                                              location=self.location,
                                              reminders=self.reminders)
            self.session.mark_briefed()
            log.info("delivering morning briefing")
        else:
            self.session.mark_interaction()

    async def _wait_for_wake(self, mic: MicStream, speaker: SpeakerStream,
                             warm_task: asyncio.Task) -> bool:
        """Listen for the wake word while the session warms in the background.
        True → woken, go converse. False → the warm session died first; the
        caller loops to spin up a fresh one."""
        from venom.live import is_normal_closure

        wake_task = asyncio.create_task(self._wake_phase(mic, speaker))
        done, _pending = await asyncio.wait(
            {wake_task, warm_task}, return_when=asyncio.FIRST_COMPLETED)
        if wake_task in done:
            exc = wake_task.exception()
            if exc is not None:
                # The lifecycle is being torn down — the pre-warmed session
                # must die with it, or its open socket leaks into the next
                # cycle (one orphan per rebuild, forever, on a dead headset).
                warm_task.cancel()
                try:
                    await warm_task
                except BaseException:
                    pass
                raise exc  # StreamsDied → rebuild the whole audio lifecycle
            self._prewarm_fails = 0  # a warm session survived to wake — healthy
            return True
        # Warm session ended before the wake word (server idle-closed it, or it
        # failed to connect). Stop listening; the caller re-warms.
        wake_task.cancel()
        try:
            await wake_task
        except BaseException:
            pass
        exc = None if warm_task.cancelled() else warm_task.exception()
        if exc is not None and not is_normal_closure(exc):
            # Exponential backoff, capped: 0.5 → 1 → 2 → 4 → 5s. First retry is
            # 4x faster than the old fixed 2s (snappy recovery from a transient
            # blip); a sustained outage settles at 5s instead of hammering.
            delay = min(0.5 * 2 ** min(self._prewarm_fails, 4), 5.0)
            self._prewarm_fails += 1
            log.warning("pre-warm session failed: %s — retrying in %.1fs",
                        exc, delay)
            await asyncio.sleep(delay)
        return False

    @staticmethod
    def _drain(mic: MicStream) -> None:
        try:
            while True:
                mic.frames.get_nowait()
        except asyncio.QueueEmpty:
            pass


async def run_voice_forever(config: VenomConfig, set_state, activity=None) -> None:
    """Supervisor entry: keep the voice loop alive across crashes."""
    backoff = 2.0
    console = None
    if config.web_enabled:
        try:
            from venom.web import WebConsole

            console = WebConsole(config.web_port, token=config.web_token,
                                 bind=config.web_bind)
            console.start()
            if not config.web_token and config.web_bind != "127.0.0.1":
                log.warning("web console has NO token AND is not loopback-bound "
                            "— open to anyone who can reach %s:%d",
                            config.web_bind, config.web_port)
        except Exception:
            log.exception("web console failed to start — continuing without it")
    while True:
        # FIXED (Fix 2): pass the shared activity flag through to every
        # orchestrator instance so brain-switch gating survives voice restarts.
        orchestrator = VoiceOrchestrator(config, activity)
        if console is not None:
            console.attach(orchestrator, asyncio.get_event_loop())
        try:
            set_state("voice: starting")
            started = asyncio.get_event_loop().time()
            task = asyncio.create_task(orchestrator.run())
            while not task.done():
                set_state(f"voice: {orchestrator.state}")
                await asyncio.sleep(1)
            await task
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("voice loop crashed: %s", exc)
            ran_for = asyncio.get_event_loop().time() - started
            backoff = 2.0 if ran_for > 60 else min(backoff * 2, 60.0)
            set_state("voice: restarting")
            await asyncio.sleep(backoff)
        finally:
            orchestrator.shutdown()
