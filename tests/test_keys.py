#!/usr/bin/env python3
"""M4 密钥保险库测试 (服务器, 隔离 KEYS_DB/SECRET_FILE)
验证: AES-GCM 往返 / 掩码 / 明文不出库 / 限额校验 / 解绑
"""
import json
import os
import sys
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="m4_keys_test_")
os.environ["KEYS_DB"] = os.path.join(_TMP, "keys.db")
os.environ["USERS_DB"] = os.path.join(_TMP, "users.db")

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dash"))

from app import keys, config  # noqa: E402

# 签名密钥重定向 (主密钥派生源)
config.SECRET_FILE = os.path.join(_TMP, ".dash_secret")
open(config.SECRET_FILE, "w").write("test-signing-secret-v1")


class TestKeys(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        keys.init_db()

    def test_01_encrypt_roundtrip(self):
        secret = "BybitSecretABC123!@#"
        enc = keys.encrypt(secret)
        self.assertNotEqual(enc, secret)
        self.assertNotIn(secret, enc)
        self.assertEqual(keys.decrypt(enc), secret)

    def test_02_bind_and_mask(self):
        ok, msg = keys.bind(5, "bybit", "ABC123KEY", "XYZ789SECRET")
        self.assertTrue(ok, msg)
        rows = keys.list_keys(5)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["key_masked"], "ABC1...3KEY")
        self.assertNotIn("XYZ789SECRET", json.dumps(rows))  # 明文绝不出库

    def test_03_secrets_roundtrip(self):
        s = keys.get_secrets(5, "bybit")
        self.assertEqual(s["key"], "ABC123KEY")
        self.assertEqual(s["secret"], "XYZ789SECRET")

    def test_04_rebind_updates(self):
        keys.bind(5, "bybit", "NEWKEY999", "NEWSECRET888")
        s = keys.get_secrets(5, "bybit")
        self.assertEqual(s["key"], "NEWKEY999")

    def test_05_unbind(self):
        keys.unbind(5, "bybit")
        self.assertIsNone(keys.get_secrets(5, "bybit"))
        self.assertEqual(keys.list_keys(5), [])

    def test_06_limits_defaults_and_validation(self):
        lim = keys.get_limits(7)
        self.assertEqual(lim["max_notional"], 20.0)
        self.assertEqual(lim["live_enabled"], 0)
        ok, msg = keys.set_limits(7, max_notional=50, daily_loss_cap=10,
                                  max_positions=5, live_enabled=1)
        self.assertTrue(ok, msg)
        lim = keys.get_limits(7)
        self.assertEqual(lim["max_notional"], 50.0)
        self.assertEqual(lim["live_enabled"], 1)
        # 越界拒绝
        ok, msg = keys.set_limits(7, max_notional=0.5)
        self.assertFalse(ok)
        ok, msg = keys.set_limits(7, daily_loss_cap=99999)
        self.assertFalse(ok)

    def test_07_db_permissions(self):
        self.assertEqual(os.stat(os.path.join(_TMP, "keys.db")).st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main(verbosity=2)
