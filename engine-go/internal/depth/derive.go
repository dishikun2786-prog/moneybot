package depth

import "github.com/moneybot/aetherhft/internal/feed"

// TradePoint 是一笔归一化成交（用于 CVD/大单聚合）。
type TradePoint struct {
	Ts   int64   // 毫秒
	Side string  // Buy / Sell
	V    float64 // 成交量
	P    float64 // 成交价
}

// DetectWalls 挂单墙检测：单档 size ≥ max(前20档均值×8, 该侧总量×0.25)。
// 返回价格与 size 对（与 Python detect_walls 对齐）。
func DetectWalls(levels []feed.Level) [][2]float64 {
	if len(levels) < 5 {
		return nil
	}
	// 复制并按 size 降序取 top20
	sorted := make([]feed.Level, len(levels))
	copy(sorted, levels)
	for i := 0; i < len(sorted); i++ {
		for j := i + 1; j < len(sorted); j++ {
			if sorted[j].Size > sorted[i].Size {
				sorted[i], sorted[j] = sorted[j], sorted[i]
			}
		}
	}
	topN := sorted
	if len(topN) > 20 {
		topN = topN[:20]
	}
	var avg, total float64
	for _, l := range topN {
		avg += l.Size
	}
	for _, l := range levels {
		total += l.Size
	}
	avg /= float64(len(topN))
	thr := avg * 8.0
	if t2 := total * 0.25; t2 > thr {
		thr = t2
	}
	var out [][2]float64
	for _, l := range levels {
		if l.Size >= thr {
			out = append(out, [2]float64{l.Price, l.Size})
		}
	}
	return out
}

// AggWindow 主动量聚合：按价格桶聚合 15/60/300s 买卖量。
// 返回 map[bucketIdx][买15,卖15,买60,卖60,买300,卖300]。
func AggWindow(trades []TradePoint, step float64, now int64) map[int64][6]float64 {
	grid := map[int64][6]float64{}
	for _, t := range trades {
		if now-t.Ts > 300000 {
			continue
		}
		k := int64(t.P / step)
		g := grid[k]
		delta := t.V
		base := 0
		if t.Side == "Sell" {
			base = 1
		}
		if now-t.Ts <= 15000 {
			g[base] += delta
		}
		if now-t.Ts <= 60000 {
			g[base+2] += delta
		}
		if now-t.Ts <= 300000 {
			g[base+4] += delta
		}
		grid[k] = g
	}
	return grid
}

// Micro 是微观结构特征（与 Python microstructure.features 对齐）。
type Micro struct {
	SpreadBP float64 `json:"spread_bp"`
	Imb      float64 `json:"imb"`
	DepthTop float64 `json:"depth_top"`
	WallBid  float64 `json:"wall_bid"`
	WallAsk  float64 `json:"wall_ask"`
	WallImb  float64 `json:"wall_imb"`
	Cvd60    float64 `json:"cvd60"`
	FlowImb  float64 `json:"flow_imb"`
	BasisBP  float64 `json:"basis_bp"`
	Risk     float64 `json:"risk"`
}

// ComputeMicro 从盘口/墙/成交/基差计算微观特征与风险分（权重与 Python 一致）。
func ComputeMicro(bids, asks []feed.Level, wallBid, wallAsk [][2]float64,
	cvd60, flowImb float64, basisBP float64) Micro {
	m := Micro{BasisBP: basisBP, Cvd60: cvd60, FlowImb: flowImb}
	if len(bids) == 0 || len(asks) == 0 {
		return m
	}
	bestBid, bestAsk := bids[0].Price, asks[0].Price
	mid := (bestBid + bestAsk) / 2
	if mid > 0 {
		m.SpreadBP = (bestAsk - bestBid) / mid * 10000
	}
	var b5, a5 float64
	for i := 0; i < 5 && i < len(bids); i++ {
		b5 += bids[i].Size
	}
	for i := 0; i < 5 && i < len(asks); i++ {
		a5 += asks[i].Size
	}
	tot := b5 + a5
	if tot > 0 {
		m.Imb = (b5 - a5) / tot
	}
	if b5 < a5 {
		m.DepthTop = b5
	} else {
		m.DepthTop = a5
	}
	var wb, wa float64
	for _, w := range wallBid {
		if w[1] > wb {
			wb = w[1]
		}
	}
	for _, w := range wallAsk {
		if w[1] > wa {
			wa = w[1]
		}
	}
	m.WallBid, m.WallAsk = wb, wa
	if wb+wa > 0 {
		m.WallImb = (wb - wa) / (wb + wa)
	}
	// 风险分 0~1
	s := 0.0
	if m.SpreadBP > 8.0 {
		s += 0.25
	} else if m.SpreadBP > 4.0 {
		s += 0.125
	}
	if m.DepthTop < 10.0 {
		s += 0.20
	}
	if m.Imb > 0.4 || m.Imb < -0.4 {
		s += 0.15
	}
	if (m.WallImb > 0.5 || m.WallImb < -0.5) && (len(wallBid)+len(wallAsk) > 0) {
		s += 0.20
	}
	if m.FlowImb > 0.5 || m.FlowImb < -0.5 {
		s += 0.20
	}
	if s > 1.0 {
		s = 1.0
	}
	m.Risk = s
	return m
}
