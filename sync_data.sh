#!/bin/bash
# 增量拉取服务器 parquet 数据到 E:\量化数据\pm_data\
# 当前用 scp -r (数据量小); 数据量增长后改 rsync 增量
set -e
DEST="E:/量化数据/pm_data"
mkdir -p "$DEST"
scp -i /e/ssh_keys/lightsail-eu-west-1.pem -o StrictHostKeyChecking=no -r \
  ubuntu@34.253.194.97:~/polymarket/data/* "$DEST/"
echo "✅ 同步完成: $DEST"
find "$DEST" -name "*.parquet" | wc -l
