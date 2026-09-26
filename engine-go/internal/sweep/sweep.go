package sweep

import (
	"math"

	"github.com/moneybot/aetherhft/internal/wall"
)

// Kline 是一根 5m/15m K线。
type Kline struct {
	Open, High, Low, Close float64
	Ts                     int64
}

// SweepDetector 是 5m-15m 扫盘拒绝形态识别器。
// 识别：价格穿透 1H 锚定墙（假突破扫盘）→ 1-2 根 K线内被大单收回 → 长影线/吞没。
type SweepDetector struct {
	recent []Kline // 最近 3 根 K线
}

// NewSweepDetector 构造识别器。
func NewSweepDetector() *SweepDetector { return &SweepDetector{} }

// OnKline 输入一根新 K线 + 当前锚定区，返回是否触发扫盘拒绝。
func (s *SweepDetector) OnKline(k Kline, anchors []wall.MacroAnchorZone) bool {
	s.recent = append(s.recent, k)
	if len(s.recent) > 3 {
		s.recent = s.recent[1:]
	}
	if len(s.recent) < 1 || len(anchors) == 0 {
		return false
	}
	// 对每个锚定墙检查是否被扫盘
	for _, a := range anchors {
		if s.sweptAt(k, a) {
			return true
		}
	}
	return false
}

// sweptAt 判断当前 K线是否对某个锚定墙发生「扫盘后收回」。
func (s *SweepDetector) sweptAt(k Kline, a wall.MacroAnchorZone) bool {
	if a.Side == "bid" {
		// 支撑墙：价格穿透下方（扫多单止损）后收回上方
		if k.Low < a.Price && k.Close > a.Price {
			return isRejection(k, true)
		}
	} else {
		// 阻力墙：价格穿透上方（扫空单止损）后收回下方
		if k.High > a.Price && k.Close < a.Price {
			return isRejection(k, false)
		}
	}
	return false
}

// isRejection 判断长影线/吞没（拒绝形态）。
func isRejection(k Kline, bullish bool) bool {
	body := math.Abs(k.Close - k.Open)
	if bullish {
		// 下影线 > 实体×2
		lowerWick := math.Min(k.Open, k.Close) - k.Low
		return lowerWick > body*2
	}
	// 上影线 > 实体×2
	upperWick := k.High - math.Max(k.Open, k.Close)
	return upperWick > body*2
}
