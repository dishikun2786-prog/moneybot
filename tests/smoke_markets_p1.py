# -*- coding: utf-8 -*-
"""P1 全标的行情屏冒烟: 提取 m.html script → node 桩 DOM 执行 → 断言市场列表全流程"""
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = os.path.join(ROOT, "dash", "static", "m.html")
SCRATCH = os.path.join(os.environ.get("TMPDIR", os.path.expanduser("~/AppData/Local/Temp")), "mcheck_p1.js")

src = open(HTML, encoding="utf-8").read()
m = re.search(r"<script>(.*?)</script>", src, re.S)
assert m, "script 块缺失"
js = m.group(1)

STUB = r"""
const stubs = {};
const document = {getElementById: function(x){ return stubs[x] || (stubs[x] = {textContent:'',innerHTML:'',className:'',style:{},value:'',
  addEventListener(){}, querySelectorAll(){return []}, dataset:{}, classList:{add(){},remove(){},toggle(){}}, setAttribute(){}, getAttribute(){return null}}); },
  querySelectorAll(){return []}, addEventListener(){}};
const window = {addEventListener(){}, location:{}};
const localStorage = {getItem(){return '{}'}, setItem(){}, removeItem(){}};
const EventSource = function(){ this.onmessage=null; this.onerror=null; };
const navigator = {};
const fetch = function(){ return Promise.resolve({json:()=>Promise.resolve({})}); };
async function api(){ return {data:null}; }
"""

TEST = r"""
function esc(s){return String(s==null?'':s)}
function num(x,n){return Number(x||0).toFixed(n==null?2:n)}
function fmtPx(x){return Number(x).toLocaleString('en-US',{maximumFractionDigits:4})}
function toast(m){console.log('TOAST:',m)}
function go(s){console.log('go:',s)}
var curScreen='markets';
try{
  INSTR={linear:[{symbol:'SOLUSDT',name:'Solana',turnover24h:1e9},
                  {symbol:'BTCUSDT',name:'Bitcoin',turnover24h:2e9},
                  {symbol:'ETHUSDT',name:'Ethereum',turnover24h:1.5e9}],
         spot:[{symbol:'SOLUSDT',name:'Solana',turnover24h:5e8},{symbol:'NOTPERP',name:'XCoin',turnover24h:1e6}]};
  PRICES={SOLUSDT:{last:145.2,change_pct:3.4,spot:145.1,ts:1},
          BTCUSDT:{last:67000,change_pct:-1.2,spot:66980,ts:2},
          ETHUSDT:{last:3500,change_pct:0.5,spot:3499,ts:3},
          NOTPERP:{spot:1.23,change_pct:9,ts:4}};
  renderTickers();
  var html=stubs['ticker-list'].innerHTML;
  if(html.indexOf('SOLUSDT')<0) throw new Error('SOLUSDT 未渲染');
  if(html.indexOf('NOTPERP')<0) throw new Error('现货专属标的未渲染');
  if(html.indexOf('☆')<0) throw new Error('收藏按钮缺失');
  if(stubs['mk-count'].textContent.indexOf('4 标的')<0) throw new Error('计数错误: '+stubs['mk-count'].textContent);
  setMkTab('linear');
  if(stubs['ticker-list'].innerHTML.indexOf('NOTPERP')>=0) throw new Error('linear tab 混入现货');
  setMkTab('spot');
  if(stubs['ticker-list'].innerHTML.indexOf('BTCUSDT')>=0) throw new Error('spot tab 混入合约');
  setMkTab('all'); MKQ='sol'; MKCUR=0; renderTickers();
  if(stubs['ticker-list'].innerHTML.indexOf('BTCUSDT')>=0) throw new Error('搜索过滤失效');
  MKQ=''; MKCUR=0; MKSORT='vol'; renderTickers();
  toggleFav('SOLUSDT'); setMkTab('fav');
  if(stubs['ticker-list'].innerHTML.indexOf('SOLUSDT')<0) throw new Error('自选 tab 失效');
  if(stubs['ticker-list'].innerHTML.indexOf('⭐')<0) throw new Error('已收藏未显示⭐');
  // 排序: chg 降序 → NOTPERP(9%) 应在 SOLUSDT(3.4%) 前
  setMkTab('all'); setMkSort('chg');
  var h2=stubs['ticker-list'].innerHTML;
  if(h2.indexOf('NOTPERP')>h2.indexOf('SOLUSDT')) throw new Error('24h涨跌排序失效');
  // 点现货专属标的应提示不支持交易
  mkTap('NOTPERP','spot');
  mkTap('SOLUSDT','spot'); // SOLUSDT 有合约 → 应进入交易
  console.log('SMOKE-P1-OK 行情屏全流程通过');
  process.exit(0);
}catch(e){ console.error('SMOKE-FAIL', e.message); process.exit(1); }
"""

code = STUB + js + TEST
open(SCRATCH, "w", encoding="utf-8").write(code)
r = subprocess.run(["node", SCRATCH], capture_output=True, text=True)
print(r.stdout.strip())
if r.returncode:
    print(r.stderr[:1000])
sys.exit(r.returncode)
