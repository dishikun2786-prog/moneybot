#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1 生产 e2e (服务器 ~/polymarket 下运行, 同进程 TestClient → 验证码内存可读)
覆盖: 验证码→注册临时用户→会话→/api/instruments(885+)→SSE diff(首帧full+后续delta)→全标的K线→清理用户
"""
import json
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "dash"))

from app import config, users  # noqa: E402
from app.main import app  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

UNAME = "p1e2e_tmp"
DB = os.environ.get("USERS_DB", os.path.expanduser("~/polymarket/dash/users.db"))

_n = 0
_fail = 0


def check(name, cond, extra=""):
    global _n, _fail
    _n += 1
    if cond:
        print("  ✓ " + name)
    else:
        _fail += 1
        print("  ✗ FAIL: " + name + (" | " + str(extra)[:120] if extra else ""))


# ---- 清理历史残留 ----
try:
    con = sqlite3.connect(DB)
    con.execute("DELETE FROM users WHERE username=?", (UNAME,))
    con.commit()
    con.close()
except Exception as e:
    print("  (清理残留跳过:", e, ")")

client = TestClient(app)

# 1. 验证码 → 注册
cap = client.get("/api/captcha").json()
check("验证码颁发", cap.get("ok") and cap.get("captcha_id"))
cid = cap.get("captcha_id")
code = users._captchas.get(cid, {}).get("code") if cid else None
check("验证码内存可读(同进程)", bool(code))

r = client.post("/api/auth/register", json={
    "username": UNAME, "email": "p1e2e@t.com", "password": "p1e2epass123",
    "terms": True, "captcha_id": cid, "captcha_code": code})
check("注册成功", r.status_code == 200 and r.json().get("ok"), r.text[:100])

# 2. 全标的目录
r2 = client.get("/api/instruments")
d2 = r2.json()
n_lin = len(d2.get("linear", []))
n_spot = len(d2.get("spot", []))
check("instruments linear≥800", n_lin >= 800, f"linear={n_lin}")
check("instruments spot≥400", n_spot >= 400, f"spot={n_spot}")
check("标的含精度字段", d2["linear"][0].get("tickSize") is not None)

# 3. SSE diff 协议 (TestClient 流式读取会死锁 → 直接单测 _price_delta 核心)
from app.main import _price_delta  # noqa: E402

prices = {s: {"last": 1.0, "ts": 1000 + i} for i, s in enumerate("ABCDEFGHIJ")}
seen = {s: (v.get("ts") or 0) for s, v in prices.items()}
d1 = _price_delta(prices, seen)
check("全帧无变化 → delta 空", d1 == {})
prices2 = dict(prices)
prices2["C"] = {"last": 1.5, "ts": 9999}
prices2["F"] = {"last": 2.0, "ts": 9998}
d2 = _price_delta(prices2, seen)
check("仅变化标的进 delta", set(d2.keys()) == {"C", "F"}, d2)
check("seen 已更新", seen["C"] == 9999 and seen["F"] == 9998)
d3 = _price_delta(prices2, seen)
check("重复帧 → 空 delta (幂等)", d3 == {})
prices3 = dict(prices2)
prices3["G"] = {"last": 3.0}   # 新标的无 ts
d4 = _price_delta(prices3, seen)
check("无 ts 标的按 0 处理且进入 delta", "G" in d4, d4)

# 4. 全标的 K线 (REST 回源)
r3 = client.get("/api/klines", params={"symbol": "SOLUSDT", "interval": "15m", "limit": 5}).json()
check("SOLUSDT K线 5 根", len(r3.get("bars", [])) == 5, r3)
r4 = client.get("/api/klines", params={"symbol": "BTCUSDT", "interval": "15m", "limit": 5}).json()
check("BTCUSDT K线(parquet) 5 根", len(r4.get("bars", [])) == 5)

# 5. 清理
try:
    con = sqlite3.connect(DB)
    con.execute("DELETE FROM users WHERE username=?", (UNAME,))
    con.commit()
    con.close()
    print("  ✓ 临时用户已清理")
except Exception as e:
    print("  ✗ 清理失败:", e)

print(f"\nP1 e2e 完成: {_n - _fail}/{_n} 通过")
sys.exit(1 if _fail else 0)
