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
        out[group] = {zh.get(group, {}).get(k, k): v for k, v in params.items()}
    return {"参数": out, "说明": "改参数需走审批流程(M7上线), 当前为只读模式"}


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
    {"type": "function", "function": {"name": "git_log",
        "description": "查看策略代码与参数版本历史 (最近10条, 只读)",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "run_backtest",
        "description": "用候选入场阈值θ跑现货永续套利回测(近2个月), 供参数对比决策 (只读, 不落地)",
        "parameters": {"type": "object", "properties": {"theta": {
            "type": "number", "description": "入场阈值: 资金费率年化% (0-20, 默认5)"}}, "required": ["theta"]}}},
]

_DISPATCH = {"strategy_status": lambda a: t_strategy_status(), "list_params": lambda a: t_list_params(),
             "get_pnl": lambda a: t_get_pnl(), "git_log": lambda a: t_git_log(),
             "run_backtest": t_run_backtest}


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
