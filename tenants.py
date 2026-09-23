#!/usr/bin/env python3
"""多租户路径层 (M2 商业化升级)
- 每用户独立状态目录: ~/polymarket/tenants/<uid>/{logs,engine,strategy_params.json}
- admin(uid=1) 沿用旧路径 ~/polymarket/* (零迁移, 现有生产状态不动)
- 共享(中心数据, 不分租户): 行情快照/微结构/kline/books/数据管道文件
- tenant(uid) 上下文管理器持全局 RLock → 同进程内租户操作串行化 (防全局路径竞态)
"""
import json
import os
import shutil
import threading
from contextlib import contextmanager

ROOT = os.environ.get("PAPER_BASE") or os.path.expanduser("~/polymarket")
_LOCK = threading.RLock()
_CUR = 1  # 默认租户 = admin


@contextmanager
def tenant(uid):
    """在当前租户上下文中执行 (嵌套安全, 退出恢复)"""
    global _CUR
    with _LOCK:
        old = _CUR
        _CUR = int(uid or 1)
        try:
            yield
        finally:
            _CUR = old


def current():
    return _CUR


def is_admin():
    return _CUR == 1


def base(uid=None):
    u = int(_CUR if uid is None else uid)
    return ROOT if u == 1 else os.path.join(ROOT, "tenants", str(u))


def logs(uid=None):
    return os.path.join(base(uid), "logs")


def enginedir(uid=None):
    return os.path.join(base(uid), "engine")


def state(kind, uid=None):
    return os.path.join(logs(uid), f"{kind}_state.json")


def trades(kind, uid=None):
    return os.path.join(logs(uid), f"{kind}_trades.jsonl")


def params_file(uid=None):
    return os.path.join(base(uid), "strategy_params.json")


def mode_file(uid=None):
    return os.path.join(enginedir(uid), "mode.json")


def lock_path(uid=None):
    return os.path.join(logs(uid), ".state.lock")


def audit_manual(uid=None):
    return os.path.join(logs(uid), "manual_actions.jsonl")


def audit_mode(uid=None):
    return os.path.join(logs(uid), "mode_actions.jsonl")


def halt_file(uid=None):
    return os.path.join(enginedir(uid), "HALT")


def cycle_state(uid=None):
    return os.path.join(logs(uid), "swing_cycle.json")


def cycle_rounds(uid=None):
    return os.path.join(logs(uid), "swing_rounds.jsonl")


def ai_pending(uid=None):
    return os.path.join(enginedir(uid), "ai_pending.json")


def ai_audit(uid=None):
    return os.path.join(logs(uid), "ai_actions.jsonl")


def equity_daily(uid=None):
    return os.path.join(logs(uid), "equity_daily.jsonl")


def shared_log(name):
    """共享中心数据文件 (恒指向 root, 不分租户)"""
    return os.path.join(ROOT, "logs", name)


_DEFAULT_STATE = {
    "carry": {"positions": {}, "orphans": {}, "naked": {}, "day_pnl": 0.0,
              "cum_pnl": 0.0, "n_rounds": 0, "day": ""},
    "paper": {"positions": {}, "day": "", "day_pnl": 0.0, "n_trades": 0},
    "cycle": {"round": 0, "phase": "IDLE", "ladder": 0, "cum_pnl": 0.0,
              "cycle_day_pnl": 0.0, "day": "", "frozen_until": 0.0,
              "n_rounds": 0, "n_wins": 0},
}


def seed(uid):
    """注册时初始化租户目录: 默认参数 + 空状态 + 双策略托管模式"""
    uid = int(uid)
    os.makedirs(logs(uid), exist_ok=True)
    os.makedirs(enginedir(uid), exist_ok=True)
    pf = params_file(uid)
    if not os.path.exists(pf):
        src = os.path.join(ROOT, "strategy_params.json")
        try:
            shutil.copy(src, pf)
        except Exception:
            json.dump({}, open(pf, "w"), ensure_ascii=False, indent=1)
    for kind, default in _DEFAULT_STATE.items():
        p = state(kind, uid) if kind != "cycle" else cycle_state(uid)
        if not os.path.exists(p):
            json.dump(default, open(p, "w"), ensure_ascii=False, indent=1)
    mf = mode_file(uid)
    if not os.path.exists(mf):
        json.dump({"carry": "auto", "paper_pm": "auto"}, open(mf, "w"),
                  ensure_ascii=False, indent=1)
    return base(uid)
