from pathlib import Path
from fastapi import FastAPI, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from . import auth, config, readers

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


@app.get("/api/system")
def api_system(__=Depends(require_session)):
    return readers.system()
