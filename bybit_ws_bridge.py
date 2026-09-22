#!/usr/bin/env python3
"""Bybit 行情桥 v1: WSS → 内存缓存 → 原子快照文件 → SSE 数据源
- 单连接多订阅: tickers.BTCUSDT/ETHUSDT (~100ms) + kline.1 (实时末根)
- 每次 ticker 消息原子写 logs/bybit_prices.json (dash SSE 读取)
- 每秒快照持久化 logs/price_1s.jsonl (秒级价格数据集)
- 断线: run_forever 外层指数退避重连 (1s→30s)
用法: python bybit_ws_bridge.py [--test]   (--test 收到8条推送后退出)"""
import argparse
import json
import os
import threading
import time
from collections import deque

import websocket

BASE = os.path.expanduser("~/polymarket")
SYMBOLS = ["BTCUSDT", "ETHUSDT"]
TICKER_TOPICS = [f"tickers.{s}" for s in SYMBOLS]
KLINE_TOPICS = [f"kline.1.{s}" for s in SYMBOLS]
BOOK_TOPICS = [f"orderbook.200.{s}" for s in SYMBOLS]
TRADE_TOPICS = [f"publicTrade.{s}" for s in SYMBOLS]
ALL_TOPICS = TICKER_TOPICS + KLINE_TOPICS + BOOK_TOPICS + TRADE_TOPICS
SPOT_WS = "wss://stream.bybit.com/v5/public/spot"
SPOT_TOPICS = [f"tickers.{s}" for s in SYMBOLS]
SNAP_FILE = f"{BASE}/logs/bybit_prices.json"
PRICE_LOG = f"{BASE}/logs/price_1s.jsonl"
STATE_FILE = f"{BASE}/logs/bybit_bridge_state.json"
DEPTH_FILE = f"{BASE}/logs/orderbook.json"
TRADES_LOG = f"{BASE}/logs/trades_1s.jsonl"
STEP_FINE = {"BTCUSDT": 0.1, "ETHUSDT": 0.01}   # 最细聚合档位
BIG_TH = {"BTCUSDT": 5.0, "ETHUSDT": 50.0}       # 大单阈值(币)

LOCK = threading.Lock()
PRICES = {}
KLINES = {}
SNAP = {"ts": 0, "lag_ms": None, "prices": {}}
N_TICKS = {"n": 0}
TEST_DONE = threading.Event()
WS_REF = {"ws": None}
# ---- 盘口/成交量状态 (M1) ----
BOOKS = {}   # {sym: {"bids": {p:sz}, "asks": {p:sz}, "u": seq, "snap": bool, "gaps": n}}
TRADES = {s: deque(maxlen=30) for s in SYMBOLS}   # 最近30笔逐笔
AGGS = {s: deque(maxlen=60000) for s in SYMBOLS}  # (T_ms, side, v, p) 滚动窗口原始流
BIG = deque(maxlen=12)                            # 大单事件
DEPTH_SAID = {"hello": False}


def on_open(ws):
    WS_REF["ws"] = ws
    ws.send(json.dumps({"op": "subscribe", "args": ALL_TOPICS}))
    with LOCK:
        SNAP["connected_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def on_msg(ws, m):
    d = json.loads(m)
    topic = d.get("topic", "")
    if not topic:
        return
    if topic.startswith("tickers."):
        sym = topic.split(".")[1]
        t = d.get("data", {})
        if t.get("lastPrice") is None:
            return
        lag = int(time.time() * 1000) - int(d["ts"])
        with LOCK:
            # 用 update 保留 spot 键 (spot通道写在同一dict; 整体替换会每100ms抹掉现货价)
            PRICES.setdefault(sym, {})
            PRICES[sym].update({"last": float(t["lastPrice"]),
                                "change_pct": float(t.get("price24hPcnt", 0) or 0) * 100,
                                "high": float(t.get("highPrice24h", 0) or 0),
                                "low": float(t.get("lowPrice24h", 0) or 0),
                                "vol": float(t.get("turnover24h", 0) or 0), "ts": int(d["ts"])})
            SNAP.update(ts=int(time.time() * 1000), lag_ms=lag, prices=dict(PRICES))
            N_TICKS["n"] += 1
    elif topic.startswith("kline."):
        sym = topic.split(".")[2]
        with LOCK:
            KLINES[sym] = {int(k["start"]): k for k in d.get("data", [])}
    elif topic.startswith("orderbook."):
        _on_book(d)
    elif topic.startswith("publicTrade."):
        _on_trade(d)
    _write_snap()


# ---- 盘口深度: 快照+增量按u序列重组 (乱序丢弃, 缺口重订阅) ----
def _on_book(d):
    sym = d["topic"].split(".")[2]  # orderbook.200.BTCUSDT → [0]=orderbook [1]=depth [2]=sym
    data = d.get("data", {})
    ty = d.get("type")
    with LOCK:
        b = BOOKS.setdefault(sym, {"bids": {}, "asks": {}, "u": 0, "snap": False, "gaps": 0})
        if ty == "snapshot":
            b["bids"] = {str(p): float(s) for p, s in data.get("b", []) if float(s) > 0}
            b["asks"] = {str(p): float(s) for p, s in data.get("a", []) if float(s) > 0}
            b["u"] = int(data.get("u", 0))
            b["snap"] = True
        elif ty == "delta" and b["snap"]:
            u = int(data.get("u", 0))
            if u != b["u"] + 1:
                b["gaps"] += 1
                b["snap"] = False  # 缺口: 等重订阅拿新快照
                return
            b["u"] = u
            for p, s in data.get("b", []):
                if float(s) == 0:
                    b["bids"].pop(str(p), None)
                else:
                    b["bids"][str(p)] = float(s)
            for p, s in data.get("a", []):
                if float(s) == 0:
                    b["asks"].pop(str(p), None)
                else:
                    b["asks"][str(p)] = float(s)
    # 缺口自动修复: 重订阅触发新快照
    with LOCK:
        b = BOOKS.get(sym)
        if b and not b["snap"] and b["gaps"] <= 3:
            ws = WS_REF["ws"]
            if ws:
                threading.Thread(target=lambda: ws.send(json.dumps(
                    {"op": "subscribe", "args": [f"orderbook.200.{sym}"]})), daemon=True).start()
                b["gaps"] += 1


# ---- 逐笔成交: 方向/量/价入环, 大单检测, 最细档位聚合原料 ----
def _on_trade(d):
    sym = d["topic"].split(".")[1]
    with LOCK:
        for t in d.get("data", []):
            v = float(t.get("v", 0))
            if v <= 0:
                continue
            side = t.get("S", "")
            p = float(t.get("p", 0))
            ts = int(t.get("T", 0))
            TRADES[sym].append({"T": ts, "S": side, "v": v, "p": p})
            AGGS[sym].append((ts, side, v, p))
            if v >= BIG_TH.get(sym, 1e9):
                BIG.append({"sym": sym, "S": side, "v": v, "p": p, "T": ts})


def _agg_window(q, step, now, wins=(15, 60, 300)):
    """窗口主动量聚合: {bucket_idx: [买15,卖15,买60,卖60,买300,卖300]}"""
    grid = {}
    for ts, side, v, p in q:
        if now - ts > wins[-1] * 1000:
            continue
        k = int(round(p / step))
        g = grid.setdefault(k, [0.0] * 6)
        for i, w in enumerate(wins):
            if now - ts <= w * 1000:
                g[i + (0 if side == "Buy" else 1)] += v
    return grid


def depth_loop():
    """每秒: 盘口快照+逐笔带+聚合 → orderbook.json 原子写; trades_1s.jsonl 秒级留痕"""
    while True:
        time.sleep(1)
        with LOCK:
            books_out, trades_out, aggs_out, big_out = {}, {}, {}, list(BIG)
            for sym in SYMBOLS:
                b = BOOKS.get(sym)
                if b and b["snap"]:
                    books_out[sym] = {
                        "bids": [[p, s] for p, s in sorted(b["bids"].items(), key=lambda x: -float(x[0]))[:200]],
                        "asks": [[p, s] for p, s in sorted(b["asks"].items(), key=lambda x: float(x[0]))[:200]]}
                trades_out[sym] = list(TRADES[sym])  # 逐笔带独立于盘口就绪
            now = int(time.time() * 1000)
            for sym in SYMBOLS:
                q = AGGS[sym]
                while q and now - q[0][0] > 300000:
                    q.popleft()
                grid = _agg_window(q, STEP_FINE[sym], now)
                if grid:
                    aggs_out[sym] = {"step": STEP_FINE[sym],
                                     "grid": {str(k): v for k, v in grid.items()}}
        snap = {"ts": now, "books": books_out, "trades": trades_out, "aggs": aggs_out, "big": big_out}
        tmp = DEPTH_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False)
        os.replace(tmp, DEPTH_FILE)
        if not DEPTH_SAID["hello"] and books_out:
            DEPTH_SAID["hello"] = True
            print("[bridge] 盘口+逐笔通道已上线", flush=True)
        # 秒级成交留痕 (DuckDB 管道扩展)
        for sym in SYMBOLS:
            q = AGGS[sym]
            if not q:
                continue
            recent = [x for x in q if now - x[0] <= 1000]
            if recent:
                rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()), "sym": sym,
                       "n_buy": sum(1 for x in recent if x[1] == "Buy"),
                       "n_sell": sum(1 for x in recent if x[1] == "Sell"),
                       "bv": round(sum(x[2] for x in recent if x[1] == "Buy"), 6),
                       "sv": round(sum(x[2] for x in recent if x[1] == "Sell"), 6)}
                with open(TRADES_LOG, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _write_snap():
    with LOCK:
        payload = json.dumps(SNAP, ensure_ascii=False)
    tmp = SNAP_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(payload)
    os.replace(tmp, SNAP_FILE)


# ---- 现货行情通道 (carry页实时基差需要现货价) ----
SPOT_LAST_MSG = {"t": time.time()}
SPOT_SAID = {"hello": False}


def spot_on_open(ws):
    ws.send(json.dumps({"op": "subscribe", "args": SPOT_TOPICS}))
    print("[bridge] spot通道已连接+订阅", flush=True)


def spot_on_msg(ws, m):
    SPOT_LAST_MSG["t"] = time.time()
    d = json.loads(m)
    if not d.get("topic", "").startswith("tickers."):
        return
    sym = d["topic"].split(".")[1]
    t = d.get("data", {})
    if t.get("lastPrice") is None:
        return
    with LOCK:
        PRICES.setdefault(sym, {})
        PRICES[sym]["spot"] = float(t["lastPrice"])
        if not SPOT_SAID["hello"]:
            SPOT_SAID["hello"] = True
            print("[bridge] spot首条行情到达", flush=True)
        SNAP.update(ts=int(time.time() * 1000), prices=dict(PRICES))
    _write_snap()


def spot_loop():
    while True:
        SPOT_LAST_MSG["t"] = time.time()
        ws = websocket.WebSocketApp(SPOT_WS, on_message=spot_on_msg, on_open=spot_on_open)

        def _watchdog():
            while True:
                time.sleep(15)
                if time.time() - SPOT_LAST_MSG["t"] > 90:
                    print("[bridge] spot 90s无消息(半开连接), 强制重连", flush=True)
                    try:
                        ws.close()
                    except Exception:
                        pass
                    return

        threading.Thread(target=_watchdog, daemon=True).start()
        try:
            ws.run_forever(ping_interval=20, ping_timeout=10)
        except Exception as e:
            print(f"[bridge] spot连接异常: {e}", flush=True)
        print("[bridge] spot连接中断, 5s后重连", flush=True)
        time.sleep(5)


def persist_loop():
    """每秒一条快照 → price_1s.jsonl"""
    last_sec = 0
    while True:
        time.sleep(0.5)
        with LOCK:
            snap = json.loads(json.dumps(SNAP))
        ts = snap.get("ts", 0)
        if not ts or ts == last_sec:
            continue
        last_sec = ts
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()), "lag_ms": snap["lag_ms"],
               "prices": snap["prices"]}
        with open(PRICE_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def state_loop():
    """每5秒写健康状态 (看板可读)"""
    while True:
        time.sleep(5)
        with LOCK:
            st = {"alive": N_TICKS["n"] > 0, "ticks": N_TICKS["n"], "lag_ms": SNAP["lag_ms"],
                  "symbols": sorted(PRICES), "snap_ts": SNAP["ts"],
                  "connected_at": SNAP.get("connected_at"), "now": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False)
        os.replace(tmp, STATE_FILE)


def test_cb_hook():
    """--test 模式: 收够推送即关闭连接退出"""
    while True:
        time.sleep(0.1)
        if N_TICKS["n"] >= 8 and WS_REF["ws"]:
            WS_REF["ws"].close()
            TEST_DONE.set()
            return


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true")
    args = ap.parse_args()
    threading.Thread(target=persist_loop, daemon=True).start()
    threading.Thread(target=state_loop, daemon=True).start()
    threading.Thread(target=spot_loop, daemon=True).start()
    threading.Thread(target=depth_loop, daemon=True).start()
    if args.test:
        threading.Thread(target=test_cb_hook, daemon=True).start()
    backoff = 1
    while True:
        ws = websocket.WebSocketApp("wss://stream.bybit.com/v5/public/linear",
                                    on_message=on_msg, on_open=on_open)
        t0 = time.time()
        try:
            ws.run_forever(ping_interval=20, ping_timeout=10)
        except Exception as e:
            print(f"[bridge] 异常: {e}", flush=True)
        if args.test and TEST_DONE.is_set():
            with LOCK:
                print(f"  [test] ticks: {N_TICKS['n']} | lag_ms: {SNAP['lag_ms']} | symbols: {sorted(PRICES)}",
                      flush=True)
            return
        # 断线重连 (指数退避)
        dt = time.time() - t0
        print(f"[bridge] 连接中断(存活{dt:.0f}s), {backoff}s后重连", flush=True)
        time.sleep(backoff)
        backoff = min(backoff * 2, 30)


if __name__ == "__main__":
    main()
