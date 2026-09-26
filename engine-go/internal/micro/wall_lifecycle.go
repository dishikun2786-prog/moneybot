package micro

// Level 是一档（价格+量）。
type Level struct {
	Price float64
	Size  float64
}

// TradeHit 是一笔成交（用于区分「墙被吃」vs「墙撤单」）。
type TradeHit struct {
	Price float64
	Size  float64
	Side  string // Taker 方向 Buy/Sell
}

// WallEvent 是墙生命周期事件（时序微观信号的核心输出）。
type WallEvent struct {
	Price    float64
	Side     string  // bid / ask
	Kind     string  // eaten(被吃=真实进攻) / spoofed(撤单=虚假挂单)
	PeakSize float64 // 峰值量
}

// WallLifecycle 跟踪墙的「出现 → 被吃(真实) / 撤单(虚假)」生命周期。
// 关键洞察：静态厚墙无 alpha，只有「被 Taker 吃掉」的墙才是真实进攻，
// 「撤单消失」的墙是主力诱多/诱空（Spoofing）。
type WallLifecycle struct {
	prev      map[float64]float64 // 上一帧档位量
	threshold float64             // 墙判定阈值（绝对量）
	mult      float64             // 阈值倍数（相对均值）
}

// NewWallLifecycle 构造墙生命周期跟踪器。
func NewWallLifecycle(threshold float64) *WallLifecycle {
	if threshold <= 0 {
		threshold = 10
	}
	return &WallLifecycle{prev: map[float64]float64{}, threshold: threshold, mult: 3}
}

// OnDepth 输入一帧盘口 + 本帧成交，返回墙生命周期事件（被吃/撤单）。
// 时间复杂度 O(N)，N=档位数。
func (w *WallLifecycle) OnDepth(levels []Level, trades []TradeHit) []WallEvent {
	cur := map[float64]float64{}
	for _, l := range levels {
		if l.Size > 0 {
			cur[l.Price] = l.Size
		}
	}
	// 成交映射：价格 -> 成交量
	traded := map[float64]float64{}
	for _, t := range trades {
		traded[t.Price] += t.Size
	}

	var events []WallEvent
	// 1. 检测消失的墙（上一帧有，当前帧消失或大幅减少）
	for price, oldSize := range w.prev {
		newSize, exists := cur[price]
		wasWall := oldSize >= w.threshold
		if !wasWall {
			continue
		}
		gone := !exists || newSize < oldSize*0.5
		if !gone {
			continue
		}
		consumed := oldSize - newSize
		side := "bid"
		// 被吃 vs 撤单：被吃量 ≈ 成交量的墙 = 真实进攻
		tv := traded[price]
		if tv > 0 && consumed <= tv*1.2 {
			events = append(events, WallEvent{Price: price, Side: side, Kind: "eaten", PeakSize: oldSize})
		} else {
			events = append(events, WallEvent{Price: price, Side: side, Kind: "spoofed", PeakSize: oldSize})
		}
	}
	// 2. 检测新出现的墙（当前帧出现）
	for price, size := range cur {
		oldSize := w.prev[price]
		if size >= w.threshold && oldSize < w.threshold {
			// 新墙出现（这里只记录，不产事件）
		}
	}
	// 更新 prev
	w.prev = cur
	return events
}

// Score 汇总时序微观信号：真实进攻分 - 虚假挂单分，归一化 0~1。
func Score(events []WallEvent) (trueScore, spoofScore float64) {
	for _, e := range events {
		if e.Kind == "eaten" {
			trueScore += e.PeakSize
		} else {
			spoofScore += e.PeakSize
		}
	}
	total := trueScore + spoofScore
	if total <= 0 {
		return 0, 0
	}
	return trueScore / total, spoofScore / total
}
