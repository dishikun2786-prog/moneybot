#!/usr/bin/env python3
"""手动纸面交易操作模块 (API 与引擎共用) v1
- fcntl 文件锁串行化状态读写 (手动操作 vs 引擎 tick)
- 动作: open_hedge / close_perp_leg / close_both / close_spot_to_naked / close_naked /
        edit_naked_tpsl / close_pm
- 价格: bybit_prices.json 实时快照 (永续+现货), 回退 carry_1m 最新行; PM 用引擎同源盘口快照
- 审计: logs/manual_actions.jsonl
- 全部纸面交易, 不涉真实资金"""
import json
import os
import time

try:
    import fcntl
except ImportError:  # Windows 本地测试环境
    fcntl = None

import tenants

BASE = tenants.ROOT  # 保留: 兼容旧引用/测试 (实际路径一律走 __getattr__ 租户解析)

# 租户感知路径 (PEP 562 动态属性; 测试可 setattr monkeypatch 覆盖)
def _resolve(name):
    """内部路径解析: 测试 setattr monkeypatch 优先, 否则租户动态解析"""
    if name in globals():
        return globals()[name]
    return _DYN[name]()

_DYN = {
    "CARRY_STATE": lambda: tenants.state("carry"),
    "PAPER_STATE": lambda: tenants.state("paper"),
    "CARRY_TRADES": lambda: tenants.trades("carry"),
    "PAPER_TRADES": lambda: tenants.trades("paper"),
    "AUDIT": lambda: tenants.audit_manual(),
    "SNAP": lambda: tenants.shared_log("bybit_prices.json"),
    "INSTR": lambda: tenants.shared_log("bybit_instruments.json"),
    "PM_TOKENS": lambda: tenants.shared_log("pm_tokens.json"),
    "PM_PX": lambda: tenants.shared_log("pm_prices.json"),
    "CARRY_LOG": lambda: tenants.shared_log("carry_1m.jsonl"),
    "LOCK_PATH": lambda: tenants.lock_path(),
}


def __getattr__(name):
    f = _DYN.get(name)
    if f:
        return f()
    raise AttributeError(f"module 'paper_ops' has no attribute '{name}'")

FEE_SPOT = 0.001
FEE_PERP = 0.00055
MAX_NAKED = 2
MAX_NAKED_NOTIONAL = 50.0


def _lock():
    os.makedirs(os.path.dirname(_resolve("LOCK_PATH")), exist_ok=True)
    f = open(_resolve("LOCK_PATH"), "w")
    if fcntl:
        fcntl.flock(f, fcntl.LOCK_EX)
    return f


def _unlock(f):
    try:
        if fcntl:
            fcntl.flock(f, fcntl.LOCK_UN)
    finally:
        f.close()


def _read(p, default):
    try:
        return json.load(open(p))
    except Exception:
        return default


def _write(p, obj):
    tmp = p + ".tmp"
    json.dump(obj, open(tmp, "w"), ensure_ascii=False, indent=1)
    os.replace(tmp, p)


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _audit(action, sym, detail):
    rec = {"ts": _now(), "action": action, "symbol": sym}
    rec.update(detail or {})
    with open(_resolve("AUDIT"), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _log_trade(path, rec):
    rec.setdefault("ts", _now())
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _prices():
    """{sym: {"perp": float, "spot": float|None}}"""
    out = {}
    try:
        snap = json.load(open(_resolve("SNAP")))
        for sym, p in snap.get("prices", {}).items():
            if p.get("last"):
                out[sym] = {"perp": float(p["last"]),
                            "spot": float(p["spot"]) if p.get("spot") else None}
    except Exception:
        pass
    if not out:
        try:
            rows = [json.loads(l) for l in open(_resolve("CARRY_LOG")) if l.strip()]
            last_ts = rows[-1]["ts"]
            for r in rows:
                if r["ts"] == last_ts:
                    out.setdefault(r["symbol"], {"perp": r["perp_last"], "spot": r.get("spot")})
        except Exception:
            pass
    return out


def _latest_carry_row(sym):
    try:
        rows = [json.loads(l) for l in open(_resolve("CARRY_LOG")) if l.strip()]
        for r in reversed(rows):
            if r.get("symbol") == sym:
                return r
    except Exception:
        pass
    return None


# ================= 原生单边交易 (P3: 像 Bybit APP 一样直接做多/做空) =================

NATIVE_MAX_POS = 10          # 原生仓位上限(标的数)
NATIVE_WHITELIST_RANK = int(os.environ.get("NATIVE_WHITELIST_RANK", "50"))  # 成交额 Top N 可交易
NATIVE_MIN_TURNOVER = 5e6    # 24h 成交额下限 ($5M), 防低流动性滑点
NATIVE_FEE = 0.0005          # 永续单边费率 5bp (taker 近似)


def _instruments_linear():
    """标的目录缓存 (symbol → {tickSize, qtyStep, turnover24h})"""
    try:
        with open(_resolve("INSTR"), encoding="utf-8") as f:
            d = json.load(f)
        return {r["symbol"]: r for r in d.get("linear", [])}
    except Exception:
        return {}


def _native_allowed(sym):
    """原生交易白名单: BTC/ETH 恒可 + 24h成交额 Top N 且 ≥ $5M"""
    if sym in ("BTCUSDT", "ETHUSDT"):
        return True
    inst = _instruments_linear()
    it = inst.get(sym)
    if not it:
        return False
    if float(it.get("turnover24h") or 0) < NATIVE_MIN_TURNOVER:
        return False
    rank = 0
    for r in sorted(inst.values(), key=lambda r: -float(r.get("turnover24h") or 0)):
        rank += 1
        if r["symbol"] == sym:
            break
        if rank > NATIVE_WHITELIST_RANK:
            return False
    return rank <= NATIVE_WHITELIST_RANK


def _native_qty(sym, notional, px):
    """按 qtyStep 取整数量"""
    it = _instruments_linear().get(sym) or {}
    step = float(it.get("qtyStep") or 0.0001)
    qty = float(notional) / float(px)
    return round(qty / step) * step


def open_native(sym, side, notional):
    """原生单边纸面开仓: side=long(做多)/short(做空), 市价近似"""
    sym = (sym or "").upper()
    if side not in ("long", "short"):
        return {"ok": False, "error": "方向须 long/short"}
    try:
        notional = float(notional)
    except Exception:
        return {"ok": False, "error": "名义非法"}
    if not (1 <= notional <= 200):
        return {"ok": False, "error": "名义须1-200$"}
    if not _native_allowed(sym):
        return {"ok": False, "error": f"{sym} 不在原生交易白名单 (24h成交额 Top{NATIVE_WHITELIST_RANK} 或流动性不足)"}
    px = _prices().get(sym)
    if not px:
        return {"ok": False, "error": f"{sym} 无实时价"}
    entry = px["perp"]
    qty = _native_qty(sym, notional, entry)
    if qty <= 0:
        return {"ok": False, "error": "数量过小(精度不足), 请加大名义"}
    f = _lock()
    try:
        st = _read(_resolve("CARRY_STATE"), {})
        if sym in st.get("positions", {}) or sym in st.get("orphans", {}) or sym in st.get("naked", {}):
            return {"ok": False, "error": f"{sym} 已有对冲/裸腿持仓, 请先平仓 (原生与套利仓位互斥)"}
        nat = st.get("native", {})
        if sym in nat:
            return {"ok": False, "error": f"{sym} 已有原生持仓"}
        if len(nat) >= NATIVE_MAX_POS:
            return {"ok": False, "error": f"原生仓位已达上限{NATIVE_MAX_POS}"}
        fees = NATIVE_FEE * notional
        st["day_pnl"] = round(st.get("day_pnl", 0.0) - fees, 4)
        st.setdefault("native", {})[sym] = dict(side=side, entry=entry, notional=notional,
                                                qty=qty, t0=time.time())
        _write(_resolve("CARRY_STATE"), st)
        _log_trade(_resolve("CARRY_TRADES"), dict(symbol=sym, action="NATIVE_OPEN",
                                      side=side, entry=entry, notional=notional, qty=qty))
        _audit("open_native", sym, {"side": side, "entry": entry, "notional": notional})
        return {"ok": True, "msg": f"已{'做多' if side == 'long' else '做空'} {sym} @{entry:.4f} "
                                   f"(名义{notional}$, {qty}张)"}
    finally:
        _unlock(f)


def close_native(sym):
    """原生单边纸面平仓: 按当前价结算"""
    sym = (sym or "").upper()
    px = _prices().get(sym)
    if not px:
        return {"ok": False, "error": f"{sym} 无实时价"}
    f = _lock()
    try:
        st = _read(_resolve("CARRY_STATE"), {})
        nat = (st.get("native") or {}).get(sym)
        if not nat:
            return {"ok": False, "error": f"{sym} 无原生持仓"}
        entry, n, side = nat["entry"], nat.get("notional", 10.0), nat["side"]
        pnl = ((px["perp"] - entry) if side == "long" else (entry - px["perp"])) / entry * n
        fees = NATIVE_FEE * n
        st["day_pnl"] = round(st.get("day_pnl", 0.0) + pnl - fees, 4)
        del st["native"][sym]
        _write(_resolve("CARRY_STATE"), st)
        _log_trade(_resolve("CARRY_TRADES"), dict(symbol=sym, action="NATIVE_CLOSE",
                                      side=side, entry=entry, exit=px["perp"],
                                      pnl_usd=round(pnl, 4), fees=fees))
        _audit("close_native", sym, {"exit": px["perp"], "pnl": round(pnl, 4)})
        return {"ok": True, "msg": f"已平{'多' if side == 'long' else '空'}仓 {sym} @{px['perp']:.4f} "
                                   f"(PnL {pnl:+.4f}$)"}
    finally:
        _unlock(f)


# ============ R6 现货纸面交易 (buy 买入 / sell 卖出平仓) ============
SPOT_WHITELIST_RANK = int(os.environ.get("SPOT_WHITELIST_RANK", "80"))
SPOT_MIN_TURNOVER = 2e6    # 24h 成交额下限 ($2M)
SPOT_FEE = 0.001           # 现货单边费率 10bp


def _instruments_spot():
    try:
        with open(_resolve("INSTR"), encoding="utf-8") as f:
            d = json.load(f)
        return {r["symbol"]: r for r in d.get("spot", [])}
    except Exception:
        return {}


def _spot_px(sym):
    """现货实时价 (快照 spot 字段, 兜底 last)"""
    try:
        snap = json.load(open(_resolve("SNAP")))
        p = snap.get("prices", {}).get(sym) or {}
        v = p.get("spot") or p.get("last")
        return float(v) if v else None
    except Exception:
        return None


def _spot_allowed(sym):
    if sym in ("BTCUSDT", "ETHUSDT"):
        return True
    inst = _instruments_spot()
    it = inst.get(sym)
    if not it:
        return False
    if float(it.get("turnover24h") or 0) < SPOT_MIN_TURNOVER:
        return False
    rank = 0
    for r in sorted(inst.values(), key=lambda r: -float(r.get("turnover24h") or 0)):
        rank += 1
        if r["symbol"] == sym:
            break
        if rank > SPOT_WHITELIST_RANK:
            return False
    return rank <= SPOT_WHITELIST_RANK


def _spot_qty(sym, notional, px):
    it = _instruments_spot().get(sym) or {}
    step = float(it.get("qtyStep") or 0.0001)
    qty = float(notional) / float(px)
    return round(qty / step) * step


def open_spot(sym, side, notional):
    """现货纸面下单: side=buy(买入)/sell(卖出, 需持仓); 市价近似, 数量按 qtyStep 取整"""
    sym = (sym or "").upper()
    if side not in ("buy", "sell"):
        return {"ok": False, "error": "方向须 buy/sell"}
    try:
        notional = float(notional)
    except Exception:
        return {"ok": False, "error": "名义非法"}
    if not (1 <= notional <= 200):
        return {"ok": False, "error": "名义须1-200$"}
    if not _spot_allowed(sym):
        return {"ok": False, "error": f"{sym} 不在现货交易白名单 (24h成交额 Top{SPOT_WHITELIST_RANK} 或流动性不足)"}
    px = _spot_px(sym)
    if not px:
        return {"ok": False, "error": f"{sym} 无现货实时价"}
    qty = _spot_qty(sym, notional, px)
    if qty <= 0:
        return {"ok": False, "error": "数量过小(精度不足), 请加大名义"}
    f = _lock()
    try:
        st = _read(_resolve("CARRY_STATE"), {})
        spot = st.get("spot", {})
        held = spot.get(sym, {}).get("qty", 0.0)
        fees = SPOT_FEE * notional
        if side == "sell":
            if held < qty - 1e-9:
                return {"ok": False, "error": f"现货持仓不足 (持有 {held} < 卖出 {qty})"}
            avg = spot[sym]["avg_cost"]
            pnl = (px - avg) * qty
            new_qty = held - qty
            st["day_pnl"] = round(st.get("day_pnl", 0.0) + pnl - fees, 4)
            if new_qty <= 1e-9:
                del spot[sym]
            else:
                spot[sym] = dict(qty=new_qty, avg_cost=avg, t0=spot[sym]["t0"])
            _write(_resolve("CARRY_STATE"), st)
            _log_trade(_resolve("CARRY_TRADES"), dict(symbol=sym, action="SPOT_SELL",
                                          px=px, qty=qty, notional=notional, pnl_usd=round(pnl, 4)))
            _audit("open_spot_sell", sym, {"px": px, "qty": qty, "pnl": round(pnl, 4)})
            return {"ok": True, "msg": f"已卖出 {qty} {sym} @{px:.4f} (PnL {pnl:+.4f}$)"}
        # buy: 累加持仓
        old_qty, old_cost = held, spot.get(sym, {}).get("avg_cost", 0.0)
        new_qty = old_qty + qty
        avg = (old_cost * old_qty + px * qty) / new_qty if new_qty > 0 else px
        spot[sym] = dict(qty=new_qty, avg_cost=avg, t0=time.time())
        st["day_pnl"] = round(st.get("day_pnl", 0.0) - fees, 4)
        st["spot"] = spot
        _write(_resolve("CARRY_STATE"), st)
        _log_trade(_resolve("CARRY_TRADES"), dict(symbol=sym, action="SPOT_BUY",
                                      px=px, qty=qty, notional=notional))
        _audit("open_spot_buy", sym, {"px": px, "qty": qty, "notional": notional})
        return {"ok": True, "msg": f"已买入 {qty} {sym} @{px:.4f} (名义{notional}$)"}
    finally:
        _unlock(f)


def close_spot(sym):
    """现货全平: 按当前价卖出全部持仓"""
    sym = (sym or "").upper()
    px = _spot_px(sym)
    if not px:
        return {"ok": False, "error": f"{sym} 无现货实时价"}
    f = _lock()
    try:
        st = _read(_resolve("CARRY_STATE"), {})
        spot = (st.get("spot") or {}).get(sym)
        if not spot:
            return {"ok": False, "error": f"{sym} 无现货持仓"}
        qty, avg = spot["qty"], spot["avg_cost"]
        pnl = (px - avg) * qty
        fees = SPOT_FEE * px * qty
        st["day_pnl"] = round(st.get("day_pnl", 0.0) + pnl - fees, 4)
        del st["spot"][sym]
        _write(_resolve("CARRY_STATE"), st)
        _log_trade(_resolve("CARRY_TRADES"), dict(symbol=sym, action="SPOT_CLOSE",
                                      px=px, qty=qty, pnl_usd=round(pnl, 4)))
        _audit("close_spot", sym, {"px": px, "qty": qty, "pnl": round(pnl, 4)})
        return {"ok": True, "msg": f"已平 {sym} 现货 {qty} @{px:.4f} (PnL {pnl:+.4f}$)"}
    finally:
        _unlock(f)


def spot_positions():
    """现货持仓 + MTM → [{symbol, qty, avg_cost, px, value, pnl}]"""
    px_all = _prices()
    st = _read(_resolve("CARRY_STATE"), {})
    spot = st.get("spot", {})
    out = []
    for sym, p in spot.items():
        px = _spot_px(sym)
        if not px:
            continue
        pnl = (px - p["avg_cost"]) * p["qty"]
        out.append(dict(symbol=sym, qty=p["qty"], avg_cost=round(p["avg_cost"], 6),
                        px=px, value=round(px * p["qty"], 2), pnl=round(pnl, 4)))
    return out


def native_positions():
    """原生纸面持仓 + 实时 MTM → [{symbol, side, entry, notional, qty, pnl}]"""
    px_all = _prices()
    try:
        st = _read(_resolve("CARRY_STATE"), {})
    except Exception:
        st = {}
    out = []
    for sym, nat in (st.get("native") or {}).items():
        px = px_all.get(sym, {})
        cur = px.get("perp")
        entry = nat.get("entry")
        pnl = None
        if cur and entry:
            pnl = ((cur - entry) if nat["side"] == "long" else (entry - cur)) / entry * nat.get("notional", 10.0)
            pnl = round(pnl - NATIVE_FEE * nat.get("notional", 10.0), 4)
        out.append({"symbol": sym, "side": nat["side"], "entry": entry,
                    "notional": nat.get("notional"), "qty": nat.get("qty"),
                    "last": cur, "pnl": pnl, "t0": nat.get("t0")})
    return out


# ================= 执行动作 (每个动作: 锁→校验→执行→审计) =================

def open_hedge(sym, notional, dir_="fwd"):
    """一键对冲: 实时价双腿纸面开仓 (fwd=多现货+空永续; rev=空现货+多永续)"""
    if dir_ not in ("fwd", "rev"):
        return {"ok": False, "error": "方向须 fwd/rev"}
    try:
        notional = float(notional)
    except Exception:
        return {"ok": False, "error": "名义非法"}
    if not (1 <= notional <= 50):
        return {"ok": False, "error": "名义须1-50$"}
    px = _prices().get(sym)
    if not px or not px["spot"]:
        return {"ok": False, "error": f"{sym} 无实时价(现货通道未就绪), 拒绝"}
    row = _latest_carry_row(sym)
    f = _lock()
    try:
        st = _read(_resolve("CARRY_STATE"), {"positions": {}, "orphans": {}, "naked": {},
                                 "day_pnl": 0.0, "cum_pnl": 0.0, "n_rounds": 0})
        if sym in st.get("positions", {}) or sym in st.get("orphans", {}) or sym in st.get("naked", {}):
            return {"ok": False, "error": f"{sym} 已有持仓/孤儿/裸腿, 先平仓"}
        fees = (FEE_SPOT + FEE_PERP) * notional
        if dir_ == "rev":
            fees += 0.05 / 365 / 24 * notional  # 空现货借贷成本(5%年化, 预扣1小时)
        st.setdefault("day_pnl", 0.0)
        st["day_pnl"] = round(st["day_pnl"] - fees, 4)
        st.setdefault("positions", {})[sym] = dict(
            spot_entry=px["spot"], perp_entry=px["perp"], t0=time.time(),
            funding_acc=0.0, next_funding_ts=int((row or {}).get("next_funding_ts", 0)),
            last_fr=(row or {}).get("funding_rate", 0.0), reused=False,
            notional=notional, dir=dir_)
        _write(_resolve("CARRY_STATE"), st)
        _log_trade(_resolve("CARRY_TRADES"), dict(symbol=sym, action="MANUAL_OPEN_HEDGE",
                                      spot_entry=px["spot"], perp_entry=px["perp"],
                                      notional=notional, dir=dir_))
        _audit("open_hedge", sym, {"spot": px["spot"], "perp": px["perp"], "notional": notional,
                                   "dir": dir_, "fees": fees})
        legs = "多现货+空永续" if dir_ == "fwd" else "空现货+多永续"
        return {"ok": True, "msg": f"已开仓 {sym} {'正向' if dir_ == 'fwd' else '反向'}({legs}) "
                                   f"现货@{px['spot']:.1f} 永续@{px['perp']:.1f} (名义{notional}$)"}
    finally:
        _unlock(f)


def close_orphan(sym):
    """平孤儿现货腿 (单边平仓后的遗留现货敞口)"""
    px = _prices().get(sym)
    if not px or not px["spot"]:
        return {"ok": False, "error": f"{sym} 无实时价(现货价缺失)"}
    f = _lock()
    try:
        st = _read(_resolve("CARRY_STATE"), {})
        orph = (st.get("orphans") or {}).get(sym)
        if not orph:
            return {"ok": False, "error": f"{sym} 无孤儿现货腿"}
        n = orph.get("notional", 10.0)
        d = orph.get("dir", "fwd")
        pnl = ((px["spot"] - orph["spot_entry"]) if d == "fwd" else
               (orph["spot_entry"] - px["spot"])) / orph["spot_entry"] * n - FEE_SPOT * n
        st["day_pnl"] = round(st.get("day_pnl", 0.0) + pnl, 4)
        st.setdefault("n_rounds", 0)
        st["n_rounds"] += 1
        del st["orphans"][sym]
        _write(_resolve("CARRY_STATE"), st)
        _log_trade(_resolve("CARRY_TRADES"), dict(symbol=sym, action="MANUAL_CLOSE_ORPHAN",
                                      spot_entry=orph["spot_entry"], spot_exit=px["spot"],
                                      pnl_usd=round(pnl, 3), notional=n))
        _audit("close_orphan", sym, {"spot_exit": px["spot"], "pnl": round(pnl, 3)})
        return {"ok": True, "msg": f"已平孤儿现货腿 {sym} @{px['spot']:.1f} (PnL {pnl:+.3f}$)"}
    finally:
        _unlock(f)


def close_perp_leg(sym):
    """平合约腿: 现货腿转孤儿"""
    px = _prices().get(sym)
    if not px:
        return {"ok": False, "error": f"{sym} 无实时价"}
    f = _lock()
    try:
        st = _read(_resolve("CARRY_STATE"), {})
        pos = (st.get("positions") or {}).get(sym)
        if not pos:
            return {"ok": False, "error": f"{sym} 无双腿持仓"}
        d = pos.get("dir", "fwd")
        n = pos.get("notional", 10.0)
        perp_pnl = ((pos["perp_entry"] - px["perp"]) if d == "fwd" else
                    (px["perp"] - pos["perp_entry"])) / pos["perp_entry"] * n
        fees = FEE_PERP * n
        st["day_pnl"] = round(st.get("day_pnl", 0.0) + perp_pnl - fees, 4)
        st.setdefault("orphans", {})[sym] = dict(spot_entry=pos["spot_entry"], t0=pos["t0"],
                                                 next_funding_ts=pos.get("next_funding_ts", 0),
                                                 notional=n, dir=d)
        del st["positions"][sym]
        _write(_resolve("CARRY_STATE"), st)
        _log_trade(_resolve("CARRY_TRADES"), dict(symbol=sym, action="MANUAL_CLOSE_PERP_LEG",
                                      perp_entry=pos["perp_entry"], perp_exit=px["perp"], dir=d,
                                      perp_pnl_usd=round(perp_pnl, 3), funding_acc=pos.get("funding_acc", 0)))
        _audit("close_perp_leg", sym, {"perp_exit": px["perp"], "perp_pnl": round(perp_pnl, 3), "dir": d})
        return {"ok": True, "msg": f"已平合约腿 {sym} @{px['perp']:.1f} (合约腿PnL {perp_pnl:+.3f}$, 现货腿转孤儿)"}
    finally:
        _unlock(f)


def close_both(sym):
    """全平双腿"""
    px = _prices().get(sym)
    if not px or not px["spot"]:
        return {"ok": False, "error": f"{sym} 无实时价(现货价缺失)"}
    f = _lock()
    try:
        st = _read(_resolve("CARRY_STATE"), {})
        pos = (st.get("positions") or {}).get(sym)
        if not pos:
            return {"ok": False, "error": f"{sym} 无双腿持仓"}
        n = pos.get("notional", 10.0)
        d = pos.get("dir", "fwd")
        spot_pnl = ((px["spot"] - pos["spot_entry"]) if d == "fwd" else
                    (pos["spot_entry"] - px["spot"])) / pos["spot_entry"] * n
        perp_pnl = ((pos["perp_entry"] - px["perp"]) if d == "fwd" else
                    (px["perp"] - pos["perp_entry"])) / pos["perp_entry"] * n
        fees = (FEE_SPOT + FEE_PERP) * n
        total = spot_pnl + perp_pnl - fees
        st["day_pnl"] = round(st.get("day_pnl", 0.0) + total, 4)
        st.setdefault("n_rounds", 0)
        st["n_rounds"] += 1
        del st["positions"][sym]
        _write(_resolve("CARRY_STATE"), st)
        _log_trade(_resolve("CARRY_TRADES"), dict(symbol=sym, action="MANUAL_CLOSE_BOTH",
                                      spot_entry=pos["spot_entry"], spot_exit=px["spot"], dir=d,
                                      perp_entry=pos["perp_entry"], perp_exit=px["perp"],
                                      pnl_usd=round(total, 3), funding_acc=pos.get("funding_acc", 0)))
        _audit("close_both", sym, {"pnl": round(total, 3), "dir": d})
        return {"ok": True, "msg": f"已全平 {sym} (PnL {total:+.3f}$, 含funding累计{pos.get('funding_acc', 0):+.3f}$)"}
    finally:
        _unlock(f)


def close_spot_to_naked(sym, tp, sl):
    """只平现货腿 → 合约腿转裸腿 (强制止盈止损, 空头: tp<入场<sl)"""
    try:
        tp, sl = float(tp), float(sl)
    except Exception:
        return {"ok": False, "error": "止盈/止损数值非法"}
    px = _prices().get(sym)
    if not px or not px["spot"]:
        return {"ok": False, "error": f"{sym} 无实时价(现货价缺失)"}
    f = _lock()
    try:
        st = _read(_resolve("CARRY_STATE"), {})
        pos = (st.get("positions") or {}).get(sym)
        if not pos:
            return {"ok": False, "error": f"{sym} 无双腿持仓"}
        entry = pos["perp_entry"]
        d = pos.get("dir", "fwd")
        # fwd裸腿=空永续: tp<entry<sl; rev裸腿=多永续: sl<entry<tp
        if d == "fwd" and not (tp < entry < sl):
            return {"ok": False, "error": f"方向错误: 空头止盈须<入场价{entry}, 止损须>入场价{entry} (收到 止盈{tp}/止损{sl})"}
        if d == "rev" and not (sl < entry < tp):
            return {"ok": False, "error": f"方向错误: 多头止损须<入场价{entry}, 止盈须>入场价{entry} (收到 止盈{tp}/止损{sl})"}
        naked = st.get("naked", {})
        if len(naked) >= MAX_NAKED:
            return {"ok": False, "error": f"裸腿数已达上限{MAX_NAKED}"}
        n = pos.get("notional", 10.0)
        if n > MAX_NAKED_NOTIONAL:
            return {"ok": False, "error": f"名义{n}$超过裸腿上限{MAX_NAKED_NOTIONAL}$"}
        spot_pnl = ((px["spot"] - pos["spot_entry"]) if d == "fwd" else
                    (pos["spot_entry"] - px["spot"])) / pos["spot_entry"] * n
        fees = FEE_SPOT * n
        st["day_pnl"] = round(st.get("day_pnl", 0.0) + spot_pnl - fees, 4)
        st.setdefault("naked", {})[sym] = dict(perp_entry=entry, notional=n, tp=tp, sl=sl,
                                               t0=time.time(), funding_acc=pos.get("funding_acc", 0.0),
                                               last_fr=pos.get("last_fr", 0.0),
                                               next_funding_ts=pos.get("next_funding_ts", 0),
                                               dir=d)
        del st["positions"][sym]
        _write(_resolve("CARRY_STATE"), st)
        _log_trade(_resolve("CARRY_TRADES"), dict(symbol=sym, action="MANUAL_SPOT_TO_NAKED",
                                      spot_entry=pos["spot_entry"], spot_exit=px["spot"], dir=d,
                                      perp_entry=entry, tp=tp, sl=sl, notional=n))
        _audit("close_spot_to_naked", sym, {"spot_exit": px["spot"], "perp_entry": entry,
                                            "tp": tp, "sl": sl, "dir": d})
        leg_zh = "裸空仓" if d == "fwd" else "裸多仓"
        return {"ok": True, "msg": f"现货腿已平 {sym} ({spot_pnl:+.3f}$), 合约腿转{leg_zh} (止盈{tp} 止损{sl})"}
    finally:
        _unlock(f)


def close_naked(sym):
    """平裸腿合约"""
    px = _prices().get(sym)
    if not px:
        return {"ok": False, "error": f"{sym} 无实时价"}
    f = _lock()
    try:
        st = _read(_resolve("CARRY_STATE"), {})
        nk = (st.get("naked") or {}).get(sym)
        if not nk:
            return {"ok": False, "error": f"{sym} 无裸腿持仓"}
        n = nk.get("notional", 10.0)
        d = nk.get("dir", "fwd")
        perp_pnl = ((nk["perp_entry"] - px["perp"]) if d == "fwd" else
                    (px["perp"] - nk["perp_entry"])) / nk["perp_entry"] * n
        fees = FEE_PERP * n
        st["day_pnl"] = round(st.get("day_pnl", 0.0) + perp_pnl - fees, 4)
        del st["naked"][sym]
        _write(_resolve("CARRY_STATE"), st)
        _log_trade(_resolve("CARRY_TRADES"), dict(symbol=sym, action="MANUAL_CLOSE_NAKED",
                                      perp_entry=nk["perp_entry"], perp_exit=px["perp"],
                                      perp_pnl_usd=round(perp_pnl, 3), funding_acc=nk.get("funding_acc", 0)))
        _audit("close_naked", sym, {"perp_exit": px["perp"], "pnl": round(perp_pnl, 3)})
        return {"ok": True, "msg": f"已平裸腿 {sym} @{px['perp']:.1f} (PnL {perp_pnl:+.3f}$)"}
    finally:
        _unlock(f)


def edit_naked_tpsl(sym, tp, sl):
    """编辑裸腿止盈止损"""
    try:
        tp, sl = float(tp), float(sl)
    except Exception:
        return {"ok": False, "error": "止盈/止损数值非法"}
    f = _lock()
    try:
        st = _read(_resolve("CARRY_STATE"), {})
        nk = (st.get("naked") or {}).get(sym)
        if not nk:
            return {"ok": False, "error": f"{sym} 无裸腿持仓"}
        entry = nk["perp_entry"]
        d = nk.get("dir", "fwd")
        if d == "fwd" and not (tp < entry < sl):
            return {"ok": False, "error": f"方向错误: 空头止盈须<{entry}, 止损须>{entry}"}
        if d == "rev" and not (sl < entry < tp):
            return {"ok": False, "error": f"方向错误: 多头止损须<{entry}, 止盈须>{entry}"}
        old = (nk["tp"], nk["sl"])
        nk["tp"], nk["sl"] = tp, sl
        _write(_resolve("CARRY_STATE"), st)
        _audit("edit_naked_tpsl", sym, {"old_tp": old[0], "old_sl": old[1], "new_tp": tp, "new_sl": sl})
        return {"ok": True, "msg": f"止盈止损已更新 {sym}: {old[0]}→{tp} / {old[1]}→{sl}"}
    finally:
        _unlock(f)



def _pm_rt_px(key):
    """PM 实时价 (pm_clob_ws 桥): key=event|market → {bid, ask, last} 或 None
    优先 CLOB WS 实时价, 兜底 parquet 快照"""
    try:
        with open(_resolve("PM_TOKENS"), encoding="utf-8") as f:
            toks = json.load(f)
        with open(_resolve("PM_PX"), encoding="utf-8") as f:
            px = json.load(f).get("prices", {})
        cand = []
        for t in toks:
            if t.get("key") == key:
                p = px.get(t["token"])
                if p:
                    cand.append(p)
        if cand:
            bid = min(x["bid"] for x in cand if x.get("bid") is not None)
            ask = max(x["ask"] for x in cand if x.get("ask") is not None)
            last = next((x["last"] for x in cand if x.get("last") is not None), None)
            return {"bid": bid, "ask": ask, "last": last}
    except Exception:
        pass
    return None


def open_pm(key, side, size_usd):
    """PM 手动开仓 (点击时刻盘口快照定价)"""
    try:
        size = float(size_usd)
    except Exception:
        return {"ok": False, "error": "金额非法"}
    if not (1 <= size <= 20):
        return {"ok": False, "error": "金额须1-20$"}
    if side not in ("BUY", "SELL"):
        return {"ok": False, "error": "方向须BUY/SELL"}
    from paper_engine import latest_snapshot, FEE_RATE
    rt = _pm_rt_px(key)
    src_tag = "实时盘口"
    if rt and rt.get("bid") is not None and rt.get("ask") is not None and rt["ask"] >= rt["bid"] > 0:
        bid, ask = rt["bid"], rt["ask"]
    else:
        snaps = {f"{r['event']}|{r['market']}": r for r in latest_snapshot()}
        snap = snaps.get(key)
        if not snap:
            return {"ok": False, "error": "该市场无盘口快照, 稍后再试"}
        bid, ask = float(snap.get("pm_bid", 0) or 0), float(snap.get("pm_ask", 0) or 0)
        src_tag = "分钟快照"
    px = ask if side == "BUY" else bid
    if px <= 0:
        return {"ok": False, "error": "盘口价格无效"}
    f = _lock()
    try:
        st = _read(_resolve("PAPER_STATE"), {})
        if key in (st.get("positions") or {}):
            return {"ok": False, "error": "该市场已有持仓, 先平仓"}
        st.setdefault("positions", {})[key] = {"side": side, "entry": px, "t0": time.time(),
                                               "size_usd": size, "entry_fee_c": FEE_RATE,
                                               "exit_fee_c": FEE_RATE}
        fees = size * FEE_RATE / 100
        st["day_pnl"] = round(st.get("day_pnl", 0.0) - fees, 4)
        _write(_resolve("PAPER_STATE"), st)
        _log_trade(_resolve("PAPER_TRADES"), dict(key=key, side=side, action="MANUAL_OPEN_PM",
                                      entry=px, size_usd=size, entry_fee_c=FEE_RATE))
        _audit("open_pm", key, {"side": side, "entry": px, "size": size, "src": src_tag})
        return {"ok": True, "msg": f"已开PM仓位 {key[:28]}… ({src_tag}) "
                                   f"{('买入' if side == 'BUY' else '卖出')}@{px:.4f} (名义{size}$)"}
    finally:
        _unlock(f)


def open_naked(sym, dir_, notional, tp, sl):
    """直接开裸腿方向仓 (循环策略/波段单边): 强制止盈止损, 方向感知校验"""
    if dir_ not in ("fwd", "rev"):
        return {"ok": False, "error": "方向须 fwd/rev"}
    try:
        notional, tp, sl = float(notional), float(tp), float(sl)
    except Exception:
        return {"ok": False, "error": "参数非法"}
    if not (1 <= notional <= 50):
        return {"ok": False, "error": "名义须1-50$"}
    px = _prices().get(sym)
    if not px:
        return {"ok": False, "error": f"{sym} 无实时价"}
    entry = px["perp"]
    if dir_ == "fwd" and not (tp < entry < sl):
        return {"ok": False, "error": f"方向错误: 空头止盈须<{entry}, 止损须>{entry}"}
    if dir_ == "rev" and not (sl < entry < tp):
        return {"ok": False, "error": f"方向错误: 多头止损须<{entry}, 止盈须>{entry}"}
    row = _latest_carry_row(sym)
    f = _lock()
    try:
        st = _read(_resolve("CARRY_STATE"), {})
        if sym in st.get("positions", {}) or sym in st.get("orphans", {}) or sym in st.get("naked", {}):
            return {"ok": False, "error": f"{sym} 已有持仓/孤儿/裸腿"}
        naked = st.get("naked", {})
        if len(naked) >= MAX_NAKED:
            return {"ok": False, "error": f"裸腿数已达上限{MAX_NAKED}"}
        fees = FEE_PERP * notional
        st["day_pnl"] = round(st.get("day_pnl", 0.0) - fees, 4)
        st.setdefault("naked", {})[sym] = dict(perp_entry=entry, notional=notional, tp=tp, sl=sl,
                                               t0=time.time(), funding_acc=0.0,
                                               last_fr=(row or {}).get("funding_rate", 0.0),
                                               next_funding_ts=int((row or {}).get("next_funding_ts", 0)),
                                               dir=dir_, src="cycle")
        _write(_resolve("CARRY_STATE"), st)
        _log_trade(_resolve("CARRY_TRADES"), dict(symbol=sym, action="MANUAL_OPEN_NAKED",
                                      perp_entry=entry, notional=notional, tp=tp, sl=sl, dir=dir_))
        _audit("open_naked", sym, {"entry": entry, "notional": notional, "tp": tp, "sl": sl, "dir": dir_})
        return {"ok": True, "msg": f"已开裸{'空' if dir_ == 'fwd' else '多'}仓 {sym} @{entry:.1f} "
                                   f"(名义{notional}$, 止盈{tp} 止损{sl})"}
    finally:
        _unlock(f)


def close_pm(key):
    """平 PM 纸面桶 (用引擎同源盘口快照定价)"""
    from paper_engine import latest_snapshot, pnl_usd, TICK, FEE_RATE
    f = _lock()
    try:
        st = _read(_resolve("PAPER_STATE"), {})
        pos = (st.get("positions") or {}).get(key)
        if not pos:
            return {"ok": False, "error": f"{key} 无PM持仓"}
        snaps = {f"{r['event']}|{r['market']}": r for r in latest_snapshot()}
        snap = snaps.get(key)
        if not snap:
            return {"ok": False, "error": f"{key} 无实时盘口快照, 稍后再试"}
        bid, ask = float(snap.get("pm_bid", 0)), float(snap.get("pm_ask", 0))
        px_exit = max(bid - TICK, 0.001) if pos["side"] == "BUY" else min(ask + TICK, 0.999)
        trade = dict(key=key, side=pos["side"], action="MANUAL_CLOSE_PM",
                     entry=pos["entry"], exit=px_exit,
                     entry_fee_c=pos.get("entry_fee_c", FEE_RATE), exit_fee_c=pos.get("exit_fee_c", FEE_RATE))
        pnl = pnl_usd(trade)
        st["day_pnl"] = round(st.get("day_pnl", 0.0) + pnl, 4)
        st.setdefault("n_trades", 0)
        st["n_trades"] += 1
        del st["positions"][key]
        _write(_resolve("PAPER_STATE"), st)
        _log_trade(_resolve("PAPER_TRADES"), trade)
        _audit("close_pm", key, {"exit": px_exit, "pnl": pnl})
        return {"ok": True, "msg": f"已平PM仓位 {key[:30]}… @{px_exit:.4f} (PnL {pnl:+.4f}$)"}
    finally:
        _unlock(f)


DISPATCH = {
    "open_hedge": lambda a: open_hedge(a.get("symbol", ""), a.get("notional", 10.0), a.get("dir", "fwd")),
    "close_perp_leg": lambda a: close_perp_leg(a.get("symbol", "")),
    "close_orphan": lambda a: close_orphan(a.get("symbol", "")),
    "open_naked": lambda a: open_naked(a.get("symbol", ""), a.get("dir", "fwd"),
                                       a.get("notional", 10), a.get("tp"), a.get("sl")),
    "open_pm": lambda a: open_pm(a.get("key", ""), a.get("side", ""), a.get("size_usd", 5)),
    "close_both": lambda a: close_both(a.get("symbol", "")),
    "close_spot_to_naked": lambda a: close_spot_to_naked(a.get("symbol", ""), a.get("tp"), a.get("sl")),
    "close_naked": lambda a: close_naked(a.get("symbol", "")),
    "edit_naked_tpsl": lambda a: edit_naked_tpsl(a.get("symbol", ""), a.get("tp"), a.get("sl")),
    "close_pm": lambda a: close_pm(a.get("key", "")),
    "open_native": lambda a: open_native(a.get("symbol", ""), a.get("side", ""), a.get("notional", 10)),
    "close_native": lambda a: close_native(a.get("symbol", "")),
}


def execute(action, args):
    fn = DISPATCH.get(action)
    if not fn:
        return {"ok": False, "error": f"未知动作: {action}"}
    try:
        return fn(args or {})
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
