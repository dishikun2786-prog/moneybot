package risk

import "testing"

func TestSignalPoolReuse(t *testing.T) {
	sp := NewSignalPool()
	a := sp.Get()
	a.PSuccess = 0.7
	sp.Put(a)
	b := sp.Get()
	// sync.Pool 不保证一定复用，但同一池内的对象类型正确
	if b == nil {
		t.Fatal("池应返回非 nil")
	}
	sp.Put(b)
}

func BenchmarkSignalPool(b *testing.B) {
	sp := NewSignalPool()
	b.ReportAllocs()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		s := sp.Get()
		s.PSuccess = 0.7
		s.Notional = 1000
		_ = s.PSuccess + s.Notional
		sp.Put(s)
	}
}

func BenchmarkSignalAlloc(b *testing.B) {
	b.ReportAllocs()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		s := &Signal{PSuccess: 0.7, Notional: 1000}
		_ = s.PSuccess + s.Notional
	}
}