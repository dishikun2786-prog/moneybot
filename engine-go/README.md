# Aether-HFT Go 核心引擎（engine-go）

高频动量突破系统的 **Go 核心引擎**：超低延迟数据接入、FDTD 波动方程激波、Kakeya 假挂单过滤、
TDA 拓扑仲裁、手续费净期望/动态退出风控、FIFO 微观撮合沙盒，以及 CPU 亲和性硬化。
与 Python（`tda.py` 拓扑、Moneybot 平台决策层）通过子进程 / mmap 共享内存协同。

> 铁律：**模型只做判断题，代码只做执行题，模型永不直接触钱**——AI 输出概率+置信度，代码按门控阈值决定动或不动。

---

## 目录结构

```
engine-go/
├── go.mod / go.sum            # module github.com/moneybot/aetherhft (go 1.22)
├── tda.py                     # Python 持续同调计算层（H_0 / Wasserstein / H_1 可选 Ripser）
├── python/mmap_reader.py      # mmap 快照的 Python 零拷贝读端
├── cmd/
│   ├── aether/                # 完整决策链 demo（强平→FDTD→Kakeya→TDA→NetEV→沙盒）
│   ├── feed/                  # Bybit WSS 盘口接入 + mmap 快照落盘
│   ├── liquidation-smoke/     # 强平流联通性冒烟
│   └── pinbench/              # CPU 亲和性延迟抖动对比基准
└── internal/
    ├── feed/                  # 数据接入：无锁环形缓冲 + 对象池 + 零分配解析 + L2 盘口状态
    ├── fdtd/                  # 一维受迫阻尼波动方程 FDTD 内核
    ├── liquidation/           # 强平流解析 + 注入 + 断线重连
    ├── sandbox/               # FIFO 微观队列撮合沙盒（真实滑点 VWAP）
    ├── kakeya/                # 挂谷几何假挂单过滤（K_om 一票否决）
    ├── tda/                   # 持续同调 Go 网关（子进程 IPC 调 Python）
    ├── ipc/                   # mmap 共享内存快照（Linux mmap / Windows 桩）
    ├── risk/                  # 手续费净期望过滤器 + 动态止盈止损 + sync.Pool 范例
    └── pin/                   # CPU 亲和性（runtime.LockOSThread + sched_setaffinity）
```

---

## 构建 / 测试 / 基准

```bash
cd engine-go
go mod tidy                      # 拉依赖（gorilla/websocket, golang.org/x/sys）
go build ./...                   # 编译
go vet ./...                     # 静态检查
go test ./...                    # 全量单测
go test ./... -run '^$' -bench . -benchmem   # 基准 + 分配统计

# 强平流联通冒烟（连接 Bybit 公共 WSS 30s）
go run ./cmd/liquidation-smoke

# 完整决策链 demo
go run ./cmd/aether

# CPU 亲和性延迟抖动基准（Linux）
go run ./cmd/pinbench
```

> 环境变量：`GOTOOLCHAIN=local` 可禁用工具链自动下载，保持 `go 1.22`。

---

## 模块说明

### 1. `feed` — 数据接入（热路径零分配）
- `ring.go`：SPSC 无锁环形缓冲（`head`/`tail` 分离所有权 + `atomic.Uint64`）。
- `pool.go`：`sync.Pool` 订单簿节点对象池。
- `book.go`：定长有序数组 L2 盘口 + 手写二分 `lowerBound`（delta 零分配）。
- `parse.go`：手写零分配 Bybit JSON 解析（`unsafe.String` 零拷贝 + 预声明字段名）。
- `ws.go`：Bybit `orderbook.200` snapshot/delta 重组 + 缺口重订阅。
- **基准**：`RingTransport 0 allocs`、`BookDelta 89ns/0 allocs`、`ParseBookSnapshot 41μs/0 allocs`。

### 2. `fdtd` — 波动方程内核
- 一维受迫阻尼波动方程显式三点差分，三缓冲轮转零分配。
- `Step()` / `AddForcing()` / `InjectImpulse()` / `EnergyFlux()` / `DensePressure()`。
- 并发安全：`sync.RWMutex` 保护外源项 `F`（注入协程写 / 计算循环读）。
- **基准**：`StepN1024 ≈ 2.8μs · 0 allocs`（远低于 0.5ms 预算）。

### 3. `liquidation` — 强平流注入
- Bybit `liquidation.<SYM>` 解析；`side=Buy`（空头爆仓）→正脉冲，`Sell`（多头爆仓）→负脉冲。
- `Client.Run(ctx)`：连接→订阅→读循环，断线**指数退避重连** + **20s 心跳** + ctx 取消打断读阻塞。
- **验证**：真实 Bybit 联通冒烟通过（连接/订阅成功，`EverConnected=true`）。

### 4. `sandbox` — FIFO 撮合沙盒
- 限价单记录前方排队量 `Qahead`，**累计 taker 量 > Qahead 才成交**，杜绝「触价即成交」。
- 成交均价按逐笔 VWAP 增量累计；`Slippage()` 输出真实滑点；`PlaceMarket` 市价扫单。

### 5. `kakeya` — 挂谷假挂单过滤
- `K_om` = 滚动窗口「被成交吃掉的墙量 / 总墙量」：假挂单（出现即撤）→0，真进攻（被吃）→1。
- `Veto(threshold)`：`K_om < threshold` 一票否决。

### 6. `tda` + `tda.py` — 持续同调
- 盘口点云 → 价格轴 H_0 持续同调（相邻价格 gap = 连通分支死亡时间）。
- 相邻快照 Wasserstein 距离突变 → 结构性相变（流动性真空）。
- Go `Gateway` 通过子进程 stdio 调 `python tda.py --stdin`（零 gRPC 依赖，生产可换 gRPC/共享内存）。
- H_1 孔洞需 Ripser，未装则优雅降级为 H_0。

### 7. `ipc` — mmap 共享内存
- 固定二进制布局（magic/version/seq/标的槽/档位），Go `syscall.Mmap` 写入，Python `mmap_reader.py` 零拷贝读取。
- Linux `shm_unix.go` / Windows `shm_windows.go`（桩）。

### 8. `risk` — 实盘风控
- `expectancy.go`：`NetEV = P·Target − (1−P)·Stop − Fee − Slippage`，双闸（`NetEV>threshold` 且 `>feeMargin×往返手续费`）。
- `exit.go`：盈亏平衡垫 = 往返手续费 + 滑点；仅「动能结构性崩溃 且 浮盈覆盖手续费」才平仓。
- `pool.go`：`sync.Pool` 零分配范例（**适用对象逃逸场景**，非逃逸局部结构体编译器已栈分配）。

### 9. `pin` — CPU 亲和性
- `PinToCPU(cpu)`：`runtime.LockOSThread` + `unix.SchedSetaffinity`；`RunPinned(cpu, fn)`。
- 构建标签：`pin_linux.go`（`//go:build linux`）/ `pin_other.go`（非 Linux 桩）。

---

## 完整决策链（`cmd/aether`）

```
强平流 → FDTD 波场 → 突破信号 → Kakeya 否决 → TDA 相变 → NetEV 闸 → 沙盒撮合
```

各闸职责（防御纵深）：

| 闸 | 拦截什么 | 判定 |
|---|---|---|
| FDTD | 无能量激波 | `DensePressure ≥ 阈值` |
| Kakeya | 假挂单 | `K_om < 0.6` 一票否决 |
| TDA | 流动性真空/断层 | 价格点云 max gap > 阈值 |
| NetEV | 赚点数亏手续费 | `NetEV > threshold` 且 `> feeMargin×往返手续费` |
| 沙盒 | 触价即成交失真 | 市价单 VWAP 真实滑点 |

演示两条路径：**A 真墙全闸放行 → 下单**；**B 假挂单被 Kakeya 一票否决，不到执行层**。

---

## 性能基线（实测）

| 基准 | 结果 | 说明 |
|---|---|---|
| FDTD Step N=1024 | ~2.8 μs · 0 allocs | 本机 i7-4770S；服务器 ~2.6μs/步 |
| Ring 纯传输 | 0 allocs | 定长 `BookUpdate` 结构 memcpy |
| Book delta 合并 | 89 ns · 0 allocs | 手写二分 |
| ParseBook snapshot(200档) | 41 μs · 0 allocs | 手写零分配解析 |
| ParseBook delta | 1.2 μs · 0 allocs | |

**CPU 亲和性延迟抖动（Linux 2 vCPU 共享实例，每 tick=100 步 FDTD）**：

| 场景 | p50 | p99 | max | 抖动(p99−p50) |
|---|---|---|---|---|
| 未绑定 | 256.8μs | 650.1μs | 5517.7μs | 393.3μs |
| 绑定核0 | 496.7μs | 976.5μs | 4068.9μs | 479.8μs |
| 绑定核1 | 261.6μs | 943.1μs | 2162.7μs | 681.5μs |

> 结论：`sched_setaffinity` 机制可用（Linux 运行时验证通过）；但在**共享 2 核实例**上绑核并未降低抖动（甚至绑到繁忙核更差）。真正压尾延迟需**专用核**（`isolcpus` + cgroup `cpuset`），而非单纯绑核。

---

## 验证记录

| 项 | 结果 |
|---|---|
| `go build/vet/test ./...`（Windows） | 全绿 |
| Linux 交叉编译 `GOOS=linux go build ./...` | 通过 |
| `go test ./internal/pin/`（Linux 服务器） | 通过（sched_setaffinity 运行时验证） |
| 强平流联通冒烟（Bybit 公共 WSS） | 连接/订阅成功 |
| TDA Go↔Python 子进程 IPC | 端到端通过 |

---

## 部署说明

- 目标：Linux（Dublin 服务器）。`pin`/`ipc` 的 Linux 实现已在服务器交叉编译/运行时验证。
- 服务器已就位：`~/aetherhft`（源码）、`~/go`（Go 1.22.12）。
- 生产数据流：`cmd/feed` 写入 `/dev/shm/moneybot_book.shm` → `python/mmap_reader.py` 零拷贝读取 → 接入 Moneybot `microstructure.py`。
- 依赖：`gorilla/websocket v1.5.3`、`golang.org/x/sys v0.28.0`（Go 1.22 兼容）。

---

## 下一步

1. `feed.BookHub` 真实数据 → FDTD/Kakeya/TDA 生产决策循环（当前为 demo 数据）。
2. 专用核部署时用 `RunPinned` 钉核（网络 IO 核0 / 计算核1..N）。
3. H_1 持续同调接入（需服务器装 Ripser/GUDHI）。
4. 与 Python Moneybot 平台的 mmap 数据流打通 + 影子对账。
