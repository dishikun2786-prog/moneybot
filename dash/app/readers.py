import csv
import json
import os
import secrets
import subprocess
import time
import duckdb
from . import config

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
        st = json.load(open(os.path.expanduser("~/polymarket/logs/paper_state.json")))
    except Exception:
        pass
    trades = []
    tp = os.path.expanduser("~/polymarket/logs/paper_trades.jsonl")
    if os.path.exists(tp):
        with open(tp) as f:
            lines = f.readlines()[-20:]
        trades = [json.loads(l) for l in lines if l.strip()]
    return dict(state=st, recent=trades)


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
    for name, path in (("state", "~/polymarket/logs/carry_state.json"),
                       ("trades", "~/polymarket/logs/carry_trades.jsonl")):
        p = os.path.expanduser(path)
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
        ["symbol", "ts", "spot", "perp_mark", "funding_rate", "basis_bp", "ann_pct"], r)) for r in rows],
        series=series, fund_hist=[dict(t=r[0], symbol=r[1], ann=r[2]) for r in fund_hist],
        state=st, trades=trades)


INITIAL_CAPITAL = 100.0  # 模拟盘初始资金
TOKENS_FILE = os.path.expanduser("~/polymarket/dash/share_tokens.json")


def _json(path, default=None):
    try:
        return json.load(open(os.path.expanduser(path)))
    except Exception:
        return default


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
    eqp = os.path.expanduser("~/polymarket/logs/equity_daily.jsonl")
    if os.path.exists(eqp):
        with open(eqp) as f:
            for line in f:
                try:
                    eq.append(json.loads(line))
                except Exception:
                    pass
    pm_st = _json("~/polymarket/logs/paper_state.json") or {}
    cy_st = _json("~/polymarket/logs/carry_state.json") or {}
    pm_day = float(pm_st.get("day_pnl", 0.0))
    cy_day = float(cy_st.get("day_pnl", 0.0))
    realized_total = sum(float(e.get("total", 0.0)) for e in eq) + pm_day + cy_day
    unreal = _mtm_pm(pm_st) + _mtm_carry(cy_st)
    capital = round(INITIAL_CAPITAL + realized_total + unreal, 2)
    positions = []
    for key, p in (pm_st.get("positions") or {}).items():
        positions.append({"strat": "PM桶对冲", "key": key[:44], "side": p["side"],
                          "entry": p.get("entry"), "note": f"{p.get('size_usd', 0):.0f}$名义"})
    for sym, p in (cy_st.get("positions") or {}).items():
        positions.append({"strat": "现货×永续", "key": sym, "side": "多现货+空永续",
                          "entry": f"{p.get('spot_entry')}/{p.get('perp_entry')}",
                          "note": f"funding累计 {p.get('funding_acc', 0):.3f}$"})
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
    return dict(units=out, watchdog=wd, logs=logs)
