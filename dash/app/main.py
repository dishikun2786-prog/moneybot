import sys
import os
import json
import time
import base64
from collections import defaultdict, deque
from pathlib import Path
from fastapi import FastAPI, Request, Depends, HTTPException, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import iterate_in_threadpool
from . import auth, captcha, config, readers, users

sys.path.insert(0, os.path.expanduser("~/polymarket"))
import ai_client  # noqa: E402
import ai_tools  # noqa: E402
import paper_ops  # noqa: E402
import engine_mode  # noqa: E402

app = FastAPI(title="moneybot dash")


@app.on_event("startup")
def _startup():
    """启动即建用户表; 首次启动执行单用户→admin 迁移"""
    users.init_db()


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
def api_paper(__=Depends(require_session)):
    return readers.paper()


@app.get("/carry")
def carry_page():
    return FileResponse(STATIC / "carry.html")


@app.get("/api/carry")
def api_carry(__=Depends(require_session)):
    return readers.cached("carry", 15, readers.carry)


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
def api_tape(limit: int = 100, __=Depends(require_session)):
    return readers.tape(min(limit, 200))


@app.get("/api/strategy")
def api_strategy(__=Depends(require_session)):
    return readers.strategy()


@app.get("/trade-proto")
def trade_proto_page():
    return FileResponse(STATIC / "trade_proto.html")


@app.get("/share/{token}")
def share_page(token: str):
    return FileResponse(STATIC / "share.html")


@app.get("/api/pnl")
def api_pnl(__=Depends(require_session)):
    return readers.pnl_overview()


@app.get("/api/share/{token}")
def api_share(token: str):
    if not readers.valid_share(token):
        raise HTTPException(status_code=404, detail="分享链接无效或已撤销")
    return readers.share_view()


@app.post("/api/share/generate")
def api_gen(__=Depends(require_session)):
    return {"token": readers.generate_share()}


@app.post("/api/share/revoke")
async def api_revoke(request: Request, __=Depends(require_session)):
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
async def ai_chat(request: Request, __=Depends(require_session)):
    ip = request.client.host if request.client else "?"
    if not ai_allowed(ip):
        return JSONResponse({"error": "频率过高, 请稍后再试"}, status_code=429)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    messages = (body.get("messages") or [])[-20:]

    async def gen():
        try:
            agen = iterate_in_threadpool(
                ai_client.run_agent(messages, ai_tools.TOOLS, ai_tools.execute_tool))
            async for ev in agen:
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'text': str(e)[:200]}, ensure_ascii=False)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/ai/approve")
async def ai_approve(request: Request, __=Depends(require_session)):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad request"}, status_code=400)
    return ai_tools.apply_pending(str(body.get("action_id", "")), bool(body.get("approve", False)))


@app.post("/api/manual/trade")
async def manual_trade(request: Request, __=Depends(require_session)):
    """手动纸面交易: open_hedge/close_perp_leg/close_both/close_spot_to_naked/close_naked/edit_naked_tpsl/close_pm"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad request"}, status_code=400)
    action = str(body.get("action", ""))
    if action not in ("open_hedge", "close_perp_leg", "close_orphan", "close_both",
                      "close_spot_to_naked", "close_naked", "edit_naked_tpsl", "close_pm",
                      "open_pm", "open_naked"):
        return JSONResponse({"ok": False, "error": f"未知动作: {action}"}, status_code=400)
    return paper_ops.execute(action, body)


@app.post("/api/params")
async def api_params_save(request: Request, __=Depends(require_session)):
    """手动保存策略参数 (白名单+范围校验, 原子写, git留痕, 引擎热加载生效)"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad request"}, status_code=400)
    changes = body.get("changes")
    if not isinstance(changes, dict) or not changes:
        return JSONResponse({"ok": False, "error": "未提供修改内容"}, status_code=400)
    return ai_tools.apply_params_direct(changes, "manual")


@app.get("/api/mode")
def api_mode(__=Depends(require_session)):
    return engine_mode.load()


@app.post("/api/mode")
async def api_mode_set(request: Request, __=Depends(require_session)):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad request"}, status_code=400)
    return engine_mode.set_mode(str(body.get("strategy", "")), str(body.get("mode", "")))


@app.get("/api/stream/prices")
async def stream_prices(request: Request, __=Depends(require_session)):
    """SSE: Bybit 实时价格推送 (数据源 = bybit_ws_bridge 原子快照, 300ms 轮读)"""
    import asyncio
    SNAP = os.path.expanduser("~/polymarket/logs/bybit_prices.json")

    async def gen():
        last_ts = None
        last_send = time.time()
        while True:
            if await request.is_disconnected():
                break
            try:
                with open(SNAP, encoding="utf-8") as f:
                    data = json.loads(f.read())
                if data.get("ts") != last_ts:
                    last_ts = data["ts"]
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
def api_cycle(__=Depends(require_session)):
    return readers.cycle()


@app.get("/api/micro")
def api_micro(__=Depends(require_session)):
    return readers.micro()


@app.get("/api/system")
def api_system(__=Depends(require_session)):
    return readers.system()
