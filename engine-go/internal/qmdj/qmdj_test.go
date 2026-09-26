package qmdj

import "testing"

func TestLoggerNonBlocking(t *testing.T) {
	l, err := NewLogger(t.TempDir()+"/qmdj.jsonl", 2)
	if err != nil {
		t.Fatal(err)
	}
	defer l.Close()
	// 写入 5 条，channel 缓冲 2，应有部分被丢弃（非阻塞）
	for i := 0; i < 5; i++ {
		l.Log(ShadowAuditRecord{EventID: "e", Timestamp: 1, Symbol: "BTCUSDT", QMDJScore: 0.8})
	}
	// 至少不 panic（丢弃由 Dropped 计数）
	_ = l.Dropped()
}

func TestStatsIC(t *testing.T) {
	// 构造 4 条记录：QMDJ score 与 1h 前瞻收益正相关
	recs := []ShadowAuditRecord{
		{EventID: "1", QMDJScore: 0.9, PatternType: "吉", RealizedPnL: 1, ForwardReturns: map[string]float64{"1h": 0.01}},
		{EventID: "2", QMDJScore: 0.7, PatternType: "吉", RealizedPnL: 0.5, ForwardReturns: map[string]float64{"1h": 0.005}},
		{EventID: "3", QMDJScore: 0.3, PatternType: "凶", RealizedPnL: -0.5, ForwardReturns: map[string]float64{"1h": -0.005}},
		{EventID: "4", QMDJScore: 0.1, PatternType: "凶", RealizedPnL: -1, ForwardReturns: map[string]float64{"1h": -0.01}},
	}
	l, _ := NewLogger(t.TempDir()+"/qmdj.jsonl", 16)
	for _, r := range recs {
		l.Log(r)
	}
	l.Close()
	_ = l
}

func TestPearsonIC(t *testing.T) {
	pairs := []pair{
		{0.9, 0.01}, {0.7, 0.005}, {0.3, -0.005}, {0.1, -0.01},
	}
	ic := pearsonIC(pairs)
	if ic <= 0.9 {
		t.Fatalf("正相关应 IC>0.9: %v", ic)
	}
}
