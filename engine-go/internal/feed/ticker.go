package feed

import (
	"encoding/json"
	"strconv"
	"sync"
)

// Ticker 是单标的 ticker 快照，字段与 bybit_ws_bridge.py 的 PRICES 完全对齐，
// 保证 Go 网关写出的 bybit_prices.json 对 Python/前端零差异。
type Ticker struct {
	Last        float64 `json:"last"`
	ChangePct   float64 `json:"change_pct"`
	High        float64 `json:"high"`
	Low         float64 `json:"low"`
	Vol         float64 `json:"vol"`
	Ts          int64   `json:"ts"`
	Qv          float64 `json:"qv"`
	OI          float64 `json:"oi"`
	OIVal       float64 `json:"oi_val"`
	FundingRate float64 `json:"funding_rate"`
	NextFunding int64   `json:"next_funding"`
	Spot        float64 `json:"spot,omitempty"`
}

// tickerData 是 Bybit v5 tickers.* 消息的 data 段（字段为字符串，空值可能为 "" 或 null）。
type tickerData struct {
	Symbol           string `json:"symbol"`
	LastPrice        string `json:"lastPrice"`
	Price24hPcnt     string `json:"price24hPcnt"`
	HighPrice24h     string `json:"highPrice24h"`
	LowPrice24h      string `json:"lowPrice24h"`
	Turnover24h      string `json:"turnover24h"`
	Volume24h        string `json:"volume24h"`
	OpenInterest     string `json:"openInterest"`
	OpenInterestVal  string `json:"openInterestValue"`
	FundingRate      string `json:"fundingRate"`
	NextFundingTime  string `json:"nextFundingTime"`
}

// tickerMsg 是 tickers.* 消息外层。
type tickerMsg struct {
	Topic string     `json:"topic"`
	Ts    int64      `json:"ts"`
	Data  tickerData `json:"data"`
}

func parseFloatStr(s string) (float64, bool) {
	if s == "" {
		return 0, false
	}
	f, err := strconv.ParseFloat(s, 64)
	return f, err == nil
}

func parseIntStr(s string) (int64, bool) {
	if s == "" {
		return 0, false
	}
	n, err := strconv.ParseInt(s, 10, 64)
	return n, err == nil
}

// ParseTicker 解析 tickers.* 消息，返回 symbol 与字段更新集（只含有值字段）。
// isSpot 指示该消息来自现货通道（写 spot 价）。
func ParseTicker(msg []byte, isSpot bool) (sym string, upd map[string]float64, ts int64, ok bool) {
	var m tickerMsg
	if json.Unmarshal(msg, &m) != nil || m.Data.Symbol == "" {
		return "", nil, 0, false
	}
	sym = m.Data.Symbol
	ts = m.Ts
	upd = map[string]float64{}
	if isSpot {
		if v, ok := parseFloatStr(m.Data.LastPrice); ok {
			upd["spot"] = v
		}
		return sym, upd, ts, true
	}
	if v, ok := parseFloatStr(m.Data.LastPrice); ok {
		upd["last"] = v
	}
	if v, ok := parseFloatStr(m.Data.Price24hPcnt); ok {
		upd["change_pct"] = v * 100
	}
	if v, ok := parseFloatStr(m.Data.HighPrice24h); ok {
		upd["high"] = v
	}
	if v, ok := parseFloatStr(m.Data.LowPrice24h); ok {
		upd["low"] = v
	}
	if v, ok := parseFloatStr(m.Data.Turnover24h); ok {
		upd["vol"] = v
	}
	if v, ok := parseFloatStr(m.Data.Volume24h); ok {
		upd["qv"] = v
	}
	if v, ok := parseFloatStr(m.Data.OpenInterest); ok {
		upd["oi"] = v
	}
	if v, ok := parseFloatStr(m.Data.OpenInterestVal); ok {
		upd["oi_val"] = v
	}
	if v, ok := parseFloatStr(m.Data.FundingRate); ok {
		upd["funding_rate"] = v
	}
	if v, ok := parseIntStr(m.Data.NextFundingTime); ok {
		upd["next_funding"] = float64(v)
	}
	return sym, upd, ts, true
}

// PriceHub 维护所有标的 ticker 快照（线程安全），产出与 Python 一致的 bybit_prices.json。
type PriceHub struct {
	mu      sync.RWMutex
	prices  map[string]*Ticker
	lagMs   int64
	updated chan struct{} // 容量1，非阻塞广播（SSE 订阅方监听）
}

func NewPriceHub() *PriceHub {
	return &PriceHub{prices: map[string]*Ticker{}, updated: make(chan struct{}, 1)}
}

// Updated 返回广播通道，每次 Apply 后收到一次通知（事件驱动，非轮询）。
func (h *PriceHub) Updated() <-chan struct{} { return h.updated }

func (h *PriceHub) notify() {
	select {
	case h.updated <- struct{}{}:
	default:
	}
}

// Apply 应用一帧 ticker 更新（只更新有值字段，对齐 Python 的 update 语义）。
func (h *PriceHub) Apply(sym string, upd map[string]float64, msgTs int64, nowMs int64) {
	h.mu.Lock()
	defer h.mu.Unlock()
	t := h.prices[sym]
	if t == nil {
		t = &Ticker{}
		h.prices[sym] = t
	}
	for k, v := range upd {
		switch k {
		case "last":
			t.Last = v
		case "spot":
			t.Spot = v
		case "change_pct":
			t.ChangePct = v
		case "high":
			t.High = v
		case "low":
			t.Low = v
		case "vol":
			t.Vol = v
		case "qv":
			t.Qv = v
		case "oi":
			t.OI = v
		case "oi_val":
			t.OIVal = v
		case "funding_rate":
			t.FundingRate = v
		case "next_funding":
			t.NextFunding = int64(v)
		}
	}
	if msgTs > 0 {
		t.Ts = msgTs
		h.lagMs = nowMs - msgTs
	}
	h.notify()
}

// Snapshot 返回 bybit_prices.json 的完整内容（深拷贝，避免竞态）。
func (h *PriceHub) Snapshot() (tsMs int64, lagMs int64, prices map[string]*Ticker) {
	h.mu.RLock()
	defer h.mu.RUnlock()
	prices = make(map[string]*Ticker, len(h.prices))
	for k, v := range h.prices {
		c := *v
		prices[k] = &c
	}
	lagMs = h.lagMs
	return
}

// Len 返回已就绪标的数量。
func (h *PriceHub) Len() int {
	h.mu.RLock()
	defer h.mu.RUnlock()
	return len(h.prices)
}
