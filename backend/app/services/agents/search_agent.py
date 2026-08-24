from backend.app.schemas.agent_artifacts import AgentBudget, AgentSpec, AgentType
from backend.app.services.agent_tool_registry import TOOL_REGISTRY, InvocationMode

SEARCH_AGENT_SPEC = AgentSpec(
    agent_type=AgentType.search,
    prompt_version="search-stage-v1",
    allowed_tools=frozenset(),
    allowed_internal_capabilities=TOOL_REGISTRY.names_for(
        AgentType.search, InvocationMode.internal_stage
    ),
    input_artifact_types=frozenset({"intent_artifact"}),
    output_artifact_type="search_artifact",
    budget=AgentBudget(
        max_steps=1, max_input_tokens=2_000, max_output_tokens=1_000, max_cost_usd=0
    ),
)
