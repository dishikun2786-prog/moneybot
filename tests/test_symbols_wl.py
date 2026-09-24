#!/usr/bin/env python3
"""R13c: 标的收敛 + PM 下线防回归测试 (TestClient)
- /api/instruments 只返回白名单 4 标的 (linear) + BTC/ETH (spot)
- /api/pm/tokens 返回空 + off
- 管理 API ok 字段 (P0 回归)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/../dash")

from starlette.testclient import TestClient  # noqa: E402
from app import main as m, auth, config  # noqa: E402

c = TestClient(m.app)
ck = {config.COOKIE_NAME: auth.make_session(1, "admin")}

# R14-M2: 标的扩展 SOL/NEAR/XRP 后, linear 白名单 = 8 标的 (XAUT spot-only 不在 linear)
WL = {"BTCUSDT", "ETHUSDT", "XAUUSDT", "XAGUSDT", "SOLUSDT", "NEARUSDT", "XRPUSDT"}


def test_instruments_whitelist():
    d = c.get("/api/instruments", cookies=ck).json()
    assert d.get("ok"), "instruments 缺 ok"
    lin = {x["symbol"] for x in d["linear"]}
    spot = {x["symbol"] for x in d["spot"]}
    assert lin == WL, f"linear 白名单不符: {lin}"
    assert spot == {"BTCUSDT", "ETHUSDT", "XAUTUSDT", "SOLUSDT", "NEARUSDT", "XRPUSDT"}, f"spot 白名单不符: {spot}"
    # 不应包含任何非白名单标的
    assert not (lin - WL), "linear 有白名单外标的"


def test_pm_off():
    d = c.get("/api/pm/tokens", cookies=ck).json()
    assert d.get("off") is True, "pm/tokens off 标记缺失"
    assert len(d.get("rows", [])) == 0, "pm/tokens 应返回空"
    assert d.get("total") == 0, "pm/tokens total 应为 0"


def test_admin_ok_fields():
    # P0 回归: 管理 API 全部带 ok 字段
    for path in ("/api/admin/stats", "/api/admin/audit", "/api/admin/users",
                 "/api/admin/announcements"):
        d = c.get(path, cookies=ck).json()
        assert "ok" in d, f"{path} 缺 ok 字段"


if __name__ == "__main__":
    test_instruments_whitelist()
    test_pm_off()
    test_admin_ok_fields()
    print("R13c 白名单防回归: 3/3 OK")
