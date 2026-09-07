from pathlib import Path

import yaml

from backend.app.clients.amap_client import MockMapProvider
from backend.app.core.config import Settings
from backend.app.services.planning_service import PlanningService


class _Parser:
    name = "deployment-test-parser"


def test_distributed_planning_fails_closed_without_message_bus():
    settings = Settings(
        mock_map_provider=True,
        agent_execution_mode="distributed",
        agent_message_transport="redis_stream",
    )

    try:
        PlanningService(_Parser(), MockMapProvider(), settings)
    except ValueError as exc:
        assert "requires an Agent message bus" in str(exc)
    else:
        raise AssertionError("distributed planning must not fall back to in-process roles")


def test_compose_declares_independently_scalable_agent_role_services():
    root = Path(__file__).resolve().parents[3]
    compose = yaml.safe_load((root / "docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]
    environment = compose["x-api-environment"]

    assert environment["AGENT_EXECUTION_MODE"] == "${AGENT_EXECUTION_MODE:-distributed}"
    assert environment["AGENT_MESSAGE_TRANSPORT"] == "${AGENT_MESSAGE_TRANSPORT:-redis_stream}"
    for role in ("planner", "critic", "replanner"):
        service = services[f"agent-{role}"]
        assert service["command"] == [
            "python",
            "-m",
            "backend.app.agent_role_worker",
            "--role",
            role,
        ]
        assert service["restart"] == "unless-stopped"

    assert services["worker"]["command"] == ["python", "-m", "backend.app.worker"]
