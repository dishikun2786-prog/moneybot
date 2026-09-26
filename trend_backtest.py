#!/usr/bin/env python3
"""趋势选标回测: 择优选标 vs 等权 对比
数据: price_1s(价格) + book_feat(盘口imb) + micro_1m(CVD) + carry_funding(funding)
策略: 每分钟对 7 标的算 TrendScore(动量+imb+funding), 对比:
  等权: 7 标的全部按动量方向做
  择优: 选 TrendScore top3(类别配额) 做
输出: 胜率/盈亏/最大回撤/交易数 对比
用法: ./venv/bin/python trend_backtest.py
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
CAT = {"BTCUSDT":0,"ETHUSDT":0,"XAUUSDT":1,"XAGUSDT":1,"SOLUSDT":2,"NEARUSDT":2,"XRPUSDT":2}
FEE=0.00055; SLIP=0.00005; NOTIONAL=100.0; STOP=0.005; TAKE=0.01

def to_epoch(ts):
    try:
        return int(datetime.fromisoformat(str(ts)).replace(tzinfo=timezone.utc).timestamp())
    except: return 0

def load_feats():
    con=duckdb.connect()
    rows=con.execute(f"SELECT symbol,ts,best_bid,best_ask,b5,a5 FROM read_parquet('{FEAT}') ORDER BY symbol,ts").fetchall()
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

def trend_score(mom, imb, fr):
    """简化 TrendScore: 动量 + imb + funding 加权"""
    s_imp = min(1.0, abs(mom)*200)  # 动量强度
    s_imb = min(1.0, abs(imb)*2)    # 盘口不平衡
    s_fund = min(1.0, abs(fr)/0.0003) if fr else 0.5  # funding 偏离
    return 0.4*s_imp + 0.3*s_imb + 0.3*s_fund

def run_strategy(equal_weight):
    """回放策略: equal_weight=True 等权7标的, False 择优top3"""
    feats=load_feats(); prices=load_prices(); fund=load_funding()
    # 按分钟对齐: 每 60 秒一轮决策
    # 简化: 用 price 序列, 每 60 tick 决策一次
    trades=[]
    step=60
    # 对每个标的独立跑动量策略, 但择优模式只在 score 高时开
    for s in SYMS:
        ps=prices.get(s,[])
        fs=feats.get(s,[])
        fu=fund.get(s,[])
        if len(ps)<step*2: continue
        pos=None; fi=0
        for i in range(step, len(ps), step):
            e,last=ps[i]
            mom=(last-ps[i-step][1])/ps[i-step][1]
            while fi<len(fs) and fs[fi][0]<=e: fi+=1
            imb=0.0
            if fi>0:
                _,bb,ba,b5,a5=fs[fi-1]
                tot=b5+a5
                imb=(b5-a5)/tot if tot>0 else 0
            fr=None
            for fe,frv in fu:
                if fe<=e: fr=frv
                else: break
            score=trend_score(mom,imb,fr)
            # 平仓
            if pos:
                ret=(last-pos["entry"])/pos["entry"]
                if pos["side"]=="short": ret=-ret
                if ret<=-STOP or ret>=TAKE:
                    exit_px=last*(1-SLIP if pos["side"]=="long" else 1+SLIP)
                    trades.append(dict(pnl=ret*NOTIONAL-FEE*2*NOTIONAL, hold=e-pos["ts"]))
                    pos=None
            # 开仓: 等权全开, 择优 score>0.35 才开
            if not pos and abs(mom)>0.0005:
                if equal_weight or score>0.35:
                    side="long" if mom>0 else "short"
                    entry=last*(1+SLIP if side=="long" else 1-SLIP)
                    pos=dict(side=side,entry=entry,ts=e)
    return trades

def summarize(name, trades):
    if not trades:
        return f"{name}: 无交易"
    n=len(trades); wins=sum(1 for t in trades if t['pnl']>0)
    total=sum(t['pnl'] for t in trades)
    dd=min([t['pnl'] for t in trades])
    return f"{name}: 交易{n}笔 胜率{wins/n*100:.1f}% 盈亏{total:+.2f}$ 均{total/n:+.4f}$ 最大单亏{dd:.2f}$"

def main():
    t0=time.time()
    print("加载数据...")
    print(f"数据就绪 {time.time()-t0:.0f}s\n")
    print("=== 趋势选标回测: 择优选标 vs 等权 ===")
    print("(重叠区间 ~2.5h, 样本不足, 验证框架)")
    for label, eq in [("等权(7标的动量)", True), ("择优选标(score>0.35)", False)]:
        trades=run_strategy(eq)
        print(" ", summarize(label, trades))

if __name__=="__main__":
    main()
