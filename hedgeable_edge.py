#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hedgeable_edge.py — 可对冲Edge检查器 v1.0

原理:
  2×数字期权价值 ≈ 碰价期权价值 (零漂移)。用 Deribit 真实盘口价差组合近似复制
  数字期权: long K1 / short K2, 数量 q=2/(K2-K1) 每 $1 碰价payoff。
  对比 PM 买卖价 → 「卖PM净edge」= PM卖价 − 复制成本(保守: 买腿付ask) − 全部费用。

v1.0 修正:
  - 时间校正: 当 Deribit 可用到期 ≠ PM 目标到期 (如9月目标仅能用9/25期权),
    用微笑IV下的碰价概率比值 P(T_target)/P(T_opt) 校正复制成本
  - --log: 追加到 logs/hedgeable_edge.csv (供持续性检验)

⚠️ 残留近似 (记录在案):
  - 2× 因子为漂移零假设 (触而未收情形有基差; 第2版蒙特卡洛量化)
  - 静态价差近似 (第2版动态delta)
"""
import argparse, csv, os
from datetime import datetime, timezone

import bybit_pm_monitor as bpm

DERIBIT_FEE_RATE = 0.0003
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
CSV_PATH = os.path.join(LOG_DIR, 'hedgeable_edge.csv')

def deribit_chain(currency):
    d = bpm.http_json(f"https://www.deribit.com/api/v2/public/get_book_summary_by_currency?currency={currency}&kind=option", timeout=30)
    out = {}
    for x in (d.get('result') or []):
        parts = (x.get('instrument_name') or '').split('-')
        if len(parts) != 4: continue
        try:
            out[(parts[1], int(parts[2]), parts[3])] = dict(
                bid=float(x.get('bid_price') or 0), ask=float(x.get('ask_price') or 0),
                mark=float(x.get('mark_price') or 0))
        except Exception:
            continue
    return out

def nearest_expiry(chain, target_dt):
    exps = {}
    for (e, K, t) in chain:
        if e not in exps:
            try: exps[e] = datetime.strptime(e, '%d%b%y').replace(tzinfo=timezone.utc, hour=8)
            except Exception: pass
    best = None
    for e, dt in exps.items():
        dist = abs((dt - target_dt).total_seconds())
        if best is None or dist < best[0]: best = (dist, e, dt)
    return (best[1], best[2]) if best else (None, None)

def check_bucket(m, chain, S, book):
    K, d = m['K'], m['direction']
    typ = 'C' if d == 'up' else 'P'
    exp, exp_dt = nearest_expiry(chain, m['exp'])
    if not exp: return None
    strikes = sorted(set(k for (e, k, t) in chain if e == exp and t == typ))
    lo = [s for s in strikes if s < K]; hi = [s for s in strikes if s > K]
    K1 = max(lo) if lo else None; K2 = min(hi) if hi else None
    if K1 is None or K2 is None or K2 <= K1: return None
    q = 2.0 / (K2 - K1)
    def g(k, side): return chain.get((exp, k, typ), {}).get(side, 0.0) or 0.0
    if d == 'up':
        need = [g(K1, 'ask'), g(K2, 'bid'), g(K1, 'bid'), g(K2, 'ask')]
    else:
        need = [g(K2, 'ask'), g(K1, 'bid'), g(K2, 'bid'), g(K1, 'ask')]
    if any(v <= 0 for v in need): return None
    buy_cost = q * S * (need[0] - need[1])
    sell_proc = q * S * (need[2] - need[3])
    # ---- 时间校正 ----
    t_ratio = 1.0
    try:
        now = datetime.now(timezone.utc)
        T_target = max((m['exp'] - now).total_seconds(), 0) / (365 * 86400)
        T_opt = max((exp_dt - now).total_seconds(), 0) / (365 * 86400)
        sig, _ = bpm.iv_lookup(m['currency'], m['exp'], K)
        if T_target > 0 and T_opt > 0:
            p_t = bpm.touch_prob(S, K, sig, T_target, d)
            p_o = bpm.touch_prob(S, K, sig, T_opt, d)
            if p_o > 1e-6:
                t_ratio = min(max(p_t / p_o, 0.3), 5.0)
        buy_cost *= t_ratio; sell_proc *= t_ratio
    except Exception:
        pass
    dfee = q * 2 * DERIBIT_FEE_RATE * S * t_ratio
    bids, asks = book
    pm_bid = bids[0][0] if bids else None
    pm_ask = asks[0][0] if asks else None
    szb = bids[0][1] if bids else 0
    sza = asks[0][1] if asks else 0
    out = dict(exp=exp, K1=K1, K2=K2, t_ratio=t_ratio, buy_cost=buy_cost, sell_proc=sell_proc,
               dfee=dfee, pm_bid=pm_bid, pm_ask=pm_ask, sz=min(szb, sza))
    if pm_ask:
        fee = 0.07 * pm_ask * (1 - pm_ask)
        out['buy_edge_c'] = (sell_proc - pm_ask - fee - dfee) * 100
    if pm_bid:
        fee = 0.07 * pm_bid * (1 - pm_bid)
        out['sell_edge_c'] = (pm_bid - buy_cost - fee - dfee) * 100
    return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--log', action='store_true')
    args = ap.parse_args()
    mkts = bpm.discover_markets()
    if not mkts:
        print("未发现市场"); return
    books = bpm.fetch_books([m['yes_tok'] for m in mkts])
    prices = {c: bpm.bybit_price(s) for s, c in (('BTCUSDT', 'BTC'), ('ETHUSDT', 'ETH'))}
    chains = {c: deribit_chain(c) for c in ('BTC', 'ETH')}
    print(f"[数据] PM桶={len(mkts)} | Deribit链: BTC={len(chains['BTC'])} ETH={len(chains['ETH'])} | S: {prices}")
    rows = []
    for m in mkts:
        S = prices.get(m['currency']); book = books.get(m['yes_tok'])
        if not S or not book: continue
        r = check_bucket(m, chains[m['currency']], S, book)
        if r: rows.append((m, r))
    rows.sort(key=lambda z: -max(z[1].get('buy_edge_c', -99), z[1].get('sell_edge_c', -99)))
    print(f"\n[可对冲净edge v1.0] 保守价+时间校正 | 单位: 美分/$1 | 已扣 PM费+Deribit费")
    print(f"{'资产':<5}{'D到期':<8}{'方向':<5}{'行权价':>8}{'PM买':>6}{'PM卖':>6}{'量':>6}{'复制成本':>8}{'卖复制':>7}{'时间比':>7}{'买PMedge':>9}{'卖PMedge':>9}")
    for m, r in rows[:22]:
        f = lambda v: f"{v:>9.2f}" if v is not None else f"{'--':>9}"
        print(f"{m['currency']:<5}{r['exp']:<8}{m['direction']:<5}{m['K']:>8}{r['pm_bid'] or 0:>6.2f}{r['pm_ask'] or 0:>6.2f}"
              f"{r['sz']:>6.0f}{r['buy_cost']:>8.2f}{r['sell_proc']:>7.2f}{r['t_ratio']:>7.2f}{f(r.get('buy_edge_c'))}{f(r.get('sell_edge_c'))}")
    n_buy = sum(1 for _, r in rows if (r.get('buy_edge_c') or -9) > 0.5)
    n_sell = sum(1 for _, r in rows if (r.get('sell_edge_c') or -9) > 0.5)
    liquid = [(m, r) for m, r in rows if r['sz'] >= 100 and max(r.get('buy_edge_c', -9), r.get('sell_edge_c', -9)) > 1.5]
    print(f"\n=> 评估 {len(rows)} 桶 | edge>0.5c: 买PM {n_buy} / 卖PM {n_sell} | edge>1.5c且量≥100: {len(liquid)} 个")
    if args.log:
        os.makedirs(LOG_DIR, exist_ok=True)
        new = not os.path.exists(CSV_PATH)
        with open(CSV_PATH, 'a', newline='', encoding='utf-8') as fh:
            w = csv.writer(fh)
            if new:
                w.writerow(['ts_utc', 'asset', 'deribit_exp', 'dir', 'strike', 'pm_bid', 'pm_ask', 'size',
                            'repl_cost', 'repl_proc', 't_ratio', 'buy_edge_c', 'sell_edge_c'])
            ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
            for m, r in rows:
                w.writerow([ts, m['currency'], r['exp'], m['direction'], m['K'], r['pm_bid'], r['pm_ask'], r['sz'],
                            f"{r['buy_cost']:.4f}", f"{r['sell_proc']:.4f}", f"{r['t_ratio']:.3f}",
                            f"{r.get('buy_edge_c', ''):.2f}" if r.get('buy_edge_c') is not None else '',
                            f"{r.get('sell_edge_c', ''):.2f}" if r.get('sell_edge_c') is not None else ''])
        print(f"   [已记录] {CSV_PATH}")

if __name__ == '__main__':
    main()
