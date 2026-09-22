#!/usr/bin/env python3
"""现货×永续基差套利回测 v1 (T1.1)
数据: Bybit 15m K线重建基差 (spot+linear) × funding历史(parquet)
策略: 正基差carry (多现货+空永续):
  入场: 年化funding > θ_in (空腿收funding)
  出场: funding翻负 或 基差收敛(|basis|<1bp) 或 持仓>14天
费用: 现货taker 0.1% + 永续taker 0.055% ×2(开平)
输出: 轮次统计 + 年化收益 + funding收入占比 + 单边平仓次数"""
import argparse
import json
import time
import urllib.request

import duckdb

BASE = "/home/ubuntu/polymarket"
FUND_PARQ = f"{BASE}/data/carry_funding.parquet"
NOTIONAL = 100.0  # 名义 100 USDT
FEE_SPOT = 0.001
FEE_PERP = 0.00055
MAX_HOLD_H = 14 * 24
TH_OUT_BP = 1.0  # 基差收敛平仓阈值


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return json.loads(urllib.request.urlopen(req, timeout=20).read())


def klines(category, symbol, start_ms, end_ms):
    """向后翻页: 每次以'当前页最旧bar'为新窗口终点"""
    bars = []
    cur_end = end_ms
    while True:
        url = (f"https://api.bybit.com/v5/market/kline?category={category}"
               f"&symbol={symbol}&interval=15&start={start_ms}&end={cur_end}&limit=1000")
        rs = get(url)["result"]["list"]
        if not rs:
            break
        bars.extend(rs)
        oldest = int(rs[-1][0])
        if len(rs) < 1000 or oldest <= start_ms:
            break
        cur_end = oldest - 1
        time.sleep(0.25)
    bars.sort(key=lambda b: int(b[0]))
    return bars


def build_basis(symbol, start_ms, end_ms):
    sp = {int(b[0]): float(b[4]) for b in klines("spot", symbol, start_ms, end_ms)}
    pp = {int(b[0]): float(b[4]) for b in klines("linear", symbol, start_ms, end_ms)}
    ts = sorted(set(sp) & set(pp))
    return [(t, sp[t], pp[t], (pp[t] - sp[t]) / sp[t] * 10000) for t in ts]


def run(symbol, th_in_pct, th_out_bp, use_settlement_avoid=True):
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - 62 * 24 * 3600 * 1000  # ~2个月
    series = build_basis(symbol, start_ms, end_ms)
    con = duckdb.connect()
    fund = con.execute(
        f"SELECT epoch(ts) AS t, funding_rate FROM read_parquet('{FUND_PARQ}') "
        f"WHERE symbol = '{symbol}' ORDER BY t").fetchall()
    con.close()
    if not series or not fund:
        return None
    # funding 前向填充到每个15m bar (fund t 是秒 → ×1000 对齐bar毫秒)
    fi = 0
    rounds = []
    pos = None
    for t, spx, ppx, b in series:
        while fi < len(fund) - 1 and fund[fi + 1][0] * 1000 <= t:
            fi += 1
        fr = fund[fi][1]
        ann = fr * 3 * 365 * 100  # 年化%
        # 结算点避让: 距00/08/16UTC结算<15分钟不开仓 (27900s=07:45 bar)
        near_settle = use_settlement_avoid and (t // 1000) % 28800 >= 27900
        if pos is None:
            if ann > th_in_pct and not near_settle:
                pos = dict(t0=t, b0=b, funding_acc=0.0)
        else:
            hours = (t - pos["t0"]) / 3600000
            # 持仓期间累计 funding 收入 (空永续收正funding)
            pos["funding_acc"] += fr * NOTIONAL
            close = False
            reason = ""
            if fr < 0:
                close, reason = True, "funding翻转"
            elif abs(b) < th_out_bp:
                close, reason = True, "基差收敛(单边平合约腿)"
            elif hours >= MAX_HOLD_H:
                close, reason = True, "超时"
            if close:
                basis_pnl = (pos["b0"] - b) / 10000 * NOTIONAL  # 基差收益(正carry: 开仓负基差成本)
                fees = (FEE_SPOT + FEE_PERP) * 2 * NOTIONAL
                pnl = basis_pnl + pos["funding_acc"] - fees
                rounds.append(dict(symbol=symbol, t0=time.strftime("%m-%d %H:%M", time.gmtime(pos["t0"] // 1000)),
                                   hold_h=round(hours, 1), b0=round(pos["b0"], 2), b1=round(b, 2),
                                   funding=round(pos["funding_acc"], 3), basis=round(basis_pnl, 3),
                                   fees=round(fees, 3), pnl=round(pnl, 3), reason=reason))
                pos = None
    # 窗口结束时强制平仓 (按最后基差盯市)
    if pos is not None:
        t, spx, ppx, b = series[-1]
        hours = (t - pos["t0"]) / 3600000
        basis_pnl = (pos["b0"] - b) / 10000 * NOTIONAL
        fees = (FEE_SPOT + FEE_PERP) * 2 * NOTIONAL
        pnl = basis_pnl + pos["funding_acc"] - fees
        rounds.append(dict(symbol=symbol, t0=time.strftime("%m-%d %H:%M", time.gmtime(pos["t0"] // 1000)),
                           hold_h=round(hours, 1), b0=round(pos["b0"], 2), b1=round(b, 2),
                           funding=round(pos["funding_acc"], 3), basis=round(basis_pnl, 3),
                           fees=round(fees, 3), pnl=round(pnl, 3), reason="窗口结束"))
    return rounds


def report(rounds, symbol, th):
    if not rounds:
        print(f"{symbol} θ={th}%: 无交易")
        return
    pnls = [r["pnl"] for r in rounds]
    tot = sum(pnls)
    days = 62
    n_ok = sum(1 for p in pnls if p > 0)
    single = sum(1 for r in rounds if r["reason"].startswith("基差"))
    fund_tot = sum(r["funding"] for r in rounds)
    share = f"{fund_tot/tot:.0%}" if abs(tot) > 0.5 else "n/a"
    print(f"{symbol} θ_in={th}%: {len(rounds)}轮 | 总PnL {tot:+.2f}$ | "
          f"年化 {tot/NOTIONAL/days*365*100:+.2f}% | 胜率 {n_ok/len(rounds):.0%} | "
          f"单边平仓 {single}次 | funding收入{tot and fund_tot:+.2f}$(净比{share})")
    for r in rounds[-5:]:
        print(f"   {r['t0']} 持{r['hold_h']:>4}h 基差{r['b0']:+.1f}→{r['b1']:+.1f}bp "
              f"funding{r['funding']:+.2f}$ 费{r['fees']:.2f}$ PnL{r['pnl']:+.2f}$ [{r['reason']}]")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--theta", type=float, default=5.0)
    ap.add_argument("--nobasis", action="store_true", help="关闭基差收敛平仓(只留funding翻转+超时)")
    args = ap.parse_args()
    th_out = -999 if args.nobasis else 1.0
    t0 = time.time()
    for sym in ("BTCUSDT", "ETHUSDT"):
        rounds = run(sym, args.theta, th_out)
        report(rounds, sym, args.theta)
    print(f"耗时 {(time.time()-t0):.0f}s")
