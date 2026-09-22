/* Moneybot 前端共享工具: 中文化时钟/新鲜度/格式化 */
function zhClock(){
  const now = new Date();
  const utc = now.toISOString().slice(11, 19);
  const bj = new Date(now.getTime() + 8 * 3600 * 1000).toISOString().slice(11, 19);
  return `${bj} 北京 | ${utc} UTC`;
}
function startClock(elId){
  const el = document.getElementById(elId);
  if(!el) return;
  el.textContent = zhClock();
  setInterval(() => { el.textContent = zhClock(); }, 1000);
}
function fmtNum(x, d){
  if(x == null || x === '') return '—';
  return Number(x).toFixed(d === undefined ? 2 : d);
}
function ageSec(iso){
  if(!iso) return Infinity;
  const t = Date.parse(iso.endsWith('Z') ? iso : iso + 'Z');
  return Math.max(0, (Date.now() - t) / 1000);
}
function freshnessHtml(iso){
  const a = ageSec(iso);
  if(!isFinite(a)) return '';
  if(a < 150) return '<span class="badge ok">实时</span>';
  if(a < 600) return `<span class="badge bad">${Math.round(a)}秒前</span>`;
  return `<span class="badge bad">${Math.round(a/60)}分钟前</span>`;
}
function zhSide(s){ return s === 'BUY' ? '买入' : (s === 'SELL' ? '卖出' : s); }
