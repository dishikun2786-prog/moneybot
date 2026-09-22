#!/usr/bin/env python3
"""订单状态机 (Python 原型, T3.1)
状态: DRAFT → SUBMITTED → LIVE → PARTIAL → FILLED → SETTLING → SETTLED
旁路: CANCELED / REJECTED / EXPIRED (终态)
非法转移抛 OrderStateError; 每次转移记录历史 (审计)"""

TERMINAL = {"SETTLED", "CANCELED", "REJECTED", "EXPIRED"}

# 事件 → 目标状态
EVENT_MAP = {
    "SUBMIT": "SUBMITTED",
    "ACK": "LIVE",
    "PARTIAL_FILL": "PARTIAL",
    "FULL_FILL": "FILLED",
    "SETTLE_START": "SETTLING",
    "CANCEL_ACK": "CANCELED",
    "REJECT": "REJECTED",
    "EXPIRE": "EXPIRED",
    "SETTLE_CONFIRMED": "SETTLED",
}

# 合法转移表 (源状态 → 允许的目标状态集合)
TRANSITIONS = {
    "DRAFT": {"SUBMITTED", "REJECTED"},
    "SUBMITTED": {"LIVE", "REJECTED", "CANCELED"},
    "LIVE": {"PARTIAL", "FILLED", "CANCELED", "EXPIRED"},
    "PARTIAL": {"PARTIAL", "FILLED", "CANCELED", "EXPIRED"},
    "FILLED": {"SETTLING"},
    "SETTLING": {"SETTLED"},
}


class OrderStateError(Exception):
    pass


class OrderFSM:
    def __init__(self, order_id, token_id="", side="", size=0, price=0.0):
        self.order_id = order_id
        self.token_id = token_id
        self.side = side
        self.size = size
        self.price = price
        self.state = "DRAFT"
        self.history = [("DRAFT", "init")]

    @property
    def is_terminal(self):
        return self.state in TERMINAL

    def transition(self, event):
        target = EVENT_MAP.get(event)
        if target is None:
            raise OrderStateError(f"未知事件: {event}")
        if target not in TRANSITIONS.get(self.state, set()):
            raise OrderStateError(
                f"非法转移: {self.state} -({event})-> {target} (order={self.order_id})")
        self.state = target
        self.history.append((target, event))
        return self.state


if __name__ == "__main__":
    # 自测: 合法路径全过
    fsm = OrderFSM("o1", size=100, price=0.35)
    seq = ["SUBMIT", "ACK", "PARTIAL_FILL", "FULL_FILL", "SETTLE_START", "SETTLE_CONFIRMED"]
    expect = ["SUBMITTED", "LIVE", "PARTIAL", "FILLED", "SETTLING", "SETTLED"]
    for ev, want in zip(seq, expect):
        assert fsm.transition(ev) == want, f"{ev} → 期望{want}"
    assert fsm.state == "SETTLED" and fsm.is_terminal
    print("  ✓ 合法路径: DRAFT→SUBMITTED→LIVE→PARTIAL→FILLED→SETTLING→SETTLED")
    # 非法转移全拒
    for bad in [("DRAFT", "FULL_FILL"), ("SETTLED", "SUBMIT"),
                ("LIVE", "SETTLE_CONFIRMED"), ("DRAFT", "UNKNOWN_EVT")]:
        f = OrderFSM("t", size=1, price=0.5)
        if bad[0] != "DRAFT":
            for ev in ["SUBMIT", "ACK"]:
                f.transition(ev)
            if bad[0] == "SETTLED":
                for ev in ["PARTIAL_FILL", "FULL_FILL", "SETTLE_START", "SETTLE_CONFIRMED"]:
                    f.transition(ev)
        try:
            f.transition(bad[1])
            raise AssertionError(f"应拒绝: {bad}")
        except OrderStateError:
            print(f"  ✓ 非法转移被拒: {bad}")
    print("order_fsm.py 自测全部通过")
