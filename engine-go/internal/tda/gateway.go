package tda

import (
	"bytes"
	"encoding/json"
	"os/exec"
)

// Point 是点云中的一个点（价格, 挂单量）。
type Point struct {
	Price float64 `json:"price"`
	Size  float64 `json:"size"`
}

// Features 是 TDA 计算层输出。
type Features struct {
	H0Deaths []float64 `json:"h0_deaths"`
	MaxGap   float64   `json:"max_gap"`
	NPoints  int       `json:"n_points"`
}

// Gateway 通过子进程 stdio 调用 Python tda.py（零 gRPC 依赖的简单 IPC）。
// 生产可替换为 gRPC / 共享内存；此处子进程方式即可用、可测。
type Gateway struct {
	python string
	script string
}

// NewGateway 构造网关。
func NewGateway(python, script string) *Gateway {
	return &Gateway{python: python, script: script}
}

// Compute 把盘口点云传给 Python TDA 层，返回拓扑特征。
func (g *Gateway) Compute(points []Point) (Features, error) {
	in, err := json.Marshal(points)
	if err != nil {
		return Features{}, err
	}
	cmd := exec.Command(g.python, g.script, "--stdin")
	cmd.Stdin = bytes.NewReader(in)
	out, err := cmd.Output()
	if err != nil {
		return Features{}, err
	}
	var f Features
	if err := json.Unmarshal(out, &f); err != nil {
		return Features{}, err
	}
	return f, nil
}