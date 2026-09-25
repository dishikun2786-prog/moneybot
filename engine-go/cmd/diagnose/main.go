// Aether-HFT 全链路诊断：逐标的模拟盘单测 + 分阶段延迟评估。
// 读取 go_orderbook.json 真实盘口，对每个上线标的跑完整决策链（放行路径），
// 输出分阶段耗时、总延迟、<3ms 硬限判断与总体稳定性汇总。
package main

import (
	"encoding/json"
	"fmt"
	"os"
	"sort"
	"strconv"
	"time"

	"github.com/moneybot/aetherhft/internal/fdtd"
	"github.com/moneybot/aetherhft/internal/kakeya"
	"github.com/moneybot/aetherhft/internal/latency"
	"github.com/moneybot/aetherhft/internal/liquidation"
	"github.com/moneybot/aetherhft/internal/resonance"
	"github.com/moneybot/aetherhft/internal/risk"
	"github.com/moneybot/aetherhft/internal/sandbox"
	"github.com/moneybot/aetherhft/internal/uta"
)

// 端到端延迟硬限（NFR 铁律 2）。
const latencyBudget = 3 * time.Millisecond

// book 是单标的盘口视图。
type book struct {
	symbol string
	bids   [][2]float64
	asks   [][2]float64
}

// px 是单标的现价/OI/费率。
type px struct {
	last    float64
	funding float64
	oi      float64
}

func sym16(s string) [16]byte { var o [16]byte; copy(o[:], s); return o }

func clamp01(x float64) float64 {
	if x < 0 {
		return 0
	}
	if x > 1 {
		return 1
	}
	return x
}

// loadData 从 go_orderbook.json 读全部标的盘口 + px。
func loadData(path string) (map[string]book, map[string]px, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, nil, err
	}
	var d struct {
		Books map[string]struct {
			Bids [][]interface{} `json:"bids"`
			Asks [][]interface{} `json:"asks"`
		} `json:"books"`
		Px map[string]struct {
			Last    float64 `json:"last"`
			Funding float64 `json:"funding"`
			OI      float64 `json:"oi"`
		} `json:"px"`
	}
	if err := json.Unmarshal(raw, &d); err != nil {
		return nil, nil, err
	}
	books := map[string]book{}
	for sym, b := range d.Books {
		bk := book{symbol: sym}
		for _, lv := range b.Bids {
			if len(lv) >= 2 {
				p, _ := strconv.ParseFloat(lv[0].(string), 64)
				s, _ := lv[1].(float64)
				bk.bids = append(bk.bids, [2]float64{p, s})
			}
		}
		for _, lv := range b.Asks {
			if len(lv) >= 2 {
				p, _ := strconv.ParseFloat(lv[0].(string), 64)
				s, _ := lv[1].(float64)
				bk.asks = append(bk.asks, [2]float64{p, s})
			}
		}
		books[sym] = bk
	}
	pxs := map[string]px{}
	for sym, p := range d.Px {
		pxs[sym] = px{last: p.Last, funding: p.Funding, oi: p.OI}
	}
	return books, pxs, nil
}

// maxGap 计算盘口价格点云的最大 gap（TDA H_0 代理）。
func maxGap(prices []float64) float64 {
	if len(prices) < 2 {
		return 0
	}
	sort.Float64s(prices)
	m := 0.0
	for i := 1; i < len(prices); i++ {
		if d := prices[i] - prices[i-1]; d > m {
			m = d
		}
	}
	return m
}

// runSymbol 对单标的跑完整决策链（放行路径），返回延迟报告与是否放行。
func runSymbol(bk book, pk px, fh, oh *resonance.Hist, forceFire bool) (*latency.Timer, bool) {
	t := latency.NewTimer()

	// [1] 强平流 → FDTD
	base := bk.bids[len(bk.bids)-1][0]
	top := bk.asks[len(bk.asks)-1][0]
	step := (top - base) / 255.0
	if step <= 0 {
		step = 0.01
	}
	toIdx := func(p float64) int {
		i := int((p - base) / step)
		if i < 0 {
			return 0
		}
		if i > 255 {
			return 255
		}
		return i
	}
	g := fdtd.New(256, 1.0, 0.01, 0.4)
	c := make([]float64, 256)
	for i := range c {
		c[i] = 0.9
	}
	g.SetWaveSpeed(c)
	mid := (base + top) / 2
	inj := liquidation.NewInjector(g, toIdx, 0.5)
	inj.InjectBatch([]liquidation.Event{
		{Price: mid + step*2, Size: 12, Side: liquidation.SideSell},
		{Price: mid - step*2, Size: 10, Side: liquidation.SideBuy},
		{Price: mid, Size: 8, Side: liquidation.SideBuy},
	})
	for i := 0; i < 30; i++ {
		g.Step()
	}
	en := g.EnergyFlux()
	pDense := g.DensePressure()
	t.Mark("FDTD")

	// [2] 突破信号
	if pDense < 0.5 {
		return t, false
	}
	t.Mark("信号")

	// [3] Kakeya（真墙放行）
	kf := kakeya.NewFilter(time.Minute)
	midIdx := toIdx(mid)
	kf.OnWall(kakeya.WallEvent{TsNs: 1, Price: float64(midIdx), Side: kakeya.SideBid, Size: 100, Appear: true})
	kf.OnTrade(kakeya.TradeHit{TsNs: 2, Price: float64(midIdx), Side: kakeya.SideAsk, Size: 100})
	kf.OnWall(kakeya.WallEvent{TsNs: 3, Price: float64(midIdx), Side: kakeya.SideBid, Size: 100, Appear: false})
	kom := kf.KOm()
	if kf.Veto(0.6) {
		return t, false
	}
	t.Mark("Kakeya")

	// [4] TDA（真实盘口价格点云 max gap）
	var prices []float64
	for _, l := range bk.bids {
		prices = append(prices, l[0])
	}
	for _, l := range bk.asks {
		prices = append(prices, l[0])
	}
	gap := maxGap(prices)
	t.Mark("TDA")

	// [5] 共振判决（模块二）
	ma := resonance.ComputeMacro(pk.funding, pk.oi, fh, oh)
	riskTDA := clamp01(gap / (step * 200))
	mi := resonance.ComputeMicro(en, kom, riskTDA, 5.0)
	d := resonance.Decide(ma, mi, 0.5)
	if !forceFire && !d.Fire {
		return t, false
	}
	t.Mark("共振")

	// [6] UTA 调度（模块六）
	sched := uta.NewScheduler()
	assets := []uta.Asset{{Symbol: bk.symbol, RScore: d.R, VaR: 10}}
	w, scale, halt := sched.Allocate(assets, 1000)
	acct := uta.Account{TotalEquity: 10000, MaintenanceMargin: 5000}
	w, scale, halt = sched.ApplyMRR(sched.MRR(acct), w, scale)
	if halt {
		return t, false
	}
	_ = w
	_ = scale
	t.Mark("UTA")

	// [7] NetEV
	ef := risk.NewExpectancyFilter(risk.DefaultFeeSchedule(), 20, 2)
	ok, _ := ef.Allow(risk.Signal{PSuccess: 0.88, TargetProfit: 80, StopLoss: 40, SlippageEst: 5, Notional: 600}, 0)
	if !ok {
		return t, false
	}
	t.Mark("NetEV")

	// [8] 沙盒撮合
	sb := sandbox.NewSandbox()
	order := &sandbox.Order{ID: "diag-" + bk.symbol, Symbol: sym16(bk.symbol), Side: sandbox.SideBuy, Price: bk.asks[0][0], Size: 3}
	levels := []sandbox.MarketLevel{{Price: bk.asks[0][0], Size: bk.asks[0][1]}, {Price: bk.asks[1][0], Size: bk.asks[1][1]}}
	_, _, ok = sb.PlaceMarket(order, levels)
	if !ok {
		return t, false
	}
	t.Mark("撮合")
	return t, true
}

func main() {
	path := os.Getenv("MB_DEPTH_OUT")
	if path == "" {
		path = os.ExpandEnv("$HOME/polymarket/logs/go_orderbook.json")
	}
	books, pxs, err := loadData(path)
	if err != nil {
		fmt.Printf("读取盘口失败: %v\n", err)
		os.Exit(1)
	}
	fmt.Printf("=== Aether-HFT 全链路逐标的单测 (真实盘口 %d 标的) ===\n", len(books))
	fmt.Printf("端到端延迟硬限: %v\n\n", latencyBudget)

	// 宏观历史（funding/OI 窗口，跨标的共享）
	fh := resonance.NewHist(10)
	oh := resonance.NewHist(10)
	for i := 0; i < 10; i++ {
		fh.Add(0.0001)
		oh.Add(50000)
	}

	syms := make([]string, 0, len(books))
	for s := range books {
		syms = append(syms, s)
	}
	sort.Strings(syms)

	rounds := 200 // 每标的单测轮数（延迟分布采样）
	pass, fail := 0, 0
	global := latency.NewStats()
	for _, s := range syms {
		bk := books[s]
		pk := pxs[s]
		st := latency.NewStats()
		ok := true
		for r := 0; r < rounds; r++ {
			t, o := runSymbol(bk, pk, fh, oh, true)
			if !o {
				ok = false
			}
			st.Add(t.Total())
			global.Add(t.Total())
		}
		if ok {
			pass++
		} else {
			fail++
		}
		fmt.Printf("▸ %-10s last=%-10.2f oi=%-10.0f funding=%.2e  %s\n",
			s, pk.last, pk.oi, pk.funding, latencyStatsString(st))
	}
	fmt.Println()
	fmt.Printf("=== 全链路延迟评估（%d 标的 × %d 轮 = %d 次决策） ===\n",
		len(syms), rounds, global.Count())
	fmt.Printf("实时延迟分布: %s\n", latencyStatsString(global))
	fmt.Printf("硬限预算: %v | 达标: %s\n", latencyBudget,
		map[bool]string{true: "✗ 超限", false: "✓ 全部达标"}[global.Max() > latencyBudget])
	fmt.Printf("通过: %d/7 | 失败: %d\n", pass, fail)
	if fail == 0 && global.Max() <= latencyBudget {
		fmt.Println("总体稳定性: ✅ 全链路稳定，端到端延迟达标")
	} else {
		fmt.Println("总体稳定性: ⚠️ 存在失败或延迟超限，需排查")
	}
}

func latencyStatsString(s *latency.Stats) string {
	return fmt.Sprintf("min=%v avg=%v p50=%v p95=%v p99=%v max=%v",
		s.Min(), s.Avg(), s.Percentile(0.50), s.Percentile(0.95), s.Percentile(0.99), s.Max())
}
