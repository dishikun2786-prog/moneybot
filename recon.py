#!/usr/bin/env python3
"""每日对账框架 (T4.5)
三方对账: 本地DB(orders/positions) vs 交易所API vs 链上余额
当前: 框架+自测 (实盘启用后接入真实数据源)
规则: 差异>0 → critical 告警 + HALT"""
import json
import os
import time

BASE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(BASE, "logs", "recon_latest.json")


def reconcile(local_orders, api_orders, chain_balances, api_balances):
    """各源输入 dict; 返回差异清单 [{level, detail}]"""
    diffs = []
    ids_l, ids_a = set(local_orders), set(api_orders)
    if ids_l != ids_a:
        diffs.append({"level": "order", "detail":
                      f"订单集合不一致: 本地独有{ids_l - ids_a} API独有{ids_a - ids_l}"})
    for k in set(local_orders) & set(api_orders):
        if local_orders[k] != api_orders[k]:
            diffs.append({"level": "order", "detail":
                          f"订单状态不一致: {k} 本地={local_orders[k]} API={api_orders[k]}"})
    for k in set(chain_balances) | set(api_balances):
        if chain_balances.get(k) != api_balances.get(k):
            diffs.append({"level": "balance", "detail":
                          f"余额不一致: {k} 链上={chain_balances.get(k)} API={api_balances.get(k)}"})
    return diffs


def run():
    # TODO(实盘): 从 SQLite / PM CLOB API / Bybit API / 链上 RPC 拉取真实数据
    local_orders, api_orders = {}, {}
    chain_balances, api_balances = {}, {}
    diffs = reconcile(local_orders, api_orders, chain_balances, api_balances)
    out = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "diffs": diffs}
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    json.dump(out, open(LOG, "w"), ensure_ascii=False, indent=1)
    if diffs:
        print("⚠️ 对账不平:", json.dumps(diffs, ensure_ascii=False))
        # TODO: 触发 Telegram critical 告警 + HALT
    else:
        print(f"[{out['ts']}] 对账通过 (0差异)")
    return diffs


if __name__ == "__main__":
    # 自测: 构造差异
    assert reconcile({"a": 1}, {"a": 1}, {"w": 5}, {"w": 5}) == []
    d = reconcile({"a": 1, "b": 2}, {"a": 1}, {"w": 5}, {"w": 4})
    assert len(d) == 2, d
    print("recon.py 自测通过 (2类差异可检出)")
    run()
