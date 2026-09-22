#!/usr/bin/env python3
"""swing_cycle 循环恢复策略单元测试 (纯函数+结算逻辑, 隔离环境)"""
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, "D:/Program Files/hermes/polymarket_arb")
import paper_ops
import swing_cycle as sc

TMP = tempfile.mkdtemp(prefix="cycle_test_")
paper_ops.CARRY_STATE = os.path.join(TMP, "carry_state.json")
paper_ops.CARRY_TRADES = os.path.join(TMP, "carry_trades.jsonl")
sc.ROUNDS_LOG = os.path.join(TMP, "swing_rounds.jsonl")
sc.STATE_FILE = os.path.join(TMP, "swing_cycle.json")

ok = []


def check(name, cond, extra=""):
    ok.append(cond)
    print(("  ✓ " if cond else "  ✗ ") + name + ((" | " + extra) if extra and not cond else ""))


# 1. 恢复阶梯名义
sc.BASE_NOTIONAL = 10.0
sc.MULT = 1.5
sc.NOTIONAL_CAP = 30.0
sc.MAX_LADDER = 3
check("阶梯0=基础10$", sc.ladder_notional({"ladder": 0}) == 10.0)
check("阶梯1=15$", sc.ladder_notional({"ladder": 1}) == 15.0)
check("阶梯2=22.5$", sc.ladder_notional({"ladder": 2}) == 22.5)
check("阶梯3封顶30$", sc.ladder_notional({"ladder": 3}) == 30.0)
check("阶梯5仍封顶30$", sc.ladder_notional({"ladder": 5}) == 30.0)

# 2. 波段评分方向对齐
rows = [{"bv": 10, "sv": 2, "cum_cvd": 100 + i * 5, "oi": 1000 + i} for i in range(10)]
s_fwd = sc.swing_score(rows, [{"side": "bids", "type": "appear"}] * 3, "fwd")
check("强买微结构 fwd 高分", s_fwd >= 60, str(s_fwd))
check("同数据 rev 低分", sc.swing_score(rows, [{"side": "bids", "type": "appear"}] * 3, "rev") <= 40)

# 3. 亏损结算 → 阶梯升级
json.dump({"naked": {}}, open(paper_ops.CARRY_STATE, "w"))
with open(paper_ops.CARRY_TRADES, "w") as f:
    f.write(json.dumps({"symbol": "BTCUSDT", "action": "MANUAL_NAKED_SL", "perp_pnl_usd": -0.85}) + "\n")
st = sc._read()
st.update(phase="OPEN", symbol="BTCUSDT", ladder=1, n_rounds=0, n_wins=0, cum_pnl=0.0, cycle_day_pnl=0.0)
st2, pnl = sc.settle_round(st)
check("亏损结算: 阶梯1→2", st2["ladder"] == 2 and pnl == -0.85)
check("回合+1 胜场不变", st2["n_rounds"] == 1 and st2["n_wins"] == 0)
check("SETTLED+冷却窗口", st2["phase"] == "SETTLED" and st2["cooldown_until"] > time.time())

# 4. 盈利结算 → 复位
with open(paper_ops.CARRY_TRADES, "a") as f:
    f.write(json.dumps({"symbol": "BTCUSDT", "action": "MANUAL_NAKED_TP", "perp_pnl_usd": 1.2}) + "\n")
st3 = sc._read()
st3.update(phase="OPEN", symbol="BTCUSDT", ladder=2, n_rounds=1, n_wins=0, cum_pnl=-0.85,
           cycle_day_pnl=-0.85, cooldown_until=0.0)
st4, pnl = sc.settle_round(st3)
check("盈利结算: 阶梯复位0", st4["ladder"] == 0 and pnl == 1.2)
check("胜场+1", st4["n_wins"] == 1)

# 5. 持仓中不结算
json.dump({"naked": {"BTCUSDT": {"tp": 1, "sl": 2}}}, open(paper_ops.CARRY_STATE, "w"))
st5 = sc._read()
st5.update(phase="OPEN", symbol="BTCUSDT", ladder=0, n_rounds=0)
st6, pnl = sc.settle_round(st5)
check("持仓中不结算", st6["phase"] == "OPEN" and pnl is None)

# 6. 阶梯封顶
sc.MAX_LADDER = 3
st7 = sc._read()
st7.update(phase="OPEN", symbol="BTCUSDT", ladder=3, n_rounds=5, n_wins=2, cum_pnl=0.0, cycle_day_pnl=0.0)
json.dump({"naked": {}}, open(paper_ops.CARRY_STATE, "w"))
with open(paper_ops.CARRY_TRADES, "a") as f:
    f.write(json.dumps({"symbol": "BTCUSDT", "action": "MANUAL_NAKED_SL", "perp_pnl_usd": -1.0}) + "\n")
st8, _ = sc.settle_round(st7)
check("阶梯3再亏仍封顶3", st8["ladder"] == 3)

n_fail = sum(1 for x in ok if not x)
print(f"\n结果: {len(ok) - n_fail}/{len(ok)} 通过")
sys.exit(1 if n_fail else 0)
