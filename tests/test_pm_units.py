#!/usr/bin/env python3
"""R12 PM 单位全链测试: 股数→成本/回报/PnL; outcome 区分; 部分卖出"""
import json
import os
import sys
import tempfile
import types

TMP = tempfile.mkdtemp(prefix="pm_units_")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---- 桩: tenants / paper_engine (import 时即读) ----
fake_tenants = types.ModuleType("tenants")
_STATE = {}
def _state(kind):
    return os.path.join(TMP, kind + ".json")
def _save_state(kind, st):
    with open(os.path.join(TMP, kind + ".json"), "w") as f:
        json.dump(st, f)
def _lock():
    return 1
def _unlock(f):
    pass
def _trades(kind="paper"):
    return os.path.join(TMP, kind + ".jsonl")
def _audit(*a, **k):
    pass
def _audit_path():
    return os.path.join(TMP, "audit.jsonl")
def _resolve(name):
    return os.path.join(TMP, name + ".json")
fake_tenants.state = _state
fake_tenants.trades = _trades
fake_tenants.ROOT = TMP
fake_tenants.lock_path = lambda: os.path.join(TMP, "op.lock")
fake_tenants.audit_manual = _audit_path
def _shared_log(name):
    return os.path.join(TMP, name)
fake_tenants.shared_log = _shared_log
sys.modules["tenants"] = fake_tenants

fake_pe = types.ModuleType("paper_engine")
fake_pe.FEE_RATE = 0.07
fake_pe.TICK = 0.001
fake_pe.SHARES = 100
fake_pe.latest_snapshot = lambda: [{"event": "e1", "market": "m1", "pm_bid": 0.60, "pm_ask": 0.62, "model_p": 0.58}]
fake_pe.pnl_usd = lambda t: round((t["exit"] - t["entry"]) * 100, 4)
sys.modules["paper_engine"] = fake_pe

import paper_ops  # noqa: E402

# 桩文件: PM_TOKENS/PM_PX/state 读写由 _resolve 走 TMP
toks = [
    {"key": "e1|m1", "token": "T_YES", "outcome": "YES"},
    {"key": "e1|m1", "token": "T_NO", "outcome": "NO"},
]
with open(os.path.join(TMP, "pm_tokens.json"), "w") as f:
    json.dump(toks, f)
with open(os.path.join(TMP, "pm_prices.json"), "w") as f:
    json.dump({"prices": {
        "T_YES": {"bid": 0.60, "ask": 0.62, "last": 0.61},
        "T_NO": {"bid": 0.35, "ask": 0.37, "last": 0.36},
    }}, f)


ok = []
def check(n, c, x=None):
    ok.append(c)
    print(("  OK " if c else "  XX ") + n + ("" if c else " | " + str(x)[:120]))

# 1. outcome 区分: YES ask 0.62 / NO ask 0.37
rt_y = paper_ops._pm_rt_px("e1|m1", "YES")
rt_n = paper_ops._pm_rt_px("e1|m1", "NO")
check("YES token ask=0.62", abs(rt_y["ask"] - 0.62) < 1e-9, rt_y)
check("NO token ask=0.37", abs(rt_n["ask"] - 0.37) < 1e-9, rt_n)

# 2. 买入 10 股 YES: 成本 6.20, 潜在回报 3.80
r = paper_ops.open_pm("e1|m1", "BUY", shares=10, outcome="YES")
check("买入 YES 成功", r["ok"], r)
st = json.load(open(os.path.join(TMP, "paper.json")))
pos = st["positions"]["e1|m1"]
check("持仓股数=10", pos["shares"] == 10, pos)
check("持仓成本=6.2", abs(pos["cost_usd"] - 6.2) < 0.01, pos)
check("outcome=YES 记录", pos["outcome"] == "YES", pos)

# 3. 买入不选 outcome 拒绝
r2 = paper_ops.open_pm("e1|m1", "BUY", shares=10)
check("买入无 outcome 拒绝", not r2["ok"], r2)

# 4. 部分卖出 4 股: PnL = (0.60-0.62)*4 - 4*0.0014
r3 = paper_ops.pm_sell_shares("e1|m1", 4)
check("部分卖出成功", r3["ok"], r3)
st = json.load(open(os.path.join(TMP, "paper.json")))
pos = st["positions"]["e1|m1"]
check("剩余 6 股", abs(pos["shares"] - 6) < 1e-6, pos)
# pnl = (0.60-0.62)*4 - 4*(0.07+0.07)/100 = -0.08 - 0.0056 = -0.0856
check("卖出 PnL ≈ -0.0856", abs(st["day_pnl"] - (-0.0856 - 6.2 * 0.07 / 100)) < 0.01, st["day_pnl"])

# 5. 卖出超过持有拒绝
r4 = paper_ops.pm_sell_shares("e1|m1", 100)
check("超持有卖出拒绝", not r4["ok"], r4)

# 6. 全平
r5 = paper_ops.pm_sell_shares("e1|m1", 6)
check("全平成功", r5["ok"], r5)
st = json.load(open(os.path.join(TMP, "paper.json")))
check("持仓已清空", not st["positions"], st["positions"])

# 7. size_usd 兼容换算: 5$ / 0.62 ≈ 8.06 股
r6 = paper_ops.open_pm("e1|m1", "BUY", size_usd=5, outcome="YES")
check("size_usd 兼容开仓", r6["ok"], r6)
st = json.load(open(os.path.join(TMP, "paper.json")))
pos = st["positions"]["e1|m1"]
check("换算股数≈8.06", abs(pos["shares"] - 8.06) < 0.01, pos["shares"])

print(f"\nR12 test_pm_units: {sum(ok)}/{len(ok)} 通过")
sys.exit(0 if all(ok) else 1)
