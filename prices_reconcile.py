#!/usr/bin/env python3
"""Phase 4 影子对账: Go prices gateway vs Python bridge 数据一致性 + 新鲜度监控
- 校验 bybit_prices.json 新鲜度(ts<10s) / 标的数 / 关键字段合理性
- 校验 Go gateway SSE(8090) 与 bybit_prices.json 价格一致
- 异常 → TG 告警 (复用 alert_config.json)
零外部依赖。用法: python prices_reconcile.py
"""
import json, os, time, urllib.request

BASE = os.path.expanduser("~/polymarket")
PRICES = os.path.join(BASE, "logs", "bybit_prices.json")
SSE_URL = "http://127.0.0.1:8090/api/stream/prices"
EXPECT_SYMS = 7

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

def _sse_prices():
    """从 Go gateway SSE 抓一帧 prices（2s 超时）"""
    import http.client
    try:
        c = http.client.HTTPConnection("127.0.0.1", 8090, timeout=2)
        c.request("GET", "/api/stream/prices")
        r = c.getresponse()
        # 读前 64KB, 取第一条 data:
        buf = r.read(65536).decode("utf-8", "ignore")
        c.close()
        for line in buf.splitlines():
            if line.startswith("data: "):
                return json.loads(line[6:])
    except Exception:
        return None
    return None

def main():
    problems = []
    try:
        d = json.load(open(PRICES, encoding="utf-8"))
    except Exception as e:
        problems.append(f"bybit_prices.json 读取失败: {e}")
        d = None

    if d:
        ts = d.get("ts") or 0
        age = (time.time() * 1000 - ts) / 1000
        if age > 10:
            problems.append(f"价格快照陈旧 {age:.0f}s")
        n = len(d.get("prices", {}))
        if n != EXPECT_SYMS:
            problems.append(f"标的数异常 {n} (期望 {EXPECT_SYMS})")
        btc = d.get("prices", {}).get("BTCUSDT", {})
        if btc.get("last", 0) <= 0:
            problems.append("BTCUSDT.last 异常")
        fr = btc.get("funding_rate")
        if fr is not None and abs(float(fr)) > 0.01:
            problems.append(f"BTC funding_rate 异常 {fr}")

    # Go SSE 对账
    sse = _sse_prices()
    if sse is None:
        problems.append("Go gateway SSE 不可达(8090)")
    elif d:
        sp = sse.get("prices", {}).get("BTCUSDT", {})
        fp = d.get("prices", {}).get("BTCUSDT", {})
        if sp.get("last") and fp.get("last"):
            diff = abs(float(sp["last"]) - float(fp["last"]))
            if diff > 1.0:
                problems.append(f"SSE vs 文件 BTC last 偏差 {diff:.2f}")

    status = "OK" if not problems else " | ".join(problems)
    print(f"[prices_reconcile] {time.strftime('%H:%M:%S')} {status}")
    if problems:
        _tg(f"⚠️ prices 对账异常: {status}")
    return 0 if not problems else 1

if __name__ == "__main__":
    raise SystemExit(main())
