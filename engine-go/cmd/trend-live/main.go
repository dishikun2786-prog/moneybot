// trend-live: 把 internal/trend 串入真实数据流。
// 读 go_orderbook.json(盘口/funding/OI/trades)，每分钟对 7 标的做趋势评分 + 择优选标，
// 输出评分/选择/权重，并落盘 jsonl 供回测。
package main

import (
	"encoding/json"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"time"

	"github.com/moneybot/aetherhft/internal/trend"
)

var catOf = map[string]trend.Category{
	"BTCUSDT": trend.LargeCap, "ETHUSDT": trend.LargeCap,
	"XAUUSDT": trend.Precious, "XAGUSDT": trend.Precious,
	"SOLUSDT": trend.Altcoin, "NEARUSDT": trend.Altcoin, "XRPUSDT": trend.Altcoin,
}

// obJSON 是 go_orderbook.json 的解析结构。
type obJSON struct {
	Books map[string]struct {
		Bids [][]json.RawMessage `json:"bids"`
		Asks [][]json.RawMessage `json:"asks"`
	} `json:"books"`
	Px map[string]struct {
		Last    float64 `json:"last"`
		Funding float64 `json:"funding"`
		OI      float64 `json:"oi"`
	} `json:"px"`
	Trades map[string][]struct {
		S string  `json:"S"`
		V float64 `json:"v"`
	} `json:"trades"`
}

// parseLevel 解析 [[price,size],...] 单档。
func parseLevel(lv []json.RawMessage) (price, size float64) {
	if len(lv) < 2 {
		return 0, 0
	}
	var ps string
	_ = json.Unmarshal(lv[0], &ps)
	price, _ = strconv.ParseFloat(ps, 64)
	_ = json.Unmarshal(lv[1], &size)
	return price, size
}

// computeInput 从 orderbook 计算单标的 TrendInput。
func computeInput(sym string, ob *obJSON) trend.TrendInput {
	in := trend.TrendInput{}
	if p, ok := ob.Px[sym]; ok {
		in.Price = p.Last
		in.Funding = p.Funding
		in.OI = p.OI
	}
	if b, ok := ob.Books[sym]; ok {
		var bb, ba, b5, a5 float64
		for i, lv := range b.Bids {
			p, s := parseLevel(lv)
			if i == 0 {
				bb = p
			}
			if i < 5 {
				b5 += s
			}
		}
		for i, lv := range b.Asks {
			p, s := parseLevel(lv)
			if i == 0 {
				ba = p
			}
			if i < 5 {
				a5 += s
			}
		}
		mid := (bb + ba) / 2
		if mid > 0 {
			in.SpreadBP = (ba - bb) / mid * 10000
		}
		if tot := b5 + a5; tot > 0 {
			in.Imb = (b5 - a5) / tot
		}
	}
	// CVD：近 1 分钟主动量（买-卖）
	if ts, ok := ob.Trades[sym]; ok {
		for _, t := range ts {
			if t.S == "Buy" {
				in.CVD += t.V
			} else {
				in.CVD -= t.V
			}
		}
	}
	return in
}

func main() {
	path := os.Getenv("MB_DEPTH_OUT")
	if path == "" {
		path = filepath.Join(mustHome(), "polymarket", "logs", "go_orderbook.json")
	}
	out := os.Getenv("MB_TREND_OUT")
	if out == "" {
		out = filepath.Join(mustHome(), "polymarket", "logs", "trend_scan.jsonl")
	}

	analyzers := map[string]*trend.Analyzer{}
	for sym := range catOf {
		analyzers[sym] = trend.NewAnalyzer()
	}
	sel := trend.NewSelector()

	log.Printf("trend-live: 读 %s, 落盘 %s", path, out)

	lastMin := ""
	tick := time.NewTicker(2 * time.Second)
	defer tick.Stop()
	for range tick.C {
		raw, err := os.ReadFile(path)
		if err != nil {
			continue
		}
		var ob obJSON
		if json.Unmarshal(raw, &ob) != nil {
			continue
		}
		now := time.Now()
		min := now.Format("15:04")
		if min == lastMin {
			continue // 每分钟评分一次
		}
		lastMin = min

		// 7 标的同步评分
		var syms []trend.Symbol
		for sym, cat := range catOf {
			in := computeInput(sym, &ob)
			st := analyzers[sym].Analyze(sym, in)
			syms = append(syms, trend.Symbol{Name: sym, Cat: cat, State: st})
		}
		sort.Slice(syms, func(i, j int) bool { return syms[i].State.Score > syms[j].State.Score })

		// 择优
		actions := sel.Select(syms)

		// 输出
		ts := now.Format("2006-01-02T15:04:05Z")
		fmt.Printf("[%s] 7标的评分: ", ts)
		for _, s := range syms {
			fmt.Printf("%s=%s/%.2f ", s.Name, s.State.Dir, s.State.Score)
		}
		fmt.Println()
		var opened []string
		for _, a := range actions {
			if a.Type == "open" {
				opened = append(opened, a.Symbol)
			}
		}
		fmt.Printf("  择优: %v | 权重: ", opened)
		for name, w := range sel.Weights() {
			fmt.Printf("%s=%.0f%% ", name, w*100)
		}
		fmt.Println()

		// 落盘 jsonl
		rec := map[string]any{
			"ts": ts, "scores": symsToMap(syms), "selected": opened, "weights": sel.Weights(),
		}
		if b, err := json.Marshal(rec); err == nil {
			f, _ := os.OpenFile(out, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
			if f != nil {
				f.Write(append(b, '\n'))
				f.Close()
			}
		}
	}
}

func symsToMap(syms []trend.Symbol) map[string]any {
	out := map[string]any{}
	for _, s := range syms {
		out[s.Name] = map[string]any{
			"dir": s.State.Dir, "score": s.State.Score, "r": s.State.RScore,
			"strength": s.State.Strength, "macro": s.State.Macro, "micro": s.State.Micro,
		}
	}
	return out
}

func mustHome() string {
	h, err := os.UserHomeDir()
	if err != nil {
		return "/home/ubuntu"
	}
	return h
}
