"""Voice tools over an OutcomeLog — why she chose that, what she has learned.

Both tools read back rows that were recorded at the time. Neither asks a model
to reconstruct a rationale afterwards, which is the whole discipline of
`flint_core.outcomes`: a confabulated explanation is worse than none.
"""

from __future__ import annotations


def register_learning_tools(reg, outcomes):
    """Reading back her own record: why she chose something, what she's learned."""

    @reg.tool(
        description=(
            "Explains why she actually chose what she chose — reads back the "
            "reason recorded at the time, never a story made up afterwards. "
            "Use for 'why did you do that?', 'why that one?', 'aisa kyun kiya?'. "
            "If nothing was recorded, say so plainly."
        ),
        parameters={
            "type": "object",
            "properties": {
                "about": {"type": "string",
                          "description": "Which kind of decision, e.g. 'which agent'"},
            },
        },
    )
    def why_did_you(about: str = "") -> str:
        return outcomes.explain(about)

    @reg.tool(
        description=("Says what she's picked up about how he likes things done, "
                     "from what actually happened before. Use for 'what have "
                     "you learned about me?', 'what do you know by now?'."),
    )
    def what_have_you_learned() -> str:
        notes = outcomes.advice()
        if not notes:
            return ("Nothing solid yet — I haven't seen enough to call it a "
                    "pattern, and I'd rather not guess.")
        return " ".join(notes)
