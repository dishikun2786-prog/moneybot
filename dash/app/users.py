"""多用户认证与用户存储 (M1 商业化升级)
- SQLite users.db (users + audit 两表), chmod 600
- 密码 bcrypt (与既有看板密码同方案 → 迁移零成本, 现密码无缝保留)
- 登录限流: 每用户连续 5 次失败锁 15 分钟 + 每 IP 5次/分钟
- 图形验证码: 内存存储 (单 worker 进程), 5 分钟过期, 一次性消费
"""
import os
import re
import sys
import time
import uuid
import bcrypt
import sqlite3
import threading

from . import config

DB_FILE = os.environ.get("USERS_DB", os.path.join(config.BASE, "dash", "users.db"))
LOCK_AFTER = 5
LOCK_SECONDS = 900
CAPTCHA_TTL = 300

USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{4,20}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_lock = threading.RLock()
_captchas = {}  # id -> {"code", "exp", "tries"}


def _db():
    os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
    con = sqlite3.connect(DB_FILE, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=5000")
    return con


def init_db():
    """建表 + 迁移现有单用户为 admin (uid=1, 沿用现有 bcrypt 哈希 → 现密码无缝保留)。
    返回 True 表示执行了迁移。"""
    with _lock:
        con = _db()
        try:
            con.executescript("""
            CREATE TABLE IF NOT EXISTS users(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              username TEXT NOT NULL UNIQUE,
              email TEXT NOT NULL DEFAULT '',
              pass_hash TEXT NOT NULL,
              role TEXT NOT NULL DEFAULT 'user',
              status TEXT NOT NULL DEFAULT 'active',
              plan TEXT NOT NULL DEFAULT 'free',
              plan_expires REAL NOT NULL DEFAULT 0,
              failed_attempts INTEGER NOT NULL DEFAULT 0,
              locked_until REAL NOT NULL DEFAULT 0,
              created_at REAL NOT NULL DEFAULT 0,
              last_login_at REAL
            );
            CREATE TABLE IF NOT EXISTS audit(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ts REAL NOT NULL,
              uid INTEGER,
              action TEXT NOT NULL,
              detail TEXT NOT NULL DEFAULT '',
              ip TEXT NOT NULL DEFAULT '',
              ua TEXT NOT NULL DEFAULT ''
            );
            """)
            migrated = False
            if con.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
                legacy = open(config.PASSWD_FILE, "rb").read().decode().strip()
                con.execute(
                    "INSERT INTO users(username, email, pass_hash, role, created_at) "
                    "VALUES('admin', '', ?, 'admin', ?)", (legacy, time.time()))
                migrated = True
            con.commit()
        finally:
            con.close()
    if os.path.exists(DB_FILE):
        os.chmod(DB_FILE, 0o600)
    return migrated


def get_user(uid):
    with _lock:
        con = _db()
        try:
            row = con.execute("SELECT * FROM users WHERE id=?", (int(uid),)).fetchone()
            return dict(row) if row else None
        finally:
            con.close()


def _ensure_paper_balance(con):
    """M-S四期: users 表加 paper_balance 列 (模拟余额, 默认100), 幂等"""
    cols = [r[1] for r in con.execute("PRAGMA table_info(users)").fetchall()]
    if "paper_balance" not in cols:
        con.execute("ALTER TABLE users ADD COLUMN paper_balance REAL NOT NULL DEFAULT 100")


def get_paper_balance(uid):
    """模拟余额 (admin 可调)"""
    con = _db()
    try:
        _ensure_paper_balance(con)
        r = con.execute("SELECT paper_balance FROM users WHERE id=?", (uid,)).fetchone()
        return float(r[0]) if r else 100.0
    finally:
        con.close()


def set_paper_balance(uid, amount):
    """admin 调整模拟余额 (直接设置)"""
    con = _db()
    try:
        _ensure_paper_balance(con)
        con.execute("UPDATE users SET paper_balance=? WHERE id=?", (float(amount), uid))
        con.commit()
        return True, f"模拟余额已设为 {amount} USDT"
    finally:
        con.close()


def _ensure_fee_tier(con):
    """R14-M3: users 表加 fee_tier 列 (0=标准 1=VIP), 幂等"""
    cols = [r[1] for r in con.execute("PRAGMA table_info(users)").fetchall()]
    if "fee_tier" not in cols:
        con.execute("ALTER TABLE users ADD COLUMN fee_tier INTEGER NOT NULL DEFAULT 0")
        con.commit()


def get_fee_tier(uid):
    """R14-M3: 用户费率等级 0=标准 1=VIP"""
    con = _db()
    try:
        _ensure_fee_tier(con)
        r = con.execute("SELECT fee_tier FROM users WHERE id=?", (int(uid),)).fetchone()
        return int(r[0]) if r else 0
    finally:
        con.close()


def set_fee_tier(uid, tier):
    """R14-M3: 设置费率等级 (0/1)"""
    con = _db()
    try:
        _ensure_fee_tier(con)
        con.execute("UPDATE users SET fee_tier=? WHERE id=?", (int(tier), int(uid)))
        con.commit()
        return True
    finally:
        con.close()


def get_by_username(username):
    with _lock:
        con = _db()
        try:
            row = con.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
            return dict(row) if row else None
        finally:
            con.close()


def list_users():
    with _lock:
        con = _db()
        try:
            _ensure_fee_tier(con)
            _ensure_paper_balance(con)
            rows = con.execute(
                "SELECT id, username, email, role, status, plan, plan_expires, "
                "created_at, last_login_at, fee_tier, paper_balance FROM users ORDER BY id").fetchall()
            return [dict(r) for r in rows]
        finally:
            con.close()


def audit_log(uid, action, detail="", ip="", ua=""):
    with _lock:
        con = _db()
        try:
            con.execute("INSERT INTO audit(ts, uid, action, detail, ip, ua) VALUES(?,?,?,?,?,?)",
                        (time.time(), uid, action, detail, ip, ua))
            con.commit()
        finally:
            con.close()


def create_user(username, email, pw):
    """注册校验 + 建用户。返回 (ok, msg)"""
    if not USERNAME_RE.match(username or ""):
        return False, "账号需4-20位字母/数字/下划线"
    if not EMAIL_RE.match(email or ""):
        return False, "邮箱格式不正确"
    if len(pw or "") < 8 or not re.search(r"[A-Za-z]", pw) or not re.search(r"[0-9]", pw):
        return False, "密码至少8位且同时包含字母和数字"
    h = bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()
    with _lock:
        con = _db()
        try:
            if con.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
                return False, "账号已存在，请直接登录"
            cur = con.execute(
                "INSERT INTO users(username, email, pass_hash, role, created_at) VALUES(?,?,?,?,?)",
                (username, email.strip(), h, "user", time.time()))
            con.commit()
            uid = cur.lastrowid
        finally:
            con.close()
    audit_log(uid, "register", f"注册 {username}", "", "")
    try:  # M2: 注册即初始化租户目录 ($100纸面账户+默认参数+托管模式)
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
        import tenants  # noqa: E402
        tenants.seed(uid)
    except Exception:
        pass  # 种子失败不阻断注册 (首轮引擎调度会兜底初始化)
    return True, "ok"


def verify_login(username, pw):
    """登录校验。返回 (ok, user_dict_or_msg)"""
    with _lock:
        con = _db()
        try:
            row = con.execute("SELECT * FROM users WHERE username=?", (username or "",)).fetchone()
            if not row:
                return False, "账号或密码错误"
            if row["locked_until"] > time.time():
                return False, "失败次数过多，账号已锁定15分钟"
            if row["status"] != "active":
                return False, "账号已被禁用，请联系管理员"
            if bcrypt.checkpw((pw or "").encode(), row["pass_hash"].encode()):
                con.execute("UPDATE users SET failed_attempts=0, locked_until=0, last_login_at=? "
                            "WHERE id=?", (time.time(), row["id"]))
                con.commit()
                # 成功审计由路由层记录 (带 IP/UA)
                u = dict(row)
                u.pop("pass_hash", None)
                return True, u
            fails = row["failed_attempts"] + 1
            locked = time.time() + LOCK_SECONDS if fails >= LOCK_AFTER else 0
            con.execute("UPDATE users SET failed_attempts=?, locked_until=? WHERE id=?",
                        (fails, locked, row["id"]))
            con.commit()
            if fails >= LOCK_AFTER:
                audit_log(row["id"], "login_locked", "连续5次失败，锁定15分钟")
                return False, "连续5次失败，账号锁定15分钟"
            audit_log(row["id"], "login_fail", f"密码错误(第{fails}次)")
            return False, f"账号或密码错误（剩余{5 - fails}次机会）"
        finally:
            con.close()


def change_password(uid, old_pw, new_pw):
    """改密: 校验旧密码+强度 → 更新哈希。返回 (ok, msg)"""
    with _lock:
        con = _db()
        try:
            row = con.execute("SELECT * FROM users WHERE id=?", (int(uid),)).fetchone()
            if not row:
                return False, "用户不存在"
            if not bcrypt.checkpw((old_pw or "").encode(), row["pass_hash"].encode()):
                return False, "当前密码错误"
            if len(new_pw) < 8:
                return False, "新密码至少8位"
            if new_pw == old_pw:
                return False, "新密码不能与旧密码相同"
            h = bcrypt.hashpw(new_pw.encode(), bcrypt.gensalt()).decode()
            con.execute("UPDATE users SET pass_hash=? WHERE id=?", (h, int(uid)))
            con.commit()
        finally:
            con.close()
    audit_log(int(uid), "password_change", "密码已修改")
    return True, "ok"


def set_status(uid, status):
    """管理员操作: 启用/禁用用户"""
    if status not in ("active", "disabled"):
        return False, "状态非法"
    with _lock:
        con = _db()
        try:
            con.execute("UPDATE users SET status=? WHERE id=?", (status, int(uid)))
            con.commit()
        finally:
            con.close()
    audit_log(int(uid), "status_change", f"状态改为 {status}")
    return True, "ok"


def set_plan(uid, code, expires_ts):
    """设置套餐 + 到期时间 (unix 秒; 0=不过期) — M7 余额购买/管理用"""
    with _lock:
        con = _db()
        try:
            con.execute("UPDATE users SET plan=?, plan_expires=? WHERE id=?",
                        (str(code), float(expires_ts or 0), int(uid)))
            con.commit()
        finally:
            con.close()
    audit_log(int(uid), "plan_change", f"套餐改为 {code} 到期 {expires_ts}")
    return True, "ok"


def expire_plans():
    """到期套餐自动降级 free (cron 每日调用) → 返回降级数"""
    from time import time as _t
    n = 0
    with _lock:
        con = _db()
        try:
            rows = con.execute("SELECT id, plan, plan_expires FROM users "
                               "WHERE plan!='free' AND plan_expires>0 AND plan_expires<?")\
                .fetchall()
            now = _t()
            for uid, plan, exp in rows:
                if exp and exp < now:
                    con.execute("UPDATE users SET plan='free', plan_expires=0 WHERE id=?", (uid,))
                    n += 1
            con.commit()
        finally:
            con.close()
    if n:
        audit_log(0, "plan_expire", f"{n} 个用户套餐到期自动降级")
    return n


# ---------- 图形验证码 (内存, 单进程) ----------

def _ensure_captchas(con):
    """M-S五期: captchas 表 (验证码持久化, pm-dash 重启不失效)"""
    con.execute("""CREATE TABLE IF NOT EXISTS captchas(
        id TEXT PRIMARY KEY, code TEXT, exp REAL, tries INTEGER DEFAULT 0)""")
    con.commit()


def captcha_new(code):
    """登记验证码, 返回 id (调用方生成 code) — SQLite 持久化"""
    cid = uuid.uuid4().hex[:12]
    with _lock:
        con = _db()
        try:
            _ensure_captchas(con)
            con.execute("INSERT INTO captchas(id,code,exp,tries) VALUES(?,?,?,0)",
                        (cid, (code or "").upper(), time.time() + CAPTCHA_TTL))
            con.commit()
        finally:
            con.close()
    return cid


def captcha_check(cid, code):
    """校验验证码。正确即销毁(一次性); 错5次销毁; 过期销毁 — SQLite 持久化"""
    with _lock:
        con = _db()
        try:
            _ensure_captchas(con)
            row = con.execute("SELECT code, exp, tries FROM captchas WHERE id=?",
                              (cid or "",)).fetchone()
            if not row or row["exp"] < time.time():
                return False
            tries = int(row["tries"]) + 1
            ok = row["code"] == (code or "").strip().upper()
            if ok or tries >= 5:
                con.execute("DELETE FROM captchas WHERE id=?", (cid or "",))
            else:
                con.execute("UPDATE captchas SET tries=? WHERE id=?", (tries, cid or ""))
            con.commit()
            return ok
        finally:
            con.close()
