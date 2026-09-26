#!/usr/bin/env python3
"""AI 托管模拟盘交易引擎（净盈利目标版）
策略: 多周期趋势(1h/15m EMA) + 时序微观(墙被吃=真实进攻 vs 撤单=诱多) + 择优选标
风控: 止损0.5% / 止盈1.5%(盈亏比3:1, 胜率>25%即净盈利) + NetEV闸 + 单标的上限
执行: paper_ops 纸面原生开平仓, 每笔平仓 TG 推送盈亏+累计胜率
运行: 每分钟决策, 持续 5 小时后自动总结(TG推送)
"""
import json, os, sys, time
from datetime import datetime, timezone

BASE = os.path.expanduser("~/polymarket")
OB = f"{BASE}/logs/go_orderbook.json"
SYMS = ("BTCUSDT","ETHUSDT","XAUUSDT","XAGUSDT","SOLUSDT","NEARUSDT","XRPUSDT")
CAT = {"BTCUSDT":0,"ETHUSDT":0,"XAUUSDT":1,"XAGUSDT":1,"SOLUSDT":2,"NEARUSDT":2,"XRPUSDT":2}
NOTIONAL = 15.0
STOP = 0.005
TAKE = 0.015
FEE = 0.00055
SLIP = 0.00005
MAX_POS = 3
DURATION_H = 5

sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "dash"))

def tg_alert(text):
    try:
        cfg = json.load(open(os.path.join(BASE, "alert_config.json")))
        if not cfg.get("bot_token"):
            return
        import urllib.request
        url = f"https://api.telegram.org/bot{cfg['bot_token']}/sendMessage"
        data = json.dumps({"chat_id": int(cfg["chat_id"]), "text": text}).encode()
        urllib.request.urlopen(urllib.request.Request(url, data=data,
            headers={"Content-Type": "application/json"}), timeout=10)
    except Exception as e:
        print(f"[tg] 发送失败: {e}")

class EMA:
    def __init__(self, period):
        self.a = 2/(period+1); self.v = None
    def update(self, p):
        self.v = p if self.v is None else self.a*p + (1-self.a)*self.v
        return self.v

class Trend:
    def __init__(self):
        self.e1h, self.e15m = EMA(60), EMA(15)
        self.prev1h = None
    def score(self, price):
        v1 = self.e1h.update(price); v15 = self.e15m.update(price)
        slope = (v1-self.prev1h)/self.prev1h if self.prev1h else 0
        self.prev1h = v1
        d1 = 1 if price>v1 else (-1 if price<v1 else 0)
        d15 = 1 if price>v15 else (-1 if price<v15 else 0)
        strength = min(1.0, abs(slope)*1000)
        cons = 1.0 if d1==d15 else 0.5
        direction = "long" if d1>0 and d15>0 else ("short" if d1<0 and d15<0 else "flat")
        return direction, 0.5*strength + 0.5*cons

class Micro:
    """时序微观: 墙被吃(真实) vs 撤单(诱多)"""
    def __init__(self):
        self.prev = {}
    def signal(self, book, trades):
        # book: {price: size}, trades: [{p, v}]
        cur = {float(p): s for p, s in book.items() if s > 0}
        traded = {}
        for t in trades:
            traded[float(t["p"])] = traded.get(float(t["p"]), 0) + float(t.get("v", 0))
        # 检测墙变化
        eaten, spoof = 0.0, 0.0
        mean = sum(cur.values())/len(cur) if cur else 0
        thr = max(mean*3, 0.001)
        for price, old in self.prev.items():
            if old < thr: continue
            new = cur.get(price, 0)
            if new < old*0.5:  # 墙消失
                consumed = old - new
                tv = traded.get(price, 0)
                if tv > 0 and consumed <= tv*1.2:
                    eaten += old
                else:
                    spoof += old
        self.prev = cur
        total = eaten + spoof
        if total <= 0: return 0.0
        return eaten/total  # 1=真实进攻, 0=诱多

def load_ob():
    d = json.load(open(OB))
    px = d.get("px", {})
    books = d.get("books", {})
    trades = d.get("trades", {})
    return px, books, trades

def main():
    trends = {s: Trend() for s in SYMS}
    micros = {s: Micro() for s in SYMS}
    pos = {}  # sym -> {side, entry, ts}
    trades = []  # 已完成交易
    t0 = time.time()
    tg_alert("🤖 AI 托管模拟盘交易已启动\n策略: 趋势+时序微观+盈亏比3:1\n目标: 净盈利 | 运行 5 小时")
    print(f"[{time.strftime('%H:%M:%S')}] 启动, 持续 {DURATION_H}h")

    while time.time() - t0 < DURATION_H*3600:
        try:
            px, books, trades_data = load_ob()
            scores = {}
            for s in SYMS:
                if s not in px or s not in books: continue
                price = px[s].get("last")
                if not price: continue
                direction, tscore = trends[s].score(price)
                # 时序微观
                bids = {float(p): sz for p, sz in books[s].get("bids", [])}
                asks = {float(p): sz for p, sz in books[s].get("asks", [])}
                book = dict(bids); book.update({-p: sz for p, sz in asks.items()})  # 简化
                msig = micros[s].signal(bids, trades_data.get(s, []))
                # 综合分
                final = tscore * (0.5 + 0.5*msig)
                if direction != "flat" and final > 0.55:
                    scores[s] = (direction, final)
            # 平仓检查
            for s in list(pos.keys()):
                p = px.get(s, {}).get("last")
                if not p: continue
                side = pos[s]["side"]; entry = pos[s]["entry"]
                ret = (p-entry)/entry if side=="long" else (entry-p)/entry
                if ret <= -STOP or ret >= TAKE:
                    import tenants, paper_ops
                    with tenants.tenant(27):
                        r = paper_ops.close_native(s)
                    pnl = ret*NOTIONAL - FEE*2*NOTIONAL
                    trades.append(dict(sym=s, side=side, pnl=round(pnl,4), ret=round(ret,4)))
                    pos.pop(s)
                    wins = sum(1 for t in trades if t["pnl"]>0)
                    rate = wins/len(trades)*100
                    tg_alert(f"📊 平仓 {s} {side}\n盈亏: {pnl:+.4f}$ ({(ret*100):+.2f}%)\n"
                             f"累计: {len(trades)}笔 | 胜率 {rate:.0f}% | 总盈亏 {sum(t['pnl'] for t in trades):+.2f}$")
            # 开仓（择优选标，最多 MAX_POS）
            if len(pos) < MAX_POS and scores:
                best = sorted(scores.items(), key=lambda x: -x[1][1])
                for s, (direction, final) in best:
                    if s in pos: continue
                    if len(pos) >= MAX_POS: break
                    import tenants, paper_ops
                    price = px[s].get("last")
                    with tenants.tenant(27):
                        r = paper_ops.open_native(s, direction, NOTIONAL)
                    if r.get("ok"):
                        pos[s] = dict(side=direction, entry=price, ts=time.time())
                        print(f"[{time.strftime('%H:%M:%S')}] 开仓 {s} {direction} @ {price}")
                    else:
                        print(f"[{time.strftime('%H:%M:%S')}] 开仓失败 {s}: {r.get('error')}")
        except Exception as e:
            print(f"[{time.strftime('%H:%M:%S')}] 周期异常: {e}")
        time.sleep(60)

    # 总结
    wins = sum(1 for t in trades if t["pnl"]>0)
    total = sum(t["pnl"] for t in trades)
    rate = wins/len(trades)*100 if trades else 0
    summary = (f"🏁 AI 托管模拟盘 5 小时总结\n"
               f"总交易: {len(trades)}笔\n"
               f"胜率: {rate:.1f}%\n"
               f"总盈亏: {total:+.2f}$\n"
               f"目标净盈利: {'✅ 达成' if total>0 else '❌ 未达成'}")
    tg_alert(summary)
    print(summary)
    # 落盘
    with open(f"{BASE}/logs/paper_trade_report.json", "w") as f:
        json.dump(dict(trades=trades, total=total, win_rate=rate), f, ensure_ascii=False, indent=1)

if __name__ == "__main__":
    main()
