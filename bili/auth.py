"""登录：扫码登录（主）+ 手动 Cookie 导入（备）+ 登录态校验

扫码流程（B 站 passport web 接口）：
  1. generate  取二维码内容 url 与 qrcode_key
  2. 人用手机 B 站客户端扫码确认
  3. poll 轮询状态，成功时响应 Set-Cookie 里带 SESSDATA / bili_jct / DedeUserID
  4. 落盘到 .data/cookie.json，之后所有入口自动带上登录态
"""

import asyncio
import json
import os
import sys
from pathlib import Path

from .http import COOKIE_FILE, DATA_DIR, WEB, Api

PASSPORT = "https://passport.bilibili.com"
NAV = "https://api.bilibili.com/x/web-interface/nav"
QR_PNG = DATA_DIR / "login_qr.png"

# poll 状态码
ST_PENDING = 86101   # 未扫描
ST_SCANNED = 86090   # 已扫码，待手机端确认
ST_EXPIRED = 86038   # 二维码已失效
ST_OK = 0            # 登录成功


async def nav(api: Api) -> dict:
    """返回 {is_login, uname, mid, vip}；网络异常返回 error 字段"""
    try:
        v = await api.get_json_any(NAV, {}, WEB)
    except Exception as e:                       # noqa: BLE001
        return {"is_login": False, "error": f"{type(e).__name__}: {e}"}
    d = v.get("data") or {}
    return {
        "is_login": bool(d.get("isLogin")),
        "uname": d.get("uname") or "",
        "mid": d.get("mid") or 0,
        "avatar": d.get("face") or "",
        "vip": (d.get("vipStatus") or 0) == 1,
        "code": v.get("code"),
        "message": v.get("message") or "",
    }


# ---------------- 分步扫码（供后台走 HTTP 轮询，不做长阻塞） ----------------

async def qr_start(api: Api) -> dict:
    """申请二维码：返回 {ok, url, key}"""
    v = await api.get_json_any(f"{PASSPORT}/x/passport-login/web/qrcode/generate",
                               {}, WEB)
    d = v.get("data") or {}
    if not d.get("url") or not d.get("qrcode_key"):
        return {"ok": False,
                "msg": f"申请二维码失败：{v.get('code')} {v.get('message') or ''}".strip()}
    return {"ok": True, "url": d["url"], "key": d["qrcode_key"]}


def qr_png_bytes(content: str) -> bytes | None:
    """二维码内容 -> PNG 字节流（后台直接吐图片，不落盘）"""
    try:
        import io

        import segno
    except ImportError:
        return None
    try:
        buf = io.BytesIO()
        segno.make(content, error="m").save(buf, kind="png", scale=7, border=3)
        return buf.getvalue()
    except Exception:                            # noqa: BLE001
        return None


async def qr_poll(api: Api, key: str) -> dict:
    """轮询一次：status = pending / scanned / expired / ok"""
    r = await api.get_json_any(f"{PASSPORT}/x/passport-login/web/qrcode/poll",
                               {"qrcode_key": key, "source": "main-fe-header"}, WEB)
    code = (r.get("data") or {}).get("code")
    if code == ST_OK:
        path = api.save_cookies()
        info = await nav(api)
        return {"ok": True, "status": "ok", "uname": info.get("uname") or "",
                "mid": info.get("mid") or 0, "avatar": info.get("avatar") or "",
                "cookie_file": str(path)}
    if code == ST_EXPIRED:
        return {"ok": True, "status": "expired"}
    if code == ST_SCANNED:
        return {"ok": True, "status": "scanned"}
    return {"ok": True, "status": "pending"}


def _render_qr(content: str) -> Path | None:
    """把二维码内容渲染成 PNG，失败返回 None（调用方退回打印 url）"""
    try:
        import segno
    except ImportError:
        return None
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        segno.make(content, error="m").save(str(QR_PNG), scale=7, border=3)
        return QR_PNG
    except Exception:                            # noqa: BLE001
        return None


def _open_image(path: Path) -> None:
    """Windows 直接调起看图工具，方便手机扫屏幕"""
    if sys.platform != "win32":
        return
    try:
        os.startfile(str(path))                  # noqa: S606
    except OSError:
        pass


async def qr_login(api: Api, timeout: float = 180.0, open_qr: bool = True,
                   quiet: bool = False) -> dict:
    """扫码登录；返回 {ok, uname, mid, cookie_file, msg}"""
    say = (lambda *a: None) if quiet else print

    v = await api.get_json_any(f"{PASSPORT}/x/passport-login/web/qrcode/generate",
                               {}, WEB)
    data = v.get("data") or {}
    url, key = data.get("url"), data.get("qrcode_key")
    if not url or not key:
        return {"ok": False, "msg": f"申请二维码失败：{v.get('code')} {v.get('message')}"}

    png = _render_qr(url)
    if png:
        say(f"二维码图片：{png}")
        say("用手机 B 站客户端「扫一扫」扫码并在手机上确认登录\n")
        if open_qr:
            _open_image(png)
    else:
        say(f"二维码内容（自行生成二维码扫）：{url}\n")

    say("等待扫码…")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    last = None
    while loop.time() < deadline:
        await asyncio.sleep(1.5)
        try:
            r = await api.get_json_any(f"{PASSPORT}/x/passport-login/web/qrcode/poll",
                                       {"qrcode_key": key, "source": "main-fe-header"},
                                       WEB)
        except Exception as e:                   # noqa: BLE001
            last = f"{type(e).__name__}: {e}"
            continue

        code = (r.get("data") or {}).get("code")
        if code == ST_OK:
            path = api.save_cookies()
            info = await nav(api)
            say(f"\n登录成功：{info.get('uname') or '(未知用户)'}  mid={info.get('mid')}")
            say(f"Cookie 已保存：{path}")
            return {"ok": True, "uname": info.get("uname"), "mid": info.get("mid"),
                    "cookie_file": str(path), "msg": "ok"}
        if code == ST_EXPIRED:
            return {"ok": False, "msg": "二维码已失效，请重新执行登录"}
        if code == ST_SCANNED and last != ST_SCANNED:
            say("已扫码，请在手机上点「确认登录」")
            last = ST_SCANNED
        elif code == ST_PENDING and last != ST_PENDING:
            say("尚未扫描…")
            last = ST_PENDING
        elif code != ST_PENDING and code != ST_SCANNED:
            return {"ok": False, "msg": f"轮询异常：code={code}"}

    return {"ok": False, "msg": f"超时 {timeout:.0f}s 未完成登录"}


def import_cookie(text: str) -> dict:
    """手动导入 Cookie：支持 JSON 或浏览器复制的 'a=1; b=2' 串"""
    text = (text or "").strip()
    if not text:
        return {"ok": False, "msg": "Cookie 为空"}
    jar: dict[str, str] = {}
    if text.startswith("{"):
        try:
            raw = json.loads(text)
            jar = {k: v for k, v in raw.items() if isinstance(v, str) and v}
        except ValueError as e:
            return {"ok": False, "msg": f"JSON 解析失败：{e}"}
    else:
        for seg in text.replace("\n", ";").split(";"):
            if "=" in seg:
                k, _, v = seg.partition("=")
                if k.strip() and v.strip():
                    jar[k.strip()] = v.strip()

    if not jar.get("SESSDATA"):
        return {"ok": False, "msg": "缺少 SESSDATA，无法构成登录态"}

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    old = {}
    if COOKIE_FILE.exists():
        try:
            old = json.loads(COOKIE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            old = {}
    old.update(jar)
    COOKIE_FILE.write_text(json.dumps(old, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        COOKIE_FILE.chmod(0o600)
    except OSError:
        pass
    return {"ok": True, "cookie_file": str(COOKIE_FILE), "keys": sorted(jar)}
