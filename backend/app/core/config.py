from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    app_name: str = "MapGo AI Planner"
    app_version: str = "7.0.0"
    environment: str = "development"
    database_url: str = f"sqlite+aiosqlite:///{(ROOT / 'data' / 'mapgo-python.db').as_posix()}"
    database_pool_size: int = 20
    database_max_overflow: int = 20
    database_pool_timeout_seconds: float = 10.0
    sqlite_busy_timeout_seconds: float = 15.0
    public_dir: Path = ROOT / "public"
    session_days: int = 30
    admin_init_token: str = ""
    amap_web_key: str = ""
    amap_key: str = ""
    amap_jscode: str = ""
    disable_configured_map_credentials: bool = False
    llm_api_key: str = ""
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4.1-mini"
    llm_small_model: str = ""
    llm_strong_model: str = ""
    model_router_enabled: bool = True
    model_router_rule_max_complexity: int = Field(default=1, ge=0, le=20)
    model_router_strong_min_complexity: int = Field(default=5, ge=1, le=20)
    model_router_strong_min_uncertainty: int = Field(default=2, ge=1, le=20)
    max_llm_output_tokens: int = 2_000
    prompt_version: str = "intent-v2"
    external_timeout_seconds: float = 8.0
    external_connect_timeout_seconds: float = 5.0
    http_max_connections: int = 100
    http_max_keepalive_connections: int = 20
    map_max_concurrency: int = 8
    upstream_max_retries: int = 2
    circuit_breaker_failure_threshold: int = 5
    circuit_breaker_recovery_seconds: float = 30.0
    mock_map_provider: bool = False
    max_request_bytes: int = 1_000_000
    max_route_matrix_points: int = 25
    idempotency_ttl_seconds: int = 86_400
    required_schema_revision: str = "0014"
    precise_location_ttl_minutes: int = 120
    location_encryption_key: str = ""
    max_agent_tool_calls: int = 8
    max_agent_tool_calls_per_run: int = 4
    max_agent_tool_calls_per_task: int = 8
    max_agent_tool_calls_per_trip: int = 40
    max_agent_steps: int = 4
    max_agent_input_tokens: int = 6_000
    max_agent_output_tokens: int = 800
    max_agent_run_cost_usd: float = 0.05
    max_replans_per_trip: int = 10
    daily_ai_token_quota: int = 100_000
    max_ai_request_cost_usd: float = 0.20
    feature_companion_agent: bool = True
    multi_agent_enabled: bool = True
    plan_critic_mode: str = "shadow"
    max_agent_workflow_cost_usd: float = 0.08
    max_agent_handoffs: int = 12
    agent_stage_timeout_seconds: float = 12.0
    agent_search_max_attempts: int = 2
    agent_shared_state_ttl_seconds: int = 7_200
    agent_shared_state_max_history: int = 100
    agent_shared_state_max_bytes: int = 512_000
    max_critic_input_tokens: int = 4_000
    max_critic_output_tokens: int = 800
    max_critic_retries: int = 1
    critic_enforce_min_shadow_samples: int = 30
    critic_enforce_max_fallback_rate: float = 0.02
    critic_enforce_max_blocking_rate: float = 0.05
    critic_enforce_max_budget_exceeded_rate: float = 0.01
    critic_enforce_max_p95_latency_ms: int = 5_000
    worker_recover_processing_limit: int = 100
    worker_lock_ttl_seconds: int = 30
    worker_lock_renew_interval_seconds: int = 10
    redis_url: str = ""
    agent_message_transport: Literal["auto", "memory", "redis_stream"] = "auto"
    agent_stream_prefix: str = "mapgo:agent-messages"
    agent_consumer_group_prefix: str = "mapgo:agent-workers"
    agent_stream_max_length: int = 20_000
    agent_message_max_attempts: int = 3
    agent_message_reclaim_idle_ms: int = 30_000
    api_requests_per_minute: int = 120
    api_ip_requests_per_minute: int = 3000
    auth_device_requests_per_minute: int = 30
    auth_ip_requests_per_minute: int = 600
    amap_proxy_requests_per_minute: int = 300
    amap_proxy_max_response_bytes: int = 2_000_000
    amap_proxy_max_cache_bytes: int = 250_000
    ai_plans_per_day: int = 50
    mock_weather_provider: bool = True
    llm_input_cost_per_million_usd: float = 0.40
    llm_output_cost_per_million_usd: float = 1.60
    llm_small_input_cost_per_million_usd: float = Field(default=0.40, ge=0)
    llm_small_output_cost_per_million_usd: float = Field(default=1.60, ge=0)
    llm_strong_input_cost_per_million_usd: float = Field(default=2.00, ge=0)
    llm_strong_output_cost_per_million_usd: float = Field(default=8.00, ge=0)

    model_config = SettingsConfigDict(
        env_file=ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @model_validator(mode="after")
    def validate_model_router_thresholds(self) -> "Settings":
        if self.model_router_rule_max_complexity >= self.model_router_strong_min_complexity:
            raise ValueError(
                "MODEL_ROUTER_RULE_MAX_COMPLEXITY must be lower than "
                "MODEL_ROUTER_STRONG_MIN_COMPLEXITY"
            )
        return self

    @property
    def use_mock_map(self) -> bool:
        if self.disable_configured_map_credentials:
            return True
        has_real_credentials = bool(self.amap_web_key or (self.amap_key and self.amap_jscode))
        return self.mock_map_provider or not has_real_credentials


@lru_cache
def get_settings() -> Settings:
    return Settings()
