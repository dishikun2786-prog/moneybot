#!/usr/bin/env python3
"""托管策略回测 v2: 接入盘口微观结构(imb/spread/depth) + funding 宏观 + NetEV 闸
重建 R_score = M_score(宏观) × m_score(微观)，验证微观结构是否改善信号质量。
数据: bybit_depth_1s(盘口) + price_1s(价格) + micro_1m(CVD) + carry_funding(费率)
"""
import json, time, sys
from datetime import datetime, timezone
import duckdb

BASE = "/home/ubuntu/polymarket"
PRICE = f"{BASE}/logs/price_1s.jsonl"
FEAT = "/tmp/book_feat.parquet"
FUND = f"{BASE}/data/carry_funding.parquet"
MICRO = f"{BASE}/logs/micro_1m.jsonl"
SYMS = ("BTCUSDT","ETHUSDT","XAUUSDT","XAGUSDT","SOLUSDT","NEARUSDT","XRPUSDT")

FEE = 0.00055; SLIP = 0.00005; NOTIONAL = 100.0
STOP = 0.003; TAKE = 0.006

def to_epoch(ts):
    try:
        dt = datetime.fromisoformat(str(ts))
        return int(dt.replace(tzinfo=timezone.utc).timestamp())
    except: return 0

def load_feats():
    """盘口特征 {sym: [(epoch, best_bid, best_ask, b5, a5)]}"""
    con=duckdb.connect()
    rows=con.execute(f"SELECT symbol, ts, best_bid, best_ask, b5, a5 FROM read_parquet('{FEAT}') ORDER BY symbol, ts").fetchall()
    con.close()
    data={s:[] for s in SYMS}
    for sym,ts,bb,ba,b5,a5 in rows:
        if sym in data and bb and ba:
            e=to_epoch(ts)
            if e: data[sym].append((e,float(bb),float(ba),float(b5 or 0),float(a5 or 0)))
    return data

def load_prices():
    """价格 {sym: [(epoch, last)]}"""
    data={s:[] for s in SYMS}
    with open(PRICE) as f:
        for line in f:
            try: d=json.loads(line)
            except: continue
            ts=d.get("ts"); prices=d.get("prices") or {}
            for s in SYMS:
                p=prices.get(s)
                if p and p.get("last"):
                    e=to_epoch(ts)
                    if e: data[s].append((e,float(p["last"])))
    return data

def load_funding():
    """funding {sym: [(epoch, funding_rate)]} 8h 稀疏"""
    con=duckdb.connect()
    rows=con.execute(f"SELECT symbol, ts, funding_rate FROM read_parquet('{FUND}') ORDER BY symbol, ts").fetchall()
    con.close()
    data={s:[] for s in SYMS}
    for sym,ts,fr in rows:
        if sym in data:
            e=to_epoch(ts)
            if e: data[sym].append((e,float(fr)))
    return data

def load_cvd():
    """micro_1m CVD {sym: [(epoch, cvd_1m)]}"""
    data={s:[] for s in SYMS}
    with open(MICRO) as f:
        for line in f:
            try: d=json.loads(line)
            except: continue
            s=d.get("sym"); ts=d.get("ts")
            if s in data and ts:
                e=to_epoch(ts)
                if e: data[s].append((e,float(d.get("cvd_1m") or 0)))
    return data

def funding_at(fund, e):
    """8h 稀疏 funding 前值插值"""
    last=None
    for fe,fr in fund:
        if fe<=e: last=fr
        else: break
    return last

def run(sym, feats, prices, fund, cvd, use_micro, use_netev):
    """回测单标的。use_micro=是否用盘口微观; use_netev=是否加 NetEV 闸"""
    # 按 epoch 对齐价格和盘口（盘口稀疏，用最近盘口）
    trades=[]; pos=None
    # 价格序列主循环
    n=len(prices)
    if n<120: return trades
    fi=0  # feats 指针
    for i in range(120, n):
        e,last=prices[i]
        mom=(last-prices[i-60][1])/prices[i-60][1]
        # 推进盘口指针到当前 e
        while fi<len(feats) and feats[fi][0] <= e: fi+=1
        imb=0.0; spread_bp=0.0
        if fi>0:
            _,bb,ba,b5,a5=feats[fi-1]
            mid=(bb+ba)/2
            spread_bp=(ba-bb)/mid*10000 if mid else 0
            tot=b5+a5
            imb=(b5-a5)/tot if tot>0 else 0
        # 宏观 M_score: funding 偏离(8h 插值)
        fr=funding_at(fund,e)
        m_score=0.0
        if fr is not None:
            # funding 偏离: 高于 0.0001(年化~11%) 视为杠杆拥挤
            m_score=min(1.0, abs(fr)/0.0003)
        # 微观 m_score
        micro=0.0
        if use_micro:
            micro=abs(imb)
        r_score=m_score*micro if use_micro else m_score*abs(mom)*200
        # 平仓
        if pos:
            ret=(last-pos["entry"])/pos["entry"]
            if pos["side"]=="short": ret=-ret
            if ret<=-STOP or ret>=TAKE:
                exit_px=last*(1-SLIP if pos["side"]=="long" else 1+SLIP)
                pnl=ret*NOTIONAL-FEE*2*NOTIONAL
                trades.append(dict(pnl=round(pnl,4), ret=round(ret,5), hold_s=e-pos["ts"], r=pos["r"]))
                pos=None
        # 开仓
        if not pos and r_score>0.25:
            side="long" if (mom>0 if not use_micro else imb>0) else "short"
            # NetEV 闸: P(r_score) × 0.6% - (1-P) × 0.3% - 11bp - 0.5bp
            if use_netev:
                p=min(0.9, 0.4+0.5*r_score)
                netev=p*TAKE-(1-p)*STOP-FEE*2-SLIP
                if netev<=0: continue
            entry=last*(1+SLIP if side=="long" else 1-SLIP)
            pos=dict(side=side,entry=entry,ts=e,r=r_score)
    return trades

def summarize(name, trades):
    if not trades:
        return f"{name}: 无交易"
    n=len(trades); wins=sum(1 for t in trades if t['pnl']>0)
    total=sum(t['pnl'] for t in trades)
    avg=sum(t['pnl'] for t in trades)/n
    return f"{name}: 交易{n}笔 胜率{wins/n*100:.1f}% 盈亏{total:+.2f}$ 均{avg:+.4f}$"

def main():
    t0=time.time()
    print("加载盘口特征...")
    feats=load_feats()
    print("加载价格...")
    prices=load_prices()
    print("加载 funding + cvd...")
    fund=load_funding(); cvd=load_cvd()
    print(f"数据就绪 {time.time()-t0:.0f}s\n")
    print("=== 回测对比(重叠区间 9/25 16:35~19:04, ~2.5h) ===")
    for label, use_micro, use_netev in [
        ("纯价格动量", False, False),
        ("微观共振(盘口imb)", True, False),
        ("微观共振+NetEV闸", True, True),
    ]:
        allt=[]
        for s in SYMS:
            if s not in feats or s not in prices: continue
            allt+=run(s, feats.get(s,[]), prices.get(s,[]), fund.get(s,[]), cvd.get(s,[]), use_micro, use_netev)
        print(" ", summarize(label, allt))

if __name__=="__main__":
    main()
