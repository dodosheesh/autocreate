"""Auth sans dépendance externe : hachage de mot de passe PBKDF2-HMAC-SHA256
et jetons de session signés (HMAC), style itsdangerous.

Suffisant pour un outil interne mono/quelques utilisateurs. Pour un vrai
multi-tenant public, passer à argon2 + rotation de clé.
"""

import base64
import hashlib
import hmac
import json
import secrets
import time

from app.config import get_settings

_PBKDF2_ROUNDS = 240_000
_ALGO = "pbkdf2_sha256"


# ---------- mot de passe ----------


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ROUNDS)
    return f"{_ALGO}${_PBKDF2_ROUNDS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, rounds, salt_hex, hash_hex = stored.split("$")
        if algo != _ALGO:
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), int(rounds)
        )
        return hmac.compare_digest(dk.hex(), hash_hex)
    except (ValueError, AttributeError):
        return False


# Hash factice au vrai coût (240k rounds) : le login le vérifie quand l'email
# n'existe pas, pour que la latence soit identique à un compte réel → pas
# d'énumération de comptes par timing. Calculé une fois au chargement.
DUMMY_PASSWORD_HASH = hash_password("autocreate-dummy-password-never-matches")


# ---------- jeton de session signé ----------


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64d(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _sign(payload_b64: str) -> str:
    key = get_settings().secret_key.encode()
    return _b64e(hmac.new(key, payload_b64.encode(), hashlib.sha256).digest())


def issue_session(user_id: str, token_version: int = 0, *, now: int | None = None) -> str:
    """Jeton `payload.signature` ; payload = user_id + version + expiration.
    La version lie le jeton à User.token_version pour la révocation."""
    now = int(time.time()) if now is None else now
    payload = {
        "uid": user_id,
        "ver": token_version,
        "exp": now + get_settings().session_ttl_s,
    }
    payload_b64 = _b64e(json.dumps(payload, separators=(",", ":")).encode())
    return f"{payload_b64}.{_sign(payload_b64)}"


def read_session(token: str, *, now: int | None = None) -> dict | None:
    """Renvoie le payload {uid, ver, exp} si le jeton est valide et non
    expiré, sinon None. L'appelant compare `ver` à User.token_version."""
    if not token or "." not in token:
        return None
    payload_b64, sig = token.rsplit(".", 1)
    if not hmac.compare_digest(sig, _sign(payload_b64)):
        return None
    try:
        payload = json.loads(_b64d(payload_b64))
    except (ValueError, json.JSONDecodeError):
        return None
    now = int(time.time()) if now is None else now
    if payload.get("exp", 0) < now:
        return None
    if "uid" not in payload:
        return None
    return payload
