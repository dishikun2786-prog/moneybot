#!/usr/bin/env python3
"""M2 租户隔离生产 e2e (服务器, 使用真实 users.db 的临时用户)
步骤: 建两临时用户→seed→发会话cookie→HTTP验证隔离→清理
注意: 跑在 pm-dash 同机, 会话由 auth.make_session 直接签发(验证码已由单测覆盖)
"""
import json
import os
import sys
import time
import urllib.request

BASE = os.path.expanduser("~/polymarket")
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "dash"))
sys.path.insert(0, os.path.join(BASE, "dash", "app"))

from app import auth, config, users  # noqa: E402
import tenants  # noqa: E402

URL = "http://127.0.0.1:8080"
COOKIE = config.COOKIE_NAME
passed, failed = 0, 0


def ok(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ✓ {name}")
    else:
        failed += 1
        print(f"  ✗ {name} {extra}")


def http(method, path, body=None, cookie=None):
    req = urllib.request.Request(URL + path, method=method)
    req.add_header("Content-Type", "application/json")
    if cookie:
        req.add_header("Cookie", f"{COOKIE}={cookie}")
    data = json.dumps(body).encode() if body is not None else None
    try:
        r = urllib.request.urlopen(req, data=data, timeout=30)
        return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, {}


def main():
    t = int(time.time())
    ua, ub = f"m2a_{t}", f"m2b_{t}"
    # 建临时用户 (直插 DB, 跳过验证码—已由单测覆盖)
    okc, msg = users.create_user(ua, f"{ua}@t.com", "Test1234x")
    ok("建用户A", okc, msg)
    okc2, msg2 = users.create_user(ub, f"{ub}@t.com", "Test1234x")
    ok("建用户B", okc2, msg2)
    A = users.get_by_username(ua)
    B = users.get_by_username(ub)
    uidA, uidB = A["id"], B["id"]
    ca = auth.make_session(uidA, "user")
    cb = auth.make_session(uidB, "user")

    # 1. A 开裸腿仓 (tp/sl = 远不可达触发价, 防引擎触发)
    s, r = http("POST", "/api/manual/trade",
                {"action": "open_naked", "symbol": "BTCUSDT", "dir": "fwd",
                 "notional": 10, "tp": 0.01, "sl": 1e9}, ca)
    ok("A开裸腿仓", s == 200 and r.get("ok"), str(r)[:100])

    # 2. A 的 pnl 可见持仓
    s, r = http("GET", "/api/pnl", cookie=ca)
    pos = r.get("positions") or []
    ok("A可见裸仓持仓", s == 200 and any("裸" in p.get("key", "") for p in pos))

    # 3. B 的 pnl 无持仓
    s, r = http("GET", "/api/pnl", cookie=cb)
    ok("B无持仓(隔离)", s == 200 and not (r.get("positions") or []))

    # 4. A 成交带留痕, B 空
    s, r = http("GET", "/api/tape", cookie=ca)
    ok("A成交带留痕", s == 200 and len(r) > 0)
    s, r = http("GET", "/api/tape", cookie=cb)
    ok("B成交带为空(隔离)", s == 200 and len(r) == 0)

    # 5. A 改参数 → 只影响 A
    s, r = http("POST", "/api/params", {"changes": {"carry": {"theta_in_ann_pct": 7.7}}}, ca)
    ok("A改参数成功", s == 200 and r.get("ok"), str(r)[:100])
    s, r = http("GET", "/api/strategy", cookie=ca)
    ok("A参数已改", r.get("params", {}).get("carry", {}).get("theta_in_ann_pct") == 7.7)
    s, r = http("GET", "/api/strategy", cookie=cb)
    ok("B参数未受影响", r.get("params", {}).get("carry", {}).get("theta_in_ann_pct") != 7.7)

    # 6. A 切手动 → 只影响 A
    s, r = http("POST", "/api/mode", {"strategy": "carry", "mode": "manual"}, ca)
    ok("A切手动", s == 200 and r.get("ok"))
    s, r = http("GET", "/api/mode", cookie=cb)
    ok("B仍托管", r.get("carry") == "auto")

    # 7. B 平仓操作不影响 A 持仓
    s, r = http("POST", "/api/manual/trade", {"action": "close_both", "symbol": "BTCUSDT"}, cb)
    s2, r2 = http("GET", "/api/pnl", cookie=ca)
    ok("B平仓不影响A持仓", any("裸" in p.get("key", "") for p in (r2.get("positions") or [])))

    # 8. 分享管理 = 仅管理员: B 被拒
    s, r = http("POST", "/api/share/generate", cookie=cb)
    ok("B生成分享被拒(403)", s == 403)

    # 9. 租户目录已建
    ok("A租户目录存在", os.path.isdir(tenants.base(uidA)))
    ok("B租户目录存在", os.path.isdir(tenants.base(uidB)))

    # 清理: 删临时用户 + 租户目录 (直删SQL: 仅测试临时用户)
    con = users._db()
    try:
        con.execute("DELETE FROM audit WHERE uid IN (?,?)", (uidA, uidB))
        con.execute("DELETE FROM users WHERE id IN (?,?)", (uidA, uidB))
        con.commit()
    finally:
        con.close()
    import shutil
    shutil.rmtree(tenants.base(uidA), ignore_errors=True)
    shutil.rmtree(tenants.base(uidB), ignore_errors=True)
    print(f"\n结果: {passed}/{passed + failed} 通过")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
