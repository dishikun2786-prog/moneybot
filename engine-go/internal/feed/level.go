package feed

// MaxBookLevels 是 L2 盘口每侧最大档位数（Bybit orderbook.200 = 200 档）。
const MaxBookLevels = 200

// Level 是一档（价格 + 量）。
type Level struct {
	Price float64
	Size  float64
}

// BookUpdate 是一帧流经环形缓冲的盘口快照。
// 用定长数组而非切片：BookUpdate 是纯值类型，入环/出环只是固定大小的 memcpy，
// 切片头不再逃逸 → 零堆分配。字段总计约 6.4KB，环形缓冲容量按此设计。
type BookUpdate struct {
	Symbol [16]byte
	Seq    uint64
	TsNs   int64
	Bids   [MaxBookLevels]Level
	Asks   [MaxBookLevels]Level
	BidSz  int
	AskSz  int
}
// SymString 把 [16]byte 标的转成 string（仅用于低频路径：重订阅/快照刷新，允许分配）。
func SymString(s [16]byte) string {
	n := 0
	for n < 16 && s[n] != 0 {
		n++
	}
	return string(s[:n])
}