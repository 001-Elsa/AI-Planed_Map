from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import Field

from backend.app.schemas.ai_intent import Coordinate, StrictModel


class TripState(str, Enum):
    idle = "IDLE"
    discovering = "DISCOVERING"
    clarifying = "CLARIFYING"
    planning = "PLANNING"
    plan_ready = "PLAN_READY"
    active_trip = "ACTIVE_TRIP"
    paused = "PAUSED"
    off_route = "OFF_ROUTE"
    at_risk = "AT_RISK"
    replanning = "REPLANNING"
    completed = "COMPLETED"
    cancelled = "CANCELLED"


class TripEventType(str, Enum):
    location_updated = "LocationUpdated"
    schedule_delay = "ScheduleDelayDetected"
    traffic_changed = "TrafficChanged"
    weather_alert = "WeatherAlertReceived"
    poi_status_changed = "PoiStatusChanged"
    user_off_route = "UserOffRoute"
    deadline_risk = "DeadlineRiskDetected"
    stop_completed = "PlanStopCompleted"
    stop_skipped = "PlanStopSkipped"
    user_paused = "UserPausedTrip"
    user_resumed = "UserResumedTrip"
    trip_completed = "TripCompleted"


class ConsentScope(str, Enum):
    precise_location = "precise_location"
    background_location = "background_location"
    share_location = "share_location"
    save_preference = "save_preference"
    contact_person = "contact_person"


class CreateTripSessionRequest(StrictModel):
    planning_run_id: int = Field(gt=0)
    reminder_cooldown_minutes: int = Field(default=15, ge=1, le=240)


class ConsentRequest(StrictModel):
    scope: ConsentScope
    granted: bool
    expires_at: datetime | None = None


class LocationUpdateRequest(StrictModel):
    event_id: str = Field(min_length=8, max_length=100)
    location: Coordinate
    accuracy_meters: float = Field(ge=0, le=10000)
    captured_at: datetime


class TripEventRequest(StrictModel):
    event_id: str = Field(min_length=8, max_length=100)
    type: TripEventType
    occurred_at: datetime
    payload: dict[str, Any] = Field(default_factory=dict)


class TripTransitionRequest(StrictModel):
    target_state: TripState
    reason: str = Field(min_length=2, max_length=300)


class ExplicitPreferenceRequest(StrictModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    value: Any
    confirmed: bool


class AgentActionProposal(StrictModel):
    action: str
    tool: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    reason: str
    requires_confirmation: bool
    risk_level: str


class ExecuteAgentToolRequest(StrictModel):
    tool: str = Field(min_length=2, max_length=100)
    arguments: dict[str, Any] = Field(default_factory=dict)
    confirmed: bool = False


class PrivacyPurgeRequest(StrictModel):
    confirmation: str


class PreTripCheckRequest(StrictModel):
    location: Coordinate


class ReplanTripRequest(StrictModel):
    current_location: Coordinate
    current_time: datetime
    completed_stop_ids: list[str] = Field(default_factory=list, max_length=20)
    reason: str = Field(min_length=3, max_length=300)
