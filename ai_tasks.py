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


def tick():
    """每60s扫描全部租户的到期任务 (pm-dash 后台线程调用)"""
    try:
        # M-D2: Jev 5分钟决策巡检 (所有付费套餐用户; 超时降级由 jev_engine 自处理)
        global _LAST_JEV
        _now = time.time()
        if _now - _LAST_JEV >= 300:
            _LAST_JEV = _now
            try:
                import jev_engine
                from dash.app import users as _users
                for u in _users.list_users():
                    if ((u.get("plan") or "free") != "free" and u.get("status") == "active"):
                        try:
                            jev_engine.run_cycle(u["id"])
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
