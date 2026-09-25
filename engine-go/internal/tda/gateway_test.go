package tda

import (
	"os"
	"testing"
)

// TestGatewayCompute 用真实 Python tda.py 做一次端到端 IPC。
// 需要 python + numpy 与 tda.py；找不到则跳过。
func TestGatewayCompute(t *testing.T) {
	script := "../../../tda.py" // 相对 engine-go/tda.py
	if _, err := os.Stat(script); err != nil {
		t.Skip("tda.py 不在工作区, 跳过")
	}
	python := "python"
	g := NewGateway(python, script)
	pts := []Point{
		{Price: 100, Size: 1}, {Price: 100.1, Size: 1}, {Price: 100.2, Size: 1},
		{Price: 200, Size: 1}, // 孤立远点 → 大 gap
	}
	f, err := g.Compute(pts)
	if err != nil {
		t.Fatalf("Compute 失败: %v", err)
	}
	if f.NPoints != 4 {
		t.Fatalf("点数应=4, got %d", f.NPoints)
	}
	if f.MaxGap < 90 {
		t.Fatalf("孤立远点应产生大 gap, got %v", f.MaxGap)
	}
}