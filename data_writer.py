#!/usr/bin/env python3
"""数据平台写入器 — CSV/JSONL → 分区 Parquet (DuckDB, ZSTD)
当前: 每次运行全量重建当日分区（数据量小、实现简单可靠；量级增长后改增量）
单写者: 仅由本脚本 + systemd timer 驱动，其他进程不得写 parquet"""
import os, time
from datetime import datetime, timezone
import duckdb

BASE = os.path.expanduser("~/polymarket")
CSV = f"{BASE}/logs/bybit_pm_fv.csv"
EVENTS = f"{BASE}/logs/events.jsonl"
DATA = f"{BASE}/data"

# 纯 TRY_CAST（不 NULLIF）: 列被推断为 VARCHAR 时空串→NULL, 推断为数值时直通
FV_SELECT = """SELECT
  TRY_CAST(ts_utc AS TIMESTAMP) AS ts, event, market, dir,
  TRY_CAST(strike AS DOUBLE) AS strike,
  TRY_CAST("S" AS DOUBLE) AS spot,
  TRY_CAST(T_days AS DOUBLE) AS T_days,
  TRY_CAST(iv AS DOUBLE) AS iv,
  deribit_exp,
  TRY_CAST(pm_bid AS DOUBLE) AS best_bid,
  TRY_CAST(pm_ask AS DOUBLE) AS best_ask,
  TRY_CAST(sz_bid AS DOUBLE) AS bid_size,
  TRY_CAST(sz_ask AS DOUBLE) AS ask_size,
  TRY_CAST(model_p AS DOUBLE) AS model_p,
  TRY_CAST(edge_buy_net_c AS DOUBLE) AS edge_buy_c,
  TRY_CAST(edge_sell_net_c AS DOUBLE) AS edge_sell_c,
  TRY_CAST(pm_impl_sigma AS DOUBLE) AS pm_impl_sigma,
  TRY_CAST(hedge_btc_per_1k AS DOUBLE) AS hedge_per_1k
FROM read_csv_auto('{}')
WHERE TRY_CAST(ts_utc AS TIMESTAMP) IS NOT NULL"""

def _partition_path(dataset, ts):
    d = ts.strftime('%Y%m%d')
    y, m = d[0:4], d[4:6]
    p = f"{DATA}/{dataset}/year={y}/month={m}/{dataset}_{d}.parquet"
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p

def rebuild(con, dataset, select_sql, src_exists):
    if not src_exists:
        return None
    today = datetime.now(timezone.utc)
    dest = _partition_path(dataset, today)
    tmp = dest + '.tmp'
    t0 = time.time()
    con.execute(f"COPY ({select_sql}) TO '{tmp}' (FORMAT PARQUET, COMPRESSION ZSTD)")
    os.replace(tmp, dest)
    # 清理旧分区文件: 新文件含全量历史, 旧文件不清理会导致 glob 读取重复计数
    import glob
    for old in glob.glob(f"{DATA}/{dataset}/year=*/month=*/*.parquet"):
        if os.path.abspath(old) != os.path.abspath(dest):
            try:
                os.remove(old)
            except OSError:
                pass
    n = con.execute(f"SELECT count(*) FROM read_parquet('{dest}')").fetchone()[0]
    return n, (time.time() - t0) * 1000

def main():
    con = duckdb.connect()
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}Z] data_writer start")
    if os.path.exists(CSV):
        r = rebuild(con, "fv_snapshot", FV_SELECT.format(CSV), True)
        if r:
            n, ms = r
            sz = os.path.getsize(_partition_path("fv_snapshot", datetime.now(timezone.utc))) // 1024
            print(f"  fv_snapshot: {n} 行, {ms:.0f}ms, {sz}KB")
    if os.path.exists(EVENTS):
        sel = f"""SELECT TRY_CAST(ts AS TIMESTAMP) AS ts, cycle_ms, bybit_ms, pm_ms,
                  buckets, actionable_buy, actionable_sell
                  FROM read_json_auto('{EVENTS}', format='newline_delimited')
                  WHERE ts IS NOT NULL"""
        r = rebuild(con, "latency", sel, True)
        if r:
            n, ms = r
            print(f"  latency: {n} 行, {ms:.0f}ms")
    carry = f"{BASE}/logs/carry_1m.jsonl"
    if os.path.exists(carry):
        sel = f"""SELECT TRY_CAST(ts AS TIMESTAMP) AS ts, symbol,
                  TRY_CAST(spot AS DOUBLE) AS spot,
                  TRY_CAST(perp_last AS DOUBLE) AS perp_last,
                  TRY_CAST(perp_mark AS DOUBLE) AS perp_mark,
                  TRY_CAST(funding_rate AS DOUBLE) AS funding_rate,
                  TRY_CAST(basis_mark_bp AS DOUBLE) AS basis_mark_bp,
                  TRY_CAST(basis_last_bp AS DOUBLE) AS basis_last_bp,
                  TRY_CAST(ann_funding_pct AS DOUBLE) AS ann_funding_pct,
                  TRY_CAST(open_interest AS DOUBLE) AS open_interest
                  FROM read_json_auto('{carry}', format='newline_delimited')
                  WHERE ts IS NOT NULL"""
        r = rebuild(con, "carry_1m", sel, True)
        if r:
            n, ms = r
            print(f"  carry_1m: {n} 行, {ms:.0f}ms")

    # R13c PM-OFF: books_1s 是 PM 盘口留痕, PM 下线后停写 (5.7GB 全量读曾贡献 ~300MB 内存)
    if False:
        books = f"{BASE}/logs/books_1s.jsonl"
        if os.path.exists(books):
            sel = f"""SELECT TRY_CAST(ts AS TIMESTAMP) AS ts, asset_id,
                      TRY_CAST(best_bid AS DOUBLE) AS best_bid,
                      TRY_CAST(best_ask AS DOUBLE) AS best_ask,
                      TRY_CAST(bid_size AS DOUBLE) AS bid_size,
                      TRY_CAST(ask_size AS DOUBLE) AS ask_size
                      FROM read_json_auto('{books}', format='newline_delimited')
                      WHERE ts IS NOT NULL"""
            r = rebuild(con, "book_1s", sel, True)
            if r:
                n, ms = r
                print(f"  book_1s: {n} 行, {ms:.0f}ms")

    # K线数据集 (8间隔; JSONL行=[ts_ms, symbol, o, h, l, c, v]; 按(symbol,ts)去重)
    for key in ("1m", "5m", "15m", "1h", "4h", "D", "W", "M"):
        src = f"{BASE}/logs/kline_{key}.jsonl"
        if os.path.exists(src):
            sel = f"""WITH raw AS (
              SELECT json[1] AS ts_ms, json[2] AS symbol,
                     json[3] AS o, json[4] AS h, json[5] AS l, json[6] AS c, json[7] AS v
              FROM read_json_auto('{src}', format='newline_delimited',
                                  columns={{'json': 'VARCHAR[]'}})
            ), dedup AS (
              SELECT *, row_number() OVER (PARTITION BY symbol, ts_ms ORDER BY ts_ms) AS rn
              FROM raw
            )
            SELECT to_timestamp(TRY_CAST(ts_ms AS BIGINT) / 1000) AS ts, symbol,
                   TRY_CAST(o AS DOUBLE) AS open, TRY_CAST(h AS DOUBLE) AS high,
                   TRY_CAST(l AS DOUBLE) AS low, TRY_CAST(c AS DOUBLE) AS close,
                   TRY_CAST(v AS DOUBLE) AS volume
            FROM dedup WHERE rn = 1"""
            r = rebuild(con, f"kline_{key}", sel, True)
            if r:
                n, ms = r
                print(f"  kline_{key}: {n} 行, {ms:.0f}ms")
    con.close()

if __name__ == "__main__":
    main()
