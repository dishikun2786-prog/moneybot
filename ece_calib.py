#!/usr/bin/env python3
"""M-D 补测: Jev 开仓信号概率校准 (ECE)
- 读 jev_decisions.jsonl 的 open 信号 (ts, sym, P)
- 事后标签: 信号后 24h 该标的 funding 累计 > 0 (carry 盈利条件)
- ECE: P 分桶 vs 实际命中率; 输出校准报告
用法: python3 ece_calib.py [uid] [hours]
"""
import sys
import os
import json
import time
import urllib.request

BASE = os.path.expanduser("~/polymarket")
DEC = os.path.join(BASE, "data", "jev_decisions.jsonl")


def funding_rest(symbol, ts_start, ts_end):
    """Bybit funding history REST (5min 粒度, 分两页拉满24h=288条, 返回 [(ts, rate), ...])"""
    out = []
    try:
        mid = (ts_start + ts_end) // 2
        for a, b in ((ts_start, mid), (mid, ts_end)):
            url = (f"https://api.bybit.com/v5/market/funding/history?category=linear"
                   f"&symbol={symbol}&startTime={a}&endTime={b}&limit=200")
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            d = json.load(urllib.request.urlopen(req, timeout=15))
            if d.get("retCode") != 0:
                return []
            out += [(int(x["fundingRateTimestamp"]), float(x["fundingRate"]))
                    for x in d["result"]["list"]]
        return out
    except Exception as e:
        print(f"  [funding_rest 失败] {symbol}: {e}", file=sys.stderr)
        return []


def main():
    uid = int(sys.argv[1]) if len(sys.argv) > 1 else 27
    now = time.time()
    signals = []
    for line in open(DEC, encoding="utf-8"):
        d = json.loads(line)
        if d.get("uid") != uid or d.get("event") != "cycle":
            continue
        import calendar
        ts = calendar.timegm(time.strptime(d["ts"], "%Y-%m-%dT%H:%M:%SZ"))
        for k, v in (d.get("answers") or {}).items():
            if k.startswith("open_") and v.get("noul") is not None:
                sym = k[5:]
                p = v["noul"]
                if p >= 0.5 and d.get("signals", {}).get("open") and sym in d["signals"]["open"]:
                    signals.append({"ts": d["ts"], "sym": sym, "p": p, "ts_epoch": ts})
    print(f"开仓信号总数: {len(signals)}")
    if signals:
        oldest = min(s["ts_epoch"] for s in signals)
        newest = max(s["ts_epoch"] for s in signals)
        print(f"信号时间跨度: {time.strftime('%m-%d %H:%M', time.gmtime(oldest))} ~ {time.strftime('%m-%d %H:%M', time.gmtime(newest))} (最老距今 {(now-oldest)/3600:.1f}h, 满24h可标注: {now-oldest>=86400})")
    if not signals:
        print("样本不足, 无法校准 (需 ≥30)")
        return
    # 事后标签: 信号后 24h funding 累计
    buckets = {}
    labeled = 0
    for s in signals:
        if s["ts_epoch"] > now - 24 * 3600:  # 太新的信号跳过(未满24h)
            continue
        rates = funding_rest(s["sym"], int(s["ts_epoch"] * 1000), int((s["ts_epoch"] + 24 * 3600) * 1000))
        if not rates:
            continue
        if len(rates) < 259:  # 覆盖率<90% (288条) 视为窗口不全, 跳过
            continue
        lab = sum(r for _, r in rates) > 0
        b = min(int(s["p"] * 10), 9)  # 0.5-0.9+ 分桶
        if b >= 5:
            buckets.setdefault(b, []).append((s["p"], lab))
        labeled += 1
    print(f"可标注信号: {labeled} (满24h且funding可取)")
    print("=" * 56)
    print(f"{'P桶':>6} {'样本':>5} {'实际命中':>8} {'桶中心P':>8} {'偏差':>7}")
    print("-" * 56)
    ece_sum, n_tot = 0.0, 0
    for b in sorted(buckets):
        rows = buckets[b]
        hit = sum(1 for _, l in rows if l)
        acc = hit / len(rows)
        center = (b + 0.5) / 10
        ece_sum += abs(center - acc) * len(rows)
        n_tot += len(rows)
        print(f"{b/10:.1f}-{b/10+0.1:.1f} {len(rows):>5} {hit:>4}/{len(rows):<4} {center:>8.2f} {acc-center:>+7.2f}")
    print("-" * 56)
    if n_tot:
        ece = ece_sum / n_tot
        print(f"ECE = {ece:.3f} ({'校准良好 <0.10' if ece < 0.10 else '校准一般 0.10-0.20' if ece < 0.20 else '校准差 ≥0.20'})")
        overall = sum(1 for rows in buckets.values() for _, l in rows if l) / n_tot
        print(f"总体命中率: {overall:.1%} | 样本: {n_tot}")
        print("结论提示: 命中率显著高于/低于桶中心 → 门控阈值可上调/下调")


if __name__ == "__main__":
    main()
