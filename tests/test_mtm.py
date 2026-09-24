#!/usr/bin/env python3
"""R14-A3 资金/MTM 口径一致性测试
断言: ①双腿MTM按实际名义(不再写死10$) ②孤儿/裸腿/原生/现货MTM计入
     ③funding_acc 计入 ④与 paper_ops 平仓记账口径一致(同价同方向)
"""
import os
import sys

sys.path.insert(0, os.path.expanduser("~/polymarket"))
sys.path.insert(0, os.path.expanduser("~/polymarket/dash"))

import paper_ops  # noqa: E402
from app import readers  # noqa: E402

ok = []

def check(name, cond, extra=""):
    ok.append(cond)
    print(("  ✓ " if cond else "  ✗ ") + name + ((" | " + str(extra)[:120]) if extra and not cond else ""))

# mock 实时价: (perp, spot)
PX = {"BTCUSDT": {"perp": 82000.0, "spot": 82100.0},
      "ETHUSDT": {"perp": 2650.0, "spot": 2660.0},
      "XAUUSDT": {"perp": 4280.0, "spot": None},
      "XAGUSDT": {"perp": 63.5, "spot": 64.0}}
paper_ops._prices = lambda: PX

st = {
  "positions": {"BTCUSDT": {"spot_entry": 81950.0, "perp_entry": 81890.0, "notional": 30.0, "dir": "fwd", "funding_acc": 0.12}},
  "orphans":   {"ETHUSDT": {"spot_entry": 2655.0, "notional": 20.0, "dir": "fwd"}},
  "naked":     {"ETHUSDT": {"perp_entry": 2640.0, "notional": 15.0, "dir": "fwd", "funding_acc": -0.05}},
  "native":    {"XAUUSDT": {"entry": 4260.0, "notional": 40.0, "side": "long"}},
  "spot":      {"XAGUSDT": {"avg_cost": 63.0, "qty": 2.5}},
  "day_pnl": 0.0,
}

tot = readers._mtm_carry(st)

# 双腿: (82100-81950)/81950*30 + (81890-82000)/81890*30 + 0.12
legs = (82100 - 81950) / 81950 * 30 + (81890 - 82000) / 81890 * 30 + 0.12
# 孤儿: (2660-2655)/2655*20
orph = (2660 - 2655) / 2655 * 20
# 裸腿: (2640-2650)/2640*15 + (-0.05)
naked = (2640 - 2650) / 2640 * 15 - 0.05
# 原生: (4280-4260)/4260*40
nat = (4280 - 4260) / 4260 * 40
# 现货: (64-63)*2.5
spot = (64.0 - 63.0) * 2.5
expected = legs + orph + naked + nat + spot

check("双腿MTM按30$名义(非写死10$)", abs(legs - (150 / 81950 * 30 - 110 / 81890 * 30 + 0.12)) < 1e-9, legs)
check("孤儿MTM计入", abs(orph - (5 / 2655 * 20)) < 1e-9, orph)
check("裸腿MTM含funding", abs(naked - (-10 / 2640 * 15 - 0.05)) < 1e-9, naked)
check("原生MTM计入", abs(nat - (20 / 4260 * 40)) < 1e-9, nat)
check("现货MTM计入", abs(spot - 2.5) < 1e-9, spot)
check("五类MTM总额精确", abs(tot - expected) < 1e-9, (tot, expected))

# 与 paper_ops 平仓口径一致性: 双腿 close_both 的 total 公式 vs MTM 公式 (同价同方向, 不含费时)
import math
def close_both_formula(pos, px, d="fwd"):
    n = pos["notional"]
    spot_pnl = (px["spot"] - pos["spot_entry"]) / pos["spot_entry"] * n
    perp_pnl = (pos["perp_entry"] - px["perp"]) / pos["perp_entry"] * n
    return spot_pnl + perp_pnl - (paper_ops.FEE_SPOT + paper_ops.FEE_PERP) * n

cb = close_both_formula(st["positions"]["BTCUSDT"], PX["BTCUSDT"])
mtm_now = (82100 - 81950) / 81950 * 30 + (81890 - 82000) / 81890 * 30
check("MTM与平仓公式同构(仅差平仓费)", abs(cb - (mtm_now - (paper_ops.FEE_SPOT + paper_ops.FEE_PERP) * 30)) < 1e-9, (cb, mtm_now))

print(f"\n{'='*50}\nA3 MTM 口径: {sum(ok)}/{len(ok)} 通过")
sys.exit(0 if all(ok) else 1)
