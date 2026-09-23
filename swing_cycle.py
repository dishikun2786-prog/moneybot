#!/usr/bin/env python3
"""循环恢复对冲策略 v1: 波段量能择时 + 小金额单边对冲 + 受限恢复阶梯
状态机: IDLE → ARMED → OPEN(裸腿带TP/SL) → SETTLED(冷却) → ARMED ...
- 裸腿平仓由 carry_engine 的 TP/SL 检查完成; 本引擎负责开仓决策与回合结算记账
- 受限恢复阶梯: ×1.5、3级封顶、每级信号重新确认、日亏冻结24h
- 独立预算: 循环策略盈亏单独核算 (cycle_day_pnl), 不污染套利策略 day_pnl
用法: python swing_cycle.py [--once]"""
import argparse
import json
import os
import time

import paper_ops

import tenants

BASE = tenants.ROOT  # 保留: 兼容旧引用 (实际路径走 __getattr__)

def _resolve(name):
    """内部路径解析: 测试 setattr monkeypatch 优先, 否则租户动态解析"""
    if name in globals():
        return globals()[name]
    return _DYN[name]()

_DYN = {
    "STATE_FILE": lambda: tenants.cycle_state(),
    "ROUNDS_LOG": lambda: tenants.cycle_rounds(),
    "PARAMS_FILE": lambda: tenants.params_file(),
}


def __getattr__(name):
    f = _DYN.get(name)
    if f:
        return f()
    raise AttributeError(f"module 'swing_cycle' has no attribute '{name}'")
SYMBOLS = ("BTCUSDT", "ETHUSDT")

ENABLED = 0
MIN_SCORE = 60.0
BASE_NOTIONAL = 10.0
MULT = 1.5
MAX_LADDER = 3
TP_PCT = 1.0
SL_PCT = 1.0
DAILY_LOSS_CAP = 2.0
COOLDOWN_S = 900
NOTIONAL_CAP = 30.0


def hot_load():
    global ENABLED, MIN_SCORE, BASE_NOTIONAL, MULT, MAX_LADDER, TP_PCT, SL_PCT, DAILY_LOSS_CAP, COOLDOWN_S
    try:
        d = json.load(open(_resolve("PARAMS_FILE"))).get("cycle", {})
        if d.get("enabled") is not None:
            ENABLED = int(float(d["enabled"]))
        if d.get("min_score") is not None:
            MIN_SCORE = float(d["min_score"])
        if d.get("base_notional") is not None:
            BASE_NOTIONAL = float(d["base_notional"])
        if d.get("mult") is not None:
            MULT = float(d["mult"])
        if d.get("max_ladder") is not None:
            MAX_LADDER = int(float(d["max_ladder"]))
        if d.get("tp_pct") is not None:
            TP_PCT = float(d["tp_pct"])
        if d.get("sl_pct") is not None:
            SL_PCT = float(d["sl_pct"])
        if d.get("daily_loss_cap") is not None:
            DAILY_LOSS_CAP = float(d["daily_loss_cap"])
        if d.get("cooldown_s") is not None:
            COOLDOWN_S = float(d["cooldown_s"])
    except Exception:
        pass


def _read():
    try:
        return json.load(open(_resolve("STATE_FILE")))
    except Exception:
        return {"round": 0, "phase": "IDLE", "ladder": 0, "cum_pnl": 0.0, "cycle_day_pnl": 0.0,
                "day": "", "frozen_until": 0.0, "cooldown_until": 0.0, "symbol": None, "dir": None,
                "notional": 0.0, "last_score": None, "n_rounds": 0, "n_wins": 0, "open_ts": 0.0}


def _write(st):
    tmp = _resolve("STATE_FILE") + ".tmp"
    json.dump(st, open(tmp, "w"), ensure_ascii=False, indent=1)
    os.replace(tmp, _resolve("STATE_FILE"))


def _audit_round(rec):
    with open(_resolve("ROUNDS_LOG"), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def ladder_notional(st):
    """当前阶梯名义: base × mult^ladder, 封顶 NOTIONAL_CAP"""
    n = BASE_NOTIONAL * (MULT ** st["ladder"])
    return min(n, NOTIONAL_CAP)


def settle_round(st):
    """检测 OPEN 回合是否已被 TP/SL 平仓 → 结算记账, 更新阶梯"""
    sym = st.get("symbol")
    carry = paper_ops._read(paper_ops.CARRY_STATE, {})
    if sym and sym in (carry.get("naked") or {}):
        return st, None  # 还在持仓, 等 TP/SL
    if not sym:
        return st, None
    # 找最近一条该标的的裸腿平仓记录定胜负
    pnl = None
    try:
        lines = open(paper_ops.CARRY_TRADES).read().splitlines()
        for line in reversed(lines):
            try:
                t = json.loads(line)
            except Exception:
                continue
            if t.get("symbol") == sym and t.get("action") in ("MANUAL_NAKED_TP", "MANUAL_NAKED_SL",
                                                              "MANUAL_CLOSE_NAKED"):
                pnl = float(t.get("perp_pnl_usd", 0.0))
                break
    except Exception:
        pass
    if pnl is None:
        return st, None
    win = pnl > 0
    st["n_rounds"] = st.get("n_rounds", 0) + 1
    if win:
        st["n_wins"] = st.get("n_wins", 0) + 1
        st["ladder"] = 0
    else:
        st["ladder"] = min(st.get("ladder", 0) + 1, MAX_LADDER)
    st["cum_pnl"] = round(st.get("cum_pnl", 0.0) + pnl, 4)
    st["cycle_day_pnl"] = round(st.get("cycle_day_pnl", 0.0) + pnl, 4)
    st["phase"] = "SETTLED"
    st["cooldown_until"] = time.time() + COOLDOWN_S
    st["symbol"] = None
    st["dir"] = None
    _audit_round({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "round": st["round"],
                  "symbol": sym, "pnl": round(pnl, 4), "win": win, "ladder": st["ladder"],
                  "score": st.get("last_score")})
    st["round"] = st.get("round", 0) + 1
    return st, round(pnl, 4)


def try_open(st):
    """波段评分确认 → 开裸腿单 (方向取评分更高侧)"""
    best = None  # (score, sym, dir)
    for sym in SYMBOLS:
        mr, we = load_micro(sym)
        if len(mr) < 10:
            continue
        s_fwd = swing_score(mr, we, "fwd")
        s_rev = swing_score(mr, we, "rev")
        s, d = (s_fwd, "fwd") if s_fwd >= s_rev else (s_rev, "rev")
        if best is None or s > best[0]:
            best = (s, sym, d)
    if not best:
        return st, None, "数据不足"
    score, sym, dir_ = best
    if score < MIN_SCORE:
        return st, None, f"评分{score:.0f}<{MIN_SCORE:.0f}"
    n = ladder_notional(st)
    px = paper_ops._prices().get(sym)
    if not px:
        return st, None, "无实时价"
    entry = px["perp"]
    tp_pct = TP_PCT / 100
    sl_pct = SL_PCT / 100
    if dir_ == "fwd":
        tp, sl = round(entry * (1 - tp_pct), 1), round(entry * (1 + sl_pct), 1)
    else:
        tp, sl = round(entry * (1 + tp_pct), 1), round(entry * (1 - sl_pct), 1)
    r = paper_ops.open_naked(sym, dir_, n, tp, sl)
    if not r.get("ok"):
        return st, None, r.get("error", "开仓失败")
    st["phase"] = "OPEN"
    st["symbol"] = sym
    st["dir"] = dir_
    st["notional"] = n
    st["last_score"] = score
    st["open_ts"] = time.time()
    return st, r, f"开仓 {sym} {dir_} 名义{n}$ 评分{score:.0f}"


def cycle():
    hot_load()
    f = paper_ops._lock()
    try:
        st = _read()
        today = time.strftime("%Y-%m-%d", time.gmtime())
        if st.get("day") != today:
            st["day"] = today
            st["cycle_day_pnl"] = 0.0
        now = time.time()
        msg = ""
        if st.get("phase") == "SETTLED" and now >= st.get("cooldown_until", 0):
            st["phase"] = "ARMED"
        if not ENABLED:
            st["phase"] = "IDLE" if st.get("phase") != "OPEN" else st["phase"]
            msg = "[已关闭]"
        elif now < st.get("frozen_until", 0):
            msg = "[日亏冻结中]"
        else:
            st, res = settle_round(st)
            if st.get("phase") == "OPEN":
                msg = f"[持仓中 {st.get('symbol')} {st.get('dir')} 阶梯{st.get('ladder')}]"
            elif res is None:
                # 日亏熔断检查 (循环独立预算)
                if st.get("cycle_day_pnl", 0.0) <= -DAILY_LOSS_CAP:
                    st["frozen_until"] = now + 86400
                    msg = f"[日亏{-DAILY_LOSS_CAP}$ 触发冻结24h]"
                elif st.get("phase") in ("IDLE", "ARMED"):
                    st, r, why = try_open(st)
                    msg = f"[{why}]" if not st.get("phase") == "OPEN" else \
                          f"[开仓 {st.get('symbol')} 阶梯{st.get('ladder')}]"
            else:
                msg = f"[回合结算 {res:+.3f}$ → 阶梯{st.get('ladder')}]"
        _write(st)
        print(f"[{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}] 循环策略: "
              f"{st.get('phase')} {msg} | 回合{st.get('n_rounds')} 胜率"
              f"{round(st.get('n_wins', 0) / max(st.get('n_rounds', 1), 1) * 100)}% "
              f"累计{st.get('cum_pnl', 0):+.2f}$ 日{st.get('cycle_day_pnl', 0):+.2f}$", flush=True)
        return st
    finally:
        paper_ops._unlock(f)


def load_micro(sym, n=10):
    rows, walls = [], []
    try:
        for line in open(tenants.shared_log("micro_1m.jsonl")).read().splitlines()[-40:]:
            try:
                r = json.loads(line)
                if r.get("sym") == sym:
                    rows.append(r)
            except Exception:
                continue
    except Exception:
        pass
    try:
        for line in open(tenants.shared_log("wall_events.jsonl")).read().splitlines()[-40:]:
            try:
                w = json.loads(line)
                if w.get("sym") == sym:
                    walls.append(w)
            except Exception:
                continue
    except Exception:
        pass
    return rows[-n:], walls


def clamp01(x):
    return max(0.0, min(1.0, x))


def swing_score(micro_rows, wall_events, dir_):
    """波段微结构评分 0-100 (与 carry_engine.swing_score 同构, 独立实现供回测复用)"""
    if len(micro_rows) < 10:
        return 0.0
    rows = micro_rows[-10:]
    bv = sum(r.get("bv", 0) or 0 for r in rows)
    sv = sum(r.get("sv", 0) or 0 for r in rows)
    tot = bv + sv
    ratio = (bv / tot) if tot > 0 else 0.5
    s_imb = clamp01(ratio if dir_ == "fwd" else (1 - ratio)) * 40
    cvd = [r.get("cum_cvd") for r in rows if r.get("cum_cvd") is not None]
    if len(cvd) >= 6:
        n = len(cvd)
        xs = list(range(n))
        slope = (n * sum(x * y for x, y in zip(xs, cvd)) - sum(xs) * sum(cvd)) / \
                max(n * sum(x * x for x in xs) - sum(xs) ** 2, 1)
        norm = clamp01(slope / max(tot / n, 1e-9))
        cvd_u = norm if dir_ == "fwd" else (1 - norm)
        s_cvd = clamp01((cvd_u - 0.5) * 2 + 0.5) * 30
    else:
        s_cvd = 15.0
    net_wall = 0
    for w in wall_events[-10:]:
        if w.get("side") == "bids" and w.get("type") == "appear":
            net_wall += 1
        elif w.get("side") == "asks" and w.get("type") == "appear":
            net_wall -= 1
    wall_u = clamp01(0.5 + net_wall * 0.1)
    s_wall = (wall_u if dir_ == "fwd" else (1 - wall_u)) * 20
    oi_now = rows[-1].get("oi")
    oi_prev = rows[0].get("oi")
    oi_chg = 0.5
    if oi_now and oi_prev:
        oi_chg = clamp01(0.5 + (oi_now - oi_prev) / oi_prev * 10)
    s_oi = (oi_chg if dir_ == "fwd" else (1 - oi_chg)) * 10
    return round(s_imb + s_cvd + s_wall + s_oi, 1)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    if args.once:
        cycle()
    else:
        while True:
            try:
                cycle()
            except KeyboardInterrupt:
                break
            except Exception as e:
                print(f"  [err] {type(e).__name__}: {e}", flush=True)
            time.sleep(60)
