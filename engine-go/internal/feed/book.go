package feed

import (
	"sort"
	"strconv"
)

// Book 是单标的 L2 盘口状态：定长有序数组（bids 降序 / asks 升序）。
// 逐消息 delta 用手写二分查找 + 原地插入/删除，零堆分配。
// 价格用 float64（由交易所字符串解析），对订单簿档位精度足够。
type Book struct {
	Symbol [16]byte
	bids   [MaxBookLevels]Level
	asks   [MaxBookLevels]Level
	bidN   int
	askN   int
	u      uint64
	snap   bool
	gaps   int
}

func NewBook(sym [16]byte) *Book { return &Book{Symbol: sym} }

// Snapshot 全量重建（快照路径，非逐消息热路径，允许 sort）。
func (b *Book) Snapshot(bids, asks [][]string, u uint64) {
	b.bidN = 0
	for _, p := range bids {
		if len(p) == 2 {
			pr, e1 := strconv.ParseFloat(p[0], 64)
			sz, e2 := strconv.ParseFloat(p[1], 64)
			if e1 == nil && e2 == nil && sz > 0 && b.bidN < MaxBookLevels {
				b.bids[b.bidN] = Level{Price: pr, Size: sz}
				b.bidN++
			}
		}
	}
	sort.Slice(b.bids[:b.bidN], func(i, j int) bool { return b.bids[i].Price > b.bids[j].Price })

	b.askN = 0
	for _, p := range asks {
		if len(p) == 2 {
			pr, e1 := strconv.ParseFloat(p[0], 64)
			sz, e2 := strconv.ParseFloat(p[1], 64)
			if e1 == nil && e2 == nil && sz > 0 && b.askN < MaxBookLevels {
				b.asks[b.askN] = Level{Price: pr, Size: sz}
				b.askN++
			}
		}
	}
	sort.Slice(b.asks[:b.askN], func(i, j int) bool { return b.asks[i].Price < b.asks[j].Price })

	b.u = u
	b.snap = true
	b.gaps = 0
}

// reset 清空盘口（snapshot 重建前调用）。
func (b *Book) reset() {
	b.bidN = 0
	b.askN = 0
	b.snap = false
	b.gaps = 0
}

// completeSnapshot 标记快照重建完成。
func (b *Book) completeSnapshot(u uint64) {
	b.u = u
	b.snap = true
	b.gaps = 0
}

// Delta 校验序列连续性：仅当 u == 上一 u+1 时接受增量，否则缺口（需重订阅）。
func (b *Book) Delta(u uint64) bool {
	if !b.snap || u != b.u+1 {
		if b.snap {
			b.snap = false
			b.gaps++
		}
		return false
	}
	b.u = u
	return true
}

// ApplyBid 应用买侧增量（size==0 删除，否则插入/更新）。零分配。
func (b *Book) ApplyBid(price, size float64) {
	b.bidN = applyLevel(b.bids[:], b.bidN, price, size, true)
}

// ApplyAsk 应用卖侧增量。零分配。
func (b *Book) ApplyAsk(price, size float64) {
	b.askN = applyLevel(b.asks[:], b.askN, price, size, false)
}

// applyLevel 手写二分 + 原地插入/删除，返回新的档位数。零分配（无闭包、无反射）。
func applyLevel(levels []Level, n int, price, size float64, desc bool) int {
	i := lowerBound(levels, n, price, desc)
	if i < n && levels[i].Price == price {
		if size == 0 {
			copy(levels[i:], levels[i+1:n])
			return n - 1
		}
		levels[i].Size = size
		return n
	}
	if size == 0 {
		return n
	}
	if n < MaxBookLevels {
		copy(levels[i+1:], levels[i:n])
		levels[i] = Level{Price: price, Size: size}
		return n + 1
	}
	return n // 超出深度上限，丢弃远端
}

// lowerBound 手写二分（无闭包）：返回 price 应插入的位置。
func lowerBound(levels []Level, n int, price float64, desc bool) int {
	lo, hi := 0, n
	for lo < hi {
		mid := (lo + hi) / 2
		if (desc && levels[mid].Price > price) || (!desc && levels[mid].Price < price) {
			lo = mid + 1
		} else {
			hi = mid
		}
	}
	return lo
}

// TopLevels 返回前 k 档（快照输出，非热路径）。
func (b *Book) TopLevels(k int) (bids, asks []Level) {
	nb, na := k, k
	if nb > b.bidN {
		nb = b.bidN
	}
	if na > b.askN {
		na = b.askN
	}
	bids = b.bids[:nb]
	asks = b.asks[:na]
	return
}

// Ready 表示已收到快照且当前无序列缺口。
func (b *Book) Ready() bool { return b.snap }