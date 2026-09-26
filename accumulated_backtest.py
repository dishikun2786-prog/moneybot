#!/usr/bin/env python3
"""积累数据综合回测: 完整 R_score(宏观funding/OI × 微观盘口/CVD) + NetEV + 择优选标
数据: bybit_depth_1s(盘口11h) + price_1s(3.5天) + micro_1m(OI/CVD) + carry_funding
策略对比:
  基线(纯动量) vs 完整共振(宏观×微观) vs 共振+NetEV闸 vs 共振+择优选标
"""
import json, time, bisect
from datetime import datetime, timezone
import duckdb

BASE = "/home/ubuntu/polymarket"
PRICE = f"{BASE}/logs/price_1s.jsonl"
FEAT = "/tmp/book_feat.parquet"
FUND = f"{BASE}/data/carry_funding.parquet"
MICRO = f"{BASE}/logs/micro_1m.jsonl"
SYMS = ("BTCUSDT","ETHUSDT","XAUUSDT","XAGUSDT","SOLUSDT","NEARUSDT","XRPUSDT")
CAT = {"BTCUSDT":0,"ETHUSDT":0,"XAUUSDT":1,"XAGUSDT":1,"SOLUSDT":2,"NEARUSDT":2,"XRPUSDT":2}
FEE=0.00055; SLIP=0.00005; NOTIONAL=100.0; STOP=0.004; TAKE=0.008

def to_epoch(ts):
    try: return int(datetime.fromisoformat(str(ts)).replace(tzinfo=timezone.utc).timestamp())
    except: return 0

def agg_feats():
    con=duckdb.connect()
    con.execute("""
    COPY (SELECT symbol, ts,
        MAX(CASE WHEN side='bid' THEN CAST(price AS DOUBLE) END) AS best_bid,
        MIN(CASE WHEN side='ask' THEN CAST(price AS DOUBLE) END) AS best_ask,
        SUM(CASE WHEN side='bid' AND level<5 THEN size END) AS b5,
        SUM(CASE WHEN side='ask' AND level<5 THEN size END) AS a5
      FROM read_ndjson('logs/bybit_depth_1s_*.jsonl', auto_detect=true)
      GROUP BY symbol, ts) TO '/tmp/book_feat.parquet' (FORMAT PARQUET, OVERWRITE_OR_IGNORE)
    """)
    rows=con.execute("SELECT symbol,ts,best_bid,best_ask,b5,a5 FROM read_parquet('/tmp/book_feat.parquet') ORDER BY symbol,ts").fetchall()
    con.close()
    d={s:[] for s in SYMS}
    for sym,ts,bb,ba,b5,a5 in rows:
        if sym in d and bb and ba:
            e=to_epoch(ts)
            if e: d[sym].append((e,float(bb),float(ba),float(b5 or 0),float(a5 or 0)))
    return d

def load_prices():
    d={s:[] for s in SYMS}
    with open(PRICE) as f:
        for line in f:
            try: x=json.loads(line)
            except: continue
            ts=x.get("ts"); p=x.get("prices") or {}
            for s in SYMS:
                v=p.get(s)
                if v and v.get("last"):
                    e=to_epoch(ts)
                    if e: d[s].append((e,float(v["last"])))
    return d

def load_funding():
    con=duckdb.connect()
    rows=con.execute(f"SELECT symbol,ts,funding_rate FROM read_parquet('{FUND}') ORDER BY symbol,ts").fetchall()
    con.close()
    d={s:[] for s in SYMS}
    for sym,ts,fr in rows:
        if sym in d:
            e=to_epoch(ts)
            if e: d[sym].append((e,float(fr)))
    return d

def load_micro():
    """micro_1m: {sym: [(epoch, oi, cvd)]}"""
    d={s:[] for s in SYMS}
    with open(MICRO) as f:
        for line in f:
            try: x=json.loads(line)
            except: continue
            s=x.get("sym"); ts=x.get("ts")
            if s in d and ts:
                e=to_epoch(ts)
                if e: d[s].append((e,float(x.get("oi") or 0),float(x.get("cvd_1m") or 0)))
    return d

def nearest(series, e, col=1):
    if not series: return None
    keys=[x[0] for x in series]
    i=bisect.bisect_right(keys,e)
    return series[i-1][col] if i>0 else None

def run(sym, prices, feats, fund, micro, mode):
    """mode: baseline(纯动量) / full(完整R_score) / netev / select"""
    trades=[]; pos=None
    n=len(prices); step=60
    if n<step*2: return trades
    fi=0
    # 历史滚动胜率(NetEV 标定)
    win_hist=[]; p_succ=0.5
    for i in range(step, n, step):
        e,last=prices[i]
        mom=(last-prices[i-step][1])/prices[i-step][1]
        while fi<len(feats) and feats[fi][0]<=e: fi+=1
        imb=0.0; spread=0.0
        if fi>0:
            _,bb,ba,b5,a5=feats[fi-1]
            mid=(bb+ba)/2
            spread=(ba-bb)/mid*10000 if mid else 0
            tot=b5+a5
            imb=(b5-a5)/tot if tot>0 else 0
        fr=nearest(fund,e)
        oi=nearest(micro,e,1); cvd=nearest(micro,e,2) or 0
        # 宏观 M_score
        M=0.5
        if fr is not None: M=max(0.2,min(1.0,abs(fr)*3*365/0.11))
        # 微观 m_score
        m=abs(imb)*min(1.0,abs(cvd)/100)*max(0,1-spread/10)
        R=M*m
        # 平仓
        if pos:
            ret=(last-pos["entry"])/pos["entry"]
            if pos["side"]=="short": ret=-ret
            if ret<=-STOP or ret>=TAKE:
                exit_px=last*(1-SLIP if pos["side"]=="long" else 1+SLIP)
                pnl=ret*NOTIONAL-FEE*2*NOTIONAL
                trades.append(dict(pnl=round(pnl,4), hold=e-pos["ts"], r=pos["r"]))
                win_hist.append(1 if pnl>0 else 0)
                p_succ=sum(win_hist[-30:])/max(1,len(win_hist[-30:]))
                pos=None
        # 开仓
        if not pos:
            if mode=="baseline":
                go=abs(mom)>0.0005; r=abs(mom)*200; side="long" if mom>0 else "short"
            else:
                go=R>0.15 and abs(imb)>0.3; r=R; side="long" if imb>0 else "short"
            if mode=="select":
                go=go and R>0.2  # 更严
            if go:
                if mode=="netev" or mode=="select":
                    netev=p_succ*TAKE-(1-p_succ)*STOP-FEE*2-SLIP
                    if netev<=0: continue
                entry=last*(1+SLIP if side=="long" else 1-SLIP)
                pos=dict(side=side,entry=entry,ts=e,r=r)
    return trades

def summarize(name, trades):
    if not trades: return f"{name}: 无交易"
    n=len(trades); wins=sum(1 for t in trades if t['pnl']>0)
    total=sum(t['pnl'] for t in trades)
    dd=min([t['pnl'] for t in trades])
    avg_hold=sum(t['hold'] for t in trades)/n
    return f"{name}: 交易{n}笔 胜率{wins/n*100:.1f}% 盈亏{total:+.2f}$ 均{total/n:+.4f}$ 最大单亏{dd:.2f}$ 均持仓{avg_hold:.0f}s"

def main():
    t0=time.time()
    print("聚合盘口特征(11h)...")
    feats=agg_feats()
    print("加载价格/funding/micro...")
    prices=load_prices(); fund=load_funding(); micro=load_micro()
    print(f"数据就绪 {time.time()-t0:.0f}s\n")
    # 重叠区间
    fmin=max(min(x[0] for x in v) for v in feats.values() if v)
    pmax=max(x[0] for v in prices.values() for x in v)
    print(f"重叠区间: {datetime.fromtimestamp(fmin, tz=timezone.utc).strftime('%m-%d %H:%M')} ~ {datetime.fromtimestamp(pmax, tz=timezone.utc).strftime('%m-%d %H:%M')} (约{(pmax-fmin)/3600:.1f}h)\n")
    print("=== 积累数据综合回测 ===")
    for mode, label in [("baseline","基线(纯动量)"),("full","完整R_score(宏观×微观)"),("netev","共振+NetEV闸"),("select","共振+择优选标")]:
        allt=[]
        for s in SYMS:
            allt+=run(s,prices.get(s,[]),feats.get(s,[]),fund.get(s,[]),micro.get(s,[]),mode)
        print(" ", summarize(label, allt))

if __name__=="__main__":
    main()
