#!/bin/bash
# 启动/重启 Polymarket×Bybit 监控 (7x24, 60秒周期)
cd "$HOME/polymarket" || exit 1
pkill -f bybit_pm_monitor 2>/dev/null
sleep 1
nohup python3 bybit_pm_monitor.py --interval 60 >> logs/monitor_stdout.log 2>&1 < /dev/null &
sleep 2
echo "--- 进程状态 ---"
pgrep -af bybit_pm_monitor
echo "--- CSV行数 ---"
wc -l logs/bybit_pm_fv.csv 2>/dev/null || echo "(尚无CSV)"
