package fdtd

import (
	"sync"
	"sync/atomic"
)

// Grid 是一维受迫阻尼波动方程的 FDTD 离散网格（价格-深度格）。
//
//	∂²u/∂t² = c(x)² ∂²u/∂x² − γ ∂u/∂t + F(x,t)
//
// 显式三点差分（时间中心、空间二阶）：
//
//	uᵢⁿ⁺¹ = ( 2·uᵢⁿ − a2·uᵢⁿ⁻¹ + rᵢ·(uᵢ₋₁ⁿ − 2uᵢⁿ + uᵢ₊₁ⁿ) + Δt²·Fᵢ ) / a0
//	a0 = 1 + γΔt/2,  a2 = 1 − γΔt/2,  rᵢ = (cᵢ·Δt/Δx)²   (Courant² 逐格)
//
// 三缓冲指针轮转（u / uPrev / uNext）实现零分配推进。
//
// 并发安全：mu (RWMutex) 保护 F 外源项 —— 强平注入协程持写锁写 F，
// 计算循环持读锁读 F，热路径零数据竞争。
type Grid struct {
	mu         sync.RWMutex
	N          int
	dt, dx     float64
	gamma      float64
	c2         []float64 // 逐格 Courant² 因子
	u, uPrev   []float64
	uNext      []float64
	F          []float64 // 外部驱动（订单流/墙压/强平脉冲）
	iterations atomic.Uint64
}

// New 创建网格。dt/dx 必须满足全局 CFL：max(c)·dt/dx < 1。
func New(n int, dx, dt, gamma float64) *Grid {
	if n < 3 {
		n = 3
	}
	return &Grid{
		N:      n,
		dx:     dx,
		dt:     dt,
		gamma:  gamma,
		c2:     make([]float64, n),
		u:      make([]float64, n),
		uPrev:  make([]float64, n),
		uNext:  make([]float64, n),
		F:      make([]float64, n),
	}
}

// SetWaveSpeed 设置逐格波速 c(x) 并预计算 Courant²。
func (g *Grid) SetWaveSpeed(c []float64) {
	r := g.dt / g.dx
	r *= r
	for i := 0; i < g.N && i < len(c); i++ {
		g.c2[i] = c[i] * c[i] * r
	}
}

// Forcing 返回驱动项切片（调用方直接写 F[i]，避免每步 setter 分配）。
// 注意：直接写会绕过锁；并发注入请用 AddForcing / InjectImpulse。
func (g *Grid) Forcing() []float64 { return g.F }

// Step 推进一个 FDTD 时间步（持读锁读 F）。
//
// 热路径：单层循环、连续 float64 切片、无函数调用/无边界检查热点，
// 交由 Go 编译器自动向量化（AVX2，见 go build -gcflags）。
func (g *Grid) Step() {
	g.mu.RLock()
	defer g.mu.RUnlock()
	g.stepUnlocked()
}

func (g *Grid) stepUnlocked() {
	a0 := 1.0 + 0.5*g.gamma*g.dt
	a2 := 1.0 - 0.5*g.gamma*g.dt
	dt2 := g.dt * g.dt
	u, up, un, c2, F := g.u, g.uPrev, g.uNext, g.c2, g.F
	n := g.N
	for i := 1; i < n-1; i++ {
		un[i] = (2*u[i] - a2*up[i] + c2[i]*(u[i-1]-2*u[i]+u[i+1]) + dt2*F[i]) / a0
	}
	un[0] = un[1]
	un[n-1] = un[n-2]
	g.u, g.uPrev, g.uNext = g.uNext, g.u, g.uPrev
	g.iterations.Add(1)
}

// AddForcing 在外源项 F[idx] 上累加 val（持写锁，线程安全）。
func (g *Grid) AddForcing(idx int, val float64) {
	if idx < 0 || idx >= g.N {
		return
	}
	g.mu.Lock()
	g.F[idx] += val
	g.mu.Unlock()
}

// InjectImpulse 把一笔脉冲按 price→网格索引映射后注入外源项。
func (g *Grid) InjectImpulse(price, impulse float64, priceToIdx func(float64) int) {
	if priceToIdx == nil {
		return
	}
	g.AddForcing(priceToIdx(price), impulse)
}

// U 返回当前位移场（只读视图）。
func (g *Grid) U() []float64 { return g.u }

// Iterations 返回已推进步数（观测埋点）。
func (g *Grid) Iterations() uint64 { return g.iterations.Load() }

// EnergyFlux 返回离散波能通量 E_n = Σ(Δu/Δt)²（动能项，正比于冲击剧烈度）。
func (g *Grid) EnergyFlux() float64 {
	var e float64
	for i := 0; i < g.N; i++ {
		d := g.u[i] - g.uPrev[i]
		e += d * d
	}
	return e / (g.dt * g.dt)
}

// DensePressure 返回突破前压强 P_dense = max(du/dt)，衡量关键价前的能量压缩。
func (g *Grid) DensePressure() float64 {
	var p, v float64
	for i := 0; i < g.N; i++ {
		v = g.u[i] - g.uPrev[i]
		if v > p {
			p = v
		}
	}
	return p / g.dt
}