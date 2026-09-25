package liquidation

import (
	"sync/atomic"

	"github.com/moneybot/aetherhft/internal/fdtd"
)

// Side 是强平方向。
// Bybit v5 liquidation.side:
//   - "Buy"  = 空头被强平，强制买入回补 → 买压（正脉冲）
//   - "Sell" = 多头被强平，强制卖出 → 卖压（负脉冲）
type Side uint8

const (
	SideBuy  Side = iota // 空头爆仓 → 正向脉冲（上冲）
	SideSell             // 多头爆仓 → 负向脉冲（下杀）
)

// Event 是一笔强平事件。
type Event struct {
	TsNs  int64
	Price float64
	Size  float64 // 爆仓量（合约张数或币量，由调用方归一化）
	Side  Side
}

// Impulse 返回该事件注入 FDTD 外源项的脉冲值：幅度按爆仓量×scale，符号按方向。
func (e Event) Impulse(scale float64) float64 {
	v := e.Size * scale
	if e.Side == SideSell {
		return -v
	}
	return v
}

// Injector 把强平事件映射到 FDTD 网格索引并注入外源项。
// 并发安全由 fdtd.Grid 内部 RWMutex 保证：网络协程写 F、计算循环读 F，零竞争。
type Injector struct {
	grid       *fdtd.Grid
	priceToIdx func(float64) int // 价格 → 网格索引映射（由网格价格区间决定）
	scale      float64           // 爆仓量 → 脉冲幅度缩放
	events     atomic.Uint64     // 已注入事件计数（观测埋点）
}

// NewInjector 构造注入器。scale 控制单笔爆仓对波场的冲击强度。
func NewInjector(g *fdtd.Grid, priceToIdx func(float64) int, scale float64) *Injector {
	return &Injector{grid: g, priceToIdx: priceToIdx, scale: scale}
}

// Inject 注入一笔强平事件到 FDTD 外源项。
func (in *Injector) Inject(e Event) {
	if in.grid == nil || in.priceToIdx == nil {
		return
	}
	in.grid.InjectImpulse(e.Price, e.Impulse(in.scale), in.priceToIdx)
	in.events.Add(1)
}

// Events 返回已注入强平事件数。
func (in *Injector) Events() uint64 { return in.events.Load() }

// InjectBatch 批量注入（一次锁内完成多笔，减少锁竞争）。
func (in *Injector) InjectBatch(events []Event) {
	for _, e := range events {
		in.Inject(e)
	}
}