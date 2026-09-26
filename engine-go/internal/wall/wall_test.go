package wall

import (
	"math"
	"testing"
)

func TestDetectWalls(t *testing.T) {
	// 正常盘口 + 一个巨量墙
	var bids, asks []Level
	for i := 0; i < 50; i++ {
		bids = append(bids, Level{Price: 100 - float64(i)*0.1, Size: 1.0})
		asks = append(asks, Level{Price: 100 + float64(i)*0.1, Size: 1.0})
	}
	// 巨量买单墙 @ 99.0
	bids = append(bids, Level{Price: 99.0, Size: 500.0})
	walls := DetectWalls(bids, asks, 100, 3)
	if len(walls) == 0 {
		t.Fatal("应识别到厚墙")
	}
	found := false
	for _, w := range walls {
		if math.Abs(w.Price-99.0) < 0.01 && w.Side == "bid" {
			found = true
			if w.Thickness < 10 {
				t.Fatalf("厚度评分应高: %v", w.Thickness)
			}
		}
	}
	if !found {
		t.Fatalf("未识别到 99.0 买单墙: %v", walls)
	}
}

func TestNoWall(t *testing.T) {
	var bids, asks []Level
	for i := 0; i < 50; i++ {
		bids = append(bids, Level{Price: 100 - float64(i)*0.1, Size: 1.0})
		asks = append(asks, Level{Price: 100 + float64(i)*0.1, Size: 1.0})
	}
	walls := DetectWalls(bids, asks, 100, 3)
	if len(walls) != 0 {
		t.Fatalf("均匀盘口不应有墙: %v", walls)
	}
}
