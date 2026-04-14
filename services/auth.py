import hashlib
import json
import secrets
from datetime import datetime, timedelta
from typing import Optional

from core.config import PilipiliConfig


# ============================================================
# 密码哈希（使用 PBKDF2 + SHA256）
# ============================================================

def hash_password(password: str, salt: Optional[str] = None) -> str:
    """密码哈希 - PBKDF2 + SHA256"""
    if salt is None:
        salt = secrets.token_hex(32)

    # PBKDF2-HMAC-SHA256
    key = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        100000,  # 迭代次数
    )
    return f"{salt}${key.hex()}"


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """验证密码"""
    try:
        salt, key_hash = hashed_password.split("$")
        computed_key = hashlib.pbkdf2_hmac(
            "sha256", plain_password.encode("utf-8"), salt.encode("utf-8"), 100000
        )
        return computed_key.hex() == key_hash
    except Exception:
        return False


# ============================================================
# JWT Token 管理
# ============================================================


def _base64_url_encode(data: bytes) -> str:
    """Base64 URL 编码"""
    import base64

    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("utf-8")


def _base64_url_decode(data: str) -> bytes:
    """Base64 URL 解码"""
    import base64

    padding = 4 - (len(data) % 4)
    if padding != 4:
        data += "=" * padding
    return base64.urlsafe_b64decode(data)


def create_jwt_token(user_id: str, username: str, config: PilipiliConfig) -> tuple[str, int]:
    """创建 JWT Token

    返回: (token, expires_in_seconds)
    """
    if not config.auth.jwt_secret:
        # 自动生成密钥（首次启动时）
        config.auth.jwt_secret = secrets.token_urlsafe(32)

    # Header
    header = {"alg": "HS256", "typ": "JWT"}
    header_b64 = _base64_url_encode(json.dumps(header).encode())

    # Payload
    now = datetime.now()
    exp = now + timedelta(hours=config.auth.jwt_expire_hours)
    payload = {
        "sub": user_id,
        "name": username,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
    }
    payload_b64 = _base64_url_encode(json.dumps(payload).encode())

    # Signature
    message = f"{header_b64}.{payload_b64}"
    signature = hmac_sha256(config.auth.jwt_secret, message)
    signature_b64 = _base64_url_encode(signature)

    token = f"{header_b64}.{payload_b64}.{signature_b64}"
    expires_in = config.auth.jwt_expire_hours * 3600

    return token, expires_in


def hmac_sha256(key: str, message: str) -> bytes:
    """HMAC-SHA256"""
    import hmac

    return hmac.new(key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).digest()


def verify_jwt_token(token: str, config: PilipiliConfig) -> Optional[dict]:
    """验证 JWT Token

    返回: payload dict 或 None（如果无效）
    """
    if not config.auth.jwt_secret:
        return None

    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None

        header_b64, payload_b64, signature_b64 = parts

        # 验证签名
        message = f"{header_b64}.{payload_b64}"
        expected_signature = hmac_sha256(config.auth.jwt_secret, message)
        expected_signature_b64 = _base64_url_encode(expected_signature)

        if signature_b64 != expected_signature_b64:
            return None

        # 解析 payload
        payload = json.loads(_base64_url_decode(payload_b64))

        # 检查过期
        if payload.get("exp", 0) < datetime.now().timestamp():
            return None

        return payload

    except Exception:
        return None

