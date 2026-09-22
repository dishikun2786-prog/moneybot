#!/usr/bin/env python3
"""现货×永续基差套利纸面引擎 v1 (Phase 2)
策略 = 回测v2验证的"纯funding持有":
  入场: 年化funding>θ_in(5%) 且 距结算>15min 且 |基差|<20bp
  单边平仓(平合约腿): funding<0 (regime结束) 或 持仓>14天
  孤儿现货腿处置: 下一轮信号仍成立→复用为新一轮现货腿; 否则市价平掉
费用: 现货0.1% + 永续0.055%; 名义 $10/标的; funding按结算点累计"""
import argparse
import json
import os
import time

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
PARAMS_FILE = f"{BASE}/strategy_params.json"


def hot_load():
    """热加载策略参数 (每轮读取 strategy_params.json, 缺省/异常回退模块常量)"""
    global TH_IN_ANN, MAX_HOLD_H, MAX_BASIS_BP, NOTIONAL
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
    except Exception:
        pass
STALE_S = 180


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
    return {"positions": {}, "orphans": {}, "day": time.strftime("%Y-%m-%d", time.gmtime()),
            "day_pnl": 0.0, "n_rounds": 0}


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
    st = load_state()
    today = time.strftime("%Y-%m-%d", time.gmtime())
    if st["day"] != today:
        st = {"positions": {}, "orphans": {}, "day": today, "day_pnl": 0.0, "n_rounds": 0}
    events = []

    # ---- 1) 孤儿现货腿处置 (单边平仓后的遗留敞口) ----
    for sym, orph in list(st["orphans"].items()):
        r = data.get(sym)
        if not r:
            continue
        ann = r["ann_funding_pct"]
        if ann > TH_IN_ANN:  # 信号仍成立 → 复用为新一轮现货腿
            st["positions"][sym] = dict(spot_entry=orph["spot_entry"], perp_entry=r["perp_last"],
                                        t0=time.time(), funding_acc=0.0,
                                        next_funding_ts=orph["next_funding_ts"],
                                        last_fr=r["funding_rate"], reused=True)
            del st["orphans"][sym]
            fees = FEE_PERP * NOTIONAL  # 只重开合约腿
            st["day_pnl"] = round(st["day_pnl"] - fees, 4)
            log_trade(dict(ts=now_ts(), symbol=sym, action="REUSE_SPOT_LEG",
                           spot_entry=orph["spot_entry"], perp_entry=r["perp_last"]))
            events.append(f"复用现货腿 {sym} (省一次现货手续费)")
        else:  # 平掉孤儿现货腿
            pnl = (r["spot"] - orph["spot_entry"]) / orph["spot_entry"] * NOTIONAL - FEE_SPOT * NOTIONAL
            st["day_pnl"] = round(st["day_pnl"] + pnl, 4)
            st["n_rounds"] += 1
            log_trade(dict(ts=now_ts(), symbol=sym, action="CLOSE_SPOT_LEG",
                           spot_entry=orph["spot_entry"], spot_exit=r["spot"], pnl_usd=round(pnl, 3)))
            events.append(f"平孤儿现货腿 {sym} {orph['spot_entry']:.2f}→{r['spot']:.2f} {pnl:+.3f}$")
            del st["orphans"][sym]

    # ---- 2) 持仓管理: 单边平合约腿 ----
    for sym, pos in list(st["positions"].items()):
        r = data.get(sym)
        if not r:
            continue
        # funding 结算检测
        if int(r["next_funding_ts"]) > int(pos["next_funding_ts"]):
            st["day_pnl"] = round(st["day_pnl"] + pos["last_fr"] * NOTIONAL, 4)
            pos["funding_acc"] = round(pos["funding_acc"] + pos["last_fr"] * NOTIONAL, 4)
            log_trade(dict(ts=now_ts(), symbol=sym, action="FUNDING_SETTLE",
                           rate=pos["last_fr"], amount=round(pos["last_fr"] * NOTIONAL, 4)))
            pos["last_fr"] = r["funding_rate"]
            pos["next_funding_ts"] = int(r["next_funding_ts"])
        hours = (time.time() - pos["t0"]) / 3600
        fr = r["funding_rate"]
        exit_now = fr < 0 or hours >= MAX_HOLD_H
        if exit_now:
            reason = "funding翻负(regime结束)" if fr < 0 else "超时"
            # 平合约腿: 空永续 PnL = (entry - now)/entry × N
            perp_pnl = (pos["perp_entry"] - r["perp_last"]) / pos["perp_entry"] * NOTIONAL
            fees = (FEE_SPOT + FEE_PERP) * NOTIONAL  # 两腿开仓费 + 合约腿平仓费近似
            st["day_pnl"] = round(st["day_pnl"] + perp_pnl - fees, 4)
            log_trade(dict(ts=now_ts(), symbol=sym, action="CLOSE_PERP_LEG(单边平仓)",
                           perp_entry=pos["perp_entry"], perp_exit=r["perp_last"],
                           funding_acc=pos["funding_acc"], perp_pnl_usd=round(perp_pnl, 3), reason=reason))
            events.append(f"单边平合约腿 {sym} {reason}: 合约腿{perp_pnl:+.3f}$ "
                          f"funding累计{pos['funding_acc']:+.3f}$")
            # 现货腿 → 孤儿 (下一轮复用或平掉)
            st["orphans"][sym] = dict(spot_entry=pos["spot_entry"], t0=pos["t0"],
                                      next_funding_ts=int(r["next_funding_ts"]))
            del st["positions"][sym]

    # ---- 3) 开新仓 ----
    for sym, r in data.items():
        if sym in st["positions"] or sym in st["orphans"]:
            continue
        ann = r["ann_funding_pct"]
        basis = r["basis_mark_bp"]
        near_settle = (int(r["next_funding_ts"]) / 1000) % 28800 >= 27900
        if ann > TH_IN_ANN and abs(basis) < MAX_BASIS_BP and not near_settle:
            st["positions"][sym] = dict(spot_entry=r["spot"], perp_entry=r["perp_last"],
                                        t0=time.time(), funding_acc=0.0,
                                        next_funding_ts=int(r["next_funding_ts"]),
                                        last_fr=r["funding_rate"], reused=False)
            fees = (FEE_SPOT + FEE_PERP) * NOTIONAL
            st["day_pnl"] = round(st["day_pnl"] - fees, 4)
            log_trade(dict(ts=now_ts(), symbol=sym, action="OPEN_BOTH_LEGS",
                           spot_entry=r["spot"], perp_entry=r["perp_last"],
                           ann_pct=round(ann, 2), basis_bp=round(basis, 2)))
            events.append(f"开仓 {sym} 多现货@{r['spot']} + 空永续@{r['perp_last']} "
                          f"(年化{ann:.1f}%, 基差{basis:+.1f}bp)")

    save_state(st)
    pos_txt = ", ".join(f"{s}:{('复用' if p.get('reused') else '持有')}" for s, p in st["positions"].items()) or "(空仓)"
    orph_txt = ", ".join(st["orphans"]) or "无"
    print(f"[{now_ts()}] carry纸面: 持仓[{pos_txt}] 孤儿现货腿[{orph_txt}] | "
          f"今日 {st['n_rounds']}轮 累计PnL {st['day_pnl']:+.2f}$")
    for e in events:
        print("  ", e)
    return st


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
                print("  [err]", type(e).__name__, e)
            time.sleep(60)
