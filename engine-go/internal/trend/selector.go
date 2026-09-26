package trend

import "sort"

// Category 是标的类别（用于相关性分散的硬配额）。
type Category int

const (
	LargeCap Category = iota // 大盘 BTC/ETH
	Precious                 // 贵金属 XAU/XAG
	Altcoin                  // 山寨 SOL/NEAR/XRP
)

// Symbol 是选标输入（含类别 + 趋势状态 + 风险）。
type Symbol struct {
	Name  string
	Cat   Category
	VaR   float64 // 波动率风险（ATR×名义近似）
	State TrendState
}

// Action 是选标输出动作。
type Action struct {
	Symbol string
	Type   string // open / close / hold / swap
	Weight float64
}

// Selector 是择优选标器（排序 + 类别配额分散 + 滞回轮换）。
type Selector struct {
	quota      map[Category]int
	hysteresis float64
	minScore   float64
	exitScore  float64
	maxHeld    int
	held       map[string]float64 // 持仓标的 -> score
}

// NewSelector 构造选标器（默认配额：大盘1 贵金属1 山寨2，滞回 20%）。
func NewSelector() *Selector {
	return &Selector{
		quota:      map[Category]int{LargeCap: 1, Precious: 1, Altcoin: 2},
		hysteresis: 0.2,
		minScore:   0.5,
		exitScore:  0.4,
		maxHeld:    3,
		held:       map[string]float64{},
	}
}

// SetHeld 注入当前持仓（可选，用于轮换判断）。
func (s *Selector) SetHeld(held map[string]float64) { s.held = held }

// Held 返回当前持仓。
func (s *Selector) Held() map[string]float64 { return s.held }

// Select 对 7 标的择优选择，返回动作列表。
// 逻辑：score 降序 → 类别配额贪心 → 与持仓对比生成 open/close/hold。
func (s *Selector) Select(syms []Symbol) []Action {
	// 1. 排序
	sorted := append([]Symbol(nil), syms...)
	sort.Slice(sorted, func(i, j int) bool { return sorted[i].State.Score > sorted[j].State.Score })

	// 2. 候选池（dir != flat 且 score >= minScore）
	var candidates []Symbol
	for _, sy := range sorted {
		if sy.State.Dir != "flat" && sy.State.Score >= s.minScore {
			candidates = append(candidates, sy)
		}
	}

	// 3. 类别配额贪心选择
	used := map[Category]int{}
	var picked []Symbol
	for _, sy := range candidates {
		if used[sy.Cat] < s.quota[sy.Cat] && len(picked) < s.maxHeld {
			picked = append(picked, sy)
			used[sy.Cat]++
		}
	}

	// 4. 生成动作（滞回轮换）
	var actions []Action
	pickedSet := map[string]float64{}
	for _, p := range picked {
		pickedSet[p.Name] = p.State.Score
	}

	// close：持仓中但不在 picked（趋势衰竭，或 score < exitScore）
	for name := range s.held {
		newScore, still := pickedSet[name]
		if !still || newScore < s.exitScore {
			actions = append(actions, Action{Symbol: name, Type: "close"})
			delete(s.held, name)
		} else {
			actions = append(actions, Action{Symbol: name, Type: "hold", Weight: newScore})
		}
	}

	// open：picked 中但不在持仓，且滞回带（新 score > 持仓最低 score × 1.2 才开，避免频繁换仓）
	for name, sc := range pickedSet {
		if _, already := s.held[name]; already {
			continue
		}
		actions = append(actions, Action{Symbol: name, Type: "open", Weight: sc})
		s.held[name] = sc
	}

	return actions
}

// Weights 按 score 归一化返回选中标的的权重（供 UTA 进一步风险调整）。
func (s *Selector) Weights() map[string]float64 {
	total := 0.0
	for _, sc := range s.held {
		total += sc
	}
	out := map[string]float64{}
	if total <= 0 {
		return out
	}
	for name, sc := range s.held {
		out[name] = sc / total
	}
	return out
}
