#!/usr/bin/env python3
"""交易室页面运行时冒烟测试: 用 DOM 桩在 node 里真实执行 <script>,
捕获 TDZ/ReferenceError/TypeError 等运行时错误 (语法检查查不出这类)"""
import os
import re
import subprocess
import tempfile

BASE = "D:/Program Files/hermes/polymarket_arb/dash/static"
html = open(os.path.join(BASE, "trade.html"), encoding="utf-8").read()
scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
assert scripts, "无内联脚本"

DOM_STUB = r"""
// --- DOM 桩 ---
const stubs = new Proxy({}, { get: (t, k) => {
  if (k === 'style' || k === 'dataset' || k === 'classList') return {};
  if (k === 'textContent' || k === 'value' || k === 'innerHTML' || k === 'data') return '';
  return (typeof t[k] === 'function') ? t[k] : function(){ return stubs; };
}, set: () => true });
const _el = () => stubs;
global.document = {
  getElementById: _el, querySelector: _el, querySelectorAll: () => [],
  addEventListener(){}, body: stubs, createElement: () => stubs,
};
global.window = { addEventListener(){} };
global.location = { href: '/trade', pathname: '/trade' };
global.fetch = () => new Promise(() => {});   // 永不resolve → 异步链不执行
global.EventSource = class {
  constructor(url){ this.url = url; this.readyState = 1; global.__esCreated = (global.__esCreated||0)+1; }
  close(){}
};
global.echarts = { init: () => ({ setOption(){}, resize(){}, getOption: () => ({}), on(){}, dispatchAction(){} }) };
global.requestAnimationFrame = fn => setTimeout(fn, 0);
// app.js 外部依赖桩
global.startClock = () => {};
global.tzLabel = () => 'UTC+8';
global.fmtLocalTime = () => '00:00:00';
global.fmtLocalDT = () => '2026-01-01 00:00:00';
global.parseTs = () => new Date();
global.zhSide = x => x;
"""

with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
    f.write(DOM_STUB + "\n" + scripts[0] + "\n"
            "setTimeout(() => {\n"
            "  const n = global.__esCreated || 0;\n"
            "  if (n >= 1) { console.log('SMOKE_OK: EventSource已创建 x' + n); process.exit(0); }\n"
            "  console.error('SMOKE_FAIL: EventSource从未创建 (TDZ或调用链断裂)'); process.exit(1);\n"
            "}, 1500);\n")
    tmp = f.name

r = subprocess.run(["node", tmp], capture_output=True, text=True, timeout=30)
os.unlink(tmp)
print("STDOUT:", r.stdout.strip()[:400])
if r.returncode != 0:
    print("STDERR:", r.stderr.strip()[:800])
raise SystemExit(r.returncode)
