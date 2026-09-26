package sweep

import (
	"testing"

	"github.com/moneybot/aetherhft/internal/wall"
)

func TestSweepRejection(t *testing.T) {
	s := NewSweepDetector()
	// 支撑墙 @ 100，价格穿透到 98 后收回 101（长下影线）
	k := Kline{Open: 100.5, High: 101.5, Low: 98.0, Close: 101.0}
	anchors := []wall.MacroAnchorZone{{Price: 100, Side: "bid"}}
	if !s.OnKline(k, anchors) {
		t.Fatal("穿透支撑后收回应触发扫盘拒绝")
	}
}

func TestNoSweep(t *testing.T) {
	s := NewSweepDetector()
	// 无穿透
	k := Kline{Open: 101, High: 102, Low: 100.5, Close: 101.5}
	anchors := []wall.MacroAnchorZone{{Price: 100, Side: "bid"}}
	if s.OnKline(k, anchors) {
		t.Fatal("未穿透不应触发")
	}
}
