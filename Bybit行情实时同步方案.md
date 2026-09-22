# Bybit 行情实时同步方案（价格数字与交易所同步跳动）

> 2026-09-22 · 实测锚点：都柏林节点 → Bybit 公共 WSS
> 目标：看板价格数字从「60 秒轮询」升级为「与 Bybit 官网同频跳动」（百毫秒级）

## 1. 现状与差距

| 环节 | 现状 | 问题 |
|---|---|---|
| 服务器→Bybit | REST 轮询 60s（carry_monitor/kline_collector） | 价格最旧 60s |
| 前端→服务器 | 轮询 10-30s | 用户看到的价格最旧 ~90s |
| Bybit 官网 | WSS 推送 ~100ms | 实时跳动 |

## 2. 实测锚点（今天，都柏林节点）

| 指标 | 实测 |
|---|---|
| TCP → stream.bybit.com | **8ms**（网络距离极近，之前 185-242ms 是 HTTPS 握手+API 处理耗时，不是距离） |
| WSS 连接+订阅首包 | ~700ms（仅一次） |
| 推送频率 | **~100ms/条**（tickers.BTCUSDT 实测） |
| 稳态同步时延 | 交易所快照→本地 **100-200ms**（含 Bybit 自身 100ms 快照间隔） |

**结论：网络不是瓶颈。** 都柏林离 Bybit 边缘 8ms，能达到与 Bybit 官网几乎一致的刷新速度。

## 3. 总体架构

```
Bybit WSS (wss://stream.bybit.com/v5/public/linear)
   │  单连接多订阅: tickers + orderbook + kline
   ▼
都柏林 bybit_ws_bridge.py (常驻进程, 内存缓存最新价 + 断线重连)
   ├─→ 写 price_1s jsonl (可选持久化 → data_writer → parquet 秒级数据集)
   └─→ SSE /api/stream/prices (登录态)
        ▼
浏览器 EventSource → 价格卡片 / K线末根 / 持仓浮盈 实时更新 (300ms 节流)
```

## 4. 技术栈选型（逐层论证）

| 层 | 选型 | 理由 |
|---|---|---|
| Bybit→服务器 | **websocket-client** (Python 1.9.2) | 已实测可用；单连接多订阅；自带心跳；与现有 Python 栈同构 |
| 服务器→浏览器 | **SSE**（text/event-stream） | 单向推送足够；浏览器原生 `EventSource` **自带断线重连**；复用 FastAPI，零新依赖；不需要 WebSocket 双向 |
| 持久化 | 现有 jsonl + data_writer | 与数据平台同构；秒级价格数据集可反哺回测 |
| 前端 | 原生 EventSource + DOM | 无新依赖；300ms 节流防刷屏；断线自动降级 30s 轮询 |
| 部署 | systemd `pm-bridge` 常驻单元 | 与现有 9 单元同模式 |

**否决项：浏览器直连 Bybit WSS** —— 省一跳但用户出口网络不确定（墙/代理波动）、无法持久化、多用户重复连接；经服务器中转更稳。

## 5. 消息订阅设计

| 订阅 | 用途 | 频率 |
|---|---|---|
| `tickers.BTCUSDT` / `tickers.ETHUSDT` | 价格数字实时跳动 | ~100ms |
| `orderbook.50.*` | 盘口快照（可选） | 秒级 |
| `kline.1.*` | 1 分钟 K 线末根实时更新 | 秒级 |

## 6. 与现有系统集成（共存原则）

- `carry_monitor` 60s 轮询**保留**：funding 费率/结算时间戳是慢变量，无需 WSS
- `kline_collector` 保留：负责历史回填与去重入库；WSS 只供**实时显示**
- `trade.html`：顶部新增实时价格大字 + 持仓浮盈秒级刷新 + 时延徽标（如「延迟 0.2s」）
- `data_writer`：可选新增 `price_1s` 数据集（秒级价格历史）

## 7. 实施计划

| 里程碑 | 内容 | 预估 |
|---|---|---|
| M1 | `bybit_ws_bridge.py`：连接/订阅/内存缓存/断线重连日志 + systemd 单元 | ~80 行 |
| M2 | SSE 路由 `/api/stream/prices`（登录态 + 心跳 + 频控） | ~40 行 |
| M3 | 交易室前端：EventSource 实时价格 + 300ms 节流 + 断线降级 30s 轮询 | ~60 行 |
| M4 | price_1s 持久化 + 对照 Bybit 官网验收（时延断言 <2s） | ~30 行 |

## 8. 风险与对策

| 风险 | 对策 |
|---|---|
| WSS 断线 | run_forever + ping 心跳 + 指数退避重连（1s→2s→4s→…max 30s）；前端降级轮询 |
| nginx 缓冲 SSE | 8443 反代需 `proxy_buffering off`（FastAPI 直连 8080 无此问题） |
| 服务器出口波动 | 当前 8ms 直达；若被墙，bridge 走日本跳板代理（已有） |
| 时钟偏差 | 服务端 chrony <1μs + 前端本机时区渲染（已上线） |
| 公共 WSS 限频 | 公共频道订阅 10 次/秒上限，我们的 3-6 个订阅远低于阈值 |

## 9. 预期效果

- 交易室价格数字与 Bybit 官网**同频跳动**（相差 ≤0.2s）
- 持仓浮盈秒级刷新（现在 15s）
- 新增秒级价格数据集（回测更细粒度）
- 全部复用现有栈：Python + FastAPI + 原生 JS + systemd，零重型新依赖
