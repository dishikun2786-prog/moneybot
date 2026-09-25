#!/usr/bin/env python3
"""FDTD 波场信号回放验证 (用 kline_1m 做价格压力场, 验证 E_n/P_dense 的预测力)
数据: data/kline_1m (Bybit 1分钟K线)
方法: 滚动窗口构建 z-score 压力场 u(x) → FDTD 推进 → P_dense/E_n
      → 与未来 H 根K线的 |收益率| 做相关性 + 分位对比
用法: ./venv/bin/python fdtd_backtest.py [symbol] [窗口] [前瞻H]
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # ~/polymarket
import duckdb
import numpy as np
import wave_field as W

DB_GLOB = "data/kline_1m/year=*/month=*/*.parquet"
SYM = sys.argv[1] if len(sys.argv) > 1 else "BTCUSDT"
WINDOW = int(sys.argv[2]) if len(sys.argv) > 2 else 64
HORIZON = int(sys.argv[3]) if len(sys.argv) > 3 else 3

res = duckdb.query(
    f"SELECT ts, close, volume FROM read_parquet('{DB_GLOB}') "
    f"WHERE symbol='{SYM}' ORDER BY ts"
).fetchnumpy()
close = res["close"].astype(float)
vol = res["volume"].astype(float)
print(f"{SYM}: {len(close)} 根K线, 窗口={WINDOW}, 前瞻={HORIZON}根")

def rolling_features(close, vol, window, horizon):
    """返回 (P_dense, E_n, fwd_abs_ret) 三个对齐数组。"""
    n = len(close)
    p_dense, e_n, fwd = [], [], []
    for t in range(window, n - horizon, 1):
        w = close[t - window:t]
        # 压力场: z-score 偏离
        mu, sd = w.mean(), w.std()
        u = (w - mu) / sd if sd > 0 else np.zeros_like(w)
        g = W.WaveGrid(window, dx=1.0 / (window - 1), dt=0.01, gamma=0.5)
        g.u[:] = u
        g.u_prev[:] = u
        g.F[:] = u
        g.set_wave_speed(np.full(window, 1.0))
        for _ in range(10):
            g.step()
        p_dense.append(g.dense_pressure())
        e_n.append(g.energy_flux())
        fwd.append(abs(close[t + horizon] / close[t] - 1.0))
    return np.array(p_dense), np.array(e_n), np.array(fwd)

P, E, F = rolling_features(close, vol, WINDOW, HORIZON)
print(f"样本数: {len(P)}")

# 相关性 (Pearson)
def pearson(a, b):
    return float(np.corrcoef(a, b)[0, 1])

print(f"Pearson(P_dense, |前瞻收益|) = {pearson(P, F):+.4f}")
print(f"Pearson(E_n,     |前瞻收益|) = {pearson(E, F):+.4f}")

# 分位对比: P_dense 最高/最低 20% 的平均前瞻波动
q_hi = np.quantile(P, 0.8)
q_lo = np.quantile(P, 0.2)
hi = F[P >= q_hi]
lo = F[P <= q_lo]
print(f"P_dense 高20% 平均|前瞻收益| = {hi.mean()*100:.4f}%  (n={len(hi)})")
print(f"P_dense 低20% 平均|前瞻收益| = {lo.mean()*100:.4f}%  (n={len(lo)})")
print(f"比值(高/低) = {hi.mean()/max(lo.mean(),1e-12):.2f}  (>1 表示信号有预测力)")
