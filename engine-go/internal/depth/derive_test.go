package depth

import (
	"testing"

	"github.com/moneybot/aetherhft/internal/feed"
)

func TestDetectWalls(t *testing.T) {
	// 构造 30 档，其中 1 档明显是大墙
	levels := make([]feed.Level, 30)
	for i := range levels {
		levels[i] = feed.Level{Price: float64(100 - i), Size: 1.0}
	}
	levels[0] = feed.Level{Price: 99.5, Size: 1000.0} // 大墙
	walls := DetectWalls(levels)
	found := false
	for _, w := range walls {
		if w[1] == 1000.0 {
			found = true
		}
	}
	if !found {
		t.Fatalf("大墙未检出: %v", walls)
	}
}

func TestComputeMicroRisk(t *testing.T) {
	bids := []feed.Level{{Price: 100, Size: 50}, {Price: 99.9, Size: 50}}
	asks := []feed.Level{{Price: 100.1, Size: 50}, {Price: 100.2, Size: 50}}
	m := ComputeMicro(bids, asks, nil, nil, 0, 0, 0)
	// 点差 0.1/100.05*10000 ≈ 10bp → 风险分应含点差项 0.25
	if m.SpreadBP < 9.9 || m.SpreadBP > 10.1 {
		t.Fatalf("spread_bp 异常: %v", m.SpreadBP)
	}
	if m.Risk < 0.2 {
		t.Fatalf("风险分应含点差项: %v", m.Risk)
	}
}

func TestAggWindow(t *testing.T) {
	now := int64(1000000)
	trades := []TradePoint{
		{Ts: now - 5000, Side: "Buy", V: 10, P: 100.0},
		{Ts: now - 5000, Side: "Sell", V: 3, P: 100.0},
	}
	g := AggWindow(trades, 0.1, now)
	// 应有 1 个桶，买15=10 卖15=3
	if len(g) != 1 {
		t.Fatalf("桶数异常: %v", g)
	}
	for _, v := range g {
		if v[0] != 10 || v[1] != 3 {
			t.Fatalf("CVD 异常: %v", v)
		}
	}
}
