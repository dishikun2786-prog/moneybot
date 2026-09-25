package risk

import "sync"

// SignalPool 是开仓信号的 sync.Pool（热路径零分配范例）。
//
// 热路径纪律：订单结构体/信号结构体在循环中反复创建会触发逃逸与堆分配，
// 破坏 GC 停顿上限。用 sync.Pool 复用同一块内存：
//
//	sig := pool.Get()
//	defer pool.Put(sig)
//	... 填充字段, 调用 Allow ...
//
// 结构体按值返回/传入时由编译器栈分配；需要长期存活(入队列/跨协程)时才从池取。
type SignalPool struct {
	p sync.Pool
}

// NewSignalPool 构造信号池。
func NewSignalPool() *SignalPool {
	return &SignalPool{p: sync.Pool{New: func() any { return new(Signal) }}}
}

// Get 取一个零值 Signal（复用）。
func (sp *SignalPool) Get() *Signal { return sp.p.Get().(*Signal) }

// Put 归还 Signal（复用）。
func (sp *SignalPool) Put(s *Signal) { sp.p.Put(s) }

// Order 是 DEV 网关订单（池化对象范例）。
type Order struct {
	Symbol [16]byte
	Side   uint8
	Price  float64
	Size   float64
	Qty    float64
}

// OrderPool 是订单结构体池。
type OrderPool struct {
	p sync.Pool
}

// NewOrderPool 构造订单池。
func NewOrderPool() *OrderPool {
	return &OrderPool{p: sync.Pool{New: func() any { return new(Order) }}}
}

// Get 取一个零值 Order。
func (op *OrderPool) Get() *Order { return op.p.Get().(*Order) }

// Put 归还 Order。
func (op *OrderPool) Put(o *Order) { op.p.Put(o) }