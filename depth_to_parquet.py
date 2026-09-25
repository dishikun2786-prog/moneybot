#!/usr/bin/env python3
"""Bybit L2 深度按秒落盘 → 分区 Parquet (FDTD 深度场回放用)
读 logs/bybit_depth_1s_*.jsonl (日切 long 格式), 每个已完成的日期写一个 parquet 分区。
每日 timer 运行一次; 只归档 < 今天的已完成日文件, 当天仍在写入的跳过。
"""
import os, glob, time
import duckdb

BASE = os.path.expanduser("~/polymarket")
DATA = f"{BASE}/data"
LOGS = f"{BASE}/logs"


def day_from_name(fn):
    return os.path.basename(fn).replace("bybit_depth_1s_", "").replace(".jsonl", "")


def main():
    con = duckdb.connect()
    done = set()
    for p in glob.glob(f"{DATA}/book_1s_bybit/year=*/month=*/*.parquet"):
        done.add(os.path.basename(p).replace("book_1s_bybit_", "").replace(".parquet", ""))

    today = time.strftime("%Y%m%d", time.gmtime())
    archived = 0
    for src in sorted(glob.glob(f"{LOGS}/bybit_depth_1s_*.jsonl")):
        day = day_from_name(src)
        if day >= today or day in done:
            continue
        y, m = day[0:4], day[4:6]
        dest_dir = f"{DATA}/book_1s_bybit/year={y}/month={m}"
        os.makedirs(dest_dir, exist_ok=True)
        dest = f"{dest_dir}/book_1s_bybit_{day}.parquet"
        tmp = dest + ".tmp"
        sel = f"""SELECT TRY_CAST(ts AS TIMESTAMP) AS ts, symbol, side,
                  TRY_CAST(level AS INTEGER) AS level,
                  TRY_CAST(price AS DOUBLE) AS price,
                  TRY_CAST(size AS DOUBLE) AS size
                  FROM read_json_auto('{src}', format='newline_delimited')
                  WHERE TRY_CAST(ts AS TIMESTAMP) IS NOT NULL"""
        con.execute(f"COPY ({sel}) TO '{tmp}' (FORMAT PARQUET, COMPRESSION ZSTD)")
        os.replace(tmp, dest)
        n = con.execute(f"SELECT count(*) FROM read_parquet('{dest}')").fetchone()[0]
        print(f"book_1s_bybit {day}: {n} 行 -> {dest}")
        archived += 1
    con.close()
    print(f"归档 {archived} 个日文件")


if __name__ == "__main__":
    main()
