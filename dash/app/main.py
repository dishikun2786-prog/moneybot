import sys
import os
import re
import json
import time
import base64
from collections import defaultdict, deque
from pathlib import Path
from fastapi import FastAPI, Request, Depends, HTTPException, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import iterate_in_threadpool
from . import auth, captcha, config, readers, users, admin, keys

sys.path.insert(0, os.path.expanduser("~/polymarket"))
import ai_client  # noqa: E402
import ai_tools  # noqa: E402
import paper_ops  # noqa: E402
import engine_mode  # noqa: E402
import tenants  # noqa: E402
import bybit_live  # noqa: E402
import pm_live  # noqa: E402
import live_exec  # noqa: E402
from . import funds  # noqa: E402
from . import pm_admin  # noqa: E402

app = FastAPI(title="moneybot dash")


@app.on_event("startup")
def _startup():
    """启动即建用户表; 首次启动执行单用户→admin 迁移"""
    users.init_db()
    admin.init_announce()
    keys.init_db()
    funds.init_db()


@app.middleware("http")
async def no_cache_html(request: Request, call_next):
    """HTML页面禁止缓存 (防旧版页面/编码错乱被浏览器缓存)"""
    resp = await call_next(request)
    ct = resp.headers.get("content-type", "")
    if ct.startswith("text/html"):
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


STATIC = Path(__file__).resolve().parent.parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


def require_session(request: Request):
    if not auth.valid_session(request.cookies.get(config.COOKIE_NAME)):
        raise HTTPException(status_code=401, detail="unauthorized")


def require_session_user(request: Request) -> dict:
    """返回会话用户 {'u': uid, 'r': role}"""
    su = auth.session_user(request.cookies.get(config.COOKIE_NAME))
    if not su:
        raise HTTPException(status_code=401, detail="unauthorized")
    return su


def require_admin(request: Request) -> dict:
    su = require_session_user(request)
    if su["r"] != "admin":
        raise HTTPException(status_code=403, detail="forbidden")
    return su


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/login")
def login():
    return FileResponse(STATIC / "login.html")


@app.get("/markets")
def mk_page():
    return FileResponse(STATIC / "markets.html")


@app.get("/edge")
def edge_page():
    return FileResponse(STATIC / "edge.html")


@app.get("/system")
def sys_page():
    return FileResponse(STATIC / "system.html")


@app.get("/favicon.ico")
def favicon():
    if (STATIC / "favicon.ico").exists():
        return FileResponse(STATIC / "favicon.ico")
    return JSONResponse({}, status_code=404)


@app.post("/api/login")
async def api_login(request: Request, response: Response):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "err": "请求格式错误"}, status_code=400)
    if not auth.login_allowed():
        return JSONResponse({"ok": False, "err": "尝试过多, 1分钟后再试"}, status_code=429)
    if not users.captcha_check(body.get("captcha_id"), body.get("captcha_code")):
        return JSONResponse({"ok": False, "err": "验证码错误或已过期，请刷新重试"}, status_code=400)
    ok, u = users.verify_login(body.get("username") or "", body.get("password") or "")
    if not ok:
        auth.record_fail()
        return JSONResponse({"ok": False, "err": u}, status_code=401)
    users.audit_log(u["id"], "login", "登录成功",
                    request.client.host if request.client else "",
                    request.headers.get("user-agent", "")[:160])
    resp = JSONResponse({"ok": True,
                         "user": {"uid": u["id"], "username": u["username"], "role": u["role"]}})
    resp.set_cookie(config.COOKIE_NAME, auth.make_session(u["id"], u["role"]),
                    httponly=True, samesite="lax", max_age=86400, secure=False, path="/")
    return resp


@app.get("/api/captcha")
def api_captcha():
    """图形验证码: 返回 {captcha_id, image(dataURI)}"""
    code, img, mime = captcha.gen()
    cid = users.captcha_new(code)
    return {"ok": True, "captcha_id": cid,
            "image": f"data:{mime};base64,{base64.b64encode(img).decode()}"}


@app.post("/api/auth/register")
async def api_register(request: Request, response: Response):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "err": "请求格式错误"}, status_code=400)
    if not auth.login_allowed():
        return JSONResponse({"ok": False, "err": "尝试过多, 1分钟后再试"}, status_code=429)
    if not body.get("terms"):
        return JSONResponse({"ok": False, "err": "请先阅读并同意《服务条款》与《风险披露声明》"}, status_code=400)
    if not users.captcha_check(body.get("captcha_id"), body.get("captcha_code")):
        return JSONResponse({"ok": False, "err": "验证码错误或已过期，请刷新重试"}, status_code=400)
    ok, msg = users.create_user(body.get("username") or "", body.get("email") or "",
                                body.get("password") or "")
    if not ok:
        return JSONResponse({"ok": False, "err": msg}, status_code=400)
    u = users.get_by_username(body.get("username"))
    users.audit_log(u["id"], "register", f"注册 {u['username']}",
                    request.client.host if request.client else "",
                    request.headers.get("user-agent", "")[:160])
    resp = JSONResponse({"ok": True,
                         "user": {"uid": u["id"], "username": u["username"], "role": u["role"]}})
    resp.set_cookie(config.COOKIE_NAME, auth.make_session(u["id"], u["role"]),
                    httponly=True, samesite="lax", max_age=86400, secure=False, path="/")
    return resp


@app.post("/api/auth/logout")
def api_logout(response: Response, __=Depends(require_session)):
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(config.COOKIE_NAME, path="/")
    return resp


@app.get("/api/auth/me")
def api_me(su=Depends(require_session_user)):
    u = users.get_user(su["u"])
    if not u or u["status"] != "active":
        raise HTTPException(status_code=401, detail="unauthorized")
    return {"ok": True,
            "user": {"uid": u["id"], "username": u["username"], "email": u["email"],
                     "role": u["role"], "plan": u["plan"], "status": u["status"]}}


@app.get("/api/summary")
def api_summary(__=Depends(require_session)):
    return readers.cached("summary", config.CACHE_TTL, readers.summary)


@app.get("/api/markets")
def api_markets(sort: str = "edge", q: str = "", __=Depends(require_session)):
    return readers.cached(f"markets:{sort}:{q}", 10, lambda: readers.markets(sort, q))


@app.get("/api/market/{key}")
def api_market(key: str, hours: int = 24, __=Depends(require_session)):
    return readers.series(key, hours)





@app.get("/api/paper")
def api_paper(su=Depends(require_session_user)):
    with tenants.tenant(su["u"]):
        return readers.paper()


@app.get("/carry")
def carry_page():
    return FileResponse(STATIC / "carry.html")


@app.get("/api/carry")
def api_carry(su=Depends(require_session_user)):
    with tenants.tenant(su["u"]):
        return readers.cached(f"carry:{su['u']}", 15, readers.carry)


@app.get("/pnl")
def pnl_page():
    return FileResponse(STATIC / "pnl.html")


@app.get("/trade")
def trade_page():
    return FileResponse(STATIC / "trade.html")


@app.get("/api/klines")
def api_klines(symbol: str = "BTCUSDT", interval: str = "15m",
               limit: int = 300, category: str = "",
               __=Depends(require_session)):
    return readers.klines(symbol, interval, min(limit, 1000),
                          category=category if category in ("linear", "spot") else None)


@app.get("/api/tape")
def api_tape(limit: int = 100, su=Depends(require_session_user)):
    with tenants.tenant(su["u"]):
        return readers.tape(min(limit, 200))


@app.get("/api/strategy")
def api_strategy(su=Depends(require_session_user)):
    with tenants.tenant(su["u"]):
        return readers.strategy()


@app.get("/trade-proto")
def trade_proto_page():
    return FileResponse(STATIC / "trade_proto.html")





@app.get("/api/pnl")
def api_pnl(su=Depends(require_session_user)):
    with tenants.tenant(su["u"]):
        return readers.pnl_overview()


@app.get("/api/trades")
def api_trades(mode: str = "all", offset: int = 0, limit: int = 30,
               su=Depends(require_session_user)):
    """M8: 交易记录明细 — 合并模拟(carry_trades.jsonl)与实盘(live_orders.jsonl)台账"""
    with tenants.tenant(su["u"]):
        import os
        logs_dir = tenants.logs(su["u"])
        rows = []
        # 模拟盘
        for path, m in (("carry_trades.jsonl", "paper"),):
            fp = os.path.join(logs_dir, path)
            try:
                with open(fp, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except Exception:
                            continue
                        rec["mode"] = m
                        rows.append(rec)
            except FileNotFoundError:
                pass
        # 实盘
        try:
            with open(os.path.join(logs_dir, "live_orders.jsonl"), encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    if rec.get("venue") == "pm":
                        continue  # PM 已下线, 历史 PM 实盘单不展示
                    rec["mode"] = "live"
                    rec["symbol"] = rec.get("symbol") or ""
                    rows.append(rec)
        except FileNotFoundError:
            pass
        # 汇总
        paper_pnl = 0.0
        paper_n = live_n = 0
        live_notional = 0.0
        for r in rows:
            if r["mode"] == "paper":
                paper_n += 1
                try:
                    paper_pnl += float(r.get("pnl_usd") or 0)
                except Exception:
                    pass
            else:
                live_n += 1
                try:
                    live_notional += float(r.get("notional") or 0)
                except Exception:
                    pass
        summary = {"paper_pnl": round(paper_pnl, 2), "paper_n": paper_n,
                   "live_n": live_n, "live_notional": round(live_notional, 2)}
        rows.sort(key=lambda r: r.get("ts") or "", reverse=True)
        if mode in ("paper", "live"):
            rows = [r for r in rows if r["mode"] == mode]
        total = len(rows)
        page = rows[offset:offset + limit]
        return {"ok": True, "total": total, "rows": page, "summary": summary,
                "has_more": offset + limit < total}





@app.post("/api/password/change")
async def api_change_pw(request: Request, su=Depends(require_session_user)):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "err": "bad request"}, status_code=400)
    if not auth.login_allowed():
        return JSONResponse({"ok": False, "err": "尝试过多, 1分钟后再试"}, status_code=429)
    ok, msg = auth.change_password(su["u"], body.get("old_pw", ""), body.get("new_pw", ""))
    if not ok:
        auth.record_fail()
        return JSONResponse({"ok": False, "err": msg}, status_code=400)
    return {"ok": True, "msg": "密码已修改, 请用新密码重新登录"}


_AI_LIMIT = defaultdict(deque)


def ai_allowed(ip, max_n=15, window=300):
    q = _AI_LIMIT[ip]
    now = time.time()
    while q and now - q[0] > window:
        q.popleft()
    if len(q) >= max_n:
        return False
    q.append(now)
    return True


@app.post("/api/ai/chat")
async def ai_chat(request: Request, su=Depends(require_session_user)):
    ip = request.client.host if request.client else "?"
    if not ai_allowed(ip):
        return JSONResponse({"error": "频率过高, 请稍后再试"}, status_code=429)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    messages = (body.get("messages") or [])[-20:]
    uid = su["u"]

    def tenant_executor(tool, args):
        """工具执行级租户包裹: 仅持锁于单次工具调用, 不阻塞长LLM流"""
        with tenants.tenant(uid):
            return ai_tools.execute_tool(tool, args)

    async def gen():
        try:
            agen = iterate_in_threadpool(
                ai_client.run_agent(messages, ai_tools.TOOLS, tenant_executor))
            async for ev in agen:
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'text': str(e)[:200]}, ensure_ascii=False)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/ai/approve")
async def ai_approve(request: Request, su=Depends(require_session_user)):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad request"}, status_code=400)
    with tenants.tenant(su["u"]):
        return ai_tools.apply_pending(str(body.get("action_id", "")), bool(body.get("approve", False)))


@app.post("/api/spot/open")
async def spot_open(request: Request, su=Depends(require_session_user)):
    """R6 现货下单: side=buy/sell; live=True 走 Bybit 实盘市价"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad request"}, status_code=400)
    sym = str(body.get("symbol") or "").upper()
    side = str(body.get("side") or "buy")
    live = bool(body.get("live"))
    if not re.fullmatch(r"[A-Z0-9]{2,20}", sym):
        return {"ok": False, "error": "symbol 非法"}
    if live:
        return live_exec.bybit_spot_order(su["u"], {"symbol": sym, "side": side,
                                                    "notional": body.get("notional")})
    with tenants.tenant(su["u"]):  # R14-D1: 租户隔离 (修复越权: B用户曾写进admin状态)
        return paper_ops.open_spot(sym, side, body.get("notional"))


@app.post("/api/spot/sltp/set")
async def spot_sltp_set(req: Request, su: dict = Depends(require_session_user)):
    """R14-M2: 现货止损/止盈挂单 {symbol, sl?, tp?}"""
    try:
        b = await req.json()
        with tenants.tenant(su["u"]):
            r = paper_ops.set_spot_sltp(b.get("symbol"), b.get("sl"), b.get("tp"))
        return {"ok": r.get("ok"), "data": r}
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}


@app.post("/api/spot/sltp/clear")
async def spot_sltp_clear(req: Request, su: dict = Depends(require_session_user)):
    """R14-M2: 清除现货止损/止盈 {symbol}"""
    try:
        b = await req.json()
        with tenants.tenant(su["u"]):
            r = paper_ops.clear_spot_sltp(b.get("symbol"))
        return {"ok": r.get("ok"), "data": r}
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}


@app.get("/api/spot/sltp/list")
async def spot_sltp_list(su: dict = Depends(require_session_user)):
    """R14-M2: 全部现货挂单"""
    with tenants.tenant(su["u"]):
        return {"ok": True, "data": {"orders": paper_ops.spot_sltp_list()}}


@app.post("/api/spot/close")
async def spot_close(request: Request, su=Depends(require_session_user)):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad request"}, status_code=400)
    sym = str(body.get("symbol") or "").upper()
    live = bool(body.get("live"))
    if live:
        return live_exec.bybit_spot_order(su["u"], {"symbol": sym, "side": "sell",
                                                    "notional": body.get("notional")})
    with tenants.tenant(su["u"]):  # R14-D1: 租户隔离 (修复越权: B用户曾平admin的仓)
        return paper_ops.close_spot(sym)


@app.get("/api/spot/positions")
def spot_positions(su=Depends(require_session_user)):
    with tenants.tenant(su["u"]):  # R14-D1: 租户隔离 (修复越权: B用户曾看到admin持仓)
        return {"ok": True, "positions": paper_ops.spot_positions()}


@app.post("/api/native/open")
async def native_open(request: Request, su=Depends(require_session_user)):
    """P3 原生交易开仓: 纸面(默认)或实盘(live=1), side=long/short, 任意白名单标的"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad request"}, status_code=400)
    sym = str(body.get("symbol", "")).upper()
    side = body.get("side", "long")
    notional = float(body.get("notional") or 0)
    live = bool(body.get("live"))
    if live:
        with tenants.tenant(su["u"]):
            return live_exec.bybit_open_native(su["u"], {"symbol": sym, "side": side,
                                                         "notional": notional})
    with tenants.tenant(su["u"]):
        return paper_ops.open_native(sym, side, notional)


@app.post("/api/native/close")
async def native_close(request: Request, su=Depends(require_session_user)):
    """P3 原生交易平仓: 纸面(默认)或实盘(live=1)"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad request"}, status_code=400)
    sym = str(body.get("symbol", "")).upper()
    live = bool(body.get("live"))
    if live:
        with tenants.tenant(su["u"]):
            return live_exec.bybit_close_native(su["u"], {"symbol": sym})
    with tenants.tenant(su["u"]):
        return paper_ops.close_native(sym)


@app.get("/api/native/positions")
def native_positions(su=Depends(require_session_user)):
    """P3 原生纸面持仓 + MTM"""
    with tenants.tenant(su["u"]):
        return {"ok": True, "positions": paper_ops.native_positions()}


@app.post("/api/manual/trade")
async def manual_trade(request: Request, su=Depends(require_session_user)):
    """手动纸面交易: open_hedge/close_perp_leg/close_both/close_spot_to_naked/close_naked/edit_naked_tpsl/close_pm (租户隔离)"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad request"}, status_code=400)
    action = str(body.get("action", ""))
    if action not in ("open_hedge", "close_perp_leg", "close_orphan", "close_both",
                      "close_spot_to_naked", "close_naked", "edit_naked_tpsl", "close_pm",
                      "open_pm", "pm_sell_shares", "open_naked"):
        return JSONResponse({"ok": False, "error": f"未知动作: {action}"}, status_code=400)
    with tenants.tenant(su["u"]):
        return paper_ops.execute(action, body)


@app.post("/api/params")
async def api_params_save(request: Request, su=Depends(require_session_user)):
    """手动保存策略参数 (白名单+范围校验, 原子写, git留痕, 引擎热加载生效; 租户隔离)"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad request"}, status_code=400)
    changes = body.get("changes")
    if not isinstance(changes, dict) or not changes:
        return JSONResponse({"ok": False, "error": "未提供修改内容"}, status_code=400)
    with tenants.tenant(su["u"]):
        return ai_tools.apply_params_direct(changes, "manual")


@app.get("/api/mode")
def api_mode(su=Depends(require_session_user)):
    with tenants.tenant(su["u"]):
        return engine_mode.load()


@app.post("/api/mode")
async def api_mode_set(request: Request, su=Depends(require_session_user)):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad request"}, status_code=400)
    with tenants.tenant(su["u"]):
        return engine_mode.set_mode(str(body.get("strategy", "")), str(body.get("mode", "")))


@app.get("/api/prices")
def api_prices_snapshot(__=Depends(require_session)):
    """R13: 行情 REST 兜底快照 (SSE 断线时前端降级轮询)"""
    try:
        with open(os.path.join(config.BASE, "logs", "bybit_prices.json"),
                  encoding="utf-8") as f:
            d = json.load(f)
        return {"ok": True, "prices": d.get("prices", {}), "ts": d.get("ts")}
    except Exception:
        return {"ok": False, "prices": {}}


def _price_delta(prices, seen):
    """SSE diff 核心: 返回 {changed_sym: payload} 并更新 seen (每标的按 ts 去重)"""
    delta = {s: v for s, v in prices.items() if (v.get("ts") or 0) != seen.get(s)}
    if delta:
        seen.update({s: (v.get("ts") or 0) for s, v in delta.items()})
    return delta


@app.get("/api/stream/prices")
async def stream_prices(request: Request, __=Depends(require_session)):
    """SSE: Bybit 实时价格推送 (数据源 = bybit_ws_bridge 原子快照)
    默认全量模式(桌面端): 快照ts变化即推送完整 prices
    ?diff=1 增量模式(移动端): 仅推送有变化的标的, 客户端合并 (首帧 full)"""
    import asyncio
    SNAP = os.path.expanduser("~/polymarket/logs/bybit_prices.json")
    diff_mode = request.query_params.get("diff") == "1"

    async def gen():
        last_ts = None
        last_send = time.time()
        seen = {}  # diff模式: 每标的已推 ts
        sent_full = False
        while True:
            if await request.is_disconnected():
                break
            try:
                with open(SNAP, encoding="utf-8") as f:
                    data = json.loads(f.read())
                snap_ts = data.get("ts")
                if diff_mode:
                    prices = data.get("prices") or {}
                    if not sent_full:
                        seen = {s: (v.get("ts") or 0) for s, v in prices.items()}
                        sent_full = True
                        # R8 首帧分片: 高成交额 Top120 先推(首屏秒显), 其余 delta 合并
                        items = sorted(prices.items(),
                                       key=lambda kv: -(kv[1].get("vol") or 0))
                        top = dict(items[:120])
                        rest = dict(items[120:])
                        yield f"data: {json.dumps({'ts': snap_ts, 'prices': top, 'full': True}, ensure_ascii=False)}\n\n"
                        last_send = time.time()
                        if rest:
                            await asyncio.sleep(0.15)
                            yield f"data: {json.dumps({'ts': snap_ts, 'prices': rest, 'delta': True}, ensure_ascii=False)}\n\n"
                    else:
                        delta = _price_delta(prices, seen)
                        if delta:
                            yield f"data: {json.dumps({'ts': snap_ts, 'prices': delta, 'delta': True}, ensure_ascii=False)}\n\n"
                            last_send = time.time()
                elif snap_ts != last_ts:
                    last_ts = snap_ts
                    last_send = time.time()
                    yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
            except Exception:
                pass
            if time.time() - last_send > 15:
                last_send = time.time()
                yield ": ping\n\n"  # 心跳注释行, 防超时
            await asyncio.sleep(0.3)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/instruments")
def api_instruments(__=Depends(require_session)):
    """R13c: 标的收敛 — 只返回白名单 BTCUSDT/ETHUSDT/XAUUSDT/XAGUSDT/XAUTUSDT"""
    WL = set(os.environ.get("BYBIT_SYMS", "BTCUSDT,ETHUSDT,XAUUSDT,XAGUSDT,XAUTUSDT,SOLUSDT,NEARUSDT,XRPUSDT").split(","))  # R14-M2: +SOL/NEAR/XRP
    SPOT_ONLY = {"XAUTUSDT"}  # R14: 现货独占标的 (linear 清单中剔除, 防前端误标永续)
    try:
        with open(os.path.expanduser("~/polymarket/logs/bybit_instruments.json"),
                  encoding="utf-8") as f:
            d = json.load(f)
        def _slim(lst, exclude_spot_only=False):
            out = []
            for it in lst:
                if it.get("symbol") not in WL:
                    continue
                if exclude_spot_only and it.get("symbol") in SPOT_ONLY:
                    continue
                out.append({"symbol": it.get("symbol"), "name": it.get("name", ""),
                            "base": it.get("base", ""), "quote": it.get("quote", "USDT"),
                            "turnover24h": it.get("turnover24h", 0),
                            "lastPrice": it.get("lastPrice"),
                            "tickSize": it.get("tickSize"), "qtyStep": it.get("qtyStep")})
            return out
        return {"ok": True, "linear": _slim(d.get("linear", []), exclude_spot_only=True),
                "spot": _slim(d.get("spot", [])), "ts": d.get("ts")}
    except Exception:
        return {"ok": True, "linear": [], "spot": [], "ts": None}


@app.post("/api/kline/watch")
async def kline_watch(request: Request, su=Depends(require_session_user)):
    """R4 按需K线: 写 kline_watch.json → 桥2s内订阅 kline.<iv>.<sym> (实时末根)"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad request"}, status_code=400)
    sym = str(body.get("symbol") or "").upper()
    iv = str(body.get("interval") or "15m")
    cat = str(body.get("category") or "linear")
    if cat not in ("linear", "spot"):
        cat = "linear"
    if not re.fullmatch(r"[A-Z0-9]{2,20}", sym):
        return {"ok": False, "error": "symbol 非法"}
    if iv not in ("1m", "5m", "15m", "1h", "4h", "D", "W", "M"):
        return {"ok": False, "error": "interval 非法"}
    try:
        import pathlib
        f = pathlib.Path(config.BASE) / "logs" / "kline_watch.json"
        reqs = {}
        if f.exists():
            try:
                reqs = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                reqs = {}
        reqs[sym] = f"{iv}|{cat}"
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(reqs), encoding="utf-8")
        tmp.replace(f)
    except Exception as e:
        return {"ok": False, "error": f"写watch失败: {e}"}
    return {"ok": True, "msg": f"{sym} {iv} 实时K线订阅中"}


@app.post("/api/depth/watch")
def api_depth_watch(body: dict, __=Depends(require_session)):
    """P2 按需盘口: 前端请求标的 → 写 depth_watch.json → 桥2s内订阅 orderbook.200 (LRU 20)"""
    sym = str(body.get("symbol", "")).upper()
    if not sym or not re.fullmatch(r"[A-Z0-9]{3,20}", sym):
        return JSONResponse({"ok": False, "error": "symbol 非法"}, status_code=400)
    path = os.path.expanduser("~/polymarket/logs/depth_watch.json")
    try:
        with open(path, encoding="utf-8") as f:
            reqs = json.load(f)
    except Exception:
        reqs = {}
    reqs[sym] = time.time()
    # 只保留最近 40 个请求标的
    reqs = dict(sorted(reqs.items(), key=lambda kv: -kv[1])[:40])
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(reqs, f)
    os.replace(tmp, path)
    return {"ok": True, "symbol": sym, "watching": list(reqs)}


# ================= M7 USDT 充值提现 (用户端) =================

@app.get("/api/funds/balance")
def funds_balance(su=Depends(require_session_user)):
    return {"ok": True, "usdt": funds.get_balance(su["u"]),
            "tx": funds.tx_list(su["u"], 30)}


@app.post("/api/funds/deposit/create")
async def funds_deposit_create(request: Request, su=Depends(require_session_user)):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad request"}, status_code=400)
    return funds.create_deposit(su["u"], body.get("amount"))


@app.get("/api/funds/deposits")
def funds_deposits(su=Depends(require_session_user)):
    return {"ok": True, "orders": funds.my_deposits(su["u"], 20),
            "address": funds.platform_address()}


@app.post("/api/funds/withdraw/create")
async def funds_withdraw_create(request: Request, su=Depends(require_session_user)):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad request"}, status_code=400)
    return funds.create_withdraw(su["u"], body.get("amount"), body.get("address"))


@app.get("/api/funds/withdraws")
def funds_withdraws(su=Depends(require_session_user)):
    return {"ok": True, "orders": funds.my_withdraws(su["u"], 20)}


@app.get("/api/funds/plans")
def funds_plans(su=Depends(require_session_user)):
    u = users.get_user(su["u"]) or {}
    exp = float(u.get("plan_expires") or 0)
    return {"ok": True, "plans": funds.plans_list(),
            "my_plan": u.get("plan", "free"), "plan_expires": exp,
            "settings": funds.all_settings()}


@app.post("/api/funds/plan/buy")
async def funds_plan_buy(request: Request, su=Depends(require_session_user)):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad request"}, status_code=400)
    return funds.buy_plan(su["u"], str(body.get("code", "")))


# ================= M7 USDT 充值提现 (管理员端) =================

@app.get("/api/admin/funds/summary")
def admin_funds_summary(su=Depends(require_admin)):
    return {"ok": True, "pending_withdraws": funds.pending_withdraws(),
            "unclaimed": funds.unclaimed_list(),
            "settings": funds.all_settings(),
            "plans": funds.plans_list(include_inactive=True)}


@app.post("/api/admin/funds/claim")
async def admin_funds_claim(request: Request, su=Depends(require_admin)):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad request"}, status_code=400)
    return funds.claim_deposit(su["u"], body.get("order_id"), body.get("uid"))


@app.post("/api/admin/funds/withdraw/review")
async def admin_funds_review(request: Request, su=Depends(require_admin)):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad request"}, status_code=400)
    r = funds.review_withdraw(su["u"], body.get("id"), bool(body.get("approve")),
                              body.get("note", ""))
    users.audit_log(int(su["u"]), "withdraw_review",
                    f"提现单#{body.get('id')} {'批准' if body.get('approve') else '驳回'}",
                    "", f"admin:{su['u']}")  # R14-M3: 管理操作审计全覆盖
    return r


@app.post("/api/admin/funds/adjust")
async def admin_funds_adjust(request: Request, su=Depends(require_admin)):
    """余额调整 (正入负出, 审计留痕)"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad request"}, status_code=400)
    try:
        amt = float(body.get("amount") or 0)
    except Exception:
        return JSONResponse({"ok": False, "error": "金额非法"}, status_code=400)
    if abs(amt) < 0.0001:
        return {"ok": False, "error": "金额为 0"}
    bal = funds.add_balance(int(body.get("uid")), amt, "admin",
                            body.get("note", ""), f"管理员调整 ({su['u']})")
    users.audit_log(int(su["u"]), "funds_adjust",
                    f"用户{body.get('uid')}余额调整 {amt:+.2f} USDT ({body.get('note','')})",
                    "", f"admin:{su['u']}")  # R14-M3: 管理操作审计全覆盖
    return {"ok": True, "new_balance": bal}


@app.post("/api/admin/funds/settings")
async def admin_funds_settings(request: Request, su=Depends(require_admin)):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad request"}, status_code=400)
    key = str(body.get("key", ""))
    if key not in ("withdraw_fee", "max_withdraw", "min_withdraw", "deposit_min", "deposit_max"):
        return {"ok": False, "error": "不支持的设置项"}
    return funds.set_setting(key, body.get("value"))


@app.post("/api/admin/funds/plan")
async def admin_funds_plan(request: Request, su=Depends(require_admin)):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad request"}, status_code=400)
    return funds.save_plan(body.get("code"), body.get("name"), body.get("price"),
                           body.get("features"), body.get("sort"),
                           body.get("active", True), bool(body.get("is_new")))


@app.post("/api/admin/funds/plan/delete")
async def admin_funds_plan_delete(request: Request, su=Depends(require_admin)):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad request"}, status_code=400)
    return funds.delete_plan(body.get("code", ""))


@app.post("/api/admin/funds/platform_key")
async def admin_funds_platform_key(request: Request, su=Depends(require_admin)):
    """保存平台 Bybit 密钥 (AES-256-GCM 加密落盘, 仅管理员)"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad request"}, status_code=400)
    k = str(body.get("api_key", "")).strip()
    s = str(body.get("secret", "")).strip()
    if not k or not s:
        return {"ok": False, "error": "key/secret 必填"}
    try:
        funds.save_platform_key(k, s)
    except Exception as e:
        return {"ok": False, "error": f"加密保存失败: {e}"}
    users.audit_log(su["u"], "platform_key_saved", "平台 Bybit 密钥已更新")
    return {"ok": True}


@app.post("/api/pm/order/market")
async def pm_market_order(request: Request, su=Depends(require_session_user)):
    """P4 PM 原生市价吃单 (实盘): {token_id 或 key, side: BUY/SELL, amount_usd}"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad request"}, status_code=400)
    with tenants.tenant(su["u"]):
        return live_exec.pm_market_order(su["u"], body)


@app.get("/api/pm/prices")
def api_pm_prices(__=Depends(require_session)):
    """P4 PM 实时价 (pm_clob_ws 桥快照): {n, ts, prices: {token: {bid, ask, last, ts}}}"""
    try:
        with open(os.path.expanduser("~/polymarket/logs/pm_prices.json"),
                  encoding="utf-8") as f:
            d = json.load(f)
        return {"ok": True, "n": d.get("n", 0), "ts": d.get("ts"),
                "prices": d.get("prices", {})}
    except Exception:
        return {"ok": True, "n": 0, "ts": None, "prices": {}}


_PM_TOK_CACHE = {"t": 0, "data": None}
_PM_ZH_CACHE = {"t": 0, "data": {"t": {}, "q": {}}}


def _pm_zh():
    """R11: 中文翻译表 {t:{title:zh}, q:{question:zh}} (DeepSeek 批量翻译, 60s 缓存)"""
    now = time.time()
    if now - _PM_ZH_CACHE["t"] > 60:
        try:
            with open(os.path.expanduser("~/polymarket/logs/pm_zh.json"),
                      encoding="utf-8") as f:
                _PM_ZH_CACHE["data"] = json.load(f)
        except Exception:
            _PM_ZH_CACHE["data"] = {"t": {}, "q": {}}
        _PM_ZH_CACHE["t"] = now
    return _PM_ZH_CACHE["data"]


@app.get("/api/admin/pm-markets")
def api_pm_markets(request: Request, __=Depends(require_admin)):
    """R12 P2: PM 标的列表 (搜索+状态过滤+分页)"""
    try:
        offset = max(0, int(request.query_params.get("offset") or 0))
    except Exception:
        offset = 0
    try:
        limit = max(10, min(int(request.query_params.get("limit") or 100), 500))
    except Exception:
        limit = 100
    return {"ok": True,
            **pm_admin.list_markets(request.query_params.get("q", ""),
                                    request.query_params.get("state", ""),
                                    offset, limit)}


@app.post("/api/admin/pm-markets/set")
async def api_pm_markets_set(request: Request, __=Depends(require_admin)):
    """R12 P2: 批量上下线 {keys:[...], state:on/off, note}"""
    try:
        body = await request.json()
    except Exception:
        return {"ok": False, "error": "bad request"}
    keys = body.get("keys") or []
    if not isinstance(keys, list) or not keys or len(keys) > 500:
        return {"ok": False, "error": "keys 须为 1-500 数组"}
    state = str(body.get("state") or "")
    try:
        n = pm_admin.set_state([str(k) for k in keys], state,
                               str(body.get("note") or "")[:100])
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "msg": f"已{'上线' if state == 'on' else '下线'} {n} 个标的", "n": n}


@app.get("/api/pm/tokens")
def api_pm_tokens(request: Request, __=Depends(require_session)):
    """R13c: PM 功能已下线, 浏览/搜索接口一律返回空"""
    return {"ok": True, "rows": [], "total": 0, "cats": [], "off": True}


@app.get("/api/pm/tokens_legacy_off")
def api_pm_tokens_legacy(request: Request, __=Depends(require_session)):
    """P4 token→市场 映射, R8 懒加载: ?cat=&q=&offset=&limit= 服务端过滤分页
    默认只回第一页 (12596 条全量 5.8MB 压垮首载 → 按需加载)"""
    try:
        now = time.time()
        if _PM_TOK_CACHE["data"] is None or now - _PM_TOK_CACHE["t"] > 60:
            with open(os.path.expanduser("~/polymarket/logs/pm_tokens.json"),
                      encoding="utf-8") as f:
                _PM_TOK_CACHE["data"] = json.load(f)
            _PM_TOK_CACHE["t"] = now
        toks = _PM_TOK_CACHE["data"] or []
    except Exception:
        return {"ok": True, "tokens": [], "total": 0, "cats": {}}
    # R12 P2: 过滤下线标的
    off = pm_admin.off_keys()
    if off:
        toks = [t for t in toks if t.get("key") not in off]
    cat = (request.query_params.get("cat") or "").strip().lower()
    q = (request.query_params.get("q") or "").strip().lower()
    try:
        offset = max(0, int(request.query_params.get("offset") or 0))
    except Exception:
        offset = 0
    try:
        limit = max(10, min(int(request.query_params.get("limit") or 300), 500))
    except Exception:
        limit = 300
    # 分类统计 (缓存期内每次循环 ~10ms, 可接受)
    cats = {}
    for t in toks:
        c = t.get("cat") or "other"
        cats[c] = cats.get(c, 0) + 1
    if cat:
        toks = [t for t in toks if (t.get("cat") or "other") == cat]
    zh = _pm_zh()  # 中文表始终加载 (60s 缓存, 开销小)
    if q:
        ql = q.lower()
        def _hit(t):
            if ql in (t.get("title") or "").lower():
                return True
            if ql in (t.get("question") or "").lower():
                return True
            if ql in (t.get("key") or "").lower():
                return True
            # 中文匹配 (R11): 中文查询词命中翻译表
            if zh:
                zt = (zh["t"].get(t.get("title") or "") or "").lower()
                zq = (zh["q"].get(t.get("question") or "") or "").lower()
                if ql in zt or ql in zq:
                    return True
            return False
        toks = [t for t in toks if _hit(t)]
    total = len(toks)
    page = toks[offset:offset + limit]
    # 精简字段 (降带宽) + 中文 (R11)
    slim = []
    for t in page:
        it = {"token": t["token"], "key": t["key"], "title": t.get("title", ""),
              "question": t.get("question", ""), "cat": t.get("cat", "other"),
              "outcome": t.get("outcome", ""),
              "ev_vol": t.get("ev_vol", 0), "mk_chg": t.get("mk_chg", 0),
              "mk_vol": t.get("mk_vol", 0)}
        if zh:
            it["title_zh"] = zh["t"].get(t.get("title") or "") or ""
            it["question_zh"] = zh["q"].get(t.get("question") or "") or ""
        slim.append(it)
    return {"ok": True, "tokens": slim, "total": total, "cats": cats,
            "has_more": offset + limit < total}


@app.get("/api/stream/pm")
async def stream_pm(request: Request, __=Depends(require_session)):
    """P4 SSE: PM 实时价 (diff 增量, 首帧 full; 数据源 pm_prices.json)"""
    import asyncio
    SNAP = os.path.expanduser("~/polymarket/logs/pm_prices.json")

    async def gen():
        seen = {}
        sent_full = False
        last_send = time.time()
        while True:
            if await request.is_disconnected():
                break
            try:
                with open(SNAP, encoding="utf-8") as f:
                    data = json.loads(f.read())
                prices = data.get("prices") or {}
                if not sent_full:
                    seen = {s: (v.get("ts") or 0) for s, v in prices.items()}
                    sent_full = True
                    yield f"data: {json.dumps({'ts': data.get('ts'), 'prices': prices, 'full': True}, ensure_ascii=False)}\n\n"
                    last_send = time.time()
                else:
                    delta = _price_delta(prices, seen)
                    if delta:
                        yield f"data: {json.dumps({'ts': data.get('ts'), 'prices': delta, 'delta': True}, ensure_ascii=False)}\n\n"
                        last_send = time.time()
            except Exception:
                pass
            if time.time() - last_send > 15:
                last_send = time.time()
                yield ": ping\n\n"
            await asyncio.sleep(0.4)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/stream/depth")
async def stream_depth(request: Request, __=Depends(require_session)):
    """SSE: 盘口+成交量实时推送 (数据源 = bybit_ws_bridge orderbook.json, 1s 轮读)"""
    import asyncio
    DEPTH = os.path.expanduser("~/polymarket/logs/orderbook.json")

    async def gen():
        last_ts = None
        last_send = time.time()
        while True:
            if await request.is_disconnected():
                break
            try:
                with open(DEPTH, encoding="utf-8") as f:
                    data = json.loads(f.read())
                if data.get("ts") != last_ts:
                    last_ts = data["ts"]
                    last_send = time.time()
                    yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
            except Exception:
                pass
            if time.time() - last_send > 15:
                last_send = time.time()
                yield ": ping\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/cycle")
def api_cycle(su=Depends(require_session_user)):
    with tenants.tenant(su["u"]):
        return readers.cycle()


@app.get("/api/micro")
def api_micro(__=Depends(require_session)):
    return readers.micro()


@app.get("/api/system")
def api_system(su=Depends(require_session_user)):
    with tenants.tenant(su["u"]):
        return readers.system()


# ---------- M3 总管理后台 (仅 admin) ----------

@app.get("/admin")
def admin_page(__=Depends(require_admin)):
    return FileResponse(STATIC / "admin.html")


@app.get("/api/admin/stats")
def api_admin_stats(__=Depends(require_admin)):
    return {**admin.stats_overview(), "ok": True}


@app.get("/api/admin/users")
def api_admin_users(request: Request, __=Depends(require_admin)):
    """R13 P2: 支持 ?offset=&limit= 分页 (limit<=0 返回全量, 兼容旧调用)"""
    try:
        offset = max(0, int(request.query_params.get("offset") or 0))
    except Exception:
        offset = 0
    try:
        limit = int(request.query_params.get("limit") or 0)
    except Exception:
        limit = 0
    if limit > 0:
        return {**admin.list_users_with_stats(offset, limit), "ok": True}
    return {"rows": admin.list_users_with_stats(), "ok": True}


@app.post("/api/admin/user/{uid}/status")
async def api_admin_status(uid: int, request: Request, su=Depends(require_admin)):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad request"}, status_code=400)
    ok, msg = users.set_status(uid, body.get("status", ""))
    return {"ok": ok, "msg": msg}


@app.post("/api/admin/user/{uid}/fee-tier")
async def api_admin_fee_tier(uid: int, request: Request, su=Depends(require_admin)):
    """R14-M3: 设置用户费率等级 0=标准 1=VIP(减半)"""
    try:
        body = await request.json()
    except Exception:
        return {"ok": False, "error": "bad request"}
    tier = int(body.get("tier", 0))
    if tier not in (0, 1):
        return {"ok": False, "error": "tier 仅支持 0/1"}
    users.set_fee_tier(uid, tier)
    users.audit_log(int(su["u"]), "fee_tier", f"用户{uid}费率等级改为 tier={tier}", "", f"admin:{su['u']}")
    return {"ok": True, "msg": f"已设置 tier={tier} (标准/VIP)"}


@app.post("/api/admin/user/{uid}/plan")
async def api_admin_plan(uid: int, request: Request, su=Depends(require_admin)):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad request"}, status_code=400)
    ok, msg = admin.set_plan(uid, body.get("plan", ""), su["u"], body.get("days"))
    return {"ok": ok, "msg": msg}


@app.get("/api/admin/plans")
def api_admin_plans(su=Depends(require_admin)):
    """R14-M16: 套餐定义 (价格+默认托管时长)"""
    return {"ok": True, "plans": funds.plan_defs()}


@app.post("/api/admin/plans")
async def api_admin_plans_set(request: Request, su=Depends(require_admin)):
    """R14-M16: 改套餐价格/默认时长 (free 禁改)"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad request"}, status_code=400)
    code = str(body.get("code", ""))
    ok, msg = funds.set_plan_def(code, body.get("price"), body.get("days"),
                                 body.get("billing_period"), body.get("name"), body.get("features"))
    users.audit_log(su["u"], "plan_def_change",
                    f"套餐 {code}: price={body.get('price')} period={body.get('billing_period')} days={body.get('days')} → {msg}")
    return {"ok": ok, "msg": msg}


@app.post("/api/admin/plans/add")
async def api_admin_plans_add(request: Request, su=Depends(require_admin)):
    """R14-M17: 新增套餐"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad request"}, status_code=400)
    ok, msg = funds.add_plan(body.get("code"), body.get("name"), body.get("price", 0),
                             body.get("days", 30), body.get("billing_period", "month"),
                             body.get("features", ""))
    users.audit_log(su["u"], "plan_add", f"新增套餐 {body.get('code')} → {msg}")
    return {"ok": ok, "msg": msg}


@app.post("/api/admin/plans/toggle")
async def api_admin_plans_toggle(request: Request, su=Depends(require_admin)):
    """R14-M17: 启用/停用套餐"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad request"}, status_code=400)
    ok, msg = funds.toggle_plan(str(body.get("code", "")), bool(body.get("active")))
    users.audit_log(su["u"], "plan_toggle", f"套餐 {body.get('code')} active={body.get('active')} → {msg}")
    return {"ok": ok, "msg": msg}


@app.post("/api/admin/user/{uid}/password")
async def api_admin_pw(uid: int, request: Request, su=Depends(require_admin)):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad request"}, status_code=400)
    ok, msg = admin.reset_password(uid, body.get("password", ""), su["u"])
    return {"ok": ok, "msg": msg}


@app.get("/api/admin/audit")
def api_admin_audit(uid: int = 0, action: str = "", limit: int = 200,
                    offset: int = 0, __=Depends(require_admin)):
    total, rows = admin.audit_query(uid or None, action or None, min(limit, 500), offset)
    return {"total": total, "rows": rows, "ok": True}


@app.get("/api/admin/announcements")
def api_admin_ann_list(__=Depends(require_admin)):
    return {"rows": admin.announce_list(all_=True), "ok": True}


@app.post("/api/admin/announcements")
async def api_admin_ann_add(request: Request, su=Depends(require_admin)):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad request"}, status_code=400)
    ok, msg = admin.announce_add(body.get("text", ""), body.get("level", "info"), su["u"])
    return {"ok": ok, "msg": msg}


@app.post("/api/admin/announcements/{aid}/toggle")
async def api_admin_ann_toggle(aid: int, request: Request, su=Depends(require_admin)):
    try:
        body = await request.json()
    except Exception:
        body = {}
    ok, msg = admin.announce_toggle(aid, bool(body.get("active", True)), su["u"])
    return {"ok": ok, "msg": msg}


@app.get("/api/announcements")
def api_announcements(__=Depends(require_session)):
    """登录用户读取生效公告 (交易室横幅数据源)"""
    return {"rows": admin.announce_list(all_=False)}


# ---------- M4 密钥托管 (用户自己的密钥, AES-GCM 加密存储) ----------

@app.get("/keys")
def keys_page(__=Depends(require_session)):
    return FileResponse(STATIC / "keys.html")


@app.get("/terms")
def terms_page():
    """服务条款与风险披露 (无需登录)"""
    return FileResponse(STATIC / "terms.html")


@app.get("/m")
def mobile_page():
    """移动版交易室 (手机优先, 全功能: 登录/行情/PM/交易/资产/密钥/管理)
    页面本身含登录/注册屏, 无需会话拦截; 登录态由页内 /api/auth/me 探测"""
    return FileResponse(STATIC / "m.html")


@app.get("/api/keys")
def api_keys(su=Depends(require_session_user)):
    return {"keys": keys.list_keys(su["u"]), "limits": keys.get_limits(su["u"])}


@app.post("/api/keys/bybit/bind")
async def api_keys_bybit_bind(request: Request, su=Depends(require_session_user)):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad request"}, status_code=400)
    u = users.get_user(su["u"])
    if u.get("plan") not in ("pro", "live"):
        return JSONResponse({"ok": False,
                             "error": "绑定交易所密钥需专业版或实盘版套餐 (当前: 免费版)"}, status_code=403)
    api_key = (body.get("key") or "").strip()
    secret = (body.get("secret") or "").strip()
    if not api_key or not secret:
        return JSONResponse({"ok": False, "error": "密钥和 Secret 均不能为空"}, status_code=400)
    ok, msg = keys.bind(su["u"], "bybit", api_key, secret)
    if not ok:
        return JSONResponse({"ok": False, "error": msg}, status_code=400)
    users.audit_log(su["u"], "key_bind", "绑定 Bybit API 密钥")
    # 绑定后立即连通测试 (不阻塞绑定结果)
    try:
        secs = keys.get_secrets(su["u"], "bybit")
        ok_t, msg_t = bybit_live.test_bybit(secs["key"], secs["secret"])
        keys.mark_test(su["u"], "bybit", ok_t, msg_t)
        users.audit_log(su["u"], "key_test", f"Bybit 连通测试: {'通过' if ok_t else '失败: ' + msg_t[:80]}")
        return {"ok": True, "msg": "已绑定" + (" (连通测试通过)" if ok_t else " (测试未通过, 见测试结果)"),
                "test_ok": ok_t, "test_msg": msg_t}
    except Exception as e:
        return {"ok": True, "msg": "已绑定 (测试暂不可用)", "test_ok": None,
                "test_msg": f"{type(e).__name__}: {e}"[:120]}


@app.post("/api/keys/bybit/test")
async def api_keys_bybit_test(su=Depends(require_session_user)):
    secs = keys.get_secrets(su["u"], "bybit")
    if not secs:
        return JSONResponse({"ok": False, "error": "未绑定 Bybit 密钥"}, status_code=400)
    try:
        ok_t, msg_t = bybit_live.test_bybit(secs["key"], secs["secret"])
    except Exception as e:
        ok_t, msg_t = False, f"{type(e).__name__}: {e}"[:200]
    keys.mark_test(su["u"], "bybit", ok_t, msg_t)
    users.audit_log(su["u"], "key_test", f"Bybit 连通测试: {'通过' if ok_t else '失败: ' + msg_t[:80]}")
    return {"ok": True, "test_ok": ok_t, "test_msg": msg_t}


@app.post("/api/keys/bybit/unbind")
async def api_keys_bybit_unbind(su=Depends(require_session_user)):
    keys.unbind(su["u"], "bybit")
    users.audit_log(su["u"], "key_unbind", "解绑 Bybit 密钥")
    return {"ok": True, "msg": "已解绑"}


@app.post("/api/keys/pm/bind")
async def api_keys_pm_bind(request: Request, su=Depends(require_session_user)):
    """PM 绑定(一次性): owner私钥 → SDK 派生 L2 凭证 → 私钥即弃, 只存派生凭证"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad request"}, status_code=400)
    u = users.get_user(su["u"])
    if u.get("plan") not in ("pro", "live"):
        return JSONResponse({"ok": False,
                             "error": "绑定交易所密钥需专业版或实盘版套餐 (当前: 免费版)"}, status_code=403)
    private_key = (body.get("private_key") or "").strip()
    wallet = (body.get("wallet") or "").strip() or None
    relayer_key = (body.get("relayer_key") or "").strip() or None
    relayer_address = (body.get("relayer_address") or "").strip() or None
    if not private_key:
        return JSONResponse({"ok": False, "error": "owner 私钥不能为空 (一次性导入, 派生凭证后立即丢弃)"}, status_code=400)
    if relayer_key and not relayer_address:
        return JSONResponse({"ok": False, "error": "Relayer 密钥需同时提供签名者地址"}, status_code=400)
    try:
        creds = pm_live.derive_credentials(private_key, wallet, relayer_key, relayer_address)
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"[:200]
        users.audit_log(su["u"], "key_bind", "PM 绑定失败: " + msg)
        return JSONResponse({"ok": False, "error": f"派生失败(私钥或钱包不匹配): {msg}"}, status_code=400)
    extra = json.dumps({"passphrase": creds["passphrase"], "wallet": creds["wallet"],
                        "relayer_key": relayer_key or "", "relayer_address": relayer_address or ""})
    keys.bind(su["u"], "pm", creds["apiKey"], creds["secret"], extra=extra,
              label=f"wallet {creds['wallet'][:10]}…")
    keys.mark_test(su["u"], "pm", True, f"派生成功, 当前持仓 {creds['n_positions']} 个")
    users.audit_log(su["u"], "key_bind", f"绑定 PM (钱包 {creds['wallet'][:12]}…, 私钥已即弃)")
    return {"ok": True, "msg": f"绑定成功: 钱包 {creds['wallet'][:12]}… 持仓 {creds['n_positions']} 个",
            "wallet": creds["wallet"]}


@app.post("/api/keys/pm/test")
async def api_keys_pm_test(su=Depends(require_session_user)):
    secs = keys.get_secrets(su["u"], "pm")
    if not secs:
        return JSONResponse({"ok": False, "error": "未绑定 PM"}, status_code=400)
    extra = json.loads(secs["extra"] or "{}")
    creds = {"apiKey": secs["key"], "secret": secs["secret"],
             "passphrase": extra.get("passphrase", "")}
    wallet = extra.get("wallet", "")
    try:
        ok_t, msg_t = pm_live.test_credentials(creds, wallet)
    except Exception as e:
        ok_t, msg_t = False, f"{type(e).__name__}: {e}"[:200]
    keys.mark_test(su["u"], "pm", ok_t, msg_t)
    return {"ok": True, "test_ok": ok_t, "test_msg": msg_t}


@app.post("/api/keys/pm/unbind")
async def api_keys_pm_unbind(su=Depends(require_session_user)):
    keys.unbind(su["u"], "pm")
    users.audit_log(su["u"], "key_unbind", "解绑 PM 凭证")
    return {"ok": True, "msg": "已解绑"}


@app.post("/api/admin/limits/{uid}")
async def api_admin_limits(uid: int, request: Request, su=Depends(require_admin)):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad request"}, status_code=400)
    ok, msg = keys.set_limits(uid, body.get("max_notional"), body.get("daily_loss_cap"),
                              body.get("max_positions"), body.get("live_enabled"))
    users.audit_log(uid, "limits_change", f"风控限额更新: {body}", "", f"admin:{su['u']}")
    return {"ok": ok, "msg": msg}


# ---------- M5 实盘引擎 (真实资金, 全链路风控+二次确认) ----------

@app.get("/api/live/status")
def api_live_status(su=Depends(require_session_user)):
    try:
        return {"ok": True, **live_exec.status(su["u"])}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:200]}


@app.post("/api/live/order")
async def api_live_order(request: Request, su=Depends(require_session_user)):
    """实盘下单: 必须 confirm=true (前端二次确认弹窗), 全链路风控闸"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad request"}, status_code=400)
    if not body.get("confirm"):
        return JSONResponse({"ok": False, "error": "需在确认弹窗中二次确认 (confirm=true)"}, status_code=400)
    action = str(body.get("action", ""))
    bybit_map = {"open_naked": live_exec.bybit_open_naked,
                 "close_naked": live_exec.bybit_close_naked,
                 "open_hedge": live_exec.bybit_open_hedge,
                 "close_perp_leg": live_exec.bybit_close_perp_leg,
                 "close_both": live_exec.bybit_close_both}
    try:
        if action in bybit_map:
            r = bybit_map[action](su["u"], body)
        elif action == "pm_order":
            r = live_exec.pm_order(su["u"], body)
        else:
            r = {"ok": False, "error": f"实盘不支持该动作: {action}"}
    except Exception as e:
        r = {"ok": False, "error": f"{type(e).__name__}: {e}"[:200]}
    return JSONResponse(r, status_code=200 if r.get("ok") else 400)


# ================= R14-M12 平台营收体系 (费率配置 + 营收大盘) =================
_SYM_WHITELIST = [s.strip().upper() for s in os.environ.get(
    "BYBIT_SYMS", "BTCUSDT,ETHUSDT,XAUUSDT,XAGUSDT,XAUTUSDT,SOLUSDT,NEARUSDT,XRPUSDT").split(",") if s.strip()]

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import fee_ops  # noqa: E402


def _check_fee_value(v, lo, hi):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    if not (lo <= v <= hi):
        return None
    return v


@app.get("/api/admin/fee-config")
def api_admin_fee_config(request: Request, __=Depends(require_admin)):
    """当前配置 + 官方费率参考 + 白名单"""
    cfg = fee_ops.load()
    out = {"default": cfg.get("default", {}), "symbols": {}}
    for s in _SYM_WHITELIST:
        c = (cfg.get("symbols") or {}).get(s, {})
        out["symbols"][s] = {
            "perp_mult": c.get("perp_mult", cfg.get("default", {}).get("perp_mult", 1.0)),
            "spot_mult": c.get("spot_mult", cfg.get("default", {}).get("spot_mult", 1.0)),
            "spread_bp": c.get("spread_bp", cfg.get("default", {}).get("spread_bp", 0)),
            "official_perp_bp": round(fee_ops.official("perp", s) * 10000, 2),
            "official_spot_bp": round(fee_ops.official("spot", s) * 10000, 2),
        }
    out["updated_by"] = cfg.get("updated_by", "")
    out["updated_at"] = cfg.get("updated_at", "")
    out["whitelist"] = list(_SYM_WHITELIST)
    return {"ok": True, "data": out}


@app.get("/api/admin/fee-config/history")
def api_admin_fee_history(__=Depends(require_admin)):
    """R14-M12: 费率配置变更历史 (最近10次)"""
    try:
        return {"ok": True, "items": admin.fee_history(10)}
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"[:200]}, status_code=500)


@app.post("/api/admin/fee-config")
async def api_admin_fee_config_save(request: Request, __=Depends(require_admin)):
    """保存费率配置 (白名单校验 + 范围钳制 + 审计)"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "请求体非法"}, status_code=400)
    defv = body.get("default") or {}
    syms = body.get("symbols") or {}
    if not isinstance(syms, dict):
        return JSONResponse({"ok": False, "error": "symbols 格式错误"}, status_code=400)
    cfg = {"default": {}, "symbols": {}}
    for k, lo, hi in (("perp_mult", 0.5, 5.0), ("spot_mult", 0.5, 5.0), ("spread_bp", 0, 50)):
        v = _check_fee_value(defv.get(k), lo, hi)
        if v is None:
            return JSONResponse({"ok": False, "error": f"默认 {k} 须在 {lo}~{hi}"}, status_code=400)
        cfg["default"][k] = v
    for s in _SYM_WHITELIST:
        row = syms.get(s) or {}
        item = {}
        for k, lo, hi in (("perp_mult", 0.5, 5.0), ("spot_mult", 0.5, 5.0), ("spread_bp", 0, 50)):
            v = _check_fee_value(row.get(k, defv.get(k)), lo, hi)
            if v is None:
                return JSONResponse({"ok": False, "error": f"{s} 的 {k} 非法"}, status_code=400)
            item[k] = v
        cfg["symbols"][s] = item
    try:
        fee_ops.save(cfg, who="admin")
        _audit_req(request, "fee_config_save", {"symbols": cfg["symbols"], "default": cfg["default"]})
        return {"ok": True, "msg": "费率配置已保存 (即时生效)"}
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"[:200]}, status_code=500)


def _audit_req(request, action, detail):
    try:
        from . import users as _u
        su = auth.session_user(request.cookies.get(config.COOKIE_NAME))
        if isinstance(detail, (dict, list)):
            detail = json.dumps(detail, ensure_ascii=False)[:500]
        _u.audit_log(int((su or {}).get("u", 1)), action, detail,
                     request.client.host if request.client else "", "")
    except Exception as _e:
        try:
            with open("/tmp/mb_audit_err.log", "a", encoding="utf-8") as _f:
                _f.write(f"{type(_e).__name__}: {_e}\n")
        except Exception:
            pass


def _rev_scan(uid, mode):
    """扫描全部租户留痕, 返回 [(uid, rec, mode)] — mode: all|paper|live"""
    rows = []
    uids = [1]
    try:
        tdir = os.path.dirname(tenants.base(2))   # ROOT/tenants
        if os.path.isdir(tdir):
            for d in os.listdir(tdir):
                if d.isdigit():
                    uids.append(int(d))
    except Exception:
        pass
    for u in sorted(set(uids)):
        base = tenants.base(u)
        for fname, m in (("carry_trades.jsonl", "paper"), ("live_orders.jsonl", "live")):
            if mode != "all" and mode != m:
                continue
            paths = [os.path.join(base, "logs", fname)]
            if u == 1:
                paths.insert(0, os.path.join(base, fname))
            for p in paths:
                if not os.path.exists(p):
                    continue
                try:
                    for line in open(p, encoding="utf-8"):
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except Exception:
                            continue
                        if mode == "all" and m == "paper" and rec.get("venue") == "pm":
                            continue  # PM 历史不展示
                        rows.append((u, rec, m))
                except Exception:
                    pass
    return rows


def _rev_fin(rows, sym_whitelist):
    """财务口径: 老记录(无platform_rev)按 mult=1 回算: official=user_fee, rev=0"""
    fin = []
    for uid, rec, m in rows:
        fee = rec.get("user_fee", rec.get("fees"))
        try:
            fee = float(fee) if fee is not None else 0.0
        except (TypeError, ValueError):
            fee = 0.0
        if rec.get("platform_rev") is not None:
            off = float(rec.get("official_fee") or 0.0)
            rev = float(rec.get("platform_rev") or 0.0)
            spr = float(rec.get("spread_rev") or 0.0)
        else:
            # 老记录回算: 历史无加价 → 官方成本=用户费; 但点差收益(spread_rev)可能单独存在
            spr = float(rec.get("spread_rev") or 0.0)
            off, rev = fee, spr
        fin.append({"uid": uid, "mode": m, "rec": rec, "user_fee": fee,
                    "official_fee": off, "platform_rev": rev, "spread_rev": spr})
    return fin


@app.get("/api/admin/revenue")
def api_admin_revenue(request: Request, __=Depends(require_admin)):
    """营收汇总: ?from=ISO&to=ISO&by=symbol|user|action|day"""
    try:
        t0 = float(request.query_params.get("from") or 0)
        t1 = float(request.query_params.get("to") or (time.time() * 1000 + 3600e3))
        by = request.query_params.get("by") or "day"
        mode = request.query_params.get("mode") or "all"   # R14-M14: 默认全量(模拟+实盘)
    except Exception:
        return JSONResponse({"ok": False, "error": "参数非法"}, status_code=400)
    rows = _rev_scan(1, "all")
    fin = _rev_fin(rows, _SYM_WHITELIST)
    agg = {}
    total_rev = total_user = total_off = total_spr = 0.0
    cnt = 0
    for f in fin:
        try:
            ts = f["rec"].get("ts")
            if isinstance(ts, str):
                from datetime import datetime
                ts = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000
            ts = float(ts or 0)
        except Exception:
            ts = 0
        if ts < t0 or ts >= t1:
            continue
        if by == "day":
            key = time.strftime("%Y-%m-%d", time.gmtime(ts / 1000))
        elif by == "symbol":
            key = f["rec"].get("symbol") or "?"
        elif by == "user":
            key = str(f["uid"])
        else:
            key = f["rec"].get("action") or "?"
        a = agg.setdefault(key, {"rev": 0.0, "user": 0.0, "off": 0.0, "spr": 0.0, "n": 0})
        a["rev"] += f["platform_rev"]; a["user"] += f["user_fee"]
        a["off"] += f["official_fee"]; a["spr"] += f["spread_rev"]; a["n"] += 1
        total_rev += f["platform_rev"]; total_user += f["user_fee"]
        total_off += f["official_fee"]; total_spr += f["spread_rev"]; cnt += 1
    items = [{"key": k, **{kk: round(vv, 4) for kk, vv in v.items()}}
             for k, v in sorted(agg.items(), key=lambda kv: -kv[1]["rev"])]
    return {"ok": True, "total": {"rev": round(total_rev, 4), "user_fee": round(total_user, 4),
                                  "official_fee": round(total_off, 4), "spread_rev": round(total_spr, 4),
                                  "n": cnt}, "items": items}


@app.get("/api/admin/revenue/detail")
def api_admin_revenue_detail(request: Request, __=Depends(require_admin)):
    """营收明细: ?offset=&limit=&from=&to=&symbol=&uid="""
    try:
        offset = max(0, int(request.query_params.get("offset") or 0))
        limit = min(200, max(1, int(request.query_params.get("limit") or 50)))
        t0 = float(request.query_params.get("from") or 0)
        t1 = float(request.query_params.get("to") or (time.time() * 1000 + 3600e3))
        fsym = (request.query_params.get("symbol") or "").upper()
        fuid = request.query_params.get("uid") or ""
    except Exception:
        return JSONResponse({"ok": False, "error": "参数非法"}, status_code=400)
    rows = _rev_scan(1, "all")
    fin = _rev_fin(rows, _SYM_WHITELIST)
    fin.sort(key=lambda f: -(f["rec"].get("ts") if isinstance(f["rec"].get("ts"), (int, float)) else 0))
    out, seen = [], 0
    for f in fin:
        rec = f["rec"]
        try:
            ts = rec.get("ts")
            if isinstance(ts, str):
                from datetime import datetime
                ts = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000
            ts = float(ts or 0)
        except Exception:
            ts = 0
        if ts < t0 or ts >= t1:
            continue
        if fsym and (rec.get("symbol") or "").upper() != fsym:
            continue
        if fuid and str(f["uid"]) != fuid:
            continue
        if seen < offset:
            seen += 1
            continue
        out.append({"ts": ts, "uid": f["uid"], "mode": f["mode"],
                    "symbol": rec.get("symbol"), "action": rec.get("action"),
                    "side": rec.get("side"), "qty": rec.get("qty"), "notional": rec.get("notional"),
                    "user_fee": round(f["user_fee"], 6), "official_fee": round(f["official_fee"], 6),
                    "platform_rev": round(f["platform_rev"], 6), "spread_rev": round(f["spread_rev"], 6)})
        if len(out) >= limit:
            break
    return {"ok": True, "rows": out, "offset": offset, "limit": limit, "has_more": len(out) == limit}


@app.get("/api/admin/revenue/export")
def api_admin_revenue_export(request: Request, __=Depends(require_admin)):
    """营收明细 CSV 导出"""
    from io import StringIO
    try:
        t0 = float(request.query_params.get("from") or 0)
        t1 = float(request.query_params.get("to") or (time.time() * 1000 + 3600e3))
    except Exception:
        return JSONResponse({"ok": False, "error": "参数非法"}, status_code=400)
    fin = _rev_fin(_rev_scan(1, "all"), _SYM_WHITELIST)
    buf = StringIO()
    buf.write("时间,用户ID,模式,标的,动作,方向,数量,名义,用户实付费,官方成本,平台营收,点差收益\n")
    for f in fin:
        rec = f["rec"]
        try:
            ts = rec.get("ts")
            if isinstance(ts, str):
                from datetime import datetime
                ts = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000
            ts = float(ts or 0)
        except Exception:
            ts = 0
        if ts < t0 or ts >= t1:
            continue
        buf.write(f'{time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ts / 1000))},{f["uid"]},{f["mode"]},'
                  f'{(rec.get("symbol") or "")},{(rec.get("action") or "")},{(rec.get("side") or "")},'
                  f'{rec.get("qty") if rec.get("qty") is not None else ""},{rec.get("notional") if rec.get("notional") is not None else ""},'
                  f'{round(f["user_fee"], 6)},{round(f["official_fee"], 6)},{round(f["platform_rev"], 6)},{round(f["spread_rev"], 6)}\n')
    from fastapi.responses import Response
    return Response(content=buf.getvalue().encode("utf-8-sig"), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=revenue.csv"})
