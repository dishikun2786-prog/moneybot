package qmdj

import (
	"bytes"
	"encoding/json"
	"os/exec"
)

// QMDJContext 是时空排盘评分对象（Python 排盘服务输出）。
type QMDJContext struct {
	Timestamp   int64             `json:"timestamp"`
	Time        string            `json:"time"`
	Ganzhi      map[string]string `json:"ganzhi"`
	SolarTerm   string            `json:"solar_term"`
	Ke          int               `json:"ke"`
	Minute      int               `json:"minute"`
	Second      int               `json:"second"`
	Score       float64           `json:"qmdj_score"`
	PatternType string            `json:"pattern_type"`
}

// Gateway 通过子进程 stdio 调用 Python 排盘服务（与 tda.Gateway 同款 exec 模式）。
type Gateway struct {
	python string
	script string
}

// NewGateway 构造排盘网关。
func NewGateway(python, script string) *Gateway {
	return &Gateway{python: python, script: script}
}

// Compute 输入 Unix 毫秒时间戳，返回时空排盘评分。
func (g *Gateway) Compute(timestamp int64) (QMDJContext, error) {
	in, err := json.Marshal(map[string]int64{"timestamp": timestamp})
	if err != nil {
		return QMDJContext{}, err
	}
	cmd := exec.Command(g.python, g.script, "--stdin")
	cmd.Stdin = bytes.NewReader(in)
	out, err := cmd.Output()
	if err != nil {
		return QMDJContext{}, err
	}
	var ctx QMDJContext
	if err := json.Unmarshal(out, &ctx); err != nil {
		return QMDJContext{}, err
	}
	return ctx, nil
}
