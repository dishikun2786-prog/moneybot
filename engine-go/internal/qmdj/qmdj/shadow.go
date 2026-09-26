package qmdj

import (
	"encoding/json"
	"os"
	"sync"
)

// ShadowAuditRecord 是影子审计记录（奇门遁甲时空因子影子验证）。
// 不干预实际交易决策，平行异步记录每笔交易事件的时空打分与后续真实盈亏。
type ShadowAuditRecord struct {
	EventID              string             `json:"event_id"`
	Timestamp            int64              `json:"timestamp"`
	Symbol               string             `json:"symbol"`
	BaseStrategySignal   string             `json:"base_signal"` // long / short
	QMDJScore            float64            `json:"qmdj_score"`  // 0~1
	PatternType          string             `json:"pattern_type"` // 吉 / 凶 / 平
	RealizedPnL          float64            `json:"realized_pnl"`
	SimulatedPnLWithQMDJ float64            `json:"simulated_pnl_with_qmdj"`
	ForwardReturns       map[string]float64 `json:"forward_returns"` // 5m/15m/1h
}

// Logger 是非阻塞影子日志记录器（热路径之外，channel 异步落盘）。
// channel 满时丢弃记录（影子验证绝不阻塞主决策链），并计数 dropped。
type Logger struct {
	ch      chan ShadowAuditRecord
	file    *os.File
	mu      sync.Mutex
	dropped uint64
	closed  chan struct{}
	once    sync.Once
}

// NewLogger 构造影子日志器，异步写 jsonl。buf 为 channel 缓冲大小。
func NewLogger(path string, buf int) (*Logger, error) {
	if buf < 1 {
		buf = 1024
	}
	f, err := os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
	if err != nil {
		return nil, err
	}
	l := &Logger{ch: make(chan ShadowAuditRecord, buf), file: f, closed: make(chan struct{})}
	go l.loop()
	return l, nil
}

// Log 非阻塞记录一条影子审计（channel 满则丢弃，不阻塞）。
func (l *Logger) Log(r ShadowAuditRecord) {
	select {
	case l.ch <- r:
	default:
		l.mu.Lock()
		l.dropped++
		l.mu.Unlock()
	}
}

// Dropped 返回被丢弃的记录数（观测埋点）。
func (l *Logger) Dropped() uint64 {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.dropped
}

// Close 停止日志器并落盘。
func (l *Logger) Close() {
	l.once.Do(func() {
		close(l.closed)
		<-l.ch // 等消费完（简化）
		l.file.Close()
	})
}

func (l *Logger) loop() {
	for {
		select {
		case r := <-l.ch:
			b, err := json.Marshal(r)
			if err == nil {
				l.file.Write(append(b, '\n'))
			}
		case <-l.closed:
			return
		}
	}
}
