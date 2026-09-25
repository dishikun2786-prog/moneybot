package liquidation

import (
	"context"
	"encoding/json"
	"strconv"
	"strings"
	"sync/atomic"
	"time"

	"github.com/gorilla/websocket"
)

// liqMsg 是 Bybit v5 公共强平频道消息 (liquidation.<SYM>)。
type liqMsg struct {
	Topic string `json:"topic"`
	Type  string `json:"type"`
	Data  []struct {
		Price       string `json:"price"`
		Side        string `json:"side"` // "Buy"/"Sell"
		Size        string `json:"size"`
		UpdatedTime int64  `json:"updatedTime"`
	} `json:"data"`
}

// Client 订阅 Bybit 公共强平频道并注入 FDTD，支持断线自动重连（指数退避）。
// 心跳：Bybit 要求每 20s 发一次 {"op":"ping"}，否则服务端 ~60s 后关闭连接。
type Client struct {
	url           string
	syms          []string
	injector      *Injector
	maxBackoff    time.Duration
	conn          *websocket.Conn
	everConnected atomic.Bool // 是否成功建立过连接（观测）
}

// NewClient 构造客户端。maxBackoff 默认 30s（可在构造后调整）。
func NewClient(url string, syms []string, inj *Injector) *Client {
	return &Client{url: url, syms: syms, injector: inj, maxBackoff: 30 * time.Second}
}

// EverConnected 返回是否成功建立过连接（观测）。
func (c *Client) EverConnected() bool { return c.everConnected.Load() }

// Run 连接 → 订阅 → 读循环；断线指数退避重连，直到 ctx 取消。
func (c *Client) Run(ctx context.Context) error {
	backoff := time.Second
	for {
		if err := ctx.Err(); err != nil {
			return err
		}
		conn, _, err := websocket.DefaultDialer.Dial(c.url, nil)
		if err != nil {
			if err := sleepCtx(ctx, backoff); err != nil {
				return err
			}
			backoff = nextBackoff(backoff, c.maxBackoff)
			continue
		}
		if err := c.subscribe(conn, c.syms); err != nil {
			_ = conn.Close()
			return err
		}
		c.conn = conn
		c.everConnected.Store(true)
		backoff = time.Second

		// 心跳 + ctx 取消打断读阻塞
		pingDone := make(chan struct{})
		go c.pingLoop(conn, pingDone)
		go func() {
			<-ctx.Done()
			_ = conn.Close() // 打断 ReadMessage，使 readLoop 返回
		}()

		err = c.readLoop(conn)
		close(pingDone)
		_ = conn.Close()

		if ctx.Err() != nil {
			return ctx.Err() // ctx 取消
		}
		if err == nil {
			return nil // 正常退出
		}
		// 断线：退避后重连
		if err := sleepCtx(ctx, backoff); err != nil {
			return err
		}
		backoff = nextBackoff(backoff, c.maxBackoff)
	}
}

func (c *Client) readLoop(conn *websocket.Conn) error {
	for {
		_, data, err := conn.ReadMessage()
		if err != nil {
			return err
		}
		c.handle(data)
	}
}

func (c *Client) pingLoop(conn *websocket.Conn, done <-chan struct{}) {
	t := time.NewTicker(20 * time.Second)
	defer t.Stop()
	for {
		select {
		case <-done:
			return
		case <-t.C:
			_ = conn.WriteJSON(map[string]any{"op": "ping"})
		}
	}
}

func (c *Client) subscribe(conn *websocket.Conn, syms []string) error {
	var args []string
	for _, s := range syms {
		args = append(args, "liquidation."+s)
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

// handle 解析单条强平消息并注入 FDTD（可独立测试）。
func (c *Client) handle(data []byte) {
	var m liqMsg
	if json.Unmarshal(data, &m) != nil || !strings.HasPrefix(m.Topic, "liquidation.") {
		return
	}
	for _, d := range m.Data {
		price, e1 := strconv.ParseFloat(d.Price, 64)
		size, e2 := strconv.ParseFloat(d.Size, 64)
		if e1 != nil || e2 != nil || size <= 0 {
			continue
		}
		side := SideBuy
		if d.Side == "Sell" {
			side = SideSell
		}
		c.injector.Inject(Event{TsNs: d.UpdatedTime * 1e6, Price: price, Size: size, Side: side})
	}
}

func sleepCtx(ctx context.Context, d time.Duration) error {
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-t.C:
		return nil
	}
}

// nextBackoff 返回下一次退避（指数×2，封顶 max）。
func nextBackoff(cur, max time.Duration) time.Duration {
	if cur >= max {
		return max
	}
	if cur*2 > max {
		return max
	}
	return cur * 2
}