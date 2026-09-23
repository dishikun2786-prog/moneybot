#!/usr/bin/env python3
"""R12c: 总管理员用户名设置为 admin (幂等)
- 查 users 表: 有 admin 角色 → 若用户名非 admin 则改名 (密码/其他字段不变)
- 无 admin 角色 → 把 uid 最小/注册最早的账号升级为 admin 并改名
- 用户名 admin 已被占用(非 admin 角色) → 报告冲突, 不动
"""
import sqlite3
import sys

con = sqlite3.connect("dash/users.db")
con.row_factory = sqlite3.Row
rows = con.execute("select * from users").fetchall()
print(f"[setup-admin] 总用户 {len(rows)}")
for r in rows:
    print(f"  uid={r['id']} user={r['username']!r} email={r['email']!r} role={r['role']} status={r['status']}")

admins = [r for r in rows if r["role"] == "admin"]
conflict = [r for r in rows if r["username"] == "admin" and r["role"] != "admin"]

if conflict:
    print("[setup-admin] ❌ 用户名 admin 已被非管理员占用:", [c["id"] for c in conflict])
    sys.exit(1)

if admins:
    a = min(admins, key=lambda r: r["id"])
    if a["username"] == "admin":
        print("[setup-admin] ✅ 总管理员已是 admin (uid=%d), 无需改动" % a["id"])
        sys.exit(0)
    old = a["username"]
    con.execute("update users set username='admin' where id=?", (a["id"],))
    con.commit()
    print(f"[setup-admin] ✅ 总管理员 uid={a['id']} 用户名 {old!r} → 'admin' (密码不变)")
else:
    # 无管理员: 把注册最早的账号升级
    if not rows:
        print("[setup-admin] ❌ 无任何用户, 需先注册或用注册接口建号")
        sys.exit(1)
    a = min(rows, key=lambda r: r["id"])
    con.execute("update users set username='admin', role='admin' where id=?", (a["id"],))
    con.commit()
    print(f"[setup-admin] ✅ uid={a['id']} ({a['username']!r}) 升级为 admin 并改名 'admin' (密码不变)")

# 复核
r = con.execute("select id, username, role from users where role='admin'").fetchall()
print("[setup-admin] 复核:", [dict(x) for x in r])
