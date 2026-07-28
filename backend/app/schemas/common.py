from typing import Any

from pydantic import BaseModel, Field


class ApiEnvelope(BaseModel):
    ok: bool = True
    data: Any = None


class ErrorEnvelope(BaseModel):
    ok: bool = False
    code: str
    msg: str
    request_id: str
    details: dict[str, Any] = Field(default_factory=dict)

