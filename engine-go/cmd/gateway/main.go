// go-prices-gateway: 用 Go 接管 Bybit ticker 热路径 + SSE 实时推送。
// 与 bybit_ws_bridge.py 完全对齐 bybit_prices.json 格式，前端零改动。
// 部署后 nginx 将 /api/stream/prices 分流到此服务，Python pm-dash 不再承担价格轮询。
package main

import (
	"encoding/json"
	"log"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/gorilla/websocket"
	"github.com/moneybot/aetherhft/internal/feed"
)

const (
	linearWS = "wss://stream.bybit.com/v5/public/linear"
	spotWS   = "wss://stream.bybit.com/v5/public/spot"
)

// linearOnlySyms 无现货腿的标的（黄金/白银永续，spot 通道无 ticker）。
var linearOnlySyms = map[string]bool{"XAUUSDT": true, "XAGUSDT": true}

func readSyms() []string {
	raw := os.Getenv("BYBIT_SYMS")
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
		if s != "" {
			out = append(out, s)
		}
	}
	return out
}

// subscribeTickers 订阅 tickers.*（分批，每批 ≤10 topic）。
func subscribeTickers(conn *websocket.Conn, syms []string) error {
	var args []string
	for _, s := range syms {
		args = append(args, "tickers."+s)
	}
	for i := 0; i < len(args); i += 10 {
		end := i + 10
		if end > len(args) {
			end = len(args)
		}
		if err := conn.WriteJSON(map[string]any{"op": "subscribe", "args": args[i:end]}); err != nil {
			return err
		}
	}
	return nil
}

// readLoop 读消息并喂给 PriceHub（isSpot 区分现货/永续通道）。
func readLoop(conn *websocket.Conn, hub *feed.PriceHub, isSpot bool) {
	for {
		_, data, err := conn.ReadMessage()
		if err != nil {
			log.Printf("ws read (spot=%v): %v", isSpot, err)
			return
		}
		sym, upd, msgTs, ok := feed.ParseTicker(data, isSpot)
		if !ok {
			continue
		}
		hub.Apply(sym, upd, msgTs, time.Now().UnixMilli())
	}
}

// writePricesFile 每秒原子写 bybit_prices.json（与 Python 格式一致）。
func writePricesFile(hub *feed.PriceHub, path string) {
	for range time.Tick(time.Second) {
		_, lagMs, prices := hub.Snapshot()
		if len(prices) == 0 {
			continue
		}
		out := map[string]any{"ts": time.Now().UnixMilli(), "lag_ms": lagMs, "prices": prices}
		b, err := json.Marshal(out)
		if err != nil {
			continue
		}
		tmp := path + ".tmp"
		if err := os.WriteFile(tmp, b, 0o644); err != nil {
			continue
		}
		_ = os.Rename(tmp, path)
	}
}

// ssePrices 是 /api/stream/prices 的实现（full + diff 双模式，与 Python 对齐）。
func ssePrices(hub *feed.PriceHub) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		diff := r.URL.Query().Get("diff") == "1"
		w.Header().Set("Content-Type", "text/event-stream")
		w.Header().Set("Cache-Control", "no-cache")
		w.Header().Set("X-Accel-Buffering", "no")
		w.WriteHeader(200)

		flusher, _ := w.(http.Flusher)
		updated := hub.Updated()
			seen := map[string]int64{}
		sentFull := false
		lastSend := time.Now()

		write := func(s string) {
			_, _ = w.Write([]byte(s))
			if flusher != nil {
				flusher.Flush()
			}
		}

		for {
			select {
			case <-r.Context().Done():
				return
			case <-updated:
			case <-time.After(200 * time.Millisecond):
			}
			nowMs := time.Now().UnixMilli()
			_, lagMs, prices := hub.Snapshot()
			if len(prices) == 0 {
				continue
			}
			if diff {
				if !sentFull {
					sentFull = true
					for k, v := range prices {
						seen[k] = v.Ts
					}
					write("data: " + mustJSON(map[string]any{"ts": nowMs, "prices": prices, "full": true}) + "\n\n")
					lastSend = time.Now()
				} else {
					delta := map[string]*feed.Ticker{}
					for k, v := range prices {
						if seen[k] != v.Ts {
							delta[k] = v
							seen[k] = v.Ts
						}
					}
					if len(delta) > 0 {
						write("data: " + mustJSON(map[string]any{"ts": nowMs, "prices": delta, "delta": true}) + "\n\n")
						lastSend = time.Now()
					}
				}
			} else {
				write("data: " + mustJSON(map[string]any{"ts": nowMs, "lag_ms": lagMs, "prices": prices}) + "\n\n")
				lastSend = time.Now()
			}
			if time.Since(lastSend) > 15*time.Second {
				lastSend = time.Now()
				write(": ping\n\n")
			}
		}
	}
}

var jmu sync.Mutex

func mustJSON(v any) string {
	b, _ := json.Marshal(v)
	return string(b)
}

func main() {
	out := os.Getenv("MB_PRICES_OUT")
	if out == "" {
		out = filepath.Join(mustHome(), "polymarket", "logs", "bybit_prices.json")
	}
	listen := os.Getenv("MB_SSE_LISTEN")
	if listen == "" {
		listen = "127.0.0.1:8090"
	}

	all := readSyms()
	var linear, spot []string
	for _, s := range all {
		if !linearOnlySyms[s] {
			spot = append(spot, s) // 双通道 + spot-only
		}
		if s != "XAUTUSDT" {
			linear = append(linear, s) // 永续通道（排除 spot-only）
		}
	}
	log.Printf("linear tickers=%v", linear)
	log.Printf("spot tickers=%v", spot)

	hub := feed.NewPriceHub()

	// linear 通道
	lc, _, err := websocket.DefaultDialer.Dial(linearWS, nil)
	if err != nil {
		log.Fatalf("linear dial: %v", err)
	}
	defer lc.Close()
	if err := subscribeTickers(lc, linear); err != nil {
		log.Fatalf("linear subscribe: %v", err)
	}
	go readLoop(lc, hub, false)

	// spot 通道
	sc, _, err := websocket.DefaultDialer.Dial(spotWS, nil)
	if err != nil {
		log.Fatalf("spot dial: %v", err)
	}
	defer sc.Close()
	if err := subscribeTickers(sc, spot); err != nil {
		log.Fatalf("spot subscribe: %v", err)
	}
	go readLoop(sc, hub, true)

	// 每秒落盘（与 Python 双写兼容）
	go writePricesFile(hub, out)

	// SSE 服务
	mux := http.NewServeMux()
	mux.HandleFunc("/api/stream/prices", ssePrices(hub))
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte("ok\n"))
	})
	srv := &http.Server{Addr: listen, Handler: mux}
	go func() {
		log.Printf("SSE listening on %s", listen)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("sse serve: %v", err)
		}
	}()

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, os.Interrupt, syscall.SIGTERM)
	<-stop
	log.Println("shutdown")
	_ = srv.Close()
}

func mustHome() string {
	h, err := os.UserHomeDir()
	if err != nil {
		return "/home/ubuntu"
	}
	return h
}
