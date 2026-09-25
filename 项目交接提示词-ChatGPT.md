# Moneybot 多用户对冲套利交易平台 — 项目完整交接提示词

> 将本文件全部内容复制粘贴给 ChatGPT（或任意 AI 编程助手）即可让其快速理解本项目并接手开发/审查/维护。**粘贴前请删掉或替换第 10 节中的占位符任务描述。**

---

你是 Moneybot 项目的接手工程师。这是一个已上线运行的多用户量化对冲套利交易平台，请先通读以下项目全貌，再执行第 10 节的【本次任务】。

## 1. 项目定位

- **一句话**：多用户「现货×永续基差套利（cash-and-carry）」+「预测市场对冲」的 Web 交易平台，带 AI 策略助手与全托管自动交易。
- **商业模式**：免费套餐（模拟交易）→ 专业版 $30/月 → 实盘版 $99/年；用户 USDT 充值（TRC20）后自动开通实盘，额度=账户余额；平台统一密钥执行实盘下单，加密托管。
- **用户规模**：小规模多租户（当前个位数用户，uid=27 为长期测试租户，uid=1 为 admin）。

## 2. 技术栈与架构

```
用户浏览器 (移动端 m.html / 管理后台 admin.html)
   │ HTTPS 8443 (nginx → 日本跳板转发 → 都柏林)
   ▼
FastAPI 主服务 pm-dash (dash/app/main.py, 127.0.0.1:8080, systemd)
   ├── REST API (/api/*) + SSE 实时流 (/api/stream/depth, /api/support/stream)
   ├── 后台线程: ai_tasks.tick (每60s) + 定时任务 (pm-funds oneshot 对账)
   ├── 数据层: DuckDB+Parquet (行情/K线/成交) + SQLite (users.db 用户/资金, support.db 客服)
   └── 模拟执行: paper_ops.py (纸面持仓 carry_state.json, 租户隔离 tenants/<uid>/)
Bybit WSS 桥 pm-bridge (bybit_ws_bridge.py): tickers/kline/orderbook/成交 100ms 级 → SSE
AI 层: DeepSeek (战略巡检/对话, .ai_secrets.json) + TypeSafe Jev (窄决策/概率门控, .typesafe_key)
```

**核心铁律**：模型只做判断题，代码只做执行题，**模型永不直接触钱**——AI 输出概率+置信度，代码按门控阈值决定动或不动；AI 变更类工具强制三闸（白名单参数闸→预览闸→人审批准闸）。

## 3. 目录与核心文件（本地 Windows `D:\Program Files\hermes\polymarket_arb`，服务器 `/home/ubuntu/polymarket`）

| 文件 | 职责 | 行数 |
|---|---|---|
| `dash/app/main.py` | FastAPI 全部 REST/SSE 端点、路由挂载 | ~2000 |
| `paper_ops.py` | 纸面交易执行：open_hedge/close_both/裸腿/孤儿/funding 结算/止盈止损 | 1169 |
| `ai_tasks.py` | AI 巡检调度：tick 60s、hourly_inspect、DeepSeek 流式 tool_calls、B 级 [SUGGEST] 自动应用 | 445 |
| `autopilot.py` | 全托管：任务状态机 + 七闸风控执行器 + 授权分级 A/B/C | 409 |
| `jev_engine.py` | TypeSafe Jev 窄决策调用与门控阈值 | 139 |
| `dash/app/funds.py` | USDT 充值/提现/余额/套餐（唯一金额匹配、三方核对） | 830 |
| `dash/app/support.py` | 客服系统（SQLite 三表 + SSE 推送 + 图片鉴权 + TG 告警） | 376 |
| `dash/app/users.py` | 用户/验证码(SQLite)/审计 | 350 |
| `dash/static/m.html` | 移动端单文件（390px 优先，含 K 线 SVG/盘口/AI 浮窗/客服/托管中心） | ~3600 |
| `dash/static/admin.html` | 管理后台（用户/资金/费率/营收/客服/托管） | ~900 |
| `bybit_ws_bridge.py` | Bybit WSS 行情桥 | — |
| `carry_engine.py` | carry 策略主循环（day_pnl 跨天重置在此） | — |
| `live_exec.py` | 实盘下单执行（Bybit UTA） | — |
| `tenants.py` | 租户隔离上下文管理器 | — |

## 4. 关键业务规则（踩坑铁律，违反必出 bug）

1. **租户隔离**：凡读写 paper_ops/autopilot/jev_gate state 的 API 与后台循环，必须 `with tenants.tenant(uid):` 包裹；`tenants.base(uid)` 对 admin 返回 ROOT、非 admin 返回 `ROOT/tenants/<uid>`；后台全局循环（如 tick 结算）必须遍历所有用户逐租户包裹，否则只作用到 admin。
2. **Bybit 实盘铁律**：现货市价单 `marketUnit: baseCoin`；现货持仓读 `wallet-balance` 的 `coin.walletBalance`；实盘统一走平台密钥 `funds.platform_key()`。
3. **spot-only 铁律**：现货独占标的绝不能由 linear 端点订阅盘口；K 线/行情按 `category=linear|spot` 分通道。
4. **登录铁律**：前端 `api()` 对 401 拦截跳登录，登录/注册请求必须 `noAuth:true`；验证码已持久化到 SQLite（重启不失效），一次性/错 5 次销毁/5 分钟过期。
5. **内存对象保存坑**：改完内存中的任务/状态列表后，保存必须用**同一引用**（`_save_tasks(uid, tasks)`），绝不能 `_save(_load())` 重读磁盘覆盖修改。
6. **AI 输出注入**：AI 返回的 `[SUGGEST] key=value` 微调参数，必须过白名单范围校验（open_p∈[0.5,0.9] 等）才能写 jev_gate.json，且原子写（tmp+os.replace）。
7. **资金顺序**：扣款/设套餐类操作必须「先校验余额→扣款→设套餐→失败回滚」，严禁先设后查（曾被审计出零成本拿高级套餐的越权 bug）。
8. **时间格式**：服务器返回 ISO 带 `Z` 后缀，前端格式化函数不得再拼 `+'Z'`（会 Invalid Date）；SVG 几何属性（x/y/width/height）必须纯数字，禁止混入 `px` 单位（iOS Safari 会忽略导致蜡烛图空白）。
9. **客服会话模型**：每用户一个活跃会话（resolved 后再发=新会话），历史全保留；未读双端计数；图片 5MB/白名单/鉴权读取。
10. **全托管七闸顺序**：授权→白名单→单标的持仓上限→总仓位（任务内计数，勿用全局持仓跨任务污染）→日损熔断（day_pnl 跨天必须归零）→费率翻转保护→余额闸。

## 5. 部署拓扑

- **本地**：Windows 10，git 仓库主分支；Playwright 真机验证环境 `E:\playwright_env\`。
- **服务器**：都柏林 AWS（ubuntu），pm-dash/pm-bridge 为 systemd 服务；nginx 8443（proxy_read_timeout 300s，SSE 适配）；部署方式 `scp` + `sudo systemctl restart pm-dash`。
- **访问**：看板 `https://moneybot.openedskill.com:8443/`（移动端 `/m`，管理 `/admin`）；经日本跳板转发。
- **数据管线**：UTC 存储，前端本机时区渲染。

## 6. 当前系统状态（截至 2026-09-25）

- 服务全 active；测试租户 uid27 处于 pro 套餐（Jev 巡检数据持续积累）；托管任务全部已取消（测试后复位）。
- 已交付里程碑：M1-M17 营收模型、M-A AI 助手、M-D DeepSeek×Jev 组合策略、M-P 全托管、M-Q 全局 AI 悬浮浮窗、M-S 客服系统（含 SSE 实时+TG 告警+图片）、模拟余额管理、VIP 费率取消、全面审查修复（11 严重+8 中等已修）。

## 7. 遗留 backlog（可指派给接手者）

1. `funds.py review_withdraw`：Bybit 打款 HTTP 调用持全局锁，审批期间阻塞全部资金操作——需两阶段重构（锁内读单→锁外打款→锁内写状态）。
2. `live_exec.py`：实盘 bybit_open_hedge 未检查已有持仓（可重复开仓）；手续费扣款不校验余额（可变负）；VIP 费率与模拟盘口径不一致。
3. `paper_ops.py close_pm` 与 `pm_sell_shares` 退出定价不一致（一个减 TICK 一个不减）。
4. 优化项：K 线拖拽边界计算抽取公共函数；触控目标加大至 44px；后台循环异常留痕（现多处 except pass）；充值轮询加取消机制；未登录不建 SSE（已修）。

## 8. 工作约定

- 交付语言中文；改动必须真机验证（Playwright headless Chrome，移动端 390×844 / 桌面 1440×900）；验证脚本放 `E:\playwright_env\`。
- 每完成一个修复：本地 git commit → scp 到服务器对应路径 → 重启 pm-dash → 服务器侧 git commit 同步。
- 安全红线：所有密钥（DeepSeek/TypeSafe/平台密钥）在服务器 `600` 权限文件中，**绝不写入代码、git 或任何提示词**；AI 变更类工具三闸不可绕过。

## 9. 说明

- 本项目是真实资金系统，任何涉及扣款/下单/结算的修改必须先静态推演再小金额实测。
- 若你（接手 AI）无法访问服务器，可基于第 3 节文件职责与第 4 节铁律做纯代码层审查与修复建议。

## 10. 本次任务

> 【请在此填写你的具体请求，例如：】
> - 请审查 paper_ops.py 的 funding 结算与平仓入账逻辑，找出潜在边界 bug
> - 请完成 backlog 第 1 项 review_withdraw 两阶段锁重构，并给出完整 diff
> - 请为 /api/carry 增加日终对账报告功能
>
> （示例）本次请求：**请先通读全部铁律，然后完成 backlog 第 2 项实盘通道的三处修复（重复开仓检查、手续费余额校验、VIP 费率口径统一），输出可直接部署的修改方案。**
