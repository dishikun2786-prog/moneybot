#!/usr/bin/env python3
"""Bybit v5 签名 REST 客户端 (M4/M5 实盘线)
- HMAC-SHA256 签名: sign = hmac(secret, f"{ts}{key}{recv}{qs}{body}")
- 连通性测试: get-api-key-info (读权限) + 双交易权限 (现货+合约) + 余额查询
- 下单/撤单/持仓/余额 (M5 实盘执行器数据面)
安全: 密钥只经参数传入, 不打印不落盘; 错误信息脱敏
"""
import hashlib
import hmac
import http.client
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.bybit.com"
API_HOST = "api.bybit.com"
RECV = 5000
UA = "moneybot/1.0"

_conns = threading.local()


def _conn():
    c = getattr(_conns, "conn", None)
    if c is None or c.sock is None:
        c = http.client.HTTPSConnection(API_HOST, timeout=10)
        _conns.conn = c
    return c


def _reset_conn():
    c = getattr(_conns, "conn", None)
    if c is not None:
        try:
            c.close()
        except Exception:
            pass
        _conns.conn = None


def _req(key, secret, method, path, params=None, body=None, timeout=10):
    ts = str(int(time.time() * 1000))
    qs = urllib.parse.urlencode(params) if params else ""
    body_s = json.dumps(body) if body is not None else ""
    sign = hmac.new(secret.encode(), f"{ts}{key}{RECV}{qs}{body_s}".encode(),
                    hashlib.sha256).hexdigest()
    headers = {"X-BAPI-API-KEY": key, "X-BAPI-TIMESTAMP": ts, "X-BAPI-SIGN": sign,
               "X-BAPI-RECV-WINDOW": str(RECV), "Content-Type": "application/json",
               "User-Agent": UA}
    url = path + (("?" + qs) if qs else "")
    body_bytes = body_s.encode() if body_s else None
    for attempt in (0, 1):
        try:
            conn = _conn()
            conn.request(method, url, body=body_bytes, headers=headers)
            r = conn.getresponse()
            raw = r.read()
            if r.status == 200:
                return json.loads(raw.decode())
            # 非200: 尝试解析 Bybit 错误体; 连接状态不明则重置
            try:
                d = json.loads(raw.decode())
                if isinstance(d, dict) and d.get("retCode") is not None:
                    return d
            except Exception:
                pass
            _reset_conn()
            return {"retCode": r.status, "retMsg": "HTTP %d" % r.status}
        except Exception as e:
            _reset_conn()
            if attempt == 0:
                continue  # 陈旧 keep-alive 连接 → 重置后重试一次
            return {"retCode": -1, "retMsg": f"网络错误: {type(e).__name__}"}
    return {"retCode": -1, "retMsg": "网络错误"}


def get_api_key_info(key, secret):
    return _req(key, secret, "GET", "/v5/user/get-api-key-info")


def account_info(key, secret):
    return _req(key, secret, "GET", "/v5/account/info")


def wallet_balance(key, secret, account_type="UNIFIED"):
    return _req(key, secret, "GET", "/v5/account/wallet-balance",
                params={"accountType": account_type})


def positions(key, secret, category="linear", symbol=None):
    if category == "spot":
        # R14-M4: UTA 现货持仓不在 /v5/position/list, 在 wallet-balance 的 coin.spot 字段
        return _spot_positions(key, secret, symbol)
    params = {"category": category}
    if symbol:
        params["symbol"] = symbol
    return _req(key, secret, "GET", "/v5/position/list", params=params)


def _spot_positions(key, secret, symbol=None):
    """R14-M4: UTA 现货持仓 (coin.spot > 0) → 与 linear 同形状 {result:{list:[{symbol,size,...}]}}"""
    p = {"accountType": "UNIFIED"}
    if symbol:
        p["coin"] = symbol.replace("USDT", "")
    d = _req(key, secret, "GET", "/v5/account/wallet-balance", params=p)
    rows = []
    try:
        for acc in (d.get("result") or {}).get("list", []):
            for c in acc.get("coin", []):
                # UTA: 非稳定币的 walletBalance 即现货持仓 (spot 字段仅现货杠杆账户用)
                if (c.get("coin") or "").upper() in ("USDT", "USDC", "USD"):
                    continue
                spot = float(c.get("walletBalance") or c.get("spot") or 0)
                if spot > 0:
                    rows.append({"symbol": (c.get("coin") or "") + "USDT",
                                 "size": str(spot), "avgPrice": "0", "side": "Buy",
                                 "unrealisedPnl": "0"})
    except Exception:
        pass
    return {"retCode": d.get("retCode", 0), "result": {"list": rows}}


def place_order(key, secret, symbol, side, qty, category="linear",
                order_type="Market", price=None, reduce_only=False,
                position_idx=0, time_in_force="GTC"):
    body = {"category": category, "symbol": symbol, "side": side,
            "orderType": order_type, "qty": str(qty),
            "positionIdx": position_idx, "timeInForce": time_in_force}
    if price is not None:
        body["price"] = str(price)
    if reduce_only:
        body["reduceOnly"] = True
    return _req(key, secret, "POST", "/v5/order/create", body=body)


def place_spot_order(key, secret, symbol, side, qty, order_type="Market", price=None):
    # R14-M4: UTA 现货市价单必须 marketUnit=baseCoin, 否则 qty 被解释为 quote 金额
    # → 小额单全报 170140 "Order value exceeded lower limit" (实测)
    body = {"category": "spot", "symbol": symbol, "side": side,
            "orderType": order_type, "qty": str(qty), "marketUnit": "baseCoin"}
    if price is not None:
        body["price"] = str(price)
    return _req(key, secret, "POST", "/v5/order/create", body=body)


def cancel_order(key, secret, symbol, order_id, category="linear"):
    return _req(key, secret, "POST", "/v5/order/cancel",
                body={"category": category, "symbol": symbol, "orderId": order_id})


def query_order(key, secret, symbol, order_id, category="linear"):
    """R14-M4: 订单查询 (同步性测试: 下单/撤单后状态链核对)"""
    return _req(key, secret, "GET", "/v5/order/realtime",
                params={"category": category, "symbol": symbol, "orderId": order_id})


def open_orders(key, secret, category="linear", symbol=None, limit=20):
    """R14-M4: 挂单列表 (撤单同步验证)"""
    p = {"category": category, "limit": limit}
    if symbol:
        p["symbol"] = symbol
    return _req(key, secret, "GET", "/v5/order/realtime", params=p)


def test_bybit(key, secret):
    """连通性测试: 读权限 + 双交易权限 + 余额。返回 (ok, msg)"""
    info = get_api_key_info(key, secret)
    if info.get("retCode") != 0:
        return False, f"密钥无效: {info.get('retMsg', '')[:120]}"
    perms = info.get("result", {}).get("permissions") or {}
    spot = perms.get("Spot") or []
    ctr = perms.get("ContractTrade") or []
    has_read = info.get("result", {}).get("readOnly") == 0
    if info.get("result", {}).get("readOnly"):
        return False, "密钥为只读权限, 无法实盘交易"
    spot_trade = any("Trade" in p for p in spot)
    ctr_trade = any("Trade" in p for p in ctr)
    if not (spot_trade and ctr_trade):
        missing = []
        if not spot_trade:
            missing.append("现货交易")
        if not ctr_trade:
            missing.append("合约交易")
        return False, "密钥缺交易权限: " + "、".join(missing) + " (请在 Bybit 密钥设置里勾选)"
    bal = wallet_balance(key, secret)
    if bal.get("retCode") != 0:
        return False, f"余额查询失败: {bal.get('retMsg', '')[:120]}"
    return True, "连通正常: 读+现货交易+合约交易权限齐全"
