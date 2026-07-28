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
    external_timeout_seconds: float = 8.0
    mock_map_provider: bool = False
    max_request_bytes: int = 1_000_000

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

