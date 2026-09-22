#!/bin/bash
# 一键部署 + 版本化回滚
# 用法: bash deploy.sh            # 部署当前代码到服务器并重启监控
#       bash deploy.sh --rollback # 回滚到上一个版本
set -e
KEY=/e/ssh_keys/lightsail-eu-west-1.pem
HOST=ubuntu@34.253.194.97
SSH="ssh -i $KEY -o StrictHostKeyChecking=no $HOST"

if [ "$1" = "--rollback" ]; then
  $SSH "cd ~/polymarket && prev=\$(ls -t backups/bybit_pm_monitor.*.py | sed -n 2p) && cp \$prev bybit_pm_monitor.py && sudo systemctl restart pm-monitor && sleep 2 && systemctl is-active pm-monitor && echo \"已回滚到 \$prev\""
  exit 0
fi

TAG=$(date +%Y%m%d-%H%M%S)
scp -i $KEY -o StrictHostKeyChecking=no *.py "$HOST:~/polymarket/"
$SSH "cd ~/polymarket && mkdir -p backups && cp bybit_pm_monitor.py backups/bybit_pm_monitor.$TAG.py && sudo systemctl restart pm-monitor && sleep 2 && echo \"deployed $TAG | \$(systemctl is-active pm-monitor)\""
