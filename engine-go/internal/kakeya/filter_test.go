package kakeya

import (
	"math"
	"testing"
	"time"
)

func TestSpoofedWallsGiveLowKOm(t *testing.T) {
	f := NewFilter(time.Minute)
	// 3 堵墙出现即撤（无成交）→ 全假挂单
	for i := 0; i < 3; i++ {
		f.OnWall(WallEvent{TsNs: 1, Price: 50000 + float64(i), Side: SideBid, Size: 100, Appear: true})
		f.OnWall(WallEvent{TsNs: 2, Price: 50000 + float64(i), Side: SideBid, Size: 100, Appear: false})
	}
	if math.Abs(f.KOm()) > 1e-9 {
		t.Fatalf("全假挂单 K_om 应=0, got %v", f.KOm())
	}
}

func TestGenuineWallsGiveHighKOm(t *testing.T) {
	f := NewFilter(time.Minute)
	// 墙出现后被成交吃掉 → 全真实
	f.OnWall(WallEvent{TsNs: 1, Price: 50000, Side: SideBid, Size: 100, Appear: true})
	f.OnTrade(TradeHit{TsNs: 2, Price: 50000, Side: SideAsk, Size: 100}) // 卖 taker 吃买墙
	f.OnWall(WallEvent{TsNs: 3, Price: 50000, Side: SideBid, Size: 100, Appear: false})
	if math.Abs(f.KOm()-1.0) > 1e-9 {
		t.Fatalf("全真实 K_om 应=1, got %v", f.KOm())
	}
}

func TestMixedKOm(t *testing.T) {
	f := NewFilter(time.Minute)
	// 一半被吃、一半即撤
	f.OnWall(WallEvent{TsNs: 1, Price: 50000, Side: SideBid, Size: 100, Appear: true})
	f.OnTrade(TradeHit{TsNs: 2, Price: 50000, Side: SideAsk, Size: 100})
	f.OnWall(WallEvent{TsNs: 3, Price: 50000, Side: SideBid, Size: 100, Appear: false})

	f.OnWall(WallEvent{TsNs: 4, Price: 50100, Side: SideBid, Size: 100, Appear: true})
	f.OnWall(WallEvent{TsNs: 5, Price: 50100, Side: SideBid, Size: 100, Appear: false})
	if math.Abs(f.KOm()-0.5) > 1e-9 {
		t.Fatalf("混合 K_om 应=0.5, got %v", f.KOm())
	}
}

func TestVeto(t *testing.T) {
	f := NewFilter(time.Minute)
	// 全假挂单 → K_om=0 < 0.6 → veto
	f.OnWall(WallEvent{TsNs: 1, Price: 50000, Side: SideBid, Size: 100, Appear: true})
	f.OnWall(WallEvent{TsNs: 2, Price: 50000, Side: SideBid, Size: 100, Appear: false})
	if !f.Veto(0.6) {
		t.Fatal("假挂单应触发否决")
	}
	// 全真实 → K_om=1 >= 0.6 → 不否决
	f2 := NewFilter(time.Minute)
	f2.OnWall(WallEvent{TsNs: 1, Price: 50000, Side: SideBid, Size: 100, Appear: true})
	f2.OnTrade(TradeHit{TsNs: 2, Price: 50000, Side: SideAsk, Size: 100})
	f2.OnWall(WallEvent{TsNs: 3, Price: 50000, Side: SideBid, Size: 100, Appear: false})
	if f2.Veto(0.6) {
		t.Fatal("真实墙不应被否决")
	}
}