package risk

import (
	"math"
	"testing"
)

func TestNetEV(t *testing.T) {
	// P=0.7, target=100, stop=50, fee=2, slip=3 → 70-15-2-3=50
	ev := NetEV(0.7, 100, 50, 2, 3)
	if math.Abs(ev-50) > 1e-9 {
		t.Fatalf("NetEV 应=50, got %v", ev)
	}
}

func TestAllowPositiveExpectancy(t *testing.T) {
	s := DefaultFeeSchedule()
	f := NewExpectancyFilter(s, 20, 2) // threshold=20, feeMargin=2
	sig := Signal{PSuccess: 0.7, TargetProfit: 100, StopLoss: 50, SlippageEst: 3, Notional: 1000}
	// vip0: fee=1000*0.002=2; NetEV=70-15-2-3=50
	ok, ev := f.Allow(sig, 0)
	if !ok || math.Abs(ev-50) > 1e-9 {
		t.Fatalf("应放行, ok=%v ev=%v", ok, ev)
	}
}

func TestDenyNegativeExpectancy(t *testing.T) {
	s := DefaultFeeSchedule()
	f := NewExpectancyFilter(s, 20, 2)
	sig := Signal{PSuccess: 0.5, TargetProfit: 20, StopLoss: 20, SlippageEst: 3, Notional: 1000}
	// fee=2; NetEV=10-10-2-3=-5
	ok, ev := f.Allow(sig, 0)
	if ok {
		t.Fatalf("负期望应拒绝, ev=%v", ev)
	}
}

func TestDenyFeeMargin(t *testing.T) {
	// 正期望但未远超手续费(NetEV < feeMargin×fee) → 拒绝
	s := DefaultFeeSchedule()
	f := NewExpectancyFilter(s, 5, 5) // threshold=5, feeMargin=5
	sig := Signal{PSuccess: 0.6, TargetProfit: 30, StopLoss: 10, SlippageEst: 3, Notional: 1000}
	// fee=2; NetEV=18-4-2-3=9 > threshold(5), 但 9 < 5×2=10 → 拒绝
	ok, ev := f.Allow(sig, 0)
	if ok {
		t.Fatalf("未远超手续费应拒绝, ev=%v", ev)
	}
	if math.Abs(ev-9) > 1e-9 {
		t.Fatalf("NetEV 应=9, got %v", ev)
	}
}