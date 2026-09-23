#!/usr/bin/env python3
"""M7 资金体系单元测试 (隔离: funds.DB_FILE 临时库 + bybit 假模块 + 平台密钥桩)"""
import json
import os
import sys
import tempfile
import types

TMP = tempfile.mkdtemp(prefix="funds_test_")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dash"))

# 假 bybit_live 模块 (funds 内延迟 import)
fake_bybit = types.ModuleType("bybit_live")
FAKE_DEPOSITS = []   # 对账用: 充值记录
FAKE_WITHDRAW = {"ok": True, "id": "wd123"}


def fake_req(key, secret, method, path, params=None, body=None, timeout=10):
    if "query-record" in path and "withdraw" in path:
        return {"retCode": 0, "result": {"rows": [{"txID": FAKE_WITHDRAW["id"], "status": "success"}]}}
    if "deposit/query-record" in path:
        return {"retCode": 0, "result": {"rows": FAKE_DEPOSITS}}
    if "deposit/query-address" in path:
        return {"retCode": 0, "result": {"chains": [{"chainType": "TRON (TRC20)",
                                                       "addressDeposit": "TMVjTEST123456789012345678901234567"}]}}
    if "withdraw/create" in path:
        return {"retCode": 0 if FAKE_WITHDRAW["ok"] else 110001,
                "retMsg": "ok" if FAKE_WITHDRAW["ok"] else "withdraw not enabled",
                "result": {"id": FAKE_WITHDRAW["id"]}}
    return {"retCode": 0, "result": {}}


fake_bybit._req = fake_req
sys.modules["bybit_live"] = fake_bybit

# users 桩 (buy_plan/expire 用, 注册进 sys.modules 防真 users 的 bcrypt 依赖)
fake_users = types.ModuleType("app.users")
_U = {1: {"plan": "free", "plan_expires": 0}}
fake_users.get_user = lambda uid: _U.get(uid)
fake_users.set_plan = lambda uid, code, exp: _U.update({uid: {"plan": code, "plan_expires": exp}})
fake_users.expire_plans = lambda: 0
sys.modules["app.users"] = fake_users

from app import funds  # noqa: E402

funds.DB_FILE = os.path.join(TMP, "funds.db")
funds.PK_FILE = os.path.join(TMP, "platform_keys.json")

# 平台密钥桩 (用假加密)
funds.keys_mod = types.SimpleNamespace(
    encrypt=lambda s: "ENC(" + s + ")", decrypt=lambda s: s[4:-1])
funds.platform_key = lambda: {"key": "K", "secret": "S"}
funds.init_db()

ok = []


def check(name, cond, extra=""):
    ok.append(cond)
    print(("  ✓ " if cond else "  ✗ ") + name + ((" | " + str(extra)[:120]) if extra and not cond else ""))


# 1. 余额
b = funds.add_balance(1, 100, "deposit", "tx1", "测试入账")
check("入账后余额 100", b == 100, b)
b = funds.add_balance(1, -30, "withdraw", "w1", "提现")
check("出账后余额 70", b == 70, b)
check("余额查询 70", funds.get_balance(1) == 70)
tx = funds.tx_list(1)
check("流水 2 条", len(tx) == 2, len(tx))

# 2. 充值单 (唯一金额)
r = funds.create_deposit(1, 100)
check("充值单创建", r["ok"] and 100 < r["amount_unique"] < 101, r)
check("地址 = 平台地址", r["address"].startswith("TMVjTEST"), r["address"])
r2 = funds.create_deposit(1, 100)
check("两单金额尾数不同", r2["amount_unique"] != r["amount_unique"])
check("充值金额下限拦截", not funds.create_deposit(1, 1)["ok"])

# 3. 对账: 金额匹配自动入账
FAKE_DEPOSITS[:] = [{"chain": "TRX", "amount": str(r["amount_unique"]), "txID": "dep1", "status": "3"},
                    {"chain": "TRX", "amount": "999.1234", "txID": "dep2", "status": "3"}]
res = funds.reconcile_deposits()
check("对账 1 单入账", res["confirmed"] == 1, res)
check("入账后余额增加", funds.get_balance(1) == round(70 + r["amount_unique"], 4),
      funds.get_balance(1))
orders = funds.my_deposits(1)
check("充值单状态 confirmed", any(o["txid"] == "dep1" and o["status"] == "confirmed" for o in orders))
uc = funds.unclaimed_list()
check("未匹配进认领池", any(o["txid"] == "dep2" and o["status"] == "unclaimed" for o in uc))
# 幂等: 再跑一遍不重复入账
res = funds.reconcile_deposits()
check("重复对账不重复入账", res["confirmed"] == 0, res)

# 4. 认领
uc2 = funds.unclaimed_list()
oid = next(o["id"] for o in uc2 if o["txid"] == "dep2")
r = funds.claim_deposit(0, oid, 2)
check("管理员认领入账", r["ok"], r)
check("uid2 收到 999.1234", funds.get_balance(2) == 999.1234, funds.get_balance(2))

# 5. 提现
r = funds.create_withdraw(2, 999, "TInvalidAddress")
check("非法地址拒绝", not r["ok"])
r = funds.create_withdraw(2, 999, "TSfpNoVUraA5maC4Rgy1QJBgvmjVAh8F9N")  # 34位T开头
check("余额不足拒绝(999+1>999.12)", not r["ok"], r)
r = funds.create_withdraw(2, 500, "TSfpNoVUraA5maC4Rgy1QJBgvmjVAh8F9N")
check("提现申请成功(冻结501)", r["ok"], r)
check("提现后余额", funds.get_balance(2) == round(999.1234 - 501, 4), funds.get_balance(2))
wid = r["order_id"]

# 6. 审批拒绝 → 全额退回
r = funds.review_withdraw(0, wid, False, "地址可疑")
check("拒绝退回", r["ok"], r)
check("余额恢复", funds.get_balance(2) == 999.1234, funds.get_balance(2))

# 7. 审批通过 → 自动打款
r = funds.create_withdraw(2, 500, "TSfpNoVUraA5maC4Rgy1QJBgvmjVAh8F9N")
wid2 = r["order_id"]
r = funds.review_withdraw(0, wid2, True, "")
check("审批通过打款", r["ok"], r)
ws = funds.my_withdraws(2)
check("提现单状态 paid + txid", any(w["id"] == wid2 and w["status"] == "paid" and w["txid"] == "wd123" for w in ws))
funds.track_withdraw_status()
ws = funds.my_withdraws(2)
check("状态追踪 completed", any(w["id"] == wid2 and w["status"] == "completed" for w in ws))

# 8. 打款失败路径 → 退回
FAKE_WITHDRAW["ok"] = False
r = funds.create_withdraw(2, 100, "TSfpNoVUraA5maC4Rgy1QJBgvmjVAh8F9N")
r = funds.review_withdraw(0, r["order_id"], True, "")
check("打款失败回退余额", not r["ok"] and "退回" in r.get("error", ""), r)
check("失败后余额恢复(500已真打款)", funds.get_balance(2) == 498.1234, funds.get_balance(2))
FAKE_WITHDRAW["ok"] = True

# 9. 套餐
plans = funds.plans_list()
check("默认 3 套餐", len(plans) == 3, len(plans))
r = funds.save_plan("vip", "VIP版", 199, "全部+专属", 9, 1, is_new=True)
check("新增套餐", r["ok"], r)
r = funds.save_plan("free", "免费版", 5, "", 0, 1)
check("free 价格不可改", not r["ok"])
r = funds.save_plan("vip", "VIP豪华版", 299, "", 9, 1)
check("修改套餐", r["ok"])
check("改后价格 299", any(p["code"] == "vip" and p["price"] == 299 for p in funds.plans_list()))
r = funds.delete_plan("vip")
check("删除套餐", r["ok"])
check("free 不可删", not funds.delete_plan("free")["ok"])

# 10. 余额购买套餐
funds.add_balance(1, 500, "deposit", "x", "充值")
bal_before = funds.get_balance(1)
r = funds.buy_plan(1, "pro")
check("购买 pro 成功", r["ok"], r)
check("扣款 30", funds.get_balance(1) == round(bal_before - 30, 4), funds.get_balance(1))
check("plan 已生效", _U[1]["plan"] == "pro")
check("到期时间 ~30天", abs(_U[1]["plan_expires"] - (__import__("time").time() + 30 * 86400)) < 3600)
r = funds.buy_plan(1, "live")
check("续买 live 成功", r["ok"], r)
check("plan 升级 live", _U[1]["plan"] == "live")
# 余额耗尽后购买失败
funds.add_balance(1, -1000, "admin", "x", "清空")
r = funds.buy_plan(1, "pro")
check("余额不足买套餐拒绝", not r["ok"])

# 11. 设置
r = funds.set_setting("withdraw_fee", "2")
check("改手续费", r["ok"] and funds.get_setting("withdraw_fee") == "2")

print(f"\nM7 test_funds: {sum(ok)}/{len(ok)} 通过")
sys.exit(0 if all(ok) else 1)
