package fdtd

import (
	"math"
	"testing"
)

// totalEnergy 计算离散总能量（动能 + 势能），用于阻尼衰减检验。
func totalEnergy(g *Grid) float64 {
	var e float64
	for i := 0; i < g.N; i++ {
		d := g.u[i] - g.uPrev[i] // 动能 (du/dt)²
		e += d * d
	}
	for i := 1; i < g.N; i++ {
		d := g.u[i] - g.u[i-1] // 势能 (du/dx)²
		e += d * d
	}
	return e
}

func TestDampedWaveDecays(t *testing.T) {
	g := New(256, 1.0, 0.01, 0.5) // c=0.9 → CFL=0.009 <1
	c := make([]float64, 256)
	for i := range c {
		c[i] = 0.9
	}
	g.SetWaveSpeed(c)
	for i := range g.u {
		x := float64(i) - 128
		g.u[i] = 0.5 * math.Exp(-x*x/200)
		g.uPrev[i] = g.u[i]
	}
	e0 := totalEnergy(g)
	for i := 0; i < 500; i++ {
		g.Step()
	}
	e1 := totalEnergy(g)
	if e1 >= e0 {
		t.Fatalf("阻尼波动方程总能量未衰减: e0=%v e1=%v", e0, e1)
	}
}

func TestForcedResonanceInjectsEnergy(t *testing.T) {
	g := New(256, 1.0, 0.01, 0.1)
	c := make([]float64, 256)
	for i := range c {
		c[i] = 0.9
	}
	g.SetWaveSpeed(c)
	for i := range g.F {
		g.F[i] = 0.001
	}
	e0 := totalEnergy(g)
	for i := 0; i < 200; i++ {
		g.Step()
	}
	e1 := totalEnergy(g)
	if e1 <= e0 {
		t.Fatalf("驱动下总能量应上升: e0=%v e1=%v", e0, e1)
	}
}

func BenchmarkStepN1024(b *testing.B) {
	g := New(1024, 1.0, 0.01, 0.5)
	c := make([]float64, 1024)
	for i := range c {
		c[i] = 0.9
	}
	g.SetWaveSpeed(c)
	b.ReportAllocs()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		g.Step()
	}
}

func BenchmarkStepN4096(b *testing.B) {
	g := New(4096, 1.0, 0.01, 0.5)
	c := make([]float64, 4096)
	for i := range c {
		c[i] = 0.9
	}
	g.SetWaveSpeed(c)
	b.ReportAllocs()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		g.Step()
	}
}