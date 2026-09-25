package sandbox

import (
	"math"
	"sync"
	"time"
)

// Side 是订单/成交方向。
type Side uint8

const (
	SideBuy  Side = iota // 买
	SideSell             // 卖
)

// Status 是订单状态机。
type Status uint8

const (
	StatusPendingQueue Status = iota // 排队中
	StatusFilled                     // 已成交
	StatusCanceled                   // 已撤单
)

func (s Status) String() string {
	switch s {
	case StatusPendingQueue:
		return "PENDING_QUEUE"
	case StatusFilled:
		return "FILLED"
	case StatusCanceled:
		return "CANCELED"
	default:
		return "UNKNOWN"
	}
}

// Order 是一笔限价/市价单。
// Price 对限价单是挂单价，对市价单是触发参考价（用于算滑点）。
type Order struct {
	ID           string
	Symbol       [16]byte
	Side         Side
	Price        float64
	Size         float64
	Qahead       float64 // 下单瞬间前方排队量（FIFO 优先权）
	Filled       float64 // 已成交量
	FillAvgPrice float64 // 成交均价（VWAP，由逐笔成交累计）
	Status       Status
	TsQueue      int64
	TsFill       int64
}

// TradeTick 是一笔逐笔成交（taker 方向）。
type TradeTick struct {
	Symbol [16]byte
	Side   Side // taker 主动方向：Buy 吃 ask，Sell 吃 bid
	Price  float64
	Size   float64
	TsNs   int64
}

// levelKey 定位 (标的, taker方向, 价格) 档位的累计 taker 量。
type levelKey struct {
	sym   [16]byte
	side  Side
	price float64
}

// Sandbox 是 FIFO 微观队列撮合沙盒。
// 限价单先吃光前方排队量 Qahead 才成交；逐笔成交按 VWAP 累计真实成交均价。
type Sandbox struct {
	mu     sync.Mutex
	orders map[string]*Order
	cum    map[levelKey]float64
}

func NewSandbox() *Sandbox {
	return &Sandbox{orders: map[string]*Order{}, cum: map[levelKey]float64{}}
}

// Place 把限价单送入排队（PENDING_QUEUE），记录前方排队量 qahead。
func (s *Sandbox) Place(o *Order, qahead float64) {
	s.mu.Lock()
	defer s.mu.Unlock()
	o.Qahead = qahead
	o.Status = StatusPendingQueue
	o.Filled = 0
	o.FillAvgPrice = 0
	o.TsQueue = time.Now().UnixNano()
	s.orders[o.ID] = o
}

// OnTrade 处理一笔逐笔成交：累计同向 taker 量，按 VWAP 逐步填充对侧限价单。
// 前方排队量未吃光前不成交；吃光后按 (prev, after] 成交窗口增量计 VWAP。
func (s *Sandbox) OnTrade(t TradeTick) {
	s.mu.Lock()
	defer s.mu.Unlock()
	k := levelKey{sym: t.Symbol, side: t.Side, price: t.Price}
	prev := s.cum[k]
	s.cum[k] += t.Size
	after := s.cum[k]
	for _, o := range s.orders {
		if o.Status != StatusPendingQueue || o.Symbol != t.Symbol || o.Price != t.Price || o.Side == t.Side {
			continue
		}
		// 该订单成交窗口 [Qahead, Qahead+Size) 与 (prev, after] 的交集
		fillStart := math.Max(prev, o.Qahead)
		fillEnd := math.Min(after, o.Qahead+o.Size)
		if fillEnd <= fillStart {
			continue
		}
		fill := fillEnd - fillStart
		o.FillAvgPrice = (o.FillAvgPrice*o.Filled + fill*t.Price) / (o.Filled + fill)
		o.Filled += fill
		if o.Filled >= o.Size-1e-12 {
			o.Status = StatusFilled
			o.TsFill = t.TsNs
		}
	}
}

// MarketLevel 是一档可供市价单吃掉的流动性（调用方按成交优先级排序）。
type MarketLevel struct {
	Price float64
	Size  float64
}

// PlaceMarket 市价单扫单成交：side=Buy 吃卖单(升序)，side=Sell 吃买单(降序)。
// 返回 (成交均价, 成交量, 是否成交)。流动性不足则部分成交。
func (s *Sandbox) PlaceMarket(o *Order, book []MarketLevel) (float64, float64, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	remaining := o.Size
	var notional float64
	for _, lv := range book {
		if remaining <= 0 {
			break
		}
		take := math.Min(remaining, lv.Size)
		notional += take * lv.Price
		remaining -= take
	}
	filled := o.Size - remaining
	if filled <= 0 {
		return 0, 0, false
	}
	o.FillAvgPrice = notional / filled
	o.Filled = filled
	o.Status = StatusFilled
	o.TsQueue = time.Now().UnixNano()
	o.TsFill = o.TsQueue
	s.orders[o.ID] = o
	return o.FillAvgPrice, filled, true
}

// Cancel 撤单：仅 PENDING_QUEUE 可撤。
func (s *Sandbox) Cancel(id string) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	o, ok := s.orders[id]
	if !ok || o.Status != StatusPendingQueue {
		return false
	}
	o.Status = StatusCanceled
	return true
}

// Get 返回订单快照。
func (s *Sandbox) Get(id string) (Order, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	o, ok := s.orders[id]
	if !ok {
		return Order{}, false
	}
	return *o, true
}

// Latency 返回订单从排队到成交的延迟（纳秒）。
func (o *Order) Latency() int64 {
	if o.Status != StatusFilled {
		return 0
	}
	return o.TsFill - o.TsQueue
}

// Slippage 返回真实滑点：成交均价 vs 参考价（买正卖负）。
func (o *Order) Slippage() float64 {
	if o.Side == SideBuy {
		return o.FillAvgPrice - o.Price
	}
	return o.Price - o.FillAvgPrice
}