"""密钥保险库 (M4 商业化升级)
- user_keys 表: 密钥 AES-256-GCM 加密存储 (主密钥 = SHA256(签名密钥 .dash_secret) 派生)
- 掩码展示 (前4后4), 明文永不返回 (仅 bind 时进入进程内存)
- 连通性测试: Bybit 双权限校验 (现货+合约) / PM 凭证校验
- user_limits 表: 用户实盘风控限额 (管理员配置)
安全底线: 解密只发生在 测试/下单 瞬间; 密钥永不写日志/审计
"""
import base64
import hashlib
import json
import os
import sqlite3
import threading
import time

from . import config

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    _HAS_AES = True
except Exception:
    _HAS_AES = False

DB_FILE = os.environ.get("KEYS_DB", os.path.join(config.BASE, "dash", "keys.db"))
_lock = threading.RLock()

DEFAULT_LIMITS = {"max_notional": 20.0, "daily_loss_cap": 5.0,
                  "max_positions": 3, "live_enabled": 0}


def _db():
    os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
    con = sqlite3.connect(DB_FILE)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with _lock:
        con = _db()
        try:
            con.executescript("""
            CREATE TABLE IF NOT EXISTS user_keys(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              uid INTEGER NOT NULL,
              venue TEXT NOT NULL,
              label TEXT NOT NULL DEFAULT '',
              key_enc TEXT NOT NULL,
              secret_enc TEXT NOT NULL DEFAULT '',
              extra_enc TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL DEFAULT 'bound',
              last_test_ok INTEGER,
              last_test_at REAL,
              last_error TEXT NOT NULL DEFAULT '',
              created_at REAL NOT NULL DEFAULT 0,
              UNIQUE(uid, venue)
            );
            CREATE TABLE IF NOT EXISTS user_limits(
              uid INTEGER PRIMARY KEY,
              max_notional REAL NOT NULL DEFAULT 20,
              daily_loss_cap REAL NOT NULL DEFAULT 5,
              max_positions INTEGER NOT NULL DEFAULT 3,
              live_enabled INTEGER NOT NULL DEFAULT 0
            );
            """)
            con.commit()
        finally:
            con.close()
    if os.path.exists(DB_FILE):
        os.chmod(DB_FILE, 0o600)


def _master_key():
    """主密钥 = SHA256(签名密钥字节) — 不新增密钥文件, 随 .dash_secret 轮换自动轮换"""
    with open(config.SECRET_FILE, "rb") as f:
        return hashlib.sha256(f.read()).digest()


def encrypt(plain: str) -> str:
    if not _HAS_AES:
        raise RuntimeError("cryptography 库未安装, 无法加密存储密钥")
    nonce = os.urandom(12)
    ct = AESGCM(_master_key()).encrypt(nonce, plain.encode(), None)
    return base64.b64encode(nonce + ct).decode()


def decrypt(b64: str) -> str:
    raw = base64.b64decode(b64)
    return AESGCM(_master_key()).decrypt(raw[:12], raw[12:], None).decode()


def mask(s: str) -> str:
    if not s or len(s) < 8:
        return "****"
    return f"{s[:4]}...{s[-4:]}"


# ---------- 密钥 CRUD ----------

def bind(uid, venue, key, secret="", extra="", label=""):
    if venue not in ("bybit", "pm"):
        return False, "未知交易所"
    if not key or not key.strip():
        return False, "密钥不能为空"
    with _lock:
        con = _db()
        try:
            con.execute("""INSERT INTO user_keys(uid, venue, label, key_enc, secret_enc,
                           extra_enc, status, created_at)
                           VALUES(?,?,?,?,?,?,'bound',?)
                           ON CONFLICT(uid, venue) DO UPDATE SET
                           key_enc=excluded.key_enc, secret_enc=excluded.secret_enc,
                           extra_enc=excluded.extra_enc, label=excluded.label,
                           status='bound', last_test_ok=NULL, last_error=''""",
                        (int(uid), venue, label or "", encrypt(key.strip()),
                         encrypt(secret) if secret else "",
                         encrypt(extra) if extra else "", time.time()))
            con.commit()
        finally:
            con.close()
    return True, "ok"


def unbind(uid, venue):
    with _lock:
        con = _db()
        try:
            con.execute("DELETE FROM user_keys WHERE uid=? AND venue=?", (int(uid), venue))
            con.commit()
        finally:
            con.close()
    return True, "ok"


def list_keys(uid):
    """掩码列表 (绝不返回明文)"""
    with _lock:
        con = _db()
        try:
            rows = con.execute(
                "SELECT id, venue, label, key_enc, status, last_test_ok, last_test_at, "
                "last_error, created_at FROM user_keys WHERE uid=? ORDER BY venue",
                (int(uid),)).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d.pop("key_enc", None)
                d["key_masked"] = mask(decrypt(r["key_enc"])) if r["key_enc"] else ""
                out.append(d)
            return out
        finally:
            con.close()


def get_secrets(uid, venue):
    """解密取明文 — 仅在连通测试/下单瞬间调用"""
    with _lock:
        con = _db()
        try:
            row = con.execute("SELECT * FROM user_keys WHERE uid=? AND venue=?",
                              (int(uid), venue)).fetchone()
            if not row:
                return None
            return {"uid": row["uid"], "venue": venue,
                    "key": decrypt(row["key_enc"]),
                    "secret": decrypt(row["secret_enc"]) if row["secret_enc"] else "",
                    "extra": decrypt(row["extra_enc"]) if row["extra_enc"] else ""}
        finally:
            con.close()


def mark_test(uid, venue, ok, err=""):
    with _lock:
        con = _db()
        try:
            con.execute("UPDATE user_keys SET last_test_ok=?, last_test_at=?, last_error=? "
                        "WHERE uid=? AND venue=?", (1 if ok else 0, time.time(),
                                                    (err or "")[:300], int(uid), venue))
            con.commit()
        finally:
            con.close()


# ---------- 限额 ----------

def get_limits(uid):
    with _lock:
        con = _db()
        try:
            row = con.execute("SELECT * FROM user_limits WHERE uid=?", (int(uid),)).fetchone()
            if row:
                return dict(row)
            return dict(DEFAULT_LIMITS, uid=int(uid))
        finally:
            con.close()


def set_limits(uid, max_notional=None, daily_loss_cap=None, max_positions=None, live_enabled=None):
    """管理员配置限额 (None=不改)"""
    cur = get_limits(uid)
    if max_notional is not None:
        if not (1 <= float(max_notional) <= 10000):
            return False, "单笔名义限额须在 1~10000 之间"
        cur["max_notional"] = float(max_notional)
    if daily_loss_cap is not None:
        if not (0.5 <= float(daily_loss_cap) <= 10000):
            return False, "日亏限额须在 0.5~10000 之间"
        cur["daily_loss_cap"] = float(daily_loss_cap)
    if max_positions is not None:
        if not (1 <= int(max_positions) <= 50):
            return False, "最大持仓数须在 1~50 之间"
        cur["max_positions"] = int(max_positions)
    if live_enabled is not None:
        cur["live_enabled"] = 1 if live_enabled else 0
    with _lock:
        con = _db()
        try:
            con.execute("""INSERT INTO user_limits(uid, max_notional, daily_loss_cap,
                           max_positions, live_enabled) VALUES(?,?,?,?,?)
                           ON CONFLICT(uid) DO UPDATE SET
                           max_notional=excluded.max_notional, daily_loss_cap=excluded.daily_loss_cap,
                           max_positions=excluded.max_positions, live_enabled=excluded.live_enabled""",
                        (int(uid), cur["max_notional"], cur["daily_loss_cap"],
                         cur["max_positions"], cur["live_enabled"]))
            con.commit()
        finally:
            con.close()
    return True, "ok"
