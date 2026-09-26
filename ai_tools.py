#!/usr/bin/env python3
"""AI 助手工具层 (MCP风格 schema) — M6 只读版
工具白名单: strategy_status / list_params / get_pnl / git_log / run_backtest
安全: execute_tool 只分发白名单; run_backtest θ 校验范围; 无任何写操作"""
import json
import os
import subprocess
import sys
import time

import tenants

BASE = tenants.ROOT  # 保留: 兼容旧引用 (实际路径走 __getattr__)
sys.path.insert(0, os.path.join(BASE, "dash"))

def _resolve(name):
    """内部路径解析: 测试 setattr monkeypatch 优先, 否则租户动态解析"""
    if name in globals():
        return globals()[name]
    return _DYN[name]()

_DYN = {
    "PARAMS": lambda: tenants.params_file(),
    "PENDING_FILE": lambda: tenants.ai_pending(),
    "AUDIT_FILE": lambda: tenants.ai_audit(),
}


def __getattr__(name):
    f = _DYN.get(name)
    if f:
        return f()
    raise AttributeError(f"module 'ai_tools' has no attribute '{name}'")


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
    carry = _read_json(tenants.state("carry"), {})
    paper = _read_json(tenants.state("paper"), {})
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
    d = _read_json(_resolve("PARAMS"), {})
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
    "jev": {"open_p": ("float", 0.5, 0.95), "no_p": ("float", 0.5, 0.95),
            "conf_min": ("float", 0.5, 0.9), "risk_pause": ("float", 1, 4),
            "l1_and_mode": ("float", 0, 1)},
}
ALLOWED_UNITS = ("pm-monitor", "pm-wss", "pm-dash", "pm-carry")


def _pending():
    try:
        return json.load(open(_resolve("PENDING_FILE")))
    except Exception:
        return {}


def _save_pending(p):
    os.makedirs(os.path.dirname(_resolve("PENDING_FILE")), exist_ok=True)
    json.dump(p, open(_resolve("PENDING_FILE"), "w"), ensure_ascii=False, indent=1)


def _audit(action, detail):
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "action": action, **detail}
    with open(_resolve("AUDIT_FILE"), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _validate_changes(changes):
    """白名单+类型+范围校验 → (diff列表, 错误)"""
    if not changes:
        return [], "未提供修改内容"
    if len(changes) > 3:
        return [], "单次最多修改3个参数"
    params = _read_json(_resolve("PARAMS"), {})
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
    if not tenants.is_admin():
        return {"status": "rejected", "error": "仅管理员账户支持 git 参数回退 (用户账户参数改动不入 git, 由审计留痕)"}
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
    if not tenants.is_admin():
        return {"status": "rejected", "error": "仅管理员可重启引擎服务"}
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


def _write_jev_gate(diff):
    """M-D3: jev 组参数写租户级 jev_gate.json (jev_engine.gate 每次读文件, 实时生效)"""
    f = os.path.join(tenants.base(tenants.current_uid()), "data", "jev_gate.json")
    os.makedirs(os.path.dirname(f), exist_ok=True)
    cur = {}
    try:
        cur = json.load(open(f, encoding="utf-8"))
    except Exception:
        pass
    for c in diff:
        cur[c["key"]] = c["new"]
    tmp = f + ".tmp"
    json.dump(cur, open(tmp, "w"), ensure_ascii=False, indent=1)
    os.replace(tmp, f)
    return f


def apply_params_direct(changes, source="manual"):
    """手动直接改参 (无审批流, 用户在UI自己确认): 白名单+范围校验 → 原子写 → git提交 → 审计
    source: manual(交易室手动) / 其他标识"""
    diff, err = _validate_changes(changes)
    if err:
        return {"ok": False, "error": err}
    jev_diff = [c for c in diff if c["group"] == "jev"]
    if jev_diff and all(c["group"] == "jev" for c in diff):
        f = _write_jev_gate(jev_diff)
        _audit("jev_gate_direct", {"file": f, "diff": jev_diff, "source": source})
        return {"ok": True, "jev_gate": f,
                "msg": ", ".join(f"{c['key']}={c['old']}→{c['new']}" for c in jev_diff)}
    params = _read_json(_resolve("PARAMS"), {})
    for c in diff:
        params.setdefault(c["group"], {})[c["key"]] = c["new"]
    tmp = _resolve("PARAMS") + ".tmp"
    json.dump(params, open(tmp, "w"), ensure_ascii=False, indent=2)
    os.replace(tmp, _resolve("PARAMS"))
    summary = ", ".join(f"{c['group']}.{c['key']}={c['old']}→{c['new']}" for c in diff)
    git_note = "租户参数(不入git)"
    if tenants.is_admin():
        out = _sh(f"cd {BASE} && git add strategy_params.json && git commit -q -m '手动改参: {summary}'", 15)
        git_note = out.strip()[:40] or "committed"
    _audit("manual_params", {"source": source, "changes": changes,
                             "summary": summary, "git": git_note})
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
        jev_diff = [c for c in act["diff"] if c["group"] == "jev"]
        if jev_diff:
            f = _write_jev_gate(jev_diff)
            return True, {"msg": "Jev门控已实时生效: " + ", ".join(
                f"{c['key']}={c['old']}→{c['new']}" for c in jev_diff), "file": f}
        params = _read_json(_resolve("PARAMS"), {})
        for c in act["diff"]:
            params.setdefault(c["group"], {})[c["key"]] = c["new"]
        json.dump(params, open(_resolve("PARAMS"), "w"), ensure_ascii=False, indent=2)
        summary = ", ".join(f"{c['group']}.{c['key']}={c['old']}→{c['new']}" for c in act["diff"])
        commit = "租户参数(不入git)"
        if tenants.is_admin():
            out = _sh(f"cd {BASE} && git add strategy_params.json && git commit -q -m 'AI改参: {summary}'", 15)
            commit = out.strip()[:40] or "committed"
        return True, {"msg": f"已生效: {summary}", "commit": commit}
    if act["type"] == "open_carry":
        import paper_ops
        r = paper_ops.open_hedge(act["symbol"], act["notional"])
        if isinstance(r, dict) and r.get("ok") is False:
            return False, {"error": str(r)[:200]}
        return True, {"msg": f"纸面开仓已执行: {act['symbol']} 名义{act['notional']}USDT (成交留痕)",
                      "result": r}
    if act["type"] == "close_carry":
        import paper_ops
        r = paper_ops.close_both(act["symbol"])
        if isinstance(r, dict) and r.get("ok") is False:
            return False, {"error": str(r)[:200]}
        return True, {"msg": f"纸面平仓已执行: {act['symbol']} (成交留痕)", "result": r}
    if act["type"] == "open_native":
        import paper_ops
        r = paper_ops.open_native(act["symbol"], act["side"], act["notional"])
        if isinstance(r, dict) and r.get("ok") is False:
            return False, {"error": str(r)[:200]}
        return True, {"msg": f"原生{'做多' if act['side']=='long' else '做空'}已执行: {act['symbol']} 名义{act['notional']}USDT", "result": r}
    if act["type"] == "close_native":
        import paper_ops
        r = paper_ops.close_native(act["symbol"])
        if isinstance(r, dict) and r.get("ok") is False:
            return False, {"error": str(r)[:200]}
        return True, {"msg": f"原生平仓已执行: {act['symbol']}", "result": r}
    if act["type"] == "open_spot":
        import paper_ops
        r = paper_ops.open_spot(act["symbol"], act["side"], act["notional"])
        if isinstance(r, dict) and r.get("ok") is False:
            return False, {"error": str(r)[:200]}
        return True, {"msg": f"现货{'买入' if act['side']=='buy' else '卖出'}已执行: {act['symbol']}", "result": r}
    if act["type"] == "close_spot":
        import paper_ops
        r = paper_ops.close_spot(act["symbol"])
        if isinstance(r, dict) and r.get("ok") is False:
            return False, {"error": str(r)[:200]}
        return True, {"msg": f"现货平仓已执行: {act['symbol']}", "result": r}
    if act["type"] == "autopilot_create":
        import autopilot
        import tenants
        with tenants.tenant(tenants.current_uid()):
            r = autopilot.create_task(tenants.current_uid(), act["scope"], mode=act["mode"])
        if not r.get("ok"):
            return False, {"error": r.get("error", "创建失败")}
        return True, {"msg": f"托管任务已创建: {r.get('msg', '')}", "result": r}
    if act["type"] == "autopilot_set":
        import autopilot
        import tenants
        uid = tenants.current_uid()
        with tenants.tenant(uid):
            if act["status"] == "cancelled":
                r = autopilot.cancel_with_close(uid, act["tid"])
            else:
                r = autopilot.set_task(uid, act["tid"], status=act["status"])
        if not r.get("ok"):
            return False, {"error": r.get("error", "操作失败")}
        return True, {"msg": r.get("msg", "托管已更新"), "result": r}
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
    {"type": "function", "function": {"name": "backtest_summary",
        "description": "跑现货永续套利回测并输出结构化指标(各标的轮数/总盈亏/年化/胜率), 结果自动存档可回溯 (只读, 不落地交易)",
        "parameters": {"type": "object", "properties": {"theta": {
            "type": "number", "description": "入场阈值θ: 资金费率年化% (0-20, 默认5)"}}, "required": ["theta"]}}},
    {"type": "function", "function": {"name": "backtest_history",
        "description": "读取历史回测记录 (最近N条, 对比不同θ的效果) (只读)",
        "parameters": {"type": "object", "properties": {"n": {
            "type": "number", "description": "条数, 默认5, 最大10"}}}}},
    {"type": "function", "function": {"name": "open_carry",
        "description": "对话式开仓: 现货×永续双向套利(买现货+空永续赚资金费率)。生成执行预览(Jev复核附在预览), 必须等用户在界面点击【批准】才执行。标的限 BTCUSDT/ETHUSDT/XAUTUSDT/SOLUSDT/NEARUSDT/XRPUSDT",
        "parameters": {"type": "object", "properties": {
            "symbol": {"type": "string", "description": "标的, 如 BTCUSDT (必须用英文键)"},
            "notional": {"type": "number", "description": "名义金额USDT, 1-50, 默认10"}},
            "required": ["symbol"]}}},
    {"type": "function", "function": {"name": "close_carry",
        "description": "对话式平仓: 平掉指定标的的全部双向套利持仓(平现货+平永续)。生成预览, 必须等用户批准才执行",
        "parameters": {"type": "object", "properties": {
            "symbol": {"type": "string", "description": "标的, 如 BTCUSDT"}},
            "required": ["symbol"]}}},
    {"type": "function", "function": {"name": "open_native",
        "description": "对话式原生方向开仓: 单边永续做多/做空(押涨跌)。生成预览, 必须等用户批准才执行。标的限 BTCUSDT/ETHUSDT/XAUUSDT/XAGUSDT/SOLUSDT/NEARUSDT/XRPUSDT/TRXUSDT",
        "parameters": {"type": "object", "properties": {
            "symbol": {"type": "string", "description": "标的如 BTCUSDT"},
            "side": {"type": "string", "description": "long=做多, short=做空"},
            "notional": {"type": "number", "description": "名义USDT, 1-200, 默认10"}},
            "required": ["symbol", "side"]}}},
    {"type": "function", "function": {"name": "close_native",
        "description": "对话式原生方向平仓: 平掉指定标的的原生单边持仓。生成预览, 必须等用户批准才执行",
        "parameters": {"type": "object", "properties": {
            "symbol": {"type": "string", "description": "标的如 BTCUSDT"}},
            "required": ["symbol"]}}},
    {"type": "function", "function": {"name": "open_spot",
        "description": "对话式现货开仓: 买入/卖出。生成预览, 必须等用户批准才执行",
        "parameters": {"type": "object", "properties": {
            "symbol": {"type": "string", "description": "标的如 BTCUSDT"},
            "side": {"type": "string", "description": "buy=买入, sell=卖出"},
            "notional": {"type": "number", "description": "名义USDT, 1-200, 默认10"}},
            "required": ["symbol", "side"]}}},
    {"type": "function", "function": {"name": "close_spot",
        "description": "对话式现货平仓: 平掉指定标的的现货持仓。生成预览, 必须等用户批准才执行",
        "parameters": {"type": "object", "properties": {
            "symbol": {"type": "string", "description": "标的如 BTCUSDT"}},
            "required": ["symbol"]}}},
    {"type": "function", "function": {"name": "autopilot_create",
        "description": "创建AI全托管任务(让AI自主决策开平仓)。生成预览, 必须等用户批准才执行。scope=all(全标的)或标的列表; mode=carry(基差套利)/native(原生方向)",
        "parameters": {"type": "object", "properties": {
            "scope": {"type": "string", "description": "all 或 逗号分隔标的列表"},
            "mode": {"type": "string", "description": "carry/native, 默认carry"}},
            "required": ["scope"]}}},
    {"type": "function", "function": {"name": "autopilot_set",
        "description": "暂停/恢复/取消AI托管任务。生成预览, 必须等用户批准才执行。tid=任务ID(先用my_positions或查询托管列表); status=paused/running/cancelled",
        "parameters": {"type": "object", "properties": {
            "tid": {"type": "string", "description": "托管任务ID"},
            "status": {"type": "string", "description": "paused/running/cancelled"}},
            "required": ["tid", "status"]}}},
    {"type": "function", "function": {"name": "get_jev_decisions",
        "description": "读取Jev快速决策层最近决策留痕(开仓信号/风险评分/门控结果), 用于巡检判断门控参数是否需调整 (只读)",
        "parameters": {"type": "object", "properties": {"n": {
            "type": "number", "description": "条数, 默认10, 最大30"}}}}},
    {"type": "function", "function": {"name": "create_task",
        "description": "为用户创建智能定时任务(直接生效): 类型∈fee_watch(费率监控,需threshold年化%阈值)/risk_scan(持仓风险扫描)/backtest_run(回测,需threshold=θ值)/reminder(自定义提醒,需note提醒内容); interval_h=执行间隔小时数(fee_watch/reminder最小1, risk_scan最小2, backtest_run最小6, 最大720); name=任务名称",
        "parameters": {"type": "object", "properties": {
            "type": {"type": "string", "description": "任务类型"},
            "name": {"type": "string", "description": "任务名称(中文)"},
            "interval_h": {"type": "number", "description": "间隔小时数"},
            "note": {"type": "string", "description": "reminder类型必填: 提醒内容"},
            "threshold": {"type": "number", "description": "fee_watch=费率阈值年化%, backtest_run=θ值"}},
            "required": ["type", "name", "interval_h"]}}},
    {"type": "function", "function": {"name": "my_positions",
        "description": "查询当前登录用户的持仓: 现货持仓+合约纸面持仓(数量/均价/现价/浮动盈亏) (只读)",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "my_balance",
        "description": "查询当前登录用户的账户余额/套餐/托管到期/实盘权限 (只读)",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "trade_analysis",
        "description": "学习总结历史交易: 统计总笔数/胜率/总盈亏/平均每笔/最大盈亏, 按标的/方向/模式分布, 用于提炼策略经验 (只读)",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "my_trades",
        "description": "查询当前登录用户最近N笔交易台账(模拟+实盘, 时间/标的/方向/盈亏/费用) (只读)",
        "parameters": {"type": "object", "properties": {"n": {
            "type": "number", "description": "最近几笔, 默认10, 最大30"}}}}},
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


def _bt_parse(out):
    """解析 carry_backtest.py 汇总行 → 指标 dict"""
    import re
    metrics = {}
    for line in out.strip().splitlines():
        m = re.search(r"^(\S+) θ_in=([\d.]+)%: (\d+)轮 \| 总PnL ([+-][\d.]+)\$ \| 年化 ([+-][\d.]+)% \| 胜率 (\d+)%",
                      line)
        if m:
            metrics[m.group(1)] = {"θ(%)": float(m.group(2)), "轮数": int(m.group(3)),
                                   "总盈亏$": float(m.group(4)), "年化%": float(m.group(5)),
                                   "胜率%": int(m.group(6))}
    return metrics


def t_backtest_summary(args):
    """M-A5: 跑回测并解析结构化指标 + 持久化历史 (θ 0-20)"""
    import datetime
    theta = float((args or {}).get("theta", 5.0))
    if not (0 < theta <= 20):
        return {"error": f"θ必须0-20, 收到{theta}"}
    # grep 抓汇总行 (tail 会被每轮明细挤掉前面的标的汇总)
    out = _sh(f"cd {BASE} && ./venv/bin/python carry_backtest.py --theta {theta} --nobasis 2>&1 | grep -E 'θ_in=|数据缺失|跳过' | tail -10",
              timeout=240)
    metrics = _bt_parse(out)
    if not metrics:
        return {"theta": theta, "原始输出": out.strip()[-500:], "说明": "回测无汇总行, 可能是数据窗口问题"}
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "theta": theta, "metrics": metrics}
    try:
        import os as _os
        f = _os.path.join(tenants.base(tenants.current_uid()), "data", "backtest_results.jsonl")
        _os.makedirs(_os.path.dirname(f), exist_ok=True)
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass
    return {"θ(%)": theta, "各标的回测": metrics,
            "白话提示": "这是纸面历史回测, 不代表未来收益; 年化=历史数据外推, 实际受资金费率波动影响"}


def t_backtest_history(args):
    """M-A5: 读取历史回测记录 (最近N条)"""
    import glob as _g
    n = min(int((args or {}).get("n", 5)), 10)
    f = os.path.join(tenants.base(tenants.current_uid()), "data", "backtest_results.jsonl")
    try:
        lines = open(f, encoding="utf-8").read().strip().splitlines()
        return {"历史回测": [json.loads(l) for l in lines[-n:]], "条数": len(lines)}
    except Exception:
        return {"历史回测": [], "条数": 0, "说明": "暂无回测历史 (先让AI跑一次 backtest_summary)"}


DUAL_CARRY_SYMS = ("BTCUSDT", "ETHUSDT", "XAUTUSDT", "SOLUSDT", "NEARUSDT", "XRPUSDT")


def _jev_check(symbol):
    """M-D4: Jev 复核开仓时机 → (P, 信号, 把握度); Jev 不可用时不阻塞(返回None)"""
    try:
        import typesafe_decision as tsd
        st = tsd.build_state(tenants.current_uid())
        r = tsd.decide_open(st, [symbol], theta=5.0)
        a = (r.get("answers") or {}).get(f"open_{symbol}")
        if a and "_error" not in r:
            p = a.get("noul", 0.5)
            return {"P": round(p, 3), "把握度": round(abs(p - 0.5) * 2, 3),
                    "判断": "模型认为适合开仓" if p >= 0.65 else
                            ("模型不看好" if p <= 0.35 else "模型态度中性")}
    except Exception:
        pass
    return None


def t_open_carry(args):
    """M-D4: 对话式开仓 — 白名单+Jev复核 → 预览 → 用户批准 → paper_ops.open_hedge"""
    sym = str(args.get("symbol") or "").upper()
    if sym not in DUAL_CARRY_SYMS:
        return {"status": "rejected", "error": f"标的 {sym} 不在可交易池 {list(DUAL_CARRY_SYMS)}"}
    try:
        notional = float(args.get("notional", 10))
    except Exception:
        return {"status": "rejected", "error": "notional 必须是数字"}
    if not (1 <= notional <= 50):
        return {"status": "rejected", "error": "名义金额须在 1-50 USDT"}
    jev = _jev_check(sym)
    p = _pending()
    aid = f"a{int(time.time()*1000)}"
    p[aid] = {"type": "open_carry", "symbol": sym, "notional": notional,
              "jev": jev, "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "diff": [{"group": "trade", "key": "开仓", "old": "-", "new": f"{sym} 名义{notional}USDT (买现货+空永续)"},
                        {"group": "trade", "key": "Jev复核", "old": "-",
                         "new": f"P={jev['P']} 把握度{jev['把握度']} ({jev['判断']})" if jev else "Jev不可用, 跳过复核"}]}
    _save_pending(p)
    _audit("open_carry_preview", {"symbol": sym, "notional": notional, "jev": jev})
    return {"status": "preview", "action_id": aid, "symbol": sym, "notional": notional,
            "jev": jev, "diff": p[aid]["diff"],
            "message": f"开仓预览已生成 (尚未执行): {sym} 名义{notional}USDT 双向套利。等待用户在界面点击【批准】"}


def t_close_carry(args):
    """M-D4: 对话式平仓 — 白名单 → 预览 → 批准 → paper_ops.close_both"""
    sym = str(args.get("symbol") or "").upper()
    if sym not in DUAL_CARRY_SYMS:
        return {"status": "rejected", "error": f"标的 {sym} 不在可交易池"}
    p = _pending()
    aid = f"a{int(time.time()*1000)}"
    p[aid] = {"type": "close_carry", "symbol": sym,
              "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "diff": [{"group": "trade", "key": "平仓", "old": "-", "new": f"{sym} 全部持仓(平现货+平永续)"}]}
    _save_pending(p)
    _audit("close_carry_preview", {"symbol": sym})
    return {"status": "preview", "action_id": aid, "symbol": sym, "diff": p[aid]["diff"],
            "message": f"平仓预览已生成 (尚未执行): {sym}。等待用户批准"}


NATIVE_SYMS = ("BTCUSDT", "ETHUSDT", "XAUUSDT", "XAGUSDT", "SOLUSDT", "NEARUSDT", "XRPUSDT", "TRXUSDT")
SPOT_SYMS = ("BTCUSDT", "ETHUSDT", "XAUTUSDT", "SOLUSDT", "NEARUSDT", "XRPUSDT", "TRXUSDT")


def _preview(type_, sym, diff_lines, message):
    aid = f"a{int(time.time()*1000)}"
    p = _pending()
    p[aid] = {"type": type_, "symbol": sym, "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "diff": diff_lines}
    for k in ("side", "notional", "scope", "mode", "tid", "status"):
        if k in locals():
            p[aid][k] = locals()[k]
    _save_pending(p)
    _audit(type_ + "_preview", {"symbol": sym})
    return {"status": "preview", "action_id": aid, "symbol": sym, "diff": diff_lines, "message": message}


def t_open_native(args):
    """原生方向开仓(单边永续做多/做空) — 预览→批准"""
    sym = str(args.get("symbol") or "").upper()
    side = str(args.get("side") or "").lower()
    if sym not in NATIVE_SYMS:
        return {"status": "rejected", "error": f"标的 {sym} 不在原生可交易池"}
    if side not in ("long", "short"):
        return {"status": "rejected", "error": "side 须 long(做多)/short(做空)"}
    try:
        notional = float(args.get("notional", 10))
    except Exception:
        return {"status": "rejected", "error": "notional 必须是数字"}
    if not (1 <= notional <= 200):
        return {"status": "rejected", "error": "名义金额须 1-200 USDT"}
    return _preview("open_native", sym,
        [{"group": "trade", "key": "原生开仓", "old": "-", "new": f"{sym} {'做多' if side=='long' else '做空'} 名义{notional}USDT"}],
        f"原生开仓预览已生成(尚未执行): {sym} {'做多' if side=='long' else '做空'}。等待用户批准")


def t_close_native(args):
    sym = str(args.get("symbol") or "").upper()
    if sym not in NATIVE_SYMS:
        return {"status": "rejected", "error": f"标的 {sym} 不在原生可交易池"}
    return _preview("close_native", sym,
        [{"group": "trade", "key": "原生平仓", "old": "-", "new": f"{sym} 全部原生持仓"}],
        f"原生平仓预览已生成(尚未执行): {sym}。等待用户批准")


def t_open_spot(args):
    """现货开仓(买/卖) — 预览→批准"""
    sym = str(args.get("symbol") or "").upper()
    side = str(args.get("side") or "").lower()
    if sym not in SPOT_SYMS:
        return {"status": "rejected", "error": f"标的 {sym} 不在现货可交易池"}
    if side not in ("buy", "sell"):
        return {"status": "rejected", "error": "side 须 buy(买)/sell(卖)"}
    try:
        notional = float(args.get("notional", 10))
    except Exception:
        return {"status": "rejected", "error": "notional 必须是数字"}
    if not (1 <= notional <= 200):
        return {"status": "rejected", "error": "名义金额须 1-200 USDT"}
    return _preview("open_spot", sym,
        [{"group": "trade", "key": "现货开仓", "old": "-", "new": f"{sym} {'买入' if side=='buy' else '卖出'} 名义{notional}USDT"}],
        f"现货开仓预览已生成(尚未执行): {sym}。等待用户批准")


def t_close_spot(args):
    sym = str(args.get("symbol") or "").upper()
    if sym not in SPOT_SYMS:
        return {"status": "rejected", "error": f"标的 {sym} 不在现货可交易池"}
    return _preview("close_spot", sym,
        [{"group": "trade", "key": "现货平仓", "old": "-", "new": f"{sym} 全部现货持仓"}],
        f"现货平仓预览已生成(尚未执行): {sym}。等待用户批准")


def t_autopilot_create(args):
    """创建AI全托管任务 — 预览→批准"""
    scope = args.get("scope", "all")
    mode = str(args.get("mode", "carry")).lower()
    if mode not in ("carry", "native"):
        return {"status": "rejected", "error": "mode 须 carry(基差套利)/native(原生方向)"}
    return _preview("autopilot_create", str(scope),
        [{"group": "autopilot", "key": "创建托管", "old": "-", "new": f"scope={scope} mode={mode}"}],
        "托管任务创建预览已生成(尚未执行)。等待用户批准")


def t_autopilot_set(args):
    """托管任务 暂停/恢复/取消 — 预览→批准"""
    tid = str(args.get("tid") or "")
    status = str(args.get("status") or "")
    if not tid or status not in ("paused", "running", "cancelled"):
        return {"status": "rejected", "error": "tid 必填, status 须 paused/running/cancelled"}
    zh = {"paused": "暂停", "running": "恢复", "cancelled": "取消"}[status]
    return _preview("autopilot_set", tid,
        [{"group": "autopilot", "key": "托管操作", "old": "-", "new": f"{zh}任务 {tid}"}],
        f"托管任务{zh}预览已生成(尚未执行): {tid}。等待用户批准")


def t_get_jev_decisions(args):
    """M-D3: 读 Jev 决策留痕最近 N 条 (租户隔离)"""
    n = min(int((args or {}).get("n", 10)), 30)
    f = os.path.join(BASE, "data", "jev_decisions.jsonl")
    try:
        lines = open(f, encoding="utf-8").read().strip().splitlines()
        recs = []
        for l in lines[-n:]:
            d = json.loads(l)
            if d.get("uid") is None or d.get("uid") == tenants.current_uid():
                recs.append({k: d[k] for k in ("ts", "event", "signals", "gates", "latency_ms")
                             if k in d})
        return {"Jev最近决策": recs[-10:], "条数": len(recs)}
    except Exception:
        return {"Jev最近决策": [], "条数": 0, "说明": "暂无决策留痕"}


def t_create_task(args):
    """M-A4: AI 创建智能定时任务 (提醒类无资金风险, 直接生效)"""
    try:
        import ai_tasks
    except Exception:
        return {"error": "任务模块不可用"}
    uid = tenants.current_uid()
    return ai_tasks.create(uid, str(args.get("type", "")), str(args.get("name", "")),
                           float(args.get("interval_h") or 0), str(args.get("note", "")),
                           args.get("threshold"))


def t_trade_analysis(args):
    """学习总结: 统计历史交易胜率/盈亏/按标的/方向/模式分布, 提炼经验"""
    import glob
    uid = tenants.current_uid()
    n_win = n_trade = 0
    total_pnl = 0.0
    by_sym, by_side, by_mode = {}, {}, {}
    closes = []
    for fn in sorted(glob.glob(os.path.join(tenants.logs(uid), "*.jsonl"))):
        if not (fn.endswith("carry_trades.jsonl") or fn.endswith("live_orders.jsonl")):
            continue
        mode = "实盘" if fn.endswith("live_orders") else "模拟"
        try:
            for line in open(fn, encoding="utf-8").read().strip().splitlines():
                r = json.loads(line)
                act = str(r.get("action") or "")
                if not any(k in act for k in ("CLOSE", "SELL", "SETTLE", "TP", "SL")):
                    continue
                pnl = float(r.get("pnl_usd") or r.get("pnl") or 0)
                fee = float(r.get("fees") or r.get("user_fee") or 0)
                net = pnl - fee
                sym = r.get("symbol", "?")
                side = r.get("side", "?")
                n_trade += 1
                if net > 0:
                    n_win += 1
                total_pnl += net
                closes.append(net)
                by_sym.setdefault(sym, {"n": 0, "win": 0, "pnl": 0.0})
                by_sym[sym]["n"] += 1
                if net > 0:
                    by_sym[sym]["win"] += 1
                by_sym[sym]["pnl"] += net
                by_side.setdefault(side, {"n": 0, "pnl": 0.0})
                by_side[side]["n"] += 1
                by_side[side]["pnl"] += net
                by_mode.setdefault(mode, {"n": 0, "win": 0, "pnl": 0.0})
                by_mode[mode]["n"] += 1
                if net > 0:
                    by_mode[mode]["win"] += 1
                by_mode[mode]["pnl"] += net
        except Exception:
            pass
    if n_trade == 0:
        return {"总笔数": 0, "说明": "暂无历史平仓交易, 无法总结"}
    win_rate = round(n_win / n_trade * 100, 1)
    avg = round(total_pnl / n_trade, 4)
    max_win = round(max(closes), 4) if closes else 0
    max_loss = round(min(closes), 4) if closes else 0
    sym_stats = {s: {"笔数": v["n"], "胜率%": round(v["win"]/v["n"]*100, 1), "盈亏$": round(v["pnl"], 2)} for s, v in sorted(by_sym.items(), key=lambda x: -x[1]["pnl"])}
    side_stats = {s: {"笔数": v["n"], "盈亏$": round(v["pnl"], 2)} for s, v in sorted(by_side.items(), key=lambda x: -x[1]["pnl"])}
    mode_stats = {m: {"笔数": v["n"], "胜率%": round(v["win"]/v["n"]*100, 1), "盈亏$": round(v["pnl"], 2)} for m, v in by_mode.items()}
    return {"总笔数": n_trade, "胜率%": win_rate, "总盈亏$": round(total_pnl, 2),
            "平均每笔$": avg, "最大单笔盈利$": max_win, "最大单笔亏损$": max_loss,
            "按标的": sym_stats, "按方向": side_stats, "按模式": mode_stats,
            "说明": "净盈亏=毛盈亏-费用; 按标的/方向/模式分布用于提炼策略经验"}


def t_my_positions():
    """M-A2: 当前用户持仓 (模拟纸面+实盘台账) — 租户包裹内执行"""
    try:
        import paper_ops as po
        spot = po.spot_positions()
        native = po.native_positions()
    except Exception as e:
        return {"error": f"持仓读取失败: {str(e)[:150]}"}
    return {
        "现货持仓": [dict(symbol=s["symbol"], 数量=s["qty"], 均价=s["avg_cost"],
                          现价=s["px"], 市值USD=s["value"], 浮动盈亏USD=s["pnl"]) for s in spot],
        "合约纸面持仓": [dict(symbol=n["symbol"], 方向=n["side"], 开仓价=n["entry"],
                              名义USD=n.get("notional"), 数量=n["qty"], 浮动盈亏USD=n["pnl"]) for n in native],
        "说明": "纸面持仓=模拟资金仓位; 实盘持仓以平台Bybit账户为准(见my_balance实盘权限)"}


def t_my_balance():
    """M-A2: 当前用户资金/套餐/实盘权限"""
    from app import funds as f, users as u
    uid = tenants.current_uid()
    try:
        bal = float(f.get_balance(uid) or 0)
        usr = u.get_user(uid) or {}
        dep = bool(f.has_deposit(uid))
        return {"账户余额USDT": bal, "套餐": usr.get("plan"),
                "托管到期": usr.get("plan_expires") or 0,
                "实盘权限": "已开通(充值自动)" if dep or bal > 0 else "未开通(充值后自动开启)",
                "说明": "实盘额度=账户余额, 下单名义金额不能超过余额"}
    except Exception as e:
        return {"error": str(e)[:150]}


def t_my_trades(args):
    """M-A2: 最近 N 笔交易台账 (模拟+实盘)"""
    import glob
    n = min(int((args or {}).get("n", 10)), 30)
    uid = tenants.current_uid()
    rows = []
    for fn in sorted(glob.glob(os.path.join(tenants.logs(uid), "*.jsonl"))):
        if not (fn.endswith("carry_trades.jsonl") or fn.endswith("live_orders.jsonl")):
            continue
        try:
            for line in open(fn, encoding="utf-8").read().strip().splitlines():
                r = json.loads(line)
                rows.append({"时间": r.get("ts"), "模式": "实盘" if fn.endswith("live_orders") else "模拟",
                             "标的": r.get("symbol"), "动作": r.get("action"), "方向": r.get("side"),
                             "盈亏USD": r.get("pnl_usd"), "费用USD": r.get("fees") or r.get("user_fee")})
        except Exception:
            pass
    rows = rows[-n:]
    return {"最近交易": rows, "笔数": len(rows), "说明": "pnl_usd为毛盈亏, 净盈亏=毛盈亏-费用"}


_DISPATCH = {"strategy_status": lambda a: t_strategy_status(), "list_params": lambda a: t_list_params(),
             "get_pnl": lambda a: t_get_pnl(), "git_log": lambda a: t_git_log(),
             "get_micro": lambda a: t_get_micro(),
             "my_positions": lambda a: t_my_positions(), "my_balance": lambda a: t_my_balance(),
             "create_task": t_create_task,
             "backtest_summary": t_backtest_summary, "backtest_history": t_backtest_history,
             "get_jev_decisions": t_get_jev_decisions,
             "open_carry": t_open_carry, "close_carry": t_close_carry,
             "open_native": t_open_native, "close_native": t_close_native,
             "open_spot": t_open_spot, "close_spot": t_close_spot,
             "autopilot_create": t_autopilot_create, "autopilot_set": t_autopilot_set,
             "my_trades": t_my_trades, "trade_analysis": t_trade_analysis,
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
