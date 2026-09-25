package resonance

import "sync"

// Context 是单 tick 多标的共振决策上下文。
// 切片头会随标的数增长而逃逸 → 用 sync.Pool 复用，热路径零分配。
type Context struct {
	Macros []Macro
	Micros []Micro
	Decs   []Decision
}

var ctxPool = sync.Pool{New: func() any { return &Context{} }}

// AcquireContext 取一个容量 ≥ n 的上下文（复用底层数组）。
func AcquireContext(n int) *Context {
	c := ctxPool.Get().(*Context)
	if cap(c.Macros) < n {
		c.Macros = make([]Macro, n)
		c.Micros = make([]Micro, n)
		c.Decs = make([]Decision, n)
	} else {
		c.Macros = c.Macros[:n]
		c.Micros = c.Micros[:n]
		c.Decs = c.Decs[:n]
	}
	// 清零（复用旧数据）
	for i := range c.Macros {
		c.Macros[i] = Macro{}
	}
	for i := range c.Micros {
		c.Micros[i] = Micro{}
	}
	for i := range c.Decs {
		c.Decs[i] = Decision{}
	}
	return c
}

// ReleaseContext 归还上下文（清零切片，保留底层数组）。
func ReleaseContext(c *Context) {
	if c == nil {
		return
	}
	c.Macros = c.Macros[:0]
	c.Micros = c.Micros[:0]
	c.Decs = c.Decs[:0]
	ctxPool.Put(c)
}
