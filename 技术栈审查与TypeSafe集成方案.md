# 量化对冲技术栈全面审查 与 TypeSafe AI 集成方案

> 2026-09-22 · 基于代码审查 + 实盘链路实测 + 官方文档核实（Polymarket / Bybit / TypeSafe AI）

---

## 0. 结论速览（TL;DR）

| 维度 | 结论 |
|---|---|
| 信号研究 | ✅ 已成型（碰价定价/公允价/多因子/ATR 均可复用） |
| 数据管道 | ⚠️ 轮询为主，无 WebSocket、无事件驱动 |
| 执行安全 | ❌ 缺失（无订单状态机/幂等/熔断/kill switch） |
| 低延迟 | ❌ 未达标（15s轮询 vs 秒级错价；Bybit 腿 ~220ms 无预算管控） |
| 运维 | ⚠️ 单服务器 + 恢复路径不明确（实测重启后 ~1.5h 才恢复监控） |
| **TypeSafe AI** | ✅ **密钥有效，实测 TTFB 425–450ms（首包）/ 稳态 ~250ms** → 定位 **L2 事件级决策层**，绝不进入逐笔热路径 |

**核心判断**：研究→执行之间有断层。真正的路线是「确定性内核 + 事件级AI决策」双层结构，TypeSafe 填补的是**事件驱动判断**这一块（新闻→结构化信号、信号融合置信门控、regime授权），而不是毫秒级定价。

---

## 1. 现状盘点

| 层 | 组件 | 状态 |
|---|---|---|
| 数据 | PM Gamma/CLOB/Data/Bridge（REST） | ✅ 可用 |
| 数据 | Bybit REST、Binance klines、Deribit IV | ✅ 可用 |
| 数据 | 行情 WSS / 用户 WSS | ❌ 未接（全项目无 websocket 代码） |
| 数据 | 新闻 RSS + GDELT + FinBERT 情绪 | ⚠️ 分钟级、离线 |
| 信号 | 碰价概率定价（反射原理/数字期权复制） | ✅ `bybit_pm_monitor.py` |
| 信号 | 美债-美元公允价偏离、多因子评分 | ✅ 全球情报中心 |
| 风控 | ATR 动态仓位 | ⚠️ 公式有，无实盘联动 |
| 执行 | PM `polymarket-client`（下单API已通） | ⚠️ 未实盘测过下单 |
| 执行 | Bybit 腿：Hummingbot | ❌ 定位错误（见 P1-6） |
| 资金 | 充提闭环、owner key + relayer key | ✅ 已验证 |
| 运维 | 15s 监控 + 5min hedge 检查 + cron 看门狗(6h) | ⚠️ 恢复路径慢 |

---

## 2. 缺陷清单

### 🔴 P0 — 不修不能实盘

1. **采样率与策略周期错配**
   `bybit_pm_monitor.py` 默认 `--interval 15`（15 秒一轮 HTTP 轮询），而策略目标是「秒级错价」。错价窗口 < 采样周期时：要么系统性漏掉机会，要么拿到的是过期价格、撮合时机会已消失 → 正期望变负期望。**必须上 WSS 推送**（`wss://ws-subscriptions-clob.polymarket.com/ws/market`）。

2. **执行安全机制缺失（最大风险）**
   下单 API 已通，但直接裸下单是危险的。实盘前必须有：
   - 订单状态机（NEW→LIVE→FILLED/CANCELED→SETTLED，含部分成交）
   - 幂等键（client_order_id）防重复下单
   - 熔断器：连续 N 次异常/滑点超标 → 停止该策略
   - 硬风控：最大持仓、单市场敞口上限、日亏损上限、**全局 kill switch**
   - PM 结算异步（`wait_for_order_fill_settlement`）期间仓位不可用的状态处理

3. **端到端延迟无预算**
   实测 Bybit REST TTFB ~220ms（都柏林），PM CLOB 25–30ms；对冲两腿串行 + 决策时间必须 < 错价寿命。目前无 WSS、无 keep-alive 优化、无预签名，链路总延迟未实测。

4. **密钥分级未完成**
   owner 私钥（可提现）在服务器 `~/polymarket/pm_owner_key.txt`。做大资金前必须：授权**会话密钥**（scope=CLOB、180 天、不可提现）给交易进程，owner key 撤回冷存储。当前 beta 需 Builder 凭证（builder@polymarket.com）——**现在就发申请邮件**。

5. **运维自愈薄弱（已有实测证据）**
   2026-09-22 01:37 服务器重启，监控进程直到 03:13 才恢复（丢失 ~1.6 小时监控；看门狗 6 小时周期太慢，且恢复路径不明确）。要求：systemd 服务（Restart=always）、NTP 校时、监控进程死亡秒级告警（Telegram）。

### 🟡 P1 — 实盘前应修

6. **Hummingbot 定位错误**：容器化 GUI 导向的 bot，不适合亚秒级对冲腿。Bybit 腿应直连官方 API：keep-alive 连接池 + WSS 私有流 + 预签名 + 批量撤改单。

7. **Python 热路径局限**：GIL + 同步 HTTP。交易引擎（下单/撤单/状态机）用 Rust/Node/Go；Python 保留在研究层（定价、扫描、日志分析）。

8. **事件驱动管道缺失**：RSS 轮询 + GDELT 离线 → 新闻到信号的转化是分钟级，而「事件错价」窗口常是秒~分钟级。**TypeSafe AI 正好填这个坑**（见 §4 集成点 1）。

9. **无回测**：σ、阈值、仓位系数全部静态。需要历史回测框架 + 参数敏感性分析 + regime 自适应。

10. **PM 腿成交后的结算延迟未纳入模型**：成交≠可用，`wait_for_order_fill_settlement` 期间对冲腿必须容忍暴露。

### 🟢 P2 — 打磨

11. CSV 日志 → 结构化（JSONL）+ Prometheus 指标 + Telegram 告警
12. 本地（中国）与服务器代码同步（scp 手动的隐患）
13. 费率假设复测（$25/$100 档提现）、`Poly-RateLimit-*` 监控
14. 时钟校时（chrony）、时区统一 UTC

---

## 3. TypeSafe AI 实测数据（2026-09-22）

| 项 | 值 |
|---|---|
| 端点 | `POST https://api.typesafe.ai/v1/systemone` |
| 认证 | `Authorization: Bearer <key>`（key 已存服务器 `~/polymarket/.typesafe_key` 600） |
| 模型 | `jev-latest` → 响应 `jev-1.13.0` |
| 首包 TTFB | **425–450ms**（DNS 1-4ms + connect ~120ms + TLS ~243ms + 推理 ~205ms） |
| 稳态延迟（keep-alive） | **≈250ms/次**（TLS 与连接摊销） |
| 输出 | 校准概率 + 置信度（实测：choice 三档概率 0.62/0.30/0.08，conf 0.43；score 1.6 conf 0.57） |
| 用量 | 3 题约 input 408 tok / output 61 tok |
| 注意 | TLS 243ms 说明 API 落点距都柏林较远（推断美国）；对延迟敏感可测试自定义域名/区域（若产品支持） |

**定位结论**：Jev 适合「窄决策 + 校准概率 + 置信门控」，一次调用 ~250ms → 只能用于**事件级/分钟级**决策，绝不能放在逐笔/逐秒热路径上。

---

## 4. 集成架构：四层模型

```
L0 确定性内核（纯代码，微秒级）  定价(碰价/公允价)·风控·订单状态机·执行路由
        ▲ 永不调用外部AI            │
L1 信号层（本地，秒级）          多因子评分·公允价偏离·ATR·错价扫描(WSS)
        ▲                          │ 候选信号 + 市场快照
L2 决策层（TypeSafe，~250ms/次） 事件解释·信号融合二审·regime授权
        ▲ 置信门控/超时降级         │
L3 研究层（离线）               日报·复盘·参数建议·回测
```

**铁律**：
1. Jev 只回答「窄问题」，代码永远掌控执行；
2. 置信度 < 阈值 → 不行动（confidence-gated routing）；
3. 调用超时（250ms）→ 自动降级走 L1 默认逻辑，**永不阻塞下单路径**；
4. 所有 Jev 调用异步化（fan-out 批量提问、单调用多问题）。

### 集成点 1：新闻事件 → 结构化信号（事件驱动错价）⭐ 最高价值
```
state   = {标题, 摘要, 时间, 相关市场快照列表}
questions = {
  impact:    choice  影响方向 {up / down / no_impact}
  magnitude: score   幅度档 [0 忽略 / 1 小 / 2 中 / 3 大]
  affected:  noul    "是否会影响 [市场X]"  ← fan-out 多市场各问一次
}
→ L1 拿到 {市场, 方向, 幅度, prob, conf}，在事件窗口期内重点扫描该市场错价
```

### 集成点 2：信号融合置信门控（防错杀/防盲从）
```
L1 候选信号（如 公允价偏离 >2σ、碰价价差 >阈值）打包成 state →
  tradeable: noul  "该信号是否来自真实错价而非数据噪声/过期报价"
  hidden_risk: choice {无 / 流动性风险 / 事件风险 / 数据风险}
→ 仅 conf ≥ 0.7 且 tradeable 概率 ≥ 0.6 才放行 L0
```

### 集成点 3：波动率 regime 授权（动态风控）
```
state = {各市场 realized vol、IV 分位、错价频率统计}
  regime: score [0 平静 / 1 常态 / 2 抬升 / 3 极端]
  reduce: noul  "当前是否应降低敞口"
→ regime 输出映射 ATR 仓位系数（如 [1.0, 0.8, 0.5, 0.3]），每 15 分钟评估一次
```

---

## 5. 延迟预算（分场景）

| 场景 | 路径 | 预算 | Jev 参与 |
|---|---|---|---|
| 秒级错价对冲 | L0+L1（WSS+预签名+keep-alive） | <300ms 全链路 | ❌ |
| 事件驱动（新闻） | 事件→Jev(~250ms)→定向扫描→下单 | 1–2s（分钟级窗口） | ✅ |
| 周期再平衡/风控 | L1 信号 → Jev 二审 → L0 | 分钟级 | ✅ |
| 盘后/日报 | L3 | 离线 | ✅ |

---

## 6. 实施路线

| Phase | 内容 | 验收标准 | 周期 |
|---|---|---|---|
| **0** | 系统加固：systemd 守护监控、NTP、Telegram 告警、JSONL 日志；发 Builder 申请（会话密钥） | 杀进程 5s 内自愈+告警；重启秒级恢复 | 本周 |
| **1** | 数据层 WSS 化：PM market WS + user WS；Bybit 直连（keep-alive+预签名） | 错价延迟从 15s 降到 <1s；延迟分项埋点 | 2 周 |
| **2** | TypeSafe 集成：`typesafe_decision` 模块 + 三个集成点；新闻事件源（Webhook/RSS 秒级） | 事件→信号 端到端 <2s；置信门控生效 | 3 周 |
| **3** | 执行层：订单状态机+熔断+风控（Rust/Node）；纸面交易 2 周 | 异常注入演练通过；无重复单/裸奔单 | 4 周 |
| **4** | 回测校验 + 参数自校准 + $25 小实盘 → 扩容 | 回测夏普/回撤达标；实盘 1 周误差<预期 | 持续 |

---

## 7. 附录：TypeSafe API 速查

```bash
# 端点与认证
POST https://api.typesafe.ai/v1/systemone
Authorization: Bearer <apikey_...>

# 请求体
{"state": <文本或JSON>, "model": "jev-latest",
 "questions": {"name": {"type": "noul|choice|score",
                          "instructions": "...", "criteria": {...或[...]}}}}

# 响应
{"answers": {"name": {"choice"/"score"/"noul": <值>,
                       "confidence": 0-1, "probabilities": {...}}},
 "usage": {"input_tokens": N, "output_tokens": N}}
```

```bash
# Python SDK（服务器 venv）
pip install typesafe-sdk        # 读 TYPESAFE_API_KEY 环境变量
from typesafe_sdk import TypeSafeClient, Noul, Choice, Score
```

**凭据位置**（服务器）：`~/polymarket/.typesafe_key`（600）
**实测脚本**：`~/polymarket/typesafe_decision.py`（三个集成点的模板封装 + 延迟日志 + 超时降级）
