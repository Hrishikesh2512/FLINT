"""What this body offers — mostly the shared core, plus what a phone adds.

Worth reading next to `venom/capabilities.py`, because the difference between
the two files is the honest description of what Carnage actually is. Almost
every entry here is the same registrar the Pi uses, called with the same kinds
of store. The phone-only block is short, and that is the point: one assistant,
and the body decides what she can reach, not who she is.

She is not a different person here either. The persona comes from
`flint_core.persona` unchanged; only the one-sentence body description differs.
"""

from __future__ import annotations

import logging

from flint_core.capabilities import Capability, CapabilitySet
from flint_core.relay import register_relay_tools
from flint_core.skills import (
    register_audit_tools,
    register_basic_tools,
    register_build_tools,
    register_calendar_tools,
    register_connection_tools,
    register_dev_tools,
    register_document_tools,
    register_job_tools,
    register_learning_tools,
    register_list_tools,
    register_mail_tools,
    register_memory_tools,
    register_note_tools,
    register_project_tools,
    register_recall_tools,
    register_reminder_tools,
    register_watch_tools,
)

from carnage.tools_phone import (
    register_phone_tools,
    register_sms_tools,
    register_sos_sms,
)

log = logging.getLogger("carnage.capabilities")

PHONE_PROMPT = (
    "PHONE: you are on his phone, so you know things your other bodies "
    "cannot — a real GPS position rather than a city, the battery, and what "
    "is in the notification shade. Use where_am_i when a task turns on where "
    "he actually is. Mention the battery unprompted only when it is low and "
    "he is about to rely on the phone."
)

SMS_PROMPT = (
    "TEXT MESSAGES: send_text goes over the cellular network, so it works "
    "when there is no internet at all. Prefer it over any other channel when "
    "he says there is no signal, or when a message genuinely has to arrive."
)

SOS_PROMPT = (
    "EMERGENCY BY SMS: sos_sms reaches his emergency contacts without needing "
    "the internet. If an emergency alert could not be delivered any other "
    "way, use it — do not report a failure while this is still untried. "
    "Always say afterwards exactly who was reached and who was not."
)


def build_capabilities(config, *, phone=None, memory=None, projects=None,
                       outcomes=None, archive=None, jobs=None, watches=None,
                       sos=None, connections=None, notes=None, lists=None,
                       reminders=None, calendar=None, mailbox=None, audit=None,
                       timers=None, search=None, relay=None,
                       roster=None) -> CapabilitySet:
    """Everything this phone can do, given what was actually wired up.

    Long argument list on purpose: each one is a thing that may or may not
    exist on a given phone, and a capability whose dependency is None is
    switched off rather than registered and broken.
    """
    has_phone = phone is not None and phone.available()

    def when(dependency, register):
        """Register only if the dependency is really here."""
        return register if dependency is not None else None

    capabilities = CapabilitySet([
        # ── the floor ───────────────────────────────────────────────────
        Capability(
            name="core", order=10,
            summary="Time, timers, search, weather.",
            available=memory is not None and search is not None,
            permissions=("network",),
            register=when(memory if search is not None else None,
                          lambda reg: register_basic_tools(
                              reg, search=search, memory=memory, timers=timers,
                              current_city=_city_from(phone))),
        ),
        Capability(
            name="memory", order=11,
            summary="Remember facts about him between conversations.",
            available=memory is not None,
            permissions=("personal_data",),
            register=when(memory, lambda reg: register_memory_tools(reg, memory)),
        ),
        Capability(
            name="recall", order=12,
            summary="Search everything ever filed away, and file more.",
            available=archive is not None,
            permissions=("personal_data",),
            register=when(archive, lambda reg: register_recall_tools(reg, archive)),
        ),
        Capability(
            name="activity", order=13,
            summary="What she has actually done, refusals included.",
            available=audit is not None,
            register=when(audit, lambda reg: register_audit_tools(reg, audit)),
        ),

        # ── the everyday stores, shared with every other body ───────────
        Capability(
            name="reminders", order=20,
            summary="Wall-clock reminders that survive a restart.",
            available=reminders is not None,
            register=when(reminders,
                          lambda reg: register_reminder_tools(reg, reminders)),
        ),
        Capability(
            name="notes", order=21,
            summary="Quick notes, kept in step with his other devices.",
            available=notes is not None,
            register=when(notes, lambda reg: register_note_tools(reg, notes)),
        ),
        Capability(
            name="lists", order=22,
            summary="Shopping, to-do and any other named list.",
            available=lists is not None,
            register=when(lists, lambda reg: register_list_tools(reg, lists)),
        ),
        Capability(
            name="connections", order=23,
            summary="Who people are, and how to reach them.",
            available=connections is not None,
            permissions=("personal_data",),
            register=when(connections,
                          lambda reg: register_connection_tools(reg, connections)),
        ),
        Capability(
            name="projects", order=24,
            summary="Real work: tasks, deadlines, what is blocked on what.",
            available=projects is not None,
            register=when(projects,
                          lambda reg: register_project_tools(reg, projects)),
        ),
        Capability(
            name="learning", order=25,
            summary="Why she chose something, and what she has learned.",
            available=outcomes is not None,
            register=when(outcomes,
                          lambda reg: register_learning_tools(reg, outcomes)),
        ),

        # ── calendar and mail ───────────────────────────────────────────
        Capability(
            name="calendar", order=30,
            summary="His agenda and what is next.",
            available=calendar is not None,
            permissions=("network", "personal_data"),
            register=when(calendar,
                          lambda reg: register_calendar_tools(reg, calendar)),
        ),
        Capability(
            name="mail", order=31,
            summary="Unread mail, read-only.",
            available=mailbox is not None,
            permissions=("network", "personal_data"),
            register=when(mailbox, lambda reg: register_mail_tools(reg, mailbox)),
        ),

        # ── work that outlives the conversation ─────────────────────────
        Capability(
            name="jobs", order=40,
            summary="Research and long work she goes away and does.",
            available=jobs is not None,
            permissions=("network",),
            register=when(jobs, lambda reg: register_job_tools(reg, jobs)),
        ),
        Capability(
            name="watches", order=41,
            summary="Things she keeps an eye on and reports back about.",
            available=watches is not None,
            permissions=("network",),
            register=when(watches, lambda reg: register_watch_tools(reg, watches)),
        ),
        Capability(
            name="documents", order=42,
            summary="Write notes, spreadsheets and decks out to files.",
            available=bool(config.documents_dir),
            permissions=("files",),
            register=(lambda reg: register_document_tools(reg, config.documents_dir))
            if config.documents_dir else None,
        ),
        Capability(
            name="dev", order=43,
            summary="Git status, commits, branches and pull requests.",
            available=bool(config.repos),
            permissions=("files", "network"),
            register=(lambda reg: register_dev_tools(reg, config, jobs=jobs))
            if config.repos else None,
        ),
        Capability(
            name="building", order=44,
            summary="Build an app in the background and report back.",
            available=jobs is not None and bool(config.repos),
            permissions=("files", "network"),
            register=(lambda reg: register_build_tools(
                reg, jobs, default_dir=config.default_repo))
            if jobs is not None and config.repos else None,
        ),

        # ── being one assistant rather than three ───────────────────────
        Capability(
            name="other_bodies", order=50,
            summary="Hand work to her other devices, and hear back.",
            available=relay is not None and roster is not None,
            register=(lambda reg: register_relay_tools(
                reg, relay, roster, config.device))
            if relay is not None and roster is not None else None,
        ),

        # ── what this body adds ─────────────────────────────────────────
        Capability(
            name="phone", order=60,
            summary="GPS position, battery, and the notification shade.",
            prompt=PHONE_PROMPT, available=has_phone,
            permissions=("location", "personal_data"),
            register=(lambda reg: register_phone_tools(reg, phone))
            if has_phone else None,
        ),
        Capability(
            name="sms", order=61,
            summary="Send a text over cellular, with or without internet.",
            prompt=SMS_PROMPT, available=has_phone,
            permissions=("messaging",),
            register=(lambda reg: register_sms_tools(reg, phone,
                                                     contacts=connections))
            if has_phone else None,
        ),
        Capability(
            name="emergency_sms", order=90,
            summary="Reach emergency contacts by SMS when there is no data.",
            prompt=SOS_PROMPT,
            available=has_phone and sos is not None,
            permissions=("messaging", "location"),
            register=(lambda reg: register_sos_sms(reg, phone, sos))
            if has_phone and sos is not None else None,
        ),
    ])
    return capabilities


def _city_from(phone):
    """A city name from the phone, for the weather tool — when one exists.

    Currently always None, and deliberately so rather than as an oversight.
    The phone gives coordinates; `weather_report` wants a city name, and
    turning one into the other needs a reverse-geocode this package does not
    have. Returning None means the tool falls back to the remembered home
    city, which is a correct answer — whereas wiring up a lookup that returns
    an empty string on every call would look implemented and behave identically.

    When the host app grows a reverse-geocode, this is the one place to hand it
    in; nothing else changes.
    """
    return None
