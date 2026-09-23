#!/usr/bin/env python3
"""租户引擎调度器 (M2 多用户商业化)
- 每60s为每个 active 用户账户运行三引擎一轮 (套利/PM对冲/循环恢复)
- admin(uid=1) 由独立 systemd 服务 (pm-carry/pm-hedge/pm-cycle) 托管, 此处跳过
- 行情/微结构数据共享 (不分租户); 每用户状态/参数/审计独立
- 单进程顺序遍历: 每用户一轮 < 数秒, 无并发竞态
用法: ./venv/bin/python tenant_scheduler.py  (systemd: pm-tenants)
"""
import json
import os
import sys
import time
import traceback

BASE = os.path.expanduser("~/polymarket")
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "dash"))
sys.path.insert(0, os.path.join(BASE, "dash", "app"))

import tenants  # noqa: E402
import paper_engine  # noqa: E402
import carry_engine  # noqa: E402
import swing_cycle  # noqa: E402

try:
    from app import users  # noqa: E402  (dash/app/users.py)
except Exception:
    users = None

TICK = 60
_ERR_TTL = 600  # 连续错误日志节流
_last_err_log = 0.0


def active_users():
    if users is None:
        return []
    rows = []
    try:
        rows = users.list_users()
    except Exception:
        return []
    return [r for r in rows if r.get("status") == "active" and r.get("id") != 1]


def run_user(uid):
    with tenants.tenant(uid):
        t0 = time.time()
        n = 0
        try:
            carry_engine.cycle()
            n += 1
        except Exception as e:
            print(f"  [uid {uid}] carry err: {e}", flush=True)
        try:
            paper_engine.cycle()
            n += 1
        except Exception as e:
            print(f"  [uid {uid}] paper err: {e}", flush=True)
        try:
            swing_cycle.cycle()
            n += 1
        except Exception as e:
            print(f"  [uid {uid}] cycle err: {e}", flush=True)
        return time.time() - t0, n


def main():
    print(f"[pm-tenants] 租户引擎调度器启动 (tick={TICK}s, base={tenants.ROOT})", flush=True)
    while True:
        try:
            us = active_users()
            for u in us:
                try:
                    dur, n = run_user(u["id"])
                except Exception:
                    dur, n = 0, 0
                if dur > 5:
                    print(f"  [uid {u['id']}] 一轮耗时 {dur:.1f}s ({n}引擎)", flush=True)
            time.sleep(TICK)
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"[pm-tenants] 主循环错误: {type(e).__name__}: {e}", flush=True)
            time.sleep(TICK)


if __name__ == "__main__":
    main()
