package resonance

import "testing"

func TestHistRing(t *testing.T) {
	h := NewHist(4)
	for _, v := range []float64{1, 2, 3, 4, 5} {
		h.Add(v)
	}
	if h.n != 4 {
		t.Fatalf("n=%d want 4", h.n)
	}
	if h.Mean() != 3.5 { // (2+3+4+5)/4
		t.Fatalf("mean=%v want 3.5", h.Mean())
	}
	if h.Percentile(3) != 0.5 { // 2,3 <= 3
		t.Fatalf("percentile=%v want 0.5", h.Percentile(3))
	}
}

func TestMacro(t *testing.T) {
	fh := NewHist(10)
	oh := NewHist(10)
	for i := 0; i < 10; i++ {
		fh.Add(0.0001)
		oh.Add(1000 + float64(i)*10)
	}
	// funding 异常高 + OI 高位 → M 应显著 > 0.5
	m := ComputeMacro(0.001, 1090, fh, oh)
	if m.M <= 0.5 {
		t.Fatalf("M=%v 应>0.5 (高费率+高OI)", m.M)
	}
	// OI 低位 → OIPercent 低
	m2 := ComputeMacro(0.0001, 950, fh, oh)
	if m2.OIPercent >= 0.5 {
		t.Fatalf("OIPercent=%v 应<0.5", m2.OIPercent)
	}
}

func TestMicroAndDecide(t *testing.T) {
	// 强能量 + 高挂谷 + 低拓扑风险 → 微观共振高
	mi := ComputeMicro(10, 0.9, 0.1, 5)
	if mi.M <= 0.5 {
		t.Fatalf("micro M=%v 应>0.5", mi.M)
	}
	ma := Macro{M: 0.8}
	d := Decide(ma, mi, 0.5)
	if !d.Fire {
		t.Fatalf("R=%v 应触发", d.R)
	}
	// 拓扑风险高 → 微观低
	mi2 := ComputeMicro(10, 0.9, 0.95, 5)
	if mi2.M >= 0.3 {
		t.Fatalf("micro M=%v 应<0.3 (高拓扑风险)", mi2.M)
	}
}

func TestPoolReuse(t *testing.T) {
	c := AcquireContext(8)
	if len(c.Macros) != 8 || len(c.Micros) != 8 || len(c.Decs) != 8 {
		t.Fatalf("context len 应 8")
	}
	c.Macros[0].M = 0.9
	ReleaseContext(c)
	c2 := AcquireContext(4)
	if c2.Macros[0].M != 0 {
		t.Fatalf("复用对象未清零: %v", c2.Macros[0].M)
	}
	ReleaseContext(c2)
}
