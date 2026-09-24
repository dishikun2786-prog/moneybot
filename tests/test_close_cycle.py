#!/usr/bin/env python3
"""R14-A2 平仓全链路一致性测试 (隔离环境)
六类平仓: native / spot / hedge双腿 / perp_leg→orphan / naked / both
断言: ①day_pnl 增量=手算pnl-fees(误差<0.001) ②state 无残留 ③CARRY_TRADES留痕
     ④funds 两本账隔离(paper 平仓不动 funds 余额)
"""
import json
import os
import sys
import tempfile

TMP = tempfile.mkdtemp(prefix="close_cycle_")
os.environ["PAPER_BASE"] = TMP
os.makedirs(os.path.join(TMP, "logs"), exist_ok=True)

def write_snap(pxmap):
    snap = {"ts": 1790000000000, "lag_ms": 90, "prices": {
        s: {"last": p[0], "spot": p[1], "change_pct": 0.0} for s, p in pxmap.items()}}
    json.dump(snap, open(os.path.join(TMP, "logs", "bybit_prices.json"), "w"))

INSTR = {"linear": [
    {"symbol": "BTCUSDT", "name": "Bitcoin", "turnover24h": 5.9e9, "qtyStep": "0.001", "tickSize": "0.1"},
    {"symbol": "ETHUSDT", "name": "Ethereum", "turnover24h": 3.0e9, "qtyStep": "0.01", "tickSize": "0.01"},
    {"symbol": "SOLUSDT", "name": "Solana", "turnover24h": 1.0e9, "qtyStep": "0.1", "tickSize": "0.001"},
]}
json.dump(INSTR, open(os.path.join(TMP, "logs", "bybit_instruments.json"), "w"))

# 初始价格: (perp, spot)
write_snap({"BTCUSDT": (86000.0, 86050.0), "ETHUSDT": (2750.0, 2755.0),
            "SOLUSDT": (118.5, 118.6)})

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import paper_ops  # noqa: E402
paper_ops._DYN["INSTR"] = lambda: os.path.join(TMP, "logs", "bybit_instruments.json")
paper_ops._DYN["SNAP"] = lambda: os.path.join(TMP, "logs", "bybit_prices.json")

ok = []

def check(name, cond, extra=""):
    ok.append(cond)
    print(("  ✓ " if cond else "  ✗ ") + name + ((" | " + str(extra)[:120]) if extra and not cond else ""))

def state():
    return json.load(open(os.path.join(TMP, "logs", "carry_state.json"))) if \
        os.path.exists(os.path.join(TMP, "logs", "carry_state.json")) else {}

def day_pnl():
    return state().get("day_pnl", 0.0)

def trades():
    p = os.path.join(TMP, "logs", "carry_trades.jsonl")
    return [json.loads(l) for l in open(p)] if os.path.exists(p) else []

def assert_close(expected_pnl_delta, desc):
    """断言平仓后 day_pnl 精确增量"""
    return expected_pnl_delta

N, S = 50.0, 40.0  # 名义

# ============ 1. native 多开→平 ============
d0 = day_pnl()
r = paper_ops.open_native("BTCUSDT", "long", N); check("native 开仓", r.get("ok"), r)
d1 = day_pnl(); check("native 开仓扣费", abs((d1 - d0) - (-paper_ops.NATIVE_FEE * N)) < 1e-6, d1 - d0)
write_snap({"BTCUSDT": (87720.0, 86050.0), "ETHUSDT": (2750.0, 2755.0), "SOLUSDT": (118.5, 118.6)})  # +2%
r = paper_ops.close_native("BTCUSDT"); check("native 平仓", r.get("ok"), r)
pnl = (87720.0 - 86000.0) / 86000.0 * N; fees = paper_ops.NATIVE_FEE * N
check("native PnL 入账", abs(day_pnl() - d1 - (pnl - fees)) < 0.001, day_pnl() - d1)
check("native state 清理", "BTCUSDT" not in state().get("native", {}))
check("native 留痕", trades()[-1].get("action") == "NATIVE_CLOSE")

# ============ 2. spot 买入→全平 ============
d0 = day_pnl()
r = paper_ops.open_spot("ETHUSDT", "buy", N); check("spot 开仓", r.get("ok"), r)
qty = state()["spot"]["ETHUSDT"]["qty"]; avg = state()["spot"]["ETHUSDT"]["avg_cost"]
d1 = day_pnl(); check("spot 开仓扣费", abs((d1 - d0) - (-paper_ops.SPOT_FEE * N)) < 1e-6, d1 - d0)
write_snap({"BTCUSDT": (87720.0, 86050.0), "ETHUSDT": (2832.5, 2835.0), "SOLUSDT": (118.5, 118.6)})  # 现货+~2.9%
r = paper_ops.close_spot("ETHUSDT"); check("spot 平仓", r.get("ok"), r)
pnl = (2835.0 - avg) * qty; fees = paper_ops.SPOT_FEE * 2835.0 * qty
check("spot PnL 入账", abs(day_pnl() - d1 - (pnl - fees)) < 0.001, day_pnl() - d1)
check("spot state 清理", "ETHUSDT" not in state().get("spot", {}))
check("spot 留痕", trades()[-1].get("action") == "SPOT_CLOSE")

# ============ 3. hedge 双腿→全平 both ============
d0 = day_pnl()
r = paper_ops.open_hedge("ETHUSDT", 30.0, "fwd"); check("hedge 开仓", r.get("ok"), r)
d1 = day_pnl(); check("hedge 开仓扣费", abs((d1 - d0) - (-(paper_ops.FEE_SPOT + paper_ops.FEE_PERP) * 30.0)) < 1e-6, d1 - d0)
pos = state()["positions"]["ETHUSDT"]
se, pe = pos["spot_entry"], pos["perp_entry"]
write_snap({"BTCUSDT": (87720.0, 86050.0), "ETHUSDT": (2794.0, 2810.0), "SOLUSDT": (118.5, 118.6)})  # spot↑ perp↓
r = paper_ops.close_both("ETHUSDT"); check("both 平仓", r.get("ok"), r)
spot_pnl = (2810.0 - se) / se * 30.0; perp_pnl = (pe - 2794.0) / pe * 30.0
total = spot_pnl + perp_pnl - (paper_ops.FEE_SPOT + paper_ops.FEE_PERP) * 30.0
check("both PnL 入账", abs(day_pnl() - d1 - total) < 0.001, day_pnl() - d1)
check("both state 清理", "ETHUSDT" not in state().get("positions", {}))
check("both 留痕", trades()[-1].get("action") == "MANUAL_CLOSE_BOTH")
check("both n_rounds", state().get("n_rounds", 0) == 1)

# ============ 4. perp_leg → orphan → close_orphan 闭环 ============
r = paper_ops.open_hedge("SOLUSDT", 25.0, "fwd"); check("hedge2 开仓", r.get("ok"), r)
d0 = day_pnl()  # 开仓费已计入, 此处为平腿前基线
pos = state()["positions"]["SOLUSDT"]; se, pe = pos["spot_entry"], pos["perp_entry"]
write_snap({"BTCUSDT": (87720.0, 86050.0), "ETHUSDT": (2794.0, 2810.0), "SOLUSDT": (116.0, 118.6)})  # perp↓
r = paper_ops.close_perp_leg("SOLUSDT"); check("perp_leg 平合约腿", r.get("ok"), r)
perp_pnl = (pe - 116.0) / pe * 25.0; fees = paper_ops.FEE_PERP * 25.0
check("perp_leg PnL 入账", abs(day_pnl() - d0 - (perp_pnl - fees)) < 0.001, day_pnl() - d0)
check("孤儿腿生成", "SOLUSDT" in state().get("orphans", {}) and "SOLUSDT" not in state().get("positions", {}))
d1 = day_pnl()
write_snap({"BTCUSDT": (87720.0, 86050.0), "ETHUSDT": (2794.0, 2810.0), "SOLUSDT": (116.0, 122.0)})  # spot↑
r = paper_ops.close_orphan("SOLUSDT"); check("orphan 平现货腿", r.get("ok"), r)
orph_pnl = (122.0 - se) / se * 25.0 - paper_ops.FEE_SPOT * 25.0
check("orphan PnL 入账", abs(day_pnl() - d1 - orph_pnl) < 0.001, day_pnl() - d1)
check("orphan state 清理", "SOLUSDT" not in state().get("orphans", {}))
check("orphan 留痕", trades()[-1].get("action") == "MANUAL_CLOSE_ORPHAN")

# ============ 5. naked 裸腿开→平 ============
d0 = day_pnl()
r = paper_ops.open_naked("SOLUSDT", "fwd", 20.0, 110.0, 125.0); check("naked 开仓", r.get("ok"), r)
check("naked 开仓费用入账", day_pnl() < d0)
nk = state()["naked"]["SOLUSDT"]; nke = nk["perp_entry"]
d1 = day_pnl()
write_snap({"BTCUSDT": (87720.0, 86050.0), "ETHUSDT": (2794.0, 2810.0), "SOLUSDT": (113.5, 122.0)})  # perp↓
r = paper_ops.close_naked("SOLUSDT"); check("naked 平仓", r.get("ok"), r)
perp_pnl = (nke - 113.5) / nke * 20.0; fees = paper_ops.FEE_PERP * 20.0
check("naked PnL 入账", abs(day_pnl() - d1 - (perp_pnl - fees)) < 0.001, day_pnl() - d1)
check("naked state 清理", "SOLUSDT" not in state().get("naked", {}))
check("naked 留痕", trades()[-1].get("action") == "MANUAL_CLOSE_NAKED")

# ============ 6. funds 两本账隔离 ============
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dash"))
try:
    from app import funds
    bal0 = funds.get_balance(999)
    paper_ops.open_native("BTCUSDT", "long", 30.0)
    write_snap({"BTCUSDT": (86860.0, 86050.0), "ETHUSDT": (2794.0, 2810.0), "SOLUSDT": (113.5, 122.0)})
    paper_ops.close_native("BTCUSDT")
    bal1 = funds.get_balance(999)
    check("funds 两本账隔离(paper盈亏不动平台USDT)", bal0 == bal1, (bal0, bal1))
except Exception as e:
    check("funds 隔离(模块加载)", False, e)

print(f"\n{'='*50}\nA2 平仓链路: {sum(ok)}/{len(ok)} 通过")
sys.exit(0 if all(ok) else 1)
