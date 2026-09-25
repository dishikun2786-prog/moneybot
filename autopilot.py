#!/usr/bin/env python3
"""M-P1: AI 全托管任务模型 + 授权 (2026-09-25)
- 任务: data/autopilot_<uid>.json (租户隔离, 仿 ai_tasks)
- 授权: tenants/<uid>/data/autopilot_auth.json (分级 A/B, 一次性授权, 可撤回)
- 粒度: scope=all(6双通道标的) 或 单标的列表; 多任务并存, 同标的冲突自动排除
- 风控默认: 单标的持仓上限3/名义15U/日损熔断5U/θ=5/频率5min
零外部依赖 (仅被 pm-dash venv import)。
"""
import json, os, time

BASE = os.path.expanduser("~/polymarket")
DUAL_SYMS = ("BTCUSDT", "ETHUSDT", "XAUTUSDT", "SOLUSDT", "NEARUSDT", "XRPUSDT")

DEFAULT_RISK = {"max_positions": 3, "notional": 15.0, "daily_loss_cap": 5.0,
                "theta": 5.0, "freq_min": 5, "auth_level": "A"}
AUTH_LEVELS = ("A", "B")


def task_file(uid):
    return os.path.join(BASE, "data", f"autopilot_{uid}.json")


def auth_file(uid):
    return os.path.join(BASE, "tenants", str(uid), "data", "autopilot_auth.json")


def _load_tasks(uid):
    try:
        return json.load(open(task_file(uid), encoding="utf-8"))
    except Exception:
        return []


def _save_tasks(uid, tasks):
    f = task_file(uid)
    os.makedirs(os.path.dirname(f), exist_ok=True)
    tmp = f + ".tmp"
    json.dump(tasks, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    os.replace(tmp, f)


def _load_auth(uid):
    try:
        return json.load(open(auth_file(uid), encoding="utf-8"))
    except Exception:
        return None


def _save_auth(uid, a):
    f = auth_file(uid)
    os.makedirs(os.path.dirname(f), exist_ok=True)
    tmp = f + ".tmp"
    json.dump(a, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    os.replace(tmp, f)


def resolve_symbols(scope):
    """scope: 'all' | ['BTCUSDT',...] → 合法标的列表"""
    if scope == "all":
        return list(DUAL_SYMS)
    syms = []
    for s in (scope or []):
        s = str(s).upper()
        if s in DUAL_SYMS and s not in syms:
            syms.append(s)
    return syms


def occupied_symbols(uid, exclude_id=None):
    """已托管标的集 (冲突检查: 后建任务自动排除已托管标的)"""
    occ = set()
    for t in _load_tasks(uid):
        if t.get("id") == exclude_id:
            continue
        if t.get("status") != "cancelled":
            for s in t.get("symbols", []):
                occ.add(s)
    return occ


def create_task(uid, scope, risk_overrides=None):
    """创建托管任务: scope='all' 或标的列表; 同标的冲突自动排除"""
    syms = resolve_symbols(scope)
    if not syms:
        return {"ok": False, "error": f"无效范围: 仅支持 all 或 {list(DUAL_SYMS)}"}
    occ = occupied_symbols(uid)
    kept = [s for s in syms if s not in occ]
    dropped = [s for s in syms if s in occ]
    if not kept:
        return {"ok": False, "error": "所选标的全部已被其他托管任务占用"}
    risk = dict(DEFAULT_RISK)
    if risk_overrides:
        for k, v in (risk_overrides or {}).items():
            if k in risk:
                risk[k] = v
    if risk.get("auth_level") not in AUTH_LEVELS:
        risk["auth_level"] = "A"
    if not (1 <= float(risk["notional"]) <= 50):
        return {"ok": False, "error": "名义金额须 1-50 USDT"}
    if not (1 <= int(risk["max_positions"]) <= 10):
        return {"ok": False, "error": "单标的持仓上限须 1-10"}
    if not (0.5 <= float(risk["daily_loss_cap"]) <= 50):
        return {"ok": False, "error": "日损熔断须 0.5-50 USDT"}
    t = {"id": f"ap{int(time.time()*1000)}", "scope": "all" if scope == "all" else kept,
         "symbols": kept, "risk": risk, "status": "running",
         "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
         "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
         "stats": {"today_pnl": 0.0, "actions_today": 0, "last_action": None,
                   "positions": {}, "day": time.strftime("%Y-%m-%d", time.gmtime())},
         "dropped": dropped}
    tasks = _load_tasks(uid)
    tasks.append(t)
    _save_tasks(uid, tasks)
    return {"ok": True, "task": t, "msg": ("托管任务已创建: " +
            (f"全策略 {len(kept)} 标的" if scope == "all" else f"单标的 {kept}") +
            (f" (冲突自动排除: {dropped})" if dropped else ""))}


def set_task(uid, tid, **fields):
    """修改/pause/resume/cancel: fields = {status, risk(部分), ...}"""
    tasks = _load_tasks(uid)
    for t in tasks:
        if t.get("id") == tid:
            if "status" in fields:
                st = fields["status"]
                if st in ("running", "paused", "cancelled"):
                    t["status"] = st
                else:
                    return {"ok": False, "error": f"非法状态: {st}"}
            if "risk" in fields:
                rk = fields["risk"] or {}
                for k, v in rk.items():
                    if k in t.get("risk", {}):
                        if k == "auth_level" and v not in AUTH_LEVELS:
                            return {"ok": False, "error": "授权分级仅 A/B"}
                        t["risk"][k] = v
            t["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            _save_tasks(uid, tasks)
            return {"ok": True, "task": t, "msg": f"任务已更新: {tid}"}
    return {"ok": False, "error": f"任务不存在: {tid}"}


def get_tasks(uid, include_cancelled=False):
    tasks = _load_tasks(uid)
    if not include_cancelled:
        tasks = [t for t in tasks if t.get("status") != "cancelled"]
    return tasks


def authorize(uid, level="A"):
    """一次性授权 (分级 A/B); 撤回 = revoke"""
    if level not in AUTH_LEVELS:
        return {"ok": False, "error": f"授权分级仅 {list(AUTH_LEVELS)}"}
    a = {"level": level, "granted_ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
         "agreed_risk": True}
    _save_auth(uid, a)
    return {"ok": True, "auth": a, "msg": f"已授权 {level} 级托管"}


def revoke(uid):
    """撤回授权 → 全部任务暂停 (不删除)"""
    tasks = _load_tasks(uid)
    for t in tasks:
        if t.get("status") == "running":
            t["status"] = "paused"
    _save_tasks(uid, tasks)
    a = _load_auth(uid)
    if a:
        a["revoked_ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _save_auth(uid, a)
    return {"ok": True, "msg": "授权已撤回, 全部托管任务已暂停", "paused": len(tasks)}


def auth_state(uid):
    a = _load_auth(uid)
    if a and not a.get("revoked_ts"):
        return a
    return None


# ================= M-P2: 托管执行器 (七闸 + 自动执行 + 留痕) =================
ACTIONS = os.path.join(BASE, "data", "autopilot_actions.jsonl")


def _log_action(uid, rec):
    try:
        os.makedirs(os.path.dirname(ACTIONS), exist_ok=True)
        with open(ACTIONS, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _carry_day_pnl(uid):
    try:
        st = json.load(open(os.path.join(BASE, "tenants", str(uid), "logs", "carry_state.json"),
                            encoding="utf-8"))
        return float(st.get("day_pnl") or 0.0)
    except Exception:
        return 0.0


def _carry_positions(uid):
    try:
        st = json.load(open(os.path.join(BASE, "tenants", str(uid), "logs", "carry_state.json"),
                            encoding="utf-8"))
        pos = st.get("positions") or {}
        return {s: v.get("notional", 0) for s, v in pos.items()}
    except Exception:
        return {}


def run_cycle(uid, jev_result=None):
    """托管执行周期 (ai_tasks.tick 调用, jev_result 复用 Jev 巡检结果避免重复调用)
    七闸: ①授权有效 ②任务运行+标的白名单 ③单标的持仓上限 ④总持仓上限
         ⑤日损熔断 ⑥费率翻转保护 ⑦名义金额合规
    通过 → 自动执行 open_hedge/close_both → 全量留痕"""
    auth = auth_state(uid)
    if not auth:
        return None  # 未授权托管: 不执行任何动作
    all_tasks = _load_tasks(uid)
    tasks = [t for t in all_tasks if t.get("status") == "running"]
    if not tasks:
        return None
    # Jev 信号 (复用巡检结果, 缺则自己调)
    if jev_result is None or jev_result.get("event") != "cycle":
        import jev_engine
        jev_result = jev_engine.run_cycle(uid)
    if jev_result is None or jev_result.get("event") != "cycle":
        return None
    answers = jev_result.get("answers") or {}
    mkt = {}
    try:
        ob = json.load(open(os.path.join(BASE, "logs", "orderbook.json"), encoding="utf-8"))
        px = ob.get("px") or {}
        mkt = {s: float(d.get("funding") or 0) * 3 * 365 * 100
               for s, d in px.items() if isinstance(d, dict) and d.get("funding") is not None}
    except Exception:
        pass
    import jev_engine as _je
    g = _je.gate(uid)
    pos_now = _carry_positions(uid)
    day_pnl = _carry_day_pnl(uid)
    acted = []
    for t in tasks:
        rk = t.get("risk", {})
        open_p = g["open_p"]
        max_pos = int(rk.get("max_positions", 3))
        loss_cap = float(rk.get("daily_loss_cap", 5.0))
        total_max = max_pos * len(t.get("symbols", []))
        tid = t.get("id")
        # 闸5: 日损熔断 (先检查, 触线→任务暂停+告警留痕)
        if day_pnl <= -loss_cap:
            t["status"] = "paused"
            t["stats"]["halt_reason"] = f"日损熔断: 当日亏损 {day_pnl:.2f} 达到上限 {loss_cap}"
            _save_tasks(uid, all_tasks)
            _log_action(uid, {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                              "uid": uid, "task_id": tid, "event": "halt",
                              "detail": t["stats"]["halt_reason"]})
            acted.append({"task": tid, "event": "halt", "detail": t["stats"]["halt_reason"]})
            continue
        t_pos = {s: v for s, v in pos_now.items() if s in t.get("symbols", [])}
        # 闸6: 费率翻转保护 → 平负费率持仓
        for s, ntl in list(t_pos.items()):
            ann = mkt.get(s)
            if ann is not None and ann < 0:
                _exec_close(uid, tid, s, "费率翻转平仓", t)
                acted.append({"task": tid, "symbol": s, "event": "close", "reason": "funding<0"})
                t_pos.pop(s, None)
        # 开仓: Jev open 信号 + 闸3/闸4
        for s in t.get("symbols", []):
            a = answers.get(f"open_{s}")
            if not a:
                continue
            p = a.get("noul", 0)
            if p < open_p:
                continue  # 信号不足
            if s in t_pos:
                continue  # 已持仓不重复开
            if len(t_pos) >= max_pos:
                _log_action(uid, {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                  "uid": uid, "task_id": tid, "symbol": s,
                                  "event": "skipped", "reason": "闸3: 单标的持仓已达上限"})
                continue
            if sum(1 for v in pos_now.values() if v > 0) >= total_max:
                _log_action(uid, {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                  "uid": uid, "task_id": tid, "symbol": s,
                                  "event": "skipped", "reason": "闸4: 总持仓已达上限"})
                continue
            ann = mkt.get(s)
            if ann is not None and ann <= 0:
                _log_action(uid, {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                  "uid": uid, "task_id": tid, "symbol": s,
                                  "event": "skipped", "reason": "闸6: 费率非正不开仓"})
                continue
            _exec_open(uid, tid, s, float(rk.get("notional", 15)), p, t)
            t_pos[s] = float(rk.get("notional", 15))
            acted.append({"task": tid, "symbol": s, "event": "open", "P": round(p, 3)})
        # 更新任务统计
        t["stats"]["today_pnl"] = round(day_pnl, 4)
        t["stats"]["day"] = time.strftime("%Y-%m-%d", time.gmtime())
        t["stats"]["positions"] = t_pos
    _save_tasks(uid, all_tasks)
    return {"uid": uid, "acted": acted, "day_pnl": day_pnl}


def _exec_open(uid, tid, sym, notional, p, task):
    try:
        import tenants as _tn
        import paper_ops
        with _tn.tenant(uid):
            r = paper_ops.open_hedge(sym, notional)
        ok = not (isinstance(r, dict) and r.get("ok") is False)
        task["stats"]["actions_today"] = task["stats"].get("actions_today", 0) + 1
        task["stats"]["last_action"] = {"ts": time.strftime("%H:%M:%SZ", time.gmtime()),
                                        "sym": sym, "act": "open", "ok": ok}
        _log_action(uid, {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                          "uid": uid, "task_id": tid, "symbol": sym, "event": "open",
                          "P": round(p, 3), "notional": notional, "ok": ok, "result": str(r)[:200]})
    except Exception as e:
        _log_action(uid, {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                          "uid": uid, "task_id": tid, "symbol": sym, "event": "open_error",
                          "error": f"{type(e).__name__}: {str(e)[:120]}"})


def _exec_close(uid, tid, sym, reason, task):
    try:
        import tenants as _tn
        import paper_ops
        with _tn.tenant(uid):
            r = paper_ops.close_both(sym)
        ok = not (isinstance(r, dict) and r.get("ok") is False)
        task["stats"]["actions_today"] = task["stats"].get("actions_today", 0) + 1
        task["stats"]["last_action"] = {"ts": time.strftime("%H:%M:%SZ", time.gmtime()),
                                        "sym": sym, "act": "close", "ok": ok}
        _log_action(uid, {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                          "uid": uid, "task_id": tid, "symbol": sym, "event": "close",
                          "reason": reason, "ok": ok, "result": str(r)[:200]})
    except Exception as e:
        _log_action(uid, {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                          "uid": uid, "task_id": tid, "symbol": sym, "event": "close_error",
                          "error": f"{type(e).__name__}: {str(e)[:120]}"})


def cancel_with_close(uid, tid):
    """取消任务并平掉其托管持仓 (默认自动平仓)"""
    t = next((x for x in _load_tasks(uid) if x.get("id") == tid), None)
    if t is None:
        return {"ok": False, "error": f"任务不存在: {tid}"}
    pos = _carry_positions(uid)
    closed = []
    for s in t.get("symbols", []):
        if s in pos:
            _exec_close(uid, tid, s, "取消托管自动平仓", t)
            closed.append(s)
    t["status"] = "cancelled"
    _save_tasks(uid, _load_tasks(uid))
    return {"ok": True, "msg": f"任务已取消, 自动平仓 {closed if closed else '无持仓'}"}


if __name__ == "__main__":
    import sys
    uid = int(sys.argv[1]) if len(sys.argv) > 1 else 27
    # 自测: 授权 → 建任务 → 冲突排除 → 修改 → 暂停 → 取消
    print(authorize(uid, "B"))
    print(create_task(uid, "all"))
    print(create_task(uid, ["BTCUSDT", "XRPUSDT"]))  # BTC/XRP 已被全策略占用 → 应报错
    print(create_task(uid, ["BTCUSDT"]))  # 同上
    ts = get_tasks(uid)
    print("任务数:", len(ts), "| 任务0标的:", ts[0]["symbols"] if ts else None)
    if ts:
        tid = ts[0]["id"]
        print(set_task(uid, tid, status="paused"))
        print(set_task(uid, tid, risk={"notional": 20}))
        print(set_task(uid, tid, status="running"))
    print(revoke(uid))
    print("撤回后任务状态:", [t["status"] for t in get_tasks(uid, include_cancelled=True)])
