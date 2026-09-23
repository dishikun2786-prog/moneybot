#!/usr/bin/env python3
"""M2 租户数据隔离测试 (本地可跑, PAPER_BASE 隔离到临时目录)
覆盖: 目录种子 / 上下文切换与恢复 / 状态隔离 / uid1 旧路径兼容 / 引擎动态属性 / AI git 门禁
"""
import json
import os
import sys
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="m2_tenants_test_")
os.environ["PAPER_BASE"] = _TMP
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tenants  # noqa: E402
import paper_ops  # noqa: E402
import engine_mode  # noqa: E402
import carry_engine  # noqa: E402
import paper_engine  # noqa: E402
import swing_cycle  # noqa: E402
import ai_tools  # noqa: E402


class TestTenants(unittest.TestCase):
    def setUp(self):
        tenants.seed(2)
        tenants.seed(3)

    def test_01_seed_contents(self):
        for uid in (2, 3):
            self.assertTrue(os.path.exists(tenants.params_file(uid)), "params 未种子化")
            st = json.load(open(tenants.state("paper", uid)))
            self.assertEqual(st["day_pnl"], 0.0)
            cy = json.load(open(tenants.cycle_state(uid)))
            self.assertEqual(cy["phase"], "IDLE")
            mf = json.load(open(tenants.mode_file(uid)))
            self.assertEqual(mf, {"carry": "auto", "paper_pm": "auto"})

    def test_02_ctx_switch_and_restore(self):
        self.assertEqual(tenants.current(), 1)
        with tenants.tenant(2):
            self.assertEqual(tenants.current(), 2)
            with tenants.tenant(3):
                self.assertEqual(tenants.current(), 3)
            self.assertEqual(tenants.current(), 2)
        self.assertEqual(tenants.current(), 1)

    def test_03_uid1_legacy_paths(self):
        self.assertEqual(tenants.base(1), _TMP)
        self.assertEqual(tenants.state("carry", 1), os.path.join(_TMP, "logs", "carry_state.json"))
        self.assertEqual(tenants.params_file(1), os.path.join(_TMP, "strategy_params.json"))

    def test_04_state_isolation(self):
        # 用户A写持仓 → 只有A的状态文件变化
        with tenants.tenant(2):
            st = paper_ops._read(paper_ops.CARRY_STATE, {})
            st["naked"] = {"BTCUSDT": {"dir": "fwd", "notional": 10.0}}
            paper_ops._write(paper_ops.CARRY_STATE, st)
        b = json.load(open(tenants.state("carry", 3)))
        self.assertEqual(b.get("naked"), {}, "用户B状态被污染!")
        a = json.load(open(tenants.state("carry", 2)))
        self.assertIn("BTCUSDT", a.get("naked", {}))

    def test_05_mode_isolation(self):
        with tenants.tenant(2):
            r = engine_mode.set_mode("carry", "manual")
            self.assertTrue(r.get("ok"))
        m3 = engine_mode.load() if False else json.load(open(tenants.mode_file(3)))
        self.assertEqual(m3.get("carry"), "auto", "用户B模式被污染!")
        m2 = json.load(open(tenants.mode_file(2)))
        self.assertEqual(m2.get("carry"), "manual")

    def test_06_engine_dynamic_attrs(self):
        with tenants.tenant(2):
            self.assertTrue(carry_engine.STATE.endswith(os.path.join("tenants", "2", "logs", "carry_state.json")))
            self.assertTrue(carry_engine.PARAMS_FILE.endswith(os.path.join("tenants", "2", "strategy_params.json")))
            self.assertTrue(paper_engine.STATE.endswith(os.path.join("tenants", "2", "logs", "paper_state.json")))
            self.assertTrue(swing_cycle.ROUNDS_LOG.endswith(os.path.join("tenants", "2", "logs", "swing_rounds.jsonl")))
            self.assertTrue(ai_tools.PENDING_FILE.endswith(os.path.join("tenants", "2", "engine", "ai_pending.json")))
            self.assertTrue(engine_mode.MODE_FILE.endswith(os.path.join("tenants", "2", "engine", "mode.json")))
            # 共享数据不分租户
            self.assertEqual(carry_engine.CARRY, os.path.join(_TMP, "logs", "carry_1m.jsonl"))
            self.assertEqual(paper_engine.CSV, os.path.join(_TMP, "logs", "bybit_pm_fv.csv"))

    def test_07_params_isolation_and_ai_git_gate(self):
        with tenants.tenant(2):
            r = ai_tools.apply_params_direct({"carry": {"theta_in_ann_pct": 7.5}}, "test")
            self.assertTrue(r.get("ok"))
        audit2 = open(tenants.ai_audit(2), encoding="utf-8").read()
        self.assertIn("不入git", audit2, "租户改参应标记不入git")
        p2 = json.load(open(tenants.params_file(2)))
        self.assertEqual(p2["carry"]["theta_in_ann_pct"], 7.5)
        p3 = json.load(open(tenants.params_file(3)))
        self.assertNotEqual(p3.get("carry", {}).get("theta_in_ann_pct"), 7.5, "用户B参数被污染!")
        # git 回退/引擎重启: 非管理员拒绝
        with tenants.tenant(2):
            self.assertEqual(ai_tools.t_git_rollback({"rev": "HEAD~1"})["status"], "rejected")
            self.assertEqual(ai_tools.t_restart_engine({"unit": "pm-dash"})["status"], "rejected")

    def test_08_audit_isolation(self):
        with tenants.tenant(3):
            ai_tools._audit("test", {"x": 1})
        a3 = open(tenants.ai_audit(3), encoding="utf-8").read()
        self.assertIn('"x": 1', a3)
        if os.path.exists(tenants.ai_audit(2)):
            a2 = open(tenants.ai_audit(2), encoding="utf-8").read()
            self.assertNotIn('"x": 1', a2, "用户B审计被污染!")


if __name__ == "__main__":
    unittest.main(verbosity=2)
