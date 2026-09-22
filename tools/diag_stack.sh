#!/bin/bash
# 数据停滞诊断 v1 — 全链路状态采集
cd ~/polymarket
echo "===== [1] 服务器基础 ====="
echo "当前时间: $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
echo "运行时长: $(uptime -p)"
echo "磁盘: $(df -h / | tail -1 | awk '{print $4"可用 / "$5"已用"}')"
echo "内存: $(free -m | awk '/Mem/{print $3"MB/"$2"MB"}')"
echo "===== [2] systemd 单元状态 ====="
for u in pm-monitor pm-wss pm-dash pm-carry pm-carryengine; do
  printf "%-16s %s\n" "$u" "$(systemctl is-active $u)"
done
echo "--- 定时器(名称 下次触发 上次触发) ---"
systemctl list-timers --all 2>/dev/null | grep -E 'pm-|NEXT' | head -12
echo "===== [3] 日志尾部时间戳 ====="
for f in logs/monitor_stdout.log logs/events.jsonl logs/carry_1m.jsonl logs/paper_state.json logs/paper_trades.jsonl logs/carry_state.json logs/carry_trades.jsonl logs/equity_daily.jsonl; do
  if [ -f "$f" ]; then
    echo "--- $f (mtime $(stat -c %y "$f" | cut -c1-19)) ---"
    tail -2 "$f" | cut -c1-160
  else
    echo "--- $f 不存在! ---"
  fi
done
echo "===== [4] wss 日志 ====="
ls -la logs/ | grep -i wss || echo "无wss日志文件"
journalctl -u pm-wss --since "2 hours ago" --no-pager 2>/dev/null | tail -5 || true
echo "===== [5] 数据平台 Parquet 新鲜度 ====="
./venv/bin/python - <<'PYEOF'
import duckdb, glob, os
for pat in ["fv_snapshot", "book_1s", "carry_1m", "carry_funding"]:
    files = sorted(glob.glob(f"data/{pat}/year=*/month=*/*.parquet"))
    if not files:
        print(f"{pat}: 无文件!")
        continue
    try:
        q = duckdb.sql(f"SELECT max(ts) AS mt, count(*) AS n FROM read_parquet('data/{pat}/year=*/month=*/*.parquet')")
        r = q.fetchone()
        print(f"{pat}: {r[1]}行, 最新ts={r[0]} (文件数{len(files)}, 最新文件mtime {os.path.getmtime(files[-1]):.0f})")
    except Exception as e:
        print(f"{pat}: 查询失败 {type(e).__name__} {e}")
PYEOF
echo "===== [6] 纸面引擎状态文件 ====="
cat logs/paper_state.json 2>/dev/null | head -c 500; echo
cat logs/carry_state.json 2>/dev/null | head -c 500; echo
echo "===== [7] watchdog 最近输出 ====="
journalctl -u pm-watchdog.service --since "1 hour ago" --no-pager 2>/dev/null | tail -3 || echo "(watchdog无journal输出)"
