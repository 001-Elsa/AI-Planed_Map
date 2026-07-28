from typing import Any


class AppError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: Any | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}


class UpstreamError(AppError):
    def __init__(self, message: str, details: Any | None = None) -> None:
        super().__init__(502, "UPSTREAM_SERVICE_ERROR", message, details)

