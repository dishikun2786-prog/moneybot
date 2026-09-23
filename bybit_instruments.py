#!/usr/bin/env python3
"""Bybit 全标的目录缓存 (P1 全标的行情)
- instruments-info 全量拉取 (linear+spot, 分页 cursor)
- /v5/market/tickers 全量 24h 成交额 (活跃度排序)
- 输出 logs/bybit_instruments.json (桥/API/移动端共用)
用法: python bybit_instruments.py [--refresh]  (oneshot; systemd timer 每日 02:00 调用)
"""
import json
import os
import sys
import time
import urllib.request

BASE = os.path.expanduser("~/polymarket")
OUT = os.path.join(BASE, "logs", "bybit_instruments.json")
HDR = {"User-Agent": "Mozilla/5.0"}


def http_json(url, timeout=25):
    req = urllib.request.Request(url, headers=HDR)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def paged(category, path):
    """翻页拉全量 instruments-info"""
    out = []
    cursor = ""
    while True:
        url = f"https://api.bybit.com/v5/market/{path}?category={category}&limit=200"
        if cursor:
            url += "&cursor=" + cursor
        d = http_json(url)
        if d.get("retCode") != 0:
            print(f"[instruments] {category} 拉取失败: {d.get('retMsg')}", flush=True)
            break
        lst = d["result"]["list"]
        out.extend(lst)
        cursor = d["result"].get("nextPageCursor") or ""
        if not cursor or len(out) >= 4000:
            break
    return out


def turnover_map(category):
    """全量 tickers → {symbol: turnover24h}"""
    m = {}
    cursor = ""
    while True:
        url = f"https://api.bybit.com/v5/market/tickers?category={category}&limit=200"
        if cursor:
            url += "&cursor=" + cursor
        d = http_json(url)
        if d.get("retCode") != 0:
            print(f"[instruments] {category} tickers 失败: {d.get('retMsg')}", flush=True)
            break
        lst = d["result"]["list"]
        for t in lst:
            m[t["symbol"]] = float(t.get("turnover24h") or 0)
        cursor = d["result"].get("nextPageCursor") or ""
        if not cursor or len(m) >= 4000:
            break
    return m


def build(category, items, turnover):
    rows = []
    for it in items:
        sym = it.get("symbol", "")
        if it.get("status") != "Trading":
            continue
        rows.append({
            "symbol": sym,
            "base": it.get("baseCoin", ""),
            "quote": it.get("quoteCoin", "USDT"),
            "name": it.get("displayName") or it.get("baseCoin", ""),
            "fullName": it.get("fullName") or "",
            "tickSize": (it.get("priceFilter") or {}).get("tickSize", ""),
            "qtyStep": (it.get("lotSizeFilter") or {}).get("qtyStep", ""),
            "minQty": (it.get("lotSizeFilter") or {}).get("minOrderQty", ""),
            "preListing": bool(it.get("isPreListing")),
            "launchTime": it.get("launchTime", ""),
            "turnover24h": turnover.get(sym, 0.0),
        })
    # 按 24h 成交额降序 (活跃度)
    rows.sort(key=lambda r: -r["turnover24h"])
    return rows


def main():
    t0 = time.time()
    lin = paged("linear", "instruments-info")
    sp = paged("spot", "instruments-info")
    print(f"[instruments] linear={len(lin)} spot={len(sp)} 拉取完成 ({time.time()-t0:.1f}s)", flush=True)
    t_lin = turnover_map("linear")
    t_sp = turnover_map("spot")
    out = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "refresh_at": time.time(),
        "linear": build("linear", lin, t_lin),
        "spot": build("spot", sp, t_sp),
    }
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    os.replace(tmp, OUT)
    n_lin = len(out["linear"])
    n_sp = len(out["spot"])
    top = out["linear"][0] if out["linear"] else {}
    print(f"[instruments] 缓存完成: linear={n_lin} (Trading) spot={n_sp} (Trading) → {OUT}",
          flush=True)
    print(f"[instruments] linear Top1: {top.get('symbol')} 成交额 {top.get('turnover24h'):,.0f}$",
          flush=True)


if __name__ == "__main__":
    main()
