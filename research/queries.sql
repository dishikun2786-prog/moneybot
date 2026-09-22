-- Polymarket 量化分析常用查询（DuckDB 直读 E:\量化数据\pm_data\）
-- 运行方式: uv run --with duckdb q.py <本文件>    (q.py 在同目录 research/)
-- 或服务器上: ~/polymarket/venv/bin/python -c "import duckdb; duckdb.query(open('xxx.sql').read()).show()"

-- 1) 错价存续期分布（|edge|>10分的信号从出现到消失的平均寿命, 单位: 秒）
WITH t AS (
  SELECT market, epoch(lead(ts) OVER (PARTITION BY market ORDER BY ts)) - epoch(ts) AS life_s
  FROM read_parquet('E:/量化数据/pm_data/fv_snapshot/year=*/month=*/*.parquet')
  WHERE abs(edge_buy_c) > 10 OR abs(edge_sell_c) > 10
)
SELECT market, count(*) n, round(avg(life_s), 1) avg_life_s
FROM t WHERE life_s IS NOT NULL
GROUP BY market ORDER BY n DESC LIMIT 20;

-- 2) edge 分层统计（验证"edge 越大期望越高"是否成立; 2美分一档）
SELECT floor(edge_buy_c / 2) AS bucket_2c, count(*) n,
       round(avg(edge_buy_c), 2) avg_edge_c
FROM read_parquet('E:/量化数据/pm_data/fv_snapshot/year=*/month=*/*.parquet')
WHERE edge_buy_c > 0 AND edge_buy_c <= 20
GROUP BY bucket_2c ORDER BY bucket_2c;

-- 3) 监控延迟 P50/P99
SELECT round(median(cycle_ms)) p50_ms, round(quantile_cont(cycle_ms, 0.99)) p99_ms
FROM read_parquet('E:/量化数据/pm_data/latency/year=*/month=*/*.parquet');

-- 4) 最活跃市场: 模型价 vs 市场价 时间序列
SELECT ts, market, model_p, best_bid, best_ask, edge_buy_c, edge_sell_c
FROM read_parquet('E:/量化数据/pm_data/fv_snapshot/year=*/month=*/*.parquet')
WHERE market = (
  SELECT market FROM read_parquet('E:/量化数据/pm_data/fv_snapshot/year=*/month=*/*.parquet')
  GROUP BY market ORDER BY count(*) DESC LIMIT 1
)
ORDER BY ts DESC LIMIT 100;
