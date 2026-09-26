package trend

import (
	"math"

	"github.com/moneybot/aetherhft/internal/resonance"
)

// EMA 是指数移动平均。
type EMA struct {
	alpha float64
	value float64
	init  bool
}

// NewEMA 构造 EMA，period 为窗口。
func NewEMA(period int) *EMA {
	if period < 1 {
		period = 1
	}
	return &EMA{alpha: 2.0 / float64(period+1)}
}

// Update 更新并返回当前 EMA。
func (e *EMA) Update(p float64) float64 {
	if !e.init {
		e.value = p
		e.init = true
		return p
	}
	e.value = e.alpha*p + (1-e.alpha)*e.value
	return e.value
}

// Value 返回当前 EMA。
func (e *EMA) Value() float64 { return e.value }

// TrendInput 是单标的趋势分析输入（数据来自 feed 盘口/OI/funding）。
type TrendInput struct {
	Price    float64 // 现价
	Funding  float64 // 资金费率(原始值)
	OI       float64 // 持仓量
	Imb      float64 // 盘口不平衡 -1~1
	CVD      float64 // 主动量净流(买-卖)
	SpreadBP float64 // 点差 bp
}

// TrendState 是单标的趋势状态（同步评分输出）。
type TrendState struct {
	Symbol      string
	Dir         string  // long / short / flat
	Score       float64 // 综合 TrendScore 0~1
	Strength    float64 // 趋势强度(1h EMA 斜率)
	Macro       float64 // 宏观 M_score
	Micro       float64 // 微观 m_score
	Consistency float64 // 多周期方向一致性 0~1
	RScore      float64 // 共振乘积 R = M × m
}

// Analyzer 是多周期趋势分析器（1h/15m/5m 三周期 + 宏观 + 微观）。
type Analyzer struct {
	ema1h, ema15m, ema5m *EMA
	prevEma1h            float64
	fundHist, oiHist     *resonance.Hist
}

// NewAnalyzer 构造分析器。
func NewAnalyzer() *Analyzer {
	return &Analyzer{
		ema1h:    NewEMA(60),
		ema15m:   NewEMA(15),
		ema5m:    NewEMA(5),
		fundHist: resonance.NewHist(48), // funding 8h 周期 × 6
		oiHist:   resonance.NewHist(48),
	}
}

func sign(v float64) float64 {
	if v > 0 {
		return 1
	}
	if v < 0 {
		return -1
	}
	return 0
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

// Analyze 计算单标的趋势状态（每分钟调用一次）。
func (a *Analyzer) Analyze(symbol string, in TrendInput) TrendState {
	st := TrendState{Symbol: symbol}
	e1h := a.ema1h.Update(in.Price)
	e15m := a.ema15m.Update(in.Price)
	e5m := a.ema5m.Update(in.Price)

	// 1h EMA 斜率（趋势强度）
	slope := 0.0
	if a.prevEma1h > 0 {
		slope = (e1h - a.prevEma1h) / a.prevEma1h
	}
	a.prevEma1h = e1h

	// 多周期方向一致性
	d1h := sign(in.Price - e1h)
	d15m := sign(in.Price - e15m)
	d5m := sign(in.Price - e5m)
	cons := 0.0
	if d1h == d15m && d15m == d5m {
		cons = 1.0
	} else if d1h == d15m || d15m == d5m || d1h == d5m {
		cons = 0.5
	}

	// 宏观 M_score（funding 偏离 + OI 分位）
	a.fundHist.Add(in.Funding)
	a.oiHist.Add(in.OI)
	ma := resonance.ComputeMacro(in.Funding, in.OI, a.fundHist, a.oiHist)

	// 微观 m_score（盘口 imb + CVD + spread）
	mi := resonance.ComputeMicro(
		math.Abs(in.Imb)*10,               // En 近似
		clamp01(math.Abs(in.CVD)/100),     // K_om 近似（流不平衡）
		clamp01(in.SpreadBP/20),           // 拓扑风险近似（点差）
		5.0,
	)

	st.Strength = clamp01(math.Abs(slope) * 1000)
	st.Macro = ma.M
	st.Micro = mi.M
	st.Consistency = cons
	st.RScore = ma.M * mi.M
	st.Score = 0.35*st.Strength + 0.25*st.Macro + 0.25*st.Micro + 0.15*st.Consistency

	// 方向：1h 与 15m 同向才定方向，否则 flat
	if d1h > 0 && d15m > 0 {
		st.Dir = "long"
	} else if d1h < 0 && d15m < 0 {
		st.Dir = "short"
	} else {
		st.Dir = "flat"
	}
	return st
}
