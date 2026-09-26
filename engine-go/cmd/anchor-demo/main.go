// 宏观锚定(1H VPVR + 厚墙) + 战术入场(扫盘拒绝 + 多维共振仲裁) demo。
package main

import (
	"fmt"

	"github.com/moneybot/aetherhft/internal/gate"
	"github.com/moneybot/aetherhft/internal/sweep"
	"github.com/moneybot/aetherhft/internal/vpvr"
	"github.com/moneybot/aetherhft/internal/wall"
)

func main() {
	fmt.Println("=== 宏观锚定 + 战术入场共振 demo ===\n")

	// [1] 1H VPVR
	fmt.Println("[1] 1H Volume Profile (VPVR)")
	v := vpvr.NewVolumeProfile(72, 1.0)
	for i := 0; i < 200; i++ {
		v.AddTrade(100, 1.0) // POC @ 100
	}
	for i := 0; i < 80; i++ {
		v.AddTrade(102, 1.0)
	}
	for i := 0; i < 50; i++ {
		v.AddTrade(98, 1.0)
	}
	poc, _ := v.POC()
	vah, val, _ := v.ValueArea()
	fmt.Printf("  POC=%.1f  VAH=%.1f  VAL=%.1f (70%% 价值区)\n", poc, vah, val)

	// [2] 厚墙识别
	fmt.Println("[2] L2 厚墙识别 (MacroAnchorZone)")
	var bids, asks []wall.Level
	for i := 0; i < 50; i++ {
		bids = append(bids, wall.Level{Price: 100 - float64(i)*0.1, Size: 1})
		asks = append(asks, wall.Level{Price: 100 + float64(i)*0.1, Size: 1})
	}
	bids = append(bids, wall.Level{Price: 99.0, Size: 500}) // 巨量买单墙
	zones := wall.DetectWalls(bids, asks, 100, 3)
	for _, z := range zones {
		fmt.Printf("  %s墙 @ %.2f  厚度%.1fx  距离%+.1f%%\n", z.Side, z.Price, z.Thickness, z.DistancePct)
	}

	// [3] 扫盘拒绝识别
	fmt.Println("[3] 5m-15m 扫盘拒绝识别")
	sd := sweep.NewSweepDetector()
	k := sweep.Kline{Open: 100.5, High: 101.5, Low: 98.0, Close: 101.0} // 穿透99墙后收回
	triggered := sd.OnKline(k, zones)
	fmt.Printf("  IsSweepTriggered=%v (价格扫穿99墙后收回, 长下影线)\n", triggered)

	// [4] 多维共振仲裁
	fmt.Println("[4] FDTD×Kakeya 共振仲裁 (R_score)")
	macroDist := gate.MacroDistanceWeight(1.0) // 距离墙 1%
	en := 0.85                                 // FDTD 波能爆发
	kom := 0.9                                 // Kakeya 挂谷真实进攻
	score, fire := gate.Resonate(macroDist, en, kom, 0.5)
	fmt.Printf("  R_score = %.3f (%.3f×%.2f×%.2f) → %v\n", score, macroDist, en, kom, map[bool]string{true: "✅ 放行", false: "❌ 拦截"}[fire])

	// [5] 完整闸 + NetEV
	fmt.Println("[5] NetEV 过滤")
	ev := 40.0 // 期望收益
	_, fire2 := gate.FullGate(macroDist, en, kom, 0.5, ev, 20)
	fmt.Printf("  NetEV=%.0f vs 最低20 → %v\n", ev, map[bool]string{true: "✅ 放行", false: "❌ 拦截"}[fire2])
}

func _() {}
