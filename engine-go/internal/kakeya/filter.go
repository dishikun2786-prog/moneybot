package kakeya

import (
	"sync"
	"time"
)

// Side 是挂单/成交方向。
type Side uint8

const (
	SideBid  Side = iota // 买挂单
	SideAsk              // 卖挂单
)

// WallEvent 是一笔挂单墙事件。
type WallEvent struct {
	TsNs   int64
	Price  float64
	Side   Side
	Size   float64
	Appear bool // true=出现, false=消失
}

// TradeHit 是一笔成交（taker 方向）。taker=Buy 吃卖挂单，taker=Sell 吃买挂单。
type TradeHit struct {
	TsNs  int64
	Price float64
	Side  Side
	Size  float64
}

type wallKey struct {
	price float64
	side  Side
}

type wallState struct {
	size     float64
	appearTs int64
	consumed float64 // 被成交吃掉的部分（真实进攻）
}

type tube struct {
	ts    time.Time
	gen   float64 // 真实(被吃)量
	total float64 // 总量
}

// Filter 是 Kakeya 挂谷假挂单过滤器。
//
// 三维时空张量 (价格 X × 深度 Y × 时间 T) 中，假挂单是孤立的、平行的「死直管」
// （出现后快速撤销、无成交交叉），真动量是互相交织、被成交吃掉的「挂谷细管束」。
// 这里用可计算的代理度量 K_om = 滚动窗口内「被真实吃掉的墙量 / 总墙量」：
//   K_om → 1：真实流动性（墙被成交吃掉）
//   K_om → 0：假挂单（墙出现即撤，无成交）
type Filter struct {
	mu      sync.Mutex
	active  map[wallKey]*wallState
	window  time.Duration
	tubes   []tube
	genSum  float64
	totSum  float64
	nowFn   func() time.Time
}

// NewFilter 构造过滤器。window 是滚动窗口长度。
func NewFilter(window time.Duration) *Filter {
	return &Filter{
		active: map[wallKey]*wallState{},
		window: window,
		nowFn:  time.Now,
	}
}

// OnWall 处理挂单墙出现/消失。
func (f *Filter) OnWall(e WallEvent) {
	f.mu.Lock()
	defer f.mu.Unlock()
	k := wallKey{price: e.Price, side: e.Side}
	if e.Appear {
		f.active[k] = &wallState{size: e.Size, appearTs: e.TsNs}
		return
	}
	st, ok := f.active[k]
	if !ok {
		return // 无对应出现事件，忽略
	}
	delete(f.active, k)
	// 成交吃掉的算真实，剩下的算消失
	gen := st.consumed
	f.pushTube(f.nowFn(), gen, st.size)
}

// OnTrade 处理成交：同价位、对侧的活跃墙被真实吃掉。
func (f *Filter) OnTrade(t TradeHit) {
	f.mu.Lock()
	defer f.mu.Unlock()
	opp := SideAsk
	if t.Side == SideAsk {
		opp = SideBid
	}
	st, ok := f.active[wallKey{price: t.Price, side: opp}]
	if !ok {
		return
	}
	st.consumed += t.Size
	if st.consumed > st.size {
		st.consumed = st.size
	}
}

func (f *Filter) pushTube(ts time.Time, gen, total float64) {
	if total <= 0 {
		return
	}
	f.tubes = append(f.tubes, tube{ts: ts, gen: gen, total: total})
	f.genSum += gen
	f.totSum += total
	// 过期回收
	cutoff := ts.Add(-f.window)
	i := 0
	for i < len(f.tubes) && f.tubes[i].ts.Before(cutoff) {
		f.genSum -= f.tubes[i].gen
		f.totSum -= f.tubes[i].total
		i++
	}
	if i > 0 {
		f.tubes = f.tubes[i:]
	}
}

// KOm 返回当前 K_om（0~1，越高越真实）。
func (f *Filter) KOm() float64 {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.totSum <= 0 {
		return 1.0 // 无墙样本时视为无假挂单
	}
	return f.genSum / f.totSum
}

// Veto 当 K_om 低于阈值时一票否决（假挂单占主导）。
func (f *Filter) Veto(threshold float64) bool { return f.KOm() < threshold }