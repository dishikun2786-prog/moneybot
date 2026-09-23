#!/usr/bin/env python3
"""R12 P2: PM 标的管理测试 — 批量上下线/搜索勾选/权限隔离/全局过滤"""
import json
import os
import sys
import tempfile
import types

TMP = tempfile.mkdtemp(prefix="pm_admin_")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 桩 tenants (paper_ops 间接依赖不涉及, 但 main 会 import)
import paper_ops  # noqa: E402 (先确保 cwd 可导入)

BASE_BACKUP = None


def patch_config():
    import dash.app.config as config
    global BASE_BACKUP
    BASE_BACKUP = config.BASE
    config.BASE = TMP


def make_fake_logs():
    os.makedirs(os.path.join(TMP, "logs"), exist_ok=True)
    toks = [
        {"key": "e1|m1", "token": "T1", "title": "Bitcoin above 100k?", "cat": "crypto", "ev_vol": 9000},
        {"key": "e1|m1", "token": "T2", "title": "Bitcoin above 100k?", "cat": "crypto", "ev_vol": 9000},
        {"key": "e2|m2", "token": "T3", "title": "Election winner?", "cat": "politics", "ev_vol": 500},
    ]
    with open(os.path.join(TMP, "logs", "pm_tokens.json"), "w") as f:
        json.dump(toks, f)
    with open(os.path.join(TMP, "logs", "pm_zh.json"), "w") as f:
        json.dump({"t": {"Bitcoin above 100k?": "比特币会突破10万吗？"}, "q": {}}, f)


ok = []
def check(n, c, x=None):
    ok.append(c)
    print(("  OK " if c else "  XX ") + n + ("" if c else " | " + str(x)[:150]))


patch_config()
make_fake_logs()
from dash.app import pm_admin  # noqa: E402

# 1. 初始: 全部 on
lst = pm_admin.list_markets()
check("初始 2 个市场", lst["total"] == 2, lst)
check("初始 off_n=0", lst["off_n"] == 0, lst)

# 2. 批量下线 e1|m1
n = pm_admin.set_state(["e1|m1"], "off", "测试下线")
check("下线 1 个", n == 1, n)
check("off_keys 含 e1|m1", "e1|m1" in pm_admin.off_keys(), pm_admin.off_keys())
check("is_off(e1|m1)", pm_admin.is_off("e1|m1"))
check("is_off(e2|m2)=False", not pm_admin.is_off("e2|m2"))

# 3. 状态过滤 + 搜索(中文)
lst = pm_admin.list_markets(state="off")
check("state=off 过滤 1 个", lst["total"] == 1, lst)
lst = pm_admin.list_markets(q="比特币")
check("中文搜索命中 1 个", lst["total"] == 1, lst)
lst = pm_admin.list_markets(q="election")
check("英文搜索命中 1 个", lst["total"] == 1 and lst["rows"][0]["key"] == "e2|m2", lst)

# 4. 批量上线
n = pm_admin.set_state(["e1|m1", "e2|m2"], "on")
check("批量上线 2 个", n == 2, n)
check("off_keys 空", not pm_admin.off_keys(), pm_admin.off_keys())

# 5. 非法 state 拒绝
try:
    pm_admin.set_state(["e1|m1"], "ban")
    check("非法 state 拒绝", False, None)
except ValueError:
    check("非法 state 拒绝", True)

# 6. 分页
lst = pm_admin.list_markets(limit=1)
check("分页 limit=1 → 1 行 has_more", len(lst["rows"]) == 1 and lst["has_more"], lst)

# 7. API 权限隔离 (main 路由)
patch_config()
import dash.app.main as main_mod  # noqa: E402

from starlette.testclient import TestClient  # noqa: E402

client = TestClient(main_mod.app)
r = client.get("/api/admin/pm-markets")
check("未授权 401/403", r.status_code in (401, 403), r.status_code)

print(f"\nR12 test_pm_admin: {sum(ok)}/{len(ok)} 通过")
sys.exit(0 if all(ok) else 1)
