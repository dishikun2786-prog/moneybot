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
/* —— 时间校对: 服务端一律存UTC(ISO Z/毫秒), 前端按本机时区渲染 —— */
function parseTs(ts){
  if(typeof ts === 'number') return new Date(ts);
  return new Date(ts.endsWith('Z') ? ts : ts + 'Z');
}
function fmtLocalTime(ts){
  const d = parseTs(ts);
  return d.toLocaleTimeString('zh-CN', {hour12: false});
}
function fmtLocalDT(ts){
  const d = parseTs(ts);
  return d.toLocaleString('zh-CN', {hour12: false});
}
function tzLabel(){
  const off = -new Date().getTimezoneOffset() / 60;
  return 'UTC' + (off >= 0 ? '+' : '') + off;
}
