#!/usr/bin/env python3
"""纸面交易引擎 v1.1 — 校准后信号的模拟成交证据 (T3.6 准备)
每60s(与monitor对齐)执行:
- 信号与成交价均用最新快照(monitor同轮采的模型+盘口, 时间一致)
- 开仓: 毛edge>5c + 流动性(对手档>=100股) + 价差<=6c + 仓位/敞口/日亏限额
- 平仓: edge衰减<0.5c 或 持仓超6h (快照对手价±1tick成交)
- 记录 paper_trades.jsonl + paper_state.json
用法: ./venv/bin/python paper_engine.py [--once]"""
import argparse
import csv
import json
import os
import sys
import time

BASE = os.path.expanduser("~/polymarket")
CSV = f"{BASE}/logs/bybit_pm_fv.csv"
STATE = f"{BASE}/logs/paper_state.json"
TRADES = f"{BASE}/logs/paper_trades.jsonl"

FEE_RATE = 0.07
SHARES = 100
TICK = 0.01
TH_IN_C = 5.0        # 毛edge入场阈值 (美分/股)
TH_OUT_C = 0.5       # 毛edge平仓阈值
MAX_HOLD_H = 6.0
MAX_POSITIONS = 2
MAX_EXPOSURE_USD = 10.0
MAX_DAILY_LOSS = 2.0


def hot_load():
    """热加载策略参数 (strategy_params.json paper_pm组 → 模块全局, 每轮调用)"""
    global TH_IN_C, TH_OUT_C, MAX_HOLD_H, MAX_POSITIONS, MAX_EXPOSURE_USD, MAX_DAILY_LOSS, SHARES
    try:
        d = json.load(open(f"{BASE}/strategy_params.json")).get("paper_pm", {})
        if d.get("min_gross_edge_c") is not None:
            TH_IN_C = float(d["min_gross_edge_c"])
        if d.get("theta_out_c") is not None:
            TH_OUT_C = float(d["theta_out_c"])
        if d.get("max_hold_h") is not None:
            MAX_HOLD_H = float(d["max_hold_h"])
        if d.get("max_positions") is not None:
            MAX_POSITIONS = int(float(d["max_positions"]))
        if d.get("max_exposure_usd") is not None:
            MAX_EXPOSURE_USD = float(d["max_exposure_usd"])
        if d.get("max_daily_loss") is not None:
            MAX_DAILY_LOSS = float(d["max_daily_loss"])
        if d.get("min_opposite_size") is not None:
            SHARES = float(d["min_opposite_size"])
    except Exception:
        pass


def fee_c(p):
    return FEE_RATE * p * (1 - p) * 100  # 美分/股


def latest_snapshot():
    rows = []
    with open(CSV, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return []
    last_ts = rows[-1]["ts_utc"]
    return [r for r in rows if r["ts_utc"] == last_ts]


def underlying_of(event):
    return "BTC" if "Bitcoin" in (event or "") else "ETH"


def load_state():
    if os.path.exists(STATE):
        try:
            return json.load(open(STATE))
        except Exception:
            pass
    return {"positions": {}, "day": time.strftime("%Y-%m-%d", time.gmtime()),
            "day_pnl": 0.0, "n_trades": 0}


def save_state(st):
    json.dump(st, open(STATE, "w"), ensure_ascii=False, indent=1)


def log_trade(rec):
    with open(TRADES, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def pnl_usd(trade):
    if trade["side"] == "BUY":
        gross = (trade["exit"] - trade["entry"]) * SHARES
    else:
        gross = (trade["entry"] - trade["exit"]) * SHARES
    fees = SHARES * (trade["entry_fee_c"] + trade["exit_fee_c"]) / 100
    return round(gross - fees, 4)


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def cycle():
    hot_load()
    now = time.time()
    snaps = {f"{r['event']}|{r['market']}": r for r in latest_snapshot()}
    st = load_state()
    today = time.strftime("%Y-%m-%d", time.gmtime())
    if st["day"] != today:
        st = {"positions": {}, "day": today, "day_pnl": 0.0, "n_trades": 0}
    # 手动模式: 自动交易暂停 (用户手动开平)
    import engine_mode
    if engine_mode.load()["paper_pm"] == "manual":
        save_state(st)
        n = len(st.get("positions", {}))
        print(f"[{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}] paper纸面: "
              f"[手动模式] 自动交易已暂停 | 持仓{n}个 | 当日PnL {st['day_pnl']:+.2f}$")
        return st
    events = []

    # ---- 持仓管理 (edge 用同轮快照的对手价) ----
    for key, pos in list(st["positions"].items()):
        snap = snaps.get(key)
        if not snap:
            continue
        bid, ask = _f(snap.get("pm_bid")), _f(snap.get("pm_ask"))
        model = _f(snap.get("model_p"))
        g_edge = ((model - bid) * 100 if pos["side"] == "BUY"
                  else (ask - model) * 100)
        hours = (now - pos["t0"]) / 3600
        if g_edge < TH_OUT_C or hours >= MAX_HOLD_H:
            px = max(bid - TICK, 0.001) if pos["side"] == "BUY" else min(ask + TICK, 0.999)
            trade = dict(ts=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                         key=key, side=pos["side"], action="CLOSE",
                         entry=pos["entry"], exit=px,
                         entry_fee_c=pos["entry_fee_c"], exit_fee_c=fee_c(px),
                         hold_h=round(hours, 2),
                         reason="timeout" if hours >= MAX_HOLD_H else "edge_decay")
            p = pnl_usd(trade)
            trade["pnl_usd"] = p
            st["day_pnl"] = round(st["day_pnl"] + p, 4)
            st["n_trades"] += 1
            log_trade(trade)
            del st["positions"][key]
            events.append(f"平仓 {key[:30]} {pos['side']} {pos['entry']:.3f}→{px:.3f} "
                          f"{p:+.2f}$ ({trade['reason']})")

    # ---- 开仓 (候选 = 快照同轮信号) ----
    candidates = []
    for key, snap in snaps.items():
        if key in st["positions"] or len(st["positions"]) >= MAX_POSITIONS:
            continue
        bid, ask = _f(snap.get("pm_bid")), _f(snap.get("pm_ask"))
        bsz, asz = _f(snap.get("sz_bid")), _f(snap.get("sz_ask"))
        model = _f(snap.get("model_p"))
        if bid <= 0 or ask <= 0 or ask < bid or ask - bid > 0.06:
            continue
        g_buy = (model - ask) * 100
        g_sell = (bid - model) * 100
        if g_buy >= TH_IN_C and asz >= SHARES:
            candidates.append((g_buy, key, "BUY", ask, fee_c(ask)))
        if g_sell >= TH_IN_C and bsz >= SHARES:
            candidates.append((g_sell, key, "SELL", bid, fee_c(bid)))
    candidates.sort(key=lambda x: -x[0])
    for g, key, side, px, fc in candidates:
        if len(st["positions"]) >= MAX_POSITIONS:
            break
        if st["day_pnl"] <= -MAX_DAILY_LOSS:
            events.append("日亏上限 -$2 已触发, 停止开新仓")
            break
        under = underlying_of(snaps[key]["event"])
        cur_exp = sum(p["size_usd"] for p in st["positions"].values()
                      if p["underlying"] == under)
        size_usd = SHARES * px
        if cur_exp + size_usd > MAX_EXPOSURE_USD:
            continue
        pos = dict(key=key, side=side, entry=px, t0=now,
                   entry_fee_c=fc, underlying=under, size_usd=size_usd)
        st["positions"][key] = pos
        log_trade(dict(ts=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                       key=key, side=side, action="OPEN", entry=px,
                       gross_edge_c=round(g, 2), entry_fee_c=fc))
        events.append(f"开仓 {key[:30]} {side} @{px:.3f} (毛edge {g:.1f}¢)")

    save_state(st)
    pos_txt = ", ".join(f"{k[:18]}:{p['side']}" for k, p in st["positions"].items()) or "(空仓)"
    print(f"[{time.strftime('%H:%M:%SZ', time.gmtime())}] 纸面: {len(snaps)}桶 | "
          f"持仓{len(st['positions'])} [{pos_txt}] | 今日 {st['n_trades']}笔 "
          f"累计PnL {st['day_pnl']:+.2f}$")
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
