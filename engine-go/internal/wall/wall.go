package wall

import (
	"math"
	"sort"
)

// MacroAnchorZone 是宏观锚定区（限价厚墙）。
type MacroAnchorZone struct {
	Price       float64 `json:"price"`
	Side        string  `json:"side"` // bid / ask
	Thickness   float64 `json:"thickness"`    // 厚度评分 = 档位量 / 均值
	DistancePct float64 `json:"distance_pct"` // 距离当前价格百分比（正=上方）
}

// Level 是一档（价格+量）。
type Level struct {
	Price float64
	Size  float64
}

// DetectWalls 在 ±5% 盘口空间内识别限价厚墙。
// nStd=标准差倍数阈值（默认 3），超过 mean+nStd 的档位聚类为墙。
// 时间复杂度 O(N log N)（排序），N=档位数。
func DetectWalls(bids, asks []Level, midPrice float64, nStd float64) []MacroAnchorZone {
	if nStd <= 0 {
		nStd = 3
	}
	lo := midPrice * 0.95
	hi := midPrice * 1.05

	// 过滤 ±5% 空间
	var levels []Level
	for _, l := range bids {
		if l.Price >= lo && l.Price <= hi && l.Size > 0 {
			levels = append(levels, Level{Price: l.Price, Size: l.Size})
		}
	}
	for _, l := range asks {
		if l.Price >= lo && l.Price <= hi && l.Size > 0 {
			levels = append(levels, Level{Price: l.Price, Size: l.Size})
		}
	}
	if len(levels) < 5 {
		return nil
	}

	// 均值 + 标准差
	var mean float64
	for _, l := range levels {
		mean += l.Size
	}
	mean /= float64(len(levels))
	var variance float64
	for _, l := range levels {
		d := l.Size - mean
		variance += d * d
	}
	variance /= float64(len(levels))
	std := math.Sqrt(variance)
	if std < 1e-9 {
		return nil // 完全均匀盘口，无厚墙
	}
	threshold := mean + nStd*std

	// 超过阈值的档位
	sort.Slice(levels, func(i, j int) bool { return levels[i].Price < levels[j].Price })
	var walls []MacroAnchorZone
	var cur []Level
	flush := func() {
		if len(cur) == 0 {
			return
		}
		// 聚类：取量最大的档位为代表
		best := cur[0]
		for _, l := range cur {
			if l.Size > best.Size {
				best = l
			}
		}
		walls = append(walls, MacroAnchorZone{
			Price:       best.Price,
			Side:        sideOf(best.Price, midPrice),
			Thickness:   best.Size / mean,
			DistancePct: (best.Price - midPrice) / midPrice * 100,
		})
		cur = nil
	}
	for _, l := range levels {
		if l.Size > threshold {
			cur = append(cur, l)
		} else {
			flush()
		}
	}
	flush()
	return walls
}

func sideOf(price, mid float64) string {
	if price >= mid {
		return "ask"
	}
	return "bid"
}
