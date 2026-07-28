from datetime import datetime, timezone
from typing import Protocol

import httpx
from pydantic import BaseModel, Field

from backend.app.schemas.ai_intent import Coordinate


class WeatherSnapshot(BaseModel):
    temperature_c: float
    precipitation_probability: float = Field(ge=0, le=100)
    weather_code: int
    observed_at: datetime
    source: str
    confidence: float = Field(ge=0, le=1)
    is_mock: bool = False


class WeatherProvider(Protocol):
    name: str

    async def current(self, location: Coordinate) -> WeatherSnapshot: ...


class MockWeatherProvider:
    name = "mock-weather-v1"

    async def current(self, location: Coordinate) -> WeatherSnapshot:
        return WeatherSnapshot(
            temperature_c=24,
            precipitation_probability=15,
            weather_code=1,
            observed_at=datetime.now(timezone.utc),
            source=self.name,
            confidence=0.5,
            is_mock=True,
        )


class OpenMeteoWeatherProvider:
    name = "open-meteo-v1"

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client

    async def current(self, location: Coordinate) -> WeatherSnapshot:
        response = await self.client.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": location.lat,
                "longitude": location.lng,
                "current": "temperature_2m,weather_code",
                "hourly": "precipitation_probability",
                "forecast_days": 1,
                "timezone": "auto",
            },
        )
        response.raise_for_status()
        data = response.json()
        current = data["current"]
        hourly = data.get("hourly", {})
        probabilities = hourly.get("precipitation_probability") or [0]
        return WeatherSnapshot(
            temperature_c=float(current["temperature_2m"]),
            precipitation_probability=float(max(probabilities[:6] or [0])),
            weather_code=int(current["weather_code"]),
            observed_at=datetime.fromisoformat(current["time"]),
            source=self.name,
            confidence=0.85,
        )


def build_weather_provider(client: httpx.AsyncClient, use_mock: bool) -> WeatherProvider:
    return MockWeatherProvider() if use_mock else OpenMeteoWeatherProvider(client)
