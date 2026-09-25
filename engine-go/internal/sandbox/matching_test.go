package sandbox

import (
	"math"
	"testing"
	"time"
)

func sym(s string) [16]byte { var o [16]byte; copy(o[:], s); return o }

func TestQueueAheadBlocksFill(t *testing.T) {
	s := NewSandbox()
	btc := sym("BTCUSDT")
	o := &Order{ID: "o1", Symbol: btc, Side: SideBuy, Price: 50000, Size: 1}
	s.Place(o, 100)

	// 吃 99：前方未吃完 → 排队
	s.OnTrade(TradeTick{Symbol: btc, Side: SideSell, Price: 50000, Size: 99, TsNs: 1})
	if got, _ := s.Get("o1"); got.Status != StatusPendingQueue {
		t.Fatalf("前方未吃完应仍排队, got %v", got.Status)
	}
	// 再吃 1（累计=100，正好清空前方）→ 仍未轮到我们
	s.OnTrade(TradeTick{Symbol: btc, Side: SideSell, Price: 50000, Size: 1, TsNs: 2})
	if got, _ := s.Get("o1"); got.Status != StatusPendingQueue {
		t.Fatalf("累计=Qahead 应仍排队(尚未轮到), got %v", got.Status)
	}
	// 再吃 1（累计=101 > 100）→ FILLED
	s.OnTrade(TradeTick{Symbol: btc, Side: SideSell, Price: 50000, Size: 1, TsNs: 3})
	got, _ := s.Get("o1")
	if got.Status != StatusFilled {
		t.Fatalf("超过前方后应成交, got %v", got.Status)
	}
	if got.FillAvgPrice != 50000 {
		t.Fatalf("成交均价应=50000, got %v", got.FillAvgPrice)
	}
}

func TestSingleTradeSweepsQueueAndFills(t *testing.T) {
	// 单笔大 taker 同时吃光前方并吃掉我们：fill 窗口应正确计算
	s := NewSandbox()
	btc := sym("BTCUSDT")
	o := &Order{ID: "o1", Symbol: btc, Side: SideBuy, Price: 50000, Size: 1}
	s.Place(o, 100)
	s.OnTrade(TradeTick{Symbol: btc, Side: SideSell, Price: 50000, Size: 150, TsNs: 1})
	got, _ := s.Get("o1")
	if got.Status != StatusFilled || got.Filled != 1 {
		t.Fatalf("单笔扫单应成交1, got status=%v filled=%v", got.Status, got.Filled)
	}
}

func TestSameSideDoesNotFill(t *testing.T) {
	s := NewSandbox()
	btc := sym("BTCUSDT")
	o := &Order{ID: "o1", Symbol: btc, Side: SideBuy, Price: 50000, Size: 1}
	s.Place(o, 100)
	s.OnTrade(TradeTick{Symbol: btc, Side: SideBuy, Price: 50000, Size: 999, TsNs: 1})
	if got, _ := s.Get("o1"); got.Status != StatusPendingQueue {
		t.Fatalf("同向成交不应填充挂单, got %v", got.Status)
	}
}

func TestCancel(t *testing.T) {
	s := NewSandbox()
	btc := sym("BTCUSDT")
	o := &Order{ID: "o1", Symbol: btc, Side: SideBuy, Price: 50000, Size: 1}
	s.Place(o, 100)
	if !s.Cancel("o1") {
		t.Fatal("应能撤单")
	}
	if got, _ := s.Get("o1"); got.Status != StatusCanceled {
		t.Fatalf("撤单后应为 CANCELED, got %v", got.Status)
	}
}

func TestLimitFillZeroSlippage(t *testing.T) {
	s := NewSandbox()
	btc := sym("BTCUSDT")
	o := &Order{ID: "o1", Symbol: btc, Side: SideBuy, Price: 50000, Size: 1}
	s.Place(o, 10)
	s.OnTrade(TradeTick{Symbol: btc, Side: SideSell, Price: 50000, Size: 11, TsNs: 1})
	got, _ := s.Get("o1")
	if got.Status != StatusFilled || math.Abs(got.Slippage()) > 1e-9 {
		t.Fatalf("限价单按挂单价成交滑点应为0, status=%v slippage=%v", got.Status, got.Slippage())
	}
}

func TestMarketOrderSlippage(t *testing.T) {
	s := NewSandbox()
	btc := sym("BTCUSDT")
	o := &Order{ID: "m1", Symbol: btc, Side: SideBuy, Price: 100, Size: 3}
	book := []MarketLevel{{Price: 100, Size: 1}, {Price: 101, Size: 5}}
	avg, filled, ok := s.PlaceMarket(o, book)
	if !ok || filled != 3 {
		t.Fatalf("市价单应成交3, ok=%v filled=%v", ok, filled)
	}
	want := (1*100.0 + 2*101.0) / 3.0
	if math.Abs(avg-want) > 1e-9 {
		t.Fatalf("成交均价应=%.6f, got %v", want, avg)
	}
	if math.Abs(o.Slippage()-(want-100)) > 1e-9 {
		t.Fatalf("滑点应=%+.6f, got %v", want-100, o.Slippage())
	}
}

func TestLatency(t *testing.T) {
	s := NewSandbox()
	btc := sym("BTCUSDT")
	o := &Order{ID: "o1", Symbol: btc, Side: SideBuy, Price: 50000, Size: 1}
	s.Place(o, 50)
	tsFill := time.Now().UnixNano() + 300
	s.OnTrade(TradeTick{Symbol: btc, Side: SideSell, Price: 50000, Size: 51, TsNs: tsFill})
	got, _ := s.Get("o1")
	if got.Status != StatusFilled {
		t.Fatalf("应已成交, got %v", got.Status)
	}
	lat := got.Latency()
	if lat < 250 || lat > 400 {
		t.Fatalf("延迟应≈300ns, got %v", lat)
	}
}