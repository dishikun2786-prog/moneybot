#!/usr/bin/env python3
"""登录链路完整验证: 验证码持久化 + 全流程登录 (服务器本地跑)"""
import sys, subprocess, json, sqlite3, glob
sys.path.insert(0, "dash")
from app import users

# 1. 验证码持久化单测
cid = users.captcha_new("XY987")
ok = users.captcha_check(cid, "xy987")  # 小写兼容
print("1. 验证码生成+校验(小写):", ok)
cid2 = users.captcha_new("ZZZ1")
users.captcha_check(cid2, "wrong")
ok3 = users.captcha_check(cid2, "ZZZ1")
print("2. 错1次后正确校验:", ok3)
cid3 = users.captcha_new("EXP1")
users.captcha_check(cid3, "bad")
users.captcha_check(cid3, "bad")
users.captcha_check(cid3, "bad")
users.captcha_check(cid3, "bad")
ok4 = users.captcha_check(cid3, "bad")  # 第5次错 → 销毁
ok5 = users.captcha_check(cid3, "EXP1")  # 已销毁 → False
print("3. 错5次销毁+不可再用:", (not ok4) and (not ok5))
# 一次性: 正确即销毁
cid6 = users.captcha_new("ONCE1")
ok6a = users.captcha_check(cid6, "ONCE1")
ok6b = users.captcha_check(cid6, "ONCE1")  # 应 False
print("4. 一次性(用后失效):", ok6a and (not ok6b))

# 2. 完整登录流程: 生成验证码→DB查code→curl登录
cid7 = users.captcha_new("LOGOK")
con = sqlite3.connect("dash/users.db")
con.row_factory = sqlite3.Row
row = con.execute("SELECT code FROM captchas WHERE id=?", (cid7,)).fetchone()
con.close()
print("5. DB 读出验证码:", row["code"] if row else "读不到(失败!)")
r = subprocess.run(["curl", "-s", "-X", "POST", "http://127.0.0.1:8080/api/login",
                    "-H", "Content-Type: application/json",
                    "-d", json.dumps({"username": "nouser_xyz", "password": "wrongpw",
                                      "captcha_id": cid7, "captcha_code": row["code"]})],
                   capture_output=True, text=True, timeout=20)
print("6. curl 登录(错误密码):", r.stdout[:120])
print("   → 若报「密码/用户错误」而非「验证码错误」= 验证码持久化链路全通")
