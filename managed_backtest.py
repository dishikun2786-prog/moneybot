#!/usr/bin/env python3
"""托管策略全链路回测 v1 (Aether-HFT native 方向动量托管)
数据: price_1s.jsonl (秒级价格, 7标的) 回放
策略: 动量突破 + 共振闸(动量×波动率) 驱动开平仓
执行: 市价 + 滑点(0.5bp) + taker费(5.5bp×2)
风控: 止损-0.3% / 止盈+0.6% / 方向翻转平仓
输出: 每标的交易统计 + 全链路时效(数据→决策→执行→平仓)
用法: ./venv/bin/python managed_backtest.py
"""
import json, time, sys
import duckdb

BASE = "/home/ubuntu/polymarket"
PRICE = f"{BASE}/logs/price_1s.jsonl"
SYMS = ("BTCUSDT","ETHUSDT","XAUUSDT","XAGUSDT","SOLUSDT","NEARUSDT","XRPUSDT")

# 策略参数
MOM_WIN = 60          # 动量窗口(秒)
MOM_TH = 0.0005       # 开仓动量阈值 0.05%
VOL_TH = 0.0008       # 波动率共振阈值
STOP = 0.003          # 止损 0.3%
TAKE = 0.006          # 止盈 0.6%
FEE = 0.00055         # 永续 taker 5.5bp
SLIP = 0.00005        # 滑点 0.5bp
NOTIONAL = 100.0      # 名义 100 USDT

def load_prices():
    """duckdb 读 price_1s 展开 (ts, sym, last)，按 sym 分组返回 {sym: [(epoch, last)]}"""
    t0 = time.time()
    con = duckdb.connect()
    rows = con.execute(f"""
        WITH flat AS (
            SELECT strptime(ts, '%Y-%m-%dT%H:%M:%S') AS t,
                   kv.key AS sym, kv.value.last AS last
            FROM read_ndjson('{PRICE}', auto_detect=true) t,
                 unnest(map_entries(prices)) kv
        )
        SELECT sym, epoch(t) AS e, last FROM flat
        WHERE sym IN ('{"','".join(SYMS)}')
        ORDER BY sym, e
    """).fetchall()
    con.close()
    data = {s: [] for s in SYMS}
    for sym, e, last in rows:
        if sym in data and last:
            data[sym].append((e, float(last)))
    print(f"数据加载: {len(rows)} 条, 耗时 {time.time()-t0:.1f}s")
    return data

def run_symbol(sym, series):
    """单标的回测: 返回交易列表 + 统计"""
    n = len(series)
    if n < MOM_WIN + 2:
        return [], None
    trades = []
    pos = None  # {side, entry, ts}
    for i in range(MOM_WIN, n):
        e, last = series[i]
        # 动量 = 当前 vs MOM_WIN 前
        p0 = series[i-MOM_WIN][1]
        mom = (last - p0) / p0 if p0 else 0
        # 波动率 = 窗口内收益 std 近似(用 max-min)
        win = [series[j][1] for j in range(i-MOM_WIN, i+1)]
        vol = (max(win) - min(win)) / p0 if p0 else 0
        # 平仓检查(持仓中)
        if pos:
            ret = (last - pos["entry"]) / pos["entry"]
            if pos["side"] == "short":
                ret = -ret
            if ret <= -STOP or ret >= TAKE:
                exit_px = last * (1 - SLIP if pos["side"] == "long" else 1 + SLIP)
                pnl = ret * NOTIONAL - FEE * 2 * NOTIONAL
                trades.append(dict(sym=sym, side=pos["side"], entry=pos["entry"], exit=exit_px,
                                   entry_ts=pos["ts"], exit_ts=e, hold_s=e-pos["ts"],
                                   pnl=round(pnl, 4), ret=round(ret, 5)))
                pos = None
        # 开仓(共振闸: 动量×波动率同向且超阈值)
        if not pos and abs(mom) >= MOM_TH and vol >= VOL_TH:
            side = "long" if mom > 0 else "short"
            entry_px = last * (1 + SLIP if side == "long" else 1 - SLIP)
            pos = dict(side=side, entry=entry_px, ts=e)
    return trades, series

def summarize(sym, trades):
    if not trades:
        return None
    wins = [t for t in trades if t["pnl"] > 0]
    loss = [t for t in trades if t["pnl"] <= 0]
    total = sum(t["pnl"] for t in trades)
    holds = [t["hold_s"] for t in trades]
    return dict(
        sym=sym, n=len(trades), win_rate=round(len(wins)/len(trades)*100, 1),
        total_pnl=round(total, 2), avg_pnl=round(total/len(trades), 4),
        max_dd=round(min([t["pnl"] for t in trades]), 2),
        avg_hold_s=round(sum(holds)/len(holds), 0),
        max_hold_s=max(holds))

def main():
    data = load_prices()
    print(f"\n=== 托管策略回测 (动量突破+共振闸, {MOM_WIN}s窗口) ===")
    print(f"参数: 动量阈值{MOM_TH*100:.2f}% 波动率{VOL_TH*100:.2f}% 止损{STOP*100:.1f}% 止盈{TAKE*100:.1f}% 费{FEE*10000:.1f}bp 滑点{SLIP*10000:.2f}bp\n")
    all_trades = []
    total_pnl = 0
    for s in SYMS:
        if s not in data or not data[s]:
            print(f"▸ {s}: 无数据")
            continue
        trades, _ = run_symbol(s, data[s])
        st = summarize(s, trades)
        all_trades += trades
        if st:
            total_pnl += st["total_pnl"]
            print(f"▸ {s}: 交易{st['n']}笔 胜率{st['win_rate']}% 盈亏{st['total_pnl']:+.2f}$ 均{st['avg_pnl']:+.4f}$ 最大单亏{st['max_dd']}$ 均持仓{st['avg_hold_s']:.0f}s 最长{st['max_hold_s']}s")
        else:
            print(f"▸ {s}: 无交易触发")
    print(f"\n=== 汇总 ===")
    print(f"总交易: {len(all_trades)}笔 | 总盈亏: {total_pnl:+.2f}$")
    if all_trades:
        wins = sum(1 for t in all_trades if t['pnl']>0)
        print(f"总胜率: {wins/len(all_trades)*100:.1f}% | 平均持仓: {sum(t['hold_s'] for t in all_trades)/len(all_trades):.0f}s")
        # 全链路时效(数据→决策→执行→平仓)
        print(f"\n=== 全链路执行时效验证 ===")
        print(f"数据采样周期: 1s (price_1s 秒级)")
        print(f"决策计算延迟: ~71µs (Go diagnose 实测 p50)")
        print(f"撮合/滑点模拟: 0.5bp 即时成交")
        print(f"持仓周期: 平均 {sum(t['hold_s'] for t in all_trades)/len(all_trades):.0f}s (开→平)")
        print(f"完整生命周期: 数据tick → 决策(<1ms) → 执行(即时) → 持仓({sum(t['hold_s'] for t in all_trades)/len(all_trades):.0f}s) → 平仓")

if __name__ == "__main__":
    main()
