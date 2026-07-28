from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TransportMode(str, Enum):
    walking = "walking"
    driving = "driving"
    transit = "transit"
    cycling = "cycling"


class PlanningState(str, Enum):
    draft = "DRAFT"
    need_clarification = "NEED_CLARIFICATION"
    candidates_ready = "CANDIDATES_READY"
    optimizing = "OPTIMIZING"
    plan_ready = "PLAN_READY"
    infeasible = "INFEASIBLE"


class DataQuality(str, Enum):
    realtime = "realtime"
    provider = "provider"
    cached = "cached"
    estimated = "estimated"


class Coordinate(StrictModel):
    lng: float = Field(ge=-180, le=180)
    lat: float = Field(ge=-90, le=90)


class ObjectiveWeights(StrictModel):
    travel_time: float = Field(default=1.0, ge=0, le=10)
    walking_time: float = Field(default=0.6, ge=0, le=10)
    distance: float = Field(default=0.15, ge=0, le=10)
    low_rating: float = Field(default=0.35, ge=0, le=10)
    uncertainty: float = Field(default=0.8, ge=0, le=10)
    change: float = Field(default=0.5, ge=0, le=10)
    monetary_cost: float = Field(default=0.4, ge=0, le=10)


class PlanningPreferences(StrictModel):
    minimize_distance: bool = False
    minimize_walking: bool = False
    minimize_cost: bool = False
    prefer_high_rating: bool = False
    weights: ObjectiveWeights = Field(default_factory=ObjectiveWeights)


class PlanningTask(StrictModel):
    description: str = Field(min_length=1, max_length=300)
    location_name: str | None = Field(default=None, max_length=200)
    category: str | None = Field(default=None, max_length=100)
    location_hint: str | None = Field(default=None, max_length=200)
    service_duration_minutes: int = Field(default=0, ge=0, le=1440)
    min_service_duration_minutes: int | None = Field(default=None, ge=0, le=1440)
    earliest_arrival: datetime | None = None
    deadline: datetime | None = None
    required: bool = True
    min_rating: float | None = Field(default=None, ge=0, le=5)
    max_cost_yuan: float | None = Field(default=None, ge=0)
    require_open: bool = True
    require_wheelchair_accessible: bool = False
    appointment_time: datetime | None = None


class PartyProfile(StrictModel):
    adults: int = Field(default=1, ge=0, le=50)
    elderly: int = Field(default=0, ge=0, le=50)
    children: int = Field(default=0, ge=0, le=50)
    wheelchair_users: int = Field(default=0, ge=0, le=20)
    pets: int = Field(default=0, ge=0, le=20)
    has_luggage: bool = False


class HardConstraints(StrictModel):
    latest_return_time: datetime | None = None
    max_walking_meters: float | None = Field(default=None, ge=0)
    max_total_duration_minutes: int | None = Field(default=None, ge=0, le=10080)
    max_total_cost_yuan: float | None = Field(default=None, ge=0)
    must_return_to_origin: bool = False
    required_task_order: list[int] = Field(default_factory=list)
    max_detour_meters: float | None = Field(default=None, ge=0)
    avoid_areas: list[str] = Field(default_factory=list, max_length=20)
    must_pass_areas: list[str] = Field(default_factory=list, max_length=20)
    allowed_districts: list[str] = Field(default_factory=list, max_length=20)
    wheelchair_accessible: bool = False
    party: PartyProfile = Field(default_factory=PartyProfile)
    transport_budget_yuan: float | None = Field(default=None, ge=0)
    dining_budget_yuan: float | None = Field(default=None, ge=0)
    ticket_budget_yuan: float | None = Field(default=None, ge=0)


class UncertainConstraint(StrictModel):
    field: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=300)
    confidence: float = Field(ge=0, le=1)
    safety_buffer_minutes: int = Field(default=0, ge=0, le=240)


class TripConstraintSet(StrictModel):
    hard: HardConstraints = Field(default_factory=HardConstraints)
    uncertain: list[UncertainConstraint] = Field(default_factory=list)


class PlanningIntent(StrictModel):
    origin: str | None = Field(default=None, max_length=200)
    departure_time: datetime | None = None
    transport_mode: TransportMode = TransportMode.walking
    tasks: list[PlanningTask] = Field(min_length=1, max_length=10)
    preferences: PlanningPreferences = Field(default_factory=PlanningPreferences)
    constraints: TripConstraintSet = Field(default_factory=TripConstraintSet)


class AIPlanRequest(StrictModel):
    text: str = Field(min_length=2, max_length=4000)
    origin: Coordinate | None = None
    departure_time: datetime | None = None
    transport_mode: TransportMode | None = None
    default_service_duration_minutes: int = Field(default=15, ge=0, le=240)
    city: str | None = Field(default=None, max_length=50)
    constraints: TripConstraintSet | None = None
    max_candidates_per_task: int = Field(default=3, ge=1, le=5)


class PoiCandidate(StrictModel):
    id: str
    name: str
    address: str = ""
    location: Coordinate
    rating: float | None = None
    distance_meters: float | None = None
    source: str = "unknown"
    data_updated_at: datetime | None = None
    confidence: float = Field(default=0.7, ge=0, le=1)
    district: str | None = None
    open_now: bool | None = None
    wheelchair_accessible: bool | None = None
    estimated_cost_yuan: float | None = Field(default=None, ge=0)


class RouteEdge(StrictModel):
    origin_index: int = Field(ge=0)
    destination_index: int = Field(ge=0)
    duration_seconds: float = Field(ge=0)
    distance_meters: float = Field(ge=0)
    source: str
    quality: DataQuality
    traffic_timestamp: datetime | None = None
    confidence: float = Field(ge=0, le=1)
    fallback_used: bool = False


class RouteMatrix(StrictModel):
    edges: list[list[RouteEdge]]
    provider: str
    generated_at: datetime

    @property
    def distances(self) -> list[list[float]]:
        return [[edge.distance_meters for edge in row] for row in self.edges]

    @property
    def durations(self) -> list[list[float]]:
        return [[edge.duration_seconds for edge in row] for row in self.edges]


class PlannedStop(StrictModel):
    task_index: int
    candidate_rank: int
    task: PlanningTask
    poi: PoiCandidate
    arrival_time: datetime
    departure_time: datetime
    travel: RouteEdge
    constraint_satisfied: bool

    @property
    def travel_seconds(self) -> float:
        return self.travel.duration_seconds

    @property
    def travel_meters(self) -> float:
        return self.travel.distance_meters


class ClarificationQuestion(StrictModel):
    field: str
    reason: str
    question: str
    required: bool = True
    candidates: list[PoiCandidate] = Field(default_factory=list)


class ScoreBreakdown(StrictModel):
    travel_time: float = 0
    walking_time: float = 0
    distance: float = 0
    low_rating: float = 0
    uncertainty: float = 0
    monetary_cost: float = 0
    total: float = 0


class UncertaintySummary(StrictModel):
    expected_duration_seconds: float = Field(ge=0)
    lower_duration_seconds: float = Field(ge=0)
    upper_duration_seconds: float = Field(ge=0)
    on_time_probability: float | None = Field(default=None, ge=0, le=1)
    method: str


class AIPlanResult(StrictModel):
    status: Literal["success", "need_clarification", "infeasible"]
    planning_state: PlanningState
    intent: PlanningIntent
    origin: Coordinate | None = None
    departure_time: datetime | None = None
    stops: list[PlannedStop] = Field(default_factory=list)
    total_distance_meters: float = 0
    total_travel_seconds: float = 0
    algorithm: str | None = None
    explanation: str | None = None
    questions: list[ClarificationQuestion] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    score: ScoreBreakdown | None = None
    confidence: float = Field(default=0, ge=0, le=1)
    candidate_count: int = 0
    uncertainty: UncertaintySummary | None = None


class PlanPatchOperation(StrictModel):
    operation: Literal["remove_stop", "move_stop"]
    stop_id: str | None = None
    from_position: int | None = Field(default=None, ge=0)
    to_position: int | None = Field(default=None, ge=0)


class CreatePlanPatchRequest(StrictModel):
    base_version: int = Field(ge=1)
    operations: list[PlanPatchOperation] = Field(min_length=1, max_length=20)
    reason: str = Field(min_length=3, max_length=500)
    impact: dict[str, Any] = Field(default_factory=dict)


class DecidePlanPatchRequest(StrictModel):
    accept: bool


class ContinuePlanningConversationRequest(StrictModel):
    base_revision: int = Field(ge=1)
    answers: dict[str, Any] = Field(min_length=1, max_length=20)
