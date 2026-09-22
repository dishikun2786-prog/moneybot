#!/usr/bin/env python3
"""波段微结构评分 swing_score 单元测试"""
import sys

sys.path.insert(0, "D:/Program Files/hermes/polymarket_arb")
import carry_engine as ce

ok = []


def check(name, cond, extra=""):
    ok.append(cond)
    print(("  ✓ " if cond else "  ✗ ") + name + ((" | " + extra) if extra and not cond else ""))


def mk(rows):
    return [{"bv": b, "sv": s, "cum_cvd": c, "oi": o} for b, s, c, o in rows]


# 1. 强买微结构 + fwd 方向 → 高分
rows = mk([(10, 2, 100 + i * 5, 1000 + i) for i in range(10)])
walls = [{"side": "bids", "type": "appear"}] * 3
s_fwd = ce.swing_score(rows, walls, "fwd")
check("fwd+强买+升CVD+买墙+OI升 → 高分(≥60)", s_fwd >= 60, str(s_fwd))
s_rev = ce.swing_score(rows, walls, "rev")
check("同数据rev → 低分(方向不对齐≤40)", s_rev <= 40, str(s_rev))

# 2. 强卖微结构 + rev 方向 → 高分
rows2 = mk([(2, 10, 100 - i * 5, 1000 - i) for i in range(10)])
walls2 = [{"side": "asks", "type": "appear"}] * 3
s2 = ce.swing_score(rows2, walls2, "rev")
check("rev+强卖+降CVD+卖墙+OI降 → 高分(≥60)", s2 >= 60, str(s2))

# 3. 数据不足 → 0
check("数据不足10条 → 0分", ce.swing_score(mk([(1, 1, 0, 1)] * 3), [], "fwd") == 0.0)

# 4. 边界 0-100
bounds = all(0 <= ce.swing_score(mk([(b, 10 - b, 50.0, 1000.0)] * 10), [], d) <= 100
             for b in (0, 5, 10) for d in ("fwd", "rev"))
check("评分恒在[0,100]", bounds)

# 5. 中性数据 ≈ 中间分
mid = ce.swing_score(mk([(5, 5, 100.0, 1000.0)] * 10), [], "fwd")
check("完全中性 → 中段分(30-60)", 30 <= mid <= 60, str(mid))

n_fail = sum(1 for x in ok if not x)
print(f"\n结果: {len(ok) - n_fail}/{len(ok)} 通过")
sys.exit(1 if n_fail else 0)
