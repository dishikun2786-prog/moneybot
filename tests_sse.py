#!/usr/bin/env python3
"""SSE 双通道直测 (服务器本地跑)"""
import subprocess, time, json, sys
sys.path.insert(0, "dash")
from app import auth

AT = auth.make_session(1, "admin")
UT = auth.make_session(27, "user")
TS = str(int(time.time()) % 10000)

def test(label, base):
    print(f"== {label} 测试 ==")
    sse = subprocess.Popen(["curl", "-sN", "--max-time", "10", f"{base}/api/support/stream",
                            "-H", f"Cookie: mb_session={AT}", "-k"],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    time.sleep(3)
    r = subprocess.run(["curl", "-s", "-X", "POST", f"{base}/api/support/msg",
                        "-H", f"Cookie: mb_session={UT}",
                        "-H", "Content-Type: application/json",
                        "-d", json.dumps({"content": f"SSE测试{label}{TS}"}), "-k"],
                       capture_output=True, text=True, timeout=20)
    print("发消息:", r.stdout[:70])
    time.sleep(6)
    sse.terminate()
    out = sse.stdout.read() if sse.stdout else ""
    print(f"--- {label} SSE输出({len(out)}B):", out[:200] or "(空)")

test("本地8080", "http://127.0.0.1:8080")
test("nginx8443", "https://127.0.0.1:8443")
