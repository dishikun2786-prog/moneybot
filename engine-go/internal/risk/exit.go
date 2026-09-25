package risk

// Position 是持仓状态。
type Position struct {
	EntryPrice  float64
	Notional    float64
	SlippageEst float64 // 开仓滑点预估（金额）
	FloatingPnL float64 // 当前浮动盈亏（金额）
}

// ExitManager 是手续费与动能耗散感知的动态退出。
// 痛点：传统阻尼退出在微小动能衰减时过早平仓，利润无法覆盖手续费。
// 策略：仅当「波场能量结构性崩溃」且「浮盈已覆盖双向手续费安全垫」才平仓锁利。
type ExitManager struct {
	sched         *FeeSchedule
	vip           int
	collapseRatio float64 // 当前 En < peak × collapseRatio 视为结构性崩溃
}

// NewExitManager 构造退出管理器。
func NewExitManager(sched *FeeSchedule, vip int, collapseRatio float64) *ExitManager {
	return &ExitManager{sched: sched, vip: vip, collapseRatio: collapseRatio}
}

// BreakEvenCost 返回盈亏平衡成本 = 往返手续费 + 滑点预估。
func (e *ExitManager) BreakEvenCost(pos *Position) float64 {
	return e.sched.RoundTripFee(e.vip, pos.Notional) + pos.SlippageEst
}

// AboveBreakEven 判断当前浮盈是否已安全覆盖双向手续费 + 滑点。
func (e *ExitManager) AboveBreakEven(pos *Position) bool {
	return pos.FloatingPnL > e.BreakEvenCost(pos)
}

// EnergyCollapsed 判断波场能量是否发生结构性崩溃（En 较峰值大幅衰减）。
func (e *ExitManager) EnergyCollapsed(energy, peak float64) bool {
	return energy < peak*e.collapseRatio
}

// ShouldExit 决策：动能结构性崩溃 且 浮盈覆盖手续费 才平仓锁利。
func (e *ExitManager) ShouldExit(pos *Position, energy, peak float64) bool {
	return e.EnergyCollapsed(energy, peak) && e.AboveBreakEven(pos)
}