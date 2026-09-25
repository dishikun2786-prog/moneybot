// Aether-HFT 完整决策链 demo（主决策闭环）：
// 强平流 → FDTD 波场 → 突破信号 → Kakeya 否决 → TDA 相变
//   → 宏微观共振判决(R_score=M×m) → UTA 组合保证金权重调度(MRR/W_i)
//   → NetEV 闸 → 沙盒撮合。
// 演示两条路径：A) 全闸通过下单；B) 假挂单被 Kakeya 一票否决。
package main

import (
	"fmt"
	"sort"
	"time"

	"github.com/moneybot/aetherhft/internal/fdtd"
	"github.com/moneybot/aetherhft/internal/kakeya"
	"github.com/moneybot/aetherhft/internal/liquidation"
	"github.com/moneybot/aetherhft/internal/resonance"
	"github.com/moneybot/aetherhft/internal/risk"
	"github.com/moneybot/aetherhft/internal/sandbox"
	"github.com/moneybot/aetherhft/internal/uta"
)

func sym(s string) [16]byte { var o [16]byte; copy(o[:], s); return o }

// stage 打印一阶段结果。
func stage(n int, name string) { fmt.Printf("\n[%d] %s\n", n, name) }

// buildGrid 构造价格轴网格（价格→索引直接映射）。
func buildGrid() *fdtd.Grid {
	g := fdtd.New(256, 1.0, 0.01, 0.4)
	c := make([]float64, 256)
	for i := range c {
		c[i] = 0.9
	}
	g.SetWaveSpeed(c)
	return g
}

// maxGapH0 是 TDA H_0 的简化版：价格点云的最大 gap（流动性真空代理）。
func maxGapH0(prices []float64) float64 {
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

func clamp01(x float64) float64 {
	if x < 0 {
		return 0
	}
	if x > 1 {
		return 1
	}
	return x
}

// decide 运行完整决策链，返回是否放行。
func decide(label string, wallSpoof bool, orderbook []float64) {
	fmt.Printf("  === %s ===\n", label)
	btc := sym("BTCUSDT")

	// [1] 强平流 → FDTD
	g := buildGrid()
	inj := liquidation.NewInjector(g, func(p float64) int { return int(p) }, 0.5)
	inj.InjectBatch([]liquidation.Event{
		{Price: 128, Size: 12, Side: liquidation.SideSell}, // 多头爆仓 → 卖压
		{Price: 130, Size: 10, Side: liquidation.SideBuy},  // 空头爆仓 → 买压
		{Price: 126, Size: 8, Side: liquidation.SideBuy},
	})
	for i := 0; i < 30; i++ {
		g.Step()
	}
	pDense := g.DensePressure()
	en := g.EnergyFlux()
	fmt.Printf("    FDTD: P_dense=%.3f E_n=%.3f (强平事件注入 %d 笔)\n", pDense, en, int(inj.Events()))

	// [2] 突破信号：压强超阈值
	const pThresh = 0.5
	if pDense < pThresh {
		fmt.Printf("    ✗ 信号未触发 (P_dense %.3f < %.2f)，流程终止\n", pDense, pThresh)
		return
	}
	fmt.Printf("    ✓ 突破信号触发 (P_dense %.3f ≥ %.2f)\n", pDense, pThresh)

	// [3] Kakeya 否决
	kf := kakeya.NewFilter(time.Minute)
	if wallSpoof {
		// 假挂单：墙出现即撤、无成交
		for i := 0; i < 3; i++ {
			kf.OnWall(kakeya.WallEvent{TsNs: 1, Price: 130 + float64(i), Side: kakeya.SideBid, Size: 100, Appear: true})
			kf.OnWall(kakeya.WallEvent{TsNs: 2, Price: 130 + float64(i), Side: kakeya.SideBid, Size: 100, Appear: false})
		}
	} else {
		// 真墙：出现后被成交吃掉
		kf.OnWall(kakeya.WallEvent{TsNs: 1, Price: 130, Side: kakeya.SideBid, Size: 100, Appear: true})
		kf.OnTrade(kakeya.TradeHit{TsNs: 2, Price: 130, Side: kakeya.SideAsk, Size: 100})
		kf.OnWall(kakeya.WallEvent{TsNs: 3, Price: 130, Side: kakeya.SideBid, Size: 100, Appear: false})
	}
	if kf.Veto(0.6) {
		fmt.Printf("    ✗ Kakeya 一票否决 (K_om=%.2f < 0.6，假挂单占主导)，流程终止\n", kf.KOm())
		return
	}
	fmt.Printf("    ✓ Kakeya 通过 (K_om=%.2f ≥ 0.6)\n", kf.KOm())

	// [4] TDA 相变：价格点云最大 gap
	gap := maxGapH0(orderbook)
	const gapThresh = 5.0
	if gap > gapThresh {
		fmt.Printf("    ✗ TDA 相变风险 (max gap %.2f > %.1f，流动性真空)，流程终止\n", gap, gapThresh)
		return
	}
	fmt.Printf("    ✓ TDA 通过 (max gap %.2f ≤ %.1f，无流动性断层)\n", gap, gapThresh)

	// [5] 宏微观共振判决（模块二）：R_score = M_score × m_score
	stage(5, "宏微观共振判决 (R_score = M × m)")
	fh := resonance.NewHist(10) // 资金费率历史
	oh := resonance.NewHist(10) // OI 历史
	for i := 0; i < 10; i++ {
		fh.Add(0.0001)
		oh.Add(1000 + float64(i)*10)
	}
	ma := resonance.ComputeMacro(0.0005, 1200, fh, oh) // 当前：费率异常高 + OI 高位
	riskTDA := clamp01(gap / 20.0)                    // 拓扑风险归一化
	mi := resonance.ComputeMicro(en, kf.KOm(), riskTDA, 5.0)
	d := resonance.Decide(ma, mi, 0.5)
	fmt.Printf("    M_score=%.3f (funding_z=%.3f oi_pct=%.3f)\n", ma.M, ma.FundingZ, ma.OIPercent)
	fmt.Printf("    m_score=%.3f (E_n=%.3f K_om=%.3f RiskTDA=%.3f)\n", mi.M, mi.En, mi.Kom, mi.RiskTDA)
	if !d.Fire {
		fmt.Printf("    ✗ 共振判决拒绝 (R_score=%.3f < 0.5，宏观引信未点燃或微观动能不足)，流程终止\n", d.R)
		return
	}
	fmt.Printf("    ✓ 共振判决通过 (R_score=%.3f ≥ 0.5)\n", d.R)

	// [6] UTA 组合保证金动态资本调度（模块六）
	stage(6, "UTA 组合保证金权重调度 (MRR / W_i)")
	sched := uta.NewScheduler()
	assets := []uta.Asset{
		{Symbol: "BTCUSDT", RScore: d.R, VaR: 10},
		{Symbol: "ETHUSDT", RScore: d.R * 0.8, VaR: 12},
	}
	w, scale, halt := sched.Allocate(assets, 1000)
	if halt {
		fmt.Println("    ✗ UTA 权重分配失败，流程终止")
		return
	}
	acct := uta.Account{TotalEquity: 10000, MaintenanceMargin: 5000}
	mrr := sched.MRR(acct)
	w, scale, halt = sched.ApplyMRR(mrr, w, scale)
	fmt.Printf("    MRR=%.3f (维持保证金 %.0f / 总权益 %.0f)\n", mrr, acct.MaintenanceMargin, acct.TotalEquity)
	if halt {
		fmt.Println("    ✗ UTA 熔断 (MRR ≥ 1)，流程终止")
		return
	}
	notional := 1000 * scale * w[0]
	fmt.Printf("    ✓ UTA 调度完成: W=[BTC %.3f, ETH %.3f] scale=%.2f → BTC 名义=%.0f USDT\n",
		w[0], w[1], scale, notional)

	// [7] NetEV 闸
	ef := risk.NewExpectancyFilter(risk.DefaultFeeSchedule(), 20, 2)
	sig := risk.Signal{
		PSuccess: 0.88, TargetProfit: 80, StopLoss: 40,
		SlippageEst: 5, Notional: notional,
	}
	ok, ev := ef.Allow(sig, 0)
	if !ok {
		fmt.Printf("    ✗ NetEV 闸拒绝 (NetEV=%.2f 未超阈值/手续费)，流程终止\n", ev)
		return
	}
	fmt.Printf("    ✓ NetEV 闸通过 (NetEV=%.2f，远超往返手续费)\n", ev)

	// [8] 沙盒撮合：市价突破单
	sb := sandbox.NewSandbox()
	order := &sandbox.Order{ID: "breakout-1", Symbol: btc, Side: sandbox.SideBuy, Price: 130, Size: 3}
	book := []sandbox.MarketLevel{{Price: 130, Size: 1}, {Price: 130.5, Size: 5}}
	avg, filled, ok := sb.PlaceMarket(order, book)
	if !ok {
		fmt.Printf("    ✗ 撮合失败（流动性不足）\n")
		return
	}
	fmt.Printf("    ✓ 已下单成交: 数量=%v 成交均价=%.2f 滑点=%+.4f\n", filled, avg, order.Slippage())
}

func main() {
	fmt.Println("=== Aether-HFT 主决策闭环 demo ===")
	fmt.Println("链: 强平流 → FDTD → 突破信号 → Kakeya → TDA → 共振判决(R=M×m) → UTA权重(MRR/W_i) → NetEV → 沙盒撮合")
	normalBook := []float64{125, 126, 127, 128, 129, 130, 131, 132, 133}
	decide("路径 A：真墙（全闸放行）", false, normalBook)
	decide("路径 B：假挂单（Kakeya 否决）", true, normalBook)
	fmt.Println("\n=== demo 结束 ===")
}
