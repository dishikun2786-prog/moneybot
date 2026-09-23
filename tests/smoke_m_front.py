#!/usr/bin/env python3
"""R6 前端冒烟: m.html 内嵌 JS 的 K线/盘口渲染函数 (DOM 桩 + node 执行)"""
import os
import re
import subprocess
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
src = open(os.path.join(BASE, "dash", "static", "m.html"), encoding="utf-8").read()
scripts = re.findall(r"<script>(.*?)</script>", src, re.S)
js = "\n".join(scripts)

TMP = tempfile.mkdtemp(prefix="m_smoke_")
test = '''
// ---- DOM 桩 ----
var _els = {};
function makeEl(id){
  return {id:id, innerHTML:'', textContent:'', value:'', className:'', style:{},
    dataset:{}, classList:{add(){},remove(){},toggle(){}},
    querySelectorAll:function(){return []},
    querySelector:function(){return null},
    addEventListener:function(){},
    appendChild:function(){},
    scrollIntoView:function(){},
    getContext:function(){return null}};
}
var document = {
  getElementById:function(id){ if(!_els[id])_els[id]=makeEl(id); return _els[id]; },
  querySelector:function(){return makeEl('q')},
  querySelectorAll:function(){return []},
  addEventListener:function(){},
  createElement:function(t){return makeEl('c')}
};
var window = {hermes:{send:function(){}}};
var navigator = {clipboard:{writeText:function(){return Promise.resolve()}}};
var localStorage = {getItem:function(){return null},setItem:function(){},removeItem:function(){}};
var fetch = function(){return Promise.resolve({json:function(){return Promise.resolve({})}})};
var EventSource = function(){return {onmessage:null,onerror:null};};
var setInterval = function(){return 1};
var clearInterval = function(){};
var setTimeout = function(){return 1};
var clearTimeout = function(){};

// ---- 执行页面 JS + 测试代码 (同作用域, let/const 不泄漏到 eval 外) ----
var _src = require('fs').readFileSync(process.argv[2], 'utf8');
var TEST = [
  "globalThis.__ok = [];",
  "function check(n, c, x){ __ok.push(c); console.log((c?'  OK ':'  XX ')+n+(c?'':' | '+String(x).slice(0,120))); }",
  "check('renderKlineSVG', typeof renderKlineSVG === 'function');",
  "check('loadKline', typeof loadKline === 'function');",
  "check('fmtPx', typeof fmtPx === 'function');",
  "check('num', typeof num === 'function');",
  "var bars=[]; for(var i=0;i<30;i++){var o=100+i*0.5; bars.push({t:1790000000000+i*900000,o:o,h:o+2,l:o-1.5,c:o+1,v:1000+i*50});}",
  "var el=makeEl('kline-svg');",
  "try{ renderKlineSVG(bars,'15m',true,el); check('KlineSVG渲染', el.innerHTML.indexOf('<rect')>=0 && el.innerHTML.indexOf('<line')>=0, el.innerHTML.slice(0,100)); }catch(e){ check('KlineSVG渲染', false, e); }",
  "check('renderOb', typeof renderOb === 'function', typeof renderOb);",
  "try{ renderOb({asks:[[118.5,100],[118.4,200]],bids:[[118.2,150],[118.1,300]],mid:118.35}); check('renderOb调用', true); }catch(e){ check('renderOb调用', false, e); }",
  "check('openSymDetail', typeof openSymDetail === 'function');",
  "check('loadSpotCard', typeof loadSpotCard === 'function');",
  "check('klStart', typeof klStart === 'function');",
  "console.log('\\\\nSMOKE: '+__ok.filter(Boolean).length+'/'+__ok.length+' OK');",
  "process.exit(__ok.every(Boolean)?0:1);"
].join("\\n");
try {
  eval(_src + "\\n" + TEST);
} catch(e) {
  console.log("页面JS执行异常: " + (e && e.stack || e));
  process.exit(2);
}
'''
f = os.path.join(TMP, "smoke.js").replace("\\", "/")
with open(f, "w", encoding="utf-8") as fh:
    fh.write(test)
jsfile = os.path.join(TMP, "page.js").replace("\\", "/")
with open(jsfile, "w", encoding="utf-8") as fh:
    fh.write(js)
r = subprocess.run(["node", f, jsfile],
                   capture_output=True, text=True)
print(r.stdout)
print(r.stderr[-800:] if r.returncode else "")
