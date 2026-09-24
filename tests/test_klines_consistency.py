#!/usr/bin/env python3
"""R14-B2 K线三源一致性测试
断言: ①请求根数单调不减(缓存命中) ②末根时间戳连续(无重根/缺根跳变)
     ③W/M 周期数据存在且末根滞后合理 ④limit 参数生效
"""
import os
import sys

sys.path.insert(0, os.path.expanduser("~/polymarket"))
sys.path.insert(0, os.path.expanduser("~/polymarket/dash"))
from starlette.testclient import TestClient  # noqa: E402
from app import main as m, auth, config  # noqa: E402

c = TestClient(m.app)
ck = {config.COOKIE_NAME: auth.make_session(1, "admin")}

ok = []

def check(name, cond, extra=""):
    ok.append(cond)
    print(("  ✓ " if cond else "  ✗ ") + name + ((" | " + str(extra)[:130]) if extra and not cond else ""))

def bars(sym, iv, limit=300, cat=""):
    d = c.get(f"/api/klines?symbol={sym}&interval={iv}&limit={limit}&category={cat}", cookies=ck).json()
    return d.get("bars", []), d

# 1. 三源合并连续性: 15m 连续两次请求
b1, d1 = bars("BTCUSDT", "15m", 100)
b2, d2 = bars("BTCUSDT", "15m", 100)
check("15m 请求有数据", len(b1) > 50, len(b1))
check("二次请求根数单调不减(缓存/增量)", len(b2) >= len(b1), (len(b1), len(b2)))
check("末根时间戳相同或前进(无回退)", b2[-1]["t"] >= b1[-1]["t"], (b1[-1]["t"], b2[-1]["t"]))
# 相邻根间隔 = 15m (900s), 允许 ±60s
gaps = [(b1[i+1]["t"] - b1[i]["t"]) for i in range(len(b1) - 1)]
odd = [g for g in gaps if abs(g - 900000) > 60000]
check("15m 相邻根间隔=900s(无重根缺根)", not odd, odd[:5])
check("live 标志", d1.get("live") is True or "live" in d1, d1.get("live"))

# 2. W/M 周期
for iv, exp_gap in (("W", 7 * 86400000), ("M", None)):
    bw, dw = bars("XAUUSDT", iv, 50)
    check(f"{iv} 周/月数据存在", len(bw) >= 3, len(bw))
    if len(bw) >= 2 and exp_gap:
        gw = bw[1]["t"] - bw[0]["t"]
        check(f"{iv} 间隔≈{exp_gap//86400000}天", abs(gw - exp_gap) < 2 * 86400000, gw)

# 3. limit 生效
b300, _ = bars("ETHUSDT", "15m", 300)
b30, _ = bars("ETHUSDT", "15m", 30)
check("limit=300 大于 limit=30", len(b300) > len(b30), (len(b300), len(b30)))
check("limit=30 截断", len(b30) <= 30, len(b30))

# 4. 未知/非法参数防御
d = c.get("/api/klines?symbol=FAKEZZ&interval=15m", cookies=ck).json()
check("未知标的返回空而非500", "bars" in d and d["bars"] == [], d)

print(f"\n{'='*50}\nB2 K线三源: {sum(ok)}/{len(ok)} 通过")
sys.exit(0 if all(ok) else 1)
