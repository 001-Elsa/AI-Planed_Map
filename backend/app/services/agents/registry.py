from backend.app.schemas.agent_artifacts import AgentType
from backend.app.services.agents.companion_agent import COMPANION_AGENT_SPEC
from backend.app.services.agents.critic_agent import CRITIC_AGENT_SPEC
from backend.app.services.agents.intent_agent import INTENT_AGENT_SPEC
from backend.app.services.agents.planner_agent import PLANNER_AGENT_SPEC
from backend.app.services.agents.replanner_agent import REPLANNER_AGENT_SPEC
from backend.app.services.agents.safety_agent import SAFETY_AGENT_SPEC
from backend.app.services.agents.search_agent import SEARCH_AGENT_SPEC
from backend.app.services.agents.supervisor_agent import SUPERVISOR_AGENT_SPEC

AGENT_REGISTRY = {
    AgentType.supervisor: SUPERVISOR_AGENT_SPEC,
    AgentType.intent: INTENT_AGENT_SPEC,
    AgentType.search: SEARCH_AGENT_SPEC,
    AgentType.safety: SAFETY_AGENT_SPEC,
    AgentType.planner: PLANNER_AGENT_SPEC,
    AgentType.critic: CRITIC_AGENT_SPEC,
    AgentType.companion: COMPANION_AGENT_SPEC,
    AgentType.replanner: REPLANNER_AGENT_SPEC,
}
