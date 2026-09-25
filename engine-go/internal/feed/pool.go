package feed

import "sync"

// Pools 是热路径订单簿切片的对象池。
// 预热容量 = 交易所 L2 深度上限（如 200/1000 档），稳态运行期间 Get/Put 不发生堆分配。
type Pools struct {
	bids sync.Pool
	asks sync.Pool
}

// NewPools 创建池，levelCap 为预分配容量。
func NewPools(levelCap int) *Pools {
	p := &Pools{}
	p.bids.New = func() any { return make([]Level, 0, levelCap) }
	p.asks.New = func() any { return make([]Level, 0, levelCap) }
	return p
}

// GetBids 返回零长度、已预分配容量的买侧切片。
func (p *Pools) GetBids() []Level { return p.bids.Get().([]Level)[:0] }

// PutBids 归还买侧切片（截断到零长度但保留底层数组）。
func (p *Pools) PutBids(s []Level) { p.bids.Put(s[:0]) }

// GetAsks 返回零长度、已预分配容量的卖侧切片。
func (p *Pools) GetAsks() []Level { return p.asks.Get().([]Level)[:0] }

// PutAsks 归还卖侧切片。
func (p *Pools) PutAsks(s []Level) { p.asks.Put(s[:0]) }