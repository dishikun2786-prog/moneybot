"""会话认证 (M1 多用户版)
- itsdangerous 签名会话, payload {u: uid, r: role}, 24h 过期
- 旧版单用户会话 ({ok:True}) 向后兼容 → 映射为迁移后的 admin (uid=1)
- 改密轮换签名密钥 → 吊销全部会话
"""
import os
import secrets
import time
from itsdangerous import URLSafeTimedSerializer
from . import config
from . import users

_fails = []  # 每 IP 5次/分钟 (登录端点级限流)
_SECRET = None
_SECRET_MTIME = 0


def _serializer():
    global _SECRET, _SECRET_MTIME
    try:
        mt = os.stat(config.SECRET_FILE).st_mtime_ns
    except OSError:
        mt = 0
    if _SECRET is None or mt != _SECRET_MTIME:
        with open(config.SECRET_FILE) as f:
            _SECRET = f.read().strip()
        _SECRET_MTIME = mt
    return URLSafeTimedSerializer(_SECRET)


def make_session(uid, role):
    return _serializer().dumps({"u": int(uid), "r": role})


def session_user(cookie):
    """返回 {'u': uid, 'r': role} 或 None"""
    if not cookie:
        return None
    try:
        p = _serializer().loads(cookie, max_age=86400)
    except Exception:
        return None
    if isinstance(p, dict) and "u" in p:
        try:
            return {"u": int(p["u"]), "r": p.get("r", "user")}
        except (TypeError, ValueError):
            return None
    if isinstance(p, dict) and p.get("ok"):
        # 旧版单用户会话 → 迁移后的 admin
        return {"u": 1, "r": "admin"}
    return None


def valid_session(cookie):
    return session_user(cookie) is not None


def login_allowed():
    global _fails
    now = time.time()
    _fails = [t for t in _fails if now - t < 60]
    return len(_fails) < config.LOGIN_RATE_LIMIT


def record_fail():
    _fails.append(time.time())


def change_password(uid, old_pw, new_pw):
    """校验旧密码+强度; 成功则写新哈希并轮换签名密钥(吊销所有旧会话)"""
    global _SECRET, _SECRET_MTIME
    ok, msg = users.change_password(uid, old_pw, new_pw)
    if not ok:
        return False, msg
    with open(config.SECRET_FILE, "w") as f:
        f.write(secrets.token_hex(32))
    os.chmod(config.SECRET_FILE, 0o600)
    _SECRET = None
    _SECRET_MTIME = 0
    return True, "ok"
