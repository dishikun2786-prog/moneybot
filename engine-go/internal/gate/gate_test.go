package gate

import "testing"

func TestResonate(t *testing.T) {
	// 距离近 + 高能量 + 高挂谷 → 放行
	score, fire := Resonate(1.0, 0.9, 0.9, 0.5)
	if !fire || score < 0.7 {
		t.Fatalf("强共振应放行: score=%v fire=%v", score, fire)
	}
	// 距离远 → 权重低 → 不放行
	_, fire2 := Resonate(0.05, 0.9, 0.9, 0.5)
	if fire2 {
		t.Fatal("距离远不应放行")
	}
}

func TestFullGateNetEV(t *testing.T) {
	// NetEV 不足 → 即使共振强也拦截
	_, fire := FullGate(1.0, 0.9, 0.9, 0.5, 10, 50)
	if fire {
		t.Fatal("NetEV 不足应拦截")
	}
	_, fire2 := FullGate(1.0, 0.9, 0.9, 0.5, 60, 50)
	if !fire2 {
		t.Fatal("NetEV 充足应放行")
	}
}
