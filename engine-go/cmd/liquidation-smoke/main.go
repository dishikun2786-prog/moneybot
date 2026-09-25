// 强平流联通性冒烟：先验证 WSS 链路活跃，再跑 liquidation.Client.Run(ctx) 30 秒。
package main

import (
	"context"
	"fmt"
	"os"
	"time"

	"github.com/gorilla/websocket"
	"github.com/moneybot/aetherhft/internal/fdtd"
	"github.com/moneybot/aetherhft/internal/liquidation"
)

const url = "wss://stream.bybit.com/v5/public/linear"

func main() {
	// 阶段 A：连通性 + 链路活跃（订阅高频成交流，读到 3 条即证明 WSS 通）
	fmt.Println("[1/2] 连通性检查（订阅 publicTrade.BTCUSDT）...")
	if err := livenessCheck(); err != nil {
		fmt.Println("FAIL:", err)
		os.Exit(1)
	}
	fmt.Println("      链路活跃 ✓")

	// 阶段 B：强平流 Run(ctx) 30 秒
	fmt.Println("[2/2] 强平流 Run(ctx) 30 秒...")
	g := fdtd.New(256, 1.0, 0.01, 0.4)
	c := make([]float64, 256)
	for i := range c {
		c[i] = 0.9
	}
	g.SetWaveSpeed(c)
	inj := liquidation.NewInjector(g, func(p float64) int { return int(p) }, 0.5)
	cli := liquidation.NewClient(url, []string{"BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"}, inj)

	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	start := time.Now()
	err := cli.Run(ctx)
	elapsed := time.Since(start).Round(time.Second)
	if err != nil && err != context.DeadlineExceeded {
		fmt.Println("      Run 退出:", err)
	}
	fmt.Printf("      已连接=%v 运行=%v 收到强平事件=%d\n", cli.EverConnected(), elapsed, inj.Events())
	if cli.EverConnected() {
		fmt.Println("SMOKE OK：Bybit 强平流连接/订阅成功（强平事件低频，0 条属正常）")
	} else {
		fmt.Println("SMOKE FAIL：未能建立连接")
		os.Exit(1)
	}
}

// livenessCheck 订阅一笔高频成交流并读到 3 条消息，证明 WSS 链路通且数据在流。
func livenessCheck() error {
	conn, _, err := websocket.DefaultDialer.Dial(url, nil)
	if err != nil {
		return err
	}
	defer conn.Close()
	if err := conn.WriteJSON(map[string]any{"op": "subscribe", "args": []string{"publicTrade.BTCUSDT"}}); err != nil {
		return err
	}
	_ = conn.SetReadDeadline(time.Now().Add(15 * time.Second))
	for n := 0; n < 3; {
		_, data, err := conn.ReadMessage()
		if err != nil {
			return err
		}
		if len(data) > 0 {
			n++
		}
	}
	return nil
}