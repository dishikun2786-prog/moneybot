#!/usr/bin/env python3
"""Bybit v5 签名 REST 客户端 (M4/M5 实盘线)
- HMAC-SHA256 签名: sign = hmac(secret, f"{ts}{key}{recv}{qs}{body}")
- 连通性测试: get-api-key-info (读权限) + 双交易权限 (现货+合约) + 余额查询
- 下单/撤单/持仓/余额 (M5 实盘执行器数据面)
安全: 密钥只经参数传入, 不打印不落盘; 错误信息脱敏
"""
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.bybit.com"
RECV = 5000
UA = "moneybot/1.0"


def _req(key, secret, method, path, params=None, body=None, timeout=10):
    ts = str(int(time.time() * 1000))
    qs = urllib.parse.urlencode(params) if params else ""
    body_s = json.dumps(body) if body is not None else ""
    sign = hmac.new(secret.encode(), f"{ts}{key}{RECV}{qs}{body_s}".encode(),
                    hashlib.sha256).hexdigest()
    headers = {"X-BAPI-API-KEY": key, "X-BAPI-TIMESTAMP": ts, "X-BAPI-SIGN": sign,
               "X-BAPI-RECV-WINDOW": str(RECV), "Content-Type": "application/json",
               "User-Agent": UA}
    url = API + path + (("?" + qs) if qs else "")
    req = urllib.request.Request(url, method=method, headers=headers,
                                 data=body_s.encode() if body_s else None)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:
            return {"retCode": e.code, "retMsg": "HTTP %d" % e.code}
    except Exception as e:
        return {"retCode": -1, "retMsg": f"网络错误: {type(e).__name__}"}


def get_api_key_info(key, secret):
    return _req(key, secret, "GET", "/v5/user/get-api-key-info")


def account_info(key, secret):
    return _req(key, secret, "GET", "/v5/account/info")


def wallet_balance(key, secret, account_type="UNIFIED"):
    return _req(key, secret, "GET", "/v5/account/wallet-balance",
                params={"accountType": account_type})


def positions(key, secret, category="linear", symbol=None):
    params = {"category": category}
    if symbol:
        params["symbol"] = symbol
    return _req(key, secret, "GET", "/v5/position/list", params=params)


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
    body = {"category": "spot", "symbol": symbol, "side": side,
            "orderType": order_type, "qty": str(qty)}
    if price is not None:
        body["price"] = str(price)
    return _req(key, secret, "POST", "/v5/order/create", body=body)


def cancel_order(key, secret, symbol, order_id, category="linear"):
    return _req(key, secret, "POST", "/v5/order/cancel",
                body={"category": category, "symbol": symbol, "orderId": order_id})


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
