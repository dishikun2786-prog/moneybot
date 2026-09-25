package feed

import (
	"sync"
	"testing"
)

func TestRingSPSC(t *testing.T) {
	r := NewRing(4)
	if r.Cap() != 4 {
		t.Fatalf("容量应向上取2的幂: got %d", r.Cap())
	}
	for i := 0; i < 4; i++ {
		if !r.TryPush(BookUpdate{Seq: uint64(i)}) {
			t.Fatalf("第 %d 帧应写入成功", i)
		}
	}
	if r.TryPush(BookUpdate{Seq: 99}) {
		t.Fatal("缓冲满时 TryPush 应返回 false")
	}
	for i := 0; i < 4; i++ {
		u, ok := r.TryPop()
		if !ok || u.Seq != uint64(i) {
			t.Fatalf("应读到第 %d 帧: ok=%v seq=%d", i, ok, u.Seq)
		}
	}
	if _, ok := r.TryPop(); ok {
		t.Fatal("缓冲空时 TryPop 应返回 false")
	}
}

func TestRingConcurrent(t *testing.T) {
	const total = 1_000_000
	r := NewRing(1024)
	var wg sync.WaitGroup
	var pushed, popped uint64
	wg.Add(1)
	go func() {
		defer wg.Done()
		for i := 0; i < total; i++ {
			r.Push(BookUpdate{Seq: uint64(i)})
			pushed++
		}
	}()
	wg.Add(1)
	go func() {
		defer wg.Done()
		for popped < total {
			if _, ok := r.TryPop(); ok {
				popped++
			}
		}
	}()
	wg.Wait()
	if pushed != total || popped != total {
		t.Fatalf("计数不守恒: pushed=%d popped=%d", pushed, popped)
	}
}

func BenchmarkRingPushPop(b *testing.B) {
	r := NewRing(1024)
	b.ReportAllocs()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		if !r.TryPush(BookUpdate{Seq: uint64(i)}) {
			b.Fatal("full")
		}
		if _, ok := r.TryPop(); !ok {
			b.Fatal("empty")
		}
	}
}

func BenchmarkRingTransportNoAlloc(b *testing.B) {
	r := NewRing(1024)
	u := BookUpdate{Seq: 1}
	b.ReportAllocs()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		u.Seq = uint64(i)
		if !r.TryPush(u) {
			b.Fatal("full")
		}
		if _, ok := r.TryPop(); !ok {
			b.Fatal("empty")
		}
	}
}