#!/usr/bin/env python3
"""60s 看门狗: 健康检查 + Telegram 告警 (状态变化才发, 防刷屏)
检查: pm-monitor active / 崩溃循环(NRestarts增量) / CSV新鲜度 / 磁盘 / 内存"""
import json, os, subprocess, time, urllib.request

BASE = os.path.expanduser("~/polymarket")
CFG = f"{BASE}/alert_config.json"
STATE = f"{BASE}/logs/watchdog_state.json"
CSV = f"{BASE}/logs/bybit_pm_fv.csv"

def cfg():
    return json.load(open(CFG)) if os.path.exists(CFG) else {}

def send(c, text):
    if not c.get("bot_token"):
        print("  (bot_token 未配置, 仅打印):", text); return
    url = f"https://api.telegram.org/bot{c['bot_token']}/sendMessage"
    data = json.dumps({"chat_id": int(c["chat_id"]), "text": text}).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(url, data=data,
            headers={"Content-Type": "application/json"}), timeout=10)
    except Exception as e:
        print("  [warn] send fail:", e)

def _nrestarts():
    p = subprocess.run(["systemctl", "show", "pm-dash", "-p", "NRestarts"],
                       capture_output=True, text=True)
    try:
        return int(p.stdout.strip().split("=")[1])
    except Exception:
        return 0

def checks(prev_restarts):
    probs = []
    # R13c PM-OFF: 不再检查 pm-monitor/CSV/books_1s (PM 功能已下线)
    for svc in ("pm-dash", "pm-bridge"):
        p = subprocess.run(["systemctl", "is-active", svc], capture_output=True, text=True)
        if p.stdout.strip() != "active":
            probs.append(f"{svc} 服务非 active")
    restarts = _nrestarts()
    delta = restarts - prev_restarts
    if delta >= 3:
        probs.append(f"pm-dash 疑似重启循环(重启计数 +{delta})")
    du = subprocess.run(["df", "/"], capture_output=True, text=True).stdout.splitlines()[-1].split()
    if int(du[4].rstrip("%")) > 85:
        probs.append(f"磁盘占用 {du[4]}")
    fr = subprocess.run(["free", "-m"], capture_output=True, text=True).stdout.splitlines()[1].split()
    if int(fr[6]) < 300:
        probs.append(f"可用内存仅 {fr[6]}MB (阈值300)")
    # R14-M1: K线 REST 回源健康 (429限频/失败率告警)
    try:
        import json as _j
        hp = "/home/ubuntu/polymarket/logs/kl_rest_health.json"
        if os.path.exists(hp) and time.time() - os.path.getmtime(hp) < 180:
            with open(hp) as _f:
                hd = _j.load(_f)
            if hd.get("r429", 0) > 0:
                probs.append(f"K线REST回源429限频 {hd['r429']} 次(累计)")
            if hd.get("fails", 0) > 30:
                probs.append(f"K线REST回源失败 {hd['fails']} 次(累计,阈值30)")
    except Exception:
        pass
    return probs, restarts

def main():
    c = cfg()
    prev = {}
    if os.path.exists(STATE):
        try: prev = json.load(open(STATE))
        except Exception: pass
    probs, restarts = checks(int(prev.get("restarts", 0)))
    key = ",".join(sorted(probs))
    if key != prev.get("key"):
        if probs:
            send(c, "⚠️ 交易平台告警:\n" + "\n".join(f"- {x}" for x in probs))
        elif prev.get("key"):
            send(c, "✅ 交易平台已恢复")
        json.dump({"key": key, "restarts": restarts,
                   "ts": time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}, open(STATE, "w"))
    status = "正常" if not probs else f"异常{len(probs)}项: {key}"
    print(f"[{time.strftime('%H:%M:%SZ', time.gmtime())}] watchdog: {status} (restarts累计={restarts})")

if __name__ == "__main__":
    main()
