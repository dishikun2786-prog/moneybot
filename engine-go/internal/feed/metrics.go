package feed

import "sync/atomic"

// Counter 是无锁、零分配的自增计数器（Prometheus Counter 的原子后端）。
type Counter struct{ v uint64 }

// Inc 原子自增 1。
func (c *Counter) Inc() { atomic.AddUint64(&c.v, 1) }

// Add 原子加 delta。
func (c *Counter) Add(delta uint64) { atomic.AddUint64(&c.v, delta) }

// Load 原子读取当前值。
func (c *Counter) Load() uint64 { return atomic.LoadUint64(&c.v) }

// Metrics 是数据热路径埋点。
// 生产环境将 Load() 暴露为 Prometheus Counter（例：prometheus.NewCounterFunc）。
type Metrics struct {
	Pushed  Counter // 成功入环帧数
	Dropped Counter // 背压/解析失败丢弃帧数
	Parsed  Counter // 成功解析消息数
}