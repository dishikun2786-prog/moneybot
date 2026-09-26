#!/usr/bin/env python3
"""funding 历史合并: carry_funding.jsonl(backfill+持续落盘) → parquet 去重合并
每日定时运行, 保证 funding 历史持续积累到 parquet (供回测/策略读取)。
"""
import duckdb

BASE = "/home/ubuntu/polymarket"
JSONL = f"{BASE}/logs/carry_funding.jsonl"
PARQ = f"{BASE}/data/carry_funding.parquet"

def main():
    con = duckdb.connect()
    con.execute(f"""
        COPY (
            SELECT symbol, to_timestamp(funding_ts/1000) AS ts, funding_rate
            FROM read_json_auto('{JSONL}', format='newline_delimited')
            QUALIFY row_number() OVER (PARTITION BY symbol, funding_ts ORDER BY funding_ts) = 1
        ) TO '{PARQ}' (FORMAT PARQUET, COMPRESSION ZSTD, OVERWRITE_OR_IGNORE)
    """)
    r = con.execute(f"""
        SELECT symbol, count(*), min(ts), max(ts), round(avg(funding_rate)*3*365*100,2)
        FROM read_parquet('{PARQ}') GROUP BY symbol ORDER BY symbol
    """).fetchall()
    con.close()
    print("[funding_merge] funding 历史合并完成:")
    for row in r:
        print(f"  {row[0]}: {row[1]}条 | {str(row[2])[:10]} ~ {str(row[3])[:10]} | 平均年化 {row[4]}%")

if __name__ == "__main__":
    main()
