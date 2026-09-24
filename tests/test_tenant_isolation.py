#!/usr/bin/env python3
"""R14-D1 租户隔离测试 (越权防护)
断言: ①B开仓只进B状态(admin/C隔离) ②B持仓API只返回B的 ③C平B仓失败(无持仓)
     ④B平自己仓成功 ⑤funds: B充值不动admin余额 ⑥mode文件租户隔离
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.expanduser("~/polymarket"))
sys.path.insert(0, os.path.expanduser("~/polymarket/dash"))
from starlette.testclient import TestClient  # noqa: E402
from app import main as m, auth, config, users  # noqa: E402
import tenants  # noqa: E402

c = TestClient(m.app)
ok = []

def check(name, cond, extra=""):
    ok.append(cond)
    print(("  ✓ " if cond else "  ✗ ") + name + ((" | " + str(extra)[:120]) if extra and not cond else ""))

# 创建测试用户 B/C
suffix = str(int(time.time()))[-6:]
b_name, c_name = f"isob{suffix}", f"isoc{suffix}"
try:
    users.create_user(b_name, f"{b_name}@t.io", "Test1234!")
    users.create_user(c_name, f"{c_name}@t.io", "Test1234!")
except Exception as e:
    pass
b = users.get_by_username(b_name); c_u = users.get_by_username(c_name)
bid, cid = b["id"], c_u["id"]
check("测试用户创建", bid > 1 and cid > 1, (bid, cid))

ckb = {config.COOKIE_NAME: auth.make_session(bid, "user")}
ckc = {config.COOKIE_NAME: auth.make_session(cid, "user")}
cka = {config.COOKIE_NAME: auth.make_session(1, "admin")}

# 1. B 开 spot 仓 → 各租户可见性
r = c.post("/api/spot/open", json={"symbol": "ETHUSDT", "side": "buy", "notional": 25}, cookies=ckb)
check("B 现货开仓", r.json().get("ok"), r.json().get("error", r.json().get("msg", ""))[:80])
pb = c.get("/api/spot/positions", cookies=ckb).json()["positions"]
pc = c.get("/api/spot/positions", cookies=ckc).json()["positions"]
pa = c.get("/api/spot/positions", cookies=cka).json()["positions"]
check("B 持仓可见自己的仓", any(p.get("symbol") == "ETHUSDT" for p in pb))
check("C 看不到 B 的仓", not any(p.get("symbol") == "ETHUSDT" for p in pc), pc)
check("admin 看不到 B 的仓", not any(p.get("symbol") == "ETHUSDT" for p in pa), pa)

# 2. C 平 B 的仓 → 失败(无持仓)
r = c.post("/api/spot/close", json={"symbol": "ETHUSDT"}, cookies=ckc)
check("C 平 B 仓被拒(无持仓)", not r.json().get("ok"), r.json().get("error", "")[:60])

# 3. B 开 native 仓 → 隔离
r = c.post("/api/native/open", json={"symbol": "XAGUSDT", "side": "long", "notional": 30}, cookies=ckb)
check("B 原生开仓", r.json().get("ok"))
nb = c.get("/api/native/positions", cookies=ckb).json()["positions"]
na = c.get("/api/native/positions", cookies=cka).json()["positions"]
check("B 原生持仓隔离", any(p.get("symbol") == "XAGUSDT" for p in nb) and
      not any(p.get("symbol") == "XAGUSDT" for p in na))

# 4. B 平自己的 native 仓
r = c.post("/api/native/close", json={"symbol": "XAGUSDT"}, cookies=ckb)
check("B 平自己原生仓", r.json().get("ok"))

# 5. funds: B 余额与 admin 隔离 (get_balance 按 uid)
from app import funds
bal_a_before = funds.get_balance(1)
funds.add_balance(bid, 50.0, "test_iso")
bal_a_after = funds.get_balance(1)
bal_b = funds.get_balance(bid)
check("funds 按 uid 隔离(B+50不动admin)", bal_a_before == bal_a_after and bal_b >= 50, (bal_a_before, bal_a_after, bal_b))

# 6. mode 文件租户隔离
with tenants.tenant(1):
    import engine_mode
    admin_mode = dict(engine_mode.load())
r = c.post("/api/mode", json={"strategy": "carry", "mode": "manual"}, cookies=ckb)
with tenants.tenant(1):
    admin_mode_after = dict(engine_mode.load())
check("B 切模式不动 admin 模式", admin_mode == admin_mode_after)
with tenants.tenant(bid):
    b_mode = dict(engine_mode.load())
check("B 自己的模式已切", b_mode.get("carry") == "manual", b_mode)
# 还原
with tenants.tenant(bid):
    engine_mode.set_mode("carry", "auto")

# 7. 清理测试仓 (B 的 spot)
r = c.post("/api/spot/close", json={"symbol": "ETHUSDT"}, cookies=ckb)
check("清理 B 现货仓", r.json().get("ok"))

print(f"\n{'='*50}\nD1 租户隔离: {sum(ok)}/{len(ok)} 通过")
sys.exit(0 if all(ok) else 1)
