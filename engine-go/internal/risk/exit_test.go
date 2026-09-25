package risk

import "testing"

func TestBreakEvenCost(t *testing.T) {
	s := DefaultFeeSchedule()
	e := NewExitManager(s, 0, 0.3)
	pos := &Position{Notional: 1000, SlippageEst: 1}
	// vip0 roundTrip = 1000*0.002 = 2; 盈亏平衡 = 2+1 = 3
	if got := e.BreakEvenCost(pos); got != 3 {
		t.Fatalf("盈亏平衡成本应=3, got %v", got)
	}
}

func TestShouldExitOnlyWhenCollapseAndAboveBreakEven(t *testing.T) {
	s := DefaultFeeSchedule()
	e := NewExitManager(s, 0, 0.3)
	pos := &Position{Notional: 1000, SlippageEst: 1, FloatingPnL: 5} // 5 > 3 已覆盖

	// 崩溃 + 覆盖 → 平仓
	if !e.ShouldExit(pos, 10, 100) {
		t.Fatal("崩溃且覆盖手续费时应平仓")
	}
	// 未崩溃(动能仍强) → 不平
	if e.ShouldExit(pos, 80, 100) {
		t.Fatal("动能未崩溃不应平仓")
	}
	// 已崩溃但浮盈未覆盖手续费 → 不平(避免手续费侵蚀)
	pos2 := &Position{Notional: 1000, SlippageEst: 1, FloatingPnL: 1} // 1 < 3
	if e.ShouldExit(pos2, 10, 100) {
		t.Fatal("浮盈未覆盖手续费不应平仓")
	}
}