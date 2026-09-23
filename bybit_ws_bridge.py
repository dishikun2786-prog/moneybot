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
# 深度/微结构标的 (盘口/K线/逐笔/墙/CVD 只对活跃套利标的, 保持 BTC/ETH)
DEPTH_SYMS = ["BTCUSDT", "ETHUSDT"]
KLINE_TOPICS = [f"kline.1.{s}" for s in DEPTH_SYMS]
BOOK_TOPICS = [f"orderbook.200.{s}" for s in DEPTH_SYMS]
TRADE_TOPICS = [f"publicTrade.{s}" for s in DEPTH_SYMS]
ALL_TOPICS = KLINE_TOPICS + BOOK_TOPICS + TRADE_TOPICS
SPOT_WS = "wss://stream.bybit.com/v5/public/spot"
SPOT_DEPTH_TOPICS = [f"tickers.{s}" for s in DEPTH_SYMS]
SNAP_FILE = f"{BASE}/logs/bybit_prices.json"
PRICE_LOG = f"{BASE}/logs/price_1s.jsonl"
STATE_FILE = f"{BASE}/logs/bybit_bridge_state.json"
DEPTH_FILE = f"{BASE}/logs/orderbook.json"
TRADES_LOG = f"{BASE}/logs/trades_1s.jsonl"
MICRO_LOG = f"{BASE}/logs/micro_1m.jsonl"
WALL_LOG = f"{BASE}/logs/wall_events.jsonl"
BIG_LOG = f"{BASE}/logs/big_trades.jsonl"
INSTR_FILE = f"{BASE}/logs/bybit_instruments.json"
STEP_FINE = {"BTCUSDT": 0.1, "ETHUSDT": 0.01}   # 最细聚合档位
BIG_TH = {"BTCUSDT": 5.0, "ETHUSDT": 50.0}       # 大单阈值(币)
WALL_MULT = 8.0        # 墙: 单档size ≥ 同侧前20档均值×MULT
WALL_SHARE = 0.25      # 或 ≥ 该侧总量×SHARE
TICKER_CAP = int(os.environ.get("TICKER_CAP", "0"))  # 0=全部; N=按成交额只取前N (CPU降级)


def load_ticker_syms():
    """全标的 ticker 订阅清单 (目录缓存; 缺失时回退 BTC/ETH)"""
    lin, spot = [], []
    try:
        with open(INSTR_FILE, encoding="utf-8") as f:
            d = json.load(f)
        lin = [r["symbol"] for r in d.get("linear", []) if not r.get("preListing")]
        spot = [r["symbol"] for r in d.get("spot", [])]
    except Exception as e:
        print(f"[bridge] 目录缓存读取失败({e}), 回退 BTC/ETH", flush=True)
        return list(DEPTH_SYMS), list(DEPTH_SYMS)
    if TICKER_CAP:
        lin = lin[:TICKER_CAP]
    if not lin:
        lin = list(DEPTH_SYMS)
    print(f"[bridge] ticker订阅: linear={len(lin)} spot={len(spot)} (cap={TICKER_CAP or '全量'})",
          flush=True)
    return lin, spot


TICKER_SYMS, SPOT_TICKER_SYMS = load_ticker_syms()


def _batched_sub(ws, topics):
    """Bybit WSS 每消息最多10个 topic, 分批订阅"""
    for i in range(0, len(topics), 10):
        ws.send(json.dumps({"op": "subscribe", "args": topics[i:i + 10]}))

LOCK = threading.Lock()
PRICES = {}
KLINES = {}
SNAP = {"ts": 0, "lag_ms": None, "prices": {}}
N_TICKS = {"n": 0}
TEST_DONE = threading.Event()
WS_REF = {"ws": None}
# ---- 盘口/成交量状态 (M1) ----
BOOKS = {}   # {sym: {"bids": {p:sz}, "asks": {p:sz}, "u": seq, "snap": bool, "gaps": n}}
TRADES = {s: deque(maxlen=30) for s in DEPTH_SYMS}   # 最近30笔逐笔
AGGS = {s: deque(maxlen=60000) for s in DEPTH_SYMS}  # (T_ms, side, v, p) 滚动窗口原始流
BIG = deque(maxlen=12)                            # 大单事件
BIG_PEND = []                                     # 大单待落盘 (depth_loop冲刷)
DEPTH_SAID = {"hello": False}
CVDS = {s: {"day": "", "cum": 0.0} for s in DEPTH_SYMS}   # 当日CVD滚动累计
WALLS_PREV = {s: set() for s in DEPTH_SYMS}               # 上一帧墙价位集合
MICRO_HIST = {s: deque(maxlen=120) for s in DEPTH_SYMS}   # 分钟级指标环 (t, price, cum_cvd, oi)
MICRO_BUF = {s: {"bv": 0.0, "sv": 0.0, "nb": 0, "ns": 0} for s in DEPTH_SYMS}  # 本分钟累计


def on_open(ws):
    WS_REF["ws"] = ws
    ws.send(json.dumps({"op": "subscribe", "args": ALL_TOPICS}))
    _batched_sub(ws, [f"tickers.{s}" for s in TICKER_SYMS])
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
            upd = {"last": float(t["lastPrice"]),
                   "change_pct": float(t.get("price24hPcnt", 0) or 0) * 100,
                   "high": float(t.get("highPrice24h", 0) or 0),
                   "low": float(t.get("lowPrice24h", 0) or 0),
                   "vol": float(t.get("turnover24h", 0) or 0), "ts": int(d["ts"])}
            # OI 字段多数tick为null (Bybit仅周期性推送): 只在有值时更新, 保留最后已知值
            if t.get("openInterest"):
                upd["oi"] = float(t["openInterest"])
            if t.get("openInterestValue"):
                upd["oi_val"] = float(t["openInterestValue"])
            PRICES[sym].update(upd)
            LAST_LAG["ms"] = lag
            N_TICKS["n"] += 1
    elif topic.startswith("kline."):
        sym = topic.split(".")[2]
        with LOCK:
            KLINES[sym] = {int(k["start"]): k for k in d.get("data", [])}
    elif topic.startswith("orderbook."):
        _on_book(d)
    elif topic.startswith("publicTrade."):
        _on_trade(d)


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
                BIG_PEND.append({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                 "sym": sym, "S": side, "v": v, "p": p, "T": ts})


def detect_walls(levels):
    """挂单墙检测: 单档size ≥ max(前20档均值×WALL_MULT, 该侧总量×WALL_SHARE)
    返回 {price_str: size}"""
    if len(levels) < 5:
        return {}
    top20 = sorted(levels, key=lambda x: -x[1])[:20]
    avg = sum(s for _, s in top20) / len(top20)
    total = sum(s for _, s in levels)
    thr = max(avg * WALL_MULT, total * WALL_SHARE)
    return {str(p): s for p, s in levels if s >= thr}


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
    """每秒: 盘口快照+逐笔带+聚合+CVD+墙检测+分钟指标 → orderbook.json 原子写; 多路留痕"""
    last_min = ""
    while True:
        time.sleep(1)
        now = int(time.time() * 1000)
        ts_s = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
        with LOCK:
            books_out, trades_out, aggs_out, big_out = {}, {}, {}, list(BIG)
            for sym in DEPTH_SYMS:
                b = BOOKS.get(sym)
                if b and b["snap"]:
                    books_out[sym] = {
                        "bids": [[p, s] for p, s in sorted(b["bids"].items(), key=lambda x: -float(x[0]))[:200]],
                        "asks": [[p, s] for p, s in sorted(b["asks"].items(), key=lambda x: float(x[0]))[:200]]}
                trades_out[sym] = list(TRADES[sym])
            walls_out = {}
            for sym in DEPTH_SYMS:
                b = BOOKS.get(sym)
                if not b or not b["snap"]:
                    continue
                wb = detect_walls([(float(p), s) for p, s in b["bids"].items()])
                wa = detect_walls([(float(p), s) for p, s in b["asks"].items()])
                walls_out[sym] = {"bids": [[p, s] for p, s in wb.items()],
                                  "asks": [[p, s] for p, s in wa.items()]}
                cur = set(wb) | set(wa)
                prev = WALLS_PREV[sym]
                for p in cur - prev:
                    side = "bids" if p in wb else "asks"
                    sz = wb.get(p) or wa.get(p)
                    _append_log(WALL_LOG, {"ts": ts_s, "sym": sym, "side": side,
                                           "price": float(p), "size": sz, "type": "appear"})
                for p in prev - cur:
                    _append_log(WALL_LOG, {"ts": ts_s, "sym": sym, "type": "vanish",
                                           "price": float(p)})
                WALLS_PREV[sym] = cur
            # 本秒 CVD 与分钟缓冲
            for sym in DEPTH_SYMS:
                q = AGGS[sym]
                while q and now - q[0][0] > 300000:
                    q.popleft()
                recent = [x for x in q if now - x[0] <= 1000]
                bv = sum(x[2] for x in recent if x[1] == "Buy")
                sv = sum(x[2] for x in recent if x[1] == "Sell")
                nb = sum(1 for x in recent if x[1] == "Buy")
                ns = sum(1 for x in recent if x[1] == "Sell")
                today = time.strftime("%Y-%m-%d", time.gmtime())
                c = CVDS[sym]
                if c["day"] != today:
                    c["day"] = today
                    c["cum"] = 0.0
                c["cum"] = round(c["cum"] + bv - sv, 6)
                mb = MICRO_BUF[sym]
                mb["bv"] += bv
                mb["sv"] += sv
                mb["nb"] += nb
                mb["ns"] += ns
                grid = _agg_window(q, STEP_FINE[sym], now)
                if grid:
                    aggs_out[sym] = {"step": STEP_FINE[sym],
                                     "grid": {str(k): v for k, v in grid.items()}}
                if recent:
                    _append_log(TRADES_LOG, {"ts": ts_s, "sym": sym, "n_buy": nb, "n_sell": ns,
                                             "bv": round(bv, 6), "sv": round(sv, 6),
                                             "cvd": round(bv - sv, 6), "cum_cvd": c["cum"]})
            # 分钟边界: micro_1m 落盘 + 指标环
            micro_out = {}
            cur_min = ts_s[:16]
            if last_min and cur_min != last_min:
                for sym in DEPTH_SYMS:
                    mb = MICRO_BUF[sym]
                    px = PRICES.get(sym, {})
                    rec = {"ts": last_min + ":00", "sym": sym,
                           "bv": round(mb["bv"], 6), "sv": round(mb["sv"], 6),
                           "nb": mb["nb"], "ns": mb["ns"],
                           "cvd_1m": round(mb["bv"] - mb["sv"], 6),
                           "cum_cvd": CVDS[sym]["cum"],
                           "last": px.get("last"), "oi": px.get("oi"), "oi_val": px.get("oi_val")}
                    _append_log(MICRO_LOG, rec)
                    MICRO_HIST[sym].append((last_min, rec["last"], rec["cum_cvd"], rec["oi"]))
                    MICRO_BUF[sym] = {"bv": 0.0, "sv": 0.0, "nb": 0, "ns": 0}
            last_min = cur_min
            for sym in DEPTH_SYMS:
                micro_out[sym] = {"hist": [list(x) for x in MICRO_HIST[sym]],
                                  "oi": PRICES.get(sym, {}).get("oi"),
                                  "oi_val": PRICES.get(sym, {}).get("oi_val")}
            big_pend = list(BIG_PEND)
            BIG_PEND.clear()
        # 大单历史落盘 (锁外)
        for rec in big_pend:
            _append_log(BIG_LOG, rec)
        snap = {"ts": now, "books": books_out, "trades": trades_out, "aggs": aggs_out,
                "big": big_out, "walls": walls_out, "micro": micro_out}
        tmp = DEPTH_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False)
        os.replace(tmp, DEPTH_FILE)
        if not DEPTH_SAID["hello"] and books_out:
            DEPTH_SAID["hello"] = True
            print("[bridge] 盘口+逐笔通道已上线", flush=True)


def _append_log(path, rec):
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[bridge] 日志写入失败 {path}: {e}", flush=True)


LAST_LAG = {"ms": None}


def snap_loop():
    """500ms 节流: 全量 PRICES → SNAP → 原子写快照 (1500标的每秒2次, 不再逐消息写)"""
    while True:
        time.sleep(0.5)
        with LOCK:
            SNAP.update(ts=int(time.time() * 1000), lag_ms=LAST_LAG["ms"],
                        prices=dict(PRICES))
            payload = json.dumps(SNAP, ensure_ascii=False)
        tmp = SNAP_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp, SNAP_FILE)


# ---- 现货行情通道 (carry页实时基差需要现货价) ----
SPOT_LAST_MSG = {"t": time.time()}
SPOT_SAID = {"hello": False}


def spot_on_open(ws):
    ws.send(json.dumps({"op": "subscribe", "args": SPOT_DEPTH_TOPICS}))
    _batched_sub(ws, [f"tickers.{s}" for s in SPOT_TICKER_SYMS])
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
        px = snap.get("prices") or {}
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()), "lag_ms": snap["lag_ms"],
               "prices": {s: px[s] for s in DEPTH_SYMS if s in px}}
        with open(PRICE_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def state_loop():
    """每5秒写健康状态 (看板可读)"""
    while True:
        time.sleep(5)
        with LOCK:
            st = {"alive": N_TICKS["n"] > 0, "ticks": N_TICKS["n"], "lag_ms": SNAP["lag_ms"],
                  "symbols": sorted(PRICES), "n_symbols": len(PRICES),
                  "ticker_syms": len(TICKER_SYMS), "spot_syms": len(SPOT_TICKER_SYMS),
                  "snap_ts": SNAP["ts"],
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
    threading.Thread(target=snap_loop, daemon=True).start()
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
