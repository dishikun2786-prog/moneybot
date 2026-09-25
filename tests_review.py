#!/usr/bin/env python3
"""审查修复回归验证 (服务器本地跑)"""
import sys, json
sys.path.insert(0, "dash")
from app import auth, users, funds

UT = auth.make_session(27, "user")

import urllib.request
def curl(method, path, body=None, token=None):
    req = urllib.request.Request("http://127.0.0.1:8080" + path, method=method)
    if token:
        req.add_header("Cookie", "mb_session=" + token)
    if body is not None:
        req.add_header("Content-Type", "application/json")
        data = json.dumps(body).encode()
    else:
        data = None
    try:
        with urllib.request.urlopen(req, data, timeout=15) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}

R = []
def ok(n, c):
    R.append(("✅" if c else "❌") + " " + n)
    print(R[-1])

# 1. 升级越权修复: 余额不足时升级 → 套餐不变
st, d = curl("POST", "/api/plans/upgrade", {"code": "live"}, UT)
# 查 uid27 当前套餐
u27 = users.get_user(27)
ok(f"余额不足升级被拒(套餐仍为 {u27['plan']})", st == 200 and not d.get("ok") and u27.get("plan") == "pro")

# 2. funding 结算+入账单测 (paper_ops, mock 固定价消除市场波动)
import paper_ops, tenants, time
with tenants.tenant(27):
    # 清理残留持仓
    stc0 = paper_ops._read(paper_ops._resolve("CARRY_STATE"), {})
    for _s in list((stc0.get("positions") or {}).keys()):
        paper_ops.close_both(_s)
    FIXED = {"XRPUSDT": {"spot": 1.5000, "perp": 1.5000}}
    _orig_prices = paper_ops._prices
    paper_ops._prices = lambda: FIXED
    try:
        # (a) settle 结算: next_funding_ts 过去 → acc 应增加
        paper_ops.open_hedge("XRPUSDT", 15)
        st0 = paper_ops._read(paper_ops._resolve("CARRY_STATE"), {})
        for sym, pos in st0.get("positions", {}).items():
            pos["funding_acc"] = 0.0003
            pos["next_funding_ts"] = int(time.time()) - 10
        paper_ops._write(paper_ops._resolve("CARRY_STATE"), st0)
        paper_ops.settle_all_funding()
        st1 = paper_ops._read(paper_ops._resolve("CARRY_STATE"), {})
        fa = [p.get("funding_acc") for p in st1.get("positions", {}).values()]
        ok("settle 结算累计 funding(acc>0.0003)", bool(fa) and all(a > 0.0003 for a in fa))
        # (b) 平仓入账: 价差0 → PnL增量 = funding - fees(0.0015*2)
        day_before = st1.get("day_pnl", 0)
        st0c = paper_ops._read(paper_ops._resolve("CARRY_STATE"), {})
        for sym, pos in st0c.get("positions", {}).items():
            pos["funding_acc"] = 0.0004
        paper_ops._write(paper_ops._resolve("CARRY_STATE"), st0c)
        r2 = paper_ops.close_both("XRPUSDT")
        st2 = paper_ops._read(paper_ops._resolve("CARRY_STATE"), {})
        delta = st2.get("day_pnl", 0) - day_before
        fa0 = 0.0004
        _fees = (paper_ops.FEE_SPOT + paper_ops.FEE_PERP) * 15
        ok(f"平仓 funding 并入 PnL(增量={round(delta,4)}, 期望={round(fa0-_fees,4)})", abs(delta - (fa0 - _fees)) < 0.001)
    finally:
        paper_ops._prices = _orig_prices

# 3. 验证码持久化 (已在服务器)
ok("captchas 表存在", "captchas" in [r[0] for r in users._db().execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()])

print("\n" + "\n".join(R))
