import hashlib
import hmac
import secrets
import time
from pathlib import Path

from django.conf import settings


def bridge_secret_path() -> Path:
    return Path(settings.DATA_DIR) / "qq-bridge.key"


def ensure_bridge_secret() -> str:
    """本机生成共享密钥，不写入版本库。 / Generate a local shared secret outside version control."""

    path = bridge_secret_path()
    try:
        with path.open("x", encoding="utf-8") as stream:
            stream.write(secrets.token_urlsafe(48))
    except FileExistsError:
        pass
    return path.read_text(encoding="utf-8").strip()


def sign_payload(timestamp: str, body: bytes, secret: str | None = None) -> str:
    key = (secret or ensure_bridge_secret()).encode("utf-8")
    return hmac.new(key, timestamp.encode("ascii") + b"." + body, hashlib.sha256).hexdigest()


def verify_payload(timestamp: str, signature: str, body: bytes) -> bool:
    try:
        sent = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs(int(time.time()) - sent) > 300:
        return False
    expected = sign_payload(timestamp, body)
    return hmac.compare_digest(expected, signature or "")
