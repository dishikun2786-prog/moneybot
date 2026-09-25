package main

import (
	"log"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"github.com/gorilla/websocket"
	"github.com/moneybot/aetherhft/internal/feed"
	"github.com/moneybot/aetherhft/internal/ipc"
)

const wsURL = "wss://stream.bybit.com/v5/public/linear"

// spotOnlySyms 与 bybit_ws_bridge.py 的 SPOT_ONLY_SYMS 对齐（现货独占标的, 不在 linear 通道订阅）。
var spotOnlySyms = map[string]bool{"XAUTUSDT": true}

// linearSyms 返回 linear 通道标的清单（与 Python LINEAR_SYMS 对齐）。
// 从 BYBIT_SYMS 环境变量读取（与 bybit_ws_bridge.py 同源），并排除现货独占标的。
func linearSyms() []string {
	raw := os.Getenv("BYBIT_SYMS")
	// 优先读 web 管理后台同步的配置文件（与 bybit_ws_bridge.py 同源）
	if home, err := os.UserHomeDir(); err == nil {
		if data, err := os.ReadFile(filepath.Join(home, "polymarket", "data", "bybit_syms.txt")); err == nil {
			if s := strings.TrimSpace(string(data)); s != "" {
				raw = s
			}
		}
	}
	if raw == "" {
		raw = "BTCUSDT,ETHUSDT,XAUUSDT,XAGUSDT,XAUTUSDT,SOLUSDT,NEARUSDT,XRPUSDT"
	}
	var out []string
	for _, s := range strings.Split(raw, ",") {
		s = strings.TrimSpace(strings.ToUpper(s))
		if s != "" && !spotOnlySyms[s] {
			out = append(out, s)
		}
	}
	return out
}

func main() {
	shmPath := os.Getenv("MB_SHM_PATH")
	if shmPath == "" {
		shmPath = "/dev/shm/moneybot_book.shm"
	}
	shm, err := ipc.Open(shmPath)
	if err != nil {
		log.Fatalf("mmap open %s: %v", shmPath, err)
	}
	defer shm.Close()
	log.Printf("mmap snapshot: %s (total=%d bytes)", shmPath, ipc.TotalSize)

	conn, _, err := websocket.DefaultDialer.Dial(wsURL, nil)
	if err != nil {
		log.Fatalf("ws dial: %v", err)
	}
	defer conn.Close()

	hub := feed.NewBookHub(conn)
	if err := hub.Subscribe(linearSyms()); err != nil {
		log.Fatalf("subscribe: %v", err)
	}
	log.Printf("subscribed %d symbols", len(linearSyms()))

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, os.Interrupt, syscall.SIGTERM)

	// 读循环（生产者）
	go func() {
		for {
			_, data, err := conn.ReadMessage()
			if err != nil {
				log.Printf("ws read: %v (will exit)", err)
				return
			}
			hub.Handle(data)
		}
	}()

	// 快照刷新循环（1s），把聚合盘口写入 mmap
	tick := time.NewTicker(time.Second)
	defer tick.Stop()
	for {
		select {
		case <-stop:
			log.Println("shutdown")
			return
		case <-tick.C:
			views := hub.Snapshot(ipc.MaxLevelsPerSide)
			syms := make([]ipc.SymView, 0, len(views))
			for _, v := range views {
				syms = append(syms, ipc.SymView{Symbol: feed.SymString(v.Symbol), Bids: toIpcLevels(v.Bids), Asks: toIpcLevels(v.Asks)})
			}
			shm.Write(syms, time.Now().UnixNano())
		}
	}
}
// toIpcLevels 把 feed.Level 转为 ipc.Level（包边界显式转换）。
func toIpcLevels(ls []feed.Level) []ipc.Level {
	out := make([]ipc.Level, len(ls))
	for i, l := range ls {
		out[i] = ipc.Level{Price: l.Price, Size: l.Size}
	}
	return out
}