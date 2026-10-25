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
           "danmaku", "danmaku_list",
           "subtitles", "subtitle", "inbox", "reply",
           "roam", "roam_list", "roam_check", "roam_next", "roam_comment", "roam_skip",
           "roam_draft", "roam_draft_set", "roam_send"}

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
    s["llm"] = _llm_view()
    return s


_LLM_CACHE: dict = {"t": 0.0, "v": {}}


def _web_ar():
    """后台专用自动回复器：与 web_core 绑定，Key 与用量落 .data/llm.json"""
    from .autoreply import get_autoreply

    return get_autoreply(web_core)


def _llm_public(llm: dict) -> dict:
    """统一给前端的形状；stats 里补上「今日额度还剩几条」"""
    llm = dict(llm or {})
    st = dict(llm.get("stats") or {})
    cap = int(llm.get("max_per_day") or 0)
    st["quota_left"] = max(0, cap - int(st.get("sent_today") or 0)) if cap else -1
    llm["stats"] = st
    return llm


def _llm_view() -> dict:
    """1 秒缓存，避免 SSE 每 0.35s 都去读盘"""
    now = time.time()
    if now - float(_LLM_CACHE.get("t") or 0) > 1.0:
        try:
            _LLM_CACHE["v"] = _llm_public(_web_ar().status())
        except Exception:                        # noqa: BLE001
            _LLM_CACHE["v"] = {}
        _LLM_CACHE["t"] = now
    return dict(_LLM_CACHE.get("v") or {})


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
    """起后台巡览 + 自动回复轮询（幂等：重复 startup 不会起第二个 loop）"""
    from .roam import start_loop

    try:
        start_loop(_web_roam())
        _web_roam()._sync()
    except Exception:                            # noqa: BLE001  轮询失败不影响后台可用
        pass
    try:
        from .autoreply import start_loop as start_ar

        start_ar(_web_ar())
    except Exception:                            # noqa: BLE001
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


# ---------------- 自动回复（外部大模型 API Key） ----------------

@app.get("/api/llm")
async def api_llm() -> dict:
    """读配置（Key 只回掩码）+ 今日用量"""
    return {"ok": True, "llm": _llm_view()}


@app.post("/api/llm")
async def api_llm_set(request: Request) -> dict:
    """存配置。api_key 留空 = 不改动；clear_key=true = 清空 Key"""
    b = await _body(request)
    llm = _web_ar().llm
    kw = {k: b.get(k) for k in ("base_url", "model", "prompt", "roam_prompt",
                                "auto_draft", "auto_publish", "interval",
                                "max_per_day", "temperature") if k in b}
    key = str(b.get("api_key") or "").strip()
    if key:
        kw["api_key"] = key
        # 「填 Key 即自动回复」：调用方没显式指定开关时，直接开启拟稿+自动发送
        if not any(k in b for k in ("auto_draft", "auto_publish")):
            kw["auto_draft"] = True
            kw["auto_publish"] = True
    if b.get("clear_key"):
        kw["clear_key"] = True
    out = llm.configure(**kw)
    _LLM_CACHE["t"] = 0.0
    # 首次填 Key 时顺便开启巡览拟稿（只拟稿，是否自动发仍由巡览面板 llm_publish 决定）
    if key and "llm_draft" not in b and "llm_publish" not in b:
        try:
            _web_roam().configure(llm_draft=True,
                                  llm_publish=None if not b.get("clear_key") else False)
        except Exception:                        # noqa: BLE001
            pass
    return {"ok": not out.startswith("ERR"), "msg": out, "llm": _llm_view()}


@app.post("/api/llm/test")
async def api_llm_test() -> dict:
    """真打一次模型接口，验证地址 / Key / 模型名"""
    r = await asyncio.to_thread(_web_ar().llm.test)
    _LLM_CACHE["t"] = 0.0
    return {**r, "llm": _llm_view()}


@app.post("/api/llm/draft")
async def api_llm_draft(request: Request) -> dict:
    """给一条消息拟草稿并存下来（不发送）"""
    b = await _body(request)
    msg_id = str(b.get("msg_id") or "").strip()
    if not msg_id:
        return {"ok": False, "msg": "缺少 msg_id"}
    r = await _web_ar().draft_one(msg_id)
    return {**r, **_inbox_snap()}


@app.post("/api/llm/run")
async def api_llm_run(request: Request) -> dict:
    """手动跑一轮：批量拟稿（auto_publish 打开时才会真发）"""
    b = await _body(request)
    try:
        limit = int(b.get("limit") or 5)
    except (TypeError, ValueError):
        limit = 5
    r = await _web_ar().run_once(limit)
    _LLM_CACHE["t"] = 0.0
    return {**r, "llm": _llm_view(), **_inbox_snap()}


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
                      keyword=str(body.get("keyword") or ""),
                      llm_draft=body.get("llm_draft"),
                      llm_publish=body.get("llm_publish"))
    r._sync()                                    # 让 store 里的开关状态立刻反映到前端
    return {"ok": not out.startswith("ERR"), "out": out,
            "roam": store.snapshot().get("roam")}


@app.post("/api/roam/draft")
async def api_roam_draft(request: Request) -> dict:
    """让内置大模型给巡览任务拟稿；publish=true 时拟完直接发"""
    try:
        body = await request.json()
    except Exception:                            # noqa: BLE001
        body = {}
    pub = body.get("publish")
    force = bool(body.get("force"))
    res = await _web_roam().draft_one(str(body.get("task_id") or ""),
                                      publish=None if pub is None else bool(pub),
                                      force=force)
    return {"ok": bool(res.get("drafted")) or bool(res.get("published")),
            "skipped": res.get("skipped", ""), "msgs": res.get("msgs") or [],
            "drafted": res.get("drafted", 0), "published": res.get("published", 0),
            "failed": res.get("failed", 0),
            "roam": store.snapshot().get("roam")}


@app.post("/api/roam/draft_set")
async def api_roam_draft_set(request: Request) -> dict:
    """把前端编辑框里的文案存成草稿（只存不发），用于没配 Key 时的人工/助手拟稿"""
    try:
        body = await request.json()
    except Exception:                            # noqa: BLE001
        return {"ok": False, "msg": "BAD_BODY"}
    res = _web_roam().set_draft(str(body.get("task_id") or ""),
                                str(body.get("text") or ""),
                                by=str(body.get("by") or "human"))
    return {"ok": bool(res.get("ok")), "msg": res.get("msg"),
            "roam": store.snapshot().get("roam")}


@app.post("/api/roam/send")
async def api_roam_send(request: Request) -> dict:
    """把已有 AI 草稿原样发出（人工确认后的最后一跳）"""
    try:
        body = await request.json()
    except Exception:                            # noqa: BLE001
        body = {}
    res = await _web_roam().send_draft(str(body.get("task_id") or ""))
    return {"ok": bool(res.get("ok")), "msg": res.get("msg"),
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
                          str(body.get("message") or ""), by="human")
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
