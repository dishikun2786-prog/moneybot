import csv
import json
import os
import re
import secrets
import subprocess
import time
import duckdb
from . import config
import sys
import os as _os
sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))))
import tenants  # noqa: E402

FV = f"{config.DATA}/fv_snapshot/year=*/month=*/*.parquet"
LAT = f"{config.DATA}/latency/year=*/month=*/*.parquet"

_CACHE = {}
_COLS = ["event", "market", "dir", "strike", "spot", "T_days", "iv", "deribit_exp",
         "best_bid", "best_ask", "bid_size", "ask_size", "model_p",
         "edge_buy_c", "edge_sell_c", "pm_impl_sigma", "hedge_per_1k"]


def _q(sql, params=None):
    con = duckdb.connect()
    try:
        cur = con.execute(sql, params or [])
        return cur.fetchall()
    finally:
        con.close()


def cached(key, ttl, fn):
    now = time.time()
    ent = _CACHE.get(key)
    if ent and now - ent[0] < ttl:
        return ent[1]
    v = fn()
    _CACHE[key] = (now, v)
    return v


def summary():
    r = _q(f"SELECT max(ts) FROM read_parquet('{FV}')")
    last = r[0][0]
    if not last:
        return {"ok": False, "err": "无数据"}
    r = _q(f"""SELECT count(*),
        coalesce(sum(CASE WHEN edge_buy_c>5 THEN 1 ELSE 0 END),0),
        coalesce(sum(CASE WHEN edge_sell_c>5 THEN 1 ELSE 0 END),0)
        FROM read_parquet('{FV}') WHERE ts = TIMESTAMP '{last}'""")[0]
    spots = {}
    for sym, cur in (("Bitcoin", "BTC"), ("Ethereum", "ETH")):
        v = _q(f"SELECT max(spot) FROM read_parquet('{FV}') WHERE ts = TIMESTAMP '{last}' AND event ILIKE '%{sym}%'")
        spots[cur] = v[0][0]
    lat = _q(f"SELECT round(median(cycle_ms)), round(quantile_cont(cycle_ms,0.99)) FROM read_parquet('{LAT}')")[0]
    return dict(ok=True, last_ts=str(last), buckets=r[0], buy_sig=r[1], sell_sig=r[2],
                btc=spots.get("BTC"), eth=spots.get("ETH"), p50_ms=lat[0], p99_ms=lat[1])


def markets(sort="edge", q=""):
    rows = _q(f"""
      WITH latest AS (
        SELECT {','.join(_COLS)}, row_number() OVER (PARTITION BY event, market ORDER BY ts DESC) rn
        FROM read_parquet('{FV}') WHERE best_bid>0 AND best_ask>0 AND best_ask>=best_bid)
      SELECT {','.join(_COLS)} FROM latest WHERE rn=1
    """)
    out = [dict(zip(_COLS, r)) for r in rows]
    for d in out:
        d["market_zh"] = zh_market(d["event"], d["market"])
        d["dir_zh"] = "涨向" if d["dir"] == "up" else "跌向"
    if q:
        out = [d for d in out if q.lower() in (d["event"] or "").lower()
               or q.lower() in (d["market"] or "").lower()]
    if sort == "edge":
        out.sort(key=lambda d: -(max(d["edge_buy_c"] or 0, d["edge_sell_c"] or 0)))
    elif sort == "bias":
        out.sort(key=lambda d: -abs((d["model_p"] or 0) - ((d["best_bid"] or 0) + (d["best_ask"] or 0)) / 2))
    elif sort == "strike":
        out.sort(key=lambda d: (d["dir"] or "", d["strike"] or 0))
    return {"n": len(out), "rows": out}


def series(key, hours=24):
    ev, mk = key.split("|", 1)
    h = min(int(hours), 72)
    rows = _q(f"""SELECT strftime(ts,'%Y-%m-%dT%H:%M') AS t, model_p, best_bid, best_ask,
                  edge_buy_c, edge_sell_c FROM read_parquet('{FV}')
                  WHERE event = ? AND market = ? AND ts > now() - INTERVAL {h} HOUR
                  ORDER BY ts""", [ev, mk])
    return {"key": key,
            "points": [dict(t=r[0], model=r[1], bid=r[2], ask=r[3], eb=r[4], es=r[5]) for r in rows]}


def analysis():
    cal = _q(f"""SELECT dir, round(median(pm_impl_sigma/NULLIF(iv,0)),3) AS ratio, count(*) n
        FROM read_parquet('{FV}')
        WHERE iv>0 AND pm_impl_sigma>0 AND pm_impl_sigma<2 AND ts > now() - INTERVAL 48 HOUR
        GROUP BY dir""")
    bt = []
    bt_path = os.path.expanduser("~/polymarket/logs/backtest_results.csv")
    if os.path.exists(bt_path):
        with open(bt_path) as f:
            bt = list(csv.DictReader(f))[:30]
    return dict(cal=[dict(dir=r[0], ratio=r[1], n=r[2]) for r in cal],
                applied={"up": 1.046, "down": 0.896}, backtest=bt)


def paper():
    st = {}
    try:
        st = json.load(open(tenants.state("paper")))
    except Exception:
        pass
    trades = []
    tp = tenants.trades("paper")
    if os.path.exists(tp):
        with open(tp) as f:
            lines = f.readlines()[-20:]
        trades = [json.loads(l) for l in lines if l.strip()]
    return dict(state=st, recent=trades)


_MONTHS_ZH = {"January": "1月", "February": "2月", "March": "3月", "April": "4月",
              "May": "5月", "June": "6月", "July": "7月", "August": "8月",
              "September": "9月", "October": "10月", "November": "11月", "December": "12月"}
_ACT_ZH = {"OPEN_BOTH_LEGS": "双腿开仓", "CLOSE_PERP_LEG(单边平仓)": "单边平仓(合约腿)",
           "FUNDING_SETTLE": "资金费结算", "FUNDING_SETTLE_NAKED": "资金费结算(裸腿)",
           "REUSE_SPOT_LEG": "现货腿复用",
           "CLOSE_SPOT_LEG": "平现货腿", "OPEN": "开仓", "CLOSE": "平仓",
           "MANUAL_OPEN_HEDGE": "手动·一键对冲开仓",
           "MANUAL_CLOSE_PERP_LEG": "手动·平合约腿",
           "MANUAL_CLOSE_ORPHAN": "手动·平孤儿现货腿",
           "MANUAL_CLOSE_BOTH": "手动·全平双腿",
           "MANUAL_SPOT_TO_NAKED": "手动·平现货腿转裸仓",
           "MANUAL_CLOSE_NAKED": "手动·平裸腿",
           "MANUAL_NAKED_TP": "裸腿·止盈平仓",
           "MANUAL_NAKED_SL": "裸腿·止损平仓",
           "MANUAL_CLOSE_PM": "手动·平PM仓位",
           "MANUAL_OPEN_PM": "手动·开PM仓位",
           "MANUAL_OPEN_NAKED": "手动·开裸腿仓"}
_SIDE_ZH = {"BUY": "买入", "SELL": "卖出"}
_STRAT_ZH = {"PM桶对冲": "预测市场对冲", "现货×永续": "现货×永续套利"}


def zh_market(event, market):
    """Polymarket 英文事件标题 → 中文展示名"""
    s = event or ""
    for en, zh in _MONTHS_ZH.items():
        s = s.replace(en, zh)
    s = s.replace("What price will ", "").replace(" hit ", "：触及 ")
    s = re.sub(r"：触及 in (\d{4})\?", r"：\1年内触及价位？", s)
    s = re.sub(r"：触及 (\d{1,2})月 (\d{1,2})-(\d{1,2})\?", r"：当月\2-\3日周内触及价位？", s)
    s = s.replace("Bitcoin", "比特币").replace("Ethereum", "以太坊")
    m2 = re.sub(r"\s+", " ", (market or "").replace("↓", "跌向").replace("↑", "涨向")).strip()
    return f"{s} {m2}".strip()


def carry():
    FV2 = f"{config.DATA}/carry_1m/year=*/month=*/*.parquet"
    rows = []
    try:
        rows = _q(f"""
          WITH latest AS (SELECT *, row_number() OVER (PARTITION BY symbol ORDER BY ts DESC) rn
            FROM read_parquet('{FV2}'))
          SELECT symbol, ts, spot, perp_mark, funding_rate, basis_mark_bp, ann_funding_pct
          FROM latest WHERE rn=1 ORDER BY ann_funding_pct DESC
        """)
    except Exception:
        pass
    series = {}
    try:
        for sym in ("BTCUSDT", "ETHUSDT"):
            pts = _q(f"""SELECT strftime(ts,'%H:%M') t, basis_mark_bp, ann_funding_pct
                FROM read_parquet('{FV2}') WHERE symbol='{sym}' AND ts > now() - INTERVAL 24 HOUR ORDER BY ts""")
            series[sym] = [dict(t=p[0], basis=p[1], ann=p[2]) for p in pts]
    except Exception:
        pass
    fund_hist = []
    try:
        fund_hist = _q(f"""SELECT strftime(ts,'%m-%d %H:%M') t, symbol, round(funding_rate*3*365*100,2) ann
            FROM read_parquet('{config.DATA}/carry_funding.parquet')
            WHERE ts > now() - INTERVAL 30 DAY ORDER BY ts DESC LIMIT 120""")
    except Exception:
        pass
    st, trades = {}, []
    for name, path in (("state", tenants.state("carry")),
                       ("trades", tenants.trades("carry"))):
        p = path
        try:
            if name == "state":
                st = json.load(open(p))
            else:
                with open(p) as f:
                    lines = f.readlines()[-20:]
                trades = [json.loads(l) for l in lines if l.strip()]
        except Exception:
            pass
    return dict(rows=[dict(zip(
        ["symbol", "ts", "spot", "perp_mark", "funding_rate", "basis_bp", "ann_pct"], r),
        symbol_zh="比特币" if r[0] == "BTCUSDT" else "以太坊") for r in rows],
        series=series, fund_hist=[dict(t=r[0], symbol=r[1], ann=r[2]) for r in fund_hist],
        state=st, trades=[dict(t, action_zh=_ACT_ZH.get(t.get("action", ""), t.get("action", "")))
                          for t in trades])


INITIAL_CAPITAL = 100.0  # 模拟盘初始资金
TOKENS_FILE = os.path.expanduser("~/polymarket/dash/share_tokens.json")


def _json(path, default=None):
    try:
        return json.load(open(os.path.expanduser(path)))
    except Exception:
        return default


def _tail_jsonl(path, n=400, max_bytes=512 * 1024):
    """轻量 tail 读 jsonl (从文件尾读, 避免大文件全量解析)"""
    p = os.path.expanduser(path)
    try:
        with open(p, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read().decode("utf-8", errors="ignore")
    except Exception:
        return []
    out = []
    for line in data.splitlines()[-n:]:
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def micro():
    """微结构指标 (基于 micro_1m 分钟级数据, tail 轻量读取)"""
    micro = _tail_jsonl("~/polymarket/logs/micro_1m.jsonl", 400)
    walls = _tail_jsonl("~/polymarket/logs/wall_events.jsonl", 800)
    out = {}
    for sym in ("BTCUSDT", "ETHUSDT"):
        mr = [l for l in micro if l.get("sym") == sym]
        wl = [l for l in walls if l.get("sym") == sym]
        cvd_series = [[m.get("ts", "")[11:16], m.get("last"), m.get("cum_cvd")]
                      for m in mr[-60:]]
        oi_now = mr[-1].get("oi") if mr else None
        oi_prev = mr[-6].get("oi") if len(mr) >= 6 else None
        oi_5m = None
        if oi_now and oi_prev:
            oi_5m = round((oi_now - oi_prev) / oi_prev * 100, 3)
        bv = sum(t.get("bv", 0) or 0 for t in mr[-120:])
        sv = sum(t.get("sv", 0) or 0 for t in mr[-120:])
        nb = sum(t.get("nb", 0) or 0 for t in mr[-120:])
        ns = sum(t.get("ns", 0) or 0 for t in mr[-120:])
        ratio = round(bv / (bv + sv) * 100, 1) if (bv + sv) else None
        out[sym] = {"cvd_series": cvd_series, "oi": oi_now,
                    "oi_val": mr[-1].get("oi_val") if mr else None,
                    "oi_5m_chg_pct": oi_5m, "taker_buy_pct_2h": ratio,
                    "n_buy_2h": nb, "n_sell_2h": ns,
                    "wall_appear_2h": sum(1 for w in wl if w.get("type") == "appear"),
                    "wall_vanish_2h": sum(1 for w in wl if w.get("type") == "vanish")}
    return out


def _mtm_pm(st):
    """PM桶纸面持仓按市场mid独立盯市"""
    pos = st.get("positions") or {}
    if not pos:
        return 0.0
    rows = _q(f"""WITH latest AS (
        SELECT event, market, best_bid, best_ask,
               row_number() OVER (PARTITION BY event, market ORDER BY ts DESC) rn
        FROM read_parquet('{FV}') WHERE best_bid>0 AND best_ask>0)
        SELECT event, market, (best_bid+best_ask)/2 AS mid FROM latest WHERE rn=1""")
    mid_map = {f"{r[0]}|{r[1]}": r[2] for r in rows}
    tot = 0.0
    for key, p in pos.items():
        mid = mid_map.get(key)
        if mid is None or not p.get("entry"):
            continue
        shares = p.get("size_usd", 10.0) / p["entry"]
        tot += (mid - p["entry"]) * shares if p["side"] == "BUY" else (p["entry"] - mid) * shares
    return tot


def _mtm_carry(st):
    """现货永续套利持仓按最新ticker盯市"""
    pos = st.get("positions") or {}
    if not pos:
        return 0.0
    rows = _q(f"""WITH latest AS (
        SELECT *, row_number() OVER (PARTITION BY symbol ORDER BY ts DESC) rn
        FROM read_parquet('{config.DATA}/carry_1m/year=*/month=*/*.parquet'))
        SELECT symbol, spot, perp_mark FROM latest WHERE rn=1""")
    m = {r[0]: (r[1], r[2]) for r in rows}
    tot = 0.0
    for sym, p in pos.items():
        cur = m.get(sym)
        if not cur:
            continue
        spot_now, perp_now = cur
        spot_pnl = (spot_now - p["spot_entry"]) / p["spot_entry"] * 10.0
        perp_pnl = (p["perp_entry"] - perp_now) / p["perp_entry"] * 10.0
        tot += spot_pnl + perp_pnl + p.get("funding_acc", 0.0)
    return tot


def pnl_overview():
    """模拟盘整体盈亏: 资金 = 初始100 + Σ每日已实现 + 今日已实现 + 未实现MTM"""
    eq = []
    eqp = tenants.equity_daily()
    if os.path.exists(eqp):
        with open(eqp) as f:
            for line in f:
                try:
                    eq.append(json.loads(line))
                except Exception:
                    pass
    pm_st = _json(tenants.state("paper")) or {}
    cy_st = _json(tenants.state("carry")) or {}
    pm_day = float(pm_st.get("day_pnl", 0.0))
    cy_day = float(cy_st.get("day_pnl", 0.0))
    realized_total = sum(float(e.get("total", 0.0)) for e in eq) + pm_day + cy_day
    unreal = _mtm_pm(pm_st) + _mtm_carry(cy_st)
    capital = round(INITIAL_CAPITAL + realized_total + unreal, 2)
    positions = []
    for key, p in (pm_st.get("positions") or {}).items():
        positions.append({"strat": _STRAT_ZH.get("PM桶对冲", "预测市场对冲"),
                          "key": zh_market(*key.split("|", 1))[:40],
                          "key_orig": key,
                          "side": _SIDE_ZH.get(p["side"], p["side"]),
                          "entry": p.get("entry"), "note": f"{p.get('size_usd', 0):.0f}$名义"})
    for sym, p in (cy_st.get("positions") or {}).items():
        sym_zh = "比特币" if sym == "BTCUSDT" else "以太坊"
        d = p.get("dir", "fwd")
        positions.append({"strat": _STRAT_ZH.get("现货×永续", "现货×永续套利"),
                          "key": f"{sym_zh}({sym}) {'正向' if d == 'fwd' else '反向'}套利",
                          "side": "买入现货+卖出合约" if d == "fwd" else "卖出现货+买入合约",
                          "entry": f"{p.get('spot_entry')}/{p.get('perp_entry')}",
                          "note": f"资金费累计 {p.get('funding_acc', 0):.3f}$",
                          "live": {"symbol": sym, "spot_entry": p.get("spot_entry"),
                                   "perp_entry": p.get("perp_entry"),
                                   "notional": float(p.get("notional", 10.0)),
                                   "dir": d}})
    for sym, p in (cy_st.get("naked") or {}).items():
        sym_zh = "比特币" if sym == "BTCUSDT" else "以太坊"
        d = p.get("dir", "fwd")
        positions.append({"strat": _STRAT_ZH.get("现货×永续", "现货×永续套利"),
                          "key": f"{sym_zh}({sym}) 裸{'空' if d == 'fwd' else '多'}仓",
                          "side": "裸空合约(手动方向仓)" if d == "fwd" else "裸多合约(手动方向仓)",
                          "entry": f"{p.get('perp_entry')}",
                          "note": f"止盈{p.get('tp')} / 止损{p.get('sl')} · 资金费累计{p.get('funding_acc', 0):.3f}$",
                          "live": {"symbol": sym, "perp_entry": p.get("perp_entry"),
                                   "notional": float(p.get("notional", 10.0)), "naked": True,
                                   "dir": d}})
    for sym, p in (cy_st.get("orphans") or {}).items():
        sym_zh = "比特币" if sym == "BTCUSDT" else "以太坊"
        d = p.get("dir", "fwd")
        positions.append({"strat": _STRAT_ZH.get("现货×永续", "现货×永续套利"),
                          "key": f"{sym_zh}({sym}) 孤儿{'多' if d == 'fwd' else '空'}现货腿",
                          "side": "仅现货(待处置)" if d == "fwd" else "仅空现货(待处置)",
                          "entry": f"{p.get('spot_entry')}",
                          "note": "合约腿已平 · 可手动平仓或等信号复用",
                          "live": {"symbol": sym, "spot_entry": p.get("spot_entry"),
                                   "notional": float(p.get("notional", 10.0)), "orphan": True,
                                   "dir": d}})
    return dict(capital=capital,
                realized_total=round(realized_total, 2),
                realized_today=round(pm_day + cy_day, 2),
                unrealized=round(unreal, 2),
                equity=eq,
                positions=positions,
                orphans=len(cy_st.get("orphans") or {}),
                pm_trades=int(pm_st.get("n_trades", 0)),
                cy_rounds=int(cy_st.get("n_rounds", 0)),
                ts=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))


def _tokens():
    try:
        return json.load(open(TOKENS_FILE))
    except Exception:
        return {}


def generate_share():
    tok = secrets.token_urlsafe(16)
    t = _tokens()
    t[tok] = {"created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    json.dump(t, open(TOKENS_FILE, "w"), ensure_ascii=False, indent=1)
    return tok


def valid_share(tok):
    return tok in _tokens()


def revoke_share(tok):
    t = _tokens()
    if tok in t:
        del t[tok]
        json.dump(t, open(TOKENS_FILE, "w"), ensure_ascii=False, indent=1)
        return True
    return False


def share_view():
    """分享页数据: 只读聚合 (无敏感信息)"""
    d = pnl_overview()
    return dict(capital=d["capital"], realized_total=d["realized_total"],
                realized_today=d["realized_today"], unrealized=d["unrealized"],
                equity=d["equity"], positions=d["positions"],
                orphans=d["orphans"], ts=d["ts"])


KLINE_IVS = ("1m", "5m", "15m", "1h", "4h", "D", "W", "M")

# 策略说明（策略面板展示）
STRATEGY_INFO = {
    "paper_pm": {"name": "预测市场对冲", "zh": "在 Polymarket 预测市场找「价格标错」的碰价期权桶：用数学模型算出桶的合理价格，市场价显著低于模型价时买入（反之卖出），赚价格回归的差价。模拟盘每桶名义 $10。"},
    "carry": {"name": "现货×永续套利", "zh": "同时买入现货+做空永续合约，价格涨跌互相抵消；真正赚的是合约「资金费率」（多头每8小时付给空头的利息）。资金费率年化超过阈值时入场持有，费率翻负时单边平仓锁定利润。"},
    "cycle": {"name": "循环恢复策略", "zh": "波段量能择时 + 小金额单边对冲 + 受限恢复阶梯：每回合等盘口微结构评分达标才开小单（强制止盈止损），亏损时按×倍率逐级恢复（级数封顶），盈利复位循环。独立预算核算。"},
}


def strategy():
    """当前策略参数 + 说明 (单一参数源 strategy_params.json, 租户隔离)"""
    params = {}
    try:
        params = json.load(open(tenants.params_file()))
    except Exception:
        pass
    zh = {k: v["zh"] for k, v in STRATEGY_INFO.items()}
    names = {k: v["name"] for k, v in STRATEGY_INFO.items()}
    return {"params": params, "zh": zh, "names": names,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


def cycle():
    """循环恢复策略状态 + 最近回合 (租户隔离)"""
    st = _json(tenants.cycle_state()) or {}
    rounds = _tail_jsonl(tenants.cycle_rounds(), 20)
    return {"state": st, "rounds": rounds}


_KLINE_CACHE = {}  # REST回源缓存: {(symbol,interval): (ts, bars)}


def klines(symbol="BTCUSDT", interval="15m", limit=300):
    """真实K线 → [{t,o,h,l,c,v}]
    优先级: ①桥内实时订阅缓存 (kline.<iv>.<sym>, 末根实时) ②BTC/ETH 本地 parquet ③REST 回源"""
    symbol = (symbol or "").upper()
    if interval not in KLINE_IVS:
        interval = "15m"
    limit = max(1, min(int(limit), 500))
    # ① 实时订阅缓存 (R4: 桥按需订阅 kline 通道, 500ms 落盘; 历史不足时 REST 补齐)
    live_bars = []
    try:
        iv_num = {"1m": "1", "5m": "5", "15m": "15", "1h": "60", "4h": "240",
                  "D": "D", "W": "W", "M": "M"}.get(interval, "15")
        klsnap = os.path.join(config.BASE, "logs", "kl_snap.json")
        if os.path.exists(klsnap) and time.time() - os.path.getmtime(klsnap) < 30:
            with open(klsnap, encoding="utf-8") as f:
                kd = json.load(f)
            ks = (kd.get(symbol) or {}).get(iv_num)
            if ks:
                for st in sorted(ks.keys()):
                    k = ks[st]
                    live_bars.append({"t": int(k.get("start", st)), "o": float(k["open"]),
                                      "h": float(k["high"]), "l": float(k["low"]),
                                      "c": float(k["close"]), "v": float(k.get("volume", 0))})
    except Exception:
        pass
    if symbol in ("BTCUSDT", "ETHUSDT"):
        rows = _q(f"""SELECT ts, open, high, low, close, volume
            FROM read_parquet('{config.DATA}/kline_{interval}/year=*/month=*/*.parquet')
            WHERE symbol = '{symbol}' ORDER BY ts DESC LIMIT {limit}""")
        rows.reverse()
        bars = [{"t": int(r[0].timestamp() * 1000), "o": r[1], "h": r[2],
                 "l": r[3], "c": r[4], "v": r[5]} for r in rows]
        if live_bars and bars:
            live_min = live_bars[0]["t"]
            hist = [b for b in bars if b["t"] < live_min]
            hist.extend([b for b in live_bars if b["t"] >= live_min])
            if hist:
                return {"symbol": symbol, "interval": interval,
                        "bars": hist[-limit:], "live": True}
        return {"symbol": symbol, "interval": interval, "bars": bars}
    # ---- 其他标的: Bybit v5 REST kline, TTL 60s ----
    key = (symbol, interval)
    now = time.time()
    hit = _KLINE_CACHE.get(key)
    if hit and now - hit[0] < 60:
        return {"symbol": symbol, "interval": interval,
                "bars": hit[1][-limit:], "cached": True}
    try:
        import urllib.request
        iv = {"1m": "1", "5m": "5", "15m": "15", "1h": "60", "4h": "240",
              "D": "D", "W": "W", "M": "M"}.get(interval, "15")
        url = (f"https://api.bybit.com/v5/market/kline?category=linear"
               f"&symbol={symbol}&interval={iv}&limit=200")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.loads(r.read().decode())
        lst = (d.get("result") or {}).get("list") or []
        bars = [{"t": int(it[0]), "o": float(it[1]), "h": float(it[2]),
                 "l": float(it[3]), "c": float(it[4]), "v": float(it[5])}
                for it in lst]
        bars.reverse()
        # R4: 实时订阅缓存存在时, 用 REST 补齐历史段 + 实时末段 (订阅初期 bar 数少)
        if live_bars:
            live_min = live_bars[0]["t"]
            hist = [b for b in bars if b["t"] < live_min]
            hist.extend([b for b in live_bars if b["t"] >= live_min])
            if hist:
                _KLINE_CACHE[key] = (now, hist)
                return {"symbol": symbol, "interval": interval,
                        "bars": hist[-limit:], "live": True}
        _KLINE_CACHE[key] = (now, bars)
        return {"symbol": symbol, "interval": interval, "bars": bars[-limit:]}
    except Exception as e:
        # 回源失败: 用旧缓存兜底
        if hit:
            return {"symbol": symbol, "interval": interval,
                    "bars": hit[1][-limit:], "cached": True}
        return {"symbol": symbol, "interval": interval, "bars": [], "error": str(e)[:80]}


def _norm_paper(t, strat):
    act = t.get("action", "")
    side = t.get("side", "")
    if act == "OPEN":
        act_zh = "买入开仓" if side == "BUY" else "卖出开仓"
    elif act == "CLOSE":
        act_zh = "平仓"
    else:
        act_zh = act
    note = f"毛价差 {t.get('gross_edge_c')}¢" if t.get("gross_edge_c") is not None else ""
    return {"ts": t.get("ts", ""), "strat": strat, "tag": "预测市场",
            "symbol": (t.get("key") or "")[:44], "action": act_zh,
            "side": side, "price": t.get("entry"),
            "pnl": t.get("pnl_usd", t.get("pnl")), "note": note}


def _norm_carry(t, strat):
    act = t.get("action", "")
    act_zh = _ACT_ZH.get(act, act)
    px = None
    if t.get("spot_entry") is not None and t.get("perp_entry") is not None:
        px = f"{t['spot_entry']}/{t['perp_entry']}"
    pnl = t.get("pnl_usd", t.get("pnl", t.get("amount")))
    note = f"年化 {t.get('ann_pct')}%" if t.get("ann_pct") is not None else ""
    return {"ts": t.get("ts", ""), "strat": strat, "tag": "现货套利",
            "symbol": t.get("symbol", ""), "action": act_zh,
            "side": None, "price": px, "pnl": pnl, "note": note}


def tape(limit=100):
    """两引擎交易事件流归一化 (尾部读取, 时间倒序)"""
    events = []
    for fname, fn in (("paper_trades.jsonl", _norm_paper), ("carry_trades.jsonl", _norm_carry)):
        p = os.path.join(tenants.logs(), fname)
        if not os.path.exists(p):
            continue
        with open(p, encoding="utf-8") as f:
            lines = f.readlines()[-limit:]
        for line in lines:
            try:
                t = json.loads(line)
                events.append(fn(t, "预测市场" if fname.startswith("paper") else "现货套利"))
            except Exception:
                continue
    events.sort(key=lambda e: e.get("ts", ""), reverse=True)
    return events[:limit]


def system():
    svc = ["pm-monitor", "pm-wss", "pm-dash"]
    tmr = ["pm-hedge", "pm-datawriter", "pm-watchdog"]
    out = {}
    for u in svc + tmr:
        p = subprocess.run(["systemctl", "is-active", u], capture_output=True, text=True, timeout=5)
        out[u] = p.stdout.strip()
    for u in tmr:
        p = subprocess.run(["systemctl", "is-active", u + ".timer"], capture_output=True, text=True, timeout=5)
        out[u + "_timer"] = p.stdout.strip()
    wd = {}
    try:
        wd = json.load(open(os.path.expanduser("~/polymarket/logs/watchdog_state.json")))
    except Exception:
        pass
    logs = {}
    for name, path in (("wss", "~/polymarket/logs/wss_stdout.log"),
                       ("monitor", "~/polymarket/logs/monitor_stdout.log")):
        p = os.path.expanduser(path)
        try:
            logs[name] = "".join(open(p, encoding="utf-8", errors="replace").readlines()[-40:])
        except Exception:
            logs[name] = ""
    # AI 操作审计 (最近12条, 租户隔离)
    ai_actions = []
    try:
        with open(tenants.ai_audit(), encoding="utf-8") as f:
            lines = f.readlines()[-12:]
        for line in lines:
            try:
                r = json.loads(line)
                ai_actions.append({"ts": r.get("ts", ""), "action": r.get("action", ""),
                                   "msg": r.get("msg", "") or r.get("type", "") or ""})
            except Exception:
                continue
        ai_actions.reverse()
    except Exception:
        pass
    return dict(units=out, watchdog=wd, logs=logs, ai_actions=ai_actions)
