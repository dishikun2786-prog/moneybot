#!/usr/bin/env python3
"""扫描v2: 负风险多结果市场套利 + 广度错价扫描 + 薄市场陈价检测"""
import json, time, urllib.request

UA = {'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36'}

def get(url, data=None):
    if data is not None:
        req = urllib.request.Request(url, data=json.dumps(data).encode(), headers={**UA, 'Content-Type': 'application/json'})
    else:
        req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.loads(r.read().decode())

def feeper(rate, p): return rate * p * (1 - p)

def parse_books(books):
    bm = {}
    items = books.values() if isinstance(books, dict) else books
    for b in items:
        if not isinstance(b, dict): continue
        aid = b.get('asset_id') or b.get('assetId') or b.get('token_id')
        bids = sorted([(float(x['price']), float(x['size'])) for x in (b.get('bids') or [])], reverse=True)
        asks = sorted([(float(x['price']), float(x['size'])) for x in (b.get('asks') or [])])
        bm[aid] = (bids, asks)
    return bm

# 1. 拉取300个事件
allev = []
for off in (0, 100, 200):
    ev = get(f"https://gamma-api.polymarket.com/events?active=true&closed=false&limit=100&offset={off}&order=volume24hr&ascending=false")
    allev += ev
    time.sleep(0.3)
print(f"[1] 拉取事件: {len(allev)} 个")

# 2. 分类
neg_events = []
binaries = []   # (vol, event, market, [yes,no])
for e in allev:
    mk = e.get('markets') or []
    if e.get('negRisk') and len(mk) >= 3:
        toks = []
        for m in mk:
            try: t = json.loads(m.get('clobTokenIds') or '[]')
            except Exception: t = []
            if len(t) == 2: toks.append((m, t[0], t[1]))
        if len(toks) >= 3: neg_events.append((e, toks))
    else:
        for m in mk:
            try: t = json.loads(m.get('clobTokenIds') or '[]')
            except Exception: t = []
            if len(t) == 2:
                try: vol = float(m.get('volumeNum') or m.get('volume') or 0)
                except Exception: vol = 0
                binaries.append((vol, e, m, t))

# 优先高成交额市场, 最多500 token
binaries.sort(key=lambda x: -x[0])
ids = []
for vol, e, m, t in binaries:
    if len(ids) + 2 <= 700: ids += t
neg_ids = []
for e, toks in neg_events:
    for m, y, n in toks: neg_ids.append(y)
neg_ids = neg_ids[:400]

all_ids = list(dict.fromkeys(ids + neg_ids))
print(f"[2] 负风险事件: {len(neg_events)} 个 (token {len(neg_ids)}) | 二元市场样本: {len(ids)//2} 个")

bm = {}
for i in range(0, len(all_ids), 450):
    chunk = all_ids[i:i+450]
    books = get("https://clob.polymarket.com/books", [{"token_id": c} for c in chunk])
    bm.update(parse_books(books))
    time.sleep(0.2)
print(f"[3] 订单簿获取: {len(bm)} 个")

# 3. 负风险: ΣYES卖 < 1 ?  以及 ΣNO卖 < n-1 ?
print("\n===== 负风险多结果事件: ΣYES卖价 vs 1.00 =====")
nr = []
for e, toks in neg_events:
    s = 0.0; ok = True; sizes = []; n = 0
    for m, y, n_tok in toks:
        if y not in bm or not bm[y][1]: ok = False; break
        a = bm[y][1][0]; s += a[0]; sizes.append(a[1]); n += 1
    if ok and n >= 3:
        nr.append(dict(s=s, n=n, title=(e.get('title') or '')[:44], vol=float(e.get('volume24hr') or 0),
                       size=min(sizes) if sizes else 0))
nr.sort(key=lambda x: x['s'])
print(f"{'事件':<46}{'结果数':>5}{'ΣYES卖':>9}{'偏离1.0':>9}{'最小量':>7}{'24hVol':>10}")
for x in nr[:12]:
    print(f"{x['title']:<46}{x['n']:>5}{x['s']:>9.3f}{100*(1-x['s']):>8.2f}c{x['size']:>7.0f}{x['vol']:>10.0f}")
if nr:
    above = [x for x in nr if x['s'] < 1]
    print(f"=> 共{len(nr)}个负风险事件, 其中 ΣYES卖<1.00 (有套利) 的: {len(above)} 个")
    print(f"=> Σ范围为 [{min(x['s'] for x in nr):.3f}, {max(x['s'] for x in nr):.3f}]")

# 4. 广度二元扫描
print("\n===== 广度二元市场: sum_ask<1 (毛) =====")
pos = []
for vol, e, m, t in binaries:
    if t[0] not in bm or t[1] not in bm: continue
    ya = bm[t[0]][1][0] if bm[t[0]][1] else None
    na = bm[t[1]][1][0] if bm[t[1]][1] else None
    if not ya or not na: continue
    s = ya[0] + na[0]
    if s < 0.9999:
        pos.append(dict(edge=1-s, vol=vol, size=min(ya[1], na[1]), ya=ya[0], na=na[0],
                        title=(e.get('title') or '')[:38], mt=(m.get('groupItemTitle') or '')[:22]))
pos.sort(key=lambda x: -x['edge'])
print(f"扫描 {len(binaries)} 个二元市场 | sum_ask<1 的: {len(pos)} 个")
for x in pos[:12]:
    print(f"  edge={x['edge']*100:>5.2f}c | YES卖{x['ya']:.3f}+NO卖{x['na']:.3f} | 量{x['size']:>6.0f} | Vol=${x['vol']:>9,.0f} | {x['title']} {x['mt']}")

# 反向: bid和>1
neg2 = []
for vol, e, m, t in binaries:
    if t[0] not in bm or t[1] not in bm: continue
    yb = bm[t[0]][0][0] if bm[t[0]][0] else None
    nb = bm[t[1]][0][0] if bm[t[1]][0] else None
    if not yb or not nb: continue
    s = yb[0] + nb[0]
    if s > 1.0001:
        neg2.append(dict(edge=s-1, vol=vol, size=min(yb[1], nb[1]), title=(e.get('title') or '')[:38],
                         mt=(m.get('groupItemTitle') or '')[:22], yb=yb[0], nb=nb[0]))
neg2.sort(key=lambda x: -x['edge'])
print(f"\n反向 sum_bid>1 的: {len(neg2)} 个")
for x in neg2[:8]:
    print(f"  edge={x['edge']*100:>5.2f}c | YES买{x['yb']:.3f}+NO买{x['nb']:.3f} | 量{x['size']:>6.0f} | Vol=${x['vol']:>9,.0f} | {x['title']} {x['mt']}")

# 5. 按成交额分层的紧绷度: 错价与流动性的关系
import statistics
lo = [vol for vol, e, m, t in binaries if t[0] in bm and t[1] in bm]
print(f"\n[注] 样本含薄市场。统计: 二元市场 {len([1 for vol,e,m,t in binaries if t[0] in bm and t[1] in bm])} 个有双边报价")
