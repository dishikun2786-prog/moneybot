#!/usr/bin/env python3
"""M7 资金对账/维护 cron (systemd timer pm-funds 每60s):
- 充值对账: Bybit 充值记录 → 匹配唯一金额 → 自动入账
- 提现状态追踪: paid → completed
- 每日 (UTC 03:00 附近): 套餐到期降级
用法: ./venv/bin/python tools/funds_reconcile.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "dash"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from app import funds  # noqa: E402


def main():
    funds.init_db()
    r = funds.reconcile_deposits()
    if r.get("ok"):
        print(f"[funds] 对账: 入账 {r.get('confirmed', 0)} 单, 新待认领 {r.get('unclaimed_new', 0)}",
              flush=True)
    else:
        print(f"[funds] 对账失败: {r.get('error')}", flush=True)
    funds.track_withdraw_status()
    # 每日一次: 套餐到期降级 (UTC 02:50~03:10 窗口)
    hh = time.strftime("%H%M", time.gmtime())
    if "0250" <= hh <= "0310":
        r = funds.check_expirations()
        print(f"[funds] 套餐到期检查: {r}", flush=True)


if __name__ == "__main__":
    main()
