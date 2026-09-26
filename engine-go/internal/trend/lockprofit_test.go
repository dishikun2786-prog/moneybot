package trend

import "testing"

func TestLockProfitStop(t *testing.T) {
	l := NewLockProfit()
	pos := &Position{Entry: 100, Side: "long", Notional: 100, BreakEven: 0.2, PeakPrice: 100}
	// 价格跌破 1.5×ATR 止损
	d := l.Update(pos, 98, 98, 98, 1.0, -2.0)
	if d.Action != "stop" {
		t.Fatalf("应触发止损: %v", d.Action)
	}
}

func TestLockProfitBreakeven(t *testing.T) {
	l := NewLockProfit()
	pos := &Position{Entry: 100, Side: "long", Notional: 100, BreakEven: 0.2, PeakPrice: 101}
	// 浮盈 0.5 > 保本 0.2，止损上移到保本（100）
	d := l.Update(pos, 100.5, 101, 100.4, 0.5, 0.5)
	if pos.Side == "long" && d.StopPrice < pos.Entry {
		t.Fatalf("保本后止损不应低于入场: %v", d.StopPrice)
	}
}

func TestLockProfitLadder(t *testing.T) {
	l := NewLockProfit()
	// 浮盈 1.2×ATR → partial 30%
	pos := &Position{Entry: 100, Side: "long", Notional: 100, BreakEven: 0.2, PeakPrice: 102}
	d := l.Update(pos, 101.2, 102, 101, 1.0, 1.2)
	if d.Action != "partial" || d.CloseRatio != 0.30 {
		t.Fatalf("1×ATR 应 partial 30%%: %v %.2f", d.Action, d.CloseRatio)
	}
	// 浮盈 2.5×ATR → partial 40%
	pos2 := &Position{Entry: 100, Side: "long", Notional: 100, BreakEven: 0.2, PeakPrice: 103}
	d2 := l.Update(pos2, 102.5, 103, 102, 1.0, 2.5)
	if d2.Action != "partial" || d2.CloseRatio != 0.40 {
		t.Fatalf("2×ATR 应 partial 40%%: %v %.2f", d2.Action, d2.CloseRatio)
	}
	// 浮盈 3.5×ATR → full
	pos3 := &Position{Entry: 100, Side: "long", Notional: 100, BreakEven: 0.2, PeakPrice: 104}
	d3 := l.Update(pos3, 103.5, 104, 103, 1.0, 3.5)
	if d3.Action != "full" || d3.CloseRatio != 1 {
		t.Fatalf("3×ATR 应 full: %v %.2f", d3.Action, d3.CloseRatio)
	}
}
