#!/usr/bin/env python3
"""M-D5 订单簿微观结构特征层 (Aether-HFT 决策思路 → 确定性特征)
把 Aether-HFT 提示词里的「波能/压强、挂谷假挂单过滤、多信号置信融合」落到本项目
可落地的确定性特征层 —— 代码只算特征、模型只做判断、代码按门控执行 (铁律不破)。

数据源: bybit_ws_bridge 每秒原子写的 ~/polymarket/logs/orderbook.json
  books / books_spot : L2 盘口 200 档  {bids:[[p,s],..], asks:[[p,s],..]}
  walls             : 挂单墙检测       {bids:[[p,s],..], asks:[[p,s],..]}
  aggs              : 主动量聚合网格   {step, grid:{bucket:[b15,s15,b60,s60,b300,s300]}}
  basis             : 现货-永续基差    {perp, spot, basis_pct}
零外部依赖、只读文件、永不触钱。
"""
import json
import os

BASE = os.path.expanduser("~/polymarket")
OB_FILE = os.path.join(BASE, "logs", "orderbook.json")

# 确定性风险分权重 (0~1, 越高越不宜入场)
W_SPREAD = 0.25   # 点差过宽
W_DEPTH = 0.20    # 前5档深度过薄
W_IMB = 0.15      # 盘口严重失衡
W_WALL = 0.20     # 单边挂单墙 (spoofing 候选)
W_FLOW = 0.20     # 主动量严重单边


def _read_ob():
    try:
        with open(OB_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _book_metrics(book):
    """L2 盘口 → 点差(bp)/不平衡/前5档深度"""
    if not isinstance(book, dict):
        return {}
    bids = [(float(p), float(s)) for p, s in (book.get("bids") or []) if float(s) > 0]
    asks = [(float(p), float(s)) for p, s in (book.get("asks") or []) if float(s) > 0]
    if not bids or not asks:
        return {}
    best_bid, best_ask = bids[0][0], asks[0][0]
    mid = (best_bid + best_ask) / 2.0
    spread_bp = (best_ask - best_bid) / mid * 10000.0 if mid > 0 else 0.0
    b5 = sum(s for _, s in bids[:5])
    a5 = sum(s for _, s in asks[:5])
    tot = b5 + a5
    imb = (b5 - a5) / tot if tot > 0 else 0.0
    return {"spread_bp": round(spread_bp, 2),
            "imb": round(imb, 3),
            "depth_top": round(min(b5, a5), 2)}


def _wall_metrics(walls):
    """挂单墙 → 买卖侧最大墙 size / 墙不平衡 / 墙数量 (Kakeya 假挂单过滤的轻量近似)"""
    if not isinstance(walls, dict):
        return {}
    wb = [float(s) for _, s in (walls.get("bids") or []) if float(s) > 0]
    wa = [float(s) for _, s in (walls.get("asks") or []) if float(s) > 0]
    wall_bid = max(wb) if wb else 0.0
    wall_ask = max(wa) if wa else 0.0
    tot = wall_bid + wall_ask
    wall_imb = (wall_bid - wall_ask) / tot if tot > 0 else 0.0
    return {"wall_bid": round(wall_bid, 2), "wall_ask": round(wall_ask, 2),
            "wall_imb": round(wall_imb, 3), "n_walls": len(wb) + len(wa)}


def _flow_metrics(aggs):
    """主动量聚合网格 → 60秒 CVD(买-卖) / 流不平衡"""
    if not isinstance(aggs, dict):
        return {}
    grid = aggs.get("grid") or {}
    b60 = s60 = 0.0
    for v in grid.values():
        if isinstance(v, (list, tuple)) and len(v) >= 6:
            b60 += float(v[2])  # 买60
            s60 += float(v[3])  # 卖60
    tot = b60 + s60
    flow_imb = (b60 - s60) / tot if tot > 0 else 0.0
    return {"cvd60": round(b60 - s60, 3), "flow_imb": round(flow_imb, 3)}


def risk_score(m):
    """确定性微观结构风险 0~1 (波能压强+挂谷过滤的复合门控)"""
    s = 0.0
    sp = m.get("spread_bp")
    if sp is not None:
        if sp > 8.0:
            s += W_SPREAD
        elif sp > 4.0:
            s += W_SPREAD * 0.5
    dep = m.get("depth_top")
    if dep is not None and dep < 10.0:
        s += W_DEPTH
    imb = m.get("imb")
    if imb is not None and abs(imb) > 0.4:
        s += W_IMB
    w_imb = m.get("wall_imb")
    if w_imb is not None and abs(w_imb) > 0.5 and m.get("n_walls"):
        s += W_WALL
    f_imb = m.get("flow_imb")
    if f_imb is not None and abs(f_imb) > 0.5:
        s += W_FLOW
    return round(min(s, 1.0), 2)


def features(symbols=None):
    """返回 {sym: 微观特征 dict} (紧凑, 供模型 state 注入与门控)"""
    ob = _read_ob()
    books = ob.get("books") or {}
    walls = ob.get("walls") or {}
    aggs = ob.get("aggs") or {}
    basis = ob.get("basis") or {}
    syms = symbols if symbols else list(books.keys())
    out = {}
    for sym in syms:
        m = {"spread_bp": None, "imb": None, "depth_top": None,
             "wall_bid": 0.0, "wall_ask": 0.0, "wall_imb": None, "n_walls": 0,
             "cvd60": None, "flow_imb": None, "basis_bp": None, "risk": None}
        m.update(_book_metrics(books.get(sym)))
        m.update(_wall_metrics(walls.get(sym)))
        m.update(_flow_metrics(aggs.get(sym)))
        b = basis.get(sym)
        if isinstance(b, dict) and b.get("basis_pct") is not None:
            m["basis_bp"] = round(float(b["basis_pct"]) * 100.0, 2)
        # M-D7: FDTD 波场特征 (E_n/P_dense/c_mean), numpy 缺失时优雅降级
        book = books.get(sym)
        if isinstance(book, dict) and book.get("bids") and book.get("asks"):
            try:
                import wave_field as _wave
                wf = _wave.features_from_book(book["bids"], book["asks"])
                if wf:
                    m["wave"] = wf
            except Exception:
                pass
        m["risk"] = risk_score(m)
        out[sym] = m
    return out


if __name__ == "__main__":
    import sys
    syms = sys.argv[1:] or None
    feats = features(syms)
    if not feats:
        print("无 orderbook.json 或盘口未就绪")
        sys.exit(0)
    print(f"标的总数: {len(feats)}")
    for s, m in list(feats.items())[:20]:
        print(f"{s}: 点差{m['spread_bp']}bp 不平衡{m['imb']} 深度{m['depth_top']} "
              f"墙({m['wall_bid']}/{m['wall_ask']}) CVD{m['cvd60']} 基差{m['basis_bp']}bp risk={m['risk']}")
