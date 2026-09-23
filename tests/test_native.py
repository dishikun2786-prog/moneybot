#!/usr/bin/env python3
"""P3 原生交易单元测试 (隔离环境): 白名单/精度/开平结算/MTM/互斥"""
import json
import os
import sys
import tempfile

TMP = tempfile.mkdtemp(prefix="native_test_")
os.environ["PAPER_BASE"] = TMP
os.makedirs(os.path.join(TMP, "logs"), exist_ok=True)

SNAP = {"ts": 1790000000000, "lag_ms": 90,
        "prices": {"BTCUSDT": {"last": 86000.0, "spot": 86050.0, "change_pct": 0.5},
                   "ETHUSDT": {"last": 2750.0, "spot": 2755.0, "change_pct": 0.3},
                   "SOLUSDT": {"last": 118.5, "change_pct": 2.1},
                   "LOWVOLXX": {"last": 0.5, "change_pct": 0.0}}}
json.dump(SNAP, open(os.path.join(TMP, "logs", "bybit_prices.json"), "w"))
INSTR = {"linear": [
    {"symbol": "BTCUSDT", "name": "Bitcoin", "turnover24h": 5.9e9, "qtyStep": "0.001", "tickSize": "0.1"},
    {"symbol": "ETHUSDT", "name": "Ethereum", "turnover24h": 3.0e9, "qtyStep": "0.01", "tickSize": "0.01"},
    {"symbol": "SOLUSDT", "name": "Solana", "turnover24h": 1.0e9, "qtyStep": "0.1", "tickSize": "0.001"},
    {"symbol": "LOWVOLXX", "name": "LowVol", "turnover24h": 1.0e6, "qtyStep": "0.1", "tickSize": "0.0001"},
]}
json.dump(INSTR, open(os.path.join(TMP, "logs", "bybit_instruments.json"), "w"))

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import paper_ops  # noqa: E402

# 隔离: 强制 INSTR 路径指向夹具 (绕过 tenants 动态解析)
paper_ops._DYN["INSTR"] = lambda: os.path.join(TMP, "logs", "bybit_instruments.json")
paper_ops._DYN["SNAP"] = lambda: os.path.join(TMP, "logs", "bybit_prices.json")

ok = []


def check(name, cond, extra=""):
    ok.append(cond)
    print(("  ✓ " if cond else "  ✗ ") + name + ((" | " + str(extra)[:100]) if extra and not cond else ""))


# 1. 白名单: BTC/ETH 恒可, 高成交额可, 低成交额拒
check("BTC 白名单通过", paper_ops._native_allowed("BTCUSDT"))
check("SOL 白名单通过(成交额Top)", paper_ops._native_allowed("SOLUSDT"))
check("低成交额标的白名单拒绝", not paper_ops._native_allowed("LOWVOLXX"))
check("未知标的白名单拒绝", not paper_ops._native_allowed("NOSUCHXX"))

# 2. 精度取整
q = paper_ops._native_qty("SOLUSDT", 100, 118.5)
check("SOL 数量按 qtyStep=0.1 取整", q == round(q, 1) and abs(q - 100 / 118.5) < 0.1, q)
q2 = paper_ops._native_qty("BTCUSDT", 8600, 86000)
check("BTC 数量按 qtyStep=0.001 取整", q2 == round(q2, 3), q2)

# 3. 开仓
r = paper_ops.open_native("SOLUSDT", "long", 100)
check("原生做多开仓", r["ok"], r)
st = json.load(open(os.path.join(TMP, "logs", "carry_state.json")))
nat = st["native"]["SOLUSDT"]
check("仓位入 native 区", nat["side"] == "long" and nat["entry"] == 118.5)
check("开仓费已扣 day_pnl", abs(st["day_pnl"] + 0.0005 * 100) < 1e-9)

# 4. 重复开仓拒 + 方向校验
r = paper_ops.open_native("SOLUSDT", "short", 10)
check("同标的重复开仓拒绝", not r["ok"])
r = paper_ops.open_native("BTCUSDT", "sideways", 10)
check("非法方向拒绝", not r["ok"])

# 5. 互斥: 已有对冲仓位的标的拒绝原生
paper_ops.open_hedge("BTCUSDT", 10)
r = paper_ops.open_native("BTCUSDT", "long", 10)
check("对冲持仓标的拒绝原生开仓", not r["ok"])

# 6. MTM
paper_ops.open_native("ETHUSDT", "short", 100)  # entry 2750
SNAP["prices"]["ETHUSDT"]["last"] = 2700.0     # 空头盈利 50/2750*100
json.dump(SNAP, open(os.path.join(TMP, "logs", "bybit_prices.json"), "w"))
ps = paper_ops.native_positions()
eth = [p for p in ps if p["symbol"] == "ETHUSDT"][0]
check("空头 MTM 盈利≈+1.77", eth["pnl"] is not None and abs(eth["pnl"] - (50 / 2750 * 100 - 0.05)) < 0.02, eth["pnl"])

# 7. 平仓结算
r = paper_ops.close_native("ETHUSDT")
check("原生平仓成功", r["ok"], r)
st = json.load(open(os.path.join(TMP, "logs", "carry_state.json")))
check("平仓后仓位移除", "ETHUSDT" not in st.get("native", {}))

# 8. 无持仓平仓拒绝
r = paper_ops.close_native("ETHUSDT")
check("重复平仓拒绝", not r["ok"])

print(f"\nP3 test_native: {sum(ok)}/{len(ok)} 通过")
sys.exit(0 if all(ok) else 1)
