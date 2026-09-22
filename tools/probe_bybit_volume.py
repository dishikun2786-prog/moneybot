#!/usr/bin/env python3
"""Bybit v5 交易量数据通道探测 (临时): orderbook.200 + publicTrade
测量: 推送速率/时延/字段结构 → 支撑实时同步展示的可行性证据"""
import json
import time

import websocket

WS = "wss://stream.bybit.com/v5/public/linear"
SUB = ["orderbook.200.BTCUSDT", "orderbook.200.ETHUSDT", "publicTrade.BTCUSDT", "publicTrade.ETHUSDT"]

stats = {"book": 0, "trade": 0, "snap": 0, "delta": 0, "trade_buy": 0, "trade_sell": 0,
         "min_lag": 1e9, "max_lag": 0, "lag_n": 0, "book_sizes": [], "trade_sizes": []}
t0 = time.time()
done = {"v": False}


def on_msg(ws, m):
    d = json.loads(m)
    if not d.get("topic"):
        return
    now = time.time() * 1000
    t = d.get("topic", "")
    if t.startswith("orderbook."):
        stats["book"] += 1
        ty = d.get("type")
        if ty == "snapshot":
            stats["snap"] += 1
        elif ty == "delta":
            stats["delta"] += 1
        b, a = d.get("data", {}).get("b", []), d.get("data", {}).get("a", [])
        if b:
            stats["book_sizes"].append(len(b) + len(a))
        ts = int(d.get("ts", 0))
        stats["min_lag"] = min(stats["min_lag"], now - ts)
        stats["max_lag"] = max(stats["max_lag"], now - ts)
        stats["lag_n"] += 1
    elif t.startswith("publicTrade."):
        stats["trade"] += 1
        for tr in d.get("data", []):
            if tr.get("S") == "Buy":
                stats["trade_buy"] += 1
            else:
                stats["trade_sell"] += 1
            stats["trade_sizes"].append(float(tr.get("v", 0)))
            ts = int(tr.get("T", 0))
            stats["min_lag"] = min(stats["min_lag"], now - ts)
            stats["max_lag"] = max(stats["max_lag"], now - ts)
            stats["lag_n"] += 1
    if time.time() - t0 >= 15:
        done["v"] = True
        ws.close()


def on_open(ws):
    ws.send(json.dumps({"op": "subscribe", "args": SUB}))


ws = websocket.WebSocketApp(WS, on_open=on_open, on_message=on_msg)
ws.run_forever(ping_interval=20, ping_timeout=10)
el = time.time() - t0
print(f"探测时长: {el:.1f}s")
print(f"orderbook: {stats['book']}条 ({stats['book']/el:.1f}/s) | 快照{stats['snap']} 增量{stats['delta']}"
      f" | 档位均值{sum(stats['book_sizes'])/max(len(stats['book_sizes']),1):.0f}")
print(f"publicTrade: {stats['trade']}条消息 ({stats['trade']/el:.1f}/s)"
      f" | 主动买{stats['trade_buy']} 主动卖{stats['trade_sell']}"
      f" | 单笔均值{sum(stats['trade_sizes'])/max(len(stats['trade_sizes']),1):.4f}")
print(f"时延: min={stats['min_lag']:.0f}ms avg≈{(stats['min_lag']+stats['max_lag'])/2:.0f}ms max={stats['max_lag']:.0f}ms (n={stats['lag_n']})")
print(f"买卖比(主动量): {stats['trade_buy']}/{stats['trade_sell']}")
