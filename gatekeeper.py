#!/usr/bin/env python3
"""确定性信号门控 + regime 授权 (T2.3, 无外部依赖)
信号门控四查: 新鲜度 / 流动性 / 价差网格 / 限频余量
regime: 波动率分位数 → ATR 仓位系数 (平静/常态/抬升/极端)"""
import json
import os
import time
from bisect import bisect

BASE = os.path.dirname(os.path.abspath(__file__))


class Gatekeeper:
    def __init__(self, cfg_path=None):
        cfg_path = cfg_path or os.path.join(BASE, "gatekeeper_config.json")
        if os.path.exists(cfg_path):
            self.cfg = json.load(open(cfg_path))
        else:
            self.cfg = {"max_data_age_s": 5, "min_size": 100,
                        "max_spread": 0.06, "max_orders_per_s": 5}
        self._order_ts = []

    def check_signal(self, sig):
        """sig: {ts(epoch), bid, ask, bid_size, ask_size}
        返回 (ok: bool, reasons: [str])"""
        reasons = []
        c = self.cfg
        if time.time() - sig.get("ts", 0) > c["max_data_age_s"]:
            reasons.append("stale_data")
        if min(sig.get("bid_size", 0), sig.get("ask_size", 0)) < c["min_size"]:
            reasons.append("illiquid")
        spread = sig.get("ask", 0) - sig.get("bid", 0)
        if spread < 0 or spread > c["max_spread"]:
            reasons.append("bad_spread")
        now = time.time()
        self._order_ts = [t for t in self._order_ts if now - t < 1.0]
        if len(self._order_ts) >= c["max_orders_per_s"]:
            reasons.append("rate_limit")
        if not reasons:
            self._order_ts.append(now)
        return (not reasons), reasons


def pctile_rank(hist, x):
    """x 在 hist 中的分位 (0~1); hist 为空返回 0.5"""
    if not hist:
        return 0.5
    return bisect(sorted(hist), x) / len(hist)


def regime_coef(rvol_pctile, iv_pctile):
    """分位数 → ATR 仓位系数: 平静1.0 / 常态0.8 / 抬升0.5 / 极端0.3"""
    p = max(rvol_pctile, iv_pctile)
    if p < 0.25:
        return 1.0
    if p < 0.60:
        return 0.8
    if p < 0.85:
        return 0.5
    return 0.3


if __name__ == "__main__":
    # 自测
    gk = Gatekeeper()
    ok_cases = [
        ({"ts": time.time(), "bid": 0.30, "ask": 0.32, "bid_size": 200, "ask_size": 150}, True, "正常信号"),
        ({"ts": time.time() - 60, "bid": 0.30, "ask": 0.32, "bid_size": 200, "ask_size": 150}, False, "过期数据"),
        ({"ts": time.time(), "bid": 0.30, "ask": 0.31, "bid_size": 10, "ask_size": 150}, False, "流动性不足"),
        ({"ts": time.time(), "bid": 0.35, "ask": 0.30, "bid_size": 200, "ask_size": 150}, False, "异常价差"),
        ({"ts": time.time(), "bid": 0.30, "ask": 0.80, "bid_size": 200, "ask_size": 150}, False, "价差过大"),
    ]
    for sig, want, name in ok_cases:
        ok, why = gk.check_signal(sig)
        assert ok == want, f"{name} 失败: got {ok} {why}"
        print(f"  ✓ {name}: ok={ok} {why}")
    assert regime_coef(0.1, 0.2) == 1.0
    assert regime_coef(0.7, 0.5) == 0.5
    assert regime_coef(0.95, 0.9) == 0.3
    print("  ✓ regime 系数映射")
    print("gatekeeper.py 自测全部通过")
