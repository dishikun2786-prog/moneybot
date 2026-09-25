package feed

import (
	"strconv"
	"testing"
)

// sym 把 string 转 [16]byte（测试用）。
func sym(s string) [16]byte {
	var o [16]byte
	copy(o[:], s)
	return o
}

func TestBookSnapshotAndDelta(t *testing.T) {
	b := NewBook(sym("BTCUSDT"))
	b.Snapshot([][]string{{"100", "5"}, {"99", "10"}}, [][]string{{"101", "6"}, {"102", "12"}}, 1)
	if !b.Ready() || b.bidN != 2 || b.askN != 2 {
		t.Fatalf("快照重建失败: bidN=%d askN=%d", b.bidN, b.askN)
	}
	if b.bids[0].Price != 100 || b.bids[1].Price != 99 {
		t.Fatalf("bids 未降序: [0]=%v [1]=%v", b.bids[0].Price, b.bids[1].Price)
	}
	if b.asks[0].Price != 101 || b.asks[1].Price != 102 {
		t.Fatalf("asks 未升序")
	}
	b.Delta(2)
	b.ApplyBid(100, 7)
	if b.bids[0].Size != 7 {
		t.Fatalf("更新失败")
	}
	b.ApplyBid(99, 0)
	if b.bidN != 1 {
		t.Fatalf("删除失败: bidN=%d", b.bidN)
	}
	b.ApplyBid(98, 3)
	if b.bidN != 2 || b.bids[1].Price != 98 {
		t.Fatalf("插入失败: bidN=%d [1]=%v", b.bidN, b.bids[1].Price)
	}
	b.ApplyAsk(101, 0)
	if b.askN != 1 {
		t.Fatalf("ask删除失败")
	}
	b.ApplyAsk(103, 9)
	if b.askN != 2 || b.asks[1].Price != 103 {
		t.Fatalf("ask插入失败")
	}
}

func BenchmarkBookDelta(b *testing.B) {
	bk := NewBook(sym("BTCUSDT"))
	bids := make([][]string, MaxBookLevels)
	asks := make([][]string, MaxBookLevels)
	for i := 0; i < MaxBookLevels; i++ {
		bids[i] = []string{strconv.Itoa(60000 - i), "1"}
		asks[i] = []string{strconv.Itoa(60001 + i), "1"}
	}
	bk.Snapshot(bids, asks, 1)
	b.ReportAllocs()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		p := 60000 - float64(i%MaxBookLevels)
		if i%3 == 0 {
			bk.ApplyBid(p, 0)
			bk.ApplyBid(p, 2)
		} else {
			bk.ApplyBid(p, float64(i%10+1))
		}
	}
}