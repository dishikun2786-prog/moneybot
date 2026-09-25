package feed

import (
	"strconv"
	"unsafe"
)

// 预声明字段名/常量字节切片，避免运行时 []byte("...") 分配。
var (
	kTopicOB    = []byte("orderbook.")
	kTopicTrade = []byte("publicTrade.")
	kType       = []byte("type")
	kData       = []byte("data")
	kS          = []byte("s")
	kB          = []byte("b")
	kA          = []byte("a")
	kU          = []byte("u")
	kSnap       = []byte("snapshot")
	kDelta      = []byte("delta")
)

// b2s 零拷贝 []byte → string（只读；调用方保证 b 在解析期间不被改写）。
// 复用 strconv.ParseFloat/ParseUint 的正确 IEEE754/十进制解析，避免手写浮点。
func b2s(b []byte) string {
	if len(b) == 0 {
		return ""
	}
	return unsafe.String(unsafe.SliceData(b), len(b))
}

func parseFloatBytes(b []byte) (float64, bool) {
	f, err := strconv.ParseFloat(b2s(b), 64)
	return f, err == nil
}

func parseUintBytes(b []byte) (uint64, bool) {
	u, err := strconv.ParseUint(b2s(b), 10, 64)
	return u, err == nil
}

func stringEq(a, b []byte) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

// scanner 轻量 JSON 字节扫描器（仅面向 Bybit v5 盘口消息的扁平结构）。
type scanner struct {
	b []byte
	i int
}

func (s *scanner) skipWS() {
	for s.i < len(s.b) {
		c := s.b[s.i]
		if c != ' ' && c != '\t' && c != '\r' && c != '\n' {
			return
		}
		s.i++
	}
}

// readStringBytes 读取当前 '"' 开头的字符串内容（不含引号），并越过闭合引号。
func (s *scanner) readStringBytes() ([]byte, bool) {
	if s.i >= len(s.b) || s.b[s.i] != '"' {
		return nil, false
	}
	s.i++
	start := s.i
	for s.i < len(s.b) {
		switch s.b[s.i] {
		case '\\':
			s.i += 2
		case '"':
			v := s.b[start:s.i]
			s.i++
			return v, true
		default:
			s.i++
		}
	}
	return nil, false
}

func (s *scanner) skipString() { _, _ = s.readStringBytes() }

// skipValue 跳过任意 JSON 值（字符串/数字/数组/对象/字面量）。
func (s *scanner) skipValue() {
	s.skipWS()
	if s.i >= len(s.b) {
		return
	}
	switch s.b[s.i] {
	case '"':
		s.skipString()
	case '[':
		s.skipArray()
	case '{':
		s.skipObject()
	default:
		for s.i < len(s.b) {
			c := s.b[s.i]
			if c == ',' || c == '}' || c == ']' || c == ' ' || c == '\t' || c == '\r' || c == '\n' {
				return
			}
			s.i++
		}
	}
}

func (s *scanner) skipArray() {
	s.i++ // [
	for {
		s.skipWS()
		if s.i >= len(s.b) {
			return
		}
		if s.b[s.i] == ']' {
			s.i++
			return
		}
		s.skipValue()
		s.skipWS()
		if s.i < len(s.b) && s.b[s.i] == ',' {
			s.i++
		}
	}
}

func (s *scanner) skipObject() {
	s.i++ // {
	for {
		s.skipWS()
		if s.i >= len(s.b) {
			return
		}
		if s.b[s.i] == '}' {
			s.i++
			return
		}
		s.skipString() // key
		s.skipWS()
		if s.i < len(s.b) && s.b[s.i] == ':' {
			s.i++
		}
		s.skipValue()
		s.skipWS()
		if s.i < len(s.b) && s.b[s.i] == ',' {
			s.i++
		}
	}
}

// findKey 在当前对象层内查找 "key":，成功则把指针移到值起始处。
func (s *scanner) findKey(key []byte) bool {
	for s.i < len(s.b) {
		c := s.b[s.i]
		if c == '}' {
			return false
		}
		if c != '"' {
			s.i++
			continue
		}
		v, ok := s.readStringBytes()
		if !ok {
			return false
		}
		s.skipWS()
		if s.i < len(s.b) && s.b[s.i] == ':' {
			s.i++
			s.skipWS()
			if stringEq(v, key) {
				return true
			}
			s.skipValue()
		}
	}
	return false
}

// readFloat 读取一个 float64 值（带引号的字符串或裸数字）。
func (s *scanner) readFloat() (float64, bool) {
	s.skipWS()
	if s.i < len(s.b) && s.b[s.i] == '"' {
		v, ok := s.readStringBytes()
		if !ok {
			return 0, false
		}
		return parseFloatBytes(v)
	}
	start := s.i
	for s.i < len(s.b) {
		c := s.b[s.i]
		if (c >= '0' && c <= '9') || c == '-' || c == '+' || c == '.' || c == 'e' || c == 'E' {
			s.i++
		} else {
			break
		}
	}
	return parseFloatBytes(s.b[start:s.i])
}

// readUint 读取一个 uint64 值（带引号的字符串或裸数字）。
func (s *scanner) readUint() (uint64, bool) {
	s.skipWS()
	if s.i < len(s.b) && s.b[s.i] == '"' {
		v, ok := s.readStringBytes()
		if !ok {
			return 0, false
		}
		return parseUintBytes(v)
	}
	start := s.i
	for s.i < len(s.b) {
		c := s.b[s.i]
		if c >= '0' && c <= '9' {
			s.i++
		} else {
			break
		}
	}
	return parseUintBytes(s.b[start:s.i])
}

// parseSymbol 快速扫描消息取标的符号（零分配，返回 [16]byte）。
func parseSymbol(msg []byte) (sym [16]byte, ok bool) {
	s := scanner{b: msg}
	if !s.findKey(kData) {
		return sym, false
	}
	s.skipWS()
	if s.i >= len(s.b) || s.b[s.i] != '{' {
		return sym, false
	}
	s.i++
	if !s.findKey(kS) {
		return sym, false
	}
	v, ok := s.readStringBytes()
	if !ok {
		return sym, false
	}
	copy(sym[:], v)
	return sym, true
}

// parseBook 解析 Bybit orderbook 消息并应用到 b（零分配）。
// 返回 isSnapshot；序列缺口或解析失败返回 ok=false。
func parseBook(msg []byte, b *Book) (isSnapshot bool, ok bool) {
	s := scanner{b: msg}

	if !s.findKey(kType) {
		return false, false
	}
	typ, ok := s.readStringBytes()
	if !ok {
		return false, false
	}
	switch {
	case stringEq(typ, kSnap):
		isSnapshot = true
	case stringEq(typ, kDelta):
		isSnapshot = false
	default:
		return false, false
	}

	// 进入 data 对象并记住起始（多遍扫描，避开字段顺序依赖）
	if !s.findKey(kData) {
		return false, false
	}
	s.skipWS()
	if s.i >= len(s.b) || s.b[s.i] != '{' {
		return false, false
	}
	s.i++
	dataStart := s.i

	// delta：先读 u 校验序列，再应用增量（避免把增量打到陈旧盘口上）
	if !isSnapshot {
		s.i = dataStart
		if !s.findKey(kU) {
			return false, false
		}
		u, ok := s.readUint()
		if !ok || !b.Delta(u) {
			return false, false
		}
	} else {
		b.reset()
	}

	// 解析 b
	s.i = dataStart
	if !s.findKey(kB) {
		return false, false
	}
	if !s.readLevels(b, true) {
		return false, false
	}

	// 解析 a
	s.i = dataStart
	if !s.findKey(kA) {
		return false, false
	}
	if !s.readLevels(b, false) {
		return false, false
	}

	// snapshot：最后读 u 完成重建
	if isSnapshot {
		s.i = dataStart
		if !s.findKey(kU) {
			return false, false
		}
		u, ok := s.readUint()
		if !ok {
			return false, false
		}
		b.completeSnapshot(u)
	}
	return isSnapshot, true
}

// readLevels 解析 [[price,size],...] 数组并应用到 b 的某一侧。
func (s *scanner) readLevels(b *Book, isBid bool) bool {
	s.skipWS()
	if s.i >= len(s.b) || s.b[s.i] != '[' {
		return false
	}
	s.i++ // [
	for {
		s.skipWS()
		if s.i >= len(s.b) {
			return false
		}
		if s.b[s.i] == ']' {
			s.i++
			return true
		}
		if s.b[s.i] != '[' {
			return false
		}
		s.i++ // [
		price, ok1 := s.readFloat()
		s.skipWS()
		if s.i < len(s.b) && s.b[s.i] == ',' {
			s.i++
		}
		size, ok2 := s.readFloat()
		if !ok1 || !ok2 {
			return false
		}
		if isBid {
			b.ApplyBid(price, size)
		} else {
			b.ApplyAsk(price, size)
		}
		s.skipWS()
		if s.i < len(s.b) && s.b[s.i] == ']' {
			s.i++
		}
		s.skipWS()
		if s.i < len(s.b) && s.b[s.i] == ',' {
			s.i++
		}
	}
}