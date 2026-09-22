#!/usr/bin/env python3
"""引擎模式管理: 托管(auto)/手动(manual), 两策略独立切换
- mode.json 原子写; 引擎每tick热读; 切换全审计
- 托管 = 模型自动交易; 手动 = 自动交易暂停, 用户决策"""
import json
import os
import time

BASE = os.environ.get("PAPER_BASE") or os.path.expanduser("~/polymarket")
MODE_FILE = f"{BASE}/engine/mode.json"
AUDIT = f"{BASE}/logs/mode_actions.jsonl"
STRATEGIES = ("carry", "paper_pm")
MODES = ("auto", "manual")
_ZH_S = {"carry": "现货×永续套利", "paper_pm": "预测市场对冲"}
_ZH_M = {"auto": "托管", "manual": "手动"}


def load():
    try:
        d = json.load(open(MODE_FILE))
    except Exception:
        d = {}
    return {s: (d.get(s) if d.get(s) in MODES else "auto") for s in STRATEGIES}


def set_mode(strategy, mode):
    if strategy not in STRATEGIES:
        return {"ok": False, "error": f"未知策略: {strategy} (可选: {list(STRATEGIES)})"}
    if mode not in MODES:
        return {"ok": False, "error": f"未知模式: {mode} (可选: auto/manual)"}
    os.makedirs(os.path.dirname(MODE_FILE), exist_ok=True)
    d = load()
    old = d[strategy]
    if old == mode:
        return {"ok": True, "msg": f"{_ZH_S[strategy]} 已是{_ZH_M[mode]}模式", "unchanged": True}
    d[strategy] = mode
    tmp = MODE_FILE + ".tmp"
    json.dump(d, open(tmp, "w"), ensure_ascii=False, indent=1)
    os.replace(tmp, MODE_FILE)
    with open(AUDIT, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                            "strategy": strategy, "old": old, "new": mode},
                           ensure_ascii=False) + "\n")
    return {"ok": True, "msg": f"{_ZH_S[strategy]} 已切换为{_ZH_M[mode]}模式"}
