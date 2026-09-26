package qmdj

import (
	"encoding/json"
	"math"
	"os"
	"sort"
)

// pair 是 IC 计算的 (score, ret) 数据对。
type pair struct{ score, ret float64 }

// Stats 是 QMDJ 影子验证的统计审计指标。
type Stats struct {
	TotalEvents   int     `json:"total_events"`
	WinRateBase   float64 `json:"win_rate_base"`    // 原始策略胜率
	WinRateQMDJ   float64 `json:"win_rate_qmdj"`    // QMDJ 高分过滤后胜率
	IC            float64 `json:"ic"`               // Pearson IC（QMDJ score vs 前瞻收益）
	RankIC        float64 `json:"rank_ic"`          // Spearman Rank IC
	FalsePositive float64 `json:"false_positive"`   // 强凶格时实际回撤的概率
	StrongBadN    int     `json:"strong_bad_n"`     // 强凶格事件数
	StrongBadDraw int     `json:"strong_bad_draw"`  // 强凶格中实际回撤数
}

// Analyze 从 jsonl 影子审计记录计算统计指标。
// 前瞻收益用 ForwardReturns["1h"] 作为真实收益方向。
func Analyze(path string) (*Stats, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var recs []ShadowAuditRecord
	for _, line := range splitLines(string(data)) {
		if line == "" {
			continue
		}
		var r ShadowAuditRecord
		if json.Unmarshal([]byte(line), &r) == nil {
			recs = append(recs, r)
		}
	}
	if len(recs) == 0 {
		return &Stats{}, nil
	}

	st := &Stats{TotalEvents: len(recs)}
	// 胜率
	baseWin, qmdjWin, qmdjN := 0, 0, 0
	strongBadN, strongBadDraw := 0, 0
	// IC 数据
	var pairs []pair
	for _, r := range recs {
		ret := r.ForwardReturns["1h"]
		if r.RealizedPnL > 0 {
			baseWin++
		}
		if r.QMDJScore >= 0.6 {
			qmdjN++
			if r.RealizedPnL > 0 {
				qmdjWin++
			}
		}
		if r.PatternType == "凶" && r.QMDJScore < 0.3 {
			strongBadN++
			if ret < 0 {
				strongBadDraw++
			}
		}
		pairs = append(pairs, pair{r.QMDJScore, ret})
	}
	st.WinRateBase = float64(baseWin) / float64(len(recs))
	if qmdjN > 0 {
		st.WinRateQMDJ = float64(qmdjWin) / float64(qmdjN)
	}
	if strongBadN > 0 {
		st.FalsePositive = float64(strongBadDraw) / float64(strongBadN)
	}
	st.StrongBadN = strongBadN
	st.StrongBadDraw = strongBadDraw
	st.IC = pearsonIC(pairs)
	st.RankIC = rankIC(pairs)
	return st, nil
}

// Significant 判断 QMDJ 因子是否具统计显著性。
// minEvents=最小事件数(默认 500), minAbsIC=最小绝对 IC(默认 0.05)。
func (s *Stats) Significant(minEvents int, minAbsIC float64) bool {
	if minEvents <= 0 {
		minEvents = 500
	}
	if minAbsIC <= 0 {
		minAbsIC = 0.05
	}
	ic := s.IC
	if ic < 0 {
		ic = -ic
	}
	return s.TotalEvents >= minEvents && ic >= minAbsIC
}

func splitLines(s string) []string {
	var out []string
	start := 0
	for i := 0; i <= len(s); i++ {
		if i == len(s) || s[i] == '\n' {
			out = append(out, s[start:i])
			start = i + 1
		}
	}
	return out
}

// pearsonIC 计算 QMDJ score 与前瞻收益的 Pearson 相关系数。
func pearsonIC(pairs []pair) float64 {
	n := len(pairs)
	if n < 2 {
		return 0
	}
	var sx, sy, sxx, syy, sxy float64
	for _, p := range pairs {
		sx += p.score
		sy += p.ret
		sxx += p.score * p.score
		syy += p.ret * p.ret
		sxy += p.score * p.ret
	}
	mx, my := sx/float64(n), sy/float64(n)
	num := sxy - float64(n)*mx*my
	dx := sxx - float64(n)*mx*mx
	dy := syy - float64(n)*my*my
	if dx <= 0 || dy <= 0 {
		return 0
	}
	return num / (math.Sqrt(dx) * math.Sqrt(dy))
}

// rankIC 计算 Spearman Rank IC。
func rankIC(pairs []pair) float64 {
	n := len(pairs)
	if n < 2 {
		return 0
	}
	rank := func(vals []float64) []float64 {
		idx := make([]int, len(vals))
		for i := range idx {
			idx[i] = i
		}
		sort.Slice(idx, func(i, j int) bool { return vals[idx[i]] < vals[idx[j]] })
		out := make([]float64, len(vals))
		for r, i := range idx {
			out[i] = float64(r)
		}
		return out
	}
	scores := make([]float64, n)
	rets := make([]float64, n)
	for i, p := range pairs {
		scores[i] = p.score
		rets[i] = p.ret
	}
	rs := rank(scores)
	rr := rank(rets)
	var sp []pair
	for i := range rs {
		sp = append(sp, pair{rs[i], rr[i]})
	}
	return pearsonIC(sp)
}


