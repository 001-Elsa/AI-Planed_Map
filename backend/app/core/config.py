from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    app_name: str = "MapGo AI Planner"
    app_version: str = "6.0.0"
    environment: str = "development"
    database_url: str = f"sqlite+aiosqlite:///{(ROOT / 'data' / 'mapgo-python.db').as_posix()}"
    public_dir: Path = ROOT / "public"
    session_days: int = 30
    admin_init_token: str = ""
    amap_web_key: str = ""
    amap_key: str = ""
    amap_jscode: str = ""
    llm_api_key: str = ""
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4.1-mini"
    max_llm_output_tokens: int = 2_000
    prompt_version: str = "intent-v2"
    external_timeout_seconds: float = 8.0
    external_connect_timeout_seconds: float = 2.0
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
    required_schema_revision: str = "0006"
    precise_location_ttl_minutes: int = 120
    location_encryption_key: str = ""
    max_agent_tool_calls: int = 8
    max_agent_steps: int = 4
    max_agent_input_tokens: int = 6_000
    max_agent_output_tokens: int = 800
    max_agent_run_cost_usd: float = 0.05
    max_replans_per_trip: int = 10
    daily_ai_token_quota: int = 100_000
    max_ai_request_cost_usd: float = 0.20
    feature_companion_agent: bool = True
    redis_url: str = ""
    api_requests_per_minute: int = 120
    ai_plans_per_day: int = 50
    mock_weather_provider: bool = True
    llm_input_cost_per_million_usd: float = 0.40
    llm_output_cost_per_million_usd: float = 1.60

    model_config = SettingsConfigDict(
        env_file=ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def use_mock_map(self) -> bool:
        return self.mock_map_provider or not self.amap_web_key


@lru_cache
def get_settings() -> Settings:
    return Settings()
