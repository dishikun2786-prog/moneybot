package trend

import "math"

// ATR 是平均真实波幅（Wilder 平滑），用于自适应止损/风险调整。
type ATR struct {
	period      int
	prevClose   float64
	value       float64
	initialized bool
}

// NewATR 构造 ATR，period 默认 14。
func NewATR(period int) *ATR {
	if period < 1 {
		period = 14
	}
	return &ATR{period: period}
}

// Update 输入一根 K 线（high/low/close），返回当前 ATR。
func (a *ATR) Update(high, low, close float64) float64 {
	if !a.initialized {
		a.prevClose = close
		a.initialized = true
		return 0
	}
	tr := math.Max(high-low, math.Max(math.Abs(high-a.prevClose), math.Abs(low-a.prevClose)))
	if a.value == 0 {
		a.value = tr
	} else {
		a.value = (a.value*float64(a.period-1) + tr) / float64(a.period)
	}
	a.prevClose = close
	return a.value
}

// Value 返回当前 ATR。
func (a *ATR) Value() float64 { return a.value }
