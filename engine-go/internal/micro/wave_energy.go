package micro

import "github.com/moneybot/aetherhft/internal/fdtd"

// WaveEnergy 用盘口量在价格轴上的移动计算 FDTD 波能 En（深度动能）。
// 把盘口量变化作为外源项注入 FDTD 网格，能量通量 En 捕捉「深度动能爆发」。
type WaveEnergy struct {
	grid        *fdtd.Grid
	nGrid       int
	prev        map[float64]float64 // 上一帧档位量
	minPrice    float64
	maxPrice    float64
	initialized bool
}

// NewWaveEnergy 构造波能计算器。nGrid=网格点数，gamma=阻尼系数。
func NewWaveEnergy(nGrid int, gamma float64) *WaveEnergy {
	if nGrid < 64 {
		nGrid = 256
	}
	g := fdtd.New(nGrid, 1.0, 0.01, gamma)
	c := make([]float64, nGrid)
	for i := range c {
		c[i] = 0.9
	}
	g.SetWaveSpeed(c)
	return &WaveEnergy{grid: g, nGrid: nGrid, prev: map[float64]float64{}}
}

// priceToIdx 把价格映射到网格索引。
func (w *WaveEnergy) priceToIdx(price float64) int {
	n := w.nGrid
	if !w.initialized || w.maxPrice <= w.minPrice {
		return n / 2
	}
	// 留边界余量，避免 forcing 注入边界被边界条件吞掉
	idx := 2 + int((price-w.minPrice)/(w.maxPrice-w.minPrice)*float64(n-5))
	if idx < 2 {
		idx = 2
	}
	if idx >= n-2 {
		idx = n - 3
	}
	return idx
}

// OnDepth 输入一帧盘口，计算盘口量变化并注入 FDTD，返回波能 En。
func (w *WaveEnergy) OnDepth(levels []Level) float64 {
	cur := map[float64]float64{}
	for _, l := range levels {
		if l.Size > 0 {
			cur[l.Price] = l.Size
			if !w.initialized || l.Price < w.minPrice {
				w.minPrice = l.Price
			}
			if !w.initialized || l.Price > w.maxPrice {
				w.maxPrice = l.Price
			}
		}
	}
	if !w.initialized {
		w.initialized = true
		w.prev = cur
		return 0
	}
	// 量变化注入 forcing（量增加=买压，量减少=卖压）
	for price, size := range cur {
		delta := size - w.prev[price]
		if delta != 0 {
			w.grid.AddForcing(w.priceToIdx(price), delta*0.1)
		}
	}
	w.prev = cur
	// 迭代推进波场
	for i := 0; i < 30; i++ {
		w.grid.Step()
	}
	return w.grid.EnergyFlux()
}

// DensePressure 返回突破前压强 P_dense。
func (w *WaveEnergy) DensePressure() float64 { return w.grid.DensePressure() }
