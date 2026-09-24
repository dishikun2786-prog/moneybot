# R13c/d 完整开发计划：PM 全线下线 + 4 标的收敛 + 稳定性强化

状态：**R13c 已全部执行上线**；R13d 为后续迭代计划。
定案标的：**BTCUSDT / ETHUSDT / XAUUSDT / XAGUSDT**（Bybit 实测存在，tick 0.01 美元/盎司，linear USDT 永续；现货仅 BTC/ETH 有）

---

## 一、R13c 已完成（2026-09-24 上线）

### 1. PM（预测市场）功能全线下线
| 项 | 动作 | 状态 |
|---|---|---|
| pm-clob / pm-wss / pm-monitor 服务 | stop + disable（重启不再拉起） | ✅ |
| 移动端底部「🎲 预测」tab | 注释关闭 | ✅ |
| 移动端管理后台 PM 子 tab | 注释关闭 | ✅ |
| 桌面版管理后台 PM tab | 注释关闭 | ✅ |
| 行情屏「PM 机会区」 | 关闭渲染 | ✅ |
| 交易屏「预测市场对冲」引擎模式行 | 注释关闭 | ✅ |
| /api/pm/tokens 浏览搜索 | 返回空 + off:true | ✅ |
| 保留 | screen-pm DOM、PM 引擎代码、pm_admin 状态库（可回滚） | ✅ |

### 2. 数据标的收敛为 4 个
| 项 | 动作 | 效果 |
|---|---|---|
| bybit_ws_bridge.py | SYM_WHITELIST 白名单（BYBIT_SYMS 环境变量可配） | ticker/kline/orderbook/成交全部只订阅 4 标的 |
| /api/instruments | 白名单过滤 | linear 4 个 + spot 2 个（实测 ✓） |
| SSE 价格流 | 随桥收敛 | 1132 标的 → 4 标的（bybit_prices.json 850 字节）|
| 金银档位 | XAU/XAG tick 0.01、大单阈值 200 盎司/1 万盎司 | ✅ |

### 3. OOM 根因修复（今天两次宕机的原因）
| 项 | 根因 | 修复 |
|---|---|---|
| 服务器 2GB swap | 1.9GB 内存无 swap | swapfile 持久化 + swappiness=10 ✅ |
| paper_engine 尾部读 | 52MB CSV 全量读→423MB 内存 | 只读尾部 512KB（回归测试 test_tail_snapshot 通过）✅ |

**实测效果**：内存 1.3GB→832MB 占用、可用 1.0GB；进程从 7 个大户降到 1 个（data_writer 360MB，下轮优化）。

---

## 二、R13d 后续开发计划

### P1：金银前端体验与交易（1-2 天）
1. **中文名**：XAUUSDT→「黄金」、XAGUSDT→「白银」、BTC/ETH 保留原名（行情/详情/持仓全链路）
2. **行情屏**：4 标的加大卡片展示（大币种布局），金银显示「美元/盎司」单位
3. **金银下单验证**：交易屏 linear 永续下单 XAU/XAG（qtyStep 0.001 盎司——前端数量输入步进适配），纸面+实盘全链路
4. **详情屏**：金银 K线/盘口验证（bridge 已订阅，前端 detCat=linear 自动走通，需实测）

### P2：data_writer 内存优化（半天）
- 360MB 大户：查 parquet 读取路径，改为 DuckDB 投影/流式（同 paper_engine 尾部读思路）
- 目标：<150MB

### P3：磁盘清理（1 小时）
- books_1s.jsonl 5.7GB（PM 盘口留痕，pm-wss 已停写）：压缩归档到 E 盘或删除（**需您确认**）
- 顺带清理 monitor_stdout.log 6.8MB 等日志轮转

### P4：引擎策略适配（2-3 天，需回测）
- paper_engine/carry_engine 金银参数：XAU/XAG 波动率、基差结构与 BTC/ETH 不同，需独立参数组（theta/ATR 阈值/持仓上限）
- 金银无现货（只有永续）→ 基差套利不适用金银，波段/趋势策略适用
- 回测 26h+ 数据后评估（沿用 swing_score 模式：默认关，达标再开）

### P5：测试同步（半天）
- test_pm_units / test_pm_admin：标记 PM-OFF（跳过或断言返回空）
- 新增 test_symbols_wl.py：断言 instruments/SSE 白名单收敛、PM API 空响应（防回归）

### P6：稳定性监控（半天）
- 内存水位告警：systemd 或 cron 每 5 分钟查 available<300MB 时 TG 告警（复用 TG 通道）
- pm-dash/pm-bridge 崩溃自动重启已由 systemd 保障 ✓；补一个「连续崩溃 3 次告警」

---

## 三、可回滚性
- PM 所有代码/服务文件保留（注释标记 PM-OFF-R13c 或 disable 状态）
- 回滚：`systemctl enable --now pm-clob pm-wss pm-monitor` + 还原 m.html/admin.html/main.py 注释块 + `BYBIT_SYMS=` 清空恢复全量订阅

## 四、建议确认项
1. books_1s 5.7GB 数据：压缩归档 or 直接删除？
2. P4 引擎策略：金银是否需要自动交易，还是仅行情展示+手动下单？
3. 回滚保留期：PM 代码保留多久后可以彻底清理？
