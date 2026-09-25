package latency

import (
	"fmt"
	"sort"
	"strings"
	"time"
)

// Timer 是全链路决策延迟评估器（分阶段埋点，零堆分配热路径可用）。
// 用法：t := NewTimer(); ... t.Mark("FDTD"); ... t.Mark("Kakeya") ...
type Timer struct {
	start  time.Time
	last   time.Time
	stages []Stage
}

// Stage 是一阶段耗时记录。
type Stage struct {
	Name string
	Dur  time.Duration
}

// NewTimer 启动计时器（记录总起点）。
func NewTimer() *Timer {
	now := time.Now()
	return &Timer{start: now, last: now}
}

// Mark 记录从上次 Mark（或 Start）到现在的耗时，并开启下一阶段。
func (t *Timer) Mark(name string) time.Duration {
	now := time.Now()
	d := now.Sub(t.last)
	t.stages = append(t.stages, Stage{Name: name, Dur: d})
	t.last = now
	return d
}

// Total 返回从 Start 到当前的总耗时。
func (t *Timer) Total() time.Duration { return time.Since(t.start) }

// TotalAt 返回从 Start 到当前的总耗时（Mark 后调用，用 last 时间戳）。
func (t *Timer) TotalAt() time.Duration { return t.last.Sub(t.start) }

// Stages 返回阶段列表。
func (t *Timer) Stages() []Stage { return t.stages }

// WithinBudget 判断总耗时是否在预算内。
func (t *Timer) WithinBudget(budget time.Duration) bool { return t.Total() <= budget }

// Report 格式化输出分阶段延迟 + 总计 + 硬限判断。

// Stats 是多次决策延迟的分布统计（实时延迟评估：Min/Max/Avg/P50/P95/P99）。
type Stats struct {
	n     int
	total time.Duration
	max   time.Duration
	min   time.Duration
	samps []time.Duration
}

// NewStats 构造延迟统计器。
func NewStats() *Stats { return &Stats{min: 1 << 62} }

// Add 记录一次端到端延迟。
func (s *Stats) Add(d time.Duration) {
	s.n++
	s.total += d
	if d > s.max {
		s.max = d
	}
	if d < s.min {
		s.min = d
	}
	s.samps = append(s.samps, d)
}

// Count 返回样本数。
func (s *Stats) Count() int { return s.n }

// Min 最小延迟。
func (s *Stats) Min() time.Duration { return s.min }

// Max 最大延迟。
func (s *Stats) Max() time.Duration { return s.max }

// Avg 平均延迟。
func (s *Stats) Avg() time.Duration {
	if s.n == 0 {
		return 0
	}
	return s.total / time.Duration(s.n)
}

// Percentile 返回分位延迟（0~1）。
func (s *Stats) Percentile(q float64) time.Duration {
	if s.n == 0 {
		return 0
	}
	c := make([]time.Duration, len(s.samps))
	copy(c, s.samps)
	sort.Slice(c, func(i, j int) bool { return c[i] < c[j] })
	i := int(float64(len(c)-1) * q)
	if i < 0 {
		i = 0
	}
	return c[i]
}

// String 格式化延迟分布（实时评估摘要）。
func (s *Stats) String() string {
	if s.n == 0 {
		return "无样本"
	}
	return fmt.Sprintf("n=%d min=%v avg=%v p50=%v p95=%v p99=%v max=%v",
		s.n, s.Min(), s.Avg(), s.Percentile(0.50), s.Percentile(0.95),
		s.Percentile(0.99), s.Max())
}

func (t *Timer) Report(budget time.Duration) string {
	var b strings.Builder
	for _, s := range t.stages {
		fmt.Fprintf(&b, "    %-14s %10v\n", s.Name, s.Dur)
	}
	fmt.Fprintf(&b, "    %-14s %10v", "TOTAL", t.Total())
	if budget > 0 {
		if t.Total() <= budget {
			fmt.Fprintf(&b, "  ✓ ≤ %v", budget)
		} else {
			fmt.Fprintf(&b, "  ✗ > %v (超限)", budget)
		}
	}
	return b.String()
}
