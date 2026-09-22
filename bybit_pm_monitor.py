#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bybit_pm_monitor.py — Bybit × Polymarket 数字期权错价监控 (Phase 0)

原理:
  Polymarket "What price will BTC/ETH hit ..." 桶市场 = 碰价期权(one-touch digital)。
  用 Deribit 期权 IV 微笑 + 反射原理计算理论碰价概率，与 PM 盘口对比
  → 输出: 错价(edge)、PM隐含IV、对冲Delta。

关键对照: PM隐含IV vs Deribit参考IV (同一个模型反推, 消除公式差异)

数据源(全部公开): Bybit永续 / Deribit期权 / Polymarket gamma+clob
用法:  --once 单次 | --interval N 循环
"""
import argparse, calendar, csv, json, math, os, re, time, urllib.parse, urllib.request
from datetime import datetime, timezone, timedelta

UA = {'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) pm-monitor/1.1'}
BASE = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE, 'logs')
CSV_PATH = os.path.join(LOG_DIR, 'bybit_pm_fv.csv')
FEE_RATE = 0.07
EVENT_CACHE_TTL = 1800
IV_CACHE_TTL = 600

def http_json(url, data=None, timeout=25):
    req = urllib.request.Request(url, headers=UA,
                                 data=json.dumps(data).encode() if data is not None else None)
    if data is not None:
        req.add_header('Content-Type', 'application/json')
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())

# ---------------------------------------------------------------- math
def ncdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

def touch_up(S, K, sig, T):
    if K <= S: return 1.0
    if T <= 0 or sig <= 0: return 0.0
    nu = -0.5 * sig * sig
    sq = sig * math.sqrt(T)
    a = math.log(K / S)
    p1 = ncdf(-(a - nu * T) / sq)
    p2 = math.exp(2.0 * nu * a / (sig * sig)) * ncdf(-(a + nu * T) / sq)
    return min(max(p1 + p2, 0.0), 1.0)

def touch_down(S, K, sig, T):
    if K >= S: return 1.0
    if T <= 0 or sig <= 0: return 0.0
    nu = -0.5 * sig * sig
    sq = sig * math.sqrt(T)
    b = math.log(S / K)
    p1 = ncdf(-(b + nu * T) / sq)
    p2 = math.exp(-2.0 * nu * b / (sig * sig)) * ncdf(-(b - nu * T) / sq)
    return min(max(p1 + p2, 0.0), 1.0)

def touch_prob(S, K, sig, T, direction):
    return touch_up(S, K, sig, T) if direction == 'up' else touch_down(S, K, sig, T)

def delta_btc(S, K, sig, T, direction, h=0.001):
    return (touch_prob(S * (1 + h), K, sig, T, direction) -
            touch_prob(S * (1 - h), K, sig, T, direction)) / (2 * S * h)

def inv_sigma(price, S, K, T, direction):
    if not (0.005 < price < 0.995) or T <= 0: return None
    lo, hi = 0.03, 5.0
    f = lambda s: touch_prob(S, K, s, T, direction) - price
    if f(lo) > 0 or f(hi) < 0: return None
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        if f(mid) > 0: hi = mid
        else: lo = mid
    return 0.5 * (lo + hi)

# ---------------------------------------------------------------- data
def bybit_price(symbol):
    d = http_json(f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={symbol}")
    it = d['result']['list'][0]
    return float(it.get('indexPrice') or it['lastPrice'])

def realized_vol(symbol, days=7):
    """Bybit 15m K线 → 年化已实现波动率"""
    try:
        d = http_json(f"https://api.bybit.com/v5/market/kline?category=linear&symbol={symbol}"
                      f"&interval=15&limit={min(days * 96, 1000)}")
        kl = d['result']['list']
        closes = [float(x[4]) for x in kl][::-1]
        rets = [math.log(closes[i] / closes[i-1]) for i in range(1, len(closes)) if closes[i-1] > 0]
        if len(rets) < 20: return None
        mu = sum(rets) / len(rets)
        var = sum((r - mu) ** 2 for r in rets) / (len(rets) - 1)
        return math.sqrt(var * 96 * 365)
    except Exception:
        return None

_iv_cache = {}
def deribit_iv_table(currency):
    now = time.time()
    c = _iv_cache.get(currency)
    if c and now - c['ts'] < IV_CACHE_TTL:
        return c['data']
    d = http_json(f"https://www.deribit.com/api/v2/public/get_book_summary_by_currency?currency={currency}&kind=option", timeout=30)
    rows = d.get('result') or []
    exp = {}
    for x in rows:
        nm = x.get('instrument_name', '')
        parts = nm.split('-')
        if len(parts) != 4 or not nm.endswith('-C'):
            continue
        try:
            k = int(parts[2]); iv = float(x.get('mark_iv') or 0)
        except Exception:
            continue
        if iv <= 0: continue
        if iv > 3: iv = iv / 100.0
        exp.setdefault(parts[1], []).append((k, iv))
    for e in exp:
        exp[e].sort()
    _iv_cache[currency] = {'ts': now, 'data': exp}
    return exp

def iv_lookup(currency, target_dt, K, fallback=0.55):
    try:
        exp = deribit_iv_table(currency)
        if not exp: return fallback, None

        def iv_at(rows, k):
            ks = [r[0] for r in rows]; ivs = [r[1] for r in rows]
            if k <= ks[0]: return ivs[0]
            if k >= ks[-1]: return ivs[-1]
            for i in range(1, len(ks)):
                if ks[i] >= k:
                    w = (k - ks[i-1]) / (ks[i] - ks[i-1] + 1e-9)
                    return ivs[i-1] * (1 - w) + ivs[i] * w
            return ivs[-1]

        items = []
        for e, rows in exp.items():
            try:
                dt = datetime.strptime(e, '%d%b%y').replace(tzinfo=timezone.utc, hour=8)
            except Exception:
                continue
            items.append((dt, e, rows))
        if not items: return fallback, None
        items.sort(key=lambda z: z[0])
        prev = None; nxt = None
        for it in items:
            if it[0] <= target_dt: prev = it
            elif nxt is None: nxt = it
        if prev and nxt:
            lp = math.log(prev[0].timestamp()); ln = math.log(nxt[0].timestamp()); lt = math.log(target_dt.timestamp())
            w = 0.0 if ln <= lp else min(max((lt - lp) / (ln - lp), 0.0), 1.0)
            iv = iv_at(prev[2], K) * (1 - w) + iv_at(nxt[2], K) * w
            ename = f"{prev[1]}~{nxt[1]}"
        elif prev:
            iv = iv_at(prev[2], K); ename = prev[1]
        else:
            iv = iv_at(nxt[2], K); ename = nxt[1]
        return float(iv), ename
    except Exception:
        return fallback, None

_MONTHS = {m: i + 1 for i, m in enumerate(
    ['January', 'February', 'March', 'April', 'May', 'June', 'July',
     'August', 'September', 'October', 'November', 'December'])}
_MRE = '|'.join(_MONTHS)

def expiry_from_title(title, now):
    """从事件标题推断到期: 周度区间→末日; 月度→月末; 年度/before YYYY→年末; 单日→None(跳过)"""
    y = now.year
    m = re.search(r'(%s)\s+\d{1,2}\s*[-–—]\s*(\d{1,2})' % _MRE, title)
    if m: return datetime(y, _MONTHS[m.group(1)], int(m.group(2)), 23, 59, tzinfo=timezone.utc)
    if re.search(r'\bon\s+(%s)\s+\d{1,2}\b' % _MRE, title): return None
    m = re.search(r'\bin\s+(%s)\b' % _MRE, title)
    if m:
        mn = _MONTHS[m.group(1)]
        return datetime(y, mn, calendar.monthrange(y, mn)[1], 23, 59, tzinfo=timezone.utc)
    m = re.search(r'\bbefore\s+(\d{4})\b', title)
    if m: return datetime(int(m.group(1)) - 1, 12, 31, 23, 59, tzinfo=timezone.utc)
    m = re.search(r'\bin\s+(\d{4})\b', title)
    if m: return datetime(int(m.group(1)), 12, 31, 23, 59, tzinfo=timezone.utc)
    return None

_ev_cache = {'ts': 0, 'data': []}
def discover_markets():
    now = time.time()
    if _ev_cache['data'] and now - _ev_cache['ts'] < EVENT_CACHE_TTL:
        return _ev_cache['data']
    found = []
    try:
        for q in ("What price will Bitcoin hit", "What price will Ethereum hit"):
            s = http_json("https://gamma-api.polymarket.com/public-search?q=" + urllib.parse.quote(q) + "&limit_per_type=10")
            for ev in (s.get('events') or []):
                t = ev.get('title') or ''
                if 'What price will' in t and ev.get('slug') and not ev.get('closed'):
                    found.append(ev['slug'])
    except Exception as ex:
        print("  [warn] search失败:", ex)
    out, seen = [], set()
    year = datetime.now(timezone.utc).year
    for slug in found:
        if slug in seen: continue
        seen.add(slug)
        try:
            d = http_json("https://gamma-api.polymarket.com/events?slug=" + urllib.parse.quote(slug))
            ev = d[0] if isinstance(d, list) and d else (d if isinstance(d, dict) else None)
            if not ev or ev.get('closed'): continue
            title = ev.get('title') or ''
            now_dt = datetime.now(timezone.utc)
            exp_dt = expiry_from_title(title, now_dt)
            if not exp_dt or exp_dt <= now_dt: continue
            cur = 'ETH' if 'Ethereum' in title else 'BTC'
            seenk = set()
            for m in (ev.get('markets') or []):
                if m.get('closed'): continue
                gt = m.get('groupItemTitle') or ''
                mm = re.search(r'([↑↓])\s*([\d,]+)', gt)
                if not mm: continue
                direction = 'up' if mm.group(1) == '↑' else 'down'
                K = int(mm.group(2).replace(',', ''))
                if (slug, direction, K, cur) in seenk: continue
                seenk.add((slug, direction, K, cur))
                try:
                    toks = json.loads(m.get('clobTokenIds') or '[]')
                except Exception:
                    continue
                if len(toks) != 2: continue
                out.append(dict(slug=slug, event=title[:44], market=gt[:18], currency=cur,
                                direction=direction, K=K, exp=exp_dt, yes_tok=toks[0]))
        except Exception as ex:
            print("  [warn] event失败", slug, ex)
    _ev_cache['ts'], _ev_cache['data'] = now, out
    return out

def fetch_books(tokens):
    d = http_json("https://clob.polymarket.com/books", [{"token_id": t} for t in tokens])
    bm = {}
    items = d.values() if isinstance(d, dict) else d
    for b in items:
        if not isinstance(b, dict): continue
        aid = b.get('asset_id') or b.get('assetId')
        bids = sorted([(float(x['price']), float(x['size'])) for x in (b.get('bids') or [])], reverse=True)
        asks = sorted([(float(x['price']), float(x['size'])) for x in (b.get('asks') or [])])
        bm[aid] = (bids, asks)
    return bm

# ---------------------------------------------------------------- cycle
EVENTS_PATH = os.path.join(LOG_DIR, 'events.jsonl')


def log_event(rec):
    """JSONL 传输型事件日志 (每轮一条, 延迟分项+可行动统计)"""
    try:
        with open(EVENTS_PATH, 'a', encoding='utf-8') as ef:
            ef.write(json.dumps(rec, ensure_ascii=False) + '\n')
    except Exception as ex:
        print("  [warn] events log:", ex)


def cycle(csv_writer):
    now = datetime.now(timezone.utc)
    t0 = time.time()
    prices = {}
    for sym, cur in (('BTCUSDT', 'BTC'), ('ETHUSDT', 'ETH')):
        try:
            prices[cur] = bybit_price(sym)
        except Exception as ex:
            print(f"  [warn] bybit {sym}: {ex}")
    t_bybit = (time.time() - t0) * 1000

    mkts = discover_markets()
    if not mkts:
        print("  [warn] 未发现目标市场")
        return []
    t1 = time.time()
    try:
        books = fetch_books([m['yes_tok'] for m in mkts])
    except Exception as ex:
        print("  [warn] PM books:", ex); return []
    t_pm = (time.time() - t1) * 1000

    rows = []
    for m in mkts:
        S = prices.get(m['currency'])
        if not S: continue
        T = (m['exp'] - now).total_seconds() / (365.0 * 86400)
        if T <= 0: continue
        sig, ename = iv_lookup(m['currency'], m['exp'], m['K'])
        model_p = touch_prob(S, m['K'], sig, T, m['direction'])
        book = books.get(m['yes_tok'])
        if not book: continue
        bids, asks = book
        bid = bids[0][0] if bids else None
        ask = asks[0][0] if asks else None
        bsz = bids[0][1] if bids else 0
        asz = asks[0][1] if asks else 0
        mid = ((bid + ask) / 2) if (bid and ask) else (bid or ask)
        fee = lambda p: FEE_RATE * p * (1 - p) * 100
        e_buy = (model_p - ask) * 100 - fee(ask) if ask else None
        e_sell = (bid - model_p) * 100 - fee(bid) if bid else None
        imp_sig = inv_sigma(mid, S, m['K'], T, m['direction']) if mid else None
        dlt = delta_btc(S, m['K'], sig, T, m['direction'])
        rows.append(dict(m=m, S=S, T=T * 365, iv=sig, ename=ename, bid=bid, ask=ask, bsz=bsz, asz=asz,
                         mid=mid, model=model_p, e_buy=e_buy, e_sell=e_sell, imp=imp_sig, dlt=dlt))
        if csv_writer:
            csv_writer.writerow([now.strftime('%Y-%m-%d %H:%M:%S'), m['event'], m['market'], m['direction'],
                                 m['K'], f"{S:.2f}", f"{T*365:.2f}", f"{sig:.4f}", ename or '',
                                 bid, ask, bsz, asz, f"{model_p:.4f}",
                                 f"{e_buy:.2f}" if e_buy is not None else '',
                                 f"{e_sell:.2f}" if e_sell is not None else '',
                                 f"{imp_sig:.4f}" if imp_sig else '', f"{dlt*1000:.4f}"])

    # ---- display ----
    def act(r):
        liquid = (r['bid'] or 0) > 0 and (r['ask'] or 0) > 0 and (r['ask'] - r['bid']) <= 0.06
        buy_ok = liquid and r['asz'] >= 100 and (r['e_buy'] or -9) > 0.5
        sell_ok = liquid and r['bsz'] >= 100 and (r['e_sell'] or -9) > 0.5
        return buy_ok, sell_ok

    rv = {}
    for sym, cur in (('BTCUSDT', 'BTC'), ('ETHUSDT', 'ETH')):
        rv[cur] = realized_vol(sym)
    rvtxt = ' '.join(f"{c}已实现7d波动率={v*100:.1f}%" for c, v in rv.items() if v)
    print(f"\n===== {now.strftime('%H:%M:%S')}Z | BTC={prices.get('BTC','?')} ETH={prices.get('ETH','?')} "
          f"| bybit={t_bybit:.0f}ms pm={t_pm:.0f}ms | {len(rows)}桶 | {rvtxt} =====")
    print(f"{'市场':<20}{'方向':<5}{'行权价':>8}{'PM买':>7}{'PM卖':>7}{'量':>6}{'模型P':>7}{'买edge':>7}{'卖edge':>7}{'PM隐IV%':>9}{'D参考IV%':>9}{'对冲/1k':>9}  动作")
    rows.sort(key=lambda r: -max(abs(r['e_buy'] or 0), abs(r['e_sell'] or 0)))
    for r in rows[:22]:
        mk = f"{r['m']['currency']} {r['m']['exp'].strftime('%m-%d')}"
        buy_ok, sell_ok = act(r)
        star = ('做多' if buy_ok else '') + ('做空' if sell_ok else '')
        f1 = lambda v: f"{v:>7.2f}" if v is not None else f"{'n/a':>7}"
        imp = f"{r['imp']*100:>9.1f}" if r['imp'] else f"{'n/a':>9}"
        sz = min(r['bsz'], r['asz'])
        print(f"{mk:<20}{r['m']['direction']:<5}{r['m']['K']:>8}{r['bid'] or 0:>7.3f}{r['ask'] or 0:>7.3f}"
              f"{sz:>6.0f}{r['model']:>7.3f}{f1(r['e_buy'])}{f1(r['e_sell'])}"
              f"{imp}{r['iv']*100:>9.1f}{r['dlt']*1000:>9.3f}  {star}")
    n_buy = sum(1 for r in rows if act(r)[0]); n_sell = sum(1 for r in rows if act(r)[1])
    print(f"  [汇总] {len(rows)}桶 | 可行动(流动性+净edge>0.5c): 买{n_buy} 卖{n_sell} | CSV: {CSV_PATH}")
    log_event({"ts": now.strftime('%Y-%m-%dT%H:%M:%SZ'),
               "cycle_ms": round((time.time() - t0) * 1000),
               "bybit_ms": round(t_bybit), "pm_ms": round(t_pm),
               "buckets": len(rows), "actionable_buy": n_buy, "actionable_sell": n_sell})
    return rows

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--once', action='store_true')
    ap.add_argument('--interval', type=int, default=15)
    args = ap.parse_args()
    os.makedirs(LOG_DIR, exist_ok=True)
    need_hdr = not os.path.exists(CSV_PATH)
    fh = open(CSV_PATH, 'a', newline='', encoding='utf-8')
    w = csv.writer(fh)
    if need_hdr:
        w.writerow(['ts_utc', 'event', 'market', 'dir', 'strike', 'S', 'T_days', 'iv', 'deribit_exp',
                    'pm_bid', 'pm_ask', 'sz_bid', 'sz_ask', 'model_p', 'edge_buy_net_c', 'edge_sell_net_c',
                    'pm_impl_sigma', 'hedge_btc_per_1k'])
    if args.once:
        cycle(w)
    else:
        while True:
            try:
                cycle(w)
            except KeyboardInterrupt:
                break
            except Exception as ex:
                print("  [err]", ex)
            fh.flush()
            time.sleep(max(2, args.interval))
    fh.close()

if __name__ == '__main__':
    main()
