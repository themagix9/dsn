"""Dashboard-Login: bcrypt-Passwörter, TOTP-2FA, signierte Session-Cookies (clusterweit gültig)."""
from __future__ import annotations

import time
from collections import defaultdict, deque

import bcrypt
import pyotp
from itsdangerous import BadSignature, URLSafeTimedSerializer

SESSION_COOKIE = "dsn_session"
SESSION_MAX_AGE = 12 * 3600


def hash_password(pw: str) -> str:
    if len(pw) < 10:
        raise ValueError("Passwort muss mindestens 10 Zeichen haben")
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()


def check_password(pw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode(), hashed.encode())
    except ValueError:
        return False


def new_totp_secret() -> str:
    return pyotp.random_base32()


def totp_uri(secret: str, username: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name="DSN Cluster")


def check_totp(secret: str, code: str) -> bool:
    return bool(code) and pyotp.TOTP(secret).verify(code.strip().replace(" ", ""), valid_window=1)


class Sessions:
    def __init__(self, secret: str):
        self.s = URLSafeTimedSerializer(secret, salt="dsn-session")

    def issue(self, username: str) -> str:
        return self.s.dumps({"u": username})

    def read(self, token: str | None) -> str | None:
        if not token:
            return None
        try:
            return self.s.loads(token, max_age=SESSION_MAX_AGE)["u"]
        except (BadSignature, KeyError, TypeError):
            return None


class RateLimiter:
    """Max. `limit` Fehlversuche pro `window` Sekunden je IP."""

    def __init__(self, limit: int = 8, window: int = 600):
        self.limit, self.window = limit, window
        self.fails: dict[str, deque] = defaultdict(deque)

    def blocked(self, key: str) -> bool:
        q = self.fails[key]
        while q and time.time() - q[0] > self.window:
            q.popleft()
        return len(q) >= self.limit

    def fail(self, key: str):
        self.fails[key].append(time.time())
