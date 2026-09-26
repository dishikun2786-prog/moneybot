package trend

import "math"

// Position 是持仓状态（锁利输入）。
type Position struct {
	Entry     float64 // 入场价
	Side      string  // long / short
	Notional  float64 // 名义金额
	BreakEven float64 // 盈亏平衡成本（往返手续费+滑点，金额）
	PeakPrice float64 // 持仓期最有利价（多头=最高价，空头=最低价）
}

// ExitDecision 是锁利退出决策。
type ExitDecision struct {
	Action     string  // hold / partial / full / stop
	CloseRatio float64 // 平仓比例 0~1
	StopPrice  float64 // 当前止损价
	ProfitATR  float64 // 当前浮盈（ATR 倍数）
}

// LockProfit 是智能锁利管理器（ATR 自适应止损 + 保本移动 + 阶梯止盈）。
type LockProfit struct {
	kStop  float64 // 止损 ATR 倍数（默认 1.5）
	r1, r2 float64 // 阶梯锁利比例（1×ATR 平 r1，2×ATR 平 r2）
}

// NewLockProfit 构造锁利管理器。
func NewLockProfit() *LockProfit {
	return &LockProfit{kStop: 1.5, r1: 0.30, r2: 0.40}
}

// Update 每 tick 计算退出决策。
// price=现价, high/low=当前 K线高低, atr=当前 ATR, floatingPnl=当前浮盈金额。
func (l *LockProfit) Update(pos *Position, price, high, low, atr, floatingPnl float64) ExitDecision {
	d := ExitDecision{Action: "hold", StopPrice: pos.Entry}
	if atr <= 0 {
		return d
	}
	// 浮盈（ATR 倍数，以价格计）
	var profitATR float64
	var favorable float64
	if pos.Side == "long" {
		favorable = math.Max(pos.PeakPrice, high)
		profitATR = (price - pos.Entry) / atr
		d.StopPrice = favorable - l.kStop*atr // 移动止损
	} else {
		favorable = math.Min(pos.PeakPrice, low)
		profitATR = (pos.Entry - price) / atr
		d.StopPrice = favorable + l.kStop*atr
	}
	// 更新 peak
	if pos.Side == "long" && favorable > pos.PeakPrice {
		pos.PeakPrice = favorable
	}
	if pos.Side == "short" && favorable < pos.PeakPrice {
		pos.PeakPrice = favorable
	}

	// 1. 止损检查
	if (pos.Side == "long" && price <= d.StopPrice) || (pos.Side == "short" && price >= d.StopPrice) {
		d.Action = "stop"
		d.CloseRatio = 1
		return d
	}
	// 2. 保本移动：浮盈覆盖手续费后，止损不低于入场价
	if floatingPnl > pos.BreakEven {
		if pos.Side == "long" && d.StopPrice < pos.Entry {
			d.StopPrice = pos.Entry
		}
		if pos.Side == "short" && d.StopPrice > pos.Entry {
			d.StopPrice = pos.Entry
		}
	}
	// 3. 阶梯锁利
	if profitATR >= 3.0 {
		d.Action = "full"
		d.CloseRatio = 1
	} else if profitATR >= 2.0 {
		d.Action = "partial"
		d.CloseRatio = l.r2
	} else if profitATR >= 1.0 {
		d.Action = "partial"
		d.CloseRatio = l.r1
	}
	d.ProfitATR = profitATR
	return d
}
