package qmdj

import (
	"testing"
	"time"
)

func TestGatewayCompute(t *testing.T) {
	// 用系统 python 调用排盘服务（若脚本路径不可用则跳过）
	g := NewGateway("python3", "/home/ubuntu/aetherhft/python/qmdj_service.py")
	ctx, err := g.Compute(1790399255000)
	if err != nil {
		t.Skipf("排盘服务不可用(可能未部署): %v", err)
	}
	if ctx.Score < 0 || ctx.Score > 1 {
		t.Fatalf("score 应在 0~1: %v", ctx.Score)
	}
	if ctx.SolarTerm == "" {
		t.Fatalf("节气不应为空")
	}
	if ctx.Ganzhi["year"] == "" {
		t.Fatalf("年干支不应为空")
	}
}

func TestForwardTracker(t *testing.T) {
	prices := map[string]float64{"BTCUSDT": 100}
	ft := NewForwardTracker(func(s string) float64 { return prices[s] })
	ft.Add(ShadowAuditRecord{EventID: "1", Timestamp: 1000, Symbol: "BTCUSDT"}, 100)
	// 5m 后价格涨到 105
	prices["BTCUSDT"] = 105
	out := ft.Tick(timeUnix(1000 + 5*60*1000))
	// 5m 到期但 15m/1h 未到期，不应完成
	if len(out) != 0 {
		t.Fatalf("5m 不应完成: %v", out)
	}
	// 1h 后
	prices["BTCUSDT"] = 110
	out = ft.Tick(timeUnix(1000 + 60*60*1000))
	if len(out) != 1 {
		t.Fatalf("1h 后应完成: %v", len(out))
	}
	if out[0].ForwardReturns["1h"] != 0.1 {
		t.Fatalf("1h 收益应 10%%: %v", out[0].ForwardReturns["1h"])
	}
}

func TestWeightManager(t *testing.T) {
	w := NewWeightManager(0.2, 0.1)
	// 未开启：权重 0
	if w.Weight() != 0 {
		t.Fatalf("未开启应 0: %v", w.Weight())
	}
	w.Enable()
	w.Advance()
	if w.Weight() != 0.1 {
		t.Fatalf("第一步应 0.1: %v", w.Weight())
	}
	w.Advance()
	w.Advance()
	if w.Weight() != 0.2 {
		t.Fatalf("应到目标 0.2: %v", w.Weight())
	}
	// Blended 融合
	b := w.Blended(0.5, 0.9)
	if b <= 0.5 {
		t.Fatalf("高 qmdj 应提升信号: %v", b)
	}
}

func TestSignificant(t *testing.T) {
	s := &Stats{TotalEvents: 600, IC: 0.08}
	if !s.Significant(500, 0.05) {
		t.Fatalf("应显著")
	}
	s2 := &Stats{TotalEvents: 100, IC: 0.08}
	if s2.Significant(500, 0.05) {
		t.Fatalf("事件不足不应显著")
	}
	s3 := &Stats{TotalEvents: 600, IC: 0.02}
	if s3.Significant(500, 0.05) {
		t.Fatalf("IC 太低不应显著")
	}
}

func timeUnix(ms int64) time.Time { return time.UnixMilli(ms) }
