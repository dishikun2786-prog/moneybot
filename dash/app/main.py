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


@app.get("/analysis")
def an_page():
    return FileResponse(STATIC / "analysis.html")


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


@app.get("/api/analysis")
def api_analysis(__=Depends(require_session)):
    return readers.cached("analysis", 60, readers.analysis)


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
               limit: int = 300, __=Depends(require_session)):
    return readers.klines(symbol, interval, min(limit, 1000))


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


@app.get("/share/{token}")
def share_page(token: str):
    return FileResponse(STATIC / "share.html")


@app.get("/api/pnl")
def api_pnl(su=Depends(require_session_user)):
    with tenants.tenant(su["u"]):
        return readers.pnl_overview()


@app.get("/api/share/{token}")
def api_share(token: str):
    if not readers.valid_share(token):
        raise HTTPException(status_code=404, detail="分享链接无效或已撤销")
    with tenants.tenant(1):  # 分享页 = 平台主账户(admin)展示盘
        return readers.share_view()


@app.post("/api/share/generate")
def api_gen(__=Depends(require_admin)):
    return {"token": readers.generate_share()}


@app.post("/api/share/revoke")
async def api_revoke(request: Request, __=Depends(require_admin)):
    try:
        body = await request.json()
    except Exception:
        return {"ok": False}
    return {"ok": readers.revoke_share(body.get("token", ""))}


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
    return paper_ops.open_spot(sym, side, body.get("notional"))


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
    return paper_ops.close_spot(sym)


@app.get("/api/spot/positions")
def spot_positions(su=Depends(require_session_user)):
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
                      "open_pm", "open_naked"):
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
                        yield f"data: {json.dumps({'ts': snap_ts, 'prices': prices, 'full': True}, ensure_ascii=False)}\n\n"
                        last_send = time.time()
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
    """Bybit 全标的目录 (P1): {linear:[{symbol,name,turnover24h,tickSize,qtyStep,...}], spot:[...]}"""
    try:
        with open(os.path.expanduser("~/polymarket/logs/bybit_instruments.json"),
                  encoding="utf-8") as f:
            d = json.load(f)
        return {"ok": True, "linear": d.get("linear", []), "spot": d.get("spot", []),
                "ts": d.get("ts")}
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
        reqs[sym] = iv
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
    return funds.review_withdraw(su["u"], body.get("id"), bool(body.get("approve")),
                                 body.get("note", ""))


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


@app.get("/api/pm/tokens")
def api_pm_tokens(__=Depends(require_session)):
    """P4 token→市场 映射: [{token, key(event|market), title, question, outcome}]"""
    try:
        with open(os.path.expanduser("~/polymarket/logs/pm_tokens.json"),
                  encoding="utf-8") as f:
            return {"ok": True, "tokens": json.load(f)}
    except Exception:
        return {"ok": True, "tokens": []}


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
    return admin.stats_overview()


@app.get("/api/admin/users")
def api_admin_users(__=Depends(require_admin)):
    return {"rows": admin.list_users_with_stats()}


@app.post("/api/admin/user/{uid}/status")
async def api_admin_status(uid: int, request: Request, su=Depends(require_admin)):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad request"}, status_code=400)
    ok, msg = users.set_status(uid, body.get("status", ""))
    return {"ok": ok, "msg": msg}


@app.post("/api/admin/user/{uid}/plan")
async def api_admin_plan(uid: int, request: Request, su=Depends(require_admin)):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad request"}, status_code=400)
    ok, msg = admin.set_plan(uid, body.get("plan", ""), su["u"])
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
    return {"total": total, "rows": rows}


@app.get("/api/admin/announcements")
def api_admin_ann_list(__=Depends(require_admin)):
    return {"rows": admin.announce_list(all_=True)}


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
