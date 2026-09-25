package risk

// FeeTier 是费率档（Maker/Taker，比例值如 0.001 = 10bp）。
type FeeTier struct {
	Maker float64
	Taker float64
}

// FeeSchedule 是 VIP 等级 → 费率表（示例值；生产按 Bybit 账号实时等级动态读）。
type FeeSchedule struct {
	tiers map[int]FeeTier
}

// DefaultFeeSchedule 返回内置示例费率表。
func DefaultFeeSchedule() *FeeSchedule {
	return &FeeSchedule{tiers: map[int]FeeTier{
		0: {Maker: 0.0010, Taker: 0.0010},   // 普通：10bp/10bp
		1: {Maker: 0.0002, Taker: 0.00055},  // VIP1
		2: {Maker: 0.0001, Taker: 0.0005},   // VIP2（示例）
	}}
}

// Tier 返回指定 VIP 等级的费率；未知等级回退普通档。
func (s *FeeSchedule) Tier(vip int) FeeTier {
	if t, ok := s.tiers[vip]; ok {
		return t
	}
	return s.tiers[0]
}

// RoundTripFee 返回往返手续费 = 名义 × (开仓 Taker + 平仓 Taker)。
// 市价突破单两腿均为 taker；若平仓可 maker 则更省（按 taker 保守计）。
func (s *FeeSchedule) RoundTripFee(vip int, notional float64) float64 {
	t := s.Tier(vip)
	return notional * (t.Taker + t.Taker)
}

// Signal 是待校验的开仓信号（来自 FDTD 激波 / Kakeya / TDA 仲裁）。
type Signal struct {
	PSuccess     float64 // 模型输出成功概率 P(success)
	TargetProfit float64 // 目标盈利（金额）
	StopLoss     float64 // 止损亏损（金额，正数）
	SlippageEst  float64 // 滑点预估（金额）
	Notional     float64 // 名义金额
}

// NetEV 计算净期望收益：
//   NetEV = P·Target − (1−P)·Stop − Fee − Slippage
func NetEV(p, target, stop, fee, slippage float64) float64 {
	return p*target - (1-p)*stop - fee - slippage
}

// ExpectancyFilter 是手续费净期望收益过滤器。
// 只有 NetEV > threshold 且 NetEV > feeMargin × 往返手续费才放行，
// 彻底杜绝「赚了点数、亏了手续费」的伪高频交易。
type ExpectancyFilter struct {
	sched     *FeeSchedule
	threshold float64 // NetEV 最低阈值（金额）
	feeMargin float64 // 要求 NetEV > feeMargin × 往返手续费
}

// NewExpectancyFilter 构造过滤器。
func NewExpectancyFilter(sched *FeeSchedule, threshold, feeMargin float64) *ExpectancyFilter {
	return &ExpectancyFilter{sched: sched, threshold: threshold, feeMargin: feeMargin}
}

// Allow 判断信号是否值得下单。返回 (放行, NetEV)。
func (f *ExpectancyFilter) Allow(sig Signal, vip int) (bool, float64) {
	fee := f.sched.RoundTripFee(vip, sig.Notional)
	ev := NetEV(sig.PSuccess, sig.TargetProfit, sig.StopLoss, fee, sig.SlippageEst)
	if ev > f.threshold && ev > f.feeMargin*fee {
		return true, ev
	}
	return false, ev
}