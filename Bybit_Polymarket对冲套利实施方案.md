# Bybit × Polymarket 量化对冲套利 · 实施方案（Hummingbot 路线）

> 生成：2026-09-22 | 全部数据当日实测 | 服务器：AWS Lightsail 都柏林 (34.253.194.97)

---

## 0. 结论先行（TL;DR）

| 问题 | 结论 |
|---|---|
| Hummingbot 能直接交易 Polymarket 吗？ | **不能**——已扫描其仓库全部文件树：有 `bybit` / `bybit_perpetual` 连接器，**无任何 Polymarket 连接器** |
| 正确架构是什么？ | **Hummingbot 管 Bybit 腿（执行/持仓/账户）＋ 官方 `polymarket-client` SDK 管 PM 腿 ＋ 自研信号引擎**（监控已上线） |
| 策略可行吗？ | 可行，但**不是"免费午餐"型套利**。静态截面上流动盘定价公允（vs 已实现波动率 ±几 vol 点）；利润来源＝ ①波动放大瞬间的错价直取＋永续对冲 ②波动率报价做市（价差+返佣）③桶间相对价值 |
| 延迟格局 | PM CLOB **25-30ms** ✓ ｜ Bybit **~220ms** ⚠️（对冲腿约束）｜ Deribit 52ms ✓ → 引擎放都柏林；纯毫秒级延迟战不适合本组合 |
| 起步状态 | **Phase 0 监控已 7×24 上线**（60秒周期，CSV 持续积累）；下一步＝标定波动率锚 + 全流程小额验证 |

---

## 1. 关键核实结果（今日实测）

### 1.1 Hummingbot 支持情况
- ✅ `bybit`（现货）、`bybit_perpetual`（永续）原生连接器齐全
- ❌ **无 Polymarket 连接器**（grep 全仓库 0 匹配；Kalshi 也没有）
- 两条集成路线：
  - **路线 A（推荐）**：Hummingbot 脚本进程内集成——自定义 `pm_bybit_hedge.py` 脚本同时持有 Bybit 连接器（HB 内）+ `polymarket-client`（pip 装进容器）。单进程、低延迟、易部署
  - 路线 B：独立大脑进程 + hummingbot-api REST 调用（解耦但多一跳）

### 1.2 延迟矩阵（都柏林 → 各交易所）
| 目标 | TTFB | 说明 |
|---|---|---|
| Polymarket CLOB | 25-30ms | 主力优势 |
| Bybit API | **212-258ms** | 新加坡/东京机房，对冲腿的物理约束 |
| Deribit API | 52-55ms | 期权 IV 数据源可用 |

### 1.3 结算规则（已逐条核实）
- 市场类型＝**碰价期权（one-touch）**：
  - 「↑ K」：月内任一 **Binance BTC/USDT 1分钟K线 High ≥ K** → 立即结算 YES
  - 「↓ K」：任一 1分钟K线 Low ≤ K → YES
- 结算源＝**Binance**（注意：不是 Bybit！对冲基差存在但可控）
- 事件周期：月（如"in September"→9月30日）、年（"in 2026"→12月31日）、周（如"September 21-27"）

### 1.4 费用结构（套利的成本基准）
| 费用 | 数值 |
|---|---|
| PM taker | `rate × p × (1-p)`，crypto 类 rate=0.07 → p=0.3 时 ≈ 1.47c/股（名义的4.9%），p=0.1 时 0.63c（6.3%） |
| PM maker | **0 手续费** + 20% 返佣（按市场份额每日结算） |
| Bybit 永续 | 标准用户 taker 0.055% / maker 0.02%（以官方费率页为准） |
| 资金费率 | 约 0.01%/8h（≈10.9%/年，随行情波动） |

---

## 2. 策略设计（三条，按优先级）

### S1 错价对冲（Event-time Relative Value）——核心策略
**逻辑**：模型公允价（碰价概率）vs PM 盘口；错价超阈值 → 吃 PM → 立即 Bybit 永续对冲 Delta。

```
公允价 P = touch_prob(S, K, σ, T, direction)     ← 反射原理公式（已实现于监控脚本）
错价     = P_model − PM_ask − fee(ask)           ← 扣费净edge，单位：美分/股
对冲量   = 股数 × ∂P/∂S（数值微分）              ← BTC计价的永续空/多
```

**执行纪律（硬性）**：
1. 净 edge ≥ 3c（覆盖双边费用 + 对冲滑点 + 缓冲）
2. 触价档位流动性 ≥ 100 股（监控已内置该门槛，过滤 10 股级死盘噪音）
3. 对冲腿死区 0.0005 BTC，超过立即调仓；单桶对冲上限硬编码
4. **公允价的波动率锚必须标定**：监控同时记录三源（Deribit IV / 已实现7d / PM隐含），回测确定 PM 实际跟踪哪个锚，再交易对该锚的偏离

**成本预算（单笔示例，BTC↓80000 / p=0.17 / 300股）**：
- PM 吃单费：0.07×0.17×0.83 = 0.99c/股 ≈ $2.97
- 对冲往返（Delta≈0.13 BTC/1k股 → 300股≈0.039 BTC ≈ $3.3k 名义）：taker 0.11% ≈ $3.6
- 合计 ≈ $6.6 ≈ 名义(51美元)的 13% → **必须吃到 ≥ 3c/股以上的错价才有利可图**（p=0.17 时 3c = 名义的 17.6%）

### S2 波动率报价做市（Hedged Market Making）——规模化后主力
- 以「模型公允 ± spread」双边挂单（**maker 0 费 + 20% 返佣**）
- 聚合 Delta 阈值对冲（净敞口 > 死区 → Bybit 调仓），而非逐笔对冲（省手续费）
- 库存管理：单桶上限、事件前加宽价差、到期前 24h 减仓近价桶（pin risk）
- 目标市场：BTC/ETH 月度/年度阶梯桶（1-3c 价差、日成交百万级）

### S3 桶间相对价值（Ladder RV）——低风险补充
- 同一事件单调性检查：`P(↑85k) ≥ P(↑87.5k) ≥ P(↑90k)…`，违反且超费用 → 多便宜桶/空贵桶
- 组合净 Delta 小（近似价差结构）→ 对冲成本低、资金效率高
- 监控的 CSV 已含同事件全阶梯同屏数据，可直接离线找违反样本

---

## 3. 系统架构

```
┌────────────────────────── 都柏林服务器 (34.253.194.97) ──────────────────────────┐
│                                                                                  │
│  [已部署] bybit_pm_monitor.py (60秒周期, 7×24)                                    │
│     ├─ Bybit 永续价格+已实现波动率                                                │
│     ├─ Deribit 期权 IV (期限插值)                                                │
│     ├─ Polymarket 盘口 (gamma/clob)                                              │
│     └─ 输出: 错价/PM隐含IV/对冲Δ → logs/bybit_pm_fv.csv (持续积累)                │
│                                                                                  │
│  [待部署] pm_bybit_hedge.py (Hummingbot 脚本)                                    │
│     ├─ bybit_perpetual 连接器 → 对冲腿执行 (订单/持仓/账户)                        │
│     ├─ polymarket-client → PM 腿执行 (盘口/下单)                                  │
│     └─ 风控: 敞口上限/死区/熔断                                                   │
│                                                                                  │
└──────────────────────────────────────────────────────────────────────────────────┘
        │ 监控/调参 (SSH)                    │ 资金                     │ 链上
   中国桌面 (只读观察)              Bybit 账户 (USDT 保证金)      PM 钱包 (pUSD)
                                                              └ Session Key 热签名
```

---

## 4. Hummingbot 落地步骤（命令级）

```bash
# 1) 服务器安装 Hummingbot (Docker)
mkdir -p ~/hummingbot_files && cd ~/hummingbot_files
docker run -it --name hummingbot --network host \
  --mount "type=bind,source=$HOME/hummingbot_files,destination=/home/hummingbot" \
  hummingbot/hummingbot:latest

# 2) Hummingbot 内: 连接 Bybit 永续
connect bybit_perpetual
#   API Key 权限: 只开 "Contract Trade" + "Read"；IP 白名单填 34.253.194.97
#   不要开提现权限！

# 3) 安装 PM SDK (容器内)
# pip install polymarket-client

# 4) 放入脚本并启动
# cp ~/polymarket/pm_bybit_hedge.py ~/hummingbot_files/scripts/
# start --script pm_bybit_hedge.py
```

**当前已附交付物**：`hummingbot_pm_hedge_sketch.py`（参考骨架：信号→PM下单→Delta对冲→死区调仓 的完整骨架，含 TODO 标记的接入点）

---

## 5. 资金流转（首次 $50-100 演练）

```
Bybit (USDT)  --提现-->  网络: TRC20(推荐, 最低$9, 费~$1) 或 Polygon(确认支持后可$2)
              -->  Polymarket 桥接地址 (官方UI/API生成)  --> 自动兑换为 pUSD
回程: PM pUSD --桥接--> USDT (Polygon) --> Bybit 充值
```

**演练清单**（逐项打勾）：
- [ ] Bybit 提现 $50（TRC20）到 PM 桥地址 → 确认 pUSD 到账
- [ ] PM 手动买 1 笔小额桶（~$10）→ 确认成交
- [ ] 手动平/等到期 → 提现 $10 回 Bybit → 全流程闭环
- [ ] 记录各环节到账时间与费用（写回本文档）

---

## 6. 分阶段路线与验收

| 阶段 | 内容 | 验收指标 |
|---|---|---|
| **Phase 0（已完成✅）** | 监控 7×24 采集 + 结算规则/延迟/费用核实 | CSV 连续 72h 无断；收盘价对账通过 |
| **Phase 1（1-2周）** | ①波动率锚标定（PM隐含σ 对三源回归）②纸上对冲（记录假设成交 vs 实际盘口）③HB 环境 + Bybit API key + $50 资金演练 | 锚定误差 <1 vol 点；资金闭环通过 |
| **Phase 2（2-4周）** | S1 实盘 $500-2000，1-2 个桶，半自动（信号+人工确认） | 连续 2 周净正值（扣全部成本）；对冲滑点实测 < 预期1.5× |
| **Phase 3（1-2月）** | S2 做市自动化（3-5桶）+ S3 RV 模块；升档 Copper/Bronze | 月化净收益（情景骨架：保守2-4% / 基准5-10%）；回撤 < 5% |
| **Phase 4** | 扩容 + （可选）Bybit 就近节点（东京/新加坡）降对冲延迟 | — |

---

## 7. 风险清单

1. **对冲滞后 ~220ms**：极端行情跳空时对冲成本放大 → 净敞口上限 + 波动熔断（暂停吃单）
2. **结算基差**：PM 用 Binance、对冲用 Bybit 指数 → 极端熔断/插针时背离 → 避免到期前 12h 重仓近价桶
3. **锚定错误**：公允价依赖波动率锚；锚错→假信号（监控三源交叉验证，Phase 1 标定）
4. **流动性陷阱**：10 股级挂单是噪音（已被 100 股门槛过滤）；吃单前必须看档位深度
5. **费用吞噬**：PM taker 费率高（名义 3.5-6.3%）→ 能 maker 不做 taker；净 edge 门槛硬性
6. **资金费率**：对冲腿持仓期间付费/收费随行情翻转 → 计入预算
7. **Pin risk**：临期临近价位的 Delta 爆炸 → 硬性减仓规则
8. **平台/合规**：Bybit API key 最小权限 + IP 白名单；PM Session Key（不可提币）；资金分散两平台
9. **运营**：监控进程/采集断档告警；密钥备份；日志审计
10. **模型风险**：反射原理假设 GBM——尾部/跳跃时刻公允价失真 → 大波动期提高门槛或暂停

---

## 8. 诚实结论（当前市场读数）

- **静态截面**：流动盘（≥100股）PM 定价 vs 已实现波动率 ≈ 公允（±几 vol 点）；"大错价"全部出现在 10 股级死盘（不可交易）
- 监控显示 **PM 隐含σ 更接近短端已实现波动率，而非远处 Deribit IV** → Phase 1 将用回归确定"PM 实际锚"
- 与上期结论一致：**利润在波动时刻（错价窗口）与做市微结构，而非静态捡漏**
- 数据已经在上线采集（60秒/轮，全天候）——下次加密急动/美联储事件时会自动记录真实错价样本，用数据决定资本分配

---

## 附录：文件清单

| 文件 | 位置 | 说明 |
|---|---|---|
| bybit_pm_monitor.py | 服务器 ~/polymarket/ + 本机 | 信号引擎（7×24 运行中, PID 11312） |
| start_monitor.sh | 服务器 ~/polymarket/ | 监控启停脚本 |
| logs/bybit_pm_fv.csv | 服务器 ~/polymarket/logs/ | 错价全量数据（持续积累） |
| hummingbot_pm_hedge_sketch.py | 本机 | HB 对冲脚本骨架 |
| scan.py / scan2.py | 服务器 ~/polymarket/ | 全站套利/做市扫描器 |
| Polymarket低延迟套利完整方案.md | 本机 | 上期总体方案 |
| 本文件 | 本机 | 本期实施方案 |
