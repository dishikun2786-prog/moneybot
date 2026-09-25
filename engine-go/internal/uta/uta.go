package uta

import "math"

// Account 是 Bybit UTA 统一交易账户快照。
type Account struct {
	TotalEquity       float64 // 总权益 USDT
	MaintenanceMargin float64 // 维持保证金 USDT
}

// Asset 是单资产资本调度输入。
type Asset struct {
	Symbol string  // 标的
	RScore float64 // 宏微观共振分（来自 resonance）
	VaR    float64 // 波动率风险（名义的近似 VaR）
}

// Scheduler 是 UTA 组合保证金动态资本调度器（模块六）。
type Scheduler struct {
	MRRThreshold float64 // MRR 风控线，默认 0.85
	MinWeight    float64 // 单资产最小权重下限
}

// NewScheduler 构造默认调度器。
func NewScheduler() *Scheduler {
	return &Scheduler{MRRThreshold: 0.85, MinWeight: 0.02}
}

// MRR 计算组合保证金风险率 = 维持保证金 / 总权益。
func (s *Scheduler) MRR(a Account) float64 {
	if a.TotalEquity <= 0 {
		return 1 // 无权益视作满风险
	}
	return a.MaintenanceMargin / a.TotalEquity
}

// Allocate 按 R_score / VaR 动态分配多资产名义权重 W_i。
// 返回 weights（归一化，和=1）与杠杆因子 scale（MRR 健康时=1，超线时缩减）。
// totalNotional: 可调度的总名义。
func (s *Scheduler) Allocate(assets []Asset, totalNotional float64) (weights []float64, scale float64, halt bool) {
	n := len(assets)
	weights = make([]float64, n)
	if n == 0 || totalNotional <= 0 {
		return weights, 0, false
	}
	// 风险调整得分 = RScore / VaR（VaR 大 → 降权重）
	score := make([]float64, n)
	var sum float64
	for i, a := range assets {
		va := math.Max(a.VaR, 1e-9)
		score[i] = math.Max(0, a.RScore) / va
		sum += score[i]
	}
	if sum <= 0 {
		return weights, 1, false
	}
	for i := range weights {
		weights[i] = score[i] / sum
		if weights[i] < s.MinWeight {
			weights[i] = 0 // 低于下限剔除
		}
	}
	// 重新归一化
	sum = 0
	for _, w := range weights {
		sum += w
	}
	if sum <= 0 {
		return weights, 1, false
	}
	for i := range weights {
		weights[i] /= sum
	}
	return weights, 1, false
}

// ApplyMRR 结合 MRR 调整杠杆：MRR ≥ 阈值 → 缩减杠杆或熔断。
// 返回调整后的权重（×scale）与是否熔断（halt）。
func (s *Scheduler) ApplyMRR(mrr float64, weights []float64, maxScale float64) ([]float64, float64, bool) {
	n := len(weights)
	out := make([]float64, n)
	if mrr >= 1 {
		return out, 0, true // MRR ≥ 1：接近强平，熔断
	}
	if mrr >= s.MRRThreshold {
		// 超线：按剩余缓冲比例缩减杠杆
		scale := (1 - mrr) / (1 - s.MRRThreshold) // 0~1
		if scale < 0 {
			scale = 0
		}
		for i := range out {
			out[i] = weights[i] * scale
		}
		return out, scale, false
	}
	copy(out, weights)
	return out, maxScale, false
}
