#!/usr/bin/env python3
"""移动版交易室 (m.html) 运行时冒烟测试:
DOM 桩 + node 真实执行整个 <script>, 捕获 TDZ/ReferenceError/TypeError"""
import os
import re
import subprocess
import sys

BASE = "D:/Program Files/hermes/polymarket_arb/dash/static"
html = open(os.path.join(BASE, "m.html"), encoding="utf-8").read()
scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
assert scripts, "无内联脚本"
script = scripts[0]

DOM_STUB = r"""
const stubs = new Proxy({}, {
  get: (t, k) => {
    if (k === 'style' || k === 'dataset' || k === 'classList') {
      return { add(){}, remove(){}, toggle(){}, contains: () => false };
    }
    if (k === 'textContent' || k === 'value' || k === 'innerHTML') return '';
    return (typeof t[k] === 'function') ? t[k] : function(){ return stubs; };
  },
  set: () => true
});
const _el = () => stubs;
global.document = {
  getElementById: _el, querySelector: _el, querySelectorAll: () => [],
  addEventListener(){}, body: stubs, createElement: () => stubs,
};
global.window = { addEventListener(){} };
global.location = { href: '/m', pathname: '/m' };
global.fetch = () => new Promise(() => {});
global.EventSource = class { constructor(url){ this.url = url; global.__es = (global.__es||0)+1; } close(){} };
global.setInterval = (fn, ms) => 0;
global.setTimeout = (fn, ms) => { if (ms === 0) fn(); return 0; };
global.clearTimeout = () => {};
"""

with open(os.path.join(os.path.dirname(__file__), "_smoke_m.js"), "w", encoding="utf-8") as f:
    f.write(DOM_STUB + "\n" + script + "\n// 同步执行完毕检查\n"
            "console.log('SMOKE-DONE');\n")

r = subprocess.run(["node", os.path.join(os.path.dirname(__file__), "_smoke_m.js")],
                   capture_output=True, text=True)
os.remove(os.path.join(os.path.dirname(__file__), "_smoke_m.js"))
sys.stdout.write(r.stdout)
sys.stderr.write(r.stderr)
if r.returncode != 0 or "SMOKE-DONE" not in r.stdout:
    print("冒烟失败: returncode=%d" % r.returncode)
    sys.exit(1)
print("移动版冒烟测试通过 ✓")
