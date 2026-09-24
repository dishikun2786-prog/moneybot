#!/usr/bin/env python3
"""R13c: latest_snapshot 尾部读 vs 全量读结果一致性 (OOM 修复回归)"""
import csv
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")

import paper_engine as pe


def _full(csv_path):
    with open(csv_path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return []
    last_ts = rows[-1]["ts_utc"]
    return [r for r in rows if r["ts_utc"] == last_ts]


def test_tail_matches_full():
    # 构造含引号逗号字段的 CSV (模拟真实格式)
    hdr = "ts_utc,event,market,dir,strike,pm_bid,pm_ask"
    lines = [hdr]
    for i in range(2000):
        lines.append(
            f'2026-09-24 07:0{i % 60:02d}:00,What price will Bitcoin hit?,'
            f'"↑ 100,000",up,100000,0.0{i % 10},0.0{(i + 1) % 10}'
        )
    # 最后 ts 行 (多条: 模拟当前事件全量)
    last_ts = "2026-09-24 08:00:00"
    for j in range(50):
        lines.append(f'{last_ts},Event{j},"↑ 200,000",up,200000,0.5,0.6')
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
        path = f.name
    # 桩: 让 _resolve("CSV") 指向临时文件
    import paper_engine
    paper_engine._resolve = lambda name: path if name == "CSV" else paper_engine._resolve_real(name)
    tail = pe.latest_snapshot()
    full = _full(path)
    assert len(tail) == 50, f"尾部行数错误: {len(tail)}"
    assert len(full) == 50
    assert [r["market"] for r in tail] == [r["market"] for r in full], "行内容不一致"
    assert tail[0]["market"] == '↑ 200,000', "引号逗号字段解析错误"
    os.unlink(path)
    print("R13c tail-read: 一致 ✓ (50 行最后ts, 引号字段正确)")


if __name__ == "__main__":
    # 桩 _resolve 原函数
    import paper_engine
    if not hasattr(paper_engine, "_resolve_real"):
        paper_engine._resolve_real = paper_engine._resolve
    test_tail_matches_full()
    print("R13c OK")
