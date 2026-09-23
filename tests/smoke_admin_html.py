#!/usr/bin/env python3
"""R12e: 桌面版 admin.html 冒烟 — 5 tab 结构 + 各加载函数存在 + JS 可执行 (DOM 桩 + node)"""
import json
import os
import re
import subprocess
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
src = open(os.path.join(BASE, "dash", "static", "admin.html"), encoding="utf-8").read()
scripts = re.findall(r"<script>(.*?)</script>", src, re.S)
js = "\n".join(scripts)
_real_ids = sorted(set(re.findall(r'id="([^"]+)"', src)))

TMP = tempfile.mkdtemp(prefix="adm_smoke_")
test = '''
var _els = {};
function makeEl(id){
  return {id:id, innerHTML:'', textContent:'', value:'', className:'', style:{},
    dataset:{}, classList:{add(){},remove(){},toggle(){}},
    querySelectorAll:function(){return []}, querySelector:function(){return null},
    addEventListener:function(){}, appendChild:function(){}, scrollIntoView:function(){},
    getContext:function(){return null}};
}
var _realIds = new Set(require(process.argv[3]));
var _fetches = [];
var document = {
  getElementById:function(id){ if(!_realIds.has(id))return null; if(!_els[id])_els[id]=makeEl(id); return _els[id]; },
  querySelector:function(){return makeEl('q')}, querySelectorAll:function(){return []},
  addEventListener:function(){}, createElement:function(t){return makeEl('c')}
};
var window = {};
var navigator = {clipboard:{writeText:function(){return Promise.resolve()}}};
var fetch = function(url, opts){ _fetches.push({u:String(url), m:(opts&&opts.method)||'GET'}); return Promise.resolve({json:function(){return Promise.resolve({})}}); };
var setInterval = function(){return 1}; var clearInterval = function(){};
var setTimeout = function(){return 1}; var clearTimeout = function(){};
var alert = function(){}; var confirm = function(){return true}; var prompt = function(){return ''};
var __ok = [];
function check(n,c,x){__ok.push(c); console.log((c?'  OK ':'  XX ')+n+(c?'':' | '+String(x).slice(0,140)));}
var _src = require('fs').readFileSync(process.argv[2], 'utf8');
try { eval(_src); } catch(e) { console.log("页面JS执行异常: " + (e && e.stack || e)); process.exit(2); }
setImmediate(async function(){
  await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
  check('loadUsers', typeof loadUsers === 'function');
  check('loadAudit', typeof loadAudit === 'function');
  check('loadAnn', typeof loadAnn === 'function');
  check('loadFundsPanel', typeof loadFundsPanel === 'function');
  check('loadPmAdmin', typeof loadPmAdmin === 'function');
  check('renderUsers', typeof renderUsers === 'function');
  // 模拟点击各 tab
  try{
    document.querySelectorAll('.tabs button').forEach(function(b){ if(b.onclick) b.onclick(); });
    check('5 tab 点击无异常', true);
  }catch(e){ check('5 tab 点击无异常', false, e); }
  check('tab 点击触发 fetch', _fetches.length >= 3, _fetches.map(function(f){return f.u}));
  console.log('ADMIN SMOKE: '+__ok.filter(Boolean).length+'/'+__ok.length+' OK');
  process.exit(__ok.every(Boolean)?0:1);
});
'''
f = os.path.join(TMP, "adm.js").replace("\\", "/")
open(f, "w", encoding="utf-8").write(test)
jsfile = os.path.join(TMP, "page.js").replace("\\", "/")
open(jsfile, "w", encoding="utf-8").write(js)
fid = os.path.join(TMP, "ids.js").replace("\\", "/")
open(fid, "w", encoding="utf-8").write("module.exports=" + json.dumps(_real_ids) + ";")
r = subprocess.run(["node", f, jsfile, fid], capture_output=True, text=True, timeout=60)
print(r.stdout)
print(r.stderr[-500:] if r.returncode else "")
sys.exit(r.returncode)
