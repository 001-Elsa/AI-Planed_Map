from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TransportMode(str, Enum):
    walking = "walking"
    driving = "driving"
    transit = "transit"
    cycling = "cycling"


class Coordinate(StrictModel):
    lng: float = Field(ge=-180, le=180)
    lat: float = Field(ge=-90, le=90)


class PlanningPreferences(StrictModel):
    minimize_distance: bool = False
    minimize_walking: bool = False
    minimize_cost: bool = False
    prefer_high_rating: bool = False


class PlanningTask(StrictModel):
    description: str = Field(min_length=1, max_length=300)
    location_name: str | None = Field(default=None, max_length=200)
    category: str | None = Field(default=None, max_length=100)
    location_hint: str | None = Field(default=None, max_length=200)
    service_duration_minutes: int = Field(default=0, ge=0, le=1440)
    deadline: datetime | None = None
    required: bool = True


class PlanningIntent(StrictModel):
    origin: str | None = Field(default=None, max_length=200)
    departure_time: datetime | None = None
    transport_mode: TransportMode = TransportMode.walking
    tasks: list[PlanningTask] = Field(min_length=1, max_length=10)
    preferences: PlanningPreferences = Field(default_factory=PlanningPreferences)


class AIPlanRequest(StrictModel):
    text: str = Field(min_length=2, max_length=4000)
    origin: Coordinate | None = None
    departure_time: datetime | None = None
    transport_mode: TransportMode | None = None
    default_service_duration_minutes: int = Field(default=15, ge=0, le=240)
    city: str | None = Field(default=None, max_length=50)

    @model_validator(mode="after")
    def require_origin_hint(self) -> "AIPlanRequest":
        # A textual origin may still be extracted by the parser, so validation is
        # completed in the planning service after parsing.
        return self


class PoiCandidate(StrictModel):
    id: str
    name: str
    address: str = ""
    location: Coordinate
    rating: float | None = None
    distance_meters: float | None = None


class PlannedStop(StrictModel):
    task_index: int
    task: PlanningTask
    poi: PoiCandidate
    arrival_time: datetime
    departure_time: datetime
    travel_seconds: float
    travel_meters: float
    constraint_satisfied: bool


class AIPlanResult(StrictModel):
    status: str
    intent: PlanningIntent
    stops: list[PlannedStop] = Field(default_factory=list)
    total_distance_meters: float = 0
    total_travel_seconds: float = 0
    algorithm: str | None = None
    explanation: str | None = None
    questions: list[dict] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
