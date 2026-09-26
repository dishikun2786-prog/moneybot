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
    # R14-M3: 平台密钥轮换告警 (>90天未换或缺失 set_ts)
    try:
        pkf = "/home/ubuntu/polymarket/.platform_keys.json"
        if os.path.exists(pkf):
            with open(pkf) as _f:
                _pk = json.load(_f)
            _set = int(_pk.get("set_ts") or 0)
            if _set and (time.time() - _set) > 90 * 86400:
                probs.append(f"平台Bybit密钥已{int((time.time()-_set)//86400)}天未轮换(建议90天内更新)")
        else:
            probs.append("平台Bybit密钥未配置(充值提现链路不可用)")
    except Exception:
        pass
    # R14-M3: 资金三方核对差异告警
    try:
        fp = "/home/ubuntu/polymarket/dash/funds_health.json"
        if os.path.exists(fp) and time.time() - os.path.getmtime(fp) < 600:
            with open(fp) as _f:
                fh = json.load(_f)
            # 仅在有真实用户资金(预期>0)时核对差异; 测试阶段 Bybit 余额为平台自有资金, 不告警
            if fh.get("expected", 0) > 0 and abs(fh.get("diff", 0)) >= 5.0:
                probs.append(f"资金三方核对差异 {fh['diff']} USDT (Bybit {fh.get('bybit_usdt')} vs 预期 {fh.get('expected')})")
    except Exception:
        pass
    # R14-M5: 内存水位线告警 (可用 < 300MB)
    try:
        with open("/proc/meminfo") as _f:
            _mi = {k: int(v.split()[0]) for k, v in
                   (l.split(":") for l in _f if l and ":" in l and l.split(":")[0].strip()
                    in ("MemAvailable", "MemTotal"))}
        _avail_mb = _mi.get("MemAvailable", 0) // 1024
        if 0 < _avail_mb < 300:
            probs.append(f"内存可用仅 {_avail_mb}MB (<300MB 水位线)")
    except Exception:
        pass
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
