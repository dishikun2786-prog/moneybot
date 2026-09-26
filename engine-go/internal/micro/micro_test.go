package micro

import "testing"

func TestWallEaten(t *testing.T) {
	w := NewWallLifecycle(10)
	// 第一帧：巨量墙 @ 100
	w.OnDepth([]Level{{Price: 100, Size: 50}}, nil)
	// 第二帧：墙被 Taker 吃掉（量降到 5，伴随成交 45）
	events := w.OnDepth([]Level{{Price: 100, Size: 5}}, []TradeHit{{Price: 100, Size: 45, Side: "Sell"}})
	if len(events) == 0 {
		t.Fatal("应产生墙事件")
	}
	if events[0].Kind != "eaten" {
		t.Fatalf("被吃应识别为 eaten: %v", events[0].Kind)
	}
}

func TestWallSpoofed(t *testing.T) {
	w := NewWallLifecycle(10)
	w.OnDepth([]Level{{Price: 100, Size: 50}}, nil)
	// 墙撤单消失（量降到 0，无成交）
	events := w.OnDepth([]Level{}, nil)
	if len(events) == 0 || events[0].Kind != "spoofed" {
		t.Fatalf("撤单应识别为 spoofed: %v", events)
	}
}

func TestScore(t *testing.T) {
	// 真实进攻 2 次 + 虚假 1 次
	events := []WallEvent{
		{Kind: "eaten", PeakSize: 50},
		{Kind: "eaten", PeakSize: 50},
		{Kind: "spoofed", PeakSize: 100},
	}
	trueScore, spoofScore := Score(events)
	if trueScore != 0.5 || spoofScore != 0.5 {
		t.Fatalf("真实/虚假应各 0.5: %.2f %.2f", trueScore, spoofScore)
	}
}

func TestWaveEnergy(t *testing.T) {
	w := NewWaveEnergy(256, 0.4)
	// 初始盘口
	w.OnDepth([]Level{{Price: 100, Size: 10}, {Price: 101, Size: 10}})
	// 量变化（买压爆发）
	en := w.OnDepth([]Level{{Price: 100, Size: 50}, {Price: 101, Size: 10}})
	if en <= 0 {
		t.Fatalf("量变化应产生波能: %v", en)
	}
}
