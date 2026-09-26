package gate

import "math"

// MacroDistanceWeight 计算距离宏观锚定区的权重（越近权重越高，0~1）。
// distancePct 是距离锚定价格的百分比（绝对值），5% 外权重归零。
func MacroDistanceWeight(distancePct float64) float64 {
	d := math.Abs(distancePct)
	if d >= 5 {
		return 0
	}
	return 1 - d/5
}

// Resonate 多维动能共振仲裁：R_score = MacroDistanceWeight × En × Kom。
// en: FDTD 波能通量(归一化 0~1)，kom: Kakeya 挂谷测度(0~1)，threshold: 阈值。
// 返回 R_score 与是否放行。
func Resonate(macroDist, en, kom, threshold float64) (score float64, fire bool) {
	score = clamp01(macroDist) * clamp01(en) * clamp01(kom)
	fire = score >= threshold
	return score, fire
}

// FullGate 完整共振仲裁 + NetEV 过滤。
// ev: NetEV 值，evMin: 最低期望收益，返回是否最终放行。
func FullGate(macroDist, en, kom, threshold, ev, evMin float64) (score float64, fire bool) {
	score, fire = Resonate(macroDist, en, kom, threshold)
	if fire && ev < evMin {
		fire = false
	}
	return score, fire
}

func clamp01(x float64) float64 {
	if x < 0 {
		return 0
	}
	if x > 1 {
		return 1
	}
	return x
}
