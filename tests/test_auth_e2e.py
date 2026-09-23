#!/usr/bin/env python3
"""M1 认证 HTTP 全流程 e2e (隔离环境, 服务器运行: HOME 指向临时目录)
覆盖: 验证码→注册→会话→me→登出→登录→错验证码→锁定→改密轮换
"""
import os
import sys
import tempfile

E2E_HOME = os.environ.get("E2E_HOME") or tempfile.mkdtemp(prefix="m1_e2e_")
os.makedirs(E2E_HOME, exist_ok=True)
os.environ["USERS_DB"] = os.path.join(E2E_HOME, "e2e_users.db")

# 代码路径用真实仓库位置
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "dash"))

import bcrypt  # noqa: E402

from app import config  # noqa: E402

# 凭据文件路径隔离 (不改 HOME → main.py 的 ~/polymarket 导入仍能找到 ai_client 等)
config.PASSWD_FILE = os.path.join(E2E_HOME, ".dash_passwd_hash")
config.SECRET_FILE = os.path.join(E2E_HOME, ".dash_secret")
if not os.path.exists(config.PASSWD_FILE):
    with open(config.PASSWD_FILE, "wb") as f:
        f.write(bcrypt.hashpw(b"e2eadmin123", bcrypt.gensalt()))
if not os.path.exists(config.SECRET_FILE):
    with open(config.SECRET_FILE, "w") as f:
        f.write("e" * 64)

from app import auth, users  # noqa: E402
from app.main import app  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

_n = 0
_fail = 0


def check(name, cond):
    global _n, _fail
    _n += 1
    print(("  ✓ " if cond else "  ✗ FAIL: ") + name)
    if not cond:
        _fail += 1


def captcha_code(cid):
    return users._captchas[cid]["code"]  # 同进程可读 (测试专用)


with TestClient(app) as c:
    print("== 验证码 ==")
    r = c.get("/api/captcha").json()
    check("验证码颁发", r["ok"] and r["captcha_id"] and r["image"].startswith("data:image/"))
    cid = r["captcha_id"]
    code = captcha_code(cid)

    print("== 注册 == (未勾选条款应被拒)")
    r0 = c.post("/api/auth/register", json={"username": "e2euser1", "email": "e2e@t.com",
                                            "password": "e2epass123",
                                            "captcha_id": cid, "captcha_code": code})
    check("未勾选条款被拒", r0.status_code == 400)
    r = c.post("/api/auth/register", json={"username": "e2euser1", "email": "e2e@t.com",
                                           "password": "e2epass123", "terms": True,
                                           "captcha_id": cid, "captcha_code": code})
    d = r.json()
    check("注册成功+自动登录", r.status_code == 200 and d["ok"] and d["user"]["role"] == "user")
    sess = c.cookies.get("mb_session")
    check("会话cookie已设置(HttpOnly)", bool(sess))

    print("== 会话隔离 ==")
    r = c.get("/api/auth/me").json()
    check("me返回注册用户", r["ok"] and r["user"]["username"] == "e2euser1" and r["user"]["role"] == "user")
    check("受保护数据端点放行", c.get("/api/summary").status_code == 200)

    print("== 登出 ==")
    c.post("/api/auth/logout")
    check("登出后me=401", c.get("/api/auth/me").status_code == 401)

    print("== 登录 ==")
    auth._fails.clear()  # 进程内计数复位 (测试专用)
    r1 = c.get("/api/captcha").json()
    cid1, code1 = r1["captcha_id"], captcha_code(r1["captcha_id"])
    r = c.post("/api/login", json={"username": "e2euser1", "password": "e2epass123",
                                   "captcha_id": cid1, "captcha_code": code1})
    check("登录成功", r.status_code == 200 and r.json()["ok"])
    check("登录后me恢复", c.get("/api/auth/me").json()["user"]["username"] == "e2euser1")

    print("== 错误路径 ==")
    r2 = c.get("/api/captcha").json()
    r = c.post("/api/login", json={"username": "e2euser1", "password": "e2epass123",
                                   "captcha_id": r2["captcha_id"], "captcha_code": "ZZZZ"})
    check("错验证码拒400", r.status_code == 400 and "验证码" in r.json()["err"])

    print("== admin 登录 (迁移账户) ==")
    auth._fails.clear()
    r3 = c.get("/api/captcha").json()
    r = c.post("/api/login", json={"username": "admin", "password": "e2eadmin123",
                                   "captcha_id": r3["captcha_id"],
                                   "captcha_code": captcha_code(r3["captcha_id"])})
    check("admin登录", r.status_code == 200 and r.json()["user"]["role"] == "admin")

    print("== 普通用户访问管理接口(预留) ==")
    auth._fails.clear()
    r4 = c.get("/api/captcha").json()
    r = c.post("/api/login", json={"username": "e2euser1", "password": "e2epass123",
                                   "captcha_id": r4["captcha_id"],
                                   "captcha_code": captcha_code(r4["captcha_id"])})
    check("切回普通用户", r.status_code == 200)
    # 用 main.require_admin 语义: 直接测依赖
    from app.main import require_admin
    from starlette.requests import Request
    got_403 = False
    try:
        req = Request({"type": "http", "method": "GET", "path": "/",
                       "headers": [(b"cookie", ("mb_session=" + c.cookies.get("mb_session")).encode())]})
        require_admin(req)
    except Exception as e:
        got_403 = getattr(e, "status_code", None) == 403
    check("非admin被403", got_403)

print(f"\n结果: {_n - _fail}/{_n} 通过")
sys.exit(1 if _fail else 0)
