#!/usr/bin/env python3
"""engine_mode 模式管理单元测试 (隔离环境)"""
import json
import os
import sys
import tempfile

TMP = tempfile.mkdtemp(prefix="mode_test_")
os.environ["PAPER_BASE"] = TMP
os.makedirs(os.path.join(TMP, "logs"), exist_ok=True)
sys.path.insert(0, "D:/Program Files/hermes/polymarket_arb")
import engine_mode as m

ok = []


def check(name, cond, extra=""):
    ok.append(cond)
    print(("  ✓ " if cond else "  ✗ ") + name + ((" | " + extra) if extra and not cond else ""))


check("默认双托管", m.load() == {"carry": "auto", "paper_pm": "auto"})
r = m.set_mode("carry", "manual")
check("切换carry→manual", r["ok"])
check("两策略独立", m.load()["carry"] == "manual" and m.load()["paper_pm"] == "auto")
r = m.set_mode("paper_pm", "manual")
check("paper_pm独立切换", r["ok"] and m.load()["paper_pm"] == "manual")
r = m.set_mode("carry", "hack")
check("非法模式拒绝", not r["ok"])
r = m.set_mode("fx", "manual")
check("非法策略拒绝", not r["ok"])
r = m.set_mode("carry", "manual")
check("重复切换返回unchanged", r.get("unchanged") is True)
r = m.set_mode("carry", "auto")
check("切回auto", r["ok"] and m.load()["carry"] == "auto")
audit = [json.loads(l) for l in open(os.path.join(TMP, "logs", "mode_actions.jsonl"))]
check("切换审计留痕", len(audit) == 3, f"{len(audit)}条")

n_fail = sum(1 for x in ok if not x)
print(f"\n结果: {len(ok) - n_fail}/{len(ok)} 通过")
sys.exit(1 if n_fail else 0)
