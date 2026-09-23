"""R12 P2: PM 标的上下线管理 (logs/pm_market_admin.json)
- state: on/off; 下线标的全站屏蔽 (移动端浏览/搜索/下单/API)
- admin 后台: 搜索+勾选+批量上下线"""
import json
import os
import time

from . import config

FILE = os.path.join(config.BASE, "logs", "pm_market_admin.json")


def _load():
    try:
        with open(FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save(st):
    tmp = FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)
    os.replace(tmp, FILE)


def state_of(key):
    return (_load().get(key) or {}).get("state", "on")


def is_off(key):
    return state_of(key) == "off"


def set_state(keys, state, note=""):
    if state not in ("on", "off"):
        raise ValueError("state 须 on/off")
    st = _load()
    now = time.time()
    for k in keys:
        st[k] = {"state": state, "note": note, "t": now}
    _save(st)
    return len(keys)


def off_keys():
    """全部下线 key 集合 (供 /api/pm/tokens 过滤)"""
    st = _load()
    return {k for k, v in st.items() if v.get("state") == "off"}


def list_markets(q="", state="", offset=0, limit=100):
    """聚合市场列表: pm_tokens 按 key 聚合 + 中文 + 管理状态 + 搜索过滤"""
    try:
        with open(os.path.join(config.BASE, "logs", "pm_tokens.json"),
                  encoding="utf-8") as f:
            toks = json.load(f)
    except Exception:
        return {"rows": [], "total": 0, "off_n": 0}
    try:
        with open(os.path.join(config.BASE, "logs", "pm_zh.json"),
                  encoding="utf-8") as f:
            zh = json.load(f)
    except Exception:
        zh = {"t": {}, "q": {}}
    admin = _load()
    m = {}
    for t in toks:
        k = t.get("key")
        if k not in m:
            m[k] = {"key": k, "title": t.get("title", ""),
                    "title_zh": (zh.get("t", {}).get(t.get("title") or "") or ""),
                    "cat": t.get("cat", "other"), "ev_vol": t.get("ev_vol", 0),
                    "mk_vol": t.get("mk_vol", 0)}
    rows = list(m.values())
    ql = (q or "").strip().lower()
    if ql:
        rows = [r for r in rows if ql in (r["title"] or "").lower()
                or ql in (r["title_zh"] or "").lower() or ql in r["key"].lower()]
    off_n = sum(1 for r in rows if admin.get(r["key"], {}).get("state") == "off")
    if state in ("on", "off"):
        rows = [r for r in rows if admin.get(r["key"], {}).get("state", "on") == state]
    rows.sort(key=lambda r: -(r.get("ev_vol") or 0))
    total = len(rows)
    page = rows[offset:offset + limit]
    for r in page:
        r["state"] = admin.get(r["key"], {}).get("state", "on")
        r["note"] = admin.get(r["key"], {}).get("note", "")
    return {"rows": page, "total": total, "off_n": off_n,
            "has_more": offset + limit < total}
