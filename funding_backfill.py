#!/usr/bin/env python3
"""funding 历史回填 (T0.2): Bybit /v5/market/funding/history → carry_funding.parquet
每个标的翻页回溯 (200条/请求, cursor分页, 500ms间隔); 6个月≈660条/标的"""
import json
import os
import time
import urllib.request

import duckdb

BASE = os.path.expanduser("~/polymarket")
OUT = f"{BASE}/logs/carry_funding.jsonl"
PARQ = f"{BASE}/data/carry_funding.parquet"
SYMBOLS = ["BTCUSDT", "ETHUSDT"]


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return json.loads(urllib.request.urlopen(req, timeout=15).read())


def fetch_all(symbol):
    rows, cursor, pages = [], "", 0
    while pages < 50:  # 上限1万条, 足够
        url = (f"https://api.bybit.com/v5/market/funding/history"
               f"?category=linear&symbol={symbol}&limit=200")
        if cursor:
            url += f"&cursor={cursor}"
        d = get(url)
        rs = d.get("result", {}).get("list") or []
        if not rs:
            break
        rows.extend(rs)
        cursor = d.get("result", {}).get("nextPageCursor") or ""
        pages += 1
        if not cursor:
            break
        time.sleep(0.5)
    return rows


def main():
    all_rows = []
    for sym in SYMBOLS:
        rows = fetch_all(sym)
        # 统一字段: symbol, funding_ts(ms), funding_rate
        for r in rows:
            all_rows.append({"symbol": sym,
                             "funding_ts": int(r["fundingRateTimestamp"]),
                             "funding_rate": float(r["fundingRate"])})
        print(f"{sym}: {len(rows)} 条 funding 记录")
    with open(OUT, "w", encoding="utf-8") as f:
        for r in all_rows:
            f.write(json.dumps(r) + "\n")
    con = duckdb.connect()
    con.execute(f"""
        COPY (SELECT symbol, to_timestamp(funding_ts/1000) AS ts, funding_rate
              FROM read_json_auto('{OUT}', format='newline_delimited'))
        TO '{PARQ}' (FORMAT PARQUET, COMPRESSION ZSTD, OVERWRITE_OR_IGNORE)
    """)
    r = con.execute(f"SELECT symbol, count(*), min(ts), max(ts), "
                    f"round(avg(funding_rate)*3*365*100,2) FROM read_parquet('{PARQ}') GROUP BY symbol").fetchall()
    for row in r:
        print(f"  {row[0]}: {row[1]}条 | {str(row[2])[:10]} ~ {str(row[3])[:10]} | 平均年化 {row[4]}%")
    print(f"parquet: {PARQ}")


if __name__ == "__main__":
    main()
