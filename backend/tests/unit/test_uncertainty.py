from backend.app.services.uncertainty import calibrate_from_history, heuristic_envelope


def test_heuristic_and_calibrated_envelopes():
    base = heuristic_envelope(
        expected_seconds=1800,
        mean_confidence=0.8,
        fallback_used=True,
        safety_buffer_minutes=10,
        has_deadline=True,
    )
    assert base.upper_seconds > base.expected_seconds
    assert base.on_time_probability == 0.8
    assert any("安全缓冲" in item for item in base.warnings)

    history = [
        {"predicted_seconds": 1000, "actual_seconds": 1100, "transport_mode": "walking", "hour": 10}
        for _ in range(10)
    ]
    calibrated = calibrate_from_history(
        expected_seconds=1000,
        mean_confidence=0.9,
        fallback_used=False,
        history=history,
        has_deadline=True,
    )
    assert calibrated.method.startswith("historical-residual")
    assert calibrated.on_time_probability is not None
