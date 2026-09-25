#!/usr/bin/env python3
"""M-D2: Jev 决策引擎 (2026-09-25)
- run_cycle(uid): 5分钟决策巡检 (由 pm-dash tick 线程调用)
- 置信门控: noul 用概率距0.5的把握度, choice/score 用 confidence
- 超时降级: Jev 异常 → 留痕降级事件 → L1 确定性规则继续 (永不阻塞)
- 决策留痕: data/jev_decisions.jsonl 全量可回放
- L1 合并: 默认观测模式(只留痕分歧, 不拦截); AND 拦截开关由 L3 调参打开
零外部依赖。
"""
import json, os, time

import typesafe_decision as tsd

BASE = os.path.expanduser("~/polymarket")
DECISIONS = f"{BASE}/data/jev_decisions.jsonl"
DUAL_SYMS = ("BTCUSDT", "ETHUSDT", "XAUTUSDT", "SOLUSDT", "NEARUSDT", "XRPUSDT")

# 门控参数 (L3 DeepSeek 巡检可调, M-D3 接入三闸; 先写文件供调)
GATE_FILE = f"{BASE}/data/jev_gate.json"
DEFAULT_GATE = {
    "open_p": 0.65,        # noul P(开仓) ≥ 此值才发开仓信号
    "no_p": 0.65,          # P ≤ 1-此值 判为明确否定
    "conf_min": 0.6,       # choice/score confidence 下限
    "risk_pause": 2.5,     # 风险 score ≥ 此值 → 暂停新开仓建议
    "micro_pause": 0.8,    # M-D5: 微观结构风险分 ≥ 此值 → open 降级 uncertain (代码双重检验)
    "l1_and_mode": False,  # True = Jev 否定时拦截 L1 开仓 (默认观测)
}


def gate(uid=None):
    """租户级 jev_gate.json 优先 → 全局 fallback → 默认 (每次读文件=实时生效)"""
    files = [f"{BASE}/tenants/{uid}/data/jev_gate.json"] if uid else []
    files.append(GATE_FILE)
    for f in files:
        try:
            if os.path.exists(f):
                g = json.load(open(f, encoding="utf-8"))
                if isinstance(g, dict):
                    return {**DEFAULT_GATE, **g}
        except Exception:
            pass
    return dict(DEFAULT_GATE)


def current_theta(uid=None):
    """读租户策略参数 θ (无则默认5)"""
    for p in (f"{BASE}/tenants/{uid}/strategy_params.json" if uid else "",
              f"{BASE}/data/strategy_params.json"):
        try:
            if p and os.path.exists(p):
                d = json.load(open(p, encoding="utf-8"))
                th = d.get("theta") or d.get("theta_in_ann_pct")
                if th is not None:
                    return float(th)
        except Exception:
            pass
    return 5.0


def _append(rec):
    try:
        os.makedirs(os.path.dirname(DECISIONS), exist_ok=True)
        with open(DECISIONS, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _noul_signal(p, g):
    """noul 概率 → 信号 + 把握度 (noul 无 confidence, 用 |p-0.5| 度量)"""
    if p >= g["open_p"]:
        return "open", round((p - 0.5) * 2, 3)
    if p <= 1 - g["no_p"]:
        return "no", round((0.5 - p) * 2, 3)
    return "uncertain", round(1 - abs(p - 0.5) * 2, 3)


def run_cycle(uid=None):
    """一轮决策: state 快照 → 批量开仓判断 → 门控 → 留痕。返回记录 dict。"""
    g = gate(uid)
    st = tsd.build_state(uid)
    syms = [s for s in DUAL_SYMS if s in st.get("行情", {})]
    if not syms:
        return None
    theta = current_theta(uid)
    r = tsd.decide_open(st, syms, theta=theta)
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "uid": uid, "event": "cycle", "theta": theta, "syms": syms}
    if "_error" in r:
        # 超时降级: 留痕 → 返回降级记录, L1 规则不受影响
        rec.update({"event": "degraded", "error": r["_error"],
                    "latency_ms": r.get("_latency_ms")})
        _append(rec)
        return rec
    ans = r.get("answers", {})
    gates = {}
    for s in syms:
        a = ans.get(f"open_{s}")
        if not a:
            continue
        sig, grip = _noul_signal(a["noul"], g)
        gates[s] = {"P": round(a["noul"], 3), "signal": sig, "grip": grip}
    # M-D5: 确定性微观结构双重检验 (模型判断 + 代码门控, 铁律: 模型永不直接触钱)
    try:
        import microstructure as _ms
        micro = _ms.features(syms)
    except Exception:
        micro = {}
    micro_risk = {}
    for s, m in micro.items():
        mr = m.get("risk")
        if mr is None:
            continue
        micro_risk[s] = mr
        if gates.get(s, {}).get("signal") == "open" and mr >= g.get("micro_pause", 0.8):
            gates[s]["signal"] = "uncertain"
            gates[s]["blocked_by_micro"] = True
    opens = [s for s in syms if gates.get(s, {}).get("signal") == "open"]
    nos = [s for s in syms if gates.get(s, {}).get("signal") == "no"]
    uncertain = [s for s in syms if gates.get(s, {}).get("signal") == "uncertain"]
    risk_a = ans.get("risk_level", {})
    risk_score = risk_a.get("score", 0) if risk_a else 0
    risk_paused = isinstance(risk_score, (int, float)) and risk_score >= g["risk_pause"]
    best_a = ans.get("best_open", {})
    gates["_risk"] = {"score": risk_score, "conf": risk_a.get("confidence"),
                      "paused": risk_paused}
    if best_a:
        gates["_best"] = {"choice": best_a.get("choice"), "conf": best_a.get("confidence"),
                          "prob": (best_a.get("probabilities") or {}).get(best_a.get("choice"))}
    rec.update({"answers": ans, "gates": gates, "micro_risk": micro_risk, "signals": {
        "open": opens, "no": nos, "uncertain": uncertain,
        "risk_paused": risk_paused},
        "latency_ms": r.get("_latency_ms"),
        "tokens": r.get("usage")})
    _append(rec)
    return rec


if __name__ == "__main__":
    import sys
    uid = int(sys.argv[1]) if len(sys.argv) > 1 else None
    rec = run_cycle(uid)
    if rec is None:
        print("无行情数据, 跳过")
    elif rec.get("event") == "degraded":
        print(f"[降级] {rec['error']} ({rec.get('latency_ms')}ms) — L1 规则不受影响")
    else:
        print(f"周期决策 {rec['ts']} (θ={rec['theta']}, {rec.get('latency_ms')}ms, "
              f"{rec['tokens'].get('input_tokens',0)+rec['tokens'].get('output_tokens',0)} tokens)")
        for s, g in rec["gates"].items():
            if not s.startswith("_"):
                print(f"  {s}: P={g['P']} {g['signal']} (把握度{g['grip']})")
        print(f"  风险: score={rec['gates']['_risk']['score']} "
              f"conf={rec['gates']['_risk']['conf']} paused={rec['signals']['risk_paused']}")
        print(f"  信号: 开仓{rec['signals']['open']} 否定{rec['signals']['no']} "
              f"观望{rec['signals']['uncertain']}")
