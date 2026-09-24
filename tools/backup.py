#!/usr/bin/env python3
"""每日用户数据备份 (systemd timer 触发)
- SQLite 在线备份 (users/announce/keys)
- tenants 目录 (用户引擎状态/台账/日志) tar 归档
- 保留 14 天
"""
import glob
import os
import shutil
import sqlite3
import subprocess
import tarfile
import time

BASE = os.path.expanduser("~/polymarket")
OUT = os.path.join(BASE, "backups")
STAMP = time.strftime("%Y%m%d_%H%M%S", time.gmtime())

os.makedirs(OUT, exist_ok=True)

n = 0
# 1. SQLite 在线备份 (不锁生产库)
for db in sorted(glob.glob(os.path.join(BASE, "dash", "*.db"))):
    name = os.path.basename(db)
    dst = os.path.join(OUT, f"{name}.{STAMP}")
    try:
        src = sqlite3.connect(db)
        b = sqlite3.connect(dst)
        src.backup(b)
        b.close()
        src.close()
        n += 1
    except Exception as e:
        print(f"[备份失败] {name}: {e}")

# 2. tenants 目录归档 (排除大日志留尾)
try:
    tgz = os.path.join(OUT, f"tenants_{STAMP}.tgz")
    with tarfile.open(tgz, "w:gz") as tf:
        tf.add(os.path.join(BASE, "tenants"), arcname="tenants")
    n += 1
except Exception as e:
    print(f"[备份失败] tenants: {e}")

# 3. 机密文件备份 (缺失则无法解密密钥/会话 — R14-M5 补)
for secret in (".platform_keys.json", ".dash_secret", ".dash_passwd_hash", ".ai_secrets.json"):
    sp = os.path.join(BASE, secret)
    if os.path.exists(sp):
        try:
            shutil.copy2(sp, os.path.join(OUT, f"{secret}.{STAMP}"))
            n += 1
        except Exception as e:
            print(f"[备份失败] {secret}: {e}")

# 4. 清理 14 天前
cut = time.time() - 14 * 86400
removed = 0
for f in glob.glob(os.path.join(OUT, "*")):
    if os.path.getmtime(f) < cut:
        os.remove(f)
        removed += 1

print(f"[备份完成] {n} 项 → {OUT} (清理 {removed} 个过期)")
