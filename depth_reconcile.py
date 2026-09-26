#!/usr/bin/env python3
"""Phase C/D 影子对账: Go depth gateway vs Python bridge 数据一致性 + 新鲜度
校验 go_orderbook.json 与 orderbook.json 的 books/px/basis/kline 关键字段偏差。
异常 → TG 告警。零外部依赖。
"""
import json, os, time, urllib.request

BASE = os.path.expanduser("~/polymarket")
GO = os.path.join(BASE, "logs", "go_orderbook.json")
PY = os.path.join(BASE, "logs", "orderbook.json")

def _tg(text):
    try:
        cfg = json.load(open(os.path.join(BASE, "alert_config.json"), encoding="utf-8"))
        if not cfg.get("bot_token"):
            return
        url = f"https://api.telegram.org/bot{cfg['bot_token']}/sendMessage"
        data = json.dumps({"chat_id": int(cfg["chat_id"]), "text": text}).encode()
        urllib.request.urlopen(urllib.request.Request(url, data=data,
            headers={"Content-Type": "application/json"}), timeout=10)
    except Exception:
        pass

def main():
    problems = []
    try:
        g = json.load(open(GO, encoding="utf-8"))
    except Exception as e:
        problems.append(f"go_orderbook.json 读取失败 {e}")
        g = None
    try:
        p = json.load(open(PY, encoding="utf-8"))
    except Exception as e:
        problems.append(f"orderbook.json 读取失败 {e}")
        p = None

    if g and p:
        # 新鲜度
        gage = (time.time()*1000 - (g.get("ts") or 0)) / 1000
        if gage > 10:
            problems.append(f"Go 帧陈旧 {gage:.0f}s")
        # books 标的一致
        if set(g.get("books",{})) != set(p.get("books",{})):
            problems.append("books 标的集合不一致")
        # BTC 盘口 bid0 价格偏差
        gb = g.get("books",{}).get("BTCUSDT",{}).get("bids",[[]])[0]
        pb = p.get("books",{}).get("BTCUSDT",{}).get("bids",[[]])[0]
        if gb and pb and abs(float(gb[0])-float(pb[0]))/float(pb[0]) > 0.002:
            problems.append(f"BTC bid0 偏差 {abs(float(gb[0])-float(pb[0]))/float(pb[0])*10000:.1f}bp")
        # px BTC last 偏差
        gl = g.get("px",{}).get("BTCUSDT",{}).get("last")
        pl = p.get("px",{}).get("BTCUSDT",{}).get("last")
        if gl and pl and abs(gl-pl)/pl > 0.002:
            problems.append(f"BTC last 偏差 {abs(gl-pl)/pl*10000:.1f}bp")
        # basis 偏差
        gbp = g.get("basis",{}).get("BTCUSDT",{}).get("basis_pct")
        pbp = p.get("basis",{}).get("BTCUSDT",{}).get("basis_pct")
        if gbp is not None and pbp is not None and abs(gbp-pbp) > 0.05:
            problems.append(f"BTC basis 偏差 {abs(gbp-pbp):.4f}")

    status = "OK" if not problems else " | ".join(problems)
    print(f"[depth_reconcile] {time.strftime('%H:%M:%S')} {status}")
    if problems:
        _tg(f"⚠️ depth 对账异常: {status}")
    return 0 if not problems else 1

if __name__ == "__main__":
    raise SystemExit(main())
