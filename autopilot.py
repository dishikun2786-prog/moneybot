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
DUAL_SYMS = ("BTCUSDT", "ETHUSDT", "XAUTUSDT", "SOLUSDT", "NEARUSDT", "XRPUSDT")  # carry 套利现货池
NATIVE_CORE = ("BTCUSDT", "ETHUSDT", "XAUUSDT", "XAGUSDT", "SOLUSDT", "NEARUSDT", "XRPUSDT")  # 原生永续池(动态扩)

DEFAULT_RISK = {"max_positions": 3, "notional": 15.0, "daily_loss_cap": 5.0,
                "theta": 5.0, "freq_min": 5, "auth_level": "A", "live": False,
                "min_hold_min": 15}
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


def native_pool():
    """原生方向可托管池: 实时盘口标的 ∩ 原生白名单 (上线几支自动几支)"""
    try:
        ob = json.load(open(os.path.join(BASE, "logs", "orderbook.json"), encoding="utf-8"))
        px = list((ob.get("px") or {}).keys())
        if not px:
            px = list((ob.get("books") or {}).keys())
    except Exception:
        px = list(NATIVE_CORE)
    try:
        from paper_ops import _native_allowed
        allowed = [s for s in px if _native_allowed(s)]
    except Exception:
        allowed = [s for s in px if s in NATIVE_CORE]
    return [s for s in allowed if s] or list(NATIVE_CORE)


def resolve_symbols(scope, mode="carry"):
    """scope: 'all' | ['BTCUSDT',...] → 合法标的列表 (native 用原生动态池)"""
    if scope == "all":
        return native_pool() if mode == "native" else list(DUAL_SYMS)
    pool = NATIVE_CORE if mode == "native" else DUAL_SYMS
    syms = []
    for s in (scope or []):
        s = str(s).upper()
        if (s in pool or (mode == "native" and s in native_pool())) and s not in syms:
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


def create_task(uid, scope, risk_overrides=None, mode="carry"):
    """创建托管任务: scope='all' 或标的列表; 同标的冲突自动排除; mode=carry|native"""
    syms = resolve_symbols(scope, mode)
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
    if mode not in ("carry", "native"):
        mode = "carry"
    if not (1 <= float(risk["notional"]) <= 50):
        return {"ok": False, "error": "名义金额须 1-50 USDT"}
    if not (1 <= int(risk["max_positions"]) <= 10):
        return {"ok": False, "error": "单标的持仓上限须 1-10"}
    if not (0.5 <= float(risk["daily_loss_cap"]) <= 50):
        return {"ok": False, "error": "日损熔断须 0.5-50 USDT"}
    t = {"id": f"ap{int(time.time()*1000)}", "scope": "all" if scope == "all" else kept,
         "symbols": kept, "risk": risk, "mode": mode, "status": "running",
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
            (f" · 模式 {'原生方向' if mode == 'native' else '基差套利'}" +
             (f" (冲突自动排除: {dropped})" if dropped else "")))}


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
                    if k in ("live", "min_hold_min", "max_positions", "notional",
                             "daily_loss_cap", "theta", "freq_min", "auth_level"):
                        if k == "auth_level" and v not in AUTH_LEVELS:
                            return {"ok": False, "error": "授权分级仅 A/B"}
                        t.setdefault("risk", {})[k] = v
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


def _tg_alert(text):
    """M-P4: 熔断/关键事件 TG 告警 (复用 watchdog 的 alert_config.json)"""
    try:
        cfg = json.load(open(os.path.join(BASE, "alert_config.json"), encoding="utf-8"))
        if not cfg.get("bot_token"):
            return
        import urllib.request
        url = f"https://api.telegram.org/bot{cfg['bot_token']}/sendMessage"
        data = json.dumps({"chat_id": int(cfg["chat_id"]), "text": text}).encode()
        urllib.request.urlopen(urllib.request.Request(url, data=data,
            headers={"Content-Type": "application/json"}), timeout=10)
    except Exception:
        pass


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
        # 审查修复: day_pnl 跨天未重置则视为 0 (防止隔夜旧值误触发日损熔断)
        day_tag = time.strftime("%Y-%m-%d")
        if st.get("day") and st.get("day") != day_tag:
            return 0.0
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


def _native_positions(uid):
    """原生纸面持仓 → {sym: side}"""
    try:
        import tenants as _tn
        import paper_ops
        with _tn.tenant(uid):
            rows = paper_ops.native_positions()
        out = {}
        for r in (rows or []):
            if isinstance(r, dict) and r.get("symbol"):
                out[r["symbol"]] = r.get("side")
        return out
    except Exception:
        return {}


def _live_enabled(uid):
    """实盘开关硬闸: 与 /api/live/status 同源 — 有充值入账即 live_enabled (模型/任务 live 标志不可绕过)"""
    try:
        import live_exec
        st = live_exec.status(uid)
        return bool(st and st.get("live_enabled"))
    except Exception:
        return False


def _exec_native_open(uid, tid, sym, side, notional, conf, task, live):
    try:
        if live and not _live_enabled(uid):
            _log_action(uid, {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                              "uid": uid, "task_id": tid, "symbol": sym,
                              "event": "live_blocked", "reason": "实盘未开通(无 live_enabled)"})
            return
        if live:
            import live_exec
            r = live_exec.bybit_open_native(uid, {"symbol": sym, "side": side, "notional": notional})
        else:
            import tenants as _tn
            import paper_ops
            with _tn.tenant(uid):
                r = paper_ops.open_native(sym, side, notional)
        ok = not (isinstance(r, dict) and r.get("ok") is False)
        task["stats"]["actions_today"] = task["stats"].get("actions_today", 0) + 1
        task["stats"]["last_action"] = {"ts": time.strftime("%H:%M:%SZ", time.gmtime()),
                                        "sym": sym, "act": f"native_open_{side}", "ok": ok}
        _log_action(uid, {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                          "uid": uid, "task_id": tid, "symbol": sym, "event": f"native_open_{side}",
                          "conf": round(conf, 3), "notional": notional, "live": live,
                          "ok": ok, "result": str(r)[:200]})
    except Exception as e:
        _log_action(uid, {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                          "uid": uid, "task_id": tid, "symbol": sym, "event": "native_open_error",
                          "error": f"{type(e).__name__}: {str(e)[:120]}"})


def _exec_native_close(uid, tid, sym, reason, task, live):
    try:
        if live and not _live_enabled(uid):
            _log_action(uid, {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                              "uid": uid, "task_id": tid, "symbol": sym,
                              "event": "live_blocked", "reason": "实盘未开通(无 live_enabled)"})
            return
        if live:
            import live_exec
            r = live_exec.bybit_close_native(uid, {"symbol": sym})
        else:
            import tenants as _tn
            import paper_ops
            with _tn.tenant(uid):
                r = paper_ops.close_native(sym)
        ok = not (isinstance(r, dict) and r.get("ok") is False)
        task["stats"]["actions_today"] = task["stats"].get("actions_today", 0) + 1
        task["stats"]["last_action"] = {"ts": time.strftime("%H:%M:%SZ", time.gmtime()),
                                        "sym": sym, "act": "native_close", "ok": ok}
        _log_action(uid, {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                          "uid": uid, "task_id": tid, "symbol": sym, "event": "native_close",
                          "reason": reason, "live": live, "ok": ok, "result": str(r)[:200]})
    except Exception as e:
        _log_action(uid, {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                          "uid": uid, "task_id": tid, "symbol": sym, "event": "native_close_error",
                          "error": f"{type(e).__name__}: {str(e)[:120]}"})


def _native_pos_with_time(uid):
    """原生持仓 {sym: side} + {sym: t0} 两视图"""
    side, t0 = {}, {}
    for r in (_native_positions(uid) or []):
        if isinstance(r, dict) and r.get("symbol"):
            side[r["symbol"]] = r.get("side")
            if r.get("t0"):
                t0[r["symbol"]] = float(r["t0"])
    return side, t0


def _run_native_opens_closes(uid, tid, t, answers, g, risk_paused=False, day_pnl=0.0):
    """原生方向托管: 方向翻转平仓(带最小持有防抖) + 方向信号开仓 (风险暂停只禁开不禁平)"""
    rk = t.get("risk", {})
    max_pos = int(rk.get("max_positions", 3))
    conf_min = float(g.get("conf_min", 0.6))
    live = bool(rk.get("live", False))
    min_hold_s = max(0.0, float(rk.get("min_hold_min", 15)) * 60.0)
    acted = []
    pos, t0map = _native_pos_with_time(uid)
    syms = t.get("symbols", [])
    now = time.time()
    for s in list(pos.keys()):
        if s not in syms:
            continue
        a = answers.get(f"dir_{s}")
        nd = (a.get("choice") if isinstance(a, dict) else None) or "none"
        if nd not in ("long", "short") or nd != pos[s]:
            # 防抖: 开仓未满最小持有期则本周期不平, 留待下一周期
            _t0 = t0map.get(s)
            if _t0 and (now - _t0) < min_hold_s:
                _log_action(uid, {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                  "uid": uid, "task_id": tid, "symbol": s,
                                  "event": "skipped", "reason": f"防抖: 最小持有期未满({int(min_hold_s)}s)"})
                continue
            _exec_native_close(uid, tid, s, f"方向翻转→{nd}", t, live)
            pos.pop(s, None)
            acted.append({"task": tid, "symbol": s, "event": "native_close", "reason": f"dir→{nd}"})
    for s in syms:
        if s in pos:
            continue
        if risk_paused:
            _log_action(uid, {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                              "uid": uid, "task_id": tid, "symbol": s,
                              "event": "skipped", "reason": "风险暂停: 原生整体风险过高禁开新仓"})
            continue
        if len(pos) >= max_pos:
            _log_action(uid, {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                              "uid": uid, "task_id": tid, "symbol": s,
                              "event": "skipped", "reason": "闸3: 单标的持仓已达上限"})
            continue
        a = answers.get(f"dir_{s}")
        if not isinstance(a, dict) or a.get("choice") not in ("long", "short"):
            continue
        conf = float(a.get("confidence") or 0)
        if conf < conf_min:
            continue
        _exec_native_open(uid, tid, s, a["choice"], float(rk.get("notional", 15)), conf, t, live)
        pos[s] = a["choice"]
        acted.append({"task": tid, "symbol": s, "event": f"native_open_{a['choice']}", "conf": round(conf, 3)})
    t["stats"]["positions"] = pos
    t["stats"]["today_pnl"] = round(day_pnl, 4)
    t["stats"]["day"] = time.strftime("%Y-%m-%d", time.gmtime())
    return acted


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
    # 审查修复: 每日滚动重置 actions_today (原持续累加虚高)
    _today = time.strftime("%Y-%m-%d")
    _dirty = False
    for t in all_tasks:
        stt = t.get("stats") or {}
        if stt.get("day") != _today:
            stt["day"] = _today
            stt["actions_today"] = 0
            _dirty = True
    if _dirty:
        _save_tasks(uid, all_tasks)
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
    # M-D6: 有 native 托管任务时, 补跑一次方向决策 (复用富特征 state)
    native_tasks = [x for x in tasks if x.get("mode") == "native"]
    native_answers = answers
    if native_tasks:
        try:
            import typesafe_decision as _tsd
            _nsyms = []
            for _nt in native_tasks:
                for _s in _nt.get("symbols", []):
                    if _s not in _nsyms:
                        _nsyms.append(_s)
            _nr = _tsd.decide_native(_tsd.build_state(uid), _nsyms)
            if isinstance(_nr, dict) and "_error" not in _nr:
                native_answers = _nr.get("answers") or {}
        except Exception:
            pass
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
            _tg_alert(f"⚠️ AI托管熔断 uid={uid}: {t['stats']['halt_reason']} — 任务已自动暂停")
            acted.append({"task": tid, "event": "halt", "detail": t["stats"]["halt_reason"]})
            continue
        if t.get("mode") == "native":
            _ra = native_answers.get("risk_level", {}) if isinstance(native_answers, dict) else {}
            _rscore = _ra.get("score", 0) if isinstance(_ra, dict) else 0
            _rp = isinstance(_rscore, (int, float)) and _rscore >= g.get("risk_pause", 2.5)
            acted += _run_native_opens_closes(uid, tid, t, native_answers, g,
                                              risk_paused=_rp, day_pnl=day_pnl)
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
            if len(t_pos) >= total_max:  # 审查修复: 统计任务内持仓 (原 pos_now 全局跨任务污染)
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
    """取消任务并平掉其托管持仓 (默认自动平仓; 原生模式平原生仓, 套利模式平套利仓)"""
    tasks = _load_tasks(uid)
    t = next((x for x in tasks if x.get("id") == tid), None)
    if t is None:
        return {"ok": False, "error": f"任务不存在: {tid}"}
    closed = []
    if t.get("mode") == "native":
        live = bool((t.get("risk") or {}).get("live", False))
        pos = _native_positions(uid)
        for s in t.get("symbols", []):
            if s in pos:
                _exec_native_close(uid, tid, s, "取消托管自动平仓", t, live)
                closed.append(s)
    else:
        pos = _carry_positions(uid)
        for s in t.get("symbols", []):
            if s in pos:
                _exec_close(uid, tid, s, "取消托管自动平仓", t)
                closed.append(s)
    t["status"] = "cancelled"
    _save_tasks(uid, tasks)  # 审查修复: 保存同一引用 (原 _load_tasks 重读磁盘丢弃内存修改 → 取消失效)
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
