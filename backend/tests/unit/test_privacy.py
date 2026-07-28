from backend.app.core.privacy import decrypt_location, encrypt_location


def test_precise_location_is_encrypted_and_round_trips():
    payload = encrypt_location(120.6196, 31.2994)
    assert "120.6196" not in payload
    lng, lat = decrypt_location(payload)
    assert lng == 120.6196
    assert lat == 31.2994
