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

import threading
threading.excepthook = lambda args: print(f"[bridge] 线程异常: {args.exc_value}", flush=True)  # R14-M7: 任何线程未捕获异常打日志不死静默
import websocket

BASE = os.path.expanduser("~/polymarket")


def _load_bybit_syms():
    """标的清单: 优先读 web 管理后台同步的配置文件, 回退环境变量, 再回退默认"""
    conf = os.path.join(BASE, "data", "bybit_syms.txt")
    try:
        if os.path.exists(conf):
            syms = [s.strip().upper() for s in open(conf, encoding="utf-8").read().split(",") if s.strip()]
            if syms:
                return syms
    except Exception:
        pass
    return [s.strip().upper() for s in os.environ.get(
        "BYBIT_SYMS", "BTCUSDT,ETHUSDT,XAUUSDT,XAGUSDT,XAUTUSDT,SOLUSDT,NEARUSDT,XRPUSDT").split(",") if s.strip()]


# 深度/微结构标的 (盘口/K线/逐笔/墙/CVD 只对活跃套利标的, 保持 BTC/ETH)
SYM_WHITELIST = _load_bybit_syms()  # R14-M2: +SOL/NEAR/XRP 双通道标的
DEPTH_SYMS = list(SYM_WHITELIST)
SPOT_ONLY_SYMS = [s for s in SYM_WHITELIST if s in ("XAUTUSDT",)]  # R14: 现货独占标的
LINEAR_SYMS = [s for s in DEPTH_SYMS if s not in SPOT_ONLY_SYMS]    # 主连接(linear)只订合约标的
SPOT_CH_SYMS = ["XAUTUSDT", "BTCUSDT", "ETHUSDT", "SOLUSDT", "NEARUSDT", "XRPUSDT"]  # R14-M2: 现货通道盘口/成交标的
KLINE_TOPICS = [f"kline.1.{s}" for s in LINEAR_SYMS]
BOOK_TOPICS = [f"orderbook.200.{s}" for s in LINEAR_SYMS]
TRADE_TOPICS = [f"publicTrade.{s}" for s in LINEAR_SYMS]
ALL_TOPICS = KLINE_TOPICS + BOOK_TOPICS + TRADE_TOPICS
SPOT_WS = "wss://stream.bybit.com/v5/public/spot"
SPOT_PIN_SYMS = [s for s in () if s]  # R13c: 白名单收敛, 不再固定订阅外围现货
def _spot_depth_topics():
    """R14: 现货端点 tickers 只订现货标的 (linear 标的在 spot 端点会 Invalid symbol 致整批失败)"""
    syms = [s for s in list(dict.fromkeys(list(DEPTH_SYMS) + SPOT_PIN_SYMS)) if s in SPOT_TICKER_SYMS or s in SPOT_PIN_SYMS]
    return [f"tickers.{s}" for s in syms]
SNAP_FILE = f"{BASE}/logs/bybit_prices.json"
PRICE_LOG = f"{BASE}/logs/price_1s.jsonl"
STATE_FILE = f"{BASE}/logs/bybit_bridge_state.json"
DEPTH_FILE = f"{BASE}/logs/orderbook.json"
DEPTH_N = 20  # 每侧落盘前 N 档 (FDTD 深度场回放)
TRADES_LOG = f"{BASE}/logs/trades_1s.jsonl"
MICRO_LOG = f"{BASE}/logs/micro_1m.jsonl"


def _basis_row(sym):
    """R14-M5: 现货-永续基差 (perp - spot) / spot ×100%, 键给 depth 帧"""
    p = PRICES.get(sym, {})
    perp, spot = p.get("last"), p.get("spot")
    try:
        perp, spot = float(perp), float(spot)
        b = (perp - spot) / spot * 100.0 if spot > 0 else None
    except Exception:
        b = None
    return {"perp": perp, "spot": spot, "basis_pct": None if b is None else round(b, 4)}
WALL_LOG = f"{BASE}/logs/wall_events.jsonl"
BIG_LOG = f"{BASE}/logs/big_trades.jsonl"
INSTR_FILE = f"{BASE}/logs/bybit_instruments.json"
STEP_FINE = {"BTCUSDT": 0.1, "ETHUSDT": 0.01, "XAUUSDT": 0.01, "XAGUSDT": 0.01,
             "XAUTUSDT": 0.01, "SOLUSDT": 0.01, "NEARUSDT": 0.001, "XRPUSDT": 0.0001}  # R14: 黄金现货  # 最细聚合档位(实测tick: XAU/XAG均0.01美元/盎司)
BIG_TH = {"BTCUSDT": 5.0, "ETHUSDT": 50.0, "XAUUSDT": 200.0, "XAGUSDT": 10000.0}  # 大单阈值(XAU:盎司 XAG:盎司)
WALL_MULT = 8.0        # 墙: 单档size ≥ 同侧前20档均值×MULT
WALL_SHARE = 0.25      # 或 ≥ 该侧总量×SHARE
TICKER_CAP = int(os.environ.get("TICKER_CAP", "0"))  # 0=全部; N=按成交额只取前N (CPU降级)


def load_ticker_syms():
    """全标的 ticker 订阅清单 (目录缓存; 缺失时回退 BTC/ETH)"""
    lin, spot = [], []
    try:
        with open(INSTR_FILE, encoding="utf-8") as f:
            d = json.load(f)
        # R13c: 白名单收敛 (BTC/ETH/XAU/XAG 4标的), 不再全量订阅
        lin = [r["symbol"] for r in d.get("linear", [])
               if not r.get("preListing") and r["symbol"] in SYM_WHITELIST]
        spot = [r["symbol"] for r in d.get("spot", []) if r["symbol"] in SYM_WHITELIST]
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


def _batched_sub(ws, topics, delay=0.0):
    """Bybit WSS 每消息最多10个 topic, 分批订阅
    delay>0 时批间限速 (spot 通道全量订阅瞬间连发会被 Bybit 限流踢断)"""
    for i in range(0, len(topics), 10):
        ws.send(json.dumps({"op": "subscribe", "args": topics[i:i + 10]}))
        if delay > 0 and i + 10 < len(topics):
            time.sleep(delay)

LOCK = threading.Lock()
PRICES = {}
KLINES = {}
SNAP = {"ts": 0, "lag_ms": None, "prices": {}}
N_TICKS = {"n": 0}
TEST_DONE = threading.Event()
WS_REF = {"ws": None, "spot_ws": None}
# ---- 盘口/成交量状态 (M1) ----
BOOKS = {}   # {sym: {"bids": {p:sz}, "asks": {p:sz}, "u": seq, "snap": bool, "gaps": n}}
WATCH_FILE = f"{BASE}/logs/depth_watch.json"   # P2 按需盘口: {sym: req_ts}
KL_WATCH_FILE = f"{BASE}/logs/kline_watch.json"  # R4 按需K线: {sym: iv}
KL_SNAP_FILE = f"{BASE}/logs/kl_snap.json"       # R4 实时K线快照
KL_IV_NUM = {"1m": "1", "5m": "5", "15m": "15", "1h": "60", "4h": "240", "D": "D", "W": "W", "M": "M"}
KL_WATCH_MAX = 8
KL_WATCH_SUB = {}  # sym -> (iv, ts)
WATCH_MAX = 20      # 动态盘口标的 LRU 上限 (不含 DEPTH_SYMS)
WATCH_SUB = {s: time.time() for s in DEPTH_SYMS}  # {sym: 订阅时间}
TRADES = {s: deque(maxlen=30) for s in DEPTH_SYMS}   # 最近30笔逐笔
AGGS = {s: deque(maxlen=60000) for s in DEPTH_SYMS}  # (T_ms, side, v, p) 滚动窗口原始流
BIG = deque(maxlen=12)                            # 大单事件
BIG_PEND = []                                     # 大单待落盘 (depth_loop冲刷)
DEPTH_SAID = {"hello": False}
CVDS = {s: {"day": "", "cum": 0.0} for s in DEPTH_SYMS}   # 当日CVD滚动累计
WALLS_PREV = {s: set() for s in DEPTH_SYMS}               # 上一帧墙价位集合
MICRO_HIST = {s: deque(maxlen=120) for s in DEPTH_SYMS}   # 分钟级指标环 (t, price, cum_cvd, oi)
MICRO_BUF = {s: {"bv": 0.0, "sv": 0.0, "nb": 0, "ns": 0} for s in DEPTH_SYMS}  # 本分钟累计


SPOT_ONLY_TOPICS = ([f"kline.1.{s}" for s in SPOT_ONLY_SYMS]
                    + [f"orderbook.200.{s}" for s in SPOT_CH_SYMS]
                    + [f"publicTrade.{s}" for s in SPOT_CH_SYMS])  # R14: 现货通道盘口/成交(含BTC/ETH现货)


BOOKS_SPOT = {}   # R14: 现货通道盘口 (与合约盘口分离, 双通道标的独立价)
TRADES_SPOT = {}  # R14: 现货通道成交


def _on_book_spot(d):
    """现货端点盘口重组 (与合约盘口分离存储)"""
    sym = d["topic"].split(".")[2]
    data = d.get("data", {})
    ty = d.get("type")
    with LOCK:
        b = BOOKS_SPOT.setdefault(sym, {"bids": {}, "asks": {}, "u": 0, "snap": False, "gaps": 0})
        if ty == "snapshot":
            b["bids"] = {str(p): float(s) for p, s in data.get("b", []) if float(s) > 0}
            b["asks"] = {str(p): float(s) for p, s in data.get("a", []) if float(s) > 0}
            b["u"] = int(data.get("u", 0))
            b["snap"] = True
        elif ty == "delta" and b["snap"]:
            u = int(data.get("u", 0))
            if u != b["u"] + 1:
                b["gaps"] += 1
                b["snap"] = False
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


def on_spot_msg(ws, m):
    """现货市场统一连接: 处理 kline + orderbook + publicTrade (现货盘口/成交独立于合约)"""
    try:
        d = json.loads(m)
    except Exception:
        return
    topic = d.get("topic", "")
    if topic.startswith("kline."):
        parts = topic.split(".")
        if len(parts) < 3:
            return
        iv = parts[1]
        sym = parts[2]
        with LOCK:
            KLINES.setdefault(sym, {})[iv] = {int(k["start"]): k for k in d.get("data", [])}
    elif topic.startswith("orderbook."):
        _on_book_spot(d)  # R14: 现货盘口分离
    elif topic.startswith("publicTrade."):
        sym = topic.split(".")[1]
        if sym not in TRADES_SPOT:
            TRADES_SPOT[sym] = []
        with LOCK:
            # R14-M6: 统一为合约格式 {T,S,v,p} — 前端 renderTrades 按 T/S/v/p 渲染
            TRADES_SPOT[sym].extend([{"p": float(t.get("p", 0)), "v": float(t.get("v", 0)),
                                      "T": int(t.get("T", 0)),
                                      "S": t.get("S", "Buy")}
                                     for t in d.get("data", [])])
            TRADES_SPOT[sym] = TRADES_SPOT[sym][-50:]  # R14: 现货成交留30-50条


def spot_mkt_loop():
    """R9/R10: 现货端点统一连接 (kline + orderbook 都只在 spot WSS 推送; R14: 补现货独占标的订阅)"""
    while True:
        def _spot_open(w):
            if SPOT_ONLY_TOPICS:
                # R14-M2: 分批订阅 (单消息>10主题被Bybit静默拒, 27主题必须分批)
                for i in range(0, len(SPOT_ONLY_TOPICS), 10):
                    w.send(json.dumps({"op": "subscribe",
                                       "args": SPOT_ONLY_TOPICS[i:i + 10]}))
                    time.sleep(0.3)
        ws = websocket.WebSocketApp(SPOT_WS, on_message=on_spot_msg,
                                    on_open=_spot_open)
        WS_REF["spot_ws"] = ws
        # R14-M6: 重连后按需K线恢复 (延迟到连接建立后)
        def _spot_resub():
            time.sleep(1)
            if WS_REF.get("spot_ws") is ws:
                _resub_kline(ws, "spot")
        threading.Thread(target=_spot_resub, daemon=True).start()
        try:
            ws.run_forever(ping_interval=20, ping_timeout=10)
        except Exception as e:
            print(f"[bridge] spot-mkt 异常: {e}", flush=True)
        time.sleep(2)


def on_open(ws):
    WS_REF["ws"] = ws
    ws.send(json.dumps({"op": "subscribe", "args": ALL_TOPICS}))
    _batched_sub(ws, [f"tickers.{s}" for s in TICKER_SYMS])
    _resub_kline(ws, "linear")  # R14-M6: 重连恢复按需K线
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
            if t.get("volume24h") is not None:
                upd["qv"] = float(t["volume24h"])
            # OI 字段多数tick为null (Bybit仅周期性推送): 只在有值时更新, 保留最后已知值
            if t.get("openInterest"):
                upd["oi"] = float(t["openInterest"])
            if t.get("openInterestValue"):
                upd["oi_val"] = float(t["openInterestValue"])
            # R14-M9: 资金费率(套利平台核心数据) — ticker 周期性推送, 有值才更新
            if t.get("fundingRate"):
                try:
                    upd["funding_rate"] = float(t["fundingRate"])
                except Exception:
                    pass
            if t.get("nextFundingTime"):
                try:
                    upd["next_funding"] = int(t["nextFundingTime"])
                except Exception:
                    pass
            PRICES[sym].update(upd)
            LAST_LAG["ms"] = lag
            N_TICKS["n"] += 1
    elif topic.startswith("kline."):
        parts = topic.split(".")  # kline.1.BTCUSDT / kline.15.SOLUSDT
        iv = parts[1]
        sym = parts[2]
        with LOCK:
            KLINES.setdefault(sym, {})[iv] = {int(k["start"]): k for k in d.get("data", [])}
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


def _kline_snap_out():
    """R14-M7: depth 帧附带K线末根快照 — 前端K线与盘口/成交同帧(1s)刷新, 不再依赖3s REST轮询"""
    out = {}
    with LOCK:  # 必须加锁: on_message 线程并发写 KLINES, 无锁遍历会 RuntimeError 杀线程
        items = [(s, dict(p)) for s, p in KLINES.items()]
    for sym, per in items:
        for iv, roots in per.items():
            if not roots:
                continue
            st = max(roots.keys())
            k = roots[st]
            out.setdefault(sym, {})[iv] = {"t": int(st), "o": float(k["open"]), "h": float(k["high"]),
                                          "l": float(k["low"]), "c": float(k["close"]),
                                          "v": float(k["volume"]), "cf": bool(k.get("confirm"))}
    return out


def depth_loop():
    """每秒: 盘口快照+逐笔带+聚合+CVD+墙检测+分钟指标 → orderbook.json 原子写; 多路留痕"""
    last_min = ""
    while True:
        time.sleep(1)
        now = int(time.time() * 1000)
        ts_s = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
        with LOCK:
            books_out, trades_out, aggs_out, big_out = {}, {}, {}, list(BIG)
            for sym in list(WATCH_SUB):          # P2: 盘口输出 = 动态订阅集合 (R14-M9: 输出前过滤白名单)
                if sym not in SYM_WHITELIST:
                    continue
                b = BOOKS.get(sym)
                if b and b["snap"]:
                    books_out[sym] = {
                        "bids": [[p, s] for p, s in sorted(b["bids"].items(), key=lambda x: -float(x[0]))[:200]],
                        "asks": [[p, s] for p, s in sorted(b["asks"].items(), key=lambda x: float(x[0]))[:200]]}

            for sym in DEPTH_SYMS:
                if sym in SYM_WHITELIST:
                    trades_out[sym] = list(TRADES[sym])
            books_spot_out, trades_spot_out = {}, {}
            for sym in SPOT_CH_SYMS:
                bs = BOOKS_SPOT.get(sym)
                if bs and bs["snap"]:
                    books_spot_out[sym] = {
                        "bids": [[p, s] for p, s in sorted(bs["bids"].items(), key=lambda x: -float(x[0]))[:200]],
                        "asks": [[p, s] for p, s in sorted(bs["asks"].items(), key=lambda x: float(x[0]))[:200]]}
                trades_spot_out[sym] = list(TRADES_SPOT.get(sym, []))  # R14: 现货通道输出
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
                grid = _agg_window(q, STEP_FINE.get(sym, 0.01), now)  # R14: 缺键兜底
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
                "books_spot": books_spot_out, "trades_spot": trades_spot_out,  # R14: 现货通道盘口/成交
                "kline_snap": _kline_snap_out(),  # R14-M7: K线末根快照(K线与盘口成交同帧1s)
                "px": {s: {"last": PRICES.get(s, {}).get("last"),
                          "chg": PRICES.get(s, {}).get("change_pct", 0),
                          "oi": PRICES.get(s, {}).get("oi"),
                          "funding": PRICES.get(s, {}).get("funding_rate"),
                          "next_funding": PRICES.get(s, {}).get("next_funding")} for s in DEPTH_SYMS},  # R14-M9: +OI+资金费率
                "px_spot": {s: PRICES.get(s, {}).get("spot") for s in SPOT_CH_SYMS},
                # R14-M5: 现货-永续基差监控 (仅双通道标的, spot-only 无永续腿是假基差)
                "basis": {s: _basis_row(s) for s in SPOT_CH_SYMS if s not in SPOT_ONLY_SYMS},
                "big": big_out, "walls": walls_out, "micro": micro_out}
        tmp = DEPTH_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False)
        os.replace(tmp, DEPTH_FILE)
        # 按秒落盘 Bybit L2 深度 (long 格式, 日切文件, 供 FDTD 深度场回放)
        _dl = []
        for _sym in DEPTH_SYMS:
            _bk = books_out.get(_sym)
            if not _bk:
                continue
            _bids = _bk.get("bids", [])[:DEPTH_N]
            _asks = _bk.get("asks", [])[:DEPTH_N]
            if not _bids or not _asks:
                continue
            for _i, _lv in enumerate(_bids):
                _dl.append(json.dumps({"ts": ts_s, "symbol": _sym, "side": "bid",
                                       "level": _i, "price": _lv[0], "size": _lv[1]},
                                      ensure_ascii=False))
            for _i, _lv in enumerate(_asks):
                _dl.append(json.dumps({"ts": ts_s, "symbol": _sym, "side": "ask",
                                       "level": _i, "price": _lv[0], "size": _lv[1]},
                                      ensure_ascii=False))
        if _dl:
            _df = os.path.join(BASE, "logs",
                               f"bybit_depth_1s_{time.strftime('%Y%m%d', time.gmtime())}.jsonl")
            with open(_df, "a", encoding="utf-8") as _f:
                _f.write("\n".join(_dl) + "\n")
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
    """500ms 节流: 全量 PRICES → 快照文件 (拷贝在锁内, JSON序列化在锁外, 避免阻塞行情线程)"""
    while True:
        time.sleep(0.5)
        with LOCK:
            snap = {"ts": int(time.time() * 1000), "lag_ms": LAST_LAG["ms"],
                    "prices": dict(PRICES)}
            SNAP.update(snap)
        payload = json.dumps(snap, ensure_ascii=False)
        tmp = SNAP_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp, SNAP_FILE)
        # 按需K线快照 (小数据: ≤8标的 × 500bar)
        try:
            with LOCK:
                kl = dict(KLINES)
            kpayload = json.dumps(kl, ensure_ascii=False)
            ktmp = KL_SNAP_FILE + ".tmp"
            with open(ktmp, "w", encoding="utf-8") as f:
                f.write(kpayload)
            os.replace(ktmp, KL_SNAP_FILE)
        except Exception:
            pass
        # 按需K线快照 (小数据: ≤8标的 × 500bar)
        try:
            with LOCK:
                kl = dict(KLINES)
            kpayload = json.dumps(kl, ensure_ascii=False)
            ktmp = KL_SNAP_FILE + ".tmp"
            with open(ktmp, "w", encoding="utf-8") as f:
                f.write(kpayload)
            os.replace(ktmp, KL_SNAP_FILE)
        except Exception:
            pass


# ---- 现货行情通道 (carry页实时基差需要现货价) ----
SPOT_LAST_MSG = {"t": time.time()}
SPOT_SAID = {"hello": False}


def spot_on_open(ws):
    shard = getattr(ws, "_shard", (0, None))
    topics = shard[1] if shard[1] is not None else [f"tickers.{s}" for s in SPOT_TICKER_SYMS]
    if shard[0] == 0:
        ws.send(json.dumps({"op": "subscribe", "args": _spot_depth_topics()}))
    n_batches = (len(topics) + 9) // 10
    print(f"[bridge] spot#{shard[0]} 连接, 分批订阅 {len(topics)} 个 ({n_batches} 批 x 0.4s)", flush=True)
    _batched_sub(ws, topics, delay=0.4)
    print(f"[bridge] spot#{shard[0]} 订阅完成", flush=True)


def spot_on_msg(ws, m):
    SPOT_LAST_MSG["t"] = time.time()
    d = json.loads(m)
    if d.get("op") == "subscribe":
        if not d.get("success"):
            print(f"[bridge] spot订阅失败: {d.get('ret_msg')}, 5s后全量限速重订", flush=True)
            def _retry():
                time.sleep(5)
                try:
                    _batched_sub(ws, [f"tickers.{s}" for s in SPOT_TICKER_SYMS], delay=0.5)
                except Exception:
                    pass
            threading.Thread(target=_retry, daemon=True).start()
        return
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


def spot_loop(shard=0, topics=None):
    """spot ticker 多连接分片: 全量订阅单连接会撞 Bybit 消息速率墙(静默丢数据)
    shard: 分片号 (0 号兼任 BTC/ETH 盘口), topics: 该分片的 ticker 主题"""
    if topics is None:
        topics = [f"tickers.{s}" for s in SPOT_TICKER_SYMS]
    while True:
        SPOT_LAST_MSG["t"] = time.time()
        ws = websocket.WebSocketApp(SPOT_WS, on_message=spot_on_msg, on_open=spot_on_open)
        ws._shard = (shard, topics)

        def _watchdog():
            while True:
                time.sleep(15)
                if time.time() - SPOT_LAST_MSG["t"] > 90:
                    print(f"[bridge] spot#{shard} 90s无消息(半开连接), 强制重连", flush=True)
                    try:
                        ws.close()
                    except Exception:
                        pass
                    return

        threading.Thread(target=_watchdog, daemon=True).start()
        try:
            ws.run_forever(ping_interval=20, ping_timeout=10)
        except Exception as e:
            print(f"[bridge] spot#{shard} 连接异常: {e}", flush=True)
        print(f"[bridge] spot#{shard} 连接中断, 5s后重连", flush=True)
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


def watch_loop():
    """P2 按需盘口: 轮询 depth_watch.json → 新标的订阅 orderbook.200; LRU 上限退订最旧非深度标的"""
    while True:
        time.sleep(2)
        try:
            with open(WATCH_FILE, encoding="utf-8") as f:
                reqs = json.load(f)
        except Exception:
            reqs = {}
        ws = WS_REF["ws"]
        if not ws:
            continue
        for sym, ts in list(reqs.items())[:40]:
            if sym in WATCH_SUB:
                continue
            is_spot = sym in SPOT_TICKER_SYMS and sym not in TICKER_SYMS
            tgt = WS_REF.get("spot_ws") if is_spot else ws
            if not tgt:
                continue
            tgt.send(json.dumps({"op": "subscribe", "args": [f"orderbook.200.{sym}"]}))
            WATCH_SUB[sym] = time.time()
            BOOKS.pop(sym, None)  # 清旧残, 等新快照
            print(f"[bridge] 按需订阅盘口: {sym} ({'spot' if is_spot else 'linear'}#{len(WATCH_SUB) - len(DEPTH_SYMS)})", flush=True)
        # LRU 退订: 超上限时退最旧的非 DEPTH_SYMS (按各自端点退订)
        dyn = {s: t for s, t in WATCH_SUB.items() if s not in DEPTH_SYMS}
        while len(dyn) > WATCH_MAX:
            oldest = min(dyn, key=dyn.get)
            is_spot_o = oldest in SPOT_TICKER_SYMS and oldest not in TICKER_SYMS
            ows = WS_REF.get("spot_ws") if is_spot_o else ws
            if ows:
                ows.send(json.dumps({"op": "unsubscribe", "args": [f"orderbook.200.{oldest}"]}))
            WATCH_SUB.pop(oldest, None)
            BOOKS.pop(oldest, None)
            print(f"[bridge] LRU退订盘口: {oldest}", flush=True)
            dyn = {s: t for s, t in WATCH_SUB.items() if s not in DEPTH_SYMS}


def state_loop():
    """每5秒写健康状态 (看板可读)"""
    while True:
        time.sleep(5)
        with LOCK:
            st = {"alive": N_TICKS["n"] > 0, "ticks": N_TICKS["n"], "lag_ms": SNAP["lag_ms"],
                  "symbols": sorted(PRICES), "n_symbols": len(PRICES),
                  "ticker_syms": len(TICKER_SYMS), "spot_syms": len(SPOT_TICKER_SYMS),
                  "watch_syms": sorted(WATCH_SUB),
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


def _resub_kline(ws, cat):
    """R14-M6: 端点重连后恢复按需K线订阅 (否则重连后只恢复常驻主题, 按需周期永久卡死)"""
    for sym, (iv, ts, c) in list(KL_WATCH_SUB.items()):
        if c != cat:
            continue
        try:
            ws.send(json.dumps({"op": "subscribe", "args": [f"kline.{KL_IV_NUM[iv]}.{sym}"]}))
        except Exception:
            pass


def watch_kline_loop():
    """R4 按需K线: 轮询 kline_watch.json → 订阅 kline.<iv>.<sym>; LRU 退订"""
    while True:
        time.sleep(2)
        try:
            with open(KL_WATCH_FILE, encoding="utf-8") as f:
                reqs = json.load(f)
        except Exception:
            reqs = {}
        ws = WS_REF["ws"]
        if not ws:
            continue
        for sym, raw in list(reqs.items())[:40]:
            cat = "linear"
            iv = raw
            if isinstance(raw, str) and "|" in raw:
                iv, cat = raw.split("|", 1)
            if isinstance(raw, dict):
                iv = raw.get("iv", "15m")
                cat = raw.get("cat", "linear")
            iv = iv if iv in KL_IV_NUM else "15m"
            tgt = WS_REF.get("spot_ws") if cat == "spot" else ws
            if not tgt:
                continue
            cur = KL_WATCH_SUB.get(sym)
            if cur and cur[0] == iv and cur[2] == cat:
                continue
            tgt.send(json.dumps({"op": "subscribe", "args": [f"kline.{KL_IV_NUM[iv]}.{sym}"]}))
            if cur:  # 同标的换周期/换分类: 退旧订新
                old_ws = WS_REF.get("spot_ws") if cur[2] == "spot" else ws
                if old_ws:
                    old_ws.send(json.dumps({"op": "unsubscribe", "args": [f"kline.{KL_IV_NUM[cur[0]]}.{sym}"]}))
            KL_WATCH_SUB[sym] = (iv, time.time(), cat)
            print(f"[bridge] 按需订阅K线: {sym} {iv}/{cat} (共{len(KL_WATCH_SUB)})", flush=True)
        # LRU 退订: 只退非白名单标的 (R14: 核心4标的K线恒驻, 不再被历史垃圾订阅挤掉)
        while len(KL_WATCH_SUB) > KL_WATCH_MAX:
            evictable = [s for s in KL_WATCH_SUB if s not in SYM_WHITELIST]
            if not evictable:
                break
            oldest = min(evictable, key=lambda s: KL_WATCH_SUB[s][1])
            oiv, ocat = KL_WATCH_SUB[oldest][0], KL_WATCH_SUB[oldest][2]
            old_ws = WS_REF.get("spot_ws") if ocat == "spot" else ws
            if old_ws:
                old_ws.send(json.dumps({"op": "unsubscribe", "args": [f"kline.{KL_IV_NUM[oiv]}.{oldest}"]}))
            KL_WATCH_SUB.pop(oldest, None)
            KLINES.pop(oldest, None)
            try:  # R14-M6: 同步清理watch文件, 防下一轮2s重订→再退订风暴
                with open(KL_WATCH_FILE, encoding="utf-8") as f:
                    _reqs = json.load(f)
                _reqs.pop(oldest, None)
                with open(KL_WATCH_FILE, "w", encoding="utf-8") as f:
                    json.dump(_reqs, f)
            except Exception:
                pass
            print(f"[bridge] LRU退订K线: {oldest}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true")
    args = ap.parse_args()
    threading.Thread(target=persist_loop, daemon=True).start()
    threading.Thread(target=state_loop, daemon=True).start()
    threading.Thread(target=snap_loop, daemon=True).start()
    threading.Thread(target=watch_loop, daemon=True).start()
    threading.Thread(target=watch_kline_loop, daemon=True).start()
    threading.Thread(target=spot_mkt_loop, daemon=True).start()
    # spot 多连接分片 (单连接全量订阅撞消息速率墙)
    SPOT_SHARDS = int(os.environ.get("SPOT_SHARDS", "3"))
    spot_topics = [f"tickers.{s}" for s in SPOT_TICKER_SYMS if s not in SPOT_PIN_SYMS]
    n_sp = len(spot_topics)
    for i in range(SPOT_SHARDS):
        shard_topics = spot_topics[i * n_sp // SPOT_SHARDS:(i + 1) * n_sp // SPOT_SHARDS]
        threading.Thread(target=spot_loop, args=(i, shard_topics), daemon=True).start()
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
