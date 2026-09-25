"""总管理后台逻辑 (M3 商业化升级) — 仅 admin 角色经 /api/admin/* 路由调用
- 用户管理: 列表(含每人模拟盘聚合)/禁用/套餐/重置密码
- 审计查询: 全用户操作流水 (登录/交易/改参/密钥), 分页+筛选
- 公告管理: 公告 CRUD (交易室横幅数据源)
- 数据面板: 注册数/活跃数/套餐分布/资金聚合
"""
import os
import sqlite3
import sys
import threading
import time

from . import config
from . import users

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import tenants  # noqa: E402

_lock = threading.RLock()
_ANNOUNCE_DB = os.environ.get("ANNOUNCE_DB", os.path.join(config.BASE, "dash", "announce.db"))

PLANS = {
    "free": {"name": "免费版", "price": 0, "zh": "模拟交易 · 手动下单"},
    "pro": {"name": "专业版", "price": 30, "zh": "模拟+实盘 · 对冲套利托管30天"},
    "live": {"name": "旗舰版", "price": 99, "zh": "模拟+实盘 · 对冲套利托管365天 · 优先支持"},
}
PLAN_DAYS = {"free": 0, "pro": 30, "live": 365}


def _adb():
    os.makedirs(os.path.dirname(_ANNOUNCE_DB), exist_ok=True)
    con = sqlite3.connect(_ANNOUNCE_DB)
    con.row_factory = sqlite3.Row
    return con


def init_announce():
    with _lock:
        con = _adb()
        try:
            con.execute("""CREATE TABLE IF NOT EXISTS announcements(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                text TEXT NOT NULL,
                level TEXT NOT NULL DEFAULT 'info',
                active INTEGER NOT NULL DEFAULT 1)""")
            con.commit()
        finally:
            con.close()


def user_stats(uid):
    """单用户模拟盘聚合 (admin 数据面板/用户列表用)"""
    try:
        from . import readers  # 延迟导入避免环
        with tenants.tenant(uid):
            d = readers.pnl_overview()
        return {"capital": d.get("capital"), "positions": len(d.get("positions") or []),
                "pm_trades": d.get("pm_trades"), "cy_rounds": d.get("cy_rounds"),
                "realized_today": d.get("realized_today")}
    except Exception:
        return {"capital": None, "positions": 0, "pm_trades": 0, "cy_rounds": 0,
                "realized_today": 0}


def list_users_with_stats(offset=0, limit=0):
    rows = users.list_users()
    for r in rows:
        r["stats"] = user_stats(r["id"])
        r["plan_zh"] = PLANS.get(r.get("plan"), {}).get("name", r.get("plan"))
    if limit and limit > 0:
        return {"rows": rows[offset:offset + limit],
                "total": len(rows),
                "has_more": offset + limit < len(rows)}
    return rows


def set_plan(uid, plan, admin_uid, days=None):
    if plan not in PLANS:
        return False, "未知套餐"
    # R14-M16: days=None → 用套餐默认时长; days=0 → 手动; 自定义 N 天
    if days is None:
        days = PLAN_DAYS.get(plan, 30)
    days = max(0, int(days or 0))
    if plan == "free":
        days = 0
    exp = 0 if days == 0 else time.time() + days * 86400
    with users._lock:
        con = users._db()
        try:
            con.execute("UPDATE users SET plan=?, plan_expires=? WHERE id=?",
                        (plan, exp, int(uid)))
            con.commit()
        finally:
            con.close()
    note = f"套餐改为 {PLANS[plan]['name']}" + (f" · 托管 {days} 天" if days else "")
    users.audit_log(int(uid), "plan_change", note, "", f"admin:{admin_uid}")
    return True, note


def reset_password(uid, new_pw, admin_uid):
    """管理员重置密码 (不需旧密码), 8位+字母数字"""
    import re as _re
    if len(new_pw or "") < 8 or not _re.search(r"[A-Za-z]", new_pw or "") or not _re.search(r"[0-9]", new_pw or ""):
        return False, "密码至少8位且同时包含字母和数字"
    import bcrypt
    h = bcrypt.hashpw(new_pw.encode(), bcrypt.gensalt()).decode()
    with users._lock:
        con = users._db()
        try:
            con.execute("UPDATE users SET pass_hash=?, failed_attempts=0, locked_until=0 WHERE id=?",
                        (h, int(uid)))
            con.commit()
        finally:
            con.close()
    users.audit_log(int(uid), "password_reset", "管理员重置密码", "", f"admin:{admin_uid}")
    return True, "ok"


def audit_query(uid=None, action=None, limit=200, offset=0):
    with users._lock:
        con = users._db()
        try:
            sql = "SELECT * FROM audit"
            cond, args = [], []
            if uid:
                cond.append("uid=?")
                args.append(int(uid))
            if action:
                cond.append("action LIKE ?")
                args.append(f"%{action}%")
            if cond:
                sql += " WHERE " + " AND ".join(cond)
            total = con.execute("SELECT COUNT(*) FROM (" + sql + ")", args).fetchone()[0]
            rows = con.execute(sql + " ORDER BY id DESC LIMIT ? OFFSET ?",
                               args + [int(limit), int(offset)]).fetchall()
            return total, [dict(r) for r in rows]
        finally:
            con.close()


def stats_overview():
    us = users.list_users()
    active = [u for u in us if u.get("status") == "active"]
    plans = {}
    for u in us:
        plans[u.get("plan", "free")] = plans.get(u.get("plan", "free"), 0) + 1
    capital_total = 0.0
    for u in active:
        s = user_stats(u["id"])
        capital_total += float(s.get("capital") or 0.0)
    day_ago = time.time() - 86400
    return {
        "total": len(us), "active": len(active),
        "new_today": sum(1 for u in us if u.get("created_at", 0) >= day_ago),
        "plans": plans, "capital_total": round(capital_total, 2),
    }


# ---------- 公告 ----------

def announce_list(all_=True):
    with _lock:
        con = _adb()
        try:
            sql = "SELECT * FROM announcements" + ("" if all_ else " WHERE active=1")
            rows = con.execute(sql + " ORDER BY id DESC LIMIT 50").fetchall()
            return [dict(r) for r in rows]
        finally:
            con.close()


def announce_add(text, level, admin_uid):
    text = (text or "").strip()
    if not text:
        return False, "公告内容不能为空"
    if level not in ("info", "warn"):
        level = "info"
    with _lock:
        con = _adb()
        try:
            cur = con.execute("INSERT INTO announcements(ts, text, level) VALUES(?,?,?)",
                              (time.time(), text, level))
            con.commit()
            aid = cur.lastrowid
        finally:
            con.close()
    users.audit_log(int(admin_uid), "announce_add", f"发布公告#{aid}", "", "")
    return True, "ok"


def announce_toggle(aid, active, admin_uid):
    with _lock:
        con = _adb()
        try:
            con.execute("UPDATE announcements SET active=? WHERE id=?",
                        (1 if active else 0, int(aid)))
            con.commit()
        finally:
            con.close()
    users.audit_log(int(admin_uid), "announce_toggle", f"公告#{aid} {'启用' if active else '停用'}", "", "")
    return True, "ok"


def fee_history(limit=10):
    """R14-M12: 费率配置变更历史 (audit 表 fee_config_save)"""
    try:
        con = users._db()
        rows = con.execute(
            "SELECT ts, detail FROM audit WHERE action='fee_config_save' "
            "ORDER BY rowid DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for ts, detail in rows:
            item = {"ts": ts, "detail": (detail or "")[:300]}
            out.append(item)
        return out
    except Exception:
        return []
