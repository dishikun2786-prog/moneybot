#!/usr/bin/env python3
"""R6 现货纸面交易单测: 白名单/价格/买入累加/卖出PnL/全平/精度"""
import json
import os
import sys
import tempfile
import types

TMP = tempfile.mkdtemp(prefix="spot_test_")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dash"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# tenants 桩: state/trades 返回临时路径
fake_tenants = types.ModuleType("tenants")
def _state(kind):
    return os.path.join(TMP, f"{kind}_state.json")
def _trades(kind):
    return os.path.join(TMP, f"{kind}_trades.jsonl")
fake_tenants.state = _state
fake_tenants.trades = _trades
fake_tenants.ROOT = TMP
fake_tenants.lock_path = lambda: os.path.join(TMP, "locks")
fake_tenants.audit_manual = lambda: os.path.join(TMP, "audit.jsonl")
fake_tenants.shared_log = lambda name: os.path.join(TMP, name)
sys.modules["tenants"] = fake_tenants

import paper_ops  # noqa: E402

# 桩路径与快照
paper_ops.SNAP = os.path.join(TMP, "bybit_prices.json")
paper_ops.INSTR = os.path.join(TMP, "bybit_instruments.json")

# 假目录: BTCUSDT 恒可 + BTCUSDC (成交额 3.9e7 排名1) + POORUSDT (成交额 1e5 被排除)
with open(paper_ops.INSTR, "w", encoding="utf-8") as f:
    json.dump({"spot": [
        {"symbol": "BTCUSDC", "turnover24h": 3.9e7, "qtyStep": "0.0001"},
        {"symbol": "ETHUSDC", "turnover24h": 9.9e6, "qtyStep": "0.001"},
        {"symbol": "POORUSDT", "turnover24h": 1e5, "qtyStep": "0.01"},
    ]}, f)
# 快照: BTCUSDC spot 85920; ETHUSDC 2733.88; POORUSDT 1.0
with open(paper_ops.SNAP, "w", encoding="utf-8") as f:
    json.dump({"prices": {
        "BTCUSDC": {"spot": 85920.0},
        "ETHUSDC": {"spot": 2733.88},
        "POORUSDT": {"spot": 1.0},
    }}, f)

ok = []


def check(name, cond, extra=""):
    ok.append(cond)
    print(("  ✓ " if cond else "  ✗ ") + name + ((" | " + str(extra)[:140]) if not cond else ""))


# 1. 白名单
check("BTCUSDC 白名单通过", paper_ops._spot_allowed("BTCUSDC"))
check("低流动性拒绝", not paper_ops._spot_allowed("POORUSDT"))
check("BTCUSDT 恒可", paper_ops._spot_allowed("BTCUSDT"))

# 2. 价格 + 数量精度
px = paper_ops._spot_px("BTCUSDC")
check("现货价 85920", px == 85920.0, px)
qty = paper_ops._spot_qty("BTCUSDC", 10, 85920)
check("qty 精度 0.0001 取整", qty == round(10 / 85920 / 0.0001) * 0.0001, qty)

# 3. 买入
r = paper_ops.open_spot("BTCUSDC", "buy", 10)
if not r.get("ok"):
    print("买入失败详情:", r)
check("买入成功", r["ok"], r)
pos = paper_ops.spot_positions()
check("持仓 1 条", len(pos) == 1 and pos[0]["symbol"] == "BTCUSDC", pos)
check("持仓数量 = qty", abs(pos[0]["qty"] - qty) < 1e-12, pos[0]["qty"])

# 4. 再买累加 (均价)
r = paper_ops.open_spot("BTCUSDC", "buy", 10)
pos = paper_ops.spot_positions()
check("两次买入数量累加", abs(pos[0]["qty"] - 2 * qty) < 1e-12, pos[0]["qty"])
check("均价不变(同价)", abs(pos[0]["avg_cost"] - 85920) < 0.01, pos[0]["avg_cost"])

# 5. 卖出部分 (PnL)
# 改快照价: 涨到 90000 → 卖出实现盈利
with open(paper_ops.SNAP, "w", encoding="utf-8") as f:
    json.dump({"prices": {"BTCUSDC": {"spot": 90000.0},
                          "ETHUSDC": {"spot": 2733.88}}}, f)
r = paper_ops.open_spot("BTCUSDC", "sell", 10)
check("卖出成功", r["ok"], r)
pos = paper_ops.spot_positions()
check("卖出后剩 1 份", abs(pos[0]["qty"] - qty) < 1e-12, pos[0]["qty"])
# 状态 day_pnl 为正 (盈利 +4080*qty - fees)
st = json.load(open(paper_ops.CARRY_STATE, encoding="utf-8"))
check("day_pnl > 0 (盈利)", st.get("day_pnl", 0) > 0, st.get("day_pnl"))

# 6. 卖出超持仓拒绝
r = paper_ops.open_spot("BTCUSDC", "sell", 200)
check("超持仓卖出拒绝", not r["ok"], r)

# 7. 全平
r = paper_ops.close_spot("BTCUSDC")
check("全平成功", r["ok"], r)
check("持仓清空", paper_ops.spot_positions() == [], paper_ops.spot_positions())
r = paper_ops.close_spot("BTCUSDC")
check("重复全平拒绝", not r["ok"])

# 8. 非法输入
check("方向非法拒绝", not paper_ops.open_spot("BTCUSDC", "hold", 10)["ok"])
check("名义超限拒绝", not paper_ops.open_spot("BTCUSDC", "buy", 9999)["ok"])
check("无价标的拒绝", not paper_ops.open_spot("NOPXUSDT", "buy", 10)["ok"])

print(f"\nR6 test_spot: {sum(ok)}/{len(ok)} 通过")
sys.exit(0 if all(ok) else 1)
