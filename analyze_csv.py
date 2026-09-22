#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
analyze_csv.py — 监控数据标定分析 (Phase 1)
1) 数据完整性  2) PM隐含σ vs Deribit参考σ (锚定谁?)  3) 微笑斜率对比
4) 以已实现波动率为锚的实时错价清单
"""
import csv, json, math, statistics as st, urllib.request
from collections import defaultdict

UA = {'User-Agent': 'Mozilla/5.0'}
def http_json(url, data=None, timeout=25):
    req = urllib.request.Request(url, headers=UA,
                                 data=json.dumps(data).encode() if data is not None else None)
    if data is not None: req.add_header('Content-Type', 'application/json')
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())

def ncdf(x): return 0.5 * (1 + math.erf(x / math.sqrt(2)))
def touch_up(S, K, sig, T):
    if K <= S: return 1.0
    if T <= 0 or sig <= 0: return 0.0
    nu = -0.5 * sig * sig; sq = sig * math.sqrt(T); a = math.log(K / S)
    return min(max(ncdf(-(a - nu * T) / sq) + math.exp(2 * nu * a / (sig * sig)) * ncdf(-(a + nu * T) / sq), 0), 1)
def touch_down(S, K, sig, T):
    if K >= S: return 1.0
    if T <= 0 or sig <= 0: return 0.0
    nu = -0.5 * sig * sig; sq = sig * math.sqrt(T); b = math.log(S / K)
    return min(max(ncdf(-(b + nu * T) / sq) + math.exp(-2 * nu * b / (sig * sig)) * ncdf(-(b - nu * T) / sq), 0), 1)
def touch_prob(S, K, sig, T, d): return touch_up(S, K, sig, T) if d == 'up' else touch_down(S, K, sig, T)
FEE = 0.07

def realized_vol(symbol, interval, bars):
    d = http_json(f"https://api.bybit.com/v5/market/kline?category=linear&symbol={symbol}&interval={interval}&limit={bars}")
    closes = [float(x[4]) for x in d['result']['list']][::-1]
    rets = [math.log(closes[i] / closes[i-1]) for i in range(1, len(closes)) if closes[i-1] > 0]
    mu = sum(rets) / len(rets); var = sum((r - mu) ** 2 for r in rets) / (len(rets) - 1)
    per_year = {60: 24 * 365, 15: 96 * 365}[interval]
    return math.sqrt(var * per_year)

rows = list(csv.DictReader(open('logs/bybit_pm_fv.csv', encoding='utf-8')))
ts = sorted(set(r['ts_utc'] for r in rows))
print(f"[完整性] 行数={len(rows)} | 采集轮次={len(ts)} | {ts[0]} → {ts[-1]} UTC | 桶数={len(set((r['event'],r['market'],r['dir'],r['strike']) for r in rows))}")

def asset_of(r): return 'BTC' if 'Bitcoin' in r['event'] else 'ETH'
def exp_tag(r): return (r['deribit_exp'] or '?').split('~')[0]

by_bucket = defaultdict(list)
for r in rows:
    by_bucket[(r['event'].strip(), r['market'].strip(), r['dir'], r['strike'], r['deribit_exp'])].append(r)

# ---------- 锚定分析 ----------
print("\n[锚定对比] 只用双边量≥100的桶 | PM隐含σ vs Deribit参考σ (全样本均值)")
print(f"{'资产':<5}{'到期':<7}{'方向':<5}{'行权价':>8}{'PM隐σ':>8}{'D参考σ':>8}{'差(PM-D)':>9}{'样本':>5}")
summ = []
for k, rs in sorted(by_bucket.items(), key=lambda z: (z[0][0], z[0][3])):
    liq = [r for r in rs if r['pm_impl_sigma'] and float(r['sz_bid'] or 0) >= 100 and float(r['sz_ask'] or 0) >= 100]
    if not liq: continue
    imp = st.mean(float(r['pm_impl_sigma']) for r in liq)
    div = st.mean(float(r['iv']) for r in liq)
    a, d, K = asset_of(liq[-1]), k[2], int(float(k[3]))
    S = float(liq[-1]['S'])
    print(f"{a:<5}{exp_tag(liq[-1]):<7}{d:<5}{K:>8}{imp*100:>8.1f}{div*100:>8.1f}{(imp-div)*100:>9.1f}{len(liq):>5}")
    summ.append((a, d, K, imp, div, S, float(liq[-1]['hedge_btc_per_1k'] or 0)))

# ---------- 微笑斜率 ----------
def slope(pairs):
    n = len(pairs)
    if n < 3: return None
    mx = sum(p[0] for p in pairs) / n; my = sum(p[1] for p in pairs) / n
    num = sum((p[0]-mx)*(p[1]-my) for p in pairs)
    den = sum((p[0]-mx)**2 for p in pairs)
    return num/den if abs(den) > 1e-12 else None

print("\n[微笑斜率] σ ~ ln(K/S)  (负值=远下方σ更高/看跌偏斜)")
for a in ('BTC', 'ETH'):
    pairs_pm = [(math.log(K/S), imp) for (aa, d, K, imp, dv, S, h) in summ if aa == a]
    pairs_dv = [(math.log(K/S), dv) for (aa, d, K, imp, dv, S, h) in summ if aa == a]
    sp, sd = slope(pairs_pm), slope(pairs_dv)
    print(f"  {a}: PM斜率={sp if sp is None else round(sp*100,2)}  Deribit斜率={sd if sd is None else round(sd*100,2)}  (每1% moneyness的σ变化, ×100显示)")

# ---------- 已实现波动率 ----------
rv = {}
for sym, a in (('BTCUSDT', 'BTC'), ('ETHUSDT', 'ETH')):
    rv[a] = (realized_vol(sym, 60, 720), realized_vol(sym, 15, 960))
print(f"\n[已实现波动率] BTC: 30d={rv['BTC'][0]*100:.1f}% 7d≈{rv['BTC'][1]*100:.1f}% | ETH: 30d={rv['ETH'][0]*100:.1f}% 7d≈{rv['ETH'][1]*100:.1f}%")
print("  (7d样本为15m×960=10天窗口, 标注近似)")

# ---------- 实时清单: 已实现锚 ----------
last = [r for r in rows if r['ts_utc'] == ts[-1]]
print(f"\n[实时清单] {ts[-1]} 轮次 | 以已实现波动率(30d)为锚, 扣费后净edge (双边量≥100)")
acts = []
for r in last:
    if not r['pm_ask'] or not r['pm_bid']: continue
    if float(r['sz_ask'] or 0) < 100 or float(r['sz_bid'] or 0) < 100: continue
    a = asset_of(r); S = float(r['S']); K = int(float(r['strike']))
    T = float(r['T_days']) / 365; d = r['dir']
    sig = rv[a][0]
    fair = touch_prob(S, K, sig, T, d)
    ask, bid = float(r['pm_ask']), float(r['pm_bid'])
    nb = (fair - ask) * 100 - FEE * ask * (1 - ask) * 100     # 买入净edge
    ns = (bid - fair) * 100 - FEE * bid * (1 - bid) * 100     # 卖出净edge
    acts.append((max(nb, ns), nb, ns, a, exp_tag(r), d, K, bid, ask, fair, sig, r['hedge_btc_per_1k']))
acts.sort(key=lambda z: -z[0])
print(f"{'动作':<5}{'资产':<5}{'到期':<7}{'方向':<5}{'行权价':>8}{'买价':>6}{'卖价':>6}{'公允P':>7}{'净edge':>8}{'对冲/1k':>9}")
for m, nb, ns, a, e, d, K, bid, ask, fair, sig, h in acts[:12]:
    act = '买' if nb >= ns else '卖'
    net = max(nb, ns)
    print(f"{act:<5}{a:<5}{e:<7}{d:<5}{K:>8}{bid:>6.2f}{ask:>6.2f}{fair:>7.3f}{net:>8.2f}{float(h):>9.3f}")
n_act = sum(1 for x in acts if x[0] > 0.5)
print(f"\n=> 净edge>0.5c的可行动作: {n_act} 个 (共{len(acts)}个合格桶)")
