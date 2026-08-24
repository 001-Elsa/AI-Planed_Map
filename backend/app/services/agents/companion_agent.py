from backend.app.schemas.agent_artifacts import AgentBudget, AgentSpec, AgentType
from backend.app.services.agent_tool_registry import TOOL_REGISTRY, InvocationMode

COMPANION_AGENT_SPEC = AgentSpec(
    agent_type=AgentType.companion,
    prompt_version="companion-agent-v2",
    allowed_tools=TOOL_REGISTRY.names_for(AgentType.companion, InvocationMode.agent_callable),
    input_artifact_types=frozenset({"trip_observation"}),
    output_artifact_type="companion_action_report",
    budget=AgentBudget(
        max_steps=4, max_input_tokens=6_000, max_output_tokens=800, max_cost_usd=0.05
    ),
)
