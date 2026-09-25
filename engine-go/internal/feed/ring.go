package feed

import (
	"runtime"
	"sync/atomic"
)

// Ring 是有界单生产者单消费者（SPSC）无锁环形缓冲。
//
// 并发安全模型：
//   - head 仅由生产者写、消费者只读（atomic.Load）
//   - tail 仅由消费者写、生产者只读（atomic.Load）
//   - 无锁、无条件变量；背压通过 runtime.Gosched 让出 CPU
//
// 零分配：New 之后，Push/Pop 只整体按值拷贝固定大小的 BookUpdate 结构
// （约 104 字节：定长 Symbol + 3 个切片头），底层 Level 数组由对象池复用。
type Ring struct {
	buf  []BookUpdate
	mask uint64
	head atomic.Uint64 // 生产者写入游标
	tail atomic.Uint64 // 消费者读取游标（仅消费者写）
}

// NewRing 创建容量自动向上取 2 的幂的环形缓冲。
func NewRing(capacity int) *Ring {
	c := roundUpPow2(capacity)
	if c < 1 {
		c = 1
	}
	return &Ring{buf: make([]BookUpdate, c), mask: uint64(c - 1)}
}

func roundUpPow2(n int) int {
	if n <= 1 {
		return 1
	}
	n--
	n |= n >> 1
	n |= n >> 2
	n |= n >> 4
	n |= n >> 8
	n |= n >> 16
	return n + 1
}

// Cap 返回实际容量（2 的幂）。
func (r *Ring) Cap() int { return len(r.buf) }

// Len 返回当前积压帧数。
func (r *Ring) Len() uint64 {
	return r.head.Load() - r.tail.Load()
}

// TryPush 非阻塞写入一帧，缓冲满返回 false（此时 u 的切片所有权仍归调用方）。
func (r *Ring) TryPush(u BookUpdate) bool {
	h := r.head.Load()
	t := r.tail.Load()
	if h-t >= uint64(len(r.buf)) {
		return false
	}
	r.buf[h&r.mask] = u
	r.head.Store(h + 1)
	return true
}

// Push 阻塞式写入：缓冲满时让出 CPU（有界背压，避免无上限排队）。
func (r *Ring) Push(u BookUpdate) {
	for !r.TryPush(u) {
		runtime.Gosched()
	}
}

// TryPop 非阻塞读出一帧，空时 ok=false。
func (r *Ring) TryPop() (BookUpdate, bool) {
	t := r.tail.Load()
	if t == r.head.Load() {
		return BookUpdate{}, false
	}
	u := r.buf[t&r.mask]
	r.tail.Store(t + 1)
	return u, true
}

// Pop 阻塞式读出：空时让出 CPU。
func (r *Ring) Pop() (BookUpdate, bool) {
	for {
		if u, ok := r.TryPop(); ok {
			return u, true
		}
		runtime.Gosched()
	}
}