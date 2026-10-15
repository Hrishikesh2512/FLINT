"""Shield on the phone itself: the tools are shared (flint_core.skills.shield);
what is Carnage's own is how it gets in, with the token the Shield app hands
only to apps signed by him."""

from __future__ import annotations

from collections.abc import Callable

from flint_core.skills.shield import (  # noqa: F401, re-exported for Carnage
    DEFAULT_FOLDER,
    SHIELD_PROMPT,
    register_shield_tools,
)


def shield_connector(phone) -> Callable:
    """How Carnage reaches Shield on this phone: the token comes from the Shield
    app each time, so a new token in Shield's settings needs nothing here."""

    def connect():
        access = getattr(phone, "shield_access", lambda: None)()
        if not access:
            return None
        from shield_client import Shield

        return Shield(f"http://127.0.0.1:{access.get('port', 8791)}",
                      token=str(access["token"]), agent="jarvis")

    return connect
