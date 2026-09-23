#!/usr/bin/env python3
"""R12f: 移动版管理后台回归 — admin 不被误踢 (stats/audit 无 ok 字段的响应形状)"""
import json
import os
import re
import subprocess
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_src = open(os.path.join(BASE, "dash", "static", "m.html"), encoding="utf-8").read()
_scripts = re.findall(r"<script>(.*?)</script>", _src, re.S)
_js = "\n".join(_scripts)
_real_ids = sorted(set(re.findall(r'id="([^"]+)"', _src)))

TMP = tempfile.mkdtemp(prefix="m_admin_smoke_")
test = r'''
var _els = {};
function makeEl(id){ return {id:id, innerHTML:'', textContent:'', value:'', className:'', style:{}, dataset:{}, classList:{add(){},remove(){},toggle(){}}, src:null, querySelectorAll:function(){return []}, querySelector:function(){return null}, addEventListener:function(){}, appendChild:function(){}, scrollIntoView:function(){}, getContext:function(){return null}}; }
var _realIds = new Set(require(process.argv[3]));
var document = { getElementById:function(id){ if(!_realIds.has(id))return null; if(!_els[id])_els[id]=makeEl(id); return _els[id]; }, querySelector:function(){return makeEl('q')}, querySelectorAll:function(){return []}, addEventListener:function(){}, createElement:function(t){return makeEl('c')} };
var window = {hermes:{send:function(){}}};
var navigator = {clipboard:{writeText:function(){return Promise.resolve()}}};
var localStorage = {getItem:function(){return null},setItem:function(){},removeItem:function(){}};
var _toasts = [];
function toast(m){ _toasts.push(m); }
var fetch = function(url){
  var u = String(url);
  if(u.indexOf('/api/admin/stats')>=0) return Promise.resolve({status:200, json:function(){return Promise.resolve({total:7,active:7,new_today:7,plans:{free:7},capital_total:650})}});
  if(u.indexOf('/api/admin/users')>=0) return Promise.resolve({status:200, json:function(){return Promise.resolve({rows:[]})}});
  if(u.indexOf('/api/admin/audit')>=0) return Promise.resolve({status:200, json:function(){return Promise.resolve({total:59,rows:[]})}});
  if(u.indexOf('/api/admin/funds/summary')>=0) return Promise.resolve({status:200, json:function(){return Promise.resolve({ok:true,pending_withdraws:[],unclaimed:[],plans:[],settings:{}})}});
  if(u.indexOf('/api/admin/pm-markets')>=0) return Promise.resolve({status:200, json:function(){return Promise.resolve({ok:true,total:0,off_n:0,rows:[],has_more:false})}});
  if(u.indexOf('/api/auth/me')>=0) return Promise.resolve({status:200, json:function(){return Promise.resolve({ok:true,user:{uid:1,role:'admin'}})}});
  return Promise.resolve({status:200, json:function(){return Promise.resolve({ok:true})}});
};
var EventSource = function(){return {onmessage:null,onerror:null};};
var setInterval = function(){return 1}; var clearInterval = function(){};
var setTimeout = function(){return 1}; var clearTimeout = function(){};
var _src = require('fs').readFileSync(process.argv[2], 'utf8');
var TEST = [
"var __ok=[]; var __toasts=[];",
"var _origToast = toast; toast = function(m){ _toasts.push(m); _origToast(m); };",
"var __res=[];",
"(async function(){",
"  await loadAdmin();",
"  __res.push(['无仅管理员toast', _toasts.indexOf('仅管理员可访问')<0, _toasts]);",
"  __res.push(['stats渲染', String(document.getElementById('admin-stats')&&document.getElementById('admin-stats').innerHTML||'').indexOf('注册用户')>=0]);",
"  __res.push(['用户列表渲染', String(document.getElementById('admin-users')&&document.getElementById('admin-users').innerHTML||'').indexOf('暂无用户')>=0]);",
"  await loadAdmAudit(0);",
"  __res.push(['审计渲染', String(document.getElementById('adm-audit')&&document.getElementById('adm-audit').innerHTML||'').indexOf('加载失败')<0]);",
"  __res.push(['审计计数59', String(document.getElementById('adma-n')&&document.getElementById('adma-n').textContent||'').indexOf('59')>=0]);",
"  await loadAdmFunds();",
"  __res.push(['资金渲染', String(document.getElementById('adm-funds')&&document.getElementById('adm-funds').innerHTML||'').indexOf('加载失败')<0]);",
"  await loadAdmPm(0);",
"  __res.push(['PM标的渲染', String(document.getElementById('adm-pm-list')&&document.getElementById('adm-pm-list').innerHTML||'').indexOf('加载失败')<0]);",
"  __res.forEach(function(r){ console.log((r[1]?'  OK ':'  XX ')+r[0]+(r[1]?'':' | '+String(r[2]).slice(0,120))); });",
"  console.log('M-ADMIN SMOKE: '+__res.filter(function(r){return r[1]}).length+'/'+__res.length+' OK');",
"  process.exit(__res.every(function(r){return r[1]})?0:1);",
"})().catch(function(e){ console.log('执行异常: '+(e&&e.stack||e)); process.exit(2); });"
].join("\n");
try { eval(_src + "\n" + TEST); } catch(e) { console.log("页面JS执行异常: " + (e && e.stack || e)); process.exit(2); }
'''
f = os.path.join(TMP, "r.js").replace("\\", "/")
with open(f, "w", encoding="utf-8") as fh:
    fh.write(test)
jsfile = os.path.join(TMP, "page.js").replace("\\", "/")
with open(jsfile, "w", encoding="utf-8") as fh:
    fh.write(_js)
fid = os.path.join(TMP, "ids.js").replace("\\", "/")
with open(fid, "w", encoding="utf-8") as fh:
    fh.write("module.exports=" + json.dumps(_real_ids) + ";")
r = subprocess.run(["node", f, jsfile, fid], capture_output=True, text=True, timeout=60)
print(r.stdout)
print(r.stderr[-400:] if r.returncode else "")
sys.exit(r.returncode)
