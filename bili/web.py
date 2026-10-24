"""可视化后台：实时观察 + 命令下发 + 扫码登录 + 互动（点赞/投币/收藏/评论）

MCP 模式下 uvicorn 跑在独立线程，与 MCP 主 loop 共用一份 StateStore，
因此后台能实时看到 AI 的每一步，但命令只能在主 loop 执行 —— 此时接口降级为只读。
"""

import asyncio
import base64
import json
import pathlib
import time

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, StreamingResponse

from . import auth
from .core import Core, core
from .http import WEB, NeedLogin
from .store import store

STATIC = pathlib.Path(__file__).resolve().parent.parent / "static"

_BLANK_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYGD4DwABBAEAcCBlCwAAAABJRU5ErkJggg=="
)
ALLOWED = {"search", "open_video", "play", "pause", "resume", "seek", "state", "status",
           "comments", "comment", "like", "coin", "fav", "unfav", "folders", "me",
           "subtitles", "subtitle"}

# 后台专用 Core：独立 Bili 实例（自己的 httpx client，绑定 uvicorn 的事件循环），
# 与 MCP 主 loop 的 core 互不干扰；两者共用同一个 store，状态互通。
web_core = Core()

# 待扫码的二维码：qrcode_key -> 过期时间戳 / 二维码内容
PENDING: dict[str, float] = {}
_pending_url: dict[str, str] = {}

app = FastAPI(title="bili-ai", docs_url=None, redoc_url=None, openapi_url=None)


def _snap() -> dict:
    s = store.snapshot()
    s["readonly"] = core.readonly
    return s


def _set_account(info: dict) -> None:
    with store.lock:
        store.account = {"login": bool(info.get("is_login")),
                         "uname": info.get("uname") or "",
                         "mid": info.get("mid") or 0,
                         "avatar": info.get("avatar") or ""}


@app.get("/")
async def index() -> FileResponse:
    # 后台页面迭代频繁，禁缓存避免浏览器复用旧 HTML
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/api/state")
async def api_state() -> dict:
    return _snap()


@app.get("/api/events")
async def api_events() -> StreamingResponse:
    async def gen():
        last = ""
        while True:
            payload = json.dumps(_snap(), ensure_ascii=False)
            if payload != last:
                last = payload
                yield f"data: {payload}\n\n"
            await asyncio.sleep(0.35)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})


@app.post("/api/cmd")
async def api_cmd(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        return {"ok": False, "out": "BAD_BODY"}
    name = str(body.get("name", ""))
    if name not in ALLOWED:
        return {"ok": False, "out": f"BAD_CMD: {name}"}
    # 后台一律走自己的 Core：MCP 模式下主 loop 不可跨线程调用，
    # 而人点的按钮必须能立刻生效，故不套用 core.readonly。
    out = await web_core.execute(name, body.get("args") or {})
    return {"ok": not out.startswith(("ERR", "BAD", "HTTP", "NETWORK", "Runtime")), "out": out}


# ---------------- 登录（后台扫码） ----------------

@app.get("/api/auth")
async def api_auth() -> dict:
    info = await auth.nav(web_core.bili.http)
    _set_account(info)
    return info


@app.post("/api/auth/qr")
async def api_auth_qr() -> dict:
    r = await auth.qr_start(web_core.bili.http)
    if not r.get("ok"):
        return r
    PENDING[r["key"]] = time.time() + 180
    _pending_url[r["key"]] = r["url"]
    return {"ok": True, "key": r["key"], "url": r["url"],
            "png": f"/api/auth/qr.png?key={r['key']}"}


@app.get("/api/auth/qr.png")
async def api_auth_qr_png(key: str) -> Response:
    url = _pending_url.get(key)
    if not url:
        return Response("QR_GONE: 二维码已失效，请重新获取", status_code=404)
    png = auth.qr_png_bytes(url)
    if not png:
        return Response("QR_RENDER_FAIL: 未安装 segno", status_code=500)
    return Response(png, media_type="image/png",
                    headers={"Cache-Control": "no-store"})


@app.get("/api/auth/poll")
async def api_auth_poll(key: str) -> dict:
    if key not in PENDING:
        return {"ok": False, "status": "expired", "msg": "二维码已失效"}
    if time.time() > PENDING[key]:
        PENDING.pop(key, None)
        _pending_url.pop(key, None)
        return {"ok": True, "status": "expired"}
    r = await auth.qr_poll(web_core.bili.http, key)
    if r.get("status") == "ok":
        PENDING.pop(key, None)
        _pending_url.pop(key, None)
        _set_account({"is_login": True, "uname": r.get("uname"),
                      "mid": r.get("mid"), "avatar": r.get("avatar")})
        await web_core.cmd_me()                  # 同步账号快照 + 转成 store 视图
    elif r.get("status") == "expired":
        PENDING.pop(key, None)
        _pending_url.pop(key, None)
    return r


@app.post("/api/auth/logout")
async def api_auth_logout() -> dict:
    web_core.bili.http.clear_cookies()
    with store.lock:
        store.account = {"login": False, "uname": "", "mid": 0, "avatar": ""}
        store.inter = {"liked": None, "coins": None, "favoured": None}
    return {"ok": True, "msg": "已退出登录"}


@app.get("/api/folders")
async def api_folders() -> dict:
    """收藏夹列表（收藏按钮选夹用）；未登录返回 ok=False"""
    try:
        return {"ok": True, "items": await web_core.bili.fav_folders()}
    except NeedLogin as e:
        return {"ok": False, "need_login": True, "msg": e.msg}
    except Exception as e:                       # noqa: BLE001
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


@app.get("/api/thumb")
async def api_thumb(u: str) -> Response:
    """B 站图床有防盗链，必须代理并带 Referer"""
    if not any(h in u for h in ("hdslb.com", "biliimg.com")):
        return Response("FORBIDDEN: 仅允许代理 B 站图床", status_code=403)
    try:
        r = await core.bili.http.client.get(u, headers={"Referer": WEB})
        if r.status_code != 200:
            return Response(_BLANK_PNG, media_type="image/png",
                            headers={"Cache-Control": "no-store"})
        return Response(content=r.content,
                        media_type=r.headers.get("content-type", "image/jpeg"),
                        headers={"Cache-Control": "max-age=3600"})
    except Exception:                            # noqa: BLE001
        # 图链过期/超时不该把控制台刷成 502，降级为透明占位
        return Response(_BLANK_PNG, media_type="image/png",
                        headers={"Cache-Control": "no-store"})


@app.get("/favicon.ico")
async def favicon() -> Response:
    return Response(status_code=204)
