#!/usr/bin/env python3
"""引擎模式管理: 托管(auto)/手动(manual), 两策略独立切换
- mode.json 原子写; 引擎每tick热读; 切换全审计
- 托管 = 模型自动交易; 手动 = 自动交易暂停, 用户决策"""
import json
import os
import time

import tenants

BASE = tenants.ROOT  # 保留: 兼容旧引用 (实际路径走 __getattr__)

def _resolve(name):
    """内部路径解析: 测试 setattr monkeypatch 优先, 否则租户动态解析"""
    if name in globals():
        return globals()[name]
    return _DYN[name]()

_DYN = {
    "MODE_FILE": lambda: tenants.mode_file(),
    "AUDIT": lambda: tenants.audit_mode(),
}


def __getattr__(name):
    f = _DYN.get(name)
    if f:
        return f()
    raise AttributeError(f"module 'engine_mode' has no attribute '{name}'")
STRATEGIES = ("carry", "paper_pm")
MODES = ("auto", "manual")
_ZH_S = {"carry": "现货×永续套利", "paper_pm": "预测市场对冲"}
_ZH_M = {"auto": "托管", "manual": "手动"}


def load():
    try:
        d = json.load(open(_resolve("MODE_FILE")))
    except Exception:
        d = {}
    return {s: (d.get(s) if d.get(s) in MODES else "auto") for s in STRATEGIES}


def set_mode(strategy, mode):
    if strategy not in STRATEGIES:
        return {"ok": False, "error": f"未知策略: {strategy} (可选: {list(STRATEGIES)})"}
    if mode not in MODES:
        return {"ok": False, "error": f"未知模式: {mode} (可选: auto/manual)"}
    os.makedirs(os.path.dirname(_resolve("MODE_FILE")), exist_ok=True)
    d = load()
    old = d[strategy]
    if old == mode:
        return {"ok": True, "msg": f"{_ZH_S[strategy]} 已是{_ZH_M[mode]}模式", "unchanged": True}
    d[strategy] = mode
    tmp = _resolve("MODE_FILE") + ".tmp"
    json.dump(d, open(tmp, "w"), ensure_ascii=False, indent=1)
    os.replace(tmp, _resolve("MODE_FILE"))
    with open(_resolve("AUDIT"), "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                            "strategy": strategy, "old": old, "new": mode},
                           ensure_ascii=False) + "\n")
    return {"ok": True, "msg": f"{_ZH_S[strategy]} 已切换为{_ZH_M[mode]}模式"}
