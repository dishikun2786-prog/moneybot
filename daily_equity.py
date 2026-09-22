#!/usr/bin/env python3
"""每日权益日志: 每日 23:59 UTC 记录两引擎当日已实现PnL → equity_daily.jsonl"""
import json
import os
import time

BASE = os.path.expanduser("~/polymarket")
OUT = f"{BASE}/logs/equity_daily.jsonl"


def _s(name):
    try:
        return json.load(open(f"{BASE}/logs/{name}"))
    except Exception:
        return {}


def main():
    pm = float(_s("paper_state.json").get("day_pnl", 0.0))
    cy = float(_s("carry_state.json").get("day_pnl", 0.0))
    d = {"date": time.strftime("%Y-%m-%d", time.gmtime()),
         "pm": round(pm, 3), "carry": round(cy, 3),
         "total": round(pm + cy, 3)}
    with open(OUT, "a", encoding="utf-8") as f:
        f.write(json.dumps(d) + "\n")
    print(f"[{d['date']}] 权益记录: pm={d['pm']} carry={d['carry']} total={d['total']}")


if __name__ == "__main__":
    main()
