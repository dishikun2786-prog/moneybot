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

import websocket

BASE = os.path.expanduser("~/polymarket")
SYMBOLS = ["BTCUSDT", "ETHUSDT"]
TICKER_TOPICS = [f"tickers.{s}" for s in SYMBOLS]
KLINE_TOPICS = [f"kline.1.{s}" for s in SYMBOLS]
ALL_TOPICS = TICKER_TOPICS + KLINE_TOPICS
SNAP_FILE = f"{BASE}/logs/bybit_prices.json"
PRICE_LOG = f"{BASE}/logs/price_1s.jsonl"
STATE_FILE = f"{BASE}/logs/bybit_bridge_state.json"

LOCK = threading.Lock()
PRICES = {}
KLINES = {}
SNAP = {"ts": 0, "lag_ms": None, "prices": {}}
N_TICKS = {"n": 0}
TEST_DONE = threading.Event()
WS_REF = {"ws": None}


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
            PRICES[sym] = {"last": float(t["lastPrice"]), "change_pct": float(t.get("price24hPcnt", 0) or 0) * 100,
                           "high": float(t.get("highPrice24h", 0) or 0), "low": float(t.get("lowPrice24h", 0) or 0),
                           "vol": float(t.get("turnover24h", 0) or 0), "ts": int(d["ts"])}
            SNAP.update(ts=int(time.time() * 1000), lag_ms=lag, prices=dict(PRICES))
            N_TICKS["n"] += 1
    elif topic.startswith("kline."):
        sym = topic.split(".")[2]
        with LOCK:
            KLINES[sym] = {int(k["start"]): k for k in d.get("data", [])}
    _write_snap()


def _write_snap():
    with LOCK:
        payload = json.dumps(SNAP, ensure_ascii=False)
    tmp = SNAP_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(payload)
    os.replace(tmp, SNAP_FILE)


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
