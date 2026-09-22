#!/usr/bin/env python3
"""P0 单元测试: 复利名义 + 跨天保留持仓 + 结算窗口"""
import os
import sys
import time

# 隔离测试: 引擎读取的路径在Linux服务器, Windows下用猴子补丁环境
sys.path.insert(0, "D:/Program Files/hermes/polymarket_arb")
import carry_engine as ce

ok = []


def check(name, cond):
    ok.append((name, cond))
    print(("  ✓ " if cond else "  ✗ ") + name)


# 1. 复利名义
ce.NOTIONAL, ce.COMP_BASE, ce.COMP_MIN, ce.COMP_MAX = 10.0, 10.0, 0.2, 3.0
check("累计+10 → 名义20 (2x)", ce.effective_notional({"cum_pnl": 10.0, "day_pnl": 0}) == 20.0)
check("累计+20 → 上限30 (3x)", ce.effective_notional({"cum_pnl": 25.0, "day_pnl": 0}) == 30.0)
check("累计-8 → 下限2 (0.2x)", ce.effective_notional({"cum_pnl": -8.0, "day_pnl": 0}) == 2.0)
check("当日+2也计入", ce.effective_notional({"cum_pnl": 3.0, "day_pnl": 2.0}) == 15.0)

# 2. 跨天保留持仓 (复现引擎rollover逻辑)
st = {"positions": {"BTCUSDT": {"t0": time.time(), "notional": 12.0}},
      "orphans": {"ETHUSDT": {"notional": 8.0}},
      "day": "2020-01-01", "day_pnl": 1.23, "cum_pnl": 2.0, "n_rounds": 3}
st["cum_pnl"] = round(st.get("cum_pnl", 0.0) + st.get("day_pnl", 0.0), 4)
st["day_pnl"] = 0.0
st["n_rounds"] = 0
st["day"] = "2020-01-02"
check("跨天持仓保留", "BTCUSDT" in st["positions"] and "ETHUSDT" in st["orphans"])
check("累计PnL沉淀 2+1.23=3.23", abs(st["cum_pnl"] - 3.23) < 1e-9)

# 3. 结算窗口
ce.ENTRY_WINDOW_MIN = 60
now = time.time()
check("距结算30min → 入场允许", ce.in_entry_window({"next_funding_ts": (now + 1800) * 1000}))
check("距结算3小时 → 过滤", not ce.in_entry_window({"next_funding_ts": (now + 10800) * 1000}))
check("已过结算 → 过滤", not ce.in_entry_window({"next_funding_ts": (now - 100) * 1000}))
ce.ENTRY_WINDOW_MIN = 0
check("窗口关闭(0) → 不限", ce.in_entry_window({"next_funding_ts": (now + 10800) * 1000}))

# 4. 方向选择 (正向/反向/借贷门槛)
check("funding+8% → 正向", ce.pick_dir(8.0, -3.0) == "fwd")
check("funding-3% → 不入场", ce.pick_dir(-3.0, -3.0) is None)
check("funding-8% → 不入场(未覆盖借贷5%)", ce.pick_dir(-8.0, -3.0) is None)
check("funding-12% → 反向", ce.pick_dir(-12.0, -3.0) == "rev")
check("基差过宽 → 不入场", ce.pick_dir(12.0, 25.0) is None)
check("基差过宽(反向) → 不入场", ce.pick_dir(-15.0, -25.0) is None)

n_fail = sum(1 for _, c in ok if not c)
print(f"\n结果: {len(ok) - n_fail}/{len(ok)} 通过")
sys.exit(1 if n_fail else 0)
