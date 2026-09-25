#!/usr/bin/env python3
"""M-S1: 客服系统后端 (2026-09-25)
- SQLite data/support.db: conversations / messages / quick_replies
- 双端: 用户端(m.html) 与 admin 客服台(admin.html)
- 图片: data/support_uploads/ (白名单+5MB+随机名+鉴权读取)
- 每用户一个活跃会话; 已解决后再发起=新会话
"""
import os
import sqlite3
import time
import json
import uuid

from fastapi import Depends, Request, UploadFile, File
from fastapi.responses import JSONResponse, FileResponse

from . import config, auth

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB = os.path.join(BASE, "data", "support.db")
UPDIR = os.path.join(BASE, "data", "support_uploads")
ALLOWED_IMG = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
MAX_IMG = 5 * 1024 * 1024
ACTIVE_ST = ("open", "pending")


def _db():
    conn = sqlite3.connect(DB, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _init():
    os.makedirs(UPDIR, exist_ok=True)
    c = _db()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS conversations(
      id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER NOT NULL,
      status TEXT DEFAULT 'open', subject TEXT DEFAULT '',
      last_msg_at TEXT DEFAULT '', unread_user INTEGER DEFAULT 0,
      unread_admin INTEGER DEFAULT 0, created_at TEXT DEFAULT '', resolved_at TEXT DEFAULT '');
    CREATE TABLE IF NOT EXISTS messages(
      id INTEGER PRIMARY KEY AUTOINCREMENT, conv_id INTEGER NOT NULL,
      sender TEXT DEFAULT 'user', type TEXT DEFAULT 'text',
      content TEXT DEFAULT '', created_at TEXT DEFAULT '', read INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS quick_replies(
      id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT DEFAULT '', sort INTEGER DEFAULT 0);
    CREATE INDEX IF NOT EXISTS idx_msg_conv ON messages(conv_id);
    CREATE INDEX IF NOT EXISTS idx_conv_uid ON conversations(uid);
    """)
    c.commit()
    # 预置快捷回复 (仅首次)
    n = c.execute("SELECT COUNT(*) FROM quick_replies").fetchone()[0]
    if n == 0:
        for i, t in enumerate(["已收到，正在排查，请稍等", "请截图发我看一下",
                               "问题已解决，感谢您的反馈！", "该问题需要升级处理，请稍等"]):
            c.execute("INSERT INTO quick_replies(text,sort) VALUES(?,?)", (t, i))
        c.commit()
    c.close()


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _ts():
    return int(time.time())


def get_active_conv(uid):
    """用户活跃会话 (open/pending), 无则 None"""
    c = _db()
    r = c.execute("SELECT * FROM conversations WHERE uid=? AND status IN ('open','pending') "
                  "ORDER BY id DESC LIMIT 1", (uid,)).fetchone()
    c.close()
    return r


def _conv_of_user(conv_id, uid):
    c = _db()
    r = c.execute("SELECT * FROM conversations WHERE id=? AND uid=?", (conv_id, uid)).fetchone()
    c.close()
    return r


def unread_counts(su):
    """{user: n, admin: n}"""
    c = _db()
    if su["r"] == "admin":
        r = c.execute("SELECT COALESCE(SUM(unread_admin),0) FROM conversations").fetchone()[0]
        u = 0
    else:
        r = c.execute("SELECT COALESCE(SUM(unread_user),0) FROM conversations WHERE uid=?", (su["u"],)).fetchone()[0]
        u = r
    c.close()
    return {"user": u, "admin": r}


def register(app):
    from .main import require_session_user, HTTPException
    _init()

    @app.get("/api/support/conv")
    def support_conv(request: Request, su=Depends(require_session_user)):
        """用户: 我的活跃会话+消息; admin: 会话列表(+conv_id 时给该会话消息)"""
        c = _db()
        if su["r"] == "admin":
            conv_id = int(request.query_params.get("conv_id") or 0)
            if conv_id:
                conv = c.execute("SELECT * FROM conversations WHERE id=?", (conv_id,)).fetchone()
                if not conv:
                    c.close()
                    return JSONResponse({"ok": False, "error": "会话不存在"}, 404)
                c.execute("UPDATE conversations SET unread_admin=0 WHERE id=?", (conv_id,))
                c.execute("UPDATE messages SET read=1 WHERE conv_id=? AND sender='user'", (conv_id,))
                c.commit()
                msgs = [dict(m) for m in c.execute(
                    "SELECT * FROM messages WHERE conv_id=? ORDER BY id", (conv_id,)).fetchall()]
                c.close()
                return {"ok": True, "conversation": dict(conv), "messages": msgs}
            convs = [dict(r) for r in c.execute(
                "SELECT * FROM conversations ORDER BY (status='resolved'), last_msg_at DESC").fetchall()]
            c.close()
            return {"ok": True, "conversations": convs, "unread": unread_counts(su)}
        # 用户
        conv = get_active_conv(su["u"])
        if not conv:
            c.close()
            return {"ok": True, "conversation": None, "messages": []}
        c.execute("UPDATE conversations SET unread_user=0 WHERE id=?", (conv["id"],))
        c.execute("UPDATE messages SET read=1 WHERE conv_id=? AND sender='admin'", (conv["id"],))
        c.commit()
        msgs = [dict(m) for m in c.execute(
            "SELECT * FROM messages WHERE conv_id=? ORDER BY id", (conv["id"],)).fetchall()]
        c.close()
        return {"ok": True, "conversation": dict(conv), "messages": msgs}

    @app.post("/api/support/msg")
    async def support_msg(request: Request, su=Depends(require_session_user)):
        """发文字消息 {conv_id?, content}。用户自动建/续会话"""
        try:
            body = json.loads((await request.body()).decode() or "{}")
        except Exception:
            body = {}
        content = str(body.get("content") or "").strip()
        if not content or len(content) > 4000:
            return JSONResponse({"ok": False, "error": "内容为空或过长"}, 400)
        c = _db()
        if su["r"] == "admin":
            conv_id = int(body.get("conv_id") or 0)
            conv = c.execute("SELECT * FROM conversations WHERE id=?", (conv_id,)).fetchone()
            if not conv:
                c.close()
                return JSONResponse({"ok": False, "error": "会话不存在"}, 404)
            c.execute("INSERT INTO messages(conv_id,sender,type,content,created_at,read) VALUES(?,?,?,?,?,1)",
                      (conv_id, "admin", "text", content, _now()))
            c.execute("UPDATE conversations SET last_msg_at=?, unread_user=unread_user+1, "
                      "status=CASE WHEN status='resolved' THEN 'open' ELSE status END WHERE id=?",
                      (_now(), conv_id))
        else:
            conv = get_active_conv(su["u"])
            if not conv:
                cur = c.execute("INSERT INTO conversations(uid,status,created_at,last_msg_at) VALUES(?,?,?,?)",
                                (su["u"], "open", _now(), _now()))
                c.commit()
                conv_id = cur.lastrowid
                subject = content[:40]
                c.execute("UPDATE conversations SET subject=? WHERE id=?", (subject, conv_id))
            else:
                conv_id = conv["id"]
            c.execute("INSERT INTO messages(conv_id,sender,type,content,created_at,read) VALUES(?,?,?,?,?,0)",
                      (conv_id, "user", "text", content, _now()))
            c.execute("UPDATE conversations SET last_msg_at=?, unread_admin=unread_admin+1 WHERE id=?", (_now(), conv_id))
        c.commit()
        c.close()
        return {"ok": True, "unread": unread_counts(su)}

    @app.post("/api/support/upload")
    async def support_upload(request: Request, su=Depends(require_session_user),
                             file: UploadFile = File(...)):
        """图片上传 → 存盘 + 插 image 消息"""
        ext = os.path.splitext(file.filename or "")[1].lower()
        if ext not in ALLOWED_IMG:
            return JSONResponse({"ok": False, "error": "仅支持 jpg/png/webp/gif"}, 400)
        data = await file.read()
        if len(data) > MAX_IMG:
            return JSONResponse({"ok": False, "error": "图片不能超过 5MB"}, 400)
        fname = f"{_ts()}_{uuid.uuid4().hex[:10]}{ext}"
        with open(os.path.join(UPDIR, fname), "wb") as f:
            f.write(data)
        c = _db()
        if su["r"] == "admin":
            conv_id = int((await request.form()).get("conv_id") or 0)
            conv = c.execute("SELECT * FROM conversations WHERE id=?", (conv_id,)).fetchone()
            if not conv:
                c.close()
                return JSONResponse({"ok": False, "error": "会话不存在"}, 404)
            cur = c.execute("INSERT INTO messages(conv_id,sender,type,content,created_at,read) VALUES(?,?,?,?,?,1)",
                            (conv_id, "admin", "image", fname, _now()))
            c.execute("UPDATE conversations SET last_msg_at=?, unread_user=unread_user+1 WHERE id=?", (_now(), conv_id))
        else:
            conv = get_active_conv(su["u"])
            if not conv:
                cur = c.execute("INSERT INTO conversations(uid,status,created_at,last_msg_at) VALUES(?,?,?,?)",
                                (su["u"], "open", _now(), _now()))
                c.commit()
                conv_id = cur.lastrowid
            else:
                conv_id = conv["id"]
            cur = c.execute("INSERT INTO messages(conv_id,sender,type,content,created_at,read) VALUES(?,?,?,?,?,0)",
                            (conv_id, "user", "image", fname, _now()))
            c.execute("UPDATE conversations SET last_msg_at=?, unread_admin=unread_admin+1 WHERE id=?", (_now(), conv_id))
        c.commit()
        mid = cur.lastrowid
        c.close()
        return {"ok": True, "msg_id": mid, "unread": unread_counts(su)}

    @app.get("/api/support/img/{mid}")
    def support_img(mid: int, request: Request, su=Depends(require_session_user)):
        """图片鉴权读取: 用户仅自己会话的图; admin 全量"""
        c = _db()
        m = c.execute("SELECT * FROM messages WHERE id=? AND type='image'", (mid,)).fetchone()
        if not m:
            c.close()
            return JSONResponse({"ok": False, "error": "图片不存在"}, 404)
        if su["r"] != "admin":
            conv = c.execute("SELECT * FROM conversations WHERE id=?", (m["conv_id"],)).fetchone()
            if not conv or conv["uid"] != su["u"]:
                c.close()
                return JSONResponse({"ok": False, "error": "无权查看"}, 403)
        c.close()
        path = os.path.join(UPDIR, m["content"])
        if not os.path.exists(path):
            return JSONResponse({"ok": False, "error": "文件缺失"}, 404)
        return FileResponse(path)

    @app.post("/api/support/status")
    async def support_status(request: Request, su=Depends(require_session_user)):
        """admin: 改会话状态 {conv_id, status: open/pending/resolved}"""
        if su["r"] != "admin":
            return JSONResponse({"ok": False, "error": "仅管理员"}, 403)
        try:
            body = json.loads((await request.body()).decode() or "{}")
        except Exception:
            body = {}
        conv_id = int(body.get("conv_id") or 0)
        status = str(body.get("status") or "")
        if status not in ("open", "pending", "resolved"):
            return JSONResponse({"ok": False, "error": "状态无效"}, 400)
        c = _db()
        c.execute("UPDATE conversations SET status=?, resolved_at=? WHERE id=?",
                  (status, _now() if status == "resolved" else "", conv_id))
        c.commit()
        c.close()
        return {"ok": True}

    @app.get("/api/support/unread")
    def support_unread(request: Request, su=Depends(require_session_user)):
        return {"ok": True, "unread": unread_counts(su)}

    @app.post("/api/support/quick")
    async def support_quick(request: Request, su=Depends(require_session_user)):
        """admin: 快捷回复 CRUD {action: list/add/del, id?, text?}"""
        try:
            body = json.loads((await request.body()).decode() or "{}")
        except Exception:
            body = {}
        action = body.get("action", "list")
        c = _db()
        if action == "list":
            rows = [dict(r) for r in c.execute("SELECT * FROM quick_replies ORDER BY sort, id").fetchall()]
            c.close()
            return {"ok": True, "quicks": rows}
        if su["r"] != "admin":
            c.close()
            return JSONResponse({"ok": False, "error": "仅管理员"}, 403)
        if action == "add":
            text = str(body.get("text") or "").strip()
            if not text:
                c.close()
                return JSONResponse({"ok": False, "error": "内容为空"}, 400)
            c.execute("INSERT INTO quick_replies(text,sort) VALUES(?,?)",
                      (text, int(body.get("sort") or 0)))
            c.commit()
        elif action == "del":
            c.execute("DELETE FROM quick_replies WHERE id=?", (int(body.get("id") or 0),))
            c.commit()
        else:
            c.close()
            return JSONResponse({"ok": False, "error": "未知操作"}, 400)
        rows = [dict(r) for r in c.execute("SELECT * FROM quick_replies ORDER BY sort, id").fetchall()]
        c.close()
        return {"ok": True, "quicks": rows}
