package ipc

import (
	"encoding/binary"
	"math"
)

// 共享内存快照布局（Go 生产者 ↔ Python 消费者，小端、语言无关）：
//
//	Header (64B):
//	  [0:8]   magic "MBBOOK01"
//	  [8:12]  version uint32 = 1
//	  [12:16] reserved
//	  [16:24] seq  uint64 (单调递增)
//	  [24:32] tsNs uint64
//	  [32:36] nSyms uint32
//	  [36:64] reserved
//	SymSlots (16 × 48B):
//	  [0:16] symbol[16]byte
//	  [16:20] bidStart uint32 (levels 索引)
//	  [20:24] bidCount uint32
//	  [24:28] askStart uint32
//	  [28:32] askCount uint32
//	  [32:48] reserved
//	Levels (6400 × 16B): price float64 LE, size float64 LE
const (
	Magic            = "MBBOOK01"
	Version          = 1
	MaxSyms          = 16
	MaxLevelsPerSide = 200
	MaxLevels        = MaxSyms * 2 * MaxLevelsPerSide // 6400

	HeaderSize  = 64
	SymSlotSize = 48
	LevelSize   = 16
	TotalSize   = HeaderSize + MaxSyms*SymSlotSize + MaxLevels*LevelSize
)

// Level 是一档（价格 + 量）。
type Level struct {
	Price float64
	Size  float64
}

// SymView 是单标的 top-N 盘口视图。
type SymView struct {
	Symbol string
	Bids   []Level // 降序
	Asks   []Level // 升序
}

// Shm 是快照写入器。data 由平台相关 Open 提供（Linux=mmap，Windows=内存缓冲）。
type Shm struct {
	data []byte
	seq  uint64
}

// Write 写一帧快照。symbols 每个 Bids/Asks 各 ≤ MaxLevelsPerSide 档，否则截断。
func (s *Shm) Write(symbols []SymView, tsNs int64) {
	buf := s.data
	for i := range buf {
		buf[i] = 0
	}
	copy(buf[0:8], Magic)
	binary.LittleEndian.PutUint32(buf[8:12], Version)
	binary.LittleEndian.PutUint64(buf[16:24], s.seq)
	binary.LittleEndian.PutUint64(buf[24:32], uint64(tsNs))

	n := len(symbols)
	if n > MaxSyms {
		n = MaxSyms
	}
	binary.LittleEndian.PutUint32(buf[32:36], uint32(n))

	levelBase := HeaderSize + MaxSyms*SymSlotSize
	levelIdx := 0
	for i := 0; i < n; i++ {
		v := symbols[i]
		slot := HeaderSize + i*SymSlotSize
		sym := padSym(v.Symbol)
		copy(buf[slot:slot+16], sym[:])

		bidStart := levelIdx
		for _, l := range v.Bids {
			if levelIdx-bidStart >= MaxLevelsPerSide || levelIdx >= MaxLevels {
				break
			}
			putLevel(buf, levelBase+levelIdx*LevelSize, l)
			levelIdx++
		}
		bidCount := levelIdx - bidStart

		askStart := levelIdx
		for _, l := range v.Asks {
			if levelIdx-askStart >= MaxLevelsPerSide || levelIdx >= MaxLevels {
				break
			}
			putLevel(buf, levelBase+levelIdx*LevelSize, l)
			levelIdx++
		}
		askCount := levelIdx - askStart

		binary.LittleEndian.PutUint32(buf[slot+16:slot+20], uint32(bidStart))
		binary.LittleEndian.PutUint32(buf[slot+20:slot+24], uint32(bidCount))
		binary.LittleEndian.PutUint32(buf[slot+24:slot+28], uint32(askStart))
		binary.LittleEndian.PutUint32(buf[slot+28:slot+32], uint32(askCount))
	}
	s.seq++
}

// Seq 返回已写入帧数（观测用）。
func (s *Shm) Seq() uint64 { return s.seq }

func padSym(s string) [16]byte {
	var out [16]byte
	copy(out[:], s)
	return out
}

func putLevel(buf []byte, off int, l Level) {
	binary.LittleEndian.PutUint64(buf[off:off+8], math.Float64bits(l.Price))
	binary.LittleEndian.PutUint64(buf[off+8:off+16], math.Float64bits(l.Size))
}