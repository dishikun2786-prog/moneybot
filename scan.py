#!/usr/bin/env python3
"""Polymarket 套利/做市机会扫描器 — Phase 0 侦察版
用法: python3 scan.py
只读, 无认证. 从低延迟服务器运行 (eu-west-1).
"""
import json, time, urllib.request

UA = {'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36'}

def get(url, data=None):
    t0 = time.time()
    if data is not None:
        req = urllib.request.Request(url, data=json.dumps(data).encode(),
                                     headers={**UA, 'Content-Type': 'application/json'})
    else:
        req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        out = json.loads(r.read().decode())
    return out, time.time() - t0

def feeper(rate, p):
    return rate * p * (1 - p)

FEE = {'crypto': 0.07, 'sports': 0.05, 'finance': 0.04, 'politics': 0.04,
       'economics': 0.05, 'culture': 0.05, 'weather': 0.05, 'mentions': 0.04,
       'tech': 0.04, 'geopolitics': 0.0}

def rate_of(e, m):
    c = str((m.get('category') or e.get('category') or '')).lower()
    for k, v in FEE.items():
        if k in c:
            return v
    return 0.05

def parse_books(books):
    bm = {}
    items = books.values() if isinstance(books, dict) else books
    for b in items:
        if not isinstance(b, dict):
            continue
        aid = b.get('asset_id') or b.get('assetId') or b.get('token_id')
        bids = sorted([(float(x['price']), float(x['size'])) for x in (b.get('bids') or [])], reverse=True)
        asks = sorted([(float(x['price']), float(x['size'])) for x in (b.get('asks') or [])])
        bm[aid] = (bids, asks)
    return bm

# ============ 1. 拉取热门事件 ============
ev, tg = get("https://gamma-api.polymarket.com/events?active=true&closed=false&limit=80&order=volume24hr&ascending=false")
print(f"[1] Gamma top80事件: latency={tg*1000:.0f}ms, 事件数={len(ev)}")

need = []      # 二元市场: (event, market, [yes_token,no_token], vol)
neg_events = []  # 负风险: (event, [(market, yes_token), ...])
for e in ev:
    mk = e.get('markets') or []
    if len(mk) >= 2 and e.get('negRisk'):
        toks = []
        for m in mk:
            try:
                t = json.loads(m.get('clobTokenIds') or '[]')
            except Exception:
                t = []
            if len(t) == 2:
                toks.append((m, t[0]))
        if len(toks) >= 3:
            neg_events.append((e, toks))
    elif len(mk) >= 1:
        for m in mk:
            try:
                t = json.loads(m.get('clobTokenIds') or '[]')
            except Exception:
                t = []
            if len(t) == 2:
                try:
                    vol = float(m.get('volumeNum') or m.get('volume') or 0)
                except Exception:
                    vol = 0
                need.append((e, m, t, vol))

ids = []
for e, m, t, vol in need:
    ids += t
for e, toks in neg_events[:40]:
    for m, tid in toks:
        ids.append(tid)
ids = list(dict.fromkeys(ids))[:400]
print(f"[2] 批量查询 {len(ids)} 个 token 订单簿 (CLOB /books)...")
books, tb = get("https://clob.polymarket.com/books", [{"token_id": i} for i in ids])
print(f"[3] /books latency={tb*1000:.0f}ms")
bm = parse_books(books)

# ============ 2. 二元市场: 完整组合套利 ============
print("\n===== A. 二元市场: 买入YES+NO (sum_ask<1 才有毛套利) =====")
rows = []
for e, m, t, vol in need:
    if t[0] not in bm or t[1] not in bm:
        continue
    ya = bm[t[0]][1][0] if bm[t[0]][1] else None
    na = bm[t[1]][1][0] if bm[t[1]][1] else None
    yb = bm[t[0]][0][0] if bm[t[0]][0] else None
    nb = bm[t[1]][0][0] if bm[t[1]][0] else None
    if not ya or not na:
        continue
    r = rate_of(e, m)
    edge = 1 - ya[0] - na[0]
    net = edge - feeper(r, ya[0]) - feeper(r, na[0])
    rows.append(dict(raw=edge, net=net, size=min(ya[1], na[1]), yask=ya[0], nask=na[0], r=r,
                     title=(e.get('title') or '')[:40],
                     mt=(m.get('groupItemTitle') or m.get('question') or '')[:30], vol=vol,
                     ybid=yb[0] if yb else None, nbid=nb[0] if nb else None,
                     tok=t, q=(e.get('title') or '') + ' | ' + (m.get('question') or '')))
rows.sort(key=lambda x: -x['net'])
pos = [x for x in rows if x['net'] > 0]
print(f"有效扫描 {len(rows)} 个二元市场 | 毛edge>0: {sum(1 for x in rows if x['raw'] > 0)} 个 | 扣费后净edge>0: {len(pos)} 个")
print(f"{'市场':<40}{'YES卖':>7}{'NO卖':>7}{'毛edge':>8}{'净edge':>8}{'可吃量':>7}{'24hVol':>10}")
for x in rows[:10]:
    print(f"{x['title']:<40}{x['yask']:>7.3f}{x['nask']:>7.3f}{x['raw']*100:>7.2f}c{x['net']*100:>7.2f}c{x['size']:>7.0f}{x['vol']:>10.0f}")

print("--- 反向: YES_bid+NO_bid>1 (卖组合, 需库存或铸币) ---")
mk2 = sorted([x for x in rows if x['ybid'] and x['nbid'] and (x['ybid']+x['nbid']-1) > 0],
             key=lambda x: -(x['ybid']+x['nbid']-1))
for x in mk2[:5]:
    print(f"{x['title']:<40} YESbid={x['ybid']:.2f}+NObid={x['nbid']:.2f} 溢价={(x['ybid']+x['nbid']-1)*100:.2f}c")

# ============ 3. 负风险多结果 ============
print("\n===== B. 负风险多结果事件: ΣYES卖价 vs 1.00 =====")
nrows = []
for e, toks in neg_events:
    s = 0.0; sf = 0.0; ok = True; n = 0; sizes = []
    for m, tid in toks:
        if tid not in bm or not bm[tid][1]:
            ok = False; break
        a = bm[tid][1][0]
        s += a[0]; sf += feeper(rate_of(e, m), a[0]); sizes.append(a[1]); n += 1
    if ok and n >= 3:
        nrows.append(dict(s=s, net=1-s-sf, n=n, title=(e.get('title') or '')[:46],
                          vol=float(e.get('volume24hr') or 0), size=min(sizes) if sizes else 0))
nrows.sort(key=lambda x: -(1-x['s']))
print(f"{'事件':<48}{'结果数':>5}{'ΣYES卖':>9}{'毛edge':>8}{'净edge':>9}{'最小量':>7}")
for x in nrows[:10]:
    print(f"{x['title']:<48}{x['n']:>5}{x['s']:>9.3f}{100*(1-x['s']):>7.2f}c{x['net']*100:>8.2f}c{x['size']:>7.0f}")

# ============ 4. 做市候选 ============
print("\n===== C. 做市候选: 价差×24h成交额 =====")
mm = []
for e, m, t, vol in need:
    if t[0] not in bm:
        continue
    bids, asks = bm[t[0]]
    if not bids or not asks:
        continue
    yb, ya = bids[0][0], asks[0][0]
    sp = ya - yb; mid = (ya + yb) / 2
    if mid < 0.03 or mid > 0.97:
        continue
    mm.append(dict(sp=sp, mid=mid, vol=vol, title=(e.get('title') or '')[:42],
                   m=(m.get('groupItemTitle') or '')[:24], sz=min(bids[0][1], asks[0][1])))
mm.sort(key=lambda x: -(x['sp'] * max(x['vol'], 1)))
for x in mm[:8]:
    print(f"{x['title']:<44}{x['m']:<24}mid={x['mid']:.3f} 价差={x['sp']*100:.1f}c Vol=${x['vol']:,.0f}")

# ============ 5. 热路径延迟测试 ============
print("\n===== D. 热路径延迟 (5次 GET /midpoint) =====")
lat = []
if need:
    tok = need[0][2][0]
    for _ in range(5):
        try:
            _, t1 = get(f"https://clob.polymarket.com/midpoint?token_id={tok}")
            lat.append(t1 * 1000)
        except Exception as ex:
            print('  err', ex)
    if lat:
        print(f"  min={min(lat):.0f}ms avg={sum(lat)/len(lat):.0f}ms max={max(lat):.0f}ms")

# ============ 6. 60秒持续性复查 ============
cand = []
for e, m, t, vol in need:
    if t[0] in bm and t[1] in bm and bm[t[0]][1] and bm[t[1]][1]:
        s = bm[t[0]][1][0][0] + bm[t[1]][1][0][0]
        if s < 0.999:
            cand.append(((e.get('title') or '')[:36], t))
print(f"\n===== E. 持续性: {len(cand)} 个候选对, 60秒后复查 =====")
time.sleep(60)
if cand:
    ids2 = list(dict.fromkeys([tid for _, t in cand[:30] for tid in t]))
    books2, _ = get("https://clob.polymarket.com/books", [{"token_id": i} for i in ids2])
    bm2 = parse_books(books2)
    tot = 0; alive = 0; detail = []
    for title, t in cand[:30]:
        if t[0] in bm2 and t[1] in bm2 and bm2[t[0]][1] and bm2[t[1]][1]:
            tot += 1
            s2 = bm2[t[0]][1][0][0] + bm2[t[1]][1][0][0]
            if s2 < 1:
                alive += 1
                detail.append((title, s2, 1 - s2))
    print(f"60秒后: {tot} 对中 {alive} 对仍满足 sum<1 ({100*alive/tot if tot else 0:.0f}% 存活)")
    for title, s2, e2 in sorted(detail, key=lambda z: -z[2])[:5]:
        print(f"  仍存活: {title:<38} sum={s2:.3f} edge={e2*100:.2f}c")
print("\n[完成] 扫描结束")
