#!/usr/bin/env python3
"""六层风控引擎 (Python 原型, T3.2; Go 版见 Phase 3)
1 底层聚合限额 2 独立盯市 3 结算窗口 4 自成交防护 5 敞口/日亏/熔断/滑点 6 HALT"""
import json
import os
import time

BASE = os.path.dirname(os.path.abspath(__file__))
HALT_FILE = os.path.join(BASE, "engine", "HALT")

DEFAULT_CFG = {
    "max_open_orders": 8,
    "max_position_usd_per_market": 25,
    "max_position_usd_per_underlying": 40,
    "max_total_exposure_usd": 100,
    "settlement_warning_hours": 6,
    "settlement_force_close_hours": 2,
    "max_daily_loss_usd": 5,
    "circuit_breaker_errors": 5,
    "circuit_breaker_cooldown_s": 600,
    "slippage_max_ticks": 2,
    "regime_coef": [1.0, 0.8, 0.5, 0.3],
}


class RiskEngine:
    def __init__(self, cfg_path=None, halt_file=None):
        cfg_path = cfg_path or os.path.join(BASE, "engine", "risk_config.json")
        if os.path.exists(cfg_path):
            self.cfg = json.load(open(cfg_path))
        else:
            self.cfg = dict(DEFAULT_CFG)
        self.halt_file = halt_file or HALT_FILE
        self._error_count = 0
        self._cooldown_until = 0.0

    def check(self, order, ctx):
        """order: {order_id, token_id, underlying, side, size, price,
                    signal_price, tick_size, settle_in_hours}
        ctx: {open_orders, positions(per market/underlying), total_exposure,
              day_pnl, now}
        返回 (ok: bool, reasons: [str])"""
        reasons = []
        c = self.cfg
        # 6. HALT kill switch
        if os.path.exists(self.halt_file):
            return False, ["HALTED"]
        # 5. 熔断冷却
        if time.time() < self._cooldown_until:
            reasons.append("circuit_breaker_cooldown")
        # 5. 活动订单数
        if len(ctx.get("open_orders", [])) >= c["max_open_orders"]:
            reasons.append("too_many_open_orders")
        # 5. 日亏损
        if ctx.get("day_pnl", 0) <= -c["max_daily_loss_usd"]:
            reasons.append("daily_loss_limit")
        # 5. 滑点: 信号价 vs 当前价 > max_ticks
        px = order.get("price", 0) or 0
        sig = order.get("signal_price", 0) or 0
        tick = order.get("tick_size", 0.001) or 0.001
        if px > 0 and sig > 0 and abs(px - sig) > c["slippage_max_ticks"] * tick:
            reasons.append("slippage")
        # 1. 底层聚合
        u_pos = ctx.get("underlying_exposure", {}).get(order["underlying"], 0)
        if abs(u_pos) + abs(order["size"] * px) > c["max_position_usd_per_underlying"]:
            reasons.append("underlying_limit")
        # 1. 单市场
        m_pos = ctx.get("positions", {}).get(order["token_id"], 0)
        if abs(m_pos) + abs(order["size"] * px) > c["max_position_usd_per_market"]:
            reasons.append("market_limit")
        # 1. 总敞口
        if ctx.get("total_exposure", 0) + abs(order["size"] * px) > c["max_total_exposure_usd"]:
            reasons.append("total_exposure_limit")
        # 3. 结算窗口
        h = order.get("settle_in_hours")
        if h is not None:
            if h < c["settlement_force_close_hours"]:
                reasons.append("settlement_force_close")
            elif h < c["settlement_warning_hours"] and order.get("side") == "BUY":
                reasons.append("settlement_warning_no_new")
        # 4. 自成交: 同 token 反向活动单
        for o in ctx.get("open_orders", []):
            if o.get("token_id") == order["token_id"] and o.get("side") != order["side"]:
                reasons.append("self_trade")
                break
        if not reasons:
            return True, []
        return False, reasons

    def record_error(self):
        """执行层报错 → 熔断计数"""
        self._error_count += 1
        if self._error_count >= self.cfg["circuit_breaker_errors"]:
            self._cooldown_until = time.time() + self.cfg["circuit_breaker_cooldown_s"]
            self._error_count = 0
            return True  # 触发熔断
        return False


if __name__ == "__main__":
    r = RiskEngine()
    good_ctx = {"open_orders": [], "positions": {}, "underlying_exposure": {},
                "total_exposure": 0, "day_pnl": 0, "now": time.time()}
    o = {"order_id": "t1", "token_id": "tok1", "underlying": "BTC", "side": "BUY",
         "size": 10, "price": 0.5, "signal_price": 0.5, "tick_size": 0.01, "settle_in_hours": 30}
    assert r.check(o, good_ctx) == (True, []), "正常单应通过"
    print("  ✓ 正常单通过")
    # 底层聚合超限
    ctx = dict(good_ctx); ctx["underlying_exposure"] = {"BTC": 36}
    ok, why = r.check(o, ctx)
    assert not ok and "underlying_limit" in why, why
    print("  ✓ 底层聚合限额 (36+5 > 40)")
    # 自成交
    ctx = dict(good_ctx); ctx["open_orders"] = [{"token_id": "tok1", "side": "SELL"}]
    ok, why = r.check(o, ctx)
    assert not ok and "self_trade" in why, why
    print("  ✓ 自成交防护")
    # 结算窗口
    o2 = dict(o, settle_in_hours=1)
    ok, why = r.check(o2, good_ctx)
    assert not ok and "settlement_force_close" in why, why
    print("  ✓ 结算窗口强制减仓")
    # 熔断
    assert r.record_error() is False
    for _ in range(3):
        r.record_error()
    assert r.record_error() is True, "第5次应触发熔断"
    ok, why = r.check(o, good_ctx)
    assert not ok and "circuit_breaker_cooldown" in why, why
    print("  ✓ 熔断冷却 (5次错误→600s)")
    # HALT
    os.makedirs(os.path.dirname(HALT_FILE), exist_ok=True)
    open(HALT_FILE, "w").write("halt")
    ok, why = r.check(o, good_ctx)
    assert not ok and "HALTED" in why, why
    os.remove(HALT_FILE)
    print("  ✓ HALT kill switch")
    print("risk.py 自测全部通过")
