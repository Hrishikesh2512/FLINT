"""flint-core agents — handing whole goals to something that isn't this process.

    base      AgentRequest / AgentResult / AgentSpec — the protocol
    registry  AgentRegistry — which agent gets the job
    cli       CLIAgent — a headless coding CLI, presented as an agent

Replaces one-shot delegation (`laptop_task`: a sentence out, 600 characters
back) with something that streams progress, reports what actually changed,
can ask a question back, and can be chosen on the merits of the task.
"""

from flint_core.agents.base import (
    MAX_SPOKEN,
    Agent,
    AgentRequest,
    AgentResult,
    AgentSpec,
)
from flint_core.agents.cli import (
    CLAUDE_CODE_DEFAULT,
    CLIAgent,
    CLIAgentConfig,
    agents_from_config,
    cli_agent_spec,
)
from flint_core.agents.registry import AgentRegistry, NoAgentAvailableError

__all__ = [
    "CLAUDE_CODE_DEFAULT",
    "MAX_SPOKEN",
    "Agent",
    "AgentRegistry",
    "AgentRequest",
    "AgentResult",
    "AgentSpec",
    "CLIAgent",
    "CLIAgentConfig",
    "NoAgentAvailableError",
    "agents_from_config",
    "cli_agent_spec",
]
