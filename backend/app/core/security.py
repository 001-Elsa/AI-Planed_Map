import asyncio
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone


def _derive(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=16384,
        r=8,
        p=1,
        dklen=64,
    )


async def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = await asyncio.to_thread(_derive, password, salt)
    return f"scrypt${salt.hex()}${digest.hex()}"


async def verify_password(password: str, stored: str) -> bool:
    try:
        if stored.startswith("scrypt$"):
            _, salt_hex, digest_hex = stored.split("$", 2)
        else:
            # Backward compatible with the original Node.js salt:hash format.
            salt_hex, digest_hex = stored.split(":", 1)
        calculated = await asyncio.to_thread(_derive, password, bytes.fromhex(salt_hex))
        return hmac.compare_digest(calculated, bytes.fromhex(digest_hex))
    except (ValueError, TypeError):
        return False


def new_session_token() -> str:
    return secrets.token_hex(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def expires_at(days: int) -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=days)

