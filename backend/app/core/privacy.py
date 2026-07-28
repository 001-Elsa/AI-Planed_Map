import base64
import hashlib
import json

from cryptography.fernet import Fernet, InvalidToken

from backend.app.core.config import get_settings
from backend.app.core.exceptions import AppError


def _cipher() -> Fernet:
    secret = get_settings().location_encryption_key
    if not secret:
        raise AppError(
            503,
            "LOCATION_ENCRYPTION_NOT_CONFIGURED",
            "精确位置加密密钥尚未配置",
        )
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
    return Fernet(key)


def encrypt_location(longitude: float, latitude: float) -> str:
    payload = json.dumps(
        {"lng": longitude, "lat": latitude},
        separators=(",", ":"),
    ).encode("utf-8")
    return _cipher().encrypt(payload).decode("ascii")


def decrypt_location(encrypted_payload: str) -> tuple[float, float]:
    try:
        data = json.loads(_cipher().decrypt(encrypted_payload.encode("ascii")))
        return float(data["lng"]), float(data["lat"])
    except (InvalidToken, ValueError, KeyError, TypeError) as exc:
        raise AppError(
            500,
            "LOCATION_DECRYPTION_FAILED",
            "位置数据无法解密，请检查密钥版本",
        ) from exc


def read_location(snapshot) -> tuple[float, float]:
    if snapshot.encrypted_payload:
        return decrypt_location(snapshot.encrypted_payload)
    if snapshot.longitude is None or snapshot.latitude is None:
        raise AppError(500, "LOCATION_DATA_INVALID", "位置记录不完整")
    return float(snapshot.longitude), float(snapshot.latitude)
