package trend

import (
	"math"
	"testing"
)

func TestATR(t *testing.T) {
	a := NewATR(14)
	// 先初始化
	a.Update(100, 99, 99.5)
	// 波动 1 个点
	v := a.Update(101, 100, 100.5)
	if v <= 0 {
		t.Fatalf("ATR 应>0: %v", v)
	}
	// 震荡收敛后 ATR 应下降
	for i := 0; i < 50; i++ {
		a.Update(100.5, 100, 100.2)
	}
	if a.Value() > 1.5 {
		t.Fatalf("震荡后 ATR 应收敛: %v", a.Value())
	}
}

func TestEMA(t *testing.T) {
	e := NewEMA(5)
	e.Update(100)
	v := e.Update(110)
	if v <= 100 || v >= 110 {
		t.Fatalf("EMA 应在 100~110 之间: %v", v)
	}
}

func TestAnalyze(t *testing.T) {
	a := NewAnalyzer()
	// 预热
	for i := 0; i < 100; i++ {
		p := 100.0 + float64(i)*0.1
		a.Analyze("BTC", TrendInput{Price: p, Funding: 0.0001, OI: 50000, Imb: 0.1, CVD: 5, SpreadBP: 2})
	}
	// 上涨趋势 + 高 imb + 高 funding
	st := a.Analyze("BTC", TrendInput{Price: 115, Funding: 0.0005, OI: 60000, Imb: 0.8, CVD: 50, SpreadBP: 1})
	if st.Dir != "long" {
		t.Fatalf("上涨应 long: %v", st.Dir)
	}
	if st.Score <= 0 || st.Score > 1 {
		t.Fatalf("score 应在 0~1: %v", st.Score)
	}
	if st.RScore <= 0 {
		t.Fatalf("RScore 应>0: %v", st.RScore)
	}
	if st.Consistency != 1.0 {
		t.Fatalf("单边上涨三周期应一致: %v", st.Consistency)
	}
}

func TestAnalyzeFlat(t *testing.T) {
	a := NewAnalyzer()
	for i := 0; i < 100; i++ {
		a.Analyze("BTC", TrendInput{Price: 100, Funding: 0.0001, OI: 50000, Imb: 0, CVD: 0, SpreadBP: 2})
	}
	st := a.Analyze("BTC", TrendInput{Price: 100, Funding: 0.0001, OI: 50000, Imb: 0, CVD: 0, SpreadBP: 2})
	if st.Dir != "flat" {
		t.Fatalf("横盘应 flat: %v", st.Dir)
	}
}

func TestSelectorQuota(t *testing.T) {
	s := NewSelector()
	syms := []Symbol{
		{Name: "BTCUSDT", Cat: LargeCap, State: TrendState{Dir: "long", Score: 0.9}},
		{Name: "ETHUSDT", Cat: LargeCap, State: TrendState{Dir: "long", Score: 0.85}},
		{Name: "XAUUSDT", Cat: Precious, State: TrendState{Dir: "short", Score: 0.8}},
		{Name: "XAGUSDT", Cat: Precious, State: TrendState{Dir: "short", Score: 0.75}},
		{Name: "SOLUSDT", Cat: Altcoin, State: TrendState{Dir: "long", Score: 0.7}},
		{Name: "NEARUSDT", Cat: Altcoin, State: TrendState{Dir: "long", Score: 0.6}},
		{Name: "XRPUSDT", Cat: Altcoin, State: TrendState{Dir: "short", Score: 0.5}},
	}
	actions := s.Select(syms)
	// 大盘只应选 1 个(BTC), 贵金属 1 个(XAU), 山寨 2 个(SOL+NEAR)
	var opens []string
	for _, a := range actions {
		if a.Type == "open" {
			opens = append(opens, a.Symbol)
		}
	}
	if len(opens) != 3 {
		t.Fatalf("应选 3 标的(maxHeld=3, 配额软约束): %v", opens)
	}
	// ETH(大盘) 不应被选(BTC 已占大盘配额)
	for _, o := range opens {
		if o == "ETHUSDT" {
			t.Fatalf("ETH 不应被选(大盘配额已满): %v", opens)
		}
	}
}

func TestSelectorRotation(t *testing.T) {
	s := NewSelector()
	s.SetHeld(map[string]float64{"BTCUSDT": 0.9})
	// BTC score 跌破 exitScore(0.4) → close
	syms := []Symbol{
		{Name: "BTCUSDT", Cat: LargeCap, State: TrendState{Dir: "flat", Score: 0.1}},
	}
	actions := s.Select(syms)
	foundClose := false
	for _, a := range actions {
		if a.Type == "close" && a.Symbol == "BTCUSDT" {
			foundClose = true
		}
	}
	if !foundClose {
		t.Fatalf("趋势衰竭应 close: %v", actions)
	}
	if _, ok := s.held["BTCUSDT"]; ok {
		t.Fatalf("close 后应移除持仓")
	}
}

func TestSelectorWeights(t *testing.T) {
	s := NewSelector()
	s.SetHeld(map[string]float64{"BTCUSDT": 0.9, "XAUUSDT": 0.6})
	w := s.Weights()
	if math.Abs(w["BTCUSDT"]-0.6) > 1e-9 {
		t.Fatalf("BTC 权重应 0.6: %v", w["BTCUSDT"])
	}
	if math.Abs(w["XAUUSDT"]-0.4) > 1e-9 {
		t.Fatalf("XAU 权重应 0.4: %v", w["XAUUSDT"])
	}
}
