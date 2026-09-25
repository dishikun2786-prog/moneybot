package liquidation

import (
	"testing"
	"time"

	"github.com/moneybot/aetherhft/internal/fdtd"
)

func TestHandleInjects(t *testing.T) {
	g := fdtd.New(64, 1.0, 0.01, 0.5)
	inj := NewInjector(g, func(p float64) int { return int(p) }, 1.0)
	c := &Client{injector: inj}
	msg := []byte(`{"topic":"liquidation.BTCUSDT","type":"snapshot","data":[` +
		`{"price":"10","side":"Buy","size":"5","updatedTime":1700000000000},` +
		`{"price":"12","side":"Sell","size":"3","updatedTime":1700000000001}]}`)
	c.handle(msg)
	if g.F[10] != 5 {
		t.Fatalf("F[10] 应=+5, got %v", g.F[10])
	}
	if g.F[12] != -3 {
		t.Fatalf("F[12] 应=-3, got %v", g.F[12])
	}
}

func TestHandleIgnoresNonLiquidation(t *testing.T) {
	g := fdtd.New(64, 1.0, 0.01, 0.5)
	inj := NewInjector(g, func(p float64) int { return int(p) }, 1.0)
	c := &Client{injector: inj}
	c.handle([]byte(`{"topic":"tickers.BTCUSDT","data":[]}`))
	if g.F[10] != 0 {
		t.Fatalf("非强平消息不应注入")
	}
}

func TestNextBackoff(t *testing.T) {
	cases := []struct{ cur, max, want time.Duration }{
		{time.Second, 30 * time.Second, 2 * time.Second},
		{16 * time.Second, 30 * time.Second, 30 * time.Second}, // 16*2>30 → 封顶
		{30 * time.Second, 30 * time.Second, 30 * time.Second},
	}
	for _, c := range cases {
		if got := nextBackoff(c.cur, c.max); got != c.want {
			t.Fatalf("nextBackoff(%v,%v)=%v, want %v", c.cur, c.max, got, c.want)
		}
	}
}