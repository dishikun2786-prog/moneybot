import time
import bcrypt
from itsdangerous import URLSafeTimedSerializer
from . import config

_fails = []


def _serializer():
    return URLSafeTimedSerializer(open(config.SECRET_FILE).read().strip())


def check_password(pw: str) -> bool:
    h = open(config.PASSWD_FILE, "rb").read()
    return bcrypt.checkpw(pw.encode(), h)


def login_allowed() -> bool:
    global _fails
    now = time.time()
    _fails = [t for t in _fails if now - t < 60]
    return len(_fails) < config.LOGIN_RATE_LIMIT


def record_fail():
    _fails.append(time.time())


def make_session() -> str:
    return _serializer().dumps({"ok": True})


def valid_session(cookie) -> bool:
    if not cookie:
        return False
    try:
        return bool(_serializer().loads(cookie, max_age=86400).get("ok"))
    except Exception:
        return False
