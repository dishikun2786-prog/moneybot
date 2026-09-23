#!/usr/bin/env python3
"""M1 多用户认证单元测试 (隔离环境: 临时 USERS_DB + 临时凭据文件)
⚠️ 严禁在服务器运行: 会写入 ~/polymarket/.dash_passwd_hash 与 .dash_secret。
检测到真实凭据文件存在时自动跳过。
"""
import os
import sys
import time
import tempfile

TMP = tempfile.mkdtemp(prefix="users_test_")
os.environ["USERS_DB"] = os.path.join(TMP, "users.db")

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "dash"))
from app import auth, captcha, config, users  # noqa: E402

import bcrypt  # noqa: E402

if os.path.exists(config.PASSWD_FILE) or os.path.exists(config.SECRET_FILE):
    print("跳过: 检测到真实凭据文件, 拒绝在真实环境运行")
    sys.exit(0)

_n = 0
_fail = 0


def check(name, cond):
    global _n, _fail
    _n += 1
    if cond:
        print(f"  ✓ {name}")
    else:
        _fail += 1
        print(f"  ✗ FAIL: {name}")


# ---- 准备旧密码哈希 + 签名密钥 (模拟迁移环境) ----
legacy_pw = "oldadminpw1"
os.makedirs(os.path.dirname(config.PASSWD_FILE), exist_ok=True)
with open(config.PASSWD_FILE, "wb") as f:
    f.write(bcrypt.hashpw(legacy_pw.encode(), bcrypt.gensalt()))
with open(config.SECRET_FILE, "w") as f:
    f.write("a" * 64)

print("== 1. 迁移 ==")
check("首次启动执行迁移", users.init_db() is True)
check("二次启动不迁移", users.init_db() is False)
u = users.get_by_username("admin")
check("admin=uid1+admin角色", u is not None and u["id"] == 1 and u["role"] == "admin")
check("旧密码无缝保留", users.verify_login("admin", legacy_pw)[0] is True)

print("== 2. 注册校验 ==")
check("账号过短拒", users.create_user("ab", "a@b.com", "pass1234")[0] is False)
check("非法字符拒", users.create_user("bad name", "a@b.com", "pass1234")[0] is False)
check("邮箱非法拒", users.create_user("alice1", "badmail", "pass1234")[0] is False)
check("密码无数字拒", users.create_user("alice1", "a@b.com", "password")[0] is False)
check("密码过短拒", users.create_user("alice1", "a@b.com", "a1")[0] is False)
ok, _ = users.create_user("alice1", "alice@ex.com", "alice123")
check("合法注册", ok is True)
check("重复账号拒", users.create_user("alice1", "x@x.com", "alice123")[0] is False)

print("== 3. 登录与锁定 ==")
check("错误密码拒", users.verify_login("alice1", "wrongpass1")[0] is False)
check("正确登录", users.verify_login("alice1", "alice123")[0] is True)
for _ in range(4):
    users.verify_login("alice1", "wrongpass1")
ok, msg = users.verify_login("alice1", "wrongpass1")
check("第5次失败锁定", ok is False and "锁定" in msg)
ok, msg = users.verify_login("alice1", "alice123")
check("锁定期间正确密码也拒", ok is False and "锁定" in msg)
con = users._db()
con.execute("UPDATE users SET locked_until=0 WHERE username='alice1'")
con.commit()
con.close()
check("解锁后可登录", users.verify_login("alice1", "alice123")[0] is True)

print("== 4. 验证码 ==")
code, img, mime = captcha.gen()
check("验证码4位+图片", len(code) == 4 and len(img) > 100 and mime.startswith("image/"))
cid = users.captcha_new(code)
check("验证码正确(小写也认)", users.captcha_check(cid, code.lower()) is True)
check("验证码一次性", users.captcha_check(cid, code) is False)
cid2 = users.captcha_new("ABCD")
check("验证码错误", users.captcha_check(cid2, "ZZZZ") is False)
cid3 = users.captcha_new("EFGH")
users._captchas[cid3]["exp"] = time.time() - 10
check("验证码过期", users.captcha_check(cid3, "EFGH") is False)

print("== 5. 会话 ==")
tok = auth.make_session(1, "admin")
check("会话带uid/role", auth.session_user(tok) == {"u": 1, "r": "admin"})
check("伪造会话拒", auth.session_user("garbage") is None)
check("空会话拒", auth.session_user(None) is None)
legacy_tok = auth._serializer().dumps({"ok": True})
check("旧版会话映射admin", auth.session_user(legacy_tok) == {"u": 1, "r": "admin"})

print("== 6. 改密 ==")
ok, msg = auth.change_password(2, "alice123", "newpass99")
check("改密成功", ok is True)
check("新密码可登录", users.verify_login("alice1", "newpass99")[0] is True)
check("旧密码失效", users.verify_login("alice1", "alice123")[0] is False)
ok, msg = auth.change_password(2, "wrongpw1", "newpass99")
check("旧密码错拒改", ok is False)

print("== 7. 禁用 ==")
users.set_status(2, "disabled")
ok, msg = users.verify_login("alice1", "newpass99")
check("禁用后拒登录", ok is False and "禁用" in msg)
users.set_status(2, "active")
check("恢复后可登录", users.verify_login("alice1", "newpass99")[0] is True)

print("== 8. 审计 ==")
con = users._db()
n_audit = con.execute("SELECT COUNT(*) FROM audit").fetchone()[0]
con.close()
check(f"审计留痕({n_audit}条)", n_audit >= 8)

# ---- 清理 ----
for p in (config.PASSWD_FILE, config.SECRET_FILE):
    try:
        os.remove(p)
    except OSError:
        pass
print(f"\n结果: {_n - _fail}/{_n} 通过")
sys.exit(1 if _fail else 0)
