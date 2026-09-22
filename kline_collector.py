#!/usr/bin/env python3
"""K线采集器: Bybit 永续 BTCUSDT/ETHUSDT, 8间隔(1m/5m/15m/1h/4h/D/W/M)
回填: 向后翻页 (end=oldest-1, Bybit kline 单次1000条)
增量: 每60s拉最近3根, 按(ts,symbol)去重后追加
落库: logs/kline_{interval}.jsonl → data_writer 重建 parquet (分区 year/month)
"""
import argparse
import json
import os
import time
import urllib.request

BASE = os.path.expanduser("~/polymarket")
LOGS = f"{BASE}/logs"
# Bybit API 间隔代码: 分钟级用数字(1/5/15/60/240), D/W/M 用字符串
INTERVALS = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240, "D": "D", "W": "W", "M": "M"}
INTERVAL_SEC = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600,
                "4h": 14400, "D": 86400, "W": 604800, "M": 2592000}
BACKFILL_DAYS = {"1m": 30, "5m": 60, "15m": 90, "1h": 180,
                 "4h": 365, "D": 730, "W": 730, "M": 1095}
SYMBOLS = ["BTCUSDT", "ETHUSDT"]
URL = "https://api.bybit.com/v5/market/kline"


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return json.loads(urllib.request.urlopen(req, timeout=20).read())


def fetch(symbol, interval_key, start_ms, end_ms):
    """单页1000根 (desc: 最新在前); interval_key→API代码"""
    code = INTERVALS[interval_key]
    url = (f"{URL}?category=linear&symbol={symbol}&interval={code}"
           f"&start={start_ms}&end={end_ms}&limit=1000")
    return get(url)["result"]["list"] or []


def backfill(symbol, interval_key, days):
    """向后翻页拉满 days 天历史"""
    sec = INTERVAL_SEC[interval_key]
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * 86400 * 1000
    bars = []
    cur_end = end_ms
    while True:
        rs = fetch(symbol, interval_key, start_ms, cur_end)
        if not rs:
            break
        bars.extend(rs)
        oldest = int(rs[-1][0])
        if len(rs) < 1000 or oldest <= start_ms:
            break
        cur_end = oldest - 1
        time.sleep(0.25)
    # 升序标准化: [ts_ms, o, h, l, c, v]  (Bybit返回为开高低收量)
    out = []
    for r in bars:
        try:
            ts, o, h, l, c, v = int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])
            out.append([ts, o, h, l, c, v])
        except Exception:
            continue
    out.sort(key=lambda x: x[0])
    # 去重
    seen, dedup = set(), []
    for b in out:
        if b[0] in seen:
            continue
        seen.add(b[0])
        dedup.append(b)
    return dedup


def load_existing(path, tail=800):
    """读取尾部行建立(ts,symbol)集合 (双标的共享文件必须双键去重)"""
    seen = set()
    if os.path.exists(path):
        with open(path) as f:
            lines = f.readlines()[-tail:]
        for line in lines:
            try:
                r = json.loads(line)
                seen.add((r[0], r[1]))
            except Exception:
                pass
    return seen


def append_bars(path, bars):
    with open(path, "a", encoding="utf-8") as f:
        for b in bars:
            f.write(json.dumps(b) + "\n")


def do_backfill():
    t0 = time.time()
    for sym in SYMBOLS:
        for iv in INTERVALS:
            path = f"{LOGS}/kline_{iv}.jsonl"
            seen = load_existing(path)
            bars = backfill(sym, iv, BACKFILL_DAYS[iv])
            new2 = []
            for b in bars:
                if (b[0], sym) in seen:
                    continue
                new2.append([b[0], sym] + b[1:])
            append_bars(path, new2)
            print(f"  {sym} {iv}: 新写入 {len(new2)} 根")
    print(f"回填完成 耗时 {time.time()-t0:.0f}s")


def do_incremental():
    """最近4根增量"""
    for sym in SYMBOLS:
        for iv in INTERVALS:
            path = f"{LOGS}/kline_{iv}.jsonl"
            seen = load_existing(path)
            sec = INTERVAL_SEC[iv]
            now = int(time.time() * 1000)
            rs = fetch(sym, iv, now - 5 * sec * 1000, now)
            new2 = []
            for r in rs:
                try:
                    ts = int(r[0])
                    if (ts, sym) in seen:
                        continue
                    seen.add((ts, sym))
                    new2.append([ts, sym, float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])])
                except Exception:
                    continue
            if new2:
                append_bars(path, sorted(new2, key=lambda x: x[0]))
                print(f"  [{time.strftime('%H:%M:%SZ', time.gmtime())}] {sym} {iv}: +{len(new2)}根")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", action="store_true")
    ap.add_argument("--once", action="store_true", help="单次增量(测试用)")
    args = ap.parse_args()
    if args.backfill:
        do_backfill()
    elif args.once:
        do_incremental()
    else:
        while True:
            try:
                do_incremental()
            except KeyboardInterrupt:
                break
            except Exception as e:
                print("  [err]", type(e).__name__, e)
            time.sleep(60)
