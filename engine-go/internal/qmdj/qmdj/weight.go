package qmdj

// WeightManager 是 QMDJ 权重融合开关（验证通过后从 0 平滑过渡到目标权重）。
// 验证期 weight=0（纯影子），验证通过后 Advance() 逐步增加，最终到 target。
type WeightManager struct {
	target  float64
	current float64
	step    float64
	enabled bool
}

// NewWeightManager 构造权重管理器。target=目标权重(如 0.2)，step=每步增量(如 0.02)。
func NewWeightManager(target, step float64) *WeightManager {
	if target < 0 {
		target = 0
	}
	if target > 1 {
		target = 1
	}
	if step <= 0 {
		step = 0.02
	}
	return &WeightManager{target: target, step: step}
}

// Enable 开启权重融合（验证通过后调用）。
func (w *WeightManager) Enable() { w.enabled = true }

// Disable 关闭权重融合（回滚到纯影子）。
func (w *WeightManager) Disable() { w.enabled = false }

// Enabled 返回是否开启融合。
func (w *WeightManager) Enabled() bool { return w.enabled }

// Advance 平滑增加权重一步，返回当前权重。
func (w *WeightManager) Advance() float64 {
	if !w.enabled {
		return 0
	}
	if w.current < w.target {
		w.current += w.step
		if w.current > w.target {
			w.current = w.target
		}
	}
	return w.current
}

// Weight 返回当前权重。
func (w *WeightManager) Weight() float64 {
	if !w.enabled {
		return 0
	}
	return w.current
}

// Blended 融合 QMDJ score 与基础信号：blended = base + weight × (qmdj - 0.5)。
// 返回融合后的信号分（用于最终决策权重）。
func (w *WeightManager) Blended(baseScore, qmdjScore float64) float64 {
	return baseScore + w.Weight()*(qmdjScore-0.5)
}
