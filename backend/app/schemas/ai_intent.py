from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from backend.app.schemas.agent_artifacts import AgentWorkflowTrace, ReviewReport
from backend.app.schemas.common import StrictModel

MAX_PLANNING_TASKS = 24
HUMAN_CONFIRMATION_KEYS = frozenset(
    {
        "walking_distance",
        "estimated_cost",
    }
)


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
    optimization_goal: Literal["balanced", "shortest_time", "shortest_distance"] = "balanced"
    travel_style: Literal["balanced", "relaxed", "intensive"] = "balanced"
    minimize_distance: bool = False
    minimize_walking: bool = False
    minimize_cost: bool = False
    prefer_high_rating: bool = False
    # Kept on the formal intent so a clarification answer is visible to both
    # candidate recall and the auditable final plan snapshot.
    dietary_restrictions: list[str] = Field(default_factory=list, max_length=20)
    # Confirmed long-term discovery hints are soft recall preferences. They do
    # not add mandatory stops or bypass Provider evidence.
    preferred_categories: list[str] = Field(default_factory=list, max_length=10)
    preferred_environment: list[Literal["quiet", "uncrowded", "indoor", "outdoor"]] = Field(
        default_factory=list, max_length=4
    )
    avoid_queues: bool = False
    avoid_hiking: bool = False
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
    tasks: list[PlanningTask] = Field(min_length=1, max_length=MAX_PLANNING_TASKS)
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
    # Structured conversation answers.  These are deliberately part of the
    # request model (rather than opaque persisted extras) so every retry runs
    # the same planner input.
    task_poi_overrides: dict[str, str] = Field(default_factory=dict)
    task_location_overrides: dict[str, str] = Field(default_factory=dict)
    task_field_overrides: dict[str, dict[str, Any]] = Field(default_factory=dict)
    preferences_answers: dict[str, Any] = Field(default_factory=dict)
    human_confirmations: dict[str, bool] = Field(default_factory=dict)
    use_long_term_memory: bool = True

    @field_validator("preferences_answers")
    @classmethod
    def validate_preference_answers(cls, values: dict[str, Any]) -> dict[str, Any]:
        values = dict(values)
        boolean_keys = {
            "minimize_distance",
            "minimize_walking",
            "minimize_cost",
            "prefer_high_rating",
            "avoid_queues",
            "avoid_hiking",
        }
        allowed = boolean_keys | {
            "optimization_goal",
            "dietary_restrictions",
            "preferred_categories",
            "preferred_environment",
            "travel_style",
        }
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(f"unsupported preference answers: {sorted(unknown)}")
        for key in boolean_keys & set(values):
            if not isinstance(values[key], bool):
                raise ValueError(f"{key} must be boolean")
        goal = values.get("optimization_goal")
        if goal is not None and goal not in {"balanced", "shortest_time", "shortest_distance"}:
            raise ValueError("unsupported optimization_goal")
        travel_style = values.get("travel_style")
        if travel_style is not None and travel_style not in {
            "balanced",
            "relaxed",
            "intensive",
        }:
            raise ValueError("unsupported travel_style")
        for key, limit in (("dietary_restrictions", 20), ("preferred_categories", 10)):
            if key not in values:
                continue
            if isinstance(values[key], str):
                values[key] = [values[key]]
            items = values[key]
            if (
                not isinstance(items, list)
                or len(items) > limit
                or any(
                    not isinstance(item, str) or not item.strip() or len(item) > 50
                    for item in items
                )
            ):
                raise ValueError(f"invalid {key}")
        environments = values.get("preferred_environment")
        if isinstance(environments, str):
            environments = [environments]
            values["preferred_environment"] = environments
        if environments is not None and (
            not isinstance(environments, list)
            or len(environments) > 4
            or any(item not in {"quiet", "uncrowded", "indoor", "outdoor"} for item in environments)
        ):
            raise ValueError("invalid preferred_environment")
        return values

    @field_validator("human_confirmations")
    @classmethod
    def validate_human_confirmations(cls, values: dict[str, bool]) -> dict[str, bool]:
        values = dict(values)
        unknown = set(values) - HUMAN_CONFIRMATION_KEYS
        if unknown:
            raise ValueError(f"unsupported human confirmations: {sorted(unknown)}")
        for key, value in values.items():
            if not isinstance(value, bool):
                raise ValueError(f"{key} must be boolean")
        return values


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
    kind: Literal["clarification", "confirmation"] = "clarification"
    required: bool = True
    candidates: list[PoiCandidate] = Field(default_factory=list)


class ScoreBreakdown(StrictModel):
    travel_time: float = 0
    walking_time: float = 0
    distance: float = 0
    low_rating: float = 0
    uncertainty: float = 0
    monetary_cost: float = 0
    change: float = 0
    total: float = 0


class UncertaintySummary(StrictModel):
    expected_duration_seconds: float = Field(ge=0)
    lower_duration_seconds: float = Field(ge=0)
    upper_duration_seconds: float = Field(ge=0)
    on_time_probability: float | None = Field(default=None, ge=0, le=1)
    method: str
    calibration_sample_size: int = 0
    mae_seconds: float | None = None
    p90_error_seconds: float | None = None
    coverage: float | None = Field(default=None, ge=0, le=1)


class CandidateReview(StrictModel):
    task_index: int = Field(ge=0)
    task_description: str
    considered_count: int = Field(ge=0)
    selected_poi_id: str | None = None
    candidates: list[PoiCandidate] = Field(default_factory=list)


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
    candidate_reviews: list[CandidateReview] = Field(default_factory=list)
    uncertainty: UncertaintySummary | None = None
    critic_review: ReviewReport | None = None
    agent_workflow: AgentWorkflowTrace | None = None


class PlanPatchOperation(StrictModel):
    operation: Literal[
        "remove_stop",
        "move_stop",
        "replace_stop",
        "change_transport_mode",
        "change_departure_time",
    ]
    stop_id: str | None = None
    from_position: int | None = Field(default=None, ge=0)
    to_position: int | None = Field(default=None, ge=0)
    replacement_stop: dict[str, Any] | None = None
    transport_mode: TransportMode | None = None
    departure_time: datetime | None = None

    @model_validator(mode="after")
    def validate_operation_arguments(self) -> "PlanPatchOperation":
        if self.operation in {"remove_stop", "replace_stop"} and not self.stop_id:
            raise ValueError(f"{self.operation} requires stop_id")
        if self.operation == "move_stop" and (
            self.from_position is None or self.to_position is None
        ):
            raise ValueError("move_stop requires from_position and to_position")
        if self.operation == "replace_stop" and not self.replacement_stop:
            raise ValueError("replace_stop requires replacement_stop")
        if self.operation == "change_transport_mode" and self.transport_mode is None:
            raise ValueError("change_transport_mode requires transport_mode")
        if self.operation == "change_departure_time" and (
            self.departure_time is None or self.departure_time.tzinfo is None
        ):
            raise ValueError("change_departure_time requires a timezone-aware departure_time")
        return self


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
