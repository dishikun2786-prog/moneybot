#!/usr/bin/env python3
"""M3 总管理后台测试 (服务器运行, 隔离环境 USERS_DB/ANNOUNCE_DB/PAPER_BASE)
检测到真实凭据文件存在时自动跳过 (绝不动生产数据)
"""
import os
import sys
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="m3_admin_test_")
os.environ["USERS_DB"] = os.path.join(_TMP, "users.db")
os.environ["ANNOUNCE_DB"] = os.path.join(_TMP, "announce.db")
os.environ["PAPER_BASE"] = _TMP

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dash"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import admin, users, config  # noqa: E402
import tenants  # noqa: E402

# 凭据文件路径全部重定向到临时目录 (init_db 迁移/改密绝不触碰生产文件)
config.PASSWD_FILE = os.path.join(_TMP, ".dash_passwd_hash")
config.SECRET_FILE = os.path.join(_TMP, ".dash_secret")
open(config.PASSWD_FILE, "w").write("dummy-legacy-hash")


class TestAdmin(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        users.init_db()  # 会迁移出一个 admin (uid=1)
        admin.init_announce()
        # 建测试用户 (模拟 admin 迁移不走: 直接建普通用户)
        users.create_user("testa1", "ta1@t.com", "Test1234x")
        users.create_user("testb2", "tb2@t.com", "Test1234x")
        cls.ua = users.get_by_username("testa1")["id"]
        cls.ub = users.get_by_username("testb2")["id"]

    def test_01_set_plan(self):
        ok, msg = admin.set_plan(self.ua, "pro", 1)
        self.assertTrue(ok, msg)
        u = users.get_user(self.ua)
        self.assertEqual(u["plan"], "pro")
        self.assertGreater(u["plan_expires"], 0)
        ok, msg = admin.set_plan(self.ua, "nonsense", 1)
        self.assertFalse(ok)

    def test_02_reset_password(self):
        ok, msg = admin.reset_password(self.ub, "NewPass99x", 1)
        self.assertTrue(ok, msg)
        ok2, u = users.verify_login("testb2", "NewPass99x")
        self.assertTrue(ok2)
        ok3, msg3 = admin.reset_password(self.ub, "short", 1)
        self.assertFalse(ok3)

    def test_03_audit_query(self):
        total, rows = admin.audit_query()
        self.assertGreater(total, 0)
        total_a, rows_a = admin.audit_query(uid=self.ua)
        self.assertTrue(all(r["uid"] == self.ua for r in rows_a))
        total_f, rows_f = admin.audit_query(action="plan_change")
        self.assertGreater(total_f, 0)

    def test_04_announce_crud(self):
        ok, msg = admin.announce_add("测试公告一", "info", 1)
        self.assertTrue(ok, msg)
        ok, msg = admin.announce_add("", "info", 1)
        self.assertFalse(ok)  # 空文本拒绝
        rows = admin.announce_list(all_=False)
        self.assertEqual(len(rows), 1)
        aid = rows[0]["id"]
        ok, msg = admin.announce_toggle(aid, False, 1)
        self.assertTrue(ok)
        self.assertEqual(len(admin.announce_list(all_=False)), 0)
        self.assertEqual(len(admin.announce_list(all_=True)), 1)
        # level 非法 → info
        admin.announce_add("第二条", "bogus", 1)
        self.assertEqual(admin.announce_list(all_=True)[0]["level"], "info")

    def test_05_stats_and_user_stats(self):
        s = admin.stats_overview()
        self.assertEqual(s["total"], 3)  # admin(迁移) + 2 测试用户
        self.assertEqual(s["active"], 3)
        st = admin.user_stats(self.ua)
        self.assertIn("capital", st)  # 隔离环境: 100.0 (无持仓无MTM)
        rows = admin.list_users_with_stats()
        self.assertEqual(len(rows), 3)
        self.assertIn("plan_zh", rows[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
