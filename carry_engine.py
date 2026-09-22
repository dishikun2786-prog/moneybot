#!/usr/bin/env python3
"""现货×永续基差套利纸面引擎 v1.1 (Phase 2 + P0复利/结算窗口)
策略 = 回测v2验证的"纯funding持有":
  入场: 年化funding>θ_in(5%) 且 距结算15-60分钟窗口 且 |基差|<20bp
  单边平仓(平合约腿): funding<0 (regime结束) 或 持仓>14天
  孤儿现货腿处置: 下一轮信号仍成立→复用; 否则市价平掉
  复利: 有效名义 = 基准 × (1 + 累计PnL/基准), 限幅[0.2x, 3x], 每笔记录开仓时名义
费用: 现货0.1% + 永续0.055%; funding按结算点累计"""
import argparse
import json
import os
import time

import paper_ops

BASE = os.path.expanduser("~/polymarket")
CARRY = f"{BASE}/logs/carry_1m.jsonl"
STATE = f"{BASE}/logs/carry_state.json"
TRADES = f"{BASE}/logs/carry_trades.jsonl"
HALT = f"{BASE}/engine/HALT"

TH_IN_ANN = 5.0
MAX_HOLD_H = 14 * 24
MAX_BASIS_BP = 20.0
NOTIONAL = 10.0
FEE_SPOT = 0.001
FEE_PERP = 0.00055
COMP_BASE = 10.0      # 复利基准名义$
COMP_MIN = 0.2        # 名义最小倍率
COMP_MAX = 3.0        # 名义最大倍率
ENTRY_WINDOW_MIN = 60  # 结算前N分钟入场窗口 (0=关闭)
PARAMS_FILE = f"{BASE}/strategy_params.json"


def hot_load():
    """热加载策略参数 (每轮读取 strategy_params.json, 缺省/异常回退模块常量)"""
    global TH_IN_ANN, MAX_HOLD_H, MAX_BASIS_BP, NOTIONAL
    global COMP_BASE, COMP_MIN, COMP_MAX, ENTRY_WINDOW_MIN
    try:
        d = json.load(open(PARAMS_FILE)).get("carry", {})
        if d.get("theta_in_ann_pct") is not None:
            TH_IN_ANN = float(d["theta_in_ann_pct"])
        if d.get("max_hold_h") is not None:
            MAX_HOLD_H = float(d["max_hold_h"])
        if d.get("max_basis_bp") is not None:
            MAX_BASIS_BP = float(d["max_basis_bp"])
        if d.get("notional_usd") is not None:
            NOTIONAL = float(d["notional_usd"])
        if d.get("compounding_base_usd") is not None:
            COMP_BASE = float(d["compounding_base_usd"])
        if d.get("compounding_min_mult") is not None:
            COMP_MIN = float(d["compounding_min_mult"])
        if d.get("compounding_max_mult") is not None:
            COMP_MAX = float(d["compounding_max_mult"])
        if d.get("entry_window_min") is not None:
            ENTRY_WINDOW_MIN = float(d["entry_window_min"])
    except Exception:
        pass


def effective_notional(st):
    """复利名义 = 基准 × (1 + 累计PnL/基准), 限幅"""
    mult = 1.0 + (st.get("cum_pnl", 0.0) + st.get("day_pnl", 0.0)) / max(COMP_BASE, 0.01)
    mult = max(COMP_MIN, min(COMP_MAX, mult))
    return round(NOTIONAL * mult, 2)


def in_entry_window(r):
    """结算窗口择时: 距结算 15~N 分钟内才入场 (N<=0 关闭限制)"""
    if ENTRY_WINDOW_MIN <= 0:
        return True
    sec = int(r["next_funding_ts"]) / 1000 - time.time()
    return 0 < sec <= ENTRY_WINDOW_MIN * 60


STALE_S = 180
_EL = {"f": None}  # 引擎持有锁 (与手动操作/API串行化)


def engine_acquire():
    if _EL["f"] is None:
        _EL["f"] = paper_ops._lock()


def engine_release():
    if _EL["f"] is not None:
        paper_ops._unlock(_EL["f"])
        _EL["f"] = None


def latest():
    rows = []
    with open(CARRY, encoding="utf-8") as f:
        for line in f:
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    if not rows:
        return {}
    last_ts = rows[-1]["ts"]
    return {r["symbol"]: r for r in rows if r["ts"] == last_ts}


def load_state():
    if os.path.exists(STATE):
        try:
            return json.load(open(STATE))
        except Exception:
            pass
    return {"positions": {}, "orphans": {}, "naked": {}, "day": time.strftime("%Y-%m-%d", time.gmtime()),
            "day_pnl": 0.0, "cum_pnl": 0.0, "n_rounds": 0}


def save_state(st):
    json.dump(st, open(STATE, "w"), ensure_ascii=False, indent=1)


def log_trade(rec):
    with open(TRADES, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def now_ts():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def cycle():
    hot_load()
    data = latest()
    if not data:
        print("  [warn] carry_1m 无数据")
        return
    if os.path.exists(HALT):
        print("  [HALT] 引擎停机")
        return
    # 数据新鲜度
    stale = max(abs(time.time() - time.mktime(time.strptime(r["ts"], "%Y-%m-%dT%H:%M:%SZ"))) for r in data.values())
    if stale > STALE_S:
        print(f"  [warn] 数据停滞 {stale:.0f}s, 跳过")
        return
    engine_acquire()  # 与手动操作/API串行化
    st = load_state()
    today = time.strftime("%Y-%m-%d", time.gmtime())
    if st["day"] != today:
        # 跨天: 累计PnL沉淀, 保留持仓/孤儿/裸腿 (修复旧版午夜清仓bug)
        st["cum_pnl"] = round(st.get("cum_pnl", 0.0) + st.get("day_pnl", 0.0), 4)
        st["day_pnl"] = 0.0
        st["n_rounds"] = 0
        st["day"] = today
    st.setdefault("naked", {})
    N = effective_notional(st)
    events = []

    # ---- 0) 裸腿持仓: 止盈/止损触发检查 + funding结算 ----
    for sym, nk in list(st["naked"].items()):
        r = data.get(sym)
        if not r:
            continue
        live = r["perp_last"]
        if int(r["next_funding_ts"]) > int(nk.get("next_funding_ts", 0)):
            n_nk = nk.get("notional", N)
            st["day_pnl"] = round(st["day_pnl"] + nk.get("last_fr", 0.0) * n_nk, 4)
            nk["funding_acc"] = round(nk.get("funding_acc", 0.0) + nk.get("last_fr", 0.0) * n_nk, 4)
            log_trade(dict(ts=now_ts(), symbol=sym, action="FUNDING_SETTLE_NAKED",
                           rate=nk.get("last_fr", 0.0),
                           amount=round(nk.get("last_fr", 0.0) * n_nk, 4), notional=n_nk))
            nk["last_fr"] = r["funding_rate"]
            nk["next_funding_ts"] = int(r["next_funding_ts"])
        hit = None
        if live <= nk["tp"]:
            hit = "止盈"
        elif live >= nk["sl"]:
            hit = "止损"
        if hit:
            n_nk = nk.get("notional", N)
            perp_pnl = (nk["perp_entry"] - live) / nk["perp_entry"] * n_nk
            fees = FEE_PERP * n_nk
            st["day_pnl"] = round(st["day_pnl"] + perp_pnl - fees, 4)
            st.setdefault("n_rounds", 0)
            st["n_rounds"] += 1
            log_trade(dict(ts=now_ts(), symbol=sym,
                           action="MANUAL_NAKED_TP" if hit == "止盈" else "MANUAL_NAKED_SL",
                           perp_entry=nk["perp_entry"], perp_exit=live,
                           perp_pnl_usd=round(perp_pnl, 3), tp=nk["tp"], sl=nk["sl"],
                           funding_acc=nk.get("funding_acc", 0)))
            events.append(f"裸腿{hit}平仓 {sym} @{live} ({perp_pnl:+.3f}$)")
            del st["naked"][sym]

    # ---- 1) 孤儿现货腿处置 (单边平仓后的遗留敞口) ----
    for sym, orph in list(st["orphans"].items()):
        r = data.get(sym)
        if not r:
            continue
        ann = r["ann_funding_pct"]
        n_orph = orph.get("notional", N)
        if ann > TH_IN_ANN:  # 信号仍成立 → 复用为新一轮现货腿
            st["positions"][sym] = dict(spot_entry=orph["spot_entry"], perp_entry=r["perp_last"],
                                        t0=time.time(), funding_acc=0.0,
                                        next_funding_ts=orph["next_funding_ts"],
                                        last_fr=r["funding_rate"], reused=True,
                                        notional=n_orph)
            del st["orphans"][sym]
            fees = FEE_PERP * n_orph  # 只重开合约腿
            st["day_pnl"] = round(st["day_pnl"] - fees, 4)
            log_trade(dict(ts=now_ts(), symbol=sym, action="REUSE_SPOT_LEG",
                           spot_entry=orph["spot_entry"], perp_entry=r["perp_last"],
                           notional=n_orph))
            events.append(f"复用现货腿 {sym} (名义{n_orph}$, 省一次现货手续费)")
        else:  # 平掉孤儿现货腿
            pnl = (r["spot"] - orph["spot_entry"]) / orph["spot_entry"] * n_orph - FEE_SPOT * n_orph
            st["day_pnl"] = round(st["day_pnl"] + pnl, 4)
            st["n_rounds"] += 1
            log_trade(dict(ts=now_ts(), symbol=sym, action="CLOSE_SPOT_LEG",
                           spot_entry=orph["spot_entry"], spot_exit=r["spot"],
                           pnl_usd=round(pnl, 3), notional=n_orph))
            events.append(f"平孤儿现货腿 {sym} {orph['spot_entry']:.2f}→{r['spot']:.2f} {pnl:+.3f}$")
            del st["orphans"][sym]

    # ---- 2) 持仓管理: 单边平合约腿 ----
    for sym, pos in list(st["positions"].items()):
        r = data.get(sym)
        if not r:
            continue
        # funding 结算检测
        if int(r["next_funding_ts"]) > int(pos["next_funding_ts"]):
            n_pos = pos.get("notional", N)
            st["day_pnl"] = round(st["day_pnl"] + pos["last_fr"] * n_pos, 4)
            pos["funding_acc"] = round(pos["funding_acc"] + pos["last_fr"] * n_pos, 4)
            log_trade(dict(ts=now_ts(), symbol=sym, action="FUNDING_SETTLE",
                           rate=pos["last_fr"], amount=round(pos["last_fr"] * n_pos, 4),
                           notional=n_pos))
            pos["last_fr"] = r["funding_rate"]
            pos["next_funding_ts"] = int(r["next_funding_ts"])
        hours = (time.time() - pos["t0"]) / 3600
        fr = r["funding_rate"]
        exit_now = fr < 0 or hours >= MAX_HOLD_H
        if exit_now:
            reason = "funding翻负(regime结束)" if fr < 0 else "超时"
            n_pos = pos.get("notional", N)
            # 平合约腿: 空永续 PnL = (entry - now)/entry × N
            perp_pnl = (pos["perp_entry"] - r["perp_last"]) / pos["perp_entry"] * n_pos
            fees = (FEE_SPOT + FEE_PERP) * n_pos  # 两腿开仓费 + 合约腿平仓费近似
            st["day_pnl"] = round(st["day_pnl"] + perp_pnl - fees, 4)
            log_trade(dict(ts=now_ts(), symbol=sym, action="CLOSE_PERP_LEG(单边平仓)",
                           perp_entry=pos["perp_entry"], perp_exit=r["perp_last"],
                           funding_acc=pos["funding_acc"], perp_pnl_usd=round(perp_pnl, 3),
                           notional=n_pos, reason=reason))
            events.append(f"单边平合约腿 {sym} {reason}: 合约腿{perp_pnl:+.3f}$ "
                          f"funding累计{pos['funding_acc']:+.3f}$")
            # 现货腿 → 孤儿 (下一轮复用或平掉)
            st["orphans"][sym] = dict(spot_entry=pos["spot_entry"], t0=pos["t0"],
                                      next_funding_ts=int(r["next_funding_ts"]),
                                      notional=n_pos)
            del st["positions"][sym]

    # ---- 3) 开新仓 ----
    for sym, r in data.items():
        if sym in st["positions"] or sym in st["orphans"] or sym in st["naked"]:
            continue
        ann = r["ann_funding_pct"]
        basis = r["basis_mark_bp"]
        near_settle = (int(r["next_funding_ts"]) / 1000) % 28800 >= 27900
        in_window = in_entry_window(r)
        if ann > TH_IN_ANN and abs(basis) < MAX_BASIS_BP and not near_settle and in_window:
            st["positions"][sym] = dict(spot_entry=r["spot"], perp_entry=r["perp_last"],
                                        t0=time.time(), funding_acc=0.0,
                                        next_funding_ts=int(r["next_funding_ts"]),
                                        last_fr=r["funding_rate"], reused=False,
                                        notional=N)
            fees = (FEE_SPOT + FEE_PERP) * N
            st["day_pnl"] = round(st["day_pnl"] - fees, 4)
            log_trade(dict(ts=now_ts(), symbol=sym, action="OPEN_BOTH_LEGS",
                           spot_entry=r["spot"], perp_entry=r["perp_last"],
                           ann_pct=round(ann, 2), basis_bp=round(basis, 2),
                           notional=N))
            events.append(f"开仓 {sym} 多现货@{r['spot']} + 空永续@{r['perp_last']} "
                          f"(年化{ann:.1f}%, 基差{basis:+.1f}bp, 名义{N}$)")
        elif ann > TH_IN_ANN and not in_window:
            events.append(f"[窗口过滤] {sym} 信号成立但距结算>={ENTRY_WINDOW_MIN:.0f}分钟, 等待结算窗口")

    save_state(st)
    engine_release()
    pos_txt = ", ".join(f"{s}:{('复用' if p.get('reused') else '持有')}" for s, p in st["positions"].items()) or "(空仓)"
    orph_txt = ", ".join(st["orphans"]) or "无"
    nkd_txt = ", ".join(f"{s}(TP{nk['tp']}/SL{nk['sl']})" for s, nk in st["naked"].items()) or "无"
    print(f"[{now_ts()}] carry纸面: 持仓[{pos_txt}] 孤儿现货腿[{orph_txt}] 裸腿[{nkd_txt}] | "
          f"今日 {st['n_rounds']}轮 当日PnL {st['day_pnl']:+.2f}$ "
          f"累计 {st.get('cum_pnl', 0):+.2f}$ 有效名义{N}$({N / max(NOTIONAL, 0.01):.2f}x)")
    for e in events:
        print("  ", e)
    return st


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    if args.once:
        cycle()
        engine_release()
    else:
        while True:
            try:
                cycle()
            except KeyboardInterrupt:
                break
            except Exception as e:
                print("  [err]", type(e).__name__, e)
            finally:
                engine_release()
            time.sleep(60)
