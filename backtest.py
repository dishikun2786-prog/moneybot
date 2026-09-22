#!/usr/bin/env python3
"""回测器 v1 (T2.4A) — fv_snapshot parquet 回放
策略B(均值回归): edge_gross > θ_in 入场(下一快照对手价+1tick滑点成交),
                 edge 回落 < θ_out 平仓; C(时间截断): 持仓超 H 小时强平
费用: PM taker = shares × 0.07 × p(1-p) (crypto 档, 保守统一)
验证: 简单 70/30 时间切分 (CPCV 留待 v2, 数据量更大时)
用法: ./venv/bin/python backtest.py [--grid]  (服务器上运行)"""
import argparse
import csv
import os
import time
from datetime import datetime, timedelta, timezone

import duckdb

DATA = os.path.expanduser("~/polymarket/data")
PARQ = f"{DATA}/fv_snapshot/year=*/month=*/*.parquet"
FEE_RATE = 0.07
SHARES = 100
TICK = 0.01  # 保守: 大部分桶 tick ≤ 0.01


def fee_c(px):
    """taker 费 (美分/股): rate × p(1-p) × 100"""
    return FEE_RATE * px * (1 - px) * 100


def load(con):
    rows = con.execute(f"""
        SELECT epoch(ts) AS t, event, market, best_bid AS bid, best_ask AS ask,
               model_p, edge_buy_c, edge_sell_c
        FROM read_parquet('{PARQ}')
        WHERE best_bid IS NOT NULL AND best_ask IS NOT NULL
          AND best_bid > 0 AND best_ask > 0 AND best_ask >= best_bid
        ORDER BY ts
    """).fetchall()
    # 分组键 = (event, market) — market 标题不唯一, 不同事件可能同名!
    groups = {}
    for t, ev, mk, bid, ask, model, eb, es in rows:
        groups.setdefault((ev, mk), []).append(
            dict(t=t, bid=bid, ask=ask, eb=eb, es=es))
    return groups


def run_strategy(series, th_in, th_out, max_hours):
    """series: 单市场时间序; 返回 trades 列表"""
    trades = []
    pos = None  # dict(side, px, t0, gross_entry_edge)
    i = 0
    n = len(series)
    while i < n - 1:
        r = series[i]
        if pos is None:
            # 入场信号: 用毛edge (edge_net + 已扣的费)
            g_buy = (r["eb"] or 0) + (fee_c(r["ask"]) if r["eb"] is not None else 0)
            g_sell = (r["es"] or 0) + (fee_c(r["bid"]) if r["es"] is not None else 0)
            if g_buy > th_in:
                nxt = series[i + 1]
                px = nxt["ask"] + TICK
                pos = dict(side="BUY", px=px, t0=nxt["t"], g=g_buy)
                trades.append(dict(mkt=None, side="BUY", entry=px, exit=None,
                                   entry_t=nxt["t"], exit_t=None, entry_g=g_buy))
                i += 2
                continue
            if g_sell > th_in:
                nxt = series[i + 1]
                px = nxt["bid"] - TICK
                pos = dict(side="SELL", px=px, t0=nxt["t"], g=g_sell)
                trades.append(dict(side="SELL", entry=px, exit=None,
                                   entry_t=nxt["t"], exit_t=None, entry_g=g_sell))
                i += 2
                continue
            i += 1
        else:
            # 持仓中: 检查平仓
            nxt = series[i + 1]
            hours = (nxt["t"] - pos["t0"]) / 3600
            edge_now = (nxt["eb"] if pos["side"] == "BUY" else nxt["es"]) or 0
            exit_now = edge_now < th_out
            exit_time = hours >= max_hours
            if exit_now or exit_time:
                px = nxt["bid"] - TICK if pos["side"] == "BUY" else nxt["ask"] + TICK
                t = trades[-1]
                t["exit"] = px
                t["exit_t"] = nxt["t"]
                t["exit_reason"] = "time" if exit_time and not exit_now else "edge"
                t["hold_h"] = hours
                pos = None
                i += 2
            else:
                i += 1
    return trades


def trade_pnl(t):
    if t["exit"] is None:
        return None
    if t["side"] == "BUY":
        gross = (t["exit"] - t["entry"]) * SHARES
        fees = SHARES * (fee_c(t["entry"]) + fee_c(t["exit"])) / 100
    else:
        gross = (t["entry"] - t["exit"]) * SHARES
        fees = SHARES * (fee_c(t["entry"]) + fee_c(t["exit"])) / 100
    return gross - fees


def evaluate(groups, th_in, th_out, max_hours):
    all_trades = []
    for (ev, mk), series in groups.items():
        for t in run_strategy(series, th_in, th_out, max_hours):
            t["market"] = f"{ev[:30]}|{mk}"
            all_trades.append(t)
    closed = [t for t in all_trades if t["exit"] is not None]
    pnls = [trade_pnl(t) for t in closed]
    if not pnls:
        return dict(n_trades=0)
    wins = sum(1 for p in pnls if p > 0)
    total = sum(pnls)
    # 最大回撤 (时序近似)
    peak = -1e9
    dd = 0.0
    cum = 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        dd = min(dd, cum - peak)
    fees_usd = sum(SHARES * (fee_c(t["entry"]) + fee_c(t["exit"])) / 100 for t in closed)
    return dict(n_trades=len(all_trades), n_closed=len(closed), total_usd=round(total, 2),
                per_trade_c=round(total / len(closed), 3), win_rate=round(wins / len(closed), 3),
                max_dd=round(-dd, 2), fees_usd=round(fees_usd, 2),
                avg_hold_h=round(sum(t["hold_h"] for t in closed) / len(closed), 1))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--grid", action="store_true", help="参数网格")
    args = ap.parse_args()
    t0 = time.time()
    con = duckdb.connect()
    groups = load(con)
    n_mkts = len(groups)
    n_rows = sum(len(v) for v in groups.values())
    print(f"数据: {n_mkts} 市场, {n_rows} 行, 加载 {(time.time()-t0)*1000:.0f}ms")

    # 70/30 时间切分
    all_ts = [r["t"] for v in groups.values() for r in v]
    cut = sorted(all_ts)[int(len(all_ts) * 0.7)]
    train = {m: [r for r in s if r["t"] <= cut] for m, s in groups.items()}
    test = {m: [r for r in s if r["t"] > cut] for m, s in groups.items()}
    print(f"切分点: {datetime.fromtimestamp(cut, timezone.utc).strftime('%Y-%m-%d %H:%M')}Z "
          f"(训练 {sum(len(v) for v in train.values())} 行 / 测试 {sum(len(v) for v in test.values())} 行)")

    results = []
    if args.grid:
        grid = [(i, o, h) for i in (5, 10, 20, 50) for o in (0, 2.5, 5) for h in (1, 6, 24)]
    else:
        grid = [(10, 2.5, 6)]
    for th_in, th_out, max_h in grid:
        tr = evaluate(train, th_in, th_out, max_h)
        te = evaluate(test, th_in, th_out, max_h)
        row = dict(th_in=th_in, th_out=th_out, max_h=max_h)
        row.update({f"train_{k}": v for k, v in tr.items()})
        row.update({f"test_{k}": v for k, v in te.items()})
        results.append(row)
        print(f"θ_in={th_in:>3} θ_out={th_out:>4} H={max_h:>2} | "
              f"训练: {tr.get('n_closed', 0):>4}笔 总{tr.get('total_usd', 0):>8}$ 每笔{tr.get('per_trade_c', 0):>7}¢ "
              f"胜率{tr.get('win_rate', 0):.2f} | "
              f"测试: {te.get('n_closed', 0):>4}笔 总{te.get('total_usd', 0):>8}$ 每笔{te.get('per_trade_c', 0):>7}¢ "
              f"胜率{te.get('win_rate', 0):.2f}")
    out = os.path.expanduser("~/polymarket/logs/backtest_results.csv")
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=results[0].keys())
        w.writeheader()
        w.writerows(results)
    print(f"结果已存: {out}  总耗时 {(time.time()-t0)*1000:.0f}ms")


if __name__ == "__main__":
    main()
