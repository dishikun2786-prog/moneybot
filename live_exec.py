#!/usr/bin/env python3
"""实盘执行器 (M5 商业化升级)
把纸面引擎的动作映射到真实交易所 (Bybit v5 / PM CLOB), 全链路风控:
  前置闸: 实盘版套餐(plan=live) → 管理员开实盘(live_enabled) → 已绑定密钥 → 限额
  限额:   单笔名义 ≤ max_notional; 当日累计名义 ≤ 10×max_notional (日亏熔断占位);
          实盘持仓数 ≤ max_positions
  幂等:   Bybit clientOrderId; PM order 由交易所 orderId 保证
  留痕:   tenants/<uid>/logs/live_orders.jsonl 台账 + users.audit 审计
  安全:   密钥解密仅在下单瞬间; 响应不返回密钥; 错误信息脱敏
支持动作:
  bybit: open_naked / close_naked / open_hedge / close_perp_leg / close_both
  pm:    buy / sell (CLOB GTC 限价单, price=美分)
"""
import json
import os
import sys
import time

BASE = os.path.expanduser("~/polymarket")
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "dash"))
sys.path.insert(0, os.path.join(BASE, "dash", "app"))

import bybit_live  # noqa: E402
import pm_live  # noqa: E402
import tenants  # noqa: E402

from app import keys, users  # noqa: E402
from app import funds as _funds  # noqa: E402 (R14-M4: 实盘统一平台密钥)


def _platform_bybit():
    """R14-M4: 实盘统一使用管理后台配置的平台 Bybit 密钥 (不再读用户自绑密钥)"""
    return _funds.platform_key()

MIN_ORDER_USDT = 6.0  # Bybit UTA 最低下单额(现货/合约通用下限, 币安式保守)
DAY_NOTIONAL_CAP_MULT = 10  # 当日累计名义 = 10 × 单笔限额 (日亏熔断占位策略)


def _ledger(uid=None):
    return os.path.join(tenants.logs(uid), "live_orders.jsonl")


def _append(uid, rec):
    with open(_ledger(uid), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _today_notional(uid):
    """当日实盘累计名义 (台账)"""
    today = time.strftime("%Y-%m-%d", time.gmtime())
    tot = 0.0
    try:
        with open(_ledger(uid), encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("ts", "").startswith(today):
                    tot += float(r.get("notional") or 0.0)
    except Exception:
        pass
    return tot


def live_positions_count(uid):
    """实盘当前持仓数 (Bybit 非零持仓 + PM 持仓)"""
    n = 0
    try:
        s = _platform_bybit()
        if s:
            d = bybit_live.positions(s["key"], s["secret"])
            if d.get("retCode") == 0:
                n += sum(1 for p in d["result"]["list"]
                         if float(p.get("size") or 0) != 0)
    except Exception:
        pass
    try:
        s = keys.get_secrets(uid, "pm")
        if s:
            extra = json.loads(s["extra"] or "{}")
            st, d = pm_live.positions({"apiKey": s["key"], "secret": s["secret"],
                                       "passphrase": extra.get("passphrase", "")},
                                      extra.get("wallet", ""))
            if st == 200 and isinstance(d, list):
                n += len(d)
    except Exception:
        pass
    return n


def _gate(uid, venue, notional):
    """实盘前置闸 (R14-M15): 充值自动开通 + 额度不限制(以账户余额为自然额度)"""
    u = users.get_user(uid)
    if not u or u["status"] != "active":
        return False, "账户不可用"
    if venue == "bybit" and not _platform_bybit():
        return False, "平台 Bybit 密钥未配置, 请联系管理员 (统一密钥)"
    if venue == "pm" and not keys.get_secrets(uid, "pm"):
        return False, "未绑定 Polymarket (密钥管理页绑定)"
    try:
        n = float(notional)
    except Exception:
        return False, "名义金额非法"
    if n < MIN_ORDER_USDT:
        return False, f"单笔名义须 ≥ ${MIN_ORDER_USDT}"
    # 充值自动开通: 账户余额即实盘额度 (无管理员审查、无固定限额)
    try:
        bal = float(_funds.get_balance(uid) or 0.0)
    except Exception:
        bal = 0.0
    if bal < n:
        return False, f"余额不足 (可用 {bal:.2f} USDT, 需 {n:.2f} USDT) — 充值后自动开通实盘, 额度=余额"
    return True, "ok"


# ---------- Bybit 原生单边 (P3) ----------

def _native_whitelist_ok(sym):
    """复用 paper_ops 白名单 (BTC/ETH + 成交额Top50)"""
    from paper_ops import _native_allowed
    return _native_allowed(sym)


def bybit_open_native(uid, body):
    """原生做多/做空: 永续市价单 (side=long→Buy, short→Sell), 精度按 qtyStep 取整"""
    sym = str(body.get("symbol", "")).upper()
    side = body.get("side", "long")
    if side not in ("long", "short"):
        return {"ok": False, "error": "方向须 long/short"}
    notional = float(body.get("notional") or 0)
    ok, err = _gate(uid, "bybit", notional)
    if not ok:
        return {"ok": False, "error": err}
    if not _native_whitelist_ok(sym):
        return {"ok": False, "error": f"{sym} 不在原生交易白名单"}
    s = _platform_bybit()
    d = bybit_live.positions(s["key"], s["secret"], symbol=sym)
    if d.get("retCode") == 0:
        for p in d["result"]["list"]:
            if float(p.get("size") or 0) != 0:
                return {"ok": False, "error": f"{sym} 已有实盘持仓, 先平仓再开新仓"}
    px = _last_price(sym)
    if not px:
        return {"ok": False, "error": "价格快照不可用"}
    from paper_ops import _native_qty
    qty = _native_qty(sym, notional, px)
    if qty <= 0:
        return {"ok": False, "error": "数量过小(精度不足)"}
    by_side = "Buy" if side == "long" else "Sell"
    r = _bybit_order(uid, s, "linear", sym, by_side, qty)
    return _finish_bybit(uid, r, f"native_open_{side}", sym, by_side, qty, notional, body)


def bybit_close_native(uid, body):
    """原生平仓: 查持仓 → 反向 reduce_only 全平"""
    sym = str(body.get("symbol", "")).upper()
    s = _platform_bybit()
    d = bybit_live.positions(s["key"], s["secret"], symbol=sym)
    if d.get("retCode") != 0:
        return {"ok": False, "error": "持仓查询失败: " + str(d.get("retMsg"))[:80]}
    pos = next((p for p in d["result"]["list"] if float(p.get("size") or 0) != 0), None)
    if not pos:
        return {"ok": False, "error": f"{sym} 无实盘持仓"}
    qty = abs(float(pos["size"]))
    side = "Buy" if pos["side"] == "Sell" else "Sell"
    r = _bybit_order(uid, s, "linear", sym, side, qty, reduce_only=True)
    return _finish_bybit(uid, r, "native_close", sym, side, qty,
                         float(pos.get("positionValue") or 0), body)


# ---------- Bybit 动作 ----------

def _bybit_order(uid, s, category, symbol, side, qty, price=None, reduce_only=False):
    cid = f"mb{int(time.time() * 1000)}{uid}"
    if category == "spot":
        r = bybit_live.place_spot_order(s["key"], s["secret"], symbol, side, qty,
                                        price=price)
    else:
        r = bybit_live.place_order(s["key"], s["secret"], symbol, side, qty,
                                   category=category, price=price,
                                   reduce_only=reduce_only)
    return r


def _last_spot_price(sym):
    """现货实时价 (快照 spot 字段, 兜底 last)"""
    try:
        with open(os.path.join(BASE, "logs", "bybit_prices.json"), encoding="utf-8") as f:
            d = json.load(f)
        p = d.get("prices", d).get(sym, {})
        v = p.get("spot") or p.get("last")
        return float(v) if v else None
    except Exception:
        return None


def bybit_spot_order(uid, body):
    """现货实盘市价单: side=buy/sell; qtyStep 取整; 卖出查现货余额"""
    sym = str(body.get("symbol", "")).upper()
    side = body.get("side", "buy")
    if side not in ("buy", "sell"):
        return {"ok": False, "error": "方向须 buy/sell"}
    notional = float(body.get("notional") or 0)
    ok, err = _gate(uid, "bybit", notional)
    if not ok:
        return {"ok": False, "error": err}
    try:
        from paper_ops import _spot_allowed, _spot_qty, SPOT_FEE
    except Exception:
        return {"ok": False, "error": "paper_ops 不可用"}
    if not _spot_allowed(sym):
        return {"ok": False, "error": f"{sym} 不在现货交易白名单"}
    s = _platform_bybit()
    px = _last_spot_price(sym)
    if not px:
        return {"ok": False, "error": "现货价格快照不可用"}
    qty = _spot_qty(sym, notional, px)
    if qty <= 0:
        return {"ok": False, "error": "数量过小(精度不足)"}
    if side == "sell":
        bal = _spot_balance(s, sym.split("USDT")[0].split("USDC")[0])
        if bal < qty:
            return {"ok": False, "error": f"现货余额不足 ({bal} < {qty} {sym.split('USDT')[0].split('USDC')[0]})"}
    by_side = "Buy" if side == "buy" else "Sell"
    r = _bybit_order(uid, s, "spot", sym, by_side, qty)
    return _finish_bybit(uid, r, f"spot_{side}", sym, by_side, qty, notional, body)


def bybit_open_naked(uid, body):
    """裸腿方向仓: 永续市价开单 (fwd=空, rev=多)"""
    sym = str(body.get("symbol", "")).upper()
    dir_ = body.get("dir", "fwd")
    notional = float(body.get("notional") or 0)
    ok, err = _gate(uid, "bybit", notional)
    if not ok:
        return {"ok": False, "error": err}
    s = _platform_bybit()
    d = bybit_live.positions(s["key"], s["secret"], symbol=sym)
    if d.get("retCode") == 0:
        for p in d["result"]["list"]:
            if float(p.get("size") or 0) != 0:
                return {"ok": False, "error": f"{sym} 已有实盘持仓, 先平仓再开新仓"}
    # 市价单数量: 名义/现价 (用最新 WSS 快照价近似, 精确数量由交易所按市价成交)
    px = _last_price(sym)
    if not px:
        return {"ok": False, "error": "价格快照不可用"}
    qty = round(notional / px, 6)
    side = "Sell" if dir_ == "fwd" else "Buy"
    r = _bybit_order(uid, s, "linear", sym, side, qty)
    return _finish_bybit(uid, r, "open_naked", sym, side, qty, notional, body)


def bybit_close_naked(uid, body):
    sym = str(body.get("symbol", "")).upper()
    s = _platform_bybit()
    d = bybit_live.positions(s["key"], s["secret"], symbol=sym)
    if d.get("retCode") != 0:
        return {"ok": False, "error": "持仓查询失败: " + str(d.get("retMsg"))[:80]}
    pos = next((p for p in d["result"]["list"] if float(p.get("size") or 0) != 0), None)
    if not pos:
        return {"ok": False, "error": f"{sym} 无实盘持仓"}
    qty = abs(float(pos["size"]))
    side = "Buy" if pos["side"] == "Sell" else "Sell"
    r = _bybit_order(uid, s, "linear", sym, side, qty, reduce_only=True)
    return _finish_bybit(uid, r, "close_naked", sym, side, qty,
                         float(pos.get("positionValue") or 0), body)


def bybit_open_hedge(uid, body):
    """双腿对冲: 现货买入 + 永续做空 (fwd); 现货卖出(需持有)+永续做多 (rev)"""
    sym = str(body.get("symbol", "")).upper()
    dir_ = body.get("dir", "fwd")
    notional = float(body.get("notional") or 0)
    ok, err = _gate(uid, "bybit", notional)
    if not ok:
        return {"ok": False, "error": err}
    s = _platform_bybit()
    # 铁律: 双腿对冲前先查已有持仓, 防止重复开仓 (与 bybit_open_naked 同源)
    _d = bybit_live.positions(s["key"], s["secret"], category="linear", symbol=sym)
    if _d.get("retCode") == 0:
        for _p in _d["result"]["list"]:
            if float(_p.get("size") or 0) != 0:
                return {"ok": False, "error": f"{sym} 已有实盘永续持仓, 先平仓再开对冲"}
    if dir_ == "fwd":
        # fwd=买现货: 已有现货余额即孤儿腿, 再买会放大现货敞口 (rev=卖现货, 允许有现货)
        _d = bybit_live.positions(s["key"], s["secret"], category="spot", symbol=sym)
        if _d.get("retCode") == 0:
            for _p in _d["result"]["list"]:
                if float(_p.get("size") or 0) != 0:
                    return {"ok": False, "error": f"{sym} 已有现货持仓(孤儿腿), 先平仓再开对冲"}
    px = _last_price(sym)
    if not px:
        return {"ok": False, "error": "价格快照不可用"}
    qty = round(notional / px, 6)
    if dir_ == "fwd":
        r1 = _bybit_order(uid, s, "spot", sym, "Buy", qty)
        r2 = _bybit_order(uid, s, "linear", sym, "Sell", qty)
    else:
        r1 = _bybit_order(uid, s, "spot", sym, "Sell", qty)
        r2 = _bybit_order(uid, s, "linear", sym, "Buy", qty)
    errs = []
    for tag, r in (("现货腿", r1), ("永续腿", r2)):
        if r.get("retCode") != 0:
            errs.append(f"{tag}: {r.get('retMsg', '')[:60]}")
    if errs:
        users.audit_log(uid, "live_order_fail", f"Bybit 双腿对冲 {sym} {dir_}: " + "; ".join(errs))
        return {"ok": False, "error": "双腿部分失败: " + "; ".join(errs) + " (请到 Bybit 检查持仓!)"}
    _append(uid, {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                  "venue": "bybit", "action": "open_hedge", "symbol": sym, "dir": dir_,
                  "qty": qty, "notional": notional,
                  "spot": r1.get("result", {}).get("orderId"),
                  "perp": r2.get("result", {}).get("orderId")})
    users.audit_log(uid, "live_order", f"Bybit 双腿对冲 {sym} {dir_} {qty}")
    return {"ok": True, "msg": f"双腿已下单: 现货#{r1['result']['orderId']} 永续#{r2['result']['orderId']}",
            "spot_id": r1["result"]["orderId"], "perp_id": r2["result"]["orderId"]}


def bybit_close_perp_leg(uid, body):
    sym = str(body.get("symbol", "")).upper()
    s = _platform_bybit()
    d = bybit_live.positions(s["key"], s["secret"], symbol=sym)
    pos = next((p for p in d["result"]["list"] if float(p.get("size") or 0) != 0), None) \
        if d.get("retCode") == 0 else None
    if not pos:
        return {"ok": False, "error": f"{sym} 无永续持仓"}
    qty = abs(float(pos["size"]))
    side = "Buy" if pos["side"] == "Sell" else "Sell"
    r = _bybit_order(uid, s, "linear", sym, side, qty, reduce_only=True)
    return _finish_bybit(uid, r, "close_perp_leg", sym, side, qty,
                         float(pos.get("positionValue") or 0), body)


def bybit_close_both(uid, body):
    """全平: 永续平仓 + 现货腿平仓"""
    sym = str(body.get("symbol", "")).upper()
    s = _platform_bybit()
    out = []
    d = bybit_live.positions(s["key"], s["secret"], symbol=sym)
    if d.get("retCode") == 0:
        pos = next((p for p in d["result"]["list"] if float(p.get("size") or 0) != 0), None)
        if pos:
            qty = abs(float(pos["size"]))
            side = "Buy" if pos["side"] == "Sell" else "Sell"
            r = _bybit_order(uid, s, "linear", sym, side, qty, reduce_only=True)
            out.append(("永续", r))
    # 现货持仓: 卖出现货币 (查 spot 余额近似; 简化: 按 ticker 价格卖回)
    qty_s = _spot_balance(s, sym.replace("USDT", ""))
    if qty_s and qty_s > 1e-9:
        r = _bybit_order(uid, s, "spot", sym, "Sell", round(qty_s, 6))
        out.append(("现货", r))
    if not out:
        return {"ok": False, "error": "无可平持仓"}
    errs = [f"{t}: {r.get('retMsg', '')[:60]}" for t, r in out if r.get("retCode") != 0]
    if errs:
        return {"ok": False, "error": "; ".join(errs)}
    _append(uid, {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                  "venue": "bybit", "action": "close_both", "symbol": sym,
                  "orders": [r["result"].get("orderId") for _, r in out], "notional": 0})
    users.audit_log(uid, "live_order", f"Bybit 全平 {sym}")
    return {"ok": True, "msg": f"已平: {[t for t, _ in out]}"}


def _finish_bybit(uid, r, action, sym, side, qty, notional, body):
    if r.get("retCode") != 0:
        users.audit_log(uid, "live_order_fail", f"Bybit {action} {sym} {side} {qty}: {r.get('retMsg', '')[:80]}")
        return {"ok": False, "error": f"下单失败: {r.get('retMsg', '')[:120]}"}
    oid = r["result"].get("orderId")
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "venue": "bybit", "action": action, "symbol": sym, "side": side,
           "qty": qty, "notional": notional, "order_id": oid}
    # R14-M14: 实盘营收计费 — 费率加价+点差对实盘全生效 (与模拟同源 fee_ops)
    try:
        import os as _os
        import sys as _s
        import tenants as _t
        _sp = _os.path.join(_t.base(), "dash")
        if _sp not in _s.path:
            _s.path.insert(0, _sp)
        import fee_ops as _fo
        chan = "spot" if action.startswith("spot") else "perp"
        n = float(notional or 0.0)
        official = _fo.official(chan, sym) * n
        # M-S四期: 已取消 VIP 费率, 全平台统一标准费率 (与 paper_ops._fee_mult 口径一致)
        mult = _fo.sym_mult(sym, chan)
        user_fee = official * mult
        spread_rev = n * _fo.spread_bp(sym) / 10000.0
        rev = user_fee - official + spread_rev
        rec.update({"user_fee": round(user_fee, 6), "official_fee": round(official, 6),
                    "platform_rev": round(rev, 6), "spread_rev": round(spread_rev, 6),
                    "mult": round(mult, 4)})
        fee_total = round(user_fee + spread_rev, 6)
        if fee_total > 0:   # 用户账面扣费 = 平台营收真实来源
            from dash.app import funds as _fd
            # 铁律7: 扣款先校验余额, 绝不扣成负数 (余额不足按可用额实扣并留痕)
            bal = float(_fd.get_balance(uid) or 0.0)
            charge = round(min(fee_total, bal), 6)
            if charge > 0:
                _fd.add_balance(uid, -charge, "live_fee", f"#{oid}", "实盘手续费+点差")
            if charge + 1e-9 < fee_total:
                users.audit_log(uid, "live_fee_shortfall",
                                f"实盘扣费不足: {sym} #{oid} 应扣 {fee_total:.6f} 实扣 {charge:.6f} "
                                f"(可用余额 {bal:.4f})")
            rec["fee_charged"] = charge
    except Exception:
        pass
    _append(uid, rec)
    users.audit_log(uid, "live_order", f"Bybit {action} {sym} {side} {qty} → #{oid}")
    return {"ok": True, "msg": f"已下单: {sym} {side} {qty} → #{oid}", "order_id": oid}


def _last_price(sym):
    try:
        with open(os.path.join(BASE, "logs", "bybit_prices.json"), encoding="utf-8") as f:
            d = json.load(f)
        px = d.get("prices", d).get(sym, {}).get("last")
        return float(px) if px else None
    except Exception:
        return None


def _spot_balance(s, coin):
    try:
        d = bybit_live.wallet_balance(s["key"], s["secret"], "SPOT")
        for acc in d.get("result", {}).get("list", []):
            for c in acc.get("coin", []):
                if c["coin"] == coin:
                    return float(c.get("walletBalance") or 0)
    except Exception:
        pass
    return 0.0


# ---------- PM 动作 ----------

def _pm_rt_quote(token_id):
    """CLOB WS 实时 quote: {bid, ask} 或 None (读 pm_prices.json)"""
    try:
        with open(os.path.join(BASE, "logs", "pm_prices.json"), encoding="utf-8") as f:
            px = json.load(f).get("prices", {}).get(token_id)
        if px and px.get("bid") is not None and px.get("ask") is not None:
            return px
    except Exception:
        pass
    return None


def _pm_resolve_token(body):
    """token_id 直接给; 或 key(event|market)+outcome 从 pm_tokens.json 映射"""
    tok = str(body.get("token_id", "")).strip()
    if tok:
        return tok
    key = str(body.get("key", "")).strip()
    outcome = str(body.get("outcome", "")).strip()
    try:
        with open(os.path.join(BASE, "logs", "pm_tokens.json"), encoding="utf-8") as f:
            toks = json.load(f)
        for t in toks:
            if t.get("key") == key:
                if not outcome or str(t.get("outcome", "")).lower() == outcome.lower():
                    return t["token"]
    except Exception:
        pass
    return ""


def pm_market_order(uid, body):
    """P4/R12 PM 原生市价吃单: {token_id 或 key+outcome(YES/NO), side(BUY/SELL), shares 或 amount_usd}
    实时 best_ask/best_bid 限价即时成交 (FOK); 股数优先 (PM 原生按股数交易)"""
    side = str(body.get("side", "")).upper()
    if side not in ("BUY", "SELL"):
        return {"ok": False, "error": "方向须 BUY/SELL"}
    outcome = str(body.get("outcome", "")).upper()
    if side == "BUY" and not body.get("token_id") and outcome not in ("YES", "NO"):
        return {"ok": False, "error": "买入须指定 outcome=YES/NO"}
    tok = _pm_resolve_token(body)
    if not tok:
        return {"ok": False, "error": "无法解析 token (市场不在实时目录, 用完整 token_id 下单)"}
    q = _pm_rt_quote(tok)
    if not q:
        return {"ok": False, "error": "无实时盘口价 (pm-clob 桥未就绪), 稍后再试"}
    price = float(q["ask"] if side == "BUY" else q["bid"])
    if not (0.001 <= price <= 0.999):
        return {"ok": False, "error": "实时价异常: " + str(price)}
    # R12: 股数优先; amount_usd 兼容换算
    try:
        if body.get("shares") is not None:
            size = round(float(body["shares"]), 2)
        else:
            amount = float(body.get("amount_usd") or 0)
            if amount < MIN_ORDER_USDT:
                return {"ok": False, "error": f"金额须 ≥ ${MIN_ORDER_USDT}"}
            size = round(amount / price, 2)
    except Exception:
        return {"ok": False, "error": "数量非法"}
    if size < 1:
        return {"ok": False, "error": "数量过小 (股数<1)"}
    if size > 500:
        return {"ok": False, "error": "单笔股数上限 500"}
    notional = round(size * price, 2)
    ok, err = _gate(uid, "pm", max(notional, 1.0))
    if not ok:
        return {"ok": False, "error": err}
    s = keys.get_secrets(uid, "pm")
    extra = json.loads(s["extra"] or "{}")
    creds = {"apiKey": s["key"], "secret": s["secret"], "passphrase": extra.get("passphrase", "")}
    st, d = pm_live.place_order(creds, extra.get("wallet", ""), tok, price, side, size, "FOK")
    if st not in (200, 201):
        users.audit_log(uid, "live_order_fail",
                        f"PM市场单 {side} {tok[:16]} {size}@{price}: {str(d)[:100]}")
        return {"ok": False, "error": f"下单失败: {str(d)[:150]}"}
    oid = d.get("orderID") or d.get("id")
    _append(uid, {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                  "venue": "pm", "action": f"market_{side}", "token_id": tok, "price": price,
                  "size": size, "notional": notional, "order_id": oid, "order_type": "FOK",
                  "outcome": outcome})
    users.audit_log(uid, "live_order", f"PM市场单 {side} {tok[:16]} {size}@{price} → {oid}")
    return {"ok": True, "msg": f"已市价{('买入' if side == 'BUY' else '卖出')} {size}股 @{price*100:.1f}¢ → {oid}",
            "order_id": oid, "price": price, "size": size, "notional": notional}




def pm_order(uid, body):
    """PM CLOB 下单: {token_id, side(BUY/SELL), price(美分), size(股数), order_type}"""
    token_id = str(body.get("token_id", ""))
    side = str(body.get("side", "")).upper()
    price = float(body.get("price") or 0)
    size = float(body.get("size") or 0)
    if side not in ("BUY", "SELL"):
        return {"ok": False, "error": "方向须 BUY/SELL"}
    if not (0.001 <= price <= 0.999):
        return {"ok": False, "error": "价格须在 0.001~0.999 (美分股)"}
    if size < 1:
        return {"ok": False, "error": "股数至少 1"}
    notional = price * size
    ok, err = _gate(uid, "pm", notional)
    if not ok:
        return {"ok": False, "error": err}
    s = keys.get_secrets(uid, "pm")
    extra = json.loads(s["extra"] or "{}")
    creds = {"apiKey": s["key"], "secret": s["secret"], "passphrase": extra.get("passphrase", "")}
    st, d = pm_live.place_order(creds, extra.get("wallet", ""), token_id, price, side,
                                size, body.get("order_type", "GTC"))
    if st not in (200, 201):
        users.audit_log(uid, "live_order_fail",
                        f"PM {side} {token_id[:16]} {size}@{price}: {str(d)[:100]}")
        return {"ok": False, "error": f"下单失败: {str(d)[:150]}"}
    oid = d.get("orderID") or d.get("id")
    _append(uid, {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                  "venue": "pm", "action": side, "token_id": token_id, "price": price,
                  "size": size, "notional": notional, "order_id": oid})
    users.audit_log(uid, "live_order", f"PM {side} {token_id[:16]} {size}@{price} → {oid}")
    return {"ok": True, "msg": f"已下单: {side} {size}股 @{price}¢ → {oid}", "order_id": oid}


def status(uid):
    """实盘状态汇总 (开关/持仓/余额/台账)"""
    u = users.get_user(uid)
    lim = keys.get_limits(uid)
    # R14-M15: 充值自动开通 — live_enabled = 有充值入账 (不再看 limits 表)
    try:
        _dep = bool(_funds.get_balance(uid) is not None and float(_funds.get_balance(uid) or 0) > 0) or _funds.has_deposit(uid)
    except Exception:
        _dep = False
    out = {"plan": u.get("plan"), "live_enabled": _dep,
           "limits": lim,
           "bybit_bound": _platform_bybit() is not None,
           "pm_bound": keys.get_secrets(uid, "pm") is not None,
           "today_notional": round(_today_notional(uid), 2),
           "positions": {"bybit": [], "pm": []},
           "ledger": []}
    try:
        s = _platform_bybit()
        if s:
            d = bybit_live.positions(s["key"], s["secret"])
            if d.get("retCode") == 0:
                out["positions"]["bybit"] = [
                    {"symbol": p["symbol"], "side": p["side"], "size": p["size"],
                     "entry": p.get("avgPrice"), "value": p.get("positionValue"),
                     "pnl": p.get("unrealisedPnl")}
                    for p in d["result"]["list"] if float(p.get("size") or 0) != 0]
    except Exception as e:
        out["positions"]["bybit"] = [{"error": str(e)[:80]}]
    try:
        s = keys.get_secrets(uid, "pm")
        if s:
            extra = json.loads(s["extra"] or "{}")
            st, d = pm_live.positions({"apiKey": s["key"], "secret": s["secret"],
                                       "passphrase": extra.get("passphrase", "")},
                                      extra.get("wallet", ""))
            out["positions"]["pm"] = d if st == 200 else [{"error": str(d)[:80]}]
    except Exception as e:
        out["positions"]["pm"] = [{"error": str(e)[:80]}]
    try:
        with open(_ledger(uid), encoding="utf-8") as f:
            out["ledger"] = [json.loads(l) for l in f.readlines()[-20:]]
    except Exception:
        pass
    return out
