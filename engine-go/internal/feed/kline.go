package feed

import (
	"encoding/json"
	"strconv"
	"sync"
)

// Kline 是一根 K 线末根（字段与 orderbook.json 的 kline_snap 对齐）。
type Kline struct {
	T  int64   `json:"t"`
	O  float64 `json:"o"`
	H  float64 `json:"h"`
	L  float64 `json:"l"`
	C  float64 `json:"c"`
	V  float64 `json:"v"`
	Cf bool    `json:"cf"`
}

// klineMsg 是 Bybit v5 kline.* 消息外层。
type klineMsg struct {
	Topic string `json:"topic"`
	Data  []struct {
		Start     int64  `json:"start"`
		Open      string `json:"open"`
		High      string `json:"high"`
		Low       string `json:"low"`
		Close     string `json:"close"`
		Volume    string `json:"volume"`
		Confirm   bool   `json:"confirm"`
	} `json:"data"`
}

// KlineHub 维护每标的每周期末根 K 线（线程安全）。
type KlineHub struct {
	mu sync.RWMutex
	kv map[string]map[string]Kline // sym -> interval -> 末根
}

func NewKlineHub() *KlineHub {
	return &KlineHub{kv: map[string]map[string]Kline{}}
}

// Apply 解析 kline.* 消息并更新末根（取 start 最大根）。
func (h *KlineHub) Apply(msg []byte) {
	var m klineMsg
	if json.Unmarshal(msg, &m) != nil || len(m.Data) == 0 {
		return
	}
	// topic: kline.<interval>.<symbol>
	parts := splitTopic(m.Topic)
	if len(parts) != 3 || parts[0] != "kline" {
		return
	}
	iv, sym := parts[1], parts[2]
	// 取最新一根
	var best *klineMsg
	var bestStart int64
	for i := range m.Data {
		if m.Data[i].Start >= bestStart {
			bestStart = m.Data[i].Start
			best = &klineMsg{Data: m.Data[i : i+1]}
		}
	}
	if best == nil {
		return
	}
	d := best.Data[0]
	k := Kline{T: d.Start, Cf: d.Confirm}
	if f, err := strconv.ParseFloat(d.Open, 64); err == nil {
		k.O = f
	}
	if f, err := strconv.ParseFloat(d.High, 64); err == nil {
		k.H = f
	}
	if f, err := strconv.ParseFloat(d.Low, 64); err == nil {
		k.L = f
	}
	if f, err := strconv.ParseFloat(d.Close, 64); err == nil {
		k.C = f
	}
	if f, err := strconv.ParseFloat(d.Volume, 64); err == nil {
		k.V = f
	}
	h.mu.Lock()
	if h.kv[sym] == nil {
		h.kv[sym] = map[string]Kline{}
	}
	h.kv[sym][iv] = k
	h.mu.Unlock()
}

// Snapshot 返回全部标的全周期末根（深拷贝）。
func (h *KlineHub) Snapshot() map[string]map[string]Kline {
	h.mu.RLock()
	defer h.mu.RUnlock()
	out := make(map[string]map[string]Kline, len(h.kv))
	for sym, per := range h.kv {
		cp := make(map[string]Kline, len(per))
		for iv, k := range per {
			cp[iv] = k
		}
		out[sym] = cp
	}
	return out
}

func splitTopic(t string) []string {
	var out []string
	start := 0
	for i := 0; i <= len(t); i++ {
		if i == len(t) || t[i] == '.' {
			out = append(out, t[start:i])
			start = i + 1
		}
	}
	return out
}
