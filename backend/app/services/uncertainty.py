"""Heuristic confidence helpers with optional historical calibration stats."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class ConfidenceEnvelope:
    expected_seconds: float
    lower_seconds: float
    upper_seconds: float
    on_time_probability: float | None
    method: str
    warnings: list[str]


def heuristic_envelope(
    *,
    expected_seconds: float,
    mean_confidence: float,
    fallback_used: bool,
    safety_buffer_minutes: int = 0,
    has_deadline: bool = False,
) -> ConfidenceEnvelope:
    confidence = max(0.05, min(1.0, mean_confidence))
    spread = expected_seconds * (1.0 - confidence) * (1.35 if fallback_used else 1.0)
    buffer = safety_buffer_minutes * 60
    lower = max(0.0, expected_seconds - spread)
    upper = expected_seconds + spread + buffer
    probability = confidence if has_deadline else None
    warnings: list[str] = []
    if fallback_used:
        warnings.append("部分路段使用估计距离，置信度已下调")
    if safety_buffer_minutes:
        warnings.append(f"已叠加不确定约束安全缓冲 {safety_buffer_minutes} 分钟")
    return ConfidenceEnvelope(
        expected_seconds=expected_seconds,
        lower_seconds=lower,
        upper_seconds=upper,
        on_time_probability=probability,
        method="provider-confidence-safety-envelope-v2",
        warnings=warnings,
    )


def calibrate_from_history(
    *,
    expected_seconds: float,
    mean_confidence: float,
    fallback_used: bool,
    history: list[dict[str, Any]],
    safety_buffer_minutes: int = 0,
    has_deadline: bool = False,
) -> ConfidenceEnvelope:
    """Blend heuristic envelope with empirical residual stats when available.

    History items: {predicted_seconds, actual_seconds, transport_mode, hour}.
    This is not a full probabilistic model; it reports calibrated coverage when
    enough samples exist, otherwise falls back to the heuristic envelope.
    """
    base = heuristic_envelope(
        expected_seconds=expected_seconds,
        mean_confidence=mean_confidence,
        fallback_used=fallback_used,
        safety_buffer_minutes=safety_buffer_minutes,
        has_deadline=has_deadline,
    )
    if len(history) < 8:
        return base

    residuals = [
        float(item["actual_seconds"]) - float(item["predicted_seconds"]) for item in history
    ]
    mae = sum(abs(value) for value in residuals) / len(residuals)
    sorted_abs = sorted(abs(value) for value in residuals)
    p90 = sorted_abs[min(len(sorted_abs) - 1, int(math.ceil(0.9 * len(sorted_abs)) - 1))]
    lower = max(0.0, expected_seconds - p90)
    upper = expected_seconds + p90 + safety_buffer_minutes * 60
    coverage = sum(
        1
        for item in history
        if abs(float(item["actual_seconds"]) - float(item["predicted_seconds"])) <= p90
    ) / len(history)
    probability = None
    if has_deadline:
        # Conservative blend: never claim higher than heuristic mean confidence.
        probability = min(mean_confidence, coverage)
    warnings = list(base.warnings)
    warnings.append(
        f"基于 {len(history)} 条历史 ETA 残差校准：MAE={mae:.0f}s，P90={p90:.0f}s，覆盖率={coverage:.0%}"
    )
    return ConfidenceEnvelope(
        expected_seconds=expected_seconds,
        lower_seconds=lower,
        upper_seconds=upper,
        on_time_probability=probability,
        method="historical-residual-calibration-v1",
        warnings=warnings,
    )


def build_eta_observation(
    *,
    trip_id: int,
    stop_id: str,
    predicted_seconds: float,
    predicted_arrival: datetime,
    transport_mode: str,
) -> dict[str, Any]:
    return {
        "trip_id": trip_id,
        "stop_id": stop_id,
        "predicted_seconds": predicted_seconds,
        "predicted_arrival": predicted_arrival.astimezone(timezone.utc).isoformat(),
        "transport_mode": transport_mode,
        "hour": predicted_arrival.astimezone(timezone.utc).hour,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
