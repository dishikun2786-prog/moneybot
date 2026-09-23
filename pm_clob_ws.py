#!/usr/bin/env python3
"""PM CLOB 实时行情桥 (P4)
- gamma REST 拉 Top60 活跃事件 → 全部市场 token ids → pm_tokens.json 映射
- CLOB WS (ws-subscriptions-clob /ws/market) 订阅 price_change (assets_ids=token ids)
  → best_bid/best_ask/最新成交 → pm_prices.json (500ms 节流原子写)
- CLOB REST /markets 启动种子价 + 断线兜底
- 10s PING 心跳 + 自动重连 + 30min 目录刷新
systemd: pm-clob
"""
import json
import os
import threading
import time
import urllib.request
import urllib.parse

import websocket

BASE = os.path.expanduser("~/polymarket")
LOG_DIR = os.path.join(BASE, "logs")
TOK_FILE = os.path.join(LOG_DIR, "pm_tokens.json")
PX_FILE = os.path.join(LOG_DIR, "pm_prices.json")
CLOB_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
GAMMA = "https://gamma-api.polymarket.com"
CLOB_REST = "https://clob.polymarket.com"
HDR = {"User-Agent": "Mozilla/5.0"}
MAX_EVENTS = 150   # R5: 60→150 全市场覆盖 (分类+搜索)
REFRESH_S = 1800          # 目录刷新 30min
BATCH = 100               # 每消息订阅 token 数

PX = {}          # {token: {"bid": f, "ask": f, "last": f, "ts": ms}}
TOKENS = []      # [{token, key, event_id, title, question, outcome}]
WS_REF = {"ws": None}
STOP = {"v": False}


def http_json(url, timeout=25):
    req = urllib.request.Request(url, headers=HDR)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def refresh_catalog():
    """Top 活跃事件 → 全部 token ids + 元数据 → pm_tokens.json; 返回 token id 列表"""
    global TOKENS
    evs = []
    # gamma limit 上限 100, 分页凑 MAX_EVENTS; 失败时退避重试
    for off in range(0, MAX_EVENTS, 100):
        for attempt in range(3):
            try:
                page = http_json(f"{GAMMA}/events?active=true&closed=false&limit=100"
                                 f"&offset={off}&order=volume24hr&ascending=false")
                evs.extend(page)
                break
            except Exception as e:
                if attempt == 2:
                    print(f"[pm-clob] events 拉取失败(off={off}): {e}", flush=True)
                else:
                    time.sleep(2 * (attempt + 1))
    if not evs:
        return []
    toks = []
    n_mk = 0
    for ev in evs:
        mks = ev.get("markets") or []
        n_mk += len(mks)
        # R5: 事件分类 (gamma tags) + 市场量价元数据
        cats = []
        for tg in (ev.get("tags") or []):
            lb = tg.get("label") if isinstance(tg, dict) else str(tg)
            if lb:
                cats.append(str(lb).lower())
        cat = cats[0] if cats else "other"
        ev_vol = float(ev.get("volume24hr") or 0)
        for mk in mks:
            raw = mk.get("clobTokenIds")
            if not raw:
                continue
            try:
                ids = json.loads(raw) if isinstance(raw, str) else raw
            except Exception:
                continue
            for tok in ids:
                if not isinstance(tok, str) or not tok.isdigit():
                    continue
                key = f"{ev.get('slug', '')}|{mk.get('slug', '')}"
                toks.append({"token": tok, "key": key, "event_id": str(ev.get("id", "")),
                             "title": ev.get("title") or ev.get("slug", ""),
                             "question": mk.get("question") or "",
                             "outcome": mk.get("outcomes", "?") or "?",
                             "group": mk.get("groupItemTitle", ""),
                             "cat": cat, "ev_vol": ev_vol,
                             "mk_chg": float(mk.get("oneDayPriceChange") or 0),
                             "mk_vol": float(mk.get("volume24hr") or 0)})
    # 去重: 按 (key, token) 去重 (防事件分页重叠/市场重复)
    uniq = {}
    for t in toks:
        uniq[(t["key"], t["token"])] = t
    TOKENS = list(uniq.values())
    tmp = TOK_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(TOKENS, f, ensure_ascii=False)
    os.replace(tmp, TOK_FILE)
    print(f"[pm-clob] 目录刷新: {len(evs)} 事件/{n_mk} 市场 → {len(TOKENS)} tokens", flush=True)
    return [t["token"] for t in TOKENS]


def seed_from_rest():
    """CLOB REST /markets 种子价 (启动/断线兜底): {data:[{tokens:[{token_id, price}]}]}"""
    try:
        d = http_json(f"{CLOB_REST}/markets", timeout=60)
        lst = d.get("data", d) if isinstance(d, dict) else d
        n = 0
        for m in lst or []:
            if not isinstance(m, dict):
                continue
            for t in m.get("tokens", []):
                tok = t.get("token_id")
                if not tok:
                    continue
                PX.setdefault(tok, {
                    "bid": None, "ask": None,
                    "last": t.get("price"), "ts": time.time()})
                n += 1
        print(f"[pm-clob] REST 种子价: {n} tokens", flush=True)
    except Exception as e:
        print(f"[pm-clob] REST 种子失败: {e}", flush=True)


def on_open(ws):
    WS_REF["ws"] = ws
    print("[pm-clob] WS 已连接, 批量订阅 price_change", flush=True)
    ids = [t["token"] for t in TOKENS]
    for i in range(0, len(ids), BATCH):
        ws.send(json.dumps({"assets_ids": ids[i:i + BATCH], "type": "market",
                            "markets": [], "initial_dump": True,
                            "custom_feature_enabled": True}))
    print(f"[pm-clob] 已发送 {len(ids)} token 订阅", flush=True)


def on_message(ws, m):
    try:
        d = json.loads(m)
    except Exception:
        return
    if not isinstance(d, dict):   # CLOB 部分消息是数组
        return
    if d.get("event_type") != "price_change":
        return
    for pc in d.get("price_changes", []):
        if not isinstance(pc, dict):
            continue
        tok = pc.get("asset_id")
        if not tok:
            continue
        bid = pc.get("best_bid")
        ask = pc.get("best_ask")
        px = pc.get("price")
        PX[tok] = {"bid": float(bid) if bid is not None else None,
                   "ask": float(ask) if ask is not None else None,
                   "last": float(px) if px is not None else None,
                   "ts": time.time()}


def on_error(ws, e):
    print(f"[pm-clob] WS error: {e}", flush=True)


def on_close(ws, c, m):
    print(f"[pm-clob] WS closed: {c} {m}", flush=True)


def snap_loop():
    while not STOP["v"]:
        time.sleep(0.5)
        try:
            out = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                   "n": len(PX), "prices": PX}
            tmp = PX_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(out, f, ensure_ascii=False)
            os.replace(tmp, PX_FILE)
        except Exception:
            pass


def hb_loop(ws):
    while not STOP["v"]:
        time.sleep(10)
        try:
            ws.send("PING")
        except Exception:
            break


def run_ws():
    while not STOP["v"]:
        ws = websocket.WebSocketApp(CLOB_WS, on_open=on_open, on_message=on_message,
                                    on_error=on_error, on_close=on_close)
        t = threading.Thread(target=hb_loop, args=(ws,), daemon=True)
        t.start()
        ws.run_forever(ping_interval=20, ping_timeout=10)
        print("[pm-clob] 重连中…", flush=True)
        time.sleep(3)


def refresh_loop():
    while not STOP["v"]:
        time.sleep(REFRESH_S)
        try:
            new_ids = refresh_catalog()
            ws = WS_REF["ws"]
            if ws and new_ids:
                # 幂等重订阅 (服务器按 assets_ids 去重, 无需退订旧集合)
                for i in range(0, len(new_ids), BATCH):
                    ws.send(json.dumps({"assets_ids": new_ids[i:i + BATCH],
                                        "type": "market", "markets": [],
                                        "initial_dump": True,
                                        "custom_feature_enabled": True}))
        except Exception as e:
            print(f"[pm-clob] 刷新失败: {e}", flush=True)


def main():
    global TOKENS
    print("[pm-clob] 启动 (P4 PM实时行情桥)", flush=True)
    TOKENS = []
    tok_list = refresh_catalog() or []
    if not tok_list:
        # 目录失败: 读旧 pm_tokens.json
        try:
            TOKENS = json.load(open(TOK_FILE, encoding="utf-8"))
        except Exception:
            TOKENS = []
    threading.Thread(target=snap_loop, daemon=True).start()
    threading.Thread(target=refresh_loop, daemon=True).start()
    threading.Thread(target=seed_from_rest, daemon=True).start()
    run_ws()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        STOP["v"] = True
