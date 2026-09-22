#!/usr/bin/env python3
"""paper_ops 手动交易操作单元测试 (隔离环境: PAPER_BASE 指向临时目录)
注意: 须在 import paper_ops 前设置 PAPER_BASE"""
import json
import os
import sys
import tempfile

TMP = tempfile.mkdtemp(prefix="paper_ops_test_")
os.environ["PAPER_BASE"] = TMP
os.makedirs(os.path.join(TMP, "logs"), exist_ok=True)

# 造快照与最新行夹具
SNAP = {"ts": 1790000000000, "lag_ms": 90,
        "prices": {"BTCUSDT": {"last": 86000.0, "spot": 86050.0, "change_pct": 0.5},
                   "ETHUSDT": {"last": 2750.0, "spot": 2755.0, "change_pct": 0.3}}}
json.dump(SNAP, open(os.path.join(TMP, "logs", "bybit_prices.json"), "w"))
row = {"ts": "2026-09-22T00:00:00Z", "symbol": "BTCUSDT", "perp_last": 86000.0,
       "spot": 86050.0, "funding_rate": 0.0001, "next_funding_ts": 1790000000000}
with open(os.path.join(TMP, "logs", "carry_1m.jsonl"), "w") as f:
    f.write(json.dumps(row) + "\n")

sys.path.insert(0, "D:/Program Files/hermes/polymarket_arb")
import paper_ops  # noqa: E402

ok = []


def check(name, cond, extra=""):
    ok.append(cond)
    print(("  ✓ " if cond else "  ✗ ") + name + ((" | " + extra) if extra and not cond else ""))


# 1. 一键对冲
r = paper_ops.open_hedge("BTCUSDT", 10)
check("open_hedge 成功", r["ok"], r.get("error", ""))
st = json.load(open(os.path.join(TMP, "logs", "carry_state.json")))
check("开仓后有双腿持仓", "BTCUSDT" in st["positions"])
check("费用已扣", st["day_pnl"] == -round(0.00155 * 10, 4))
# 2. 重复开仓拒绝
r = paper_ops.open_hedge("BTCUSDT", 10)
check("重复开仓被拒", not r["ok"])
# 3. 名义越界拒绝
r = paper_ops.open_hedge("ETHUSDT", 999)
check("名义越界被拒", not r["ok"])
# 4. 平合约腿 → 孤儿
r = paper_ops.close_perp_leg("BTCUSDT")
check("close_perp_leg 成功", r["ok"])
st = json.load(open(os.path.join(TMP, "logs", "carry_state.json")))
check("合约腿平掉后现货转孤儿", "BTCUSDT" in st["orphans"] and "BTCUSDT" not in st["positions"])
# 5. 孤儿上平合约腿 → 无持仓错误
r = paper_ops.close_perp_leg("BTCUSDT")
check("孤儿上无合约腿可平", not r["ok"])
# 6. 全平 (ETH 开仓后全平)
paper_ops.open_hedge("ETHUSDT", 10)
r = paper_ops.close_both("ETHUSDT")
check("close_both 成功", r["ok"])
st = json.load(open(os.path.join(TMP, "logs", "carry_state.json")))
check("全平后无ETH持仓", "ETHUSDT" not in st["positions"] and "ETHUSDT" not in st["orphans"])
# 7. 转裸仓: 方向校验 (先清理孤儿并重建BTC双腿持仓)
st = json.load(open(os.path.join(TMP, "logs", "carry_state.json")))
st["orphans"] = {}
json.dump(st, open(os.path.join(TMP, "logs", "carry_state.json"), "w"))
r = paper_ops.open_hedge("BTCUSDT", 10)
check("重建BTC持仓", r["ok"], r.get("error", ""))
r = paper_ops.close_spot_to_naked("BTCUSDT", 90000, 89000)  # tp>entry>sl 错误
check("TP/SL方向错误被拒", not r["ok"] and "方向错误" in r["error"])
r = paper_ops.close_spot_to_naked("BTCUSDT", 85140, 86860)  # tp<86000<sl ✓
check("合法TP/SL转裸仓成功", r["ok"], r.get("error", ""))
st = json.load(open(os.path.join(TMP, "logs", "carry_state.json")))
check("裸腿入状态+TP/SL正确", "BTCUSDT" in st["naked"] and st["naked"]["BTCUSDT"]["tp"] == 85140)
# 8. 编辑TP/SL
r = paper_ops.edit_naked_tpsl("BTCUSDT", 85200, 87000)
check("edit_tpsl 成功", r["ok"])
r = paper_ops.edit_naked_tpsl("BTCUSDT", 87000, 85200)
check("edit方向错误被拒", not r["ok"])
# 9. 平裸腿
r = paper_ops.close_naked("BTCUSDT")
check("close_naked 成功", r["ok"])
st = json.load(open(os.path.join(TMP, "logs", "carry_state.json")))
check("裸腿已平", "BTCUSDT" not in st["naked"])
# 10. 未知动作
r = paper_ops.execute("hack", {})
check("未知动作被拒", not r["ok"])
# 11. 审计留痕
audit = [json.loads(l) for l in open(os.path.join(TMP, "logs", "manual_actions.jsonl"))]
check("审计有记录", len(audit) >= 7, f"{len(audit)}条")
trades = [json.loads(l) for l in open(os.path.join(TMP, "logs", "carry_trades.jsonl"))]
check("成交带留痕", len(trades) >= 6, f"{len(trades)}条")

n_fail = sum(1 for x in ok if not x)
print(f"\n结果: {len(ok) - n_fail}/{len(ok)} 通过")
sys.exit(1 if n_fail else 0)
