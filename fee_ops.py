"""平台运营费率/点差配置 (R14-M12 营收体系)
- data/fee_config.json: 按标的白名单的加价倍率(mult)与点差(spread_bp)
- 官方费率表只读参考 (Bybit 实际档位), 管理员改的是倍率/点差
- 用户实付费 = 官方费率 x VIP乘数 x 标的加价倍率
- 平台营收   = 用户实付费 - 官方成本 + 点差收益
"""
import json
import os
import time

import tenants

CFG_FILE = os.path.join(tenants.base(1), "data", "fee_config.json")
DEFAULT_CFG = {
    "version": 1,
    "updated_by": "", "updated_at": "",
    "default": {"perp_mult": 1.0, "spot_mult": 1.0, "spread_bp": 0},
    "symbols": {},   # {SYM: {"perp_mult":..,"spot_mult":..,"spread_bp":..}}
}
# Bybit 官方费率 (单边, taker 近似) — 只读参考
OFFICIAL = {
    "perp": {"BTCUSDT": 0.00055, "ETHUSDT": 0.00055, "SOLUSDT": 0.00055,
             "NEARUSDT": 0.00055, "XRPUSDT": 0.00055, "XAUUSDT": 0.00055,
             "XAGUSDT": 0.00055, "XAUTUSDT": 0.00055},
    "spot": {"BTCUSDT": 0.001, "ETHUSDT": 0.001, "SOLUSDT": 0.001,
             "NEARUSDT": 0.001, "XRPUSDT": 0.001, "XAUTUSDT": 0.001,
             "XAUUSDT": 0.001, "XAGUSDT": 0.001},
}

_cache = {"t": 0, "cfg": None}


def _load():
    cfg = None
    try:
        cfg = json.load(open(CFG_FILE, encoding="utf-8"))
    except Exception:
        cfg = None
    if not cfg or not isinstance(cfg, dict) or "symbols" not in cfg:
        cfg = json.loads(json.dumps(DEFAULT_CFG))
    return cfg


def load():
    """mtime 纳秒缓存, 免每次读盘 — R14-M13: ns 精度保证同秒保存也实时生效"""
    try:
        mt = os.stat(CFG_FILE).st_mtime_ns
    except OSError:
        mt = 0
    if _cache["cfg"] is None or mt > _cache["t"]:
        _cache["cfg"] = _load()
        _cache["t"] = mt
    return _cache["cfg"]


def save(cfg, who="admin"):
    cfg = cfg or {}
    cfg["version"] = 1
    cfg["updated_by"] = who
    cfg["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    cfg.setdefault("default", dict(DEFAULT_CFG["default"]))
    cfg.setdefault("symbols", {})
    os.makedirs(os.path.dirname(CFG_FILE), exist_ok=True)
    tmp = CFG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=1)
    os.replace(tmp, CFG_FILE)
    _cache["cfg"] = None   # 强制下次 load() 重读 (进程内缓存失效)


def sym_mult(sym, channel):
    """标的加价倍率 (channel: perp|spot), 缺省回退 default"""
    cfg = load()
    d = cfg.get("default", {})
    s = (cfg.get("symbols") or {}).get((sym or "").upper(), {})
    v = s.get((channel + "_mult") if channel in ("perp", "spot") else "perp_mult")
    if v is None:
        v = d.get(channel + "_mult", 1.0)
    try:
        return float(v)
    except (TypeError, ValueError):
        return 1.0


def spread_bp(sym):
    cfg = load()
    d = cfg.get("default", {})
    s = (cfg.get("symbols") or {}).get((sym or "").upper(), {})
    v = s.get("spread_bp")
    if v is None:
        v = d.get("spread_bp", 0)
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def official(channel, sym):
    """官方费率 (channel: perp|spot)"""
    return OFFICIAL.get(channel, {}).get((sym or "").upper(), 0.00055)


def spread_apply(px, sym, side):
    """手动成交点按点差偏移: side=long/buy 上浮, short/sell 下浮
    返回 (成交价, 点差收益率)"""
    bp = spread_bp(sym)
    if not bp:
        return px, 0.0
    half = bp / 10000.0 / 2.0
    if side in ("long", "buy", "Buy"):
        return px * (1 + half), bp / 10000.0
    return px * (1 - half), bp / 10000.0
