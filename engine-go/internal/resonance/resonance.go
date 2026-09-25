package resonance

import "math"

// Hist 是固定窗口历史（零分配环形缓冲），用于 OI/资金费率历史分位数。
type Hist struct {
	buf []float64
	n   int
	idx int
	sum float64
}

// NewHist 创建容量为 cap 的历史窗口。
func NewHist(cap int) *Hist {
	if cap < 2 {
		cap = 2
	}
	return &Hist{buf: make([]float64, cap)}
}

// Add 追加样本（满后环形覆盖，零分配）。
func (h *Hist) Add(v float64) {
	if h.n < len(h.buf) {
		h.buf[h.n] = v
		h.n++
		h.sum += v
		return
	}
	old := h.buf[h.idx]
	h.buf[h.idx] = v
	h.idx = (h.idx + 1) % len(h.buf)
	h.sum += v - old
}

// Mean 均值。
func (h *Hist) Mean() float64 {
	if h.n == 0 {
		return 0
	}
	return h.sum / float64(h.n)
}

// Std 标准差（总体）。
func (h *Hist) Std() float64 {
	if h.n < 2 {
		return 0
	}
	m := h.Mean()
	var ss float64
	for i := 0; i < h.n; i++ {
		d := h.buf[i] - m
		ss += d * d
	}
	return math.Sqrt(ss / float64(h.n))
}

// Percentile 返回样本在历史窗口中的分位（0~1）：小于等于 v 的比例。
func (h *Hist) Percentile(v float64) float64 {
	if h.n == 0 {
		return 0.5
	}
	le := 0
	for i := 0; i < h.n; i++ {
		if h.buf[i] <= v {
			le++
		}
	}
	return float64(le) / float64(h.n)
}

// Macro 是宏观杠杆失衡因子（模块二 M_score）。
type Macro struct {
	FundingZ  float64 // 资金费率偏离度（z-score 经 sigmoid 归一化 0~1）
	OIPercent float64 // OI 历史分位 0~1
	M         float64 // 宏观杠杆失衡分 0~1
}

// Micro 是微观共振分（模块二 m_score）。
type Micro struct {
	En      float64 // 波能通量归一化 0~1
	Kom     float64 // 挂谷交织测度 0~1
	RiskTDA float64 // 拓扑风险 0~1
	M       float64 // 微观共振分 0~1
}

// Decision 是共振乘积判决结果。
type Decision struct {
	M    float64 // 宏观
	Msc  float64 // 微观
	R    float64 // 共振乘积 R = M × m
	Fire bool   // 是否进入下单候选池
}

func sigmoid(x float64) float64 { return 1 / (1 + math.Exp(-x)) }

// clamp01 裁剪到 [0,1]。
func clamp01(x float64) float64 {
	if x < 0 {
		return 0
	}
	if x > 1 {
		return 1
	}
	return x
}

// ComputeMacro 计算宏观杠杆失衡分。
// funding: 当前资金费率(原始值), oi: 当前持仓量。
// fh/oh: 资金费率/OI 的历史窗口。
func ComputeMacro(funding, oi float64, fh, oh *Hist) Macro {
	m := Macro{}
	if fh != nil && fh.n > 0 && fh.Std() > 0 {
		z := (funding - fh.Mean()) / fh.Std()
		m.FundingZ = clamp01(sigmoid(z / 2)) // 收窄到 0~1
	} else {
		m.FundingZ = 0.5
	}
	if oh != nil {
		m.OIPercent = oh.Percentile(oi)
	} else {
		m.OIPercent = 0.5
	}
	// 宏观引信：资金费率极端偏离 + OI 拥挤
	m.M = clamp01(m.FundingZ * m.OIPercent)
	return m
}

// ComputeMicro 计算微观共振分。
// en: FDTD 波能通量, kom: 挂谷测度 0~1, riskTDA: 拓扑风险 0~1。
// enScale: 波能通量归一化尺度（en/enScale 过 sigmoid）。
func ComputeMicro(en, kom, riskTDA, enScale float64) Micro {
	m := Micro{En: clamp01(sigmoid(en / enScale)), Kom: clamp01(kom), RiskTDA: clamp01(riskTDA)}
	// 微观动能爆发 = 波能 × 真实挂谷 × (1 - 拓扑风险)
	m.M = clamp01(m.En * m.Kom * (1 - m.RiskTDA))
	return m
}

// Decide 计算共振乘积判决。threshold 为下单候选池门槛（默认 0.5）。
func Decide(ma Macro, mi Micro, threshold float64) Decision {
	d := Decision{M: ma.M, Msc: mi.M, R: ma.M * mi.M}
	d.Fire = d.R >= threshold
	return d
}
