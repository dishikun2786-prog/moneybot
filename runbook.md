# PM×Bybit 量化栈 Runbook（运行手册 v1）

> 2026-09-22 · 每个告警对应处理步骤；强制停机条件成文。位置：服务器 ~/polymarket/docs/ 亦同步本地。

## 告警 → 动作映射

| 告警 | 含义 | 动作 |
|---|---|---|
| pm-monitor 服务非 active | 监控进程死了且 systemd 未拉起 | `ssh 服务器 'sudo systemctl status pm-monitor; journalctl -u pm-monitor -n 50'`；手动 `sudo systemctl restart pm-monitor`；连续3次→检查磁盘/内存/代码 |
| 监控疑似重启循环(+N) | 进程反复崩溃 | 看 `logs/monitor_stderr.log` 末尾；若为代码错误→`bash deploy.sh --rollback`；若为外部API故障→等恢复(systemd会继续拉起) |
| CSV 超过5分钟未更新 | 数据管道停滞 | 看 `logs/monitor_stdout.log` 是否有 [warn]/[err]；检查网络 `curl -s https://clob.polymarket.com` |
| books_1s 超过5分钟未更新 | WSS 数据停滞 | `systemctl status pm-wss` + `tail logs/wss_stdout.log`（重连循环会打印断开原因） |
| 磁盘占用 >85% | 空间不足 | `du -sh ~/polymarket/data/* ~/polymarket/logs/* | sort -rh | head`；清理旧日志；book_1s 保留期外删除 |
| 可用内存 <200MB | OOM 风险 | `free -m`；重启非核心服务（pm-wss）；考虑扩实例 |
| (实盘后) 对账不平 | 本地记录≠交易所≠链上 | **立即 HALT**：`touch ~/polymarket/engine/HALT`；手工核对差异清单 `logs/recon_*.json` |
| (实盘后) 连续熔断 | 执行层连续异常 | HALT + 检查风控日志；原因不明前不复市 |
| (实盘后) 资金异常 | 余额突降 | HALT + 检查 PM/Bybit 登录活动 + 密钥安全 |

## 强制停机条件（满足任一即停）

1. 对账不平（任何层级差异>0）
2. 连续熔断触发（circuit_breaker_errors 达上限）
3. 密钥异常（API 报 401/403、或怀疑泄露）
4. 结算窗口规则未生效（结算前未自动减仓）
5. 用户手动 `touch ~/polymarket/engine/HALT`

停机操作：`touch ~/polymarket/engine/HALT`（引擎每秒检查该文件）
恢复操作：核查原因 → `rm ~/polymarket/engine/HALT`

## 常规操作

```bash
# 改密码（首选）：网页 /system 页 →「修改登录密码」（验证当前密码, 自动吊销所有旧会话）
# 应急（服务器命令行）：
./venv/bin/python -c "import bcrypt,os; open(os.path.expanduser('~/polymarket/.dash_passwd_hash'),'wb').write(bcrypt.hashpw(b'<新密码>'.encode(), bcrypt.gensalt()))"

# 部署（本地执行）
bash deploy.sh                # 部署当前代码+重启监控+自动备份旧版
bash deploy.sh --rollback     # 回滚上一版本

# 服务器重启监控/看门狗
sudo systemctl restart pm-monitor pm-wss
sudo systemctl restart pm-watchdog.timer

# 查看数据平台
./venv/bin/python data_writer.py          # 手动跑一次
./venv/bin/python backtest.py --grid      # 回测（本地写代码→scp→服务器跑）

# 数据同步到本地（Windows）
bash sync_data.sh
uv run --with duckdb python research/q.py research/queries.sql
```

## 灾难恢复

- Lightsail 快照：控制台 → 快照 → 恢复新实例（IP 会变，需更新本手册与本地脚本）
- 数据双备份：parquet 每日 rsync/scp 到本地 `E:\量化数据\pm_data\`（sync_data.sh）
- 密钥备份：PM owner私钥在用户密码管理器；Bybit/relayer 在服务器600文件+用户处

## 时间线约定

- 全部日志/数据用 UTC（服务器 TZ=Etc/UTC）；本地分析注意 +8 换算
- 结算日历：PM 市场 end_date 为美东时间，北京时间凌晨结算——结算窗口规则依赖此日历
