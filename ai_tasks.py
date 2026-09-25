#!/usr/bin/env python3
"""AI 定时任务模块 (M-A4)
- 任务类型白名单: fee_watch(费率监控) / risk_scan(持仓风险扫描) / backtest_run(回测) / reminder(自定义提醒)
- 存储: data/ai_tasks_<uid>.json (租户隔离)
- 调度: pm-dash 进程内 tick 线程 (每60s扫一遍到期任务, 结果写 last_result)
- 任务无资金风险 (只读查询/提醒), 故 AI 创建直接生效, 无需人审闸
"""
import json
import os
import time

import tenants

_LAST_DAILY = ""

TASK_TYPES = {
    "fee_watch": {"zh": "费率监控", "min_interval_h": 1},
    "risk_scan": {"zh": "持仓风险扫描", "min_interval_h": 2},
    "backtest_run": {"zh": "回测运行", "min_interval_h": 6},
    "reminder": {"zh": "自定义提醒", "min_interval_h": 1},
}


def _file(uid=None):
    uid = tenants.current_uid() if uid is None else uid
    return os.path.join(tenants.base(uid), "data", f"ai_tasks_{uid}.json")


def _load(uid=None):
    try:
        with open(_file(uid), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _save(tasks, uid=None):
    os.makedirs(os.path.dirname(_file(uid)), exist_ok=True)
    with open(_file(uid), "w", encoding="utf-8") as f:
        json.dump(tasks, f, ensure_ascii=False, indent=2)


def create(uid, typ, name, interval_h, note="", threshold=None):
    """创建任务 (AI 工具直调)"""
    if typ not in TASK_TYPES:
        return {"ok": False, "msg": f"未知任务类型: {typ} (允许: {list(TASK_TYPES)})"}
    try:
        ih = float(interval_h)
    except Exception:
        return {"ok": False, "msg": "间隔小时数无效"}
    if ih < TASK_TYPES[typ]["min_interval_h"]:
        return {"ok": False, "msg": f"{TASK_TYPES[typ]['zh']}最小间隔 {TASK_TYPES[typ]['min_interval_h']} 小时"}
    if ih > 24 * 30:
        return {"ok": False, "msg": "间隔最长 720 小时(30天)"}
    tasks = _load(uid)
    if len([t for t in tasks if t.get("enabled", True)]) >= 5:
        return {"ok": False, "msg": "最多同时启用 5 个任务"}
    tid = f"t{int(time.time() * 1000)}"
    tasks.append({"id": tid, "type": typ, "name": (name or TASK_TYPES[typ]["zh"])[:40],
                  "interval_h": ih, "note": (note or "")[:120], "threshold": threshold,
                  "enabled": True, "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                  "next_run": time.time() + ih * 3600, "last_run": None, "last_result": None})
    _save(tasks, uid)
    return {"ok": True, "msg": f"任务已创建: {name} (每 {ih} 小时)", "task": tasks[-1]}


def list_tasks(uid=None):
    return _load(uid)


def toggle(uid, tid, enabled):
    tasks = _load(uid)
    for t in tasks:
        if t["id"] == tid:
            t["enabled"] = bool(enabled)
            if enabled:
                t["next_run"] = time.time() + float(t["interval_h"]) * 3600
            _save(tasks, uid)
            return {"ok": True, "msg": "已启用" if enabled else "已暂停"}
    return {"ok": False, "msg": "任务不存在"}


def delete(uid, tid):
    tasks = _load(uid)
    n = len(tasks)
    tasks = [t for t in tasks if t["id"] != tid]
    _save(tasks, uid)
    return {"ok": True, "msg": "已删除"} if len(tasks) < n else {"ok": False, "msg": "任务不存在"}


def _exec(uid, task):
    """执行器: 返回结果文本"""
    typ = task["type"]
    if typ == "reminder":
        return f"提醒: {task['note'] or task['name']}"
    if typ == "fee_watch":
        try:
            # 读 bridge 输出的 orderbook 快照 (px 帧含 fundingRate, 零重依赖)
            ob = os.path.join(tenants.base(uid), "logs", "orderbook.json")
            data = json.load(open(ob, encoding="utf-8"))
            px = data.get("px") or {}
            th = float(task.get("threshold") or 0)
            lines = []
            for sym, p in list(px.items())[:12]:
                fr = p.get("funding")
                if fr is None:
                    continue
                ann = float(fr) * 3 * 365 * 100  # 每8小时费率 → 年化%
                flag = " ⚠️超阈值" if (th > 0 and abs(ann) >= th) else ""
                lines.append(f"{sym} 年化费率 {ann:+.2f}%{flag}")
            if not lines:
                return "暂无费率数据 (行情通道未就绪)"
            return "费率快照:\n" + "\n".join(lines)
        except Exception as e:
            return f"费率查询失败: {str(e)[:100]}"
    if typ == "risk_scan":
        try:
            import sys
            sys.path.insert(0, os.path.join(tenants.ROOT, "dash"))
            with tenants.tenant(uid):
                import paper_ops as po
                pos = po.spot_positions() + [
                    dict(symbol=n["symbol"], pnl=n["pnl"], qty=n["qty"]) for n in po.native_positions()]
            if not pos:
                return "当前无持仓, 风险为空"
            worst = min(pos, key=lambda p: float(p.get("pnl") or 0))
            return (f"持仓 {len(pos)} 个; 最差: {worst['symbol']} 浮亏 {worst.get('pnl'):+.2f} USDT; "
                    f"注意止损纪律, 浮亏超过本金2%建议人工复核")
        except Exception as e:
            return f"风险扫描失败: {str(e)[:100]}"
    if typ == "backtest_run":
        try:
            import sys
            sys.path.insert(0, os.path.join(tenants.ROOT, "dash"))
            import ai_tools
            r = ai_tools.t_run_backtest({"theta": float(task.get("threshold") or 5)})
            if isinstance(r, dict) and r.get("error"):
                return f"回测失败: {r['error'][:100]}"
            s = json.dumps(r, ensure_ascii=False)
            return f"回测完成: {s[:200]}"
        except Exception as e:
            return f"回测失败: {str(e)[:100]}"
    return "未知任务类型"


def daily_report_file(uid):
    return os.path.join(tenants.base(uid), "data", f"ai_daily_{uid}.json")


def gen_daily_report(uid):
    """M-A6: 旗舰版每日巡检报告 (费率快照+持仓风险, 存 data/ai_daily_<uid>.json)"""
    parts = []
    # 费率快照
    try:
        ob = os.path.join(tenants.base(uid), "logs", "orderbook.json")
        data = json.load(open(ob, encoding="utf-8"))
        px = data.get("px") or {}
        rows = []
        for sym, p in list(px.items())[:8]:
            fr = p.get("funding")
            if fr is None:
                continue
            ann = float(fr) * 3 * 365 * 100
            rows.append({"标的": sym, "年化费率%": round(ann, 2)})
        parts.append({"费率快照": rows})
    except Exception:
        parts.append({"费率快照": "行情未就绪"})
    # 持仓风险
    try:
        with tenants.tenant(uid):
            import paper_ops as po
            spot = po.spot_positions()
            native = po.native_positions()
        tot = sum(float(s["pnl"]) for s in spot) + sum(float(n.get("pnl") or 0) for n in native)
        parts.append({"持仓": f"现货{len(spot)}个+合约纸面{len(native)}个, 总浮盈 {tot:+.2f} USDT"})
    except Exception as e:
        parts.append({"持仓": f"扫描失败: {str(e)[:80]}"})
    report = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "parts": parts}
    try:
        with open(daily_report_file(uid), "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    return report


def get_daily_report(uid):
    try:
        d = json.load(open(daily_report_file(uid), encoding="utf-8"))
        return d
    except Exception:
        return None


_LAST_JEV = 0.0
_LAST_INSPECT = 0.0
_FUND_SNAP = None  # funding 快照 {sym: 年化%} 供智能触发对比


def _fund_snapshot():
    """读 orderbook.json px 帧 → {sym: 年化费率%}"""
    try:
        ob = json.load(open(os.path.join(tenants.logs(), "orderbook.json"), encoding="utf-8"))
        px = ob.get("px") or {}
        return {s: float(d.get("funding") or 0) * 3 * 365 * 100
                for s, d in px.items() if isinstance(d, dict) and d.get("funding") is not None}
    except Exception:
        return {}


def _inspect_triggered(force=False):
    """M-D3: 巡检触发判定 — 1h定时 | funding 年化变化>30% | funding 正负翻转"""
    global _LAST_INSPECT, _FUND_SNAP
    if force:
        return True
    now = time.time()
    if now - _LAST_INSPECT >= 3600:
        return True
    snap = _fund_snapshot()
    if _FUND_SNAP is None:
        _FUND_SNAP = snap
        return False
    for s, v in snap.items():
        old = _FUND_SNAP.get(s)
        if old is None:
            continue
        if (old > 0 > v) or (old < 0 < v):  # 正负翻转
            _FUND_SNAP = snap
            return True
        if old != 0 and abs((v - old) / old) > 0.30:  # 30分钟内变化>30%
            _FUND_SNAP = snap
            return True
    _FUND_SNAP = snap
    return False


def hourly_inspect(uid):
    """M-D3: DeepSeek 小时巡检 → 白话巡检报告写入 ai_daily_<uid>.json 的 hourly 字段"""
    try:
        import ai_client
        from dash.app import funds
        # 巡检素材: Jev 留痕 + 行情快照 + 持仓
        jev_tail = []
        try:
            lines = open(os.path.join(os.path.expanduser("~/polymarket"), "data", "jev_decisions.jsonl"),
                         encoding="utf-8").read().strip().splitlines()
            for l in lines[-8:]:
                d = json.loads(l)
                if d.get("event") == "cycle" and d.get("uid") == uid:
                    jev_tail.append({"ts": d.get("ts"), "signals": d.get("signals")})
        except Exception:
            pass
        mkt = _fund_snapshot()
        pos = {}
        try:
            st = json.load(open(os.path.join(tenants.logs(uid), "carry_state.json"),
                                encoding="utf-8"))
            pos = st.get("positions") or {}
        except Exception:
            pass
        ap_tasks = []
        try:
            import autopilot as _ap
            for t in _ap.get_tasks(uid):
                ap_tasks.append({"id": t.get("id"), "status": t.get("status"),
                                 "symbols": t.get("symbols"), "risk": t.get("risk"),
                                 "stats": t.get("stats")})
        except Exception:
            pass
        prompt = ("你是量化巡检官。这是1小时巡检素材(JSON)。请用简体中文白话输出巡检报告, 300字内, "
                  "格式: ①市场状态(费率regime) ②Jev决策摘要(开仓信号/风险) ③持仓与托管任务风险 "
                  "④是否建议调整门控参数(如需调整, 用一行[SUGGEST]列出, 格式: [SUGGEST] 参数名=目标值, 参数仅限"
                  " open_p(0.5-0.95)/no_p(0.5-0.95)/conf_min(0.5-0.9)/risk_pause(1-4)/l1_and_mode(0或1))。\n"
                  f"素材: {json.dumps({'Jev最近决策': jev_tail, '各标的年化费率%': mkt, '持仓': pos, '托管任务': ap_tasks}, ensure_ascii=False)}")
        body = json.dumps({"model": ai_client.MODEL,
                           "messages": [{"role": "user", "content": prompt}],
                           "stream": False, "max_tokens": 800}).encode()
        import urllib.request
        req = urllib.request.Request(ai_client.API_URL, data=body,
                                     headers={"Content-Type": "application/json",
                                              "Authorization": "Bearer " + ai_client._secrets["deepseek_api_key"]})
        d = json.load(urllib.request.urlopen(req, timeout=120))
        text = (d.get("choices") or [{}])[0].get("message", {}).get("content", "")
        # M-P4: B级授权用户, [SUGGEST] 自动应用 (白名单校验后写租户 jev_gate.json)
        try:
            import re as _re
            import autopilot as _ap2
            auth = _ap2.auth_state(uid)
            if auth and auth.get("level") == "B":
                whitelist = {"open_p": (0.5, 0.95), "no_p": (0.5, 0.95), "conf_min": (0.5, 0.9),
                             "risk_pause": (1.0, 4.0), "l1_and_mode": (0.0, 1.0)}
                applied = []
                # 支持一行多参数: [SUGGEST] a=1, b=2
                for m in _re.finditer(r"\[SUGGEST\]\s*([^\n]+)", text):
                    for _p in _re.finditer(r"(\w+)\s*=\s*([\d.]+)", m.group(1)):
                        k, v = _p.group(1), float(_p.group(2))
                        _lo, _hi = whitelist.get(k, (None, None))
                        if _lo is None or not (_lo <= v <= _hi):
                            continue
                        applied.append((k, v))
                # 去重后写入
                seen = set()
                applied = [p for p in applied if not (p[0] in seen or seen.add(p[0]))]
                for k, v in applied:
                    import os as _os
                    gf = _os.path.join(_os.path.expanduser("~/polymarket"), "tenants", str(uid), "data", "jev_gate.json")
                    _os.makedirs(_os.path.dirname(gf), exist_ok=True)
                    cur = {}
                    try:
                        cur = json.load(open(gf, encoding="utf-8"))
                    except Exception:
                        pass
                    cur[k] = v
                    json.dump(cur, open(gf, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
                if applied:
                    text += f"\n\n⚙️ [B级授权] 已自动应用门控微调: {', '.join(f'{k}={v}' for k, v in applied)} (白名单校验通过)"
        except Exception:
            pass
        # 写入 daily 文件 hourly 字段
        f = os.path.join(tenants.logs(uid), "..", "data", f"ai_daily_{uid}.json")
        cur = {}
        try:
            cur = json.load(open(f, encoding="utf-8"))
        except Exception:
            pass
        cur["hourly"] = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "report": text}
        json.dump(cur, open(f, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        return text[:100]
    except Exception as e:
        return f"巡检失败: {type(e).__name__} {str(e)[:60]}"





def tick():
    """每60s扫描全部租户的到期任务 (pm-dash 后台线程调用)"""
    try:
        # M-D2: Jev 5分钟决策巡检 (所有付费套餐用户; 超时降级由 jev_engine 自处理)
        global _LAST_JEV
        _now = time.time()
        if _now - _LAST_JEV >= 300:
            _LAST_JEV = _now
            try:
                import jev_engine, autopilot
                from dash.app import users as _users
                for u in _users.list_users():
                    if ((u.get("plan") or "free") != "free" and u.get("status") == "active"):
                        try:
                            rec = jev_engine.run_cycle(u["id"])
                            # M-P2: 托管执行 (复用Jev巡检结果, 未授权则自动跳过)
                            autopilot.run_cycle(u["id"], rec)
                        except Exception:
                            pass
            except Exception:
                pass
        # M-D3: DeepSeek 小时巡检 + 智能触发 (付费套餐用户)
        if _inspect_triggered():
            global _LAST_INSPECT
            _LAST_INSPECT = time.time()
            try:
                from dash.app import users as _us2
                for u in _us2.list_users():
                    if ((u.get("plan") or "free") != "free" and u.get("status") == "active"):
                        try:
                            hourly_inspect(u["id"])
                        except Exception:
                            pass
            except Exception:
                pass
        # M-A6: 每日巡检 (UTC 0点后第一次tick, 旗舰版用户)
        global _LAST_DAILY
        now = time.time()
        today = time.strftime("%Y-%m-%d", time.gmtime())
        if _LAST_DAILY != today:
            _LAST_DAILY = today
            try:
                from dash.app import users
                for u in users.list_users():
                    # R14-M18: 所有付费套餐 (非 free) 都生成巡检, 套餐只区分服务时长
                    if ((u.get("plan") or "free") != "free" and u.get("status") == "active"):
                        try:
                            gen_daily_report(u["id"])
                        except Exception:
                            pass
            except Exception:
                pass
        base = tenants.base()
        dirs = [os.path.join(base, "data")]
        try:
            from dash.app import users
            for u in users.list_users():
                dirs.append(os.path.join(tenants.base(u["id"]), "data"))
        except Exception:
            pass
        for d in set(dirs):
            try:
                for fn in os.listdir(d):
                    if not fn.startswith("ai_tasks_"):
                        continue
                    uid = int(fn.replace("ai_tasks_", "").replace(".json", ""))
                    tasks = _load(uid)
                    changed = False
                    for t in tasks:
                        if not t.get("enabled"):
                            continue
                        if float(t.get("next_run") or 0) <= time.time():
                            try:
                                t["last_result"] = _exec(uid, t)
                            except Exception as e:
                                t["last_result"] = f"执行异常: {str(e)[:100]}"
                            t["last_run"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                            t["next_run"] = time.time() + float(t["interval_h"]) * 3600
                            changed = True
                    if changed:
                        _save(tasks, uid)
            except Exception:
                continue
    except Exception:
        pass


if __name__ == "__main__":
    print(json.dumps(list_tasks(1), ensure_ascii=False, indent=1))
