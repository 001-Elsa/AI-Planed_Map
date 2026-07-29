from backend.app.services.offroute import distance_to_polyline, evaluate_off_route


def test_distance_to_polyline_and_sustained_off_route():
    polyline = [(116.40, 39.90), (116.41, 39.90), (116.42, 39.90)]
    near = distance_to_polyline(116.405, 39.9001, polyline)
    assert near < 30
    far = distance_to_polyline(116.405, 40.0, polyline)
    assert far > 500

    first = evaluate_off_route(
        lng=116.405,
        lat=40.0,
        polyline=polyline,
        previous_off_route_seconds=0,
        sample_interval_seconds=10,
        sustain_seconds=20,
        threshold_meters=80,
    )
    assert first.off_route is False
    assert first.sustained_seconds == 10

    second = evaluate_off_route(
        lng=116.405,
        lat=40.0,
        polyline=polyline,
        previous_off_route_seconds=first.sustained_seconds,
        sample_interval_seconds=10,
        sustain_seconds=20,
        threshold_meters=80,
    )
    assert second.off_route is True
