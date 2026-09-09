import json
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
        assert service["expose"] == ["9100"]

    assert services["worker"]["command"] == ["python", "-m", "backend.app.worker"]
    assert services["worker"]["expose"] == ["9100"]


def test_prometheus_scrapes_workers_and_provisions_agent_alerts_and_dashboard():
    root = Path(__file__).resolve().parents[3]
    prometheus = yaml.safe_load(
        (root / "infrastructure/prometheus.yml").read_text(encoding="utf-8")
    )
    targets = {
        target
        for job in prometheus["scrape_configs"]
        for config in job["static_configs"]
        for target in config["targets"]
    }
    assert {
        "api:3000",
        "worker:9100",
        "agent-planner:9100",
        "agent-critic:9100",
        "agent-replanner:9100",
    }.issubset(targets)
    assert "/etc/prometheus/agent-alerts.yml" in prometheus["rule_files"]

    alerts = yaml.safe_load(
        (root / "infrastructure/agent-alerts.yml").read_text(encoding="utf-8")
    )
    alert_names = {rule["alert"] for group in alerts["groups"] for rule in group["rules"]}
    assert {
        "AgentPendingBacklog",
        "AgentPendingMessageStale",
        "AgentDeadLetterQueueNotEmpty",
        "AgentReclaimRateHigh",
    } <= alert_names

    dashboard = json.loads(
        (root / "infrastructure/grafana/dashboards/mapgo-ai-planned.json").read_text(
            encoding="utf-8"
        )
    )
    expressions = " ".join(
        target["expr"] for panel in dashboard["panels"] for target in panel["targets"]
    )
    assert "mapgo_agent_task_duration_ms" in expressions
    assert "mapgo_agent_task_queue_delay_ms" in expressions
    assert "mapgo_agent_dlq_messages" in expressions
    assert "mapgo_agent_workflow_node_transitions_total" in expressions
