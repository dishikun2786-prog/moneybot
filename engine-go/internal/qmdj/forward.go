package qmdj

import "time"

// ForwardTracker 前瞻收益回填器。
// 交易事件入队后，按 5m/15m/1h 到期用真实价格回填 ForwardReturns，供 IC 计算。
type ForwardTracker struct {
	pending  []*pending
	priceFn  func(symbol string) float64
	horizons []struct {
		name string
		dur  time.Duration
	}
}

type pending struct {
	rec        ShadowAuditRecord
	entryPrice float64
	done       map[string]bool
}

// NewForwardTracker 构造回填器。priceFn 返回当前价格。
func NewForwardTracker(priceFn func(symbol string) float64) *ForwardTracker {
	return &ForwardTracker{
		priceFn: priceFn,
		horizons: []struct {
			name string
			dur  time.Duration
		}{
			{"5m", 5 * time.Minute},
			{"15m", 15 * time.Minute},
			{"1h", time.Hour},
		},
	}
}

// Add 入队一个待回填事件（entryPrice 为开仓价）。
func (f *ForwardTracker) Add(rec ShadowAuditRecord, entryPrice float64) {
	if rec.ForwardReturns == nil {
		rec.ForwardReturns = map[string]float64{}
	}
	f.pending = append(f.pending, &pending{rec: rec, entryPrice: entryPrice, done: map[string]bool{}})
}

// Tick 检查到期事件，回填前瞻收益。返回已全部回填完成的记录。
func (f *ForwardTracker) Tick(now time.Time) []ShadowAuditRecord {
	var out []ShadowAuditRecord
	var kept []*pending
	for _, p := range f.pending {
		elapsed := now.Sub(time.UnixMilli(p.rec.Timestamp))
		for _, h := range f.horizons {
			if p.done[h.name] {
				continue
			}
			if elapsed >= h.dur {
				if cur := f.priceFn(p.rec.Symbol); cur > 0 {
					p.rec.ForwardReturns[h.name] = (cur - p.entryPrice) / p.entryPrice
				}
				p.done[h.name] = true
			}
		}
		if len(p.done) == len(f.horizons) {
			out = append(out, p.rec)
		} else {
			kept = append(kept, p)
		}
	}
	f.pending = kept
	return out
}

// PendingCount 返回待回填事件数。
func (f *ForwardTracker) PendingCount() int { return len(f.pending) }
