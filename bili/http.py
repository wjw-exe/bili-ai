"""HTTP 层：连接复用、cookie 自动维护（含持久化登录态）、风控识别、有限退避重试"""

import asyncio
import json
from pathlib import Path

import httpx

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
WEB = "https://www.bilibili.com/"
ACCEPT = "application/json, text/plain, */*"

DATA_DIR = Path(__file__).resolve().parent.parent / ".data"
COOKIE_FILE = DATA_DIR / "cookie.json"
# 登录态必需项：SESSDATA 身份、bili_jct 写操作 csrf、DedeUserID 账号 ID
AUTH_KEYS = ("SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5")


class RiskControl(Exception):
    """命中平台风控（v_voucher / -412 / -352）"""


class ApiError(Exception):
    def __init__(self, code: int, msg: str):
        super().__init__(f"code={code} msg={msg}")
        self.code = code
        self.msg = msg


class NeedLogin(ApiError):
    """需要登录态才能调用的接口（-101 / -403 等）"""

    def __init__(self, msg: str = "账号未登录"):
        super().__init__(-101, msg)


class Api:
    def __init__(self) -> None:
        self.client = httpx.AsyncClient(
            headers={
                "User-Agent": UA,
                "Accept": ACCEPT,
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Referer": WEB,
            },
            timeout=httpx.Timeout(10.0, connect=5.0),
            limits=httpx.Limits(max_connections=16, max_keepalive_connections=8,
                                keepalive_expiry=90.0),
            follow_redirects=True,
        )
        self.warmed = False
        self._cookie_mtime = 0.0
        self.load_cookies()

    # ---------- 登录态持久化 ----------

    def load_cookies(self) -> int:
        """启动时装载 .data/cookie.json，返回装载条数"""
        if not COOKIE_FILE.exists():
            return 0
        try:
            data = json.loads(COOKIE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return 0
        if not isinstance(data, dict):
            return 0
        for k, v in data.items():
            if isinstance(v, str) and v:
                self.client.cookies.set(k, v, domain=".bilibili.com")
        self._cookie_mtime = COOKIE_FILE.stat().st_mtime
        return len(data)

    def sync_from_disk(self) -> bool:
        """cookie.json 被别处改写（如后台扫码登录）时热重载，返回是否发生重载。
        只应在持有本 client 的事件循环里调用，避免跨 loop 改状态。"""
        if not COOKIE_FILE.exists():
            if self._cookie_mtime:               # 被别处登出删除
                self.client.cookies.clear()
                self._cookie_mtime = 0.0
                return True
            return False
        try:
            mtime = COOKIE_FILE.stat().st_mtime
        except OSError:
            return False
        if mtime <= self._cookie_mtime:
            return False
        self.load_cookies()
        self.warmed = False
        return True

    def save_cookies(self) -> Path:
        """导出当前 cookie 罐到 .data/cookie.json（仅 bilibili 域）"""
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        jar = {}
        for c in self.client.cookies.jar:
            if "bilibili" in (c.domain or ""):
                jar[c.name] = c.value
        COOKIE_FILE.write_text(json.dumps(jar, ensure_ascii=False, indent=2),
                               encoding="utf-8")
        try:                       # 同机其它用户不可读
            COOKIE_FILE.chmod(0o600)
        except OSError:
            pass
        return COOKIE_FILE

    def clear_cookies(self) -> None:
        self.client.cookies.clear()
        COOKIE_FILE.unlink(missing_ok=True)

    @property
    def logged_in(self) -> bool:
        return bool(self.client.cookies.get("SESSDATA"))

    def auth_snapshot(self) -> dict:
        """当前 cookie 里登录相关字段的脱敏视图"""
        out = {}
        for k in AUTH_KEYS:
            v = self.client.cookies.get(k)
            if v:
                out[k] = v[:4] + "***" + v[-4:] if len(v) > 10 else "***"
        return out

    async def warmup(self) -> None:
        """访问主站拿 buvid3 / b_nut 等风控 cookie，未登录态必需"""
        if self.warmed:
            return
        try:
            await self.client.get(WEB, headers={"Referer": WEB})
        except httpx.HTTPError:
            pass
        self.warmed = True

    async def aclose(self) -> None:
        await self.client.aclose()

    def drop_buvid(self) -> None:
        """风控后丢弃设备指纹，下次 warmup 重新获取"""
        for k in ("buvid3", "buvid4", "b_nut"):
            try:
                self.client.cookies.delete(k)
            except Exception:
                pass
        self.warmed = False

    async def get_json(self, url: str, params: dict, referer: str) -> dict:
        self.sync_from_disk()
        last: Exception | None = None
        for attempt in range(2):
            try:
                r = await self.client.get(url, params=params, headers={"Referer": referer})
            except httpx.HTTPError as e:
                last = RuntimeError(f"NETWORK_ERROR: {e!r}")
                await asyncio.sleep(0.6 * (attempt + 1))
                continue

            if r.status_code != 200:
                last = RuntimeError(f"HTTP_{r.status_code}: {r.text[:120]}")
                await asyncio.sleep(0.8 * (attempt + 1))
                continue

            try:
                v = r.json()
            except ValueError:
                raise RuntimeError(f"PARSE_ERROR: body={r.text[:120]}")

            code = v.get("code", -1)
            if code == 0:
                data = v.get("data")
                if isinstance(data, dict) and "v_voucher" in data:
                    raise RiskControl("v_voucher：未登录频率风控")
                return v

            if code in (-412, -352, -799):
                last = RiskControl(f"code={code}")
                await asyncio.sleep(1.5 * (attempt + 1))
                continue

            if code in (-101, -403):
                raise NeedLogin(v.get("message") or v.get("msg") or "账号未登录")

            raise ApiError(code, v.get("message") or v.get("msg") or "")

        raise last or RuntimeError("REQUEST_FAILED")

    async def get_json_any(self, url: str, params: dict, referer: str) -> dict:
        """不看业务 code，直接返回 JSON（nav 这类未登录仍返回数据的接口）"""
        self.sync_from_disk()
        r = await self.client.get(url, params=params, headers={"Referer": referer})
        r.raise_for_status()
        return r.json()

    async def get_raw_json(self, url: str, referer: str) -> dict:
        """抓「裸 JSON」资源：既没有 code 字段，也绝不能带 params。

        subtitle_url 形如 //aisubtitle.hdslb.com/bfs/ai_subtitle/prod/xxx?auth_key=...
        auth_key 与完整 query 串绑定；httpx 只要收到 params 参数就会重建 query，
        实测立刻 403（2026-10 实测：带 params={} 403，不带 200），故此处不传 params。
        """
        self.sync_from_disk()
        r = await self.client.get(url, headers={"Referer": referer})
        r.raise_for_status()
        try:
            return r.json()
        except ValueError:
            raise RuntimeError(f"PARSE_ERROR: body={r.text[:120]}")

    async def post_json(self, url: str, data: dict, referer: str,
                        csrf: bool = True) -> dict:
        """表单 POST，自动附带 csrf=bili_jct；写操作接口都必须带"""
        self.sync_from_disk()
        # 空串要保留：收藏接口用 add_media_ids="" / del_media_ids="" 表达"这一侧不动作"
        payload = {k: v for k, v in data.items() if v is not None}
        if csrf:
            token = self.client.cookies.get("bili_jct")
            if not token:
                raise NeedLogin("未登录：缺少 bili_jct，无法执行写操作")
            payload["csrf"] = token
        try:
            r = await self.client.post(url, data=payload,
                                       headers={"Referer": referer,
                                                "Origin": "https://www.bilibili.com",
                                                "Content-Type": "application/x-www-form-urlencoded"})
        except httpx.HTTPError as e:
            raise RuntimeError(f"NETWORK_ERROR: {e!r}") from e
        if r.status_code != 200:
            raise RuntimeError(f"HTTP_{r.status_code}: {r.text[:120]}")
        try:
            v = r.json()
        except ValueError:
            raise RuntimeError(f"PARSE_ERROR: body={r.text[:120]}")

        code = v.get("code", -1)
        if code == 0:
            return v
        if code in (-101, -403):
            raise NeedLogin(v.get("message") or "账号未登录")
        raise ApiError(code, v.get("message") or v.get("msg") or "")
