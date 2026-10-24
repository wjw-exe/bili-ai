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
           "subtitles", "subtitle", "inbox", "reply",
           "roam", "roam_list", "roam_check", "roam_next", "roam_comment", "roam_skip"}

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


def _web_roam():
    """后台专用巡览器：与 web_core 绑定，状态与 CLI / MCP 共用 .data/roam.json"""
    from .roam import get_roam

    return get_roam(web_core)


@app.on_event("startup")
async def _startup() -> None:
    """起后台巡览轮询（幂等：重复 startup 不会起第二个 loop）"""
    from .roam import start_loop

    try:
        start_loop(_web_roam())
        _web_roam()._sync()
    except Exception:                            # noqa: BLE001  轮询失败不影响后台可用
        pass


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


# ---------------- 消息收件箱（回复我的 / @我的） ----------------

async def _body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:                            # noqa: BLE001
        return {}
    return body if isinstance(body, dict) else {}


def _inbox_snap() -> dict:
    with store.lock:
        snap = store._inbox_snapshot()
    snap["login"] = bool(web_core.bili.logged_in)
    snap["mid"] = int(web_core.bili.mid or 0)
    return snap


@app.get("/api/inbox")
async def api_inbox(kind: str = "all", only_unreplied: bool = False,
                    limit: int = 50) -> dict:
    """拉取别人回复我 / @我的消息；只做检测，不发送任何东西"""
    try:
        items = await web_core.inbox.list(kind, only_unreplied, limit)
    except NeedLogin as e:
        return {"ok": False, "need_login": True, "msg": e.msg, **_inbox_snap()}
    except Exception as e:                       # noqa: BLE001
        return {"ok": False, "msg": f"{type(e).__name__}: {e}", **_inbox_snap()}
    return {"ok": True, **_inbox_snap(), "kind": kind,
            "only_unreplied": bool(only_unreplied), "items": items}


@app.post("/api/reply")
async def api_reply(request: Request) -> dict:
    """后台手动补发：把输入框里的文案作为评论回复发出"""
    b = await _body(request)
    msg_id = str(b.get("msg_id") or "").strip()
    text = str(b.get("message") or "").strip()
    if not msg_id:
        return {"ok": False, "msg": "BAD_ARGS: 缺少 msg_id"}
    if not text:
        return {"ok": False, "msg": "ERR_NEED_TEXT: 回复内容不能为空"}
    out = await web_core.execute("reply", {"msg_id": msg_id, "message": text})
    ok = out.startswith("ok")
    return {"ok": ok, "out": out, "msg": "" if ok else out, **_inbox_snap()}


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


# ---------------- AI 巡览 ----------------

@app.get("/api/roam")
async def api_roam() -> dict:
    r = _web_roam()
    r._sync()
    with store.lock:
        roam = dict(store.roam)
    return {"ok": True, "roam": roam, "text": r.status_text()}


@app.post("/api/roam/set")
async def api_roam_set(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:                            # noqa: BLE001
        return {"ok": False, "msg": "BAD_BODY"}
    r = _web_roam()
    enabled = body.get("enabled")
    interval = body.get("interval", 0)
    if not interval and body.get("interval_min"):
        try:
            interval = int(float(body["interval_min"]) * 60)
        except (TypeError, ValueError):
            interval = 0
    try:
        max_per_day = int(body.get("max_per_day", -1))
    except (TypeError, ValueError):
        max_per_day = -1
    out = r.configure(on=enabled, interval=interval, max_per_day=max_per_day,
                      source=str(body.get("source") or ""),
                      keyword=str(body.get("keyword") or ""))
    return {"ok": not out.startswith("ERR"), "out": out,
            "roam": store.snapshot().get("roam")}


@app.post("/api/roam/check")
async def api_roam_check() -> dict:
    r = _web_roam()
    s = await r.check(1, force=True)
    return {"ok": not s["err"], "result": s,
            "roam": store.snapshot().get("roam")}


@app.post("/api/roam/next")
async def api_roam_next(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:                            # noqa: BLE001
        body = {}
    t = _web_roam().take_next(str(body.get("task_id") or ""))
    if not t:
        return {"ok": False, "msg": "没有 waiting 任务"}
    return {"ok": True, "task": t}


@app.post("/api/roam/comment")
async def api_roam_comment(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:                            # noqa: BLE001
        return {"ok": False, "msg": "BAD_BODY"}
    r = _web_roam()
    res = await r.publish(str(body.get("task_id") or ""),
                          str(body.get("message") or ""))
    return {"ok": bool(res.get("ok")), "msg": res.get("msg"),
            "roam": store.snapshot().get("roam")}


@app.post("/api/roam/skip")
async def api_roam_skip(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:                            # noqa: BLE001
        return {"ok": False, "msg": "BAD_BODY"}
    out = _web_roam().skip(str(body.get("task_id") or ""))
    return {"ok": not out.startswith("ERR"), "msg": out,
            "roam": store.snapshot().get("roam")}


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
