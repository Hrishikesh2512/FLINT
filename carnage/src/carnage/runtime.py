"""Assembling one Carnage: the stores, the seam, the skills, the hub.

The order matters in one place only, and it is worth saying out loud: the
stores are built before anything that reads them, and the sync engine is built
over the *same objects* the tools hold. Building a second `MemoryStore` on the
same path for the sync engine would appear to work and would be wrong — two
instances mean two locks, and the file gets interleaved writes under exactly
the conditions sync creates.
"""

from __future__ import annotations

import logging

from flint_core.capabilities import ANY_PLATFORM
from flint_core.memory import MemoryStore
from flint_core.outcomes import OutcomeLog
from flint_core.persona import render_persona
from flint_core.projects import ProjectStore
from flint_core.recall import Archive
from flint_core.relay import RelayStore, carry_out
from flint_core.roster import build_roster
from flint_core.stores import ConnectionStore, ListStore, NoteStore, ReminderStore
from flint_core.sync import build_engine
from flint_core.syncws import SyncServer

from carnage.capabilities import build_capabilities
from carnage.config import CarnageConfig
from carnage.platform import detect

log = logging.getLogger("carnage.runtime")

#: How this body describes itself in the persona. One sentence, and the only
#: thing about her that differs from the Pi.
BODY = "phone"


class Carnage:
    """One assistant, on this phone — and the hub her other bodies sync to."""

    def __init__(self, config: CarnageConfig, phone=None, bridge=None,
                 search=None):
        self.config = config
        self.phone = phone if phone is not None else detect(bridge)
        state = config.state_dir
        state.mkdir(parents=True, exist_ok=True)

        # The shared mind. Identical types to Venom's, on this device's disk.
        self.memory = MemoryStore(state / "memory.json")
        self.archive = Archive(state / "archive.db")
        self.projects = ProjectStore(state / "projects.json")
        self.outcomes = OutcomeLog(state / "outcomes.jsonl")
        self.notes = NoteStore(state / "notes.json")
        self.lists = ListStore(state / "lists.json")
        self.reminders = ReminderStore(state / "reminders.json")
        self.connections = ConnectionStore(state / "connections.json")

        # Who else she is. Presence is filled in by the hub as devices sync.
        self.roster = build_roster(config.device, config.devices,
                                   path=state / "roster.json")
        # Work handed between bodies, riding the same connection as sync.
        self.relay = RelayStore(state / "relay.json")

        self.sync = build_engine(
            config.device,
            archive=self.archive, projects=self.projects,
            memory=self.memory, outcomes=self.outcomes,
            notes=self.notes, connections=self.connections,
            state_path=state / "sync.json")

        self.capabilities = build_capabilities(
            config, phone=self.phone, memory=self.memory,
            projects=self.projects, outcomes=self.outcomes,
            archive=self.archive, notes=self.notes, lists=self.lists,
            reminders=self.reminders, connections=self.connections,
            relay=self.relay, roster=self.roster,
            search=search or _search_for(config))
        # Built from the capability set rather than assembled by hand, so each
        # tool inherits its capability's permissions instead of silently
        # carrying none — the failure mode that turns a permission check into
        # an audit log that always says yes.
        self.registry = self.capabilities.build_registry(platform=ANY_PLATFORM)
        self.capabilities.log_summary()

        # Built on first use: a device with no key still runs as a hub and a
        # set of tools, it just cannot hold a conversation.
        self._gateway = _gateway_for(config)
        self._conversation = None

        # The page the phone installs, when one is wanted. Off by default —
        # a headless hub on a laptop has nobody to show it to.
        self.web = None
        if config.web.enabled:
            from carnage.web import CarnageWeb

            self.web = CarnageWeb(self, token=config.web.token or
                                  config.hub.token,
                                  host=config.web.host, port=config.web.port)

        self.server: SyncServer | None = None
        if config.hub.enabled:
            self.server = SyncServer(
                self.sync, host=config.hub.host, port=config.hub.port,
                token=config.hub.token, peers=config.hub.peers,
                on_exchange=self._note_exchange,
                relay=self.relay, roster=self.roster)

    # ── the prompt ──────────────────────────────────────────────────────────
    def system_instruction(self) -> str:
        """Who she is, what this body can do, and where her other bodies are.

        The persona is the shared one, byte for byte — only the body sentence
        differs from the Pi's. Everything after it is what makes *this* device
        different, and the roster block is what stops her talking about her
        other bodies as if they were somebody else.
        """
        parts = [render_persona(self.config.user_name or "he", BODY)]
        skills = self.capabilities.render_prompt(
            user_name=self.config.user_name or "he")
        if skills:
            parts.append(skills + "\n")
        others = self.roster.render_for_prompt()
        if others:
            parts.append(others)
        remembered = self.memory.render_for_prompt()
        if remembered:
            parts.append(remembered)
        lessons = self.outcomes.render_for_prompt()
        if lessons:
            parts.append(lessons)
        return "\n".join(parts)

    # ── the hub ─────────────────────────────────────────────────────────────
    def _note_exchange(self, peer: str, result) -> None:
        log.info("sync: %s — %s", peer, result.summary())
        # A conflict means one device's edit was discarded. Recording it as an
        # outcome puts it where "why did you do that?" can find it, instead of
        # only in a log nobody reads on a phone.
        for conflict in result.conflicts:
            self.outcomes.record_decision(
                "which edit wins", conflict.kept,
                f"it was the later edit of {conflict.store}/{conflict.key}",
                alternatives=(conflict.discarded,))

    # ── talking to her ──────────────────────────────────────────────────────
    def answer(self, said: str) -> str:
        """One turn of conversation. Never raises.

        Built lazily and kept, so the last few turns survive between requests —
        a page that reloads should not make her forget the sentence before.
        """
        if self._conversation is None:
            if self._gateway is None:
                return ("I've no key to think with yet — put a Gemini API key "
                        "in carnage.json and I'll be able to answer properly.")
            from carnage.conversation import Conversation

            self._conversation = Conversation(
                self._gateway, self.registry, self.system_instruction)
        return self._conversation.ask(said)

    def run_relayed(self, run) -> list:
        """Carry out whatever her other bodies have asked this one to do.

        `run` takes the request text and returns what to say back — normally a
        conversation turn, so this body answers with its own judgement and its
        own tools rather than being remote-controlled.
        """
        return carry_out(self.relay, self.config.device, run)

    async def start(self) -> None:
        if self.server is not None and not await self.server.start():
            log.warning("carnage is running without device sync")
        if self.web is not None and not self.web.start():
            log.warning("carnage is running without the page")

    async def stop(self) -> None:
        if self.server is not None:
            await self.server.stop()
        if self.web is not None:
            self.web.stop()

    def describe(self) -> str:
        active = ", ".join(c.name for c in self.capabilities.active())
        return (f"{self.config.device} on {self.phone.name}: "
                f"{len(list(self.registry))} tools — {active}")


def _gateway_for(config: CarnageConfig):
    """The shared LLM gateway, when there is a key for one."""
    if not config.gemini_api_key:
        return None
    try:
        from flint_core.config import FlintSettings, build_gateway

        return build_gateway(FlintSettings(gemini_api_key=config.gemini_api_key))
    except Exception:                       # noqa: BLE001
        log.exception("could not build an LLM gateway — she cannot converse")
        return None


def _search_for(config: CarnageConfig):
    """Grounded web search, if there is a key for it.

    Returns None without one, which switches the whole `core` capability off
    rather than registering a `web_search` that fails on every call.
    """
    if not config.gemini_api_key:
        return None

    def search(query: str) -> str:
        from flint_core.llm.providers import GeminiProvider

        return GeminiProvider(config.gemini_api_key).grounded_search(query)

    return search
