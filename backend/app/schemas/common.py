from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ApiEnvelope(BaseModel):
    ok: bool = True
    data: Any = None


class ErrorEnvelope(BaseModel):
    ok: bool = False
    code: str
    msg: str
    request_id: str
    details: dict[str, Any] = Field(default_factory=dict)
