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


@app.get("/api/system")
def api_system(__=Depends(require_session)):
    return readers.system()
