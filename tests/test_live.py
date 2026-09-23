#!/usr/bin/env python3
"""M5 实盘执行器测试 (隔离环境; 不发起真实下单)
覆盖: 风控前置闸全链 / 台账累计 / 状态汇总 / 动作映射
"""
import json
import os
import sys
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="m5_live_test_")
os.environ["USERS_DB"] = os.path.join(_TMP, "users.db")
os.environ["KEYS_DB"] = os.path.join(_TMP, "keys.db")
os.environ["PAPER_BASE"] = _TMP

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dash"))

from app import users, keys, config  # noqa: E402
import live_exec  # noqa: E402
import tenants  # noqa: E402

config.SECRET_FILE = os.path.join(_TMP, ".dash_secret")
config.PASSWD_FILE = os.path.join(_TMP, ".dash_passwd_hash")
open(config.SECRET_FILE, "w").write("live-test-secret")
open(config.PASSWD_FILE, "w").write("x")


class TestLive(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        users.init_db()
        keys.init_db()
        users.create_user("liveu1", "lu1@t.com", "Test1234x")
        cls.uid = users.get_by_username("liveu1")["id"]
        tenants.seed(cls.uid)

    def test_01_gate_plan_required(self):
        ok, err = live_exec._gate(self.uid, "bybit", 10)
        self.assertFalse(ok)
        self.assertIn("实盘版", err)

    def test_02_gate_live_enabled_required(self):
        from app import admin
        admin.set_plan(self.uid, "live", 1)
        ok, err = live_exec._gate(self.uid, "bybit", 10)
        self.assertFalse(ok)
        self.assertIn("实盘未开启", err)

    def test_03_gate_key_required(self):
        keys.set_limits(self.uid, live_enabled=1)
        ok, err = live_exec._gate(self.uid, "bybit", 10)
        self.assertFalse(ok)
        self.assertIn("未绑定 Bybit", err)

    def test_04_gate_notional_validation(self):
        keys.bind(self.uid, "bybit", "K12345678", "S12345678")
        ok, err = live_exec._gate(self.uid, "bybit", 3)
        self.assertFalse(ok)
        self.assertIn("单笔名义", err)
        ok, err = live_exec._gate(self.uid, "bybit", 99)
        self.assertFalse(ok)
        self.assertIn("单笔名义限额", err)
        ok, err = live_exec._gate(self.uid, "bybit", 10)
        self.assertTrue(ok, err)

    def test_05_today_notional_ledger(self):
        import time as _t
        today = _t.strftime("%Y-%m-%d", _t.gmtime())
        live_exec._append(self.uid, {"ts": today + "T00:00:00Z", "venue": "bybit",
                                     "action": "x", "notional": 8.0})
        self.assertAlmostEqual(live_exec._today_notional(self.uid), 8.0)
        # 累计 8+10 > 单笔20*... 不触发(10x) — 验证限额计算
        cap = 20.0 * live_exec.DAY_NOTIONAL_CAP_MULT
        self.assertGreater(cap, 18.0)

    def test_06_status_summary(self):
        st = live_exec.status(self.uid)
        self.assertEqual(st["plan"], "live")
        self.assertTrue(st["live_enabled"])
        self.assertTrue(st["bybit_bound"])
        self.assertFalse(st["pm_bound"])
        self.assertIn("ledger", st)
        self.assertGreaterEqual(st["today_notional"], 0)

    def test_07_pm_gate(self):
        ok, err = live_exec._gate(self.uid, "pm", 10)
        self.assertFalse(ok)
        self.assertIn("未绑定 Polymarket", err)
        keys.bind(self.uid, "pm", "pmkey123456", "pmsecret", extra=json.dumps(
            {"passphrase": "pp", "wallet": "0xabc"}))
        ok, err = live_exec._gate(self.uid, "pm", 10)
        self.assertTrue(ok, err)


if __name__ == "__main__":
    unittest.main(verbosity=2)
