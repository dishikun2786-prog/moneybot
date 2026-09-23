#!/usr/bin/env python3
"""Polymarket 实盘客户端 (M4/M5b)
绑定(一次性): owner私钥 → SDK create 自动派生 L2 凭证 (apiKey/secret/passphrase) → 私钥即弃
交易(后续): 派生凭证 → CLOB REST L2 签名 (HMAC-SHA256, 官方 py-clob-client 同构)
  POST https://clob.polymarket.com/order  {token_id, price, side, size, order_type}
签名: sig = base64(hmac_sha256(secret, f"{ts}{METHOD}{requestPath}{body}"))
头: POLY_ADDRESS(小写)/POLY_TIMESTAMP/POLY_SIGNATURE/POLY_API_KEY/POLY_PASSPHRASE
安全: 私钥只进进程内存一次, 不落盘不入日志; 凭证由密钥库 AES-GCM 托管
"""
import asyncio
import base64
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request

CLOB = "https://clob.polymarket.com"
UA = "moneybot/1.0"


# ---------- 绑定: SDK 派生 (私钥一次性) ----------

def derive_credentials(private_key, wallet=None, relayer_key=None, relayer_address=None):
    """owner私钥 → L2 凭证。返回 {apiKey, secret, passphrase, wallet, signer, n_positions}
    私钥仅用于本次派生, 绝不落盘"""
    async def _run():
        from polymarket import AsyncSecureClient, RelayerApiKey
        api_key = None
        if relayer_key and relayer_address:
            api_key = RelayerApiKey(key=relayer_key, address=relayer_address)
        client = await AsyncSecureClient.create(
            private_key=private_key, wallet=wallet or None, api_key=api_key)
        try:
            creds = client.credentials
            positions = await client.list_positions() or []
            return {"apiKey": creds.apiKey, "secret": creds.secret,
                    "passphrase": creds.passphrase,
                    "wallet": client.wallet, "signer": client.signer,
                    "n_positions": len(positions)}
        finally:
            await client.close()
    return asyncio.run(_run())


# ---------- 交易: L2 凭证 REST 签名 ----------

def _l2_req(creds, wallet, method, path, body=None, timeout=10):
    ts = str(int(time.time()))
    body_s = json.dumps(body) if body is not None else ""
    msg = f"{ts}{method}{path}{body_s}"
    sig = base64.b64encode(hmac.new(creds["secret"].encode(), msg.encode(),
                                    hashlib.sha256).digest()).decode()
    headers = {"POLY_ADDRESS": wallet.lower(), "POLY_TIMESTAMP": ts,
               "POLY_SIGNATURE": sig, "POLY_API_KEY": creds["apiKey"],
               "POLY_PASSPHRASE": creds["passphrase"], "Content-Type": "application/json",
               "User-Agent": UA}
    req = urllib.request.Request(CLOB + path, method=method, headers=headers,
                                 data=body_s.encode() if body_s else None)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {"error": f"HTTP {e.code}"}
    except Exception as e:
        return -1, {"error": f"网络错误: {type(e).__name__}"}


def place_order(creds, wallet, token_id, price, side, size, order_type="GTC"):
    """CLOB 下单: price=美分(0.01-0.99), size=股数, side=BUY/SELL"""
    body = {"token_id": token_id, "price": str(price), "side": side,
            "size": float(size), "order_type": order_type}
    return _l2_req(creds, wallet, "POST", "/order", body=body)


def cancel_order(creds, wallet, order_id):
    return _l2_req(creds, wallet, "DELETE", f"/order/{order_id}")


def open_orders(creds, wallet):
    return _l2_req(creds, wallet, "GET", "/data/orders?open=true")


def positions(creds, wallet):
    return _l2_req(creds, wallet, "GET", "/data/positions")


def balance(creds, wallet):
    return _l2_req(creds, wallet, "GET", "/balance-allowance?asset_type=COLLATERAL&token_id=" + wallet.lower())


def test_credentials(creds, wallet):
    """凭证连通测试: 拉持仓接口 (读, 不动钱) — 401/403 即凭证失效"""
    st, d = positions(creds, wallet)
    if st == 200:
        return True, f"凭证有效: 当前持仓 {len(d) if isinstance(d, list) else '?'} 个"
    if st in (401, 403):
        return False, "凭证已失效(过期或钱包不匹配), 请重新绑定"
    return False, f"接口异常: HTTP {st}"


def _public(path, params=None, timeout=10):
    qs = urllib.parse.urlencode(params) if params else ""
    req = urllib.request.Request(CLOB + path + (("?" + qs) if qs else ""),
                                 headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except Exception as e:
        return -1, {"error": f"网络错误: {type(e).__name__}"}
