package vpvr

import "sort"

// VolumeProfile 是 1H 级别 VPVR（Volume Profile / Volume-at-Price）。
// 滑动窗口增量更新：维护最近 N 个 1H 小时的成交量分布，小时轮动时减去最旧小时，避免全量重算。
// 时间复杂度：AddTrade O(1)，RollHour O(桶数)，POC/ValueArea O(桶数 log 桶数)。
type VolumeProfile struct {
	window   int                       // 滑动窗口小时数（如 72）
	bucketSz float64                   // 价格桶大小
	profile  map[int64]float64         // 当前窗口累计: bucketIdx -> volume
	hours    []map[int64]float64       // 每个小时的增量（环形）
	hourHead int                       // hours 环形头
	hourN    int                       // 已积累小时数
}

// NewVolumeProfile 构造 VPVR。window=小时窗口，bucketSize=价格桶。
func NewVolumeProfile(window int, bucketSize float64) *VolumeProfile {
	if window < 1 {
		window = 72
	}
	if bucketSize <= 0 {
		bucketSize = 1
	}
	return &VolumeProfile{
		window:   window,
		bucketSz: bucketSize,
		profile:  map[int64]float64{},
		hours:    make([]map[int64]float64, window),
	}
}

// bucket 把价格映射到桶索引。
func (v *VolumeProfile) bucket(price float64) int64 {
	return int64(price / v.bucketSz)
}

// AddTrade 增量累加一笔成交（O(1)，热路径零分配）。
func (v *VolumeProfile) AddTrade(price, volume float64) {
	if volume <= 0 {
		return
	}
	b := v.bucket(price)
	v.profile[b] += volume
	// 当前小时增量（hours[hourHead] 是当前小时）
	if v.hours[v.hourHead] == nil {
		v.hours[v.hourHead] = map[int64]float64{}
	}
	v.hours[v.hourHead][b] += volume
}

// RollHour 小时轮动：淘汰最旧小时的成交量，滑动窗口前移。
func (v *VolumeProfile) RollHour() {
	v.hourHead = (v.hourHead + 1) % v.window
	if v.hourN < v.window {
		v.hourN++
	}
	// 淘汰被覆盖的最旧小时（当前 hourHead 位置指向新的空小时，先清掉旧数据）
	if old := v.hours[v.hourHead]; old != nil {
		for b, vol := range old {
			v.profile[b] -= vol
			if v.profile[b] <= 0 {
				delete(v.profile, b)
			}
		}
		v.hours[v.hourHead] = nil
	}
}

// POC 返回成交量最集中的价格（Point of Control）。
func (v *VolumeProfile) POC() (price float64, ok bool) {
	var bestB int64
	var bestV float64
	for b, vol := range v.profile {
		if vol > bestV {
			bestV = vol
			bestB = b
		}
	}
	if bestV <= 0 {
		return 0, false
	}
	return (float64(bestB) + 0.5) * v.bucketSz, true
}

// ValueArea 返回包含 70% 成交量的价值区上下边界（VAH/VAL）。
func (v *VolumeProfile) ValueArea() (vah, val float64, ok bool) {
	pocPrice, ok := v.POC()
	if !ok {
		return 0, 0, false
	}
	// 从 POC 向两侧扩展
	type entry struct {
		bucket int64
		vol    float64
	}
	var entries []entry
	for b, vol := range v.profile {
		entries = append(entries, entry{b, vol})
	}
	sort.Slice(entries, func(i, j int) bool { return entries[i].bucket < entries[j].bucket })

	var total float64
	for _, e := range entries {
		total += e.vol
	}
	target := total * 0.70

	pocB := v.bucket(pocPrice)
	// 找 POC 索引
	pocIdx := 0
	for i, e := range entries {
		if e.bucket >= pocB {
			pocIdx = i
			break
		}
	}
	acc := entries[pocIdx].vol
	lo, hi := pocIdx, pocIdx
	for acc < target {
		// 向两侧扩展，选量更大的一侧
		left, right := -1.0, -1.0
		if lo > 0 {
			left = entries[lo-1].vol
		}
		if hi < len(entries)-1 {
			right = entries[hi+1].vol
		}
		if left < 0 && right < 0 {
			break
		}
		if left >= right {
			lo--
			acc += entries[lo].vol
		} else {
			hi++
			acc += entries[hi].vol
		}
	}
	vah = (float64(entries[hi].bucket) + 0.5) * v.bucketSz
	val = (float64(entries[lo].bucket) + 0.5) * v.bucketSz
	return vah, val, true
}

// Len 返回当前桶数量。
func (v *VolumeProfile) Len() int { return len(v.profile) }
