package liquidation

import (
	"testing"

	"github.com/moneybot/aetherhft/internal/fdtd"
)

func TestImpulseSign(t *testing.T) {
	// 空头爆仓(Buy) → 正脉冲; 多头爆仓(Sell) → 负脉冲
	if v := (Event{Size: 10, Side: SideBuy}).Impulse(2.0); v != 20 {
		t.Fatalf("Buy 脉冲应=+20, got %v", v)
	}
	if v := (Event{Size: 10, Side: SideSell}).Impulse(2.0); v != -20 {
		t.Fatalf("Sell 脉冲应=-20, got %v", v)
	}
}

func TestInjectAddsForcingAtMappedIndex(t *testing.T) {
	g := fdtd.New(64, 1.0, 0.01, 0.5)
	// 价格 100 → 索引 10
	priceToIdx := func(p float64) int {
		if p == 100 {
			return 10
		}
		return 0
	}
	inj := NewInjector(g, priceToIdx, 1.0)
	inj.Inject(Event{Price: 100, Size: 5, Side: SideBuy})
	inj.Inject(Event{Price: 100, Size: 3, Side: SideSell})
	// F[10] = +5 -3 = 2
	if g.F[10] != 2 {
		t.Fatalf("F[10] 应=2, got %v", g.F[10])
	}
}

func TestInjectDrivesWave(t *testing.T) {
	// 注入买压脉冲后，波场能量应上升（外源项 → 波阵面演化）
	g := fdtd.New(128, 1.0, 0.01, 0.3)
	c := make([]float64, 128)
	for i := range c {
		c[i] = 0.9
	}
	g.SetWaveSpeed(c)
	inj := NewInjector(g, func(p float64) int {
		return int(p) // 价格直接映射索引
	}, 1.0)

	e0 := g.EnergyFlux()
	for i := 0; i < 20; i++ {
		inj.Inject(Event{Price: 64, Size: 5, Side: SideBuy})
		g.Step()
	}
	e1 := g.EnergyFlux()
	if e1 <= e0 {
		t.Fatalf("注入强平脉冲后能量应上升: e0=%v e1=%v", e0, e1)
	}
}