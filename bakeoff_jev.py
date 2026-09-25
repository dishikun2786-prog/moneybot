#!/usr/bin/env python3
"""M-D5: Jev bake-off 验证 (自一致性/门控敏感性/L1分歧)
跑完后结论写入 TypeSafe_bakeoff_report.md
"""
import json, os, time, sys

BASE = os.path.expanduser("~/polymarket")
sys.path.insert(0, BASE)
import typesafe_decision as tsd
import jev_engine as je

SYMS = ["BTCUSDT", "ETHUSDT", "XAUTUSDT", "SOLUSDT", "NEARUSDT", "XRPUSDT"]


def l1_signal(funding_ann_pct, theta=5.0, near_settle=False):
    """L1 确定性规则: 费率年化>θ 且非结算时段 → open"""
    if near_settle:
        return "no"
    return "open" if funding_ann_pct > theta else "no"


def main():
    uid = int(sys.argv[1]) if len(sys.argv) > 1 else None
    st = tsd.build_state(uid)
    print("=== 1) 自一致性 (同state连测3次, P值方差) ===")
    runs = []
    for i in range(3):
        r = tsd.decide_open(st, SYMS, theta=5.0)
        runs.append(r["answers"])
        print(f"  第{i+1}次: " + " ".join(
            f"{s[:4]}:{r['answers'][f'open_{s}']['noul']:.2f}" for s in SYMS) +
            f" | risk={r['answers']['risk_level']['score']}")
    var = {s: round(max(abs(runs[0][f"open_{s}"]["noul"] - runs[1][f"open_{s}"]["noul"]),
                        abs(runs[0][f"open_{s}"]["noul"] - runs[2][f"open_{s}"]["noul"]),
                        abs(runs[1][f"open_{s}"]["noul"] - runs[2][f"open_{s}"]["noul"])), 3)
           for s in SYMS}
    print("  最大P值漂移:", var, "平均:", round(sum(var.values()) / len(var), 3))
    print()
    print("=== 2) 门控敏感性 (open_p 0.50/0.65/0.75/0.85 信号变化) ===")
    base = runs[0]
    for p in (0.50, 0.65, 0.75, 0.85):
        opens = [s for s in SYMS if base[f"open_{s}"]["noul"] >= p]
        print(f"  open_p={p}: 开仓信号 {opens if opens else '无'}")
    print()
    print("=== 3) L1 规则 vs Jev 分歧 ===")
    mkt = st.get("行情", {})
    diffs = []
    for s in SYMS:
        ann = mkt.get(s, {}).get("年化费率%", 0)
        l1 = l1_signal(ann, theta=5.0)
        pv = base[f"open_{s}"]["noul"]
        jv = "open" if pv >= 0.65 else ("no" if pv <= 0.35 else "uncertain")
        agree = (l1 == jv) or jv == "uncertain"
        diffs.append({"sym": s, "费率%": ann, "L1": l1, "Jev": jv, "P": pv, "一致": agree})
        print(f"  {s}: 费率{ann:+.2f}% L1={l1} Jev={jv}(P={pv:.2f}) {'✓' if agree else '✗ 分歧'}")
    n_diff = sum(1 for d in diffs if not d["一致"])
    print(f"  分歧数: {n_diff}/6")
    # 写报告
    rep = f"""# TypeSafe Jev bake-off 报告 ({time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())})

## 1. 自一致性 (同state连测3次)
- 各标的P值最大漂移: {var}
- 平均漂移: {round(sum(var.values())/len(var), 3)}
- 结论: {'稳定, 可复现' if sum(var.values())/len(var) < 0.1 else '存在漂移, 门控需留裕度'}

## 2. 门控敏感性
- open_p 越高信号越少 (保守), 当前默认 0.65
- 0.50→{len([s for s in SYMS if base[f'open_{s}']['noul']>=0.5])}个, 0.75→{len([s for s in SYMS if base[f'open_{s}']['noul']>=0.75])}个, 0.85→{len([s for s in SYMS if base[f'open_{s}']['noul']>=0.85])}个

## 3. L1 规则 vs Jev 分歧
| 标的 | 费率年化% | L1规则 | Jev(P) | 一致 |
|---|---|---|---|---|
{chr(10).join(f"| {d['sym']} | {d['费率%']:+.2f} | {d['L1']} | {d['Jev']}({d['P']:.2f}) | {'✓' if d['一致'] else '✗'} |" for d in diffs)}

- 分歧数: {n_diff}/6
- 分歧解读: L1只认费率阈值, Jev综合费率+回测+风险 — 分歧≠错误, 分歧处正是Jev潜在增值点(需事后标签验证)

## 4. 结论 (条件准入判定)
- 当前证据支持: Jev 保持「观测+复核」模式 (信号附在开仓预览卡供用户参考, 不拦截执行)
- 待补验证: ECE校准需24h+真实结果标签 (记录P(开仓)与事后24h费率方向), 样本积累后补测
- 升级路径: 若 ECE 良好且分歧处事后证明 Jev 更准 → 开放 l1_and_mode=1 (Jev否定拦截L1开仓)
"""
    with open(f"{BASE}/TypeSafe_bakeoff_report.md", "w", encoding="utf-8") as f:
        f.write(rep)
    print()
    print(f"报告已写入 {BASE}/TypeSafe_bakeoff_report.md")


if __name__ == "__main__":
    main()
