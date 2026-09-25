// go-depth-gateway: Go 接管 depth 单流（盘口/成交/K线/价格/基差）热路径 + SSE 广播。
// 数据源与 bybit_ws_bridge.py 对齐，前端 EventSource('/api/stream/depth') 零改动。
package main

import (
	"bytes"
	"encoding/json"
	"log"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/gorilla/websocket"
	"github.com/moneybot/aetherhft/internal/feed"
)

const (
	linearWS = "wss://stream.bybit.com/v5/public/linear"
	spotWS   = "wss://stream.bybit.com/v5/public/spot"
)

var (
	klineIntervals = []string{"1", "5", "15", "60", "240", "D"}
	linearOnly     = map[string]bool{"XAUUSDT": true, "XAGUSDT": true}
	spotOnly       = map[string]bool{"XAUTUSDT": true}
)

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
		if s = strings.TrimSpace(strings.ToUpper(s)); s != "" {
			out = append(out, s)
		}
	}
	return out
}

func batchedSubscribe(conn *websocket.Conn, args []string) error {
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

// Hub 聚合所有实时源数据。
type Hub struct {
	mu        sync.Mutex
	bookL     *feed.BookHub
	bookS     *feed.BookHub
	kline     *feed.KlineHub
	price     *feed.PriceHub
}

var (
	frameVer   atomic.Int64
	frameBytes atomic.Value // []byte
)

func main() {
	listen := os.Getenv("MB_DEPTH_LISTEN")
	if listen == "" {
		listen = "127.0.0.1:8091"
	}
	out := os.Getenv("MB_DEPTH_OUT")
	if out == "" {
		out = filepath.Join(mustHome(), "polymarket", "logs", "go_orderbook.json")
	}

	all := readSyms()
	var linearSyms, spotSyms []string
	for _, s := range all {
		if !linearOnly[s] {
			spotSyms = append(spotSyms, s)
		}
		if !spotOnly[s] {
			linearSyms = append(linearSyms, s)
		}
	}
	log.Printf("linear=%v spot=%v", linearSyms, spotSyms)

	priceHub := feed.NewPriceHub()
	klineHub := feed.NewKlineHub()

	// linear 连接：orderbook + trade + kline + ticker
	lc, _, err := websocket.DefaultDialer.Dial(linearWS, nil)
	if err != nil {
		log.Fatalf("linear dial: %v", err)
	}
	defer lc.Close()
	bookL := feed.NewBookHub(lc)
	{
		var args []string
		for _, s := range linearSyms {
			args = append(args, "orderbook.200."+s, "publicTrade."+s)
			for _, iv := range klineIntervals {
				args = append(args, "kline."+iv+"."+s)
			}
			args = append(args, "tickers."+s)
		}
		if err := batchedSubscribe(lc, args); err != nil {
			log.Fatalf("linear subscribe: %v", err)
		}
	}
	go linearLoop(lc, bookL, klineHub, priceHub)

	// spot 连接：orderbook + trade + ticker
	sc, _, err := websocket.DefaultDialer.Dial(spotWS, nil)
	if err != nil {
		log.Fatalf("spot dial: %v", err)
	}
	defer sc.Close()
	bookS := feed.NewBookHub(sc)
	{
		var args []string
		for _, s := range spotSyms {
			args = append(args, "orderbook.200."+s, "publicTrade."+s, "tickers."+s)
		}
		if err := batchedSubscribe(sc, args); err != nil {
			log.Fatalf("spot subscribe: %v", err)
		}
	}
	go spotLoop(sc, bookS, priceHub)

	hub := &Hub{bookL: bookL, bookS: bookS, kline: klineHub, price: priceHub}

	// 每秒聚合 frame
	go frameLoop(hub, out)

	// SSE
	mux := http.NewServeMux()
	mux.HandleFunc("/api/stream/depth", sseDepth)
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) { w.Write([]byte("ok\n")) })
	srv := &http.Server{Addr: listen, Handler: mux}
	go func() {
		log.Printf("depth SSE listening on %s", listen)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("sse: %v", err)
		}
	}()

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, os.Interrupt, syscall.SIGTERM)
	<-stop
	log.Println("shutdown")
	_ = srv.Close()
}

func linearLoop(conn *websocket.Conn, book *feed.BookHub, kline *feed.KlineHub, price *feed.PriceHub) {
	for {
		_, data, err := conn.ReadMessage()
		if err != nil {
			log.Printf("linear read: %v", err)
			return
		}
		switch {
		case bytes.Contains(data, []byte("orderbook.")) || bytes.Contains(data, []byte("publicTrade.")):
			book.Handle(data)
		case bytes.Contains(data, []byte("kline.")):
			kline.Apply(data)
		case bytes.Contains(data, []byte("tickers.")):
			if sym, upd, msgTs, ok := feed.ParseTicker(data, false); ok {
				price.Apply(sym, upd, msgTs, time.Now().UnixMilli())
			}
		}
	}
}

func spotLoop(conn *websocket.Conn, book *feed.BookHub, price *feed.PriceHub) {
	for {
		_, data, err := conn.ReadMessage()
		if err != nil {
			log.Printf("spot read: %v", err)
			return
		}
		switch {
		case bytes.Contains(data, []byte("orderbook.")) || bytes.Contains(data, []byte("publicTrade.")):
			book.Handle(data)
		case bytes.Contains(data, []byte("tickers.")):
			if sym, upd, msgTs, ok := feed.ParseTicker(data, true); ok {
				price.Apply(sym, upd, msgTs, time.Now().UnixMilli())
			}
		}
	}
}

func frameLoop(hub *Hub, out string) {
	for range time.Tick(time.Second) {
		frame := buildFrame(hub)
		b, err := json.Marshal(frame)
		if err != nil {
			continue
		}
		frameBytes.Store(b)
		frameVer.Add(1)
		// 双写（测试路径，灰度对账用）
		tmp := out + ".tmp"
		if os.WriteFile(tmp, b, 0o644) == nil {
			_ = os.Rename(tmp, out)
		}
	}
}

// buildFrame 聚合所有 Hub 数据生成 depth 帧。
func buildFrame(hub *Hub) map[string]any {
	hub.mu.Lock()
	defer hub.mu.Unlock()

	books := map[string]any{}
	for _, v := range hub.bookL.Snapshot(200) {
		sym := feed.SymString(v.Symbol)
		books[sym] = map[string]any{
			"bids": levels(v.Bids, true),
			"asks": levels(v.Asks, false),
		}
	}
	trades := map[string]any{}
	for sym, t := range hub.bookL.SnapshotTrades() {
		if len(t) > 0 {
			trades[sym] = tradesJSON(t)
		}
	}

	booksSpot := map[string]any{}
	for _, v := range hub.bookS.Snapshot(200) {
		sym := feed.SymString(v.Symbol)
		booksSpot[sym] = map[string]any{
			"bids": levels(v.Bids, true),
			"asks": levels(v.Asks, false),
		}
	}
	tradesSpot := map[string]any{}
	for sym, t := range hub.bookS.SnapshotTrades() {
		if len(t) > 0 {
			tradesSpot[sym] = tradesJSON(t)
		}
	}

	kl := map[string]any{}
	for sym, per := range hub.kline.Snapshot() {
		perOut := map[string]any{}
		for iv, k := range per {
			perOut[iv] = map[string]any{"t": k.T, "o": k.O, "h": k.H, "l": k.L, "c": k.C, "v": k.V, "cf": k.Cf}
		}
		if len(perOut) > 0 {
			kl[sym] = perOut
		}
	}

	_, _, prices := hub.price.Snapshot()
	px := map[string]any{}
	pxSpot := map[string]any{}
	basis := map[string]any{}
	for sym, t := range prices {
		px[sym] = map[string]any{"last": t.Last, "chg": t.ChangePct, "oi": t.OI,
			"funding": t.FundingRate, "next_funding": t.NextFunding}
		if t.Spot > 0 {
			pxSpot[sym] = t.Spot
			if t.Last > 0 {
				basis[sym] = map[string]any{"perp": t.Last, "spot": t.Spot,
					"basis_pct": (t.Last - t.Spot) / t.Spot * 100}
			}
		}
	}
	return map[string]any{
		"ts":          time.Now().UnixMilli(),
		"books":       books,
		"trades":      trades,
		"books_spot":  booksSpot,
		"trades_spot": tradesSpot,
		"kline_snap":  kl,
		"px":          px,
		"px_spot":     pxSpot,
		"basis":       basis,
	}
}

func levels(ls []feed.Level, isBid bool) [][2]any {
	out := make([][2]any, 0, len(ls))
	for _, l := range ls {
		out = append(out, [2]any{strconv.FormatFloat(l.Price, 'f', -1, 64), l.Size})
	}
	return out
}

func tradesJSON(ts []feed.Trade) []map[string]any {
	out := make([]map[string]any, 0, len(ts))
	for _, t := range ts {
		out = append(out, map[string]any{"T": t.TsNs / 1e6, "S": t.Side, "v": t.V, "p": t.P})
	}
	return out
}

func sseDepth(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("X-Accel-Buffering", "no")
	flusher, _ := w.(http.Flusher)
	lastVer := int64(0)
	lastSend := time.Now()
	for {
		select {
		case <-r.Context().Done():
			return
		case <-time.After(200 * time.Millisecond):
		}
		v := frameVer.Load()
		if v == lastVer {
			if time.Since(lastSend) > 15*time.Second {
				lastSend = time.Now()
				_, _ = w.Write([]byte(": ping\n\n"))
				if flusher != nil {
					flusher.Flush()
				}
			}
			continue
		}
		lastVer = v
		lastSend = time.Now()
		if b, ok := frameBytes.Load().([]byte); ok {
			_, _ = w.Write([]byte("data: "))
			_, _ = w.Write(b)
			_, _ = w.Write([]byte("\n\n"))
			if flusher != nil {
				flusher.Flush()
			}
		}
	}
}

func mustHome() string {
	h, err := os.UserHomeDir()
	if err != nil {
		return "/home/ubuntu"
	}
	return h
}
