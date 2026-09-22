#!/usr/bin/env python3
"""波段微结构信号初步验证 (一次性回测):
主动量失衡比 对 15分钟价格方向的预测力
数据: trades_1s.jsonl (秒级主动买卖量) + price_1s.jsonl (秒级价格)
注: 微结构数据仅积累~26小时, 属初步验证, 非最终结论"""
import bisect
import json
import os

BASE = os.path.expanduser("~/polymarket")


def load(path):
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


print("加载 trades_1s ...", flush=True)
tr = load(f"{BASE}/logs/trades_1s.jsonl")
print("加载 price_1s ...", flush=True)
px = load(f"{BASE}/logs/price_1s.jsonl")

pmap = {}
for rec in px:
    ts = rec.get("ts")
    if not ts:
        continue
    for sym, p in rec.get("prices", {}).items():
        if p.get("last"):
            pmap.setdefault(sym, {})[ts] = p["last"]

print(f"trades行数 {len(tr)}, 价格秒数: " + ", ".join(f"{k}:{len(v)}" for k, v in pmap.items()))
print()

for sym in ("BTCUSDT", "ETHUSDT"):
    sym_tr = [r for r in tr if r.get("sym") == sym]
    wins = []
    cur = None
    for r in sym_tr:
        ts = r["ts"]
        if cur is None or ts > cur[0]:
            if cur:
                wins.append(cur)
            cur = [ts, r.get("bv", 0) or 0, r.get("sv", 0) or 0]
        else:
            cur[1] += r.get("bv", 0) or 0
            cur[2] += r.get("sv", 0) or 0
    if cur:
        wins.append(cur)
    keys = sorted(pmap.get(sym, {}))
    if not keys:
        print(f"{sym}: 无价格数据")
        continue
    n_hit = n_sig = 0
    thr_high, thr_low = 0.58, 0.42
    for w in wins:
        bv, sv = w[1], w[2]
        if bv + sv < 0.3:
            continue
        ratio = bv / (bv + sv)
        if not (ratio > thr_high or ratio < thr_low):
            continue
        t0 = w[0]
        i0 = bisect.bisect_left(keys, t0)
        i1 = i0 + 900  # 15分钟 = 900秒
        if i1 >= len(keys):
            continue
        p0 = pmap[sym][keys[i0]]
        p1 = pmap[sym][keys[i1]]
        if p1 == p0:
            continue
        up = p1 > p0
        pred_up = ratio > thr_high
        n_sig += 1
        if (pred_up and up) or ((not pred_up) and (not up)):
            n_hit += 1
    rate = n_hit / max(n_sig, 1) * 100
    print(f"{sym}: 失衡信号 {n_sig} 次 → 15分钟方向命中率 {rate:.1f}% "
          f"({'≥55%有预测力' if rate >= 55 else '当前样本不足以下结论'})")
print()
print("注: 数据窗口约26小时, 该结果仅作初步验证; 信号正式接入前需积累≥2周数据复测")
