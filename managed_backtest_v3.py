#!/usr/bin/env python3
"""托管策略回测 v3: 完整 R_score 共振 + 历史标定 NetEV 闸
数据要求(1-2周积累后重跑):
  - funding: carry_funding.parquet (7标的, 已回填+持续落盘)
  - OI: micro_1m.jsonl (已完整)
  - 盘口微观: bybit_depth_1s (积累中)
  - 价格: price_1s.jsonl
策略:
  M_score = funding偏离 × OI分位 (宏观杠杆失衡)
  m_score = 盘口imb × CVD流不平衡 × (1 - spread风险) (微观共振)
  R_score = M_score × m_score
  NetEV 闸: 滚动窗口历史胜率标定 P(success), 期望>0 才开仓
"""
import json, time
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
    con=duckdb.connect()
    rows=con.execute(f"SELECT symbol, ts, funding_rate FROM read_parquet('{FUND}') ORDER BY symbol, ts").fetchall()
    con.close()
    data={s:[] for s in SYMS}
    for sym,ts,fr in rows:
        if sym in data:
            e=to_epoch(ts)
            if e: data[sym].append((e,float(fr)))
    return data

def load_oi():
    """micro_1m OI {sym: [(epoch, oi)]}"""
    data={s:[] for s in SYMS}
    with open(MICRO) as f:
        for line in f:
            try: d=json.loads(line)
            except: continue
            s=d.get("sym"); ts=d.get("ts"); oi=d.get("oi")
            if s in data and ts and oi and oi>0:
                e=to_epoch(ts)
                if e: data[s].append((e,float(oi)))
    return data

def load_cvd():
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

def nearest(series, e):
    """最近 <= e 的值"""
    last=None
    for se,v in series:
        if se<=e: last=v
        else: break
    return last

class RollingWin:
    """滚动窗口胜率(历史标定 NetEV 的 P)"""
    def __init__(self, n=50):
        self.n=n; self.q=[]
    def add(self, win):
        self.q.append(1 if win else 0)
        if len(self.q)>self.n: self.q.pop(0)
    def rate(self):
        return sum(self.q)/len(self.q) if self.q else 0.5

def run(sym, feats, prices, fund, oi, cvd, use_full, use_netev):
    """完整 R_score 回测"""
    trades=[]; pos=None; rw=RollingWin()
    n=len(prices)
    if n<120: return trades
    fi=0
    for i in range(120, n):
        e,last=prices[i]
        mom=(last-prices[i-60][1])/prices[i-60][1]
        while fi<len(feats) and feats[fi][0]<=e: fi+=1
        imb=0.0; spread_bp=0.0
        if fi>0:
            _,bb,ba,b5,a5=feats[fi-1]
            mid=(bb+ba)/2
            spread_bp=(ba-bb)/mid*10000 if mid else 0
            tot=b5+a5
            imb=(b5-a5)/tot if tot>0 else 0
        # M_score: funding偏离 × OI分位
        fr=nearest(fund,e); oi_v=nearest(oi,e)
        m_score=0.5
        if fr is not None:
            # funding 偏离(8h) 归一化: 年化 > 11% 视为满拥挤
            m_score=min(1.0, abs(fr)*3*365/0.11)
        oi_score=0.5
        if oi_v is not None:
            # OI 分位(用全部历史近似: 当前 vs 首尾)
            oi_hist=[v for _,v in oi if v>0]
            if len(oi_hist)>10:
                lo,hi=min(oi_hist),max(oi_hist)
                if hi>lo: oi_score=(oi_v-lo)/(hi-lo)
        M=m_score*oi_score
        # m_score: imb × cvd × (1-spread)
        cvd_v=nearest(cvd,e) or 0
        cvd_score=min(1.0, abs(cvd_v)/100) if cvd_v else 0.3
        spread_score=max(0, 1-spread_bp/10)
        micro=abs(imb)*cvd_score*spread_score
        if not use_full:
            micro=abs(mom)*200  # 基线: 纯动量
        R=M*micro
        # 平仓
        if pos:
            ret=(last-pos["entry"])/pos["entry"]
            if pos["side"]=="short": ret=-ret
            if ret<=-STOP or ret>=TAKE:
                exit_px=last*(1-SLIP if pos["side"]=="long" else 1+SLIP)
                pnl=ret*NOTIONAL-FEE*2*NOTIONAL
                win=pnl>0
                trades.append(dict(pnl=round(pnl,4), ret=round(ret,5), hold_s=e-pos["ts"], r=pos["r"]))
                rw.add(win)
                pos=None
        # 开仓
        if not pos and R>0.2 and (not use_full or abs(imb)>0.3):
            side="long" if (imb>0 if use_full else mom>0) else "short"
            if use_netev:
                p=rw.rate()  # 历史滚动胜率标定 P
                netev=p*TAKE-(1-p)*STOP-FEE*2-SLIP
                if netev<=0: continue
            entry=last*(1+SLIP if side=="long" else 1-SLIP)
            pos=dict(side=side,entry=entry,ts=e,r=R)
    return trades

def summarize(name, trades):
    if not trades:
        return f"{name}: 无交易"
    n=len(trades); wins=sum(1 for t in trades if t['pnl']>0)
    total=sum(t['pnl'] for t in trades)
    return f"{name}: 交易{n}笔 胜率{wins/n*100:.1f}% 盈亏{total:+.2f}$ 均{total/n:+.4f}$"

def main():
    t0=time.time()
    print("加载数据...")
    feats=load_feats(); prices=load_prices(); fund=load_funding(); oi=load_oi(); cvd=load_cvd()
    print(f"数据就绪 {time.time()-t0:.0f}s | 盘口重叠区间约 2.5h")
    print("\n=== 完整 R_score 回测对比 ===")
    for label, uf, un in [
        ("基线(纯动量)", False, False),
        ("完整R_score(宏观×微观)", True, False),
        ("完整R_score+历史标定NetEV", True, True),
    ]:
        allt=[]
        for s in SYMS:
            if s not in feats or s not in prices: continue
            allt+=run(s, feats.get(s,[]), prices.get(s,[]), fund.get(s,[]), oi.get(s,[]), cvd.get(s,[]), uf, un)
        print(" ", summarize(label, allt))
    print("\n⚠️ 当前盘口数据仅 2.5h 重叠, 样本不足; 需 1-2 周 depth 积累后重跑才有统计意义")

if __name__=="__main__":
    main()
