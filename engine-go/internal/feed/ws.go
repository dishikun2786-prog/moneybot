package feed

import (
	"bytes"
	"encoding/json"
	"strconv"
	"sync"

	"github.com/gorilla/websocket"
)

// tradeMsg 是 Bybit v5 publicTrade 消息（低频，保留 encoding/json）。
type tradeMsg struct {
	Topic string `json:"topic"`
	Ts    int64  `json:"ts"`
	Data  []struct {
		S    string `json:"s"`
		Side string `json:"S"`
		P    string `json:"p"`
		V    string `json:"v"`
		T    int64  `json:"T"`
	} `json:"data"`
}

// Trade 是一笔归一化逐笔成交。
type Trade struct {
	TsNs int64
	Side string
	P    float64
	V    float64
}

// BookHub 管理所有标的盘口 + 订阅/重订阅。
// 盘口热路径用手写零分配解析（parse.go）；成交低频路径保留 encoding/json。
type BookHub struct {
	mu     sync.Mutex
	books  map[[16]byte]*Book
	conn   *websocket.Conn
	Trades map[string][]Trade
}

func NewBookHub(conn *websocket.Conn) *BookHub {
	return &BookHub{
		books:  map[[16]byte]*Book{},
		conn:   conn,
		Trades: map[string][]Trade{},
	}
}

// Subscribe 订阅 orderbook.200 与 publicTrade，按每消息最多 10 topic 分批。
func (h *BookHub) Subscribe(syms []string) error {
	var args []string
	for _, s := range syms {
		args = append(args, "orderbook.200."+s, "publicTrade."+s)
	}
	for i := 0; i < len(args); i += 10 {
		end := i + 10
		if end > len(args) {
			end = len(args)
		}
		if err := h.conn.WriteJSON(map[string]any{"op": "subscribe", "args": args[i:end]}); err != nil {
			return err
		}
	}
	return nil
}

// resubscribe 单标的重订阅（缺口修复）。
func (h *BookHub) resubscribe(sym string) {
	_ = h.conn.WriteJSON(map[string]any{
		"op": "subscribe", "args": []string{"orderbook.200." + sym}})
}

// Handle 解析并分发一条原始消息。
func (h *BookHub) Handle(msg []byte) {
	switch {
	case bytes.Contains(msg, kTopicOB):
		sym, ok := parseSymbol(msg)
		if !ok {
			return
		}
		h.mu.Lock()
		b := h.books[sym]
		if b == nil {
			b = NewBook(sym)
			h.books[sym] = b
		}
		h.mu.Unlock()
		if _, ok := parseBook(msg, b); !ok {
			h.resubscribe(SymString(sym))
		}
	case bytes.Contains(msg, kTopicTrade):
		var tm tradeMsg
		if json.Unmarshal(msg, &tm) != nil {
			return
		}
		h.handleTrades(tm)
	}
}

// maxTrades 与 Python TRADES deque(maxlen=30) 对齐，防 SSE 帧无限膨胀。
const maxTrades = 30

func (h *BookHub) handleTrades(m tradeMsg) {
	h.mu.Lock()
	defer h.mu.Unlock()
	for _, t := range m.Data {
		v, err := strconv.ParseFloat(t.V, 64)
		if err != nil || v <= 0 {
			continue
		}
		p, _ := strconv.ParseFloat(t.P, 64)
		q := h.Trades[t.S]
		q = append(q, Trade{TsNs: t.T * 1e6, Side: t.Side, P: p, V: v})
		if len(q) > maxTrades {
			q = q[len(q)-maxTrades:]
		}
		h.Trades[t.S] = q
	}
}

// SnapshotTrades 返回逐笔成交快照（深拷贝，线程安全）。
func (h *BookHub) SnapshotTrades() map[string][]Trade {
	h.mu.Lock()
	defer h.mu.Unlock()
	out := make(map[string][]Trade, len(h.Trades))
	for k, v := range h.Trades {
		out[k] = append([]Trade(nil), v...)
	}
	return out
}

// Snapshot 返回所有就绪标的的 top-N 盘口（1s 刷新，非热路径）。
func (h *BookHub) Snapshot(n int) []BookView {
	h.mu.Lock()
	defer h.mu.Unlock()
	out := make([]BookView, 0, len(h.books))
	for sym, b := range h.books {
		if !b.Ready() {
			continue
		}
		bids, asks := b.TopLevels(n)
		out = append(out, BookView{Symbol: sym, Bids: bids, Asks: asks})
	}
	return out
}

// BookView 是单标的 top-N 盘口视图。
type BookView struct {
	Symbol [16]byte
	Bids   []Level // 降序
	Asks   []Level // 升序
}