#!/usr/bin/env python3
"""AI 助手工具层 (MCP风格 schema) — M6 只读版
工具白名单: strategy_status / list_params / get_pnl / git_log / run_backtest
安全: execute_tool 只分发白名单; run_backtest θ 校验范围; 无任何写操作"""
import json
import os
import subprocess
import sys
import time

BASE = os.path.expanduser("~/polymarket")
PARAMS = f"{BASE}/strategy_params.json"
sys.path.insert(0, f"{BASE}/dash")


def _read_json(path, default=None):
    try:
        return json.load(open(path))
    except Exception:
        return default


def _sh(cmd, timeout=30):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return f"命令失败: {type(e).__name__}"


def t_strategy_status():
    svc = {u: _sh(f"systemctl is-active {u}", 5).strip() for u in
           ("pm-monitor", "pm-wss", "pm-dash", "pm-carry")}
    carry = _read_json(f"{BASE}/logs/carry_state.json", {})
    paper = _read_json(f"{BASE}/logs/paper_state.json", {})
    cpos = {k: "持有" for k in (carry.get("positions") or {})}
    ppos = [(k.split("|")[-1], v.get("side")) for k, v in (paper.get("positions") or {}).items()]
    last_carry = _sh("tail -1 ~/polymarket/logs/carry_1m.jsonl | head -c 120", 5).strip()
    return {"services": svc,
            "carry持仓": cpos or "空仓",
            "carry今日PnL": carry.get("day_pnl"),
            "carry轮数": carry.get("n_rounds"),
            "pm纸面持仓": ppos or "空仓",
            "pm今日PnL": paper.get("day_pnl"),
            "pm笔数": paper.get("n_trades"),
            "最新carry数据": last_carry[:100]}


def t_list_params():
    d = _read_json(PARAMS, {})
    d.pop("_comment", None)
    zh = {
        "carry": {"theta_in_ann_pct": "入场阈值(资金费率年化%)", "max_hold_h": "最长持仓小时",
                  "max_basis_bp": "基差上限bp", "notional_usd": "每标的名义$"},
        "paper_pm": {"min_gross_edge_c": "最小毛价差¢", "theta_out_c": "平仓阈值¢",
                     "min_opposite_size": "对手档下限股", "max_spread_c": "价差上限¢",
                     "max_hold_h": "最长持仓小时", "max_exposure_usd": "总敞口上限$",
                     "max_positions": "最大持仓数", "max_daily_loss": "日亏限额$"},
        "monitor": {"cal_sigma_up": "σ校准·涨桶", "cal_sigma_down": "σ校准·跌桶"},
    }
    out = {}
    for group, params in d.items():
        out[group] = {k: {"值": v, "中文名": zh.get(group, {}).get(k, k)} for k, v in params.items()}
    out["_重要"] = "update_params 的键名必须用英文键(如 theta_in_ann_pct), 不能用中文名"
    return {"参数": out, "说明": "改参数需用户批准后生效; 变更工具走预览→审批流"}


def t_get_pnl():
    from app import readers
    d = readers.pnl_overview()
    return {"整体资金$": d["capital"], "累计已实现$": d["realized_total"],
            "今日已实现$": d["realized_today"], "未实现盯市$": d["unrealized"],
            "持仓数": len(d["positions"]), "时间": d["ts"]}


def t_git_log():
    out = _sh(f"cd {BASE} && git log --oneline -10 2>&1", 10)
    return {"版本历史": out.strip()}


def t_run_backtest(args):
    theta = float(args.get("theta", 5.0))
    if not (0 < theta <= 20):
        return {"error": f"θ必须0-20, 收到{theta}"}
    out = _sh(f"cd {BASE} && ./venv/bin/python carry_backtest.py --theta {theta} --nobasis 2>&1 | tail -8", timeout=240)
    return {"theta": theta, "输出": out.strip()}


# ================= M7: 变更类工具 (预览→审批→执行) =================
PARAM_SCHEMA = {
    "carry": {"theta_in_ann_pct": ("float", 0, 20), "max_hold_h": ("float", 1, 720),
              "max_basis_bp": ("float", 0, 100), "notional_usd": ("float", 1, 50),
              "compounding_base_usd": ("float", 1, 100), "compounding_min_mult": ("float", 0.05, 1),
              "compounding_max_mult": ("float", 1, 10), "entry_window_min": ("float", 0, 480),
              "spot_borrow_ann_pct": ("float", 0, 50),
              "swing_filter_enabled": ("float", 0, 1), "swing_min_score": ("float", 0, 100)},
    "cycle": {"enabled": ("float", 0, 1), "min_score": ("float", 0, 100),
              "base_notional": ("float", 1, 50), "mult": ("float", 1, 3),
              "max_ladder": ("float", 0, 5), "tp_pct": ("float", 0.1, 5),
              "sl_pct": ("float", 0.1, 5), "daily_loss_cap": ("float", 0.5, 20),
              "cooldown_s": ("float", 0, 3600)},
    "paper_pm": {"min_gross_edge_c": ("float", 0.5, 20), "theta_out_c": ("float", 0, 5),
                 "min_opposite_size": ("float", 10, 1000), "max_spread_c": ("float", 1, 50),
                 "max_hold_h": ("float", 1, 72), "max_exposure_usd": ("float", 1, 100),
                 "max_positions": ("int", 1, 10), "max_daily_loss": ("float", 0.5, 50)},
    "monitor": {"cal_sigma_up": ("float", 0.7, 1.5), "cal_sigma_down": ("float", 0.7, 1.5)},
}
PENDING_FILE = f"{BASE}/engine/ai_pending.json"
AUDIT_FILE = f"{BASE}/logs/ai_actions.jsonl"
ALLOWED_UNITS = ("pm-monitor", "pm-wss", "pm-dash", "pm-carry")


def _pending():
    try:
        return json.load(open(PENDING_FILE))
    except Exception:
        return {}


def _save_pending(p):
    os.makedirs(os.path.dirname(PENDING_FILE), exist_ok=True)
    json.dump(p, open(PENDING_FILE, "w"), ensure_ascii=False, indent=1)


def _audit(action, detail):
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "action": action, **detail}
    with open(AUDIT_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _validate_changes(changes):
    """白名单+类型+范围校验 → (diff列表, 错误)"""
    if not changes:
        return [], "未提供修改内容"
    if len(changes) > 3:
        return [], "单次最多修改3个参数"
    params = _read_json(PARAMS, {})
    diff = []
    for group, kv in changes.items():
        schema = PARAM_SCHEMA.get(group)
        if schema is None:
            return [], f"未知参数组: {group} (合法组: {list(PARAM_SCHEMA)})"
        cur = params.get(group, {})
        for k, v in (kv or {}).items():
            spec = schema.get(k)
            if spec is None:
                return [], f"参数不在白名单: {group}.{k}"
            typ, lo, hi = spec
            try:
                nv = float(v) if typ == "float" else int(float(v))
            except Exception:
                return [], f"{group}.{k} 值非法: {v}"
            if not (lo <= nv <= hi):
                return [], f"{group}.{k}={nv} 超出范围[{lo},{hi}]"
            diff.append({"group": group, "key": k, "old": cur.get(k), "new": nv})
    return diff, None


def t_update_params(args):
    changes = args.get("changes") or {}
    diff, err = _validate_changes(changes)
    if err:
        return {"status": "rejected", "error": err}
    p = _pending()
    aid = f"a{int(time.time()*1000)}"
    p[aid] = {"type": "update_params", "changes": changes, "diff": diff,
              "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    _save_pending(p)
    _audit("update_params_preview", {"changes": changes})
    return {"status": "preview", "action_id": aid, "diff": diff,
            "message": "修改预览已生成, 尚未生效, 等待用户在界面点击【批准】"}


def t_git_rollback(args):
    rev = str(args.get("rev", "")).strip()
    if not rev:
        return {"status": "rejected", "error": "必须提供 rev (git提交哈希或HEAD~N)"}
    check = _sh(f"cd {BASE} && git rev-parse --verify {rev} 2>&1", 10).strip()
    if not check.startswith("0" * 7) and "fatal" in check.lower():
        return {"status": "rejected", "error": f"无效版本: {rev}"}
    log = _sh(f"cd {BASE} && git log --oneline -1 {rev} 2>&1", 10).strip()
    p = _pending()
    aid = f"a{int(time.time()*1000)}"
    p[aid] = {"type": "git_rollback", "rev": rev, "diff": [{"group": "git", "key": "回退到", "old": "当前", "new": log}],
              "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    _save_pending(p)
    _audit("rollback_preview", {"rev": rev})
    return {"status": "preview", "action_id": aid, "rev": rev, "target": log,
            "message": "回退预览已生成, 尚未执行, 等待用户批准"}


def t_restart_engine(args):
    unit = str(args.get("unit", "")).strip()
    if unit not in ALLOWED_UNITS:
        return {"status": "rejected", "error": f"不允许重启的单元: {unit} (允许: {list(ALLOWED_UNITS)})"}
    p = _pending()
    aid = f"a{int(time.time()*1000)}"
    p[aid] = {"type": "restart_engine", "unit": unit,
              "diff": [{"group": "systemd", "key": "重启", "old": "-", "new": unit}],
              "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    _save_pending(p)
    _audit("restart_preview", {"unit": unit})
    return {"status": "preview", "action_id": aid, "unit": unit,
            "message": "重启预览已生成, 等待用户批准"}


def apply_params_direct(changes, source="manual"):
    """手动直接改参 (无审批流, 用户在UI自己确认): 白名单+范围校验 → 原子写 → git提交 → 审计
    source: manual(交易室手动) / 其他标识"""
    diff, err = _validate_changes(changes)
    if err:
        return {"ok": False, "error": err}
    params = _read_json(PARAMS, {})
    for c in diff:
        params.setdefault(c["group"], {})[c["key"]] = c["new"]
    tmp = PARAMS + ".tmp"
    json.dump(params, open(tmp, "w"), ensure_ascii=False, indent=2)
    os.replace(tmp, PARAMS)
    summary = ", ".join(f"{c['group']}.{c['key']}={c['old']}→{c['new']}" for c in diff)
    out = _sh(f"cd {BASE} && git add strategy_params.json && git commit -q -m '手动改参: {summary}'", 15)
    _audit("manual_params", {"source": source, "changes": changes,
                             "summary": summary, "git": out.strip()[:40] or "committed"})
    return {"ok": True, "msg": f"已保存并提交: {summary}", "diff": diff,
            "note": "引擎下轮热加载生效(≤60s)"}


def apply_pending(action_id, approve):
    """用户批准/拒绝 → 执行或废弃, 记审计"""
    p = _pending()
    act = p.get(action_id)
    if act is None:
        return {"ok": False, "error": f"待审批动作不存在: {action_id}"}
    if not approve:
        del p[action_id]
        _save_pending(p)
        _audit("rejected", {"action_id": action_id, "type": act["type"]})
        return {"ok": True, "applied": False, "msg": "已拒绝"}
    ok, detail = _apply(act)
    if ok:
        del p[action_id]
        _save_pending(p)
        _audit("applied", {"action_id": action_id, **detail})
        return {"ok": True, "applied": True, "msg": detail.get("msg", "已生效"), "detail": detail}
    _audit("apply_failed", {"action_id": action_id, "error": detail.get("error")})
    return {"ok": False, "error": detail.get("error", "执行失败")}


def _apply(act):
    if act["type"] == "update_params":
        params = _read_json(PARAMS, {})
        for c in act["diff"]:
            params.setdefault(c["group"], {})[c["key"]] = c["new"]
        json.dump(params, open(PARAMS, "w"), ensure_ascii=False, indent=2)
        summary = ", ".join(f"{c['group']}.{c['key']}={c['old']}→{c['new']}" for c in act["diff"])
        out = _sh(f"cd {BASE} && git add strategy_params.json && git commit -q -m 'AI改参: {summary}'", 15)
        return True, {"msg": f"已生效并提交: {summary}", "commit": out.strip()[:40] or "committed"}
    if act["type"] == "git_rollback":
        rev = act["rev"]
        out = _sh(f"cd {BASE} && git checkout {rev} -- strategy_params.json && git commit -q -m 'AI回退参数到 {rev}'", 20)
        return True, {"msg": f"参数已回退到 {rev}", "commit": out.strip()[:40] or "committed"}
    if act["type"] == "restart_engine":
        unit = act["unit"]
        out = _sh(f"sudo systemctl restart {unit} 2>&1 && sleep 2 && systemctl is-active {unit}", 30)
        return True, {"msg": f"{unit} 已重启, 状态: {out.strip()}"}
    return False, {"error": f"未知动作类型: {act['type']}"}


TOOLS = [
    {"type": "function", "function": {"name": "strategy_status",
        "description": "查询量化系统运行状态: 引擎服务/持仓/今日盈亏/数据新鲜度 (只读)",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "list_params",
        "description": "查询当前策略参数表 (strategy_params.json 单一参数源, 只读)",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "get_pnl",
        "description": "查询模拟盘整体盈亏: 整体资金/已实现/未实现盯市 (只读)",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "get_micro",
        "description": "查询微结构指标: CVD累积成交量差/未平仓量OI/主动买卖比/挂单墙统计 (只读, 供波段分析)",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "git_log",
        "description": "查看策略代码与参数版本历史 (最近10条, 只读)",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "run_backtest",
        "description": "用候选入场阈值θ跑现货永续套利回测(近2个月), 供参数对比决策 (只读, 不落地)",
        "parameters": {"type": "object", "properties": {"theta": {
            "type": "number", "description": "入场阈值: 资金费率年化% (0-20, 默认5)"}}, "required": ["theta"]}}},
    {"type": "function", "function": {"name": "update_params",
        "description": "修改策略参数(白名单+范围校验, 生成修改预览, 需用户在界面批准后才生效; 单次最多3个键). "
                       "键名必须是英文(如 carry.theta_in_ann_pct / paper_pm.min_gross_edge_c / monitor.cal_sigma_up), 先调list_params查询",
        "parameters": {"type": "object", "properties": {"changes": {
            "type": "object", "description": "参数修改 {组名: {英文参数名: 新值}}, 组名∈carry/paper_pm/monitor"}},
            "required": ["changes"]}}},
    {"type": "function", "function": {"name": "git_rollback",
        "description": "回退策略参数到指定git版本(预览, 需用户批准后执行 git checkout)",
        "parameters": {"type": "object", "properties": {"rev": {
            "type": "string", "description": "git提交哈希或HEAD~N, 先用git_log查"}}, "required": ["rev"]}}},
    {"type": "function", "function": {"name": "restart_engine",
        "description": "重启指定服务单元(预览, 需用户批准; 允许: pm-monitor/pm-wss/pm-dash/pm-carry)",
        "parameters": {"type": "object", "properties": {"unit": {
            "type": "string", "description": "systemd单元名"}}, "required": ["unit"]}}},
]

def t_get_micro():
    """微结构指标 (只读): CVD/OI/主动买卖比/墙统计"""
    import sys
    sys.path.insert(0, os.path.join(BASE, "dash"))
    try:
        from app import readers
        d = readers.micro()
    except Exception:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from dash.app import readers
        d = readers.micro()
    out = {"note": "微结构指标 (CVD=主动买-主动卖累计, OI=未平仓量)", "symbols": {}}
    for sym, m in d.items():
        out["symbols"][sym] = {
            "CVD最近60分钟": [(x[0], x[2]) for x in m.get("cvd_series", [])[-10:]],
            "未平仓量OI": m.get("oi"),
            "OI五分钟变化率%": m.get("oi_5m_chg_pct"),
            "主动买占比%(近2小时)": m.get("taker_buy_pct_2h"),
            "主动买笔数(2h)": m.get("n_buy_2h"),
            "主动卖笔数(2h)": m.get("n_sell_2h"),
            "挂单墙出现(2h)": m.get("wall_appear_2h"),
            "挂单墙消失(2h)": m.get("wall_vanish_2h")}
    return out


_DISPATCH = {"strategy_status": lambda a: t_strategy_status(), "list_params": lambda a: t_list_params(),
             "get_pnl": lambda a: t_get_pnl(), "git_log": lambda a: t_git_log(),
             "get_micro": lambda a: t_get_micro(),
             "run_backtest": t_run_backtest, "update_params": t_update_params,
             "git_rollback": t_git_rollback, "restart_engine": t_restart_engine}


def execute_tool(name, args):
    """白名单分发 (M6: 全部只读)"""
    fn = _DISPATCH.get(name)
    if fn is None:
        return {"error": f"未知工具: {name}"}
    try:
        return fn(args or {})
    except Exception as e:
        return {"error": f"{type(e).__name__}: {str(e)[:200]}"}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("tool")
    ap.add_argument("--theta", type=float, default=5.0)
    a = ap.parse_args()
    print(json.dumps(execute_tool(a.tool, {"theta": a.theta}), ensure_ascii=False, indent=1))
