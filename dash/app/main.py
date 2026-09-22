import sys
import os
import json
import time
from collections import defaultdict, deque
from pathlib import Path
from fastapi import FastAPI, Request, Depends, HTTPException, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import iterate_in_threadpool
from . import auth, config, readers

sys.path.insert(0, os.path.expanduser("~/polymarket"))
import ai_client  # noqa: E402
import ai_tools  # noqa: E402

app = FastAPI(title="moneybot dash")
STATIC = Path(__file__).resolve().parent.parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


def require_session(request: Request):
    if not auth.valid_session(request.cookies.get(config.COOKIE_NAME)):
        raise HTTPException(status_code=401, detail="unauthorized")


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
        return JSONResponse({"ok": False, "err": "bad request"}, status_code=400)
    if not auth.login_allowed():
        return JSONResponse({"ok": False, "err": "尝试过多, 1分钟后再试"}, status_code=429)
    if auth.check_password(body.get("password") or ""):
        resp = JSONResponse({"ok": True})
        resp.set_cookie(config.COOKIE_NAME, auth.make_session(), httponly=True,
                        samesite="lax", max_age=86400, secure=False, path="/")
        return resp
    auth.record_fail()
    return JSONResponse({"ok": False, "err": "密码错误"}, status_code=401)


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
async def api_change_pw(request: Request, __=Depends(require_session)):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "err": "bad request"}, status_code=400)
    if not auth.login_allowed():
        return JSONResponse({"ok": False, "err": "尝试过多, 1分钟后再试"}, status_code=429)
    ok, msg = auth.change_password(body.get("old_pw", ""), body.get("new_pw", ""))
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


@app.get("/api/system")
def api_system(__=Depends(require_session)):
    return readers.system()
