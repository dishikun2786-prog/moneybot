import os
import secrets
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


def change_password(old_pw: str, new_pw: str):
    """校验旧密码+强度; 成功则写新哈希并轮换签名密钥(吊销所有旧会话)
    返回 (ok, msg)"""
    if not check_password(old_pw):
        return False, "当前密码错误"
    if len(new_pw) < 8:
        return False, "新密码至少8位"
    if new_pw == old_pw:
        return False, "新密码不能与旧密码相同"
    h = bcrypt.hashpw(new_pw.encode(), bcrypt.gensalt())
    with open(config.PASSWD_FILE, "wb") as f:
        f.write(h)
    os.chmod(config.PASSWD_FILE, 0o600)
    with open(config.SECRET_FILE, "w") as f:
        f.write(secrets.token_hex(32))
    os.chmod(config.SECRET_FILE, 0o600)
    return True, "ok"
