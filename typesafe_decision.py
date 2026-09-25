#!/usr/bin/env python3
"""M-D1: TypeSafe Jev 决策层升级 (2026-09-25)
- keep-alive HTTPS 连接池 (每线程复用连接, 免每次 TLS 握手 ~243ms)
- state 构建器: 回测指标 + 实时行情 + 持仓快照 → 精简 JSON
- 四类决策问题集: 开仓时机(noul) / 持仓动作(choice) / 风险等级(score) / 标的选择(choice)
- 回测健康度判断: 策略健康/衰减/失效 + 是否保留标的
- 全量留痕: data/typesafe_metrics.jsonl (延迟/状态码/token/问题数)
铁律: 窄决策 / 置信门控 / 超时降级 / 永不阻塞下单路径。
零外部依赖 (stdlib only), 系统 python3 可直接跑。
"""
import json, os, time, http.client, threading

BASE = os.path.expanduser("~/polymarket")
ENDPOINT_HOST = "api.typesafe.ai"
ENDPOINT_PATH = "/v1/systemone"
TIMEOUT = 3.0  # 秒; 超时即降级
METRICS = f"{BASE}/data/typesafe_metrics.jsonl"

_KEY = None


def _key():
    global _KEY
    if _KEY is None:
        with open(f"{BASE}/.typesafe_key") as f:
            _KEY = f.read().strip()
    return _KEY


# ── keep-alive 连接池 (thread-local) ────────────────────────────
_pool = threading.local()


def _conn():
    c = getattr(_pool, "conn", None)
    if c is None or c.sock is None:
        c = http.client.HTTPSConnection(ENDPOINT_HOST, timeout=TIMEOUT)
        _pool.conn = c
    return c


def _reset_conn():
    c = getattr(_pool, "conn", None)
    if c is not None:
        try:
            c.close()
        except Exception:
            pass
        _pool.conn = None


def _log(lat_ms, status, tokens, tag, nq=0):
    try:
        os.makedirs(os.path.dirname(METRICS), exist_ok=True)
        with open(METRICS, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                "lat_ms": lat_ms, "status": status, "tokens": tokens,
                                "tag": tag, "nq": nq}, ensure_ascii=False) + "\n")
    except Exception:
        pass


def system_one(state, questions, model="jev-latest"):
    """返回 answers/usage/_latency_ms; 失败时 _error。永不抛异常。"""
    body = json.dumps({"state": state, "model": model, "questions": questions},
                      ensure_ascii=False).encode()
    headers = {"Authorization": f"Bearer {_key()}", "Content-Type": "application/json"}
    t0 = time.time()
    try:
        conn = _conn()
        conn.request("POST", ENDPOINT_PATH, body=body, headers=headers)
        r = conn.getresponse()
        raw = r.read()
        lat = round((time.time() - t0) * 1000)
        if r.status != 200:
            _log(lat, r.status, 0, "http_err", len(questions))
            _reset_conn()
            return {"_error": f"HTTP{r.status}", "_latency_ms": lat}
        d = json.loads(raw)
        u = d.get("usage", {})
        _log(lat, 200, u.get("input_tokens", 0) + u.get("output_tokens", 0),
             "ok", len(questions))
        d["_latency_ms"] = lat
        return d
    except Exception as e:
        lat = round((time.time() - t0) * 1000)
        _reset_conn()
        _log(lat, 0, 0, type(e).__name__, len(questions))
        return {"_error": type(e).__name__, "_latency_ms": lat}


# ── 文件读取 helper (零依赖, 容错) ──────────────────────────────
def _read_json(path, default):
    try:
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return default


def _uid_dir(uid):
    if uid is None:
        return f"{BASE}/tenants"
    return f"{BASE}/tenants/{uid}"


def _backtest_file(uid):
    # 优先租户回测结果, fallback 全局
    p = f"{_uid_dir(uid)}/data/backtest_results.jsonl"
    return p if os.path.exists(p) else f"{BASE}/data/backtest_results.jsonl"


def _read_last_backtest(uid):
    """读最近一条回测结果 → 各标的指标 dict"""
    f = _backtest_file(uid)
    try:
        if os.path.exists(f):
            lines = open(f, encoding="utf-8").read().strip().splitlines()
            if lines:
                d = json.loads(lines[-1])
                m = d.get("metrics") or {}
                out = {}
                for sym, v in m.items():
                    if isinstance(v, dict):
                        out[sym] = {"轮数": v.get("轮数"), "胜率%": v.get("胜率%"),
                                    "年化%": v.get("年化%"), "总盈亏$": v.get("总盈亏$")}
                if out:
                    out["_theta"] = d.get("theta")
                    out["_ts"] = d.get("ts")
                return out
    except Exception:
        pass
    return {}


def _read_holdings(uid):
    """读租户 carry/paper 持仓快照"""
    out = {}
    for kind in ("carry", "paper"):
        ps = _read_json(f"{_uid_dir(uid)}/logs/{kind}_state.json", {})
        pos = ps.get("positions") or {}
        if pos:
            out[kind] = pos
    return out


def build_state(uid=None):
    """回测指标 + 实时行情 + 持仓 → 精简 JSON state (控制 token 成本)"""
    st = {}
    bt = _read_last_backtest(uid)
    if bt:
        st["回测"] = bt
    ob = _read_json(f"{BASE}/logs/orderbook.json", {})
    px = ob.get("px") or {}
    mkt = {}
    for sym, d in px.items():
        if not isinstance(d, dict):
            continue
        mkt[sym] = {
            "年化费率%": round(float(d.get("funding") or 0) * 3 * 365 * 100, 2),
            "未平仓量OI": d.get("OI"),
            "价格": d.get("last"),
        }
    if mkt:
        st["行情"] = mkt
    h = _read_holdings(uid)
    if h:
        st["持仓"] = h
    return st


# ── 决策问题集 (M-D2 用; M-D1 先验证可用性) ──────────────────────
def decide_open(state, symbols, theta=5.0):
    """开仓时机批量判断: 每标的 noul + 整体风险 score + 最优标的 choice"""
    qs = {}
    for s in symbols:
        qs[f"open_{s}"] = {
            "type": "noul",
            "instructions": f"标的 {s} 当前是否值得新开 carry 仓(买现货+空永续, 赚正资金费率)?",
            "criteria": {
                "true": f"年化资金费率显著高于入场阈值{theta}%, 基差方向有利且稳定, 无临近结算风险",
                "false": "费率低于阈值或已回落, 波动率异常, 或距结算点过近"}}
    qs["risk_level"] = {"type": "score", "instructions": "当前市场整体风险等级?",
                        "criteria": ["低", "正常", "偏高", "高危"]}
    qs["best_open"] = {"type": "choice", "instructions": "若必须选一个标的开仓, 选哪个?",
                       "criteria": {s: f"选 {s} 开仓" for s in symbols}}
    qs["best_open"]["criteria"]["none"] = "所有标的都不值得开仓"
    return system_one(state, qs)


def decide_position_action(state, symbol):
    """持仓动作: 持有/加仓/减仓/平仓/紧急平仓"""
    qs = {
        "action": {"type": "choice", "instructions": f"标的 {symbol} 的 carry 持仓下一步最优动作?",
                   "criteria": {
                       "hold": "继续持有, 费率仍有利",
                       "add": "费率/基差走强, 值得加仓",
                       "reduce": "费率走弱, 部分止盈减仓",
                       "close": "费率转负或基差不利, 平仓离场",
                       "urgent": "出现极端风险(爆仓风险/流动性枯竭), 立即平仓"}},
        "conf_check": {"type": "noul", "instructions": f"当前持仓 {symbol} 是否有需要立即处理的风险?"},
    }
    return system_one(state, qs)


def decide_backtest_health(state, symbols):
    """回测健康度: 每标的 score(健康/观察/衰减/失效) + 是否保留 noul"""
    qs = {}
    for s in symbols:
        qs[f"health_{s}"] = {"type": "score",
                             "instructions": f"标的 {s} 的回测策略健康度(看胜率/年化/样本轮数/近期漂移)?",
                             "criteria": ["失效", "衰减", "观察", "健康"]}
        qs[f"keep_{s}"] = {"type": "noul",
                           "instructions": f"标的 {s} 是否应继续保留在策略池?"}
    return system_one(state, qs)


def vol_regime(stats):
    """兼容旧集成点3: 波动率 regime"""
    qs = {
        "regime": {"type": "score", "instructions": "当前波动率处于什么档位?",
                   "criteria": ["平静", "常态", "抬升", "极端"]},
        "reduce": {"type": "noul", "instructions": "当前是否应降低整体敞口?"},
    }
    return system_one({"统计": stats}, qs)


if __name__ == "__main__":
    import sys
    uid = int(sys.argv[1]) if len(sys.argv) > 1 else None
    st = build_state(uid)
    print("=== state 快照 (前 500 字符) ===")
    print(json.dumps(st, ensure_ascii=False)[:500])
    print()
    # 实测1: 批量开仓判断 (6 双通道标的)
    syms = ["BTCUSDT", "ETHUSDT", "XAUTUSDT", "SOLUSDT", "NEARUSDT", "XRPUSDT"]
    r = decide_open(st, syms, theta=5.0)
    print("=== 批量开仓判断 ===")
    if "_error" in r:
        print("ERROR:", r)
    else:
        for k, v in r["answers"].items():
            if k.startswith("open_"):
                print(f"  {k[5:]}: P(开仓)={v['noul']:.2f}")
            elif k == "risk_level":
                print(f"  风险等级: score={v['score']} conf={v.get('confidence')}")
            elif k == "best_open":
                print(f"  最优标的: {v['choice']} conf={v.get('confidence')} prob={v['probabilities'].get(v['choice'])}")
        print(f"  延迟: {r['_latency_ms']}ms | tokens: {r['usage']}")
    # 实测2: 回测健康度
    r2 = decide_backtest_health(st, syms)
    print("=== 回测健康度 ===")
    if "_error" not in r2:
        for k, v in r2["answers"].items():
            if k.startswith("health_"):
                print(f"  {k[7:]}: {v['score']} ({v['legend'][str(round(v['score']))]}) conf={v.get('confidence'):.2f}")
        print(f"  延迟: {r2['_latency_ms']}ms | tokens: {r2['usage']}")
    # 实测3: keep-alive 稳态延迟 (连发3次)
    print("=== keep-alive 稳态延迟 ===")
    for i in range(3):
        r3 = system_one("ping", {"alive": {"type": "noul", "instructions": "Is this request valid?"}})
        print(f"  第{i+1}次: {r3.get('_latency_ms')}ms {'ERROR:' + r3['_error'] if '_error' in r3 else ''}")
