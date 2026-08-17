"""Tool belts a device can offer, over stores the shared core already owns.

The rule elsewhere in this package is that mechanism lives here and product
decisions live in the device — job types sit in `venom/jobs.py` for exactly
that reason. These registrars look like they break the rule and don't, because
of where the decision actually is:

    flint_core.skills   *how* a project task or a git commit is spoken about
    venom/capabilities  *whether this body offers it at all*

A registrar is an offering, never an installation. Nothing here registers
itself; a device names the ones it wants, and a device that never mentions
`register_dev_tools` cannot commit to anything no matter what is in its
config. The product decision stays exactly where it was.

What earns a module a place here is that its answer does not depend on the
body it is running in: a task blocked on another task, a commit refused on
master, a document written to a folder. Anything whose answer differs per
device — the volume, the temperature, the camera — belongs to that device, or
to a provider it injects.
"""

from flint_core.skills.agenda import (
    register_calendar_tools,
    register_mail_tools,
)
from flint_core.skills.building import register_build_tools
from flint_core.skills.delegating import (
    register_job_tools,
    register_watch_tools,
)
from flint_core.skills.dev import Workspace, register_dev_tools
from flint_core.skills.documents import register_document_tools
from flint_core.skills.everyday import (
    Timer,
    TimerBoard,
    fetch_weather,
    home_city,
    parse_reminder_time,
    register_audit_tools,
    register_basic_tools,
    register_memory_tools,
)
from flint_core.skills.keeping import (
    register_list_tools,
    register_note_tools,
    register_reminder_tools,
)
from flint_core.skills.learning import register_learning_tools
from flint_core.skills.people import register_connection_tools
from flint_core.skills.projects import register_project_tools
from flint_core.skills.recall import register_recall_tools

__all__ = [
    "Timer",
    "TimerBoard",
    "Workspace",
    "fetch_weather",
    "home_city",
    "parse_reminder_time",
    "register_audit_tools",
    "register_basic_tools",
    "register_build_tools",
    "register_calendar_tools",
    "register_connection_tools",
    "register_dev_tools",
    "register_document_tools",
    "register_job_tools",
    "register_learning_tools",
    "register_list_tools",
    "register_mail_tools",
    "register_memory_tools",
    "register_note_tools",
    "register_project_tools",
    "register_recall_tools",
    "register_reminder_tools",
    "register_watch_tools",
]
