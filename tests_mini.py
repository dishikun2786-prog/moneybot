#!/usr/bin/env python3
import sys, time
sys.path.insert(0, "dash")
import paper_ops, tenants

with tenants.tenant(27):
    FIXED = {"XRPUSDT": {"spot": 1.5000, "perp": 1.5000}}
    print("paper_ops._prices is:", paper_ops._prices.__name__ if hasattr(paper_ops._prices, "__name__") else paper_ops._prices)
    # 清理
    stc = paper_ops._read(paper_ops._resolve("CARRY_STATE"), {})
    for _s in list((stc.get("positions") or {}).keys()):
        r = paper_ops.close_both(_s)
        print("清理平仓:", r.get("msg", r.get("error")))
    _orig = paper_ops._prices
    paper_ops._prices = lambda: FIXED
    r = paper_ops.open_hedge("XRPUSDT", 15)
    print("open:", r.get("ok"), r.get("msg", r.get("error"))[:60])
    st = paper_ops._read(paper_ops._resolve("CARRY_STATE"), {})
    pos = st.get("positions", {}).get("XRPUSDT", {})
    print("entry:", pos.get("spot_entry"), pos.get("perp_entry"), "fa:", pos.get("funding_acc"))
    pos["funding_acc"] = 0.0004
    paper_ops._write(paper_ops._resolve("CARRY_STATE"), st)
    _chk = paper_ops._read(paper_ops._resolve("CARRY_STATE"), {})
    print("写回后读 fa:", _chk.get("positions", {}).get("XRPUSDT", {}).get("funding_acc"))
    print("CARRY_STATE 路径:", paper_ops._resolve("CARRY_STATE"))
    day_b = st.get("day_pnl", 0)
    _orig_read = paper_ops._read
    def _dbg_read(path, default=None):
        v = _orig_read(path, default)
        if "carry_state" in str(path):
            p2 = (v or {}).get("positions", {}).get("XRPUSDT", {})
            print("close_both 读到 pos.fa:", p2.get("funding_acc"), "| path:", str(path)[-50:])
        return v
    paper_ops._read = _dbg_read
    r2 = paper_ops.close_both("XRPUSDT")
    paper_ops._read = _orig_read
    print("close msg:", r2.get("msg", r2.get("error"))[:100])
    st2 = paper_ops._read(paper_ops._resolve("CARRY_STATE"), {})
    print("day delta:", round(st2.get("day_pnl", 0) - day_b, 4))
    paper_ops._prices = _orig
