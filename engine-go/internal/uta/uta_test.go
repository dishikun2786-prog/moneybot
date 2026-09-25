package uta

import (
	"math"
	"testing"
)

func TestMRR(t *testing.T) {
	s := NewScheduler()
	if math.Abs(s.MRR(Account{1000, 850})-0.85) > 1e-9 {
		t.Fatalf("MRR 计算错误")
	}
	if s.MRR(Account{0, 10}) != 1 {
		t.Fatalf("无权益应满风险")
	}
}

func TestAllocate(t *testing.T) {
	s := NewScheduler()
	assets := []Asset{
		{Symbol: "BTCUSDT", RScore: 0.9, VaR: 100},
		{Symbol: "ETHUSDT", RScore: 0.3, VaR: 100},
	}
	w, scale, halt := s.Allocate(assets, 1000)
	if halt || scale != 1 {
		t.Fatalf("halt=%v scale=%v", halt, scale)
	}
	var sum float64
	for _, x := range w {
		sum += x
	}
	if math.Abs(sum-1) > 1e-9 {
		t.Fatalf("权重和=%v 应=1", sum)
	}
	if w[0] <= w[1] {
		t.Fatalf("高 RScore 应更高权重: %v vs %v", w[0], w[1])
	}
}

func TestApplyMRR(t *testing.T) {
	s := NewScheduler()
	// MRR ≥ 1 → 熔断
	if _, _, halt := s.ApplyMRR(1.0, []float64{0.5, 0.5}, 1); !halt {
		t.Fatalf("MRR=1 应熔断")
	}
	// MRR ≥ 0.85 → 缩减杠杆
	w, scale, halt := s.ApplyMRR(0.925, []float64{0.5, 0.5}, 1)
	if halt || scale >= 1 {
		t.Fatalf("超线应缩减: scale=%v", scale)
	}
	var sum float64
	for _, x := range w {
		sum += x
	}
	if math.Abs(sum-scale) > 1e-9 {
		t.Fatalf("缩减后和=%v 应=scale %v", sum, scale)
	}
	// MRR < 0.85 → 不缩减
	w2, scale2, halt2 := s.ApplyMRR(0.5, []float64{0.5, 0.5}, 1)
	if halt2 || scale2 != 1 || w2[0] != 0.5 {
		t.Fatalf("健康应原样: %v %v %v", w2, scale2, halt2)
	}
}
