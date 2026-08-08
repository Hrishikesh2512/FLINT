"""Capabilities — a skill's tools and its instructions, shipped together.

The problem this solves is specific and was measured, not guessed. Venom's
persona had grown to ~270 lines because every new skill added a paragraph
telling the model how to use it: MUSIC CONTROL, CHESS, EMERGENCY, LAPTOP
CONTROL, BLUETOOTH HEADSET MODE. Those paragraphs were written in one file and
the matching tools registered in another, so the two drifted; worse, *all* of
them shipped on every session, whether or not the skill existed on that
device. A Pi with no TV configured still carried TV instructions. A Pi with no
chess engine still carried three sentences about algebraic notation.

That is a hard ceiling on how many skills an assistant can have: every one
costs prompt tokens on every session, and irrelevant instructions measurably
degrade the ones that matter.

A Capability fixes it by making the pairing structural:

    Capability(
        name="music",
        summary="Play, pause, skip and favourite songs in the earphone.",
        prompt="MUSIC CONTROL: to play, pause, ...",
        register=lambda reg: register_music_tools(reg, player),
        available=player is not None,
    )

`available=False` and neither the tools nor the prompt exist — there is no way
to ship one without the other, and no way to ship either on a device that
cannot use it. Adding a skill becomes one Capability rather than an edit to
the registry, an edit to the persona, and a note to keep them in step.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass

from flint_core.tools.registry import ANY_PLATFORM, ToolRegistry

log = logging.getLogger("flint.capabilities")


@dataclass(frozen=True)
class Capability:
    """One coherent skill: what it can do, how to use it, and whether it's here.

    `prompt` is the instruction fragment injected into the system prompt when
    this capability is active — the paragraph that used to live in the persona.
    It may contain `{placeholders}`, substituted at render time.

    `register` receives the shared ToolRegistry and registers this
    capability's tools on it. Omit it for a capability that is pure
    instruction (a behaviour rule with no tools of its own).

    `permissions` names what this capability needs to be allowed to do —
    "shell", "files", "network", "messaging". Nothing enforces them yet; they
    are declared here so that when a permission layer lands it has something
    to read, rather than needing every capability rewritten.
    """

    name: str
    summary: str
    prompt: str = ""
    register: Callable[[ToolRegistry], None] | None = None
    available: bool = True
    permissions: tuple[str, ...] = ()
    #: Tools this capability owns, by name. Set it when the tools are
    #: registered somewhere else (a legacy builder being migrated), so
    #: `apply_permissions` can still attribute this capability's permissions
    #: to them. Redundant when `register` is used — those are detected.
    tools: tuple[str, ...] = ()
    #: Lower sorts earlier in the composed prompt. Same-order capabilities keep
    #: the order they were added, so a prompt stays byte-stable across runs —
    #: which matters for prompt caching and for diffing what she was told.
    order: int = 100

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("a capability needs a name")
        if not self.summary.strip():
            raise ValueError(f"capability {self.name!r} needs a summary")


class CapabilitySet:
    """Every capability a runtime knows about, active or not.

    Two things come out of it and they are always consistent with each other:
    the ToolRegistry the model may call, and the prompt fragment telling it
    when to.
    """

    def __init__(self, capabilities: Iterable[Capability] = ()):
        self._capabilities: list[Capability] = []
        self.extend(capabilities)

    # ── building ────────────────────────────────────────────────────────────
    def add(self, capability: Capability) -> Capability:
        if any(c.name == capability.name for c in self._capabilities):
            raise ValueError(f"duplicate capability: {capability.name}")
        self._capabilities.append(capability)
        return capability

    def extend(self, capabilities: Iterable[Capability]) -> None:
        for capability in capabilities:
            self.add(capability)

    # ── inspection ──────────────────────────────────────────────────────────
    def __iter__(self) -> Iterator[Capability]:
        return iter(self._capabilities)

    def __len__(self) -> int:
        return len(self._capabilities)

    def __contains__(self, name: str) -> bool:
        return any(c.name == name for c in self._capabilities)

    def active(self) -> list[Capability]:
        """Available capabilities, in prompt order (stable within an order)."""
        live = [c for c in self._capabilities if c.available]
        return sorted(live, key=lambda c: c.order)

    def inactive(self) -> list[Capability]:
        return [c for c in self._capabilities if not c.available]

    def names(self) -> list[str]:
        return [c.name for c in self.active()]

    def permissions(self) -> tuple[str, ...]:
        """Every permission the active set needs, deduped, sorted."""
        wanted = {p for c in self.active() for p in c.permissions}
        return tuple(sorted(wanted))

    # ── output ──────────────────────────────────────────────────────────────
    def build_registry(self, platform: str = ANY_PLATFORM) -> ToolRegistry:
        """A registry holding exactly the active capabilities' tools.

        Each capability's tools inherit that capability's permissions, so
        "the music tools need audio" is stated once rather than on every
        decoration. A tool that declared its own permissions keeps them.
        """
        registry = ToolRegistry(platform=platform)
        for capability in self.active():
            if capability.register is None:
                continue
            before = set(registry.names(platform=ANY_PLATFORM))
            capability.register(registry)
            added = set(registry.names(platform=ANY_PLATFORM)) - before
            registry.grant_default_permissions(added, capability.permissions)
        return registry

    def apply_permissions(self, registry: ToolRegistry) -> dict[str, tuple[str, ...]]:
        """Attribute each capability's permissions to the tools it owns.

        For a registry built elsewhere — Venom's `build_pi_registry`, which
        predates capabilities and registers all 86 tools itself. Without this
        the tools carry no permissions, every check trivially passes, and the
        guard degrades to an audit log. Returns what was applied, so a caller
        (or a test) can see the coverage.

        Only *active* capabilities apply: an inactive one has no tools in the
        registry to attribute to.
        """
        applied: dict[str, tuple[str, ...]] = {}
        known = set(registry.names(platform=ANY_PLATFORM))
        for capability in self.active():
            if not capability.permissions:
                continue
            owned = [name for name in capability.tools if name in known]
            registry.grant_default_permissions(owned, capability.permissions)
            for name in owned:
                applied[name] = capability.permissions
        return applied

    def unclaimed_tools(self, registry: ToolRegistry) -> list[str]:
        """Registered tools that no capability claims.

        A tool nobody owns can never be permission-checked, so this is the
        list a test should keep down to the deliberately-harmless ones.
        """
        claimed = {name for c in self._capabilities for name in c.tools}
        return sorted(set(registry.names(platform=ANY_PLATFORM)) - claimed)

    def render_prompt(self, **substitutions: str) -> str:
        """The active capabilities' instructions, in order, as one block.

        Capabilities with no prompt contribute nothing — a tools-only skill
        does not get a blank paragraph.
        """
        blocks = [c.prompt.strip() for c in self.active() if c.prompt.strip()]
        text = "\n\n".join(blocks)
        for key, value in substitutions.items():
            text = text.replace("{" + key + "}", value)
        return text

    def describe(self) -> str:
        """One line per capability, for the console and the boot log."""
        lines = []
        for capability in sorted(self._capabilities, key=lambda c: c.name):
            mark = "on " if capability.available else "off"
            lines.append(f"[{mark}] {capability.name} — {capability.summary}")
        return "\n".join(lines)

    def log_summary(self) -> None:
        active = self.active()
        log.info("capabilities: %d on (%s), %d off (%s)",
                 len(active), ", ".join(c.name for c in active) or "none",
                 len(self.inactive()),
                 ", ".join(c.name for c in self.inactive()) or "none")
