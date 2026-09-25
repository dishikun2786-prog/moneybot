package feed

import (
	"testing"
)

// 构造一条 snapshot 消息（简化）
func snapMsg(symName string, levels int) []byte {
	b := []byte(`{"topic":"orderbook.200.` + symName + `","type":"snapshot","ts":1700000000000,"data":{"s":"` + symName + `","b":[`)
	for i := 0; i < levels; i++ {
		if i > 0 {
			b = append(b, ',')
		}
		b = append(b, []byte(`["`)...)
		b = append(b, []byte(intToStr(60000-i))...)
		b = append(b, []byte(`","1.5"]`)...)
	}
	b = append(b, []byte(`],"a":[["60100","2.5"]],"u":1}}`)...)
	return b
}

func intToStr(n int) string {
	if n == 0 {
		return "0"
	}
	var buf [20]byte
	i := len(buf)
	for n > 0 {
		i--
		buf[i] = byte('0' + n%10)
		n /= 10
	}
	return string(buf[i:])
}

func TestParseBookSnapshot(t *testing.T) {
	msg := snapMsg("BTCUSDT", 200)
	b := NewBook(sym("BTCUSDT"))
	isSnap, ok := parseBook(msg, b)
	if !ok || !isSnap {
		t.Fatalf("parseBook 失败: isSnap=%v ok=%v", isSnap, ok)
	}
	if !b.Ready() || b.bidN != 200 || b.askN != 1 {
		t.Fatalf("快照重建异常: bidN=%d askN=%d ready=%v", b.bidN, b.askN, b.Ready())
	}
	// 校验降序
	if b.bids[0].Price != 60000 || b.bids[199].Price != float64(60000-199) {
		t.Fatalf("bids 未降序")
	}
}

func TestParseBookDelta(t *testing.T) {
	// 先快照
	b := NewBook(sym("BTCUSDT"))
	parseBook(snapMsg("BTCUSDT", 3), b) // bids: 60000,59999,59998
	// delta: u=2, 更新 60000->8, 删除 59999, 插入 59997
	delta := []byte(`{"topic":"orderbook.200.BTCUSDT","type":"delta","ts":1,"data":{"s":"BTCUSDT","b":[["60000","8"],["59999","0"],["59997","3"]],"a":[],"u":2}}`)
	isSnap, ok := parseBook(delta, b)
	if !ok || isSnap {
		t.Fatalf("delta 解析失败: ok=%v isSnap=%v", ok, isSnap)
	}
	if b.bidN != 3 {
		t.Fatalf("delta 后 bidN=%d, 期望3", b.bidN)
	}
	if b.bids[0].Price != 60000 || b.bids[0].Size != 8 {
		t.Fatalf("更新失败: %+v", b.bids[0])
	}
	if b.bids[2].Price != 59997 {
		t.Fatalf("插入失败")
	}
	// 序列缺口：u=4（跳过3）应失败
	bad := []byte(`{"topic":"orderbook.200.BTCUSDT","type":"delta","ts":1,"data":{"s":"BTCUSDT","b":[],"a":[],"u":4}}`)
	if _, ok := parseBook(bad, b); ok {
		t.Fatal("序列缺口应返回 false")
	}
}

func BenchmarkParseBookSnapshot(b *testing.B) {
	msg := snapMsg("BTCUSDT", 200)
	bk := NewBook(sym("BTCUSDT"))
	b.ReportAllocs()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		if _, ok := parseBook(msg, bk); !ok {
			b.Fatal("parse failed")
		}
	}
}

func BenchmarkParseBookDelta(b *testing.B) {
	bk := NewBook(sym("BTCUSDT"))
	parseBook(snapMsg("BTCUSDT", 200), bk) // bk.u = 1
	delta := []byte(`{"topic":"orderbook.200.BTCUSDT","type":"delta","ts":1,"data":{"s":"BTCUSDT","b":[["59999","8"],["59998","0"],["59997","3"]],"a":[["60100","4"]],"u":2}}`)
	b.ReportAllocs()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		bk.u = 1 // 每帧把序列拨回，使 delta(u=2) 恒合法（模拟独立帧）
		if _, ok := parseBook(delta, bk); !ok {
			b.Fatal("parse failed")
		}
	}
}