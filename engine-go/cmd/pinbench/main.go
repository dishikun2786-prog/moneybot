// CPU 亲和性延迟抖动对比基准：同一热路径负载分别「未绑定」与「绑定核」各跑 N tick，
// 输出 p50/p90/p99/p99.9/max 与抖动(p99-p50)。
package main

import (
	"fmt"
	"sort"
	"time"

	"github.com/moneybot/aetherhft/internal/fdtd"
	"github.com/moneybot/aetherhft/internal/pin"
)

const (
	ticks    = 20000 // 每轮 tick 数
	workload = 100   // 每 tick 执行的 FDTD 步数
)

func makeGrid() *fdtd.Grid {
	g := fdtd.New(1024, 1.0, 0.01, 0.5)
	c := make([]float64, 1024)
	for i := range c {
		c[i] = 0.9
	}
	g.SetWaveSpeed(c)
	return g
}

func run(label string, cpu int, pinned bool) {
	g := makeGrid()
	lats := make([]int64, 0, ticks)
	var unlock func()
	if pinned {
		var err error
		unlock, err = pin.PinToCPU(cpu)
		if err != nil {
			fmt.Println("  绑定失败:", err)
			unlock = func() {}
		} else {
			fmt.Printf("  [已绑定到 CPU%d]\n", cpu)
		}
	} else {
		unlock = func() {}
	}
	defer unlock()

	for i := 0; i < ticks; i++ {
		t0 := time.Now()
		for j := 0; j < workload; j++ {
			g.Step()
		}
		lats = append(lats, time.Since(t0).Nanoseconds())
	}
	sort.Slice(lats, func(i, j int) bool { return lats[i] < lats[j] })
	n := len(lats)
	pct := func(p float64) float64 {
		return float64(lats[int(float64(n-1)*p)]) / 1e3 // μs
	}
	p99 := pct(0.99)
	p50 := pct(0.50)
	fmt.Printf("  %s: p50=%.1fμs p90=%.1fμs p99=%.1fμs p99.9=%.1fμs max=%.1fμs  抖动(p99-p50)=%.1fμs\n",
		label, p50, pct(0.90), p99, pct(0.999), float64(lats[n-1])/1e3, p99-p50)
}

func main() {
	fmt.Printf("=== CPU 亲和性延迟抖动对比 (每 tick=%d 步 FDTD N=1024, %d ticks) ===\n", workload, ticks)
	fmt.Println("未绑定:")
	run("未绑定", 0, false)
	fmt.Println("绑定核:")
	run("绑定核0", 0, true)
	run("绑定核1", 1, true)
}