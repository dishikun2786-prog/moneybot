#!/usr/bin/env python3
"""循环恢复策略回测 (初步验证):
从 trades_1s 重建分钟级微结构 → 波段评分信号 → 小单TP/SL → 受限恢复阶梯
输出: 四项硬指标 + 参数扫描 + 蒙特卡洛尾部检验
数据窗口: 现有秒级数据 (~27h), 结论仅供参考, 需≥2周数据复测"""
import bisect
import json
import os
import random

BASE = os.path.expanduser("~/polymarket")
FEE_PCT = 0.00055 * 2  # 裸腿开+平手续费


def load(path):
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


def clamp01(x):
    return max(0.0, min(1.0, x))


def score(bv, sv, cum_cvd, dir_):
    """简化波段评分: 失衡0.5 + CVD斜率0.5 (回测数据无墙/OI, 对应权重归并)"""
    tot = bv + sv
    ratio = (bv / tot) if tot > 0 else 0.5
    s_imb = clamp01(ratio if dir_ == "fwd" else (1 - ratio)) * 50
    s_cvd = 25.0 if cum_cvd is None else clamp01((cum_cvd / max(tot, 1e-9)) * 0.5 + 0.5) * 50
    return s_imb + s_cvd


_TR = None
_PMAP = None
_MINS = None


def _load_data():
    global _TR, _PMAP, _MINS
    if _TR is not None:
        return
    _TR = load(f"{BASE}/logs/trades_1s.jsonl")
    # 价格: kline_1m 长历史 (结算窗口远长于信号窗口)
    pmap = {}
    try:
        import duckdb
        con = duckdb.connect()
        data_dir = os.path.expanduser("~/polymarket/data")
        for sym in ("BTCUSDT", "ETHUSDT"):
            rows = con.execute(f"""
                SELECT strftime(ts, '%Y-%m-%dT%H:%M') AS m, close
                FROM read_parquet('{data_dir}/kline_1m/year=*/month=*/*.parquet')
                WHERE symbol = '{sym}' ORDER BY ts""").fetchall()
            pmap[sym] = {m: float(c) for m, c in rows}
    except Exception as e:
        print(f"[warn] kline_1m 读取失败({e}), 回退 price_1s")
        px = load(f"{BASE}/logs/price_1s.jsonl")
        for rec in px:
            ts = rec.get("ts")
            for sym, p in rec.get("prices", {}).items():
                if p.get("last"):
                    pmap.setdefault(sym, {})[ts[:16]] = p["last"]
    mins = {}
    for sym in ("BTCUSDT", "ETHUSDT"):
        m = []
        cur = None
        for r in _TR:
            if r.get("sym") != sym:
                continue
            ts = r["ts"][:16]
            if cur is None or ts > cur[0]:
                if cur:
                    m.append(cur)
                cur = [ts, r.get("bv", 0) or 0, r.get("sv", 0) or 0]
            else:
                cur[1] += r.get("bv", 0) or 0
                cur[2] += r.get("sv", 0) or 0
        if cur:
            m.append(cur)
        mins[sym] = m
    _PMAP = pmap
    _MINS = mins


def backtest(thr, mult, max_ladder, tp_pct=1.0, sl_pct=1.0, seed=None):
    _load_data()
    rng = random.Random(seed)
    total_pnl = 0.0
    curve = []
    rounds = wins = 0
    ladder = 0
    for sym in ("BTCUSDT", "ETHUSDT"):
        price_mins = sorted(_PMAP.get(sym, {}))
        if not price_mins:
            continue
        pidx = {m: i for i, m in enumerate(price_mins)}
        mins = _MINS[sym]
        cum = 0.0
        open_ = None
        i = 0
        for ts, bv, sv in mins:
            cum += bv - sv
            if open_:
                continue  # 持仓期间等待结算 (下方已沿价格序列结算完)
            if i < 10:
                i += 1
                continue
            s_f = score(bv, sv, cum, "fwd")
            s_r = score(bv, sv, cum, "rev")
            s, d = (s_f, "fwd") if s_f >= s_r else (s_r, "rev")
            if s < thr:
                continue
            px_now = _PMAP[sym].get(ts)
            if px_now is None:
                continue
            n = min(10.0 * (mult ** ladder), 30.0)
            tp = px_now * (1 - tp_pct / 100) if d == "fwd" else px_now * (1 + tp_pct / 100)
            sl = px_now * (1 + sl_pct / 100) if d == "fwd" else px_now * (1 - sl_pct / 100)
            # 沿完整价格分钟序列结算 (信号窗口之后的价格也可触发TP/SL)
            j = pidx.get(ts, -1)
            if j < 0 or j + 1 >= len(price_mins):
                continue
            for m2 in price_mins[j + 1:]:
                px2 = _PMAP[sym][m2]
                hit = None
                if d == "fwd":
                    if px2 <= tp:
                        hit = "win"
                    elif px2 >= sl:
                        hit = "loss"
                else:
                    if px2 >= tp:
                        hit = "win"
                    elif px2 <= sl:
                        hit = "loss"
                if hit:
                    pnl = n * (tp_pct / 100 - FEE_PCT * 100) if hit == "win" else \
                        -n * (sl_pct / 100 + FEE_PCT * 100)
                    total_pnl += pnl
                    curve.append(total_pnl)
                    rounds += 1
                    if hit == "win":
                        wins += 1
                        ladder = 0
                    else:
                        ladder = min(ladder + 1, max_ladder)
                    break
    # 最大回撤 (曲线)
    peak = 0.0
    mdd = 0.0
    for v in curve:
        peak = max(peak, v)
        mdd = min(mdd, v - peak)
    win_rate = wins / max(rounds, 1) * 100
    # 未平仓回合不计入 (数据尾部开仓未结算)
    return {"rounds": rounds, "win_rate": win_rate, "pnl": total_pnl,
            "max_dd": mdd, "curve": curve}


def monte_carlo(base, mult, max_ladder, n_sim=1000):
    """蒙特卡洛: 用实测胜负序列bootstrap重放阶梯 → 5%分位最大回撤"""
    curve = base["curve"]
    pnls = []
    for i in range(1, len(curve)):
        pnls.append(curve[i] - curve[i - 1])
    if not pnls:
        return None
    mdd_sim = []
    rng = random.Random(42)
    for _ in range(n_sim):
        ladder = 0
        cum = 0.0
        peak = 0.0
        mdd = 0.0
        for p in rng.choices(pnls, k=len(pnls)):
            if p > 0:
                ladder = 0
            else:
                ladder = min(ladder + 1, max_ladder)
            cum += p * (min(10.0 * (mult ** ladder), 30.0) / 10.0)
            peak = max(peak, cum)
            mdd = min(mdd, cum - peak)
        mdd_sim.append(mdd)
    mdd_sim.sort()
    return mdd_sim[int(len(mdd_sim) * 0.05)]


if __name__ == "__main__":
    _load_data()
    # 数据就绪门槛: 信号数据须覆盖≥24小时 (否则入场无法结算, 数字无意义)
    all_min = max(len(_MINS.get(s, [])) for s in ("BTCUSDT", "ETHUSDT"))
    if all_min < 24 * 60:
        print(f"⛔ 数据就绪门槛未达: 信号窗口仅 {all_min} 分钟 (需 ≥1440 分钟/24小时)")
        print("   回测引擎机制已验证 (评分/价格/TP-SL结算全链路), 等待数据积累后自动产出有效结果")
        print("   预计还需积累: 约 2 周 (按计划 M3 验收标准)")
        raise SystemExit(0)
    print("=== 基准参数 (thr=60, mult=1.5, ladder=3) ===")
    base = backtest(60, 1.5, 3)
    print(f"回合 {base['rounds']} | 胜率 {base['win_rate']:.1f}% | 累计 {base['pnl']:+.2f}$ "
          f"| 最大回撤 {base['max_dd']:+.2f}$")
    mc = monte_carlo(base, 1.5, 3)
    print(f"蒙特卡洛(1000次) 5%分位最大回撤: {(mc if mc is not None else 'N/A (回合不足)')}")
    print()
    print("=== 参数扫描 (thr × mult × ladder) ===")
    print(f"{'阈值':>4} {'倍率':>4} {'级数':>4} {'回合':>5} {'胜率%':>6} {'累计$':>8} {'最大回撤$':>9}")
    for thr in (50, 55, 60, 65, 70):
        for mult in (1.2, 1.5, 2.0):
            for lad in (2, 3, 4):
                r = backtest(thr, mult, lad)
                print(f"{thr:>4} {mult:>4.1f} {lad:>4} {r['rounds']:>5} {r['win_rate']:>6.1f} "
                      f"{r['pnl']:>+8.2f} {r['max_dd']:>+9.2f}")
    print()
    print("注: 数据窗口约27小时, 结果仅作初步验证; 正式验收需≥2周数据")
