// 7 标的同步趋势判断 + 择优选择 demo。
// 用合成数据模拟不同标的的趋势/宏观/微观状态，演示多周期评分 + 类别配额选标。
package main

import (
	"fmt"
	"sort"

	"github.com/moneybot/aetherhft/internal/trend"
)

func main() {
	fmt.Println("=== 7 标的同步趋势判断 + 择优选标 demo ===\n")

	// 7 标的（类别 + 合成趋势输入）
	type seed struct {
		name    string
		cat     trend.Category
		drift   float64 // 每 tick 价格漂移（趋势方向+强度）
		funding float64
		oi      float64
		imb     float64
		cvd     float64
	}
	seeds := []seed{
		{"BTCUSDT", trend.LargeCap, 0.08, 0.0005, 60000, 0.8, 50},
		{"ETHUSDT", trend.LargeCap, 0.05, 0.0004, 50000, 0.5, 25},
		{"XAUUSDT", trend.Precious, -0.06, 0.0003, 25000, -0.7, -40},
		{"XAGUSDT", trend.Precious, -0.03, 0.0002, 20000, -0.4, -15},
		{"SOLUSDT", trend.Altcoin, 0.09, 0.0002, 30000, 0.8, 50},
		{"NEARUSDT", trend.Altcoin, 0.05, 0.0003, 40000, 0.4, 15},
		{"XRPUSDT", trend.Altcoin, 0.01, 0.0001, 50000, 0.1, 2},
	}

	analyzers := map[string]*trend.Analyzer{}
	for _, sd := range seeds {
		analyzers[sd.name] = trend.NewAnalyzer()
	}

	// 预热 + 多轮评分
	for i := 0; i < 120; i++ {
		for _, sd := range seeds {
			a := analyzers[sd.name]
			price := 100 + float64(i)*sd.drift
			a.Analyze(sd.name, trend.TrendInput{Price: price, Funding: sd.funding, OI: sd.oi, Imb: sd.imb, CVD: sd.cvd, SpreadBP: 2})
		}
	}

	// 最终一轮：输出 7 标的评分
	var syms []trend.Symbol
	for _, sd := range seeds {
		st := analyzers[sd.name].Analyze(sd.name, trend.TrendInput{
			Price: 100 + 120*sd.drift, Funding: sd.funding, OI: sd.oi, Imb: sd.imb, CVD: sd.cvd, SpreadBP: 2,
		})
		syms = append(syms, trend.Symbol{Name: sd.name, Cat: sd.cat, State: st})
	}

	fmt.Println("--- 7 标的趋势评分 ---")
	sort.Slice(syms, func(i, j int) bool { return syms[i].State.Score > syms[j].State.Score })
	for _, s := range syms {
		st := s.State
		fmt.Printf("  %-9s dir=%-5s score=%.3f (强度%.2f 宏观%.2f 微观%.2f 一致%.2f) R=%.3f\n",
			s.Name, st.Dir, st.Score, st.Strength, st.Macro, st.Micro, st.Consistency, st.RScore)
	}

	fmt.Println("\n--- 择优选择（类别配额: 大盘1 贵金属1 山寨2, 上限3）---")
	sel := trend.NewSelector()
	actions := sel.Select(syms)
	for _, a := range actions {
		fmt.Printf("  %-9s %s\n", a.Symbol, a.Type)
	}
	fmt.Println("\n--- 选中标的权重 ---")
	for name, w := range sel.Weights() {
		fmt.Printf("  %-9s %.1f%%\n", name, w*100)
	}
}
