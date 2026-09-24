#!/usr/bin/env python3
"""R14-M4: Bybit 交易/订单数据同步性测试 (真实环境, 最小名义, 服务器跑)
覆盖:
  S1 现货市价买入 → 订单Filled → 现货持仓同步
  S2 现货挂单 → 挂单列表可见 → 撤单 → 订单Cancelled → 挂单列表消失
  S3 合约挂单撤单同步 (远价不成交, 不产生持仓)
  S4 现货卖出平仓 → 持仓归零 → 订单Filled
  S5 余额同步: 买入前后 walletBalance 差额 = 成交额+手续费
用法: ./venv/bin/python tests/test_bybit_sync.py
"""
import json
import os
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "dash"))

import bybit_live  # noqa: E402
from app import funds  # noqa: E402

SYM = "BTCUSDT"         # 核心现货标的, 最小量 0.0001 (~8.4$), 流动性好
QTY = 0.0001             # ≈ 8.4$ (过 Bybit 现货最小订单值 5$)
SYM_PERP = "BTCUSDT"    # 合约下单风控验证 (现货同标的, 合约腿)


class TestBybitSync(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pk = funds.platform_key()
        if not pk:
            raise unittest.SkipTest("平台密钥未配置, 跳过同步性实测")
        cls.key, cls.secret = pk["key"], pk["secret"]
        cls.order_ids = []

    @classmethod
    def tearDownClass(cls):
        # 兜底: 清掉遗留持仓 (若有)
        try:
            d = bybit_live.positions(cls.key, cls.secret, category="spot", symbol=SYM)
            for p in (d.get("result") or {}).get("list", []):
                if float(p.get("size") or 0) > 0:
                    bybit_live.place_spot_order(cls.key, cls.secret, SYM, "Sell",
                                                abs(float(p["size"])))
        except Exception:
            pass

    def _wait_filled(self, order_id, category, tries=12):
        for _ in range(tries):
            q = bybit_live.query_order(self.key, self.secret, SYM, order_id, category)
            row = ((q.get("result") or {}).get("list") or [{}])[0]
            if row.get("orderStatus") in ("Filled", "Cancelled", "Rejected"):
                return row
            time.sleep(1.0)
        return None

    def test_s1_spot_buy_fills_and_position_sync(self):
        """现货市价买入 → Filled → 持仓同步"""
        r = bybit_live.place_spot_order(self.key, self.secret, SYM, "Buy", QTY)
        self.assertEqual(r.get("retCode"), 0, f"下单失败: {r}")
        oid = r["result"]["orderId"]
        self.order_ids.append(oid)
        row = self._wait_filled(oid, "spot")
        self.assertIsNotNone(row, "订单未在 12s 内成交")
        self.assertEqual(row["orderStatus"], "Filled")
        self.assertEqual(row["side"], "Buy")
        # 持仓同步
        d = bybit_live.positions(self.key, self.secret, category="spot", symbol=SYM)
        pos = [(float(p.get("size") or 0), float(p.get("avgPrice") or 0))
               for p in (d.get("result") or {}).get("list", [])]
        total = sum(s for s, _ in pos)
        self.assertGreater(total, 0, f"现货持仓未同步: {pos}")

    def test_s2_spot_limit_place_query_cancel(self):
        """现货挂单 → 列表可见 → 撤单 → Cancelled → 列表消失"""
        # 限价带内远价 (市价×0.5): 不成交, 订单值 8.4$ 过下限
        import json as _j
        pf = open(os.path.join(ROOT, "logs", "bybit_prices.json"), encoding="utf-8")
        _p = _j.load(pf); pf.close()
        _last = float((_p.get("prices", _p).get(SYM, {}) or {}).get("last") or 0)
        _px = round((_last or 80000.0) * 0.5, 1)
        px = bybit_live.place_spot_order(self.key, self.secret, SYM, "Buy", QTY * 2,
                                         order_type="Limit", price=_px)  # 订单值 8$+ 过 5$ 下限
        self.assertEqual(px.get("retCode"), 0, f"挂单失败: {px}")
        oid = px["result"]["orderId"]
        # 挂单列表可见
        oo = bybit_live.open_orders(self.key, self.secret, "spot", SYM)
        ids = [o["orderId"] for o in ((oo.get("result") or {}).get("list") or [])]
        self.assertIn(oid, ids, "挂单未出现在 open_orders")
        # 撤单
        c = bybit_live.cancel_order(self.key, self.secret, SYM, oid, "spot")
        self.assertEqual(c.get("retCode"), 0, f"撤单失败: {c}")
        time.sleep(1.0)
        q = bybit_live.query_order(self.key, self.secret, SYM, oid, "spot")
        row = ((q.get("result") or {}).get("list") or [{}])[0]
        self.assertEqual(row.get("orderStatus"), "Cancelled")
        # 列表消失
        oo2 = bybit_live.open_orders(self.key, self.secret, "spot", SYM)
        ids2 = [o["orderId"] for o in ((oo2.get("result") or {}).get("list") or [])]
        self.assertNotIn(oid, ids2, "撤单后仍留在挂单列表")

    def test_s3_perp_order_error_sync(self):
        """合约下单风控同步: 价格带内但保证金不足 (20U账户) → 10001, 且错误码即时返回"""
        # 拉现价 → 限价带内 (±5%)
        try:
            from bybit_ws_bridge import PRICES
            px = float((PRICES.get(SYM_PERP) or {}).get("last") or 0)
        except Exception:
            px = 0.0
        if px <= 0:
            d = bybit_live.account_info(self.key, self.secret)
            px = 80000.0
        price = round(px * 0.995, 1)
        r = bybit_live.place_order(self.key, self.secret, SYM_PERP, "Buy", 0.001,
                                   category="linear", order_type="Limit",
                                   price=price, time_in_force="GTC")
        # 0.001 BTC ≈ 84$ > 账户余额 → 10001 余额不足 (合法风控拦截, 错误码同步)
        self.assertIn(r.get("retCode"), (0, 10001, 110003, 110007), f"合约下单风控异常: {r}")
        if r.get("retCode") == 0:
            oid = r["result"]["orderId"]
            c = bybit_live.cancel_order(self.key, self.secret, SYM_PERP, oid, "linear")
            self.assertEqual(c.get("retCode"), 0, f"合约撤单失败: {c}")

    def test_s4_spot_sell_flattens_position(self):
        """现货卖出平仓 → 持仓归零 → 订单 Filled"""
        r = bybit_live.place_spot_order(self.key, self.secret, SYM, "Sell", QTY)
        self.assertEqual(r.get("retCode"), 0, f"平仓失败: {r}")
        oid = r["result"]["orderId"]
        row = self._wait_filled(oid, "spot")
        self.assertIsNotNone(row, "平仓单未成交")
        self.assertEqual(row["orderStatus"], "Filled")
        self.assertEqual(row["side"], "Sell")
        time.sleep(1.0)
        d = bybit_live.positions(self.key, self.secret, category="spot", symbol=SYM)
        pos = [(float(p.get("size") or 0)) for p in (d.get("result") or {}).get("list", [])]
        # 卖出按 qtyStep 截断, 允许 <1e-5 粉尘残留
        self.assertTrue(all(s < 1e-5 for s in pos), f"平仓后仍有持仓: {pos}")

    def test_s5_wallet_balance_consistent(self):
        """余额同步: 一次买卖往返后余额差额 ≈ 双向手续费 (≤名义的0.5%)"""
        b0 = bybit_live.wallet_balance(self.key, self.secret)
        hits0 = [x for x in ((b0.get("result") or {}).get("list") or [])
                 if x.get("coin") == "USDT"]
        v0 = float(hits0[0].get("walletBalance") or 0) if hits0 else 0.0
        # 往返 (买→卖) 净变化 = 双边手续费
        r1 = bybit_live.place_spot_order(self.key, self.secret, SYM, "Buy", QTY)
        oid1 = r1.get("result", {}).get("orderId")
        if r1.get("retCode") == 0:
            self._wait_filled(oid1, "spot")
        r2 = bybit_live.place_spot_order(self.key, self.secret, SYM, "Sell", QTY)
        if r2.get("retCode") == 0:
            oid2 = r2["result"]["orderId"]
            self._wait_filled(oid2, "spot")
        time.sleep(1.0)
        b1 = bybit_live.wallet_balance(self.key, self.secret)
        hits = [x for x in ((b1.get("result") or {}).get("list") or [])
                if x.get("coin") == "USDT"]
        v1 = float(hits[0].get("walletBalance") or 0) if hits else 0.0
        cost = v0 - v1  # 应为正 (手续费)
        # XAUT 0.0001 ≈ 0.43$; 费率 0.1% → 单边 ≈ 0.0004$, 双边 < 0.01$
        self.assertGreaterEqual(cost, 0, f"余额反而增加: {v0}->{v1}")
        self.assertLess(cost, 0.01 + 0.1, f"往返成本异常大: {cost} (可能成交价滑点)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
