#!/usr/bin/env python3
"""现货×永续基差监控 (T0.1): 每60s抓 Bybit 现货/永续行情 → carry_1m.jsonl
字段: ts/symbol/spot/perp_last/perp_mark/funding_rate/next_funding_ts/basis_mark_bp/basis_last_bp/ann_funding_pct/oi"""
import argparse
import json
import os
import time
import urllib.request

BASE = os.path.expanduser("~/polymarket")
OUT = f"{BASE}/logs/carry_1m.jsonl"
SYMBOLS = ["BTCUSDT", "ETHUSDT"]


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return json.loads(urllib.request.urlopen(req, timeout=15).read())


def tickers(category, syms):
    """Bybit tickers 只支持单 symbol 查询 → 逐标的循环"""
    out = {}
    for s in syms:
        url = f"https://api.bybit.com/v5/market/tickers?category={category}&symbol={s}"
        for t in get(url)["result"]["list"]:
            out[t["symbol"]] = t
    return out


def cycle():
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    sp = tickers("spot", SYMBOLS)
    lp = tickers("linear", SYMBOLS)
    rows = []
    for s in SYMBOLS:
        a, b = sp.get(s), lp.get(s)
        if not a or not b:
            continue
        sl, pl, pm = float(a["lastPrice"]), float(b["lastPrice"]), float(b["markPrice"])
        fr = float(b.get("fundingRate") or 0)
        rows.append({"ts": ts, "symbol": s, "spot": sl, "perp_last": pl, "perp_mark": pm,
                     "funding_rate": fr, "next_funding_ts": b.get("nextFundingTime"),
                     "basis_mark_bp": round((pm - sl) / sl * 10000, 3),
                     "basis_last_bp": round((pl - sl) / sl * 10000, 3),
                     "ann_funding_pct": round(fr * 3 * 365 * 100, 3),
                     "open_interest": b.get("openInterest")})
    with open(OUT, "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    for r in rows:
        print(f"[{ts}] {r['symbol']} 现货{r['spot']} 基差{r['basis_mark_bp']:+.2f}bp "
              f"funding{r['funding_rate']*100:+.4f}%/8h 年化{r['ann_funding_pct']:+.2f}%")
    return rows


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    if args.once:
        cycle()
    else:
        while True:
            try:
                cycle()
            except KeyboardInterrupt:
                break
            except Exception as e:
                print("  [err]", type(e).__name__, e)
            time.sleep(60)
