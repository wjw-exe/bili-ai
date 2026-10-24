"""B 站接口封装：wbi 密钥缓存 + 搜索 + 详情"""

import asyncio
import time
from urllib.parse import quote, urlencode

from . import sign
from .fmt import clean
from .http import WEB, Api, NeedLogin, RiskControl

API = "https://api.bilibili.com"


def _abs(u: str) -> str:
    """B 站图床返回协议相对地址（//i0.hdslb.com/...），统一补全 scheme"""
    return "https:" + u if u.startswith("//") else u


class Bili:
    def __init__(self) -> None:
        self.http = Api()
        self._mixin: str | None = None
        self._mixin_at = 0.0

    async def warmup(self) -> None:
        """主站首页拿 buvid3 / b_nut，未登录态必需"""
        await self.http.warmup()

    async def mixin_key(self) -> str:
        if self._mixin and time.time() - self._mixin_at < 3600:
            return self._mixin
        v = await self.http.get_json_any(f"{API}/x/web-interface/nav", {}, WEB)
        wbi = (v.get("data") or {}).get("wbi_img") or {}
        img, sub = wbi.get("img_url"), wbi.get("sub_url")
        if not img or not sub:
            raise RuntimeError("NAV_NO_WBI: 未取到 wbi_img，可能已被风控")
        self._mixin = sign.mixin_key(sign.extract_key(img, sub))
        self._mixin_at = time.time()
        return self._mixin

    async def search(self, keyword: str, page: int = 1, limit: int = 10) -> dict:
        """参数保持最小集（keyword/search_type/page），多带任一即易被拦"""
        base = {"search_type": "video", "keyword": keyword, "page": page}
        params = sign.sign_params(base, await self.mixin_key())
        referer = "https://search.bilibili.com/all?keyword=" + quote(keyword, safe="")
        try:
            v = await self.http.get_json(f"{API}/x/web-interface/wbi/search/type", params, referer)
        except RiskControl:
            # 换指纹 + 退避一次（实测 1.5s 内重试必失败，取 6s）
            self.http.drop_buvid()
            await asyncio.sleep(6)
            await self.warmup()
            params = sign.sign_params(base, await self.mixin_key())
            v = await self.http.get_json(f"{API}/x/web-interface/wbi/search/type", params, referer)

        data = v.get("data") or {}
        items = []
        for r in (data.get("result") or [])[:limit]:
            items.append({
                "bvid": r.get("bvid") or "",
                "title": clean(r.get("title") or ""),
                "up": clean(r.get("author") or ""),
                "mid": r.get("mid") or 0,
                "dur": r.get("duration") or "",
                "play": r.get("play") or 0,
                "pic": _abs(r.get("pic") or ""),
            })
        return {"query": keyword, "page": page,
                "total": data.get("numResults") or 0, "items": items}

    async def view(self, bvid: str) -> dict:
        referer = f"https://www.bilibili.com/video/{bvid}"
        v = await self.http.get_json(f"{API}/x/web-interface/view", {"bvid": bvid}, referer)
        d = v.get("data") or {}
        owner = d.get("owner") or {}
        stat = d.get("stat") or {}
        return {
            "bvid": d.get("bvid") or bvid,
            "avid": d.get("aid") or 0,
            "title": clean(d.get("title") or ""),
            "up": owner.get("name") or "",
            "mid": owner.get("mid") or 0,
            "dur": int(d.get("duration") or 0),
            "pubdate": d.get("pubdate") or 0,
            "view": stat.get("view") or 0,
            "danmaku": stat.get("danmaku") or 0,
            "reply": stat.get("reply") or 0,
            "fav": stat.get("favorite") or 0,
            "coin": stat.get("coin") or 0,
            "like": stat.get("like") or 0,
            "desc": " ".join((d.get("desc") or "").split())[:200],
            "pic": _abs(d.get("pic") or ""),
        }

    # ---------------- 登录态 ----------------

    @property
    def logged_in(self) -> bool:
        return self.http.logged_in

    @property
    def mid(self) -> int:
        try:
            return int(self.http.client.cookies.get("DedeUserID") or 0)
        except (TypeError, ValueError):
            return 0

    def _video_ref(self, bvid: str) -> str:
        return f"https://www.bilibili.com/video/{bvid}"

    # ---------------- 评论 ----------------

    async def comments(self, aid: int, bvid: str, page: int = 1,
                       limit: int = 20, mode: int = 3) -> dict:
        """评论列表。

        未登录时 wbi/main 直接 -403，只有 plain 接口能拿到 3 条「精彩评论」；
        登录后走 wbi 签名接口可正常分页。这里做自动降级并把实际数据源告知上层。
        """
        ps = max(1, min(20, int(limit)))
        base = {"oid": aid, "type": 1, "mode": mode, "pn": page, "ps": ps}
        referer = self._video_ref(bvid)
        src = "wbi"
        try:
            params = sign.sign_params(base, await self.mixin_key())
            v = await self.http.get_json(f"{API}/x/v2/reply/wbi/main", params, referer)
        except (ApiError, RiskControl):
            src = "plain"
            v = await self.http.get_json(f"{API}/x/v2/reply/main", base, referer)

        data = v.get("data") or {}
        cur = data.get("cursor") or {}
        rows = list(data.get("top_replies") or []) + list(data.get("replies") or [])
        items, seen = [], set()
        for r in rows:
            rpid = r.get("rpid_str") or r.get("rpid")
            if rpid in seen:
                continue
            seen.add(rpid)
            mem = r.get("member") or {}
            items.append({
                "rpid": str(rpid or ""),
                "uname": mem.get("uname") or "",
                "mid": mem.get("mid") or 0,
                "msg": " ".join(clean((r.get("content") or {}).get("message") or "").split()),
                "like": r.get("like") or 0,
                "sub": r.get("rcount") or 0,
                "ctime": r.get("ctime") or 0,
            })
        return {"aid": aid, "bvid": bvid, "page": page, "source": src,
                "total": cur.get("all_count") or 0,
                "is_end": bool(cur.get("is_end")), "items": items[:ps]}

    async def comment_add(self, aid: int, bvid: str, message: str,
                          root: int | str = 0, parent: int | str = 0) -> dict:
        """发表评论。root/parent 非 0 时是对某条评论的回复。

        写接口：需登录态 + csrf(bili_jct)，未登录 http 层直接抛 NeedLogin。
        """
        msg = (message or "").strip()
        if not msg:
            raise ValueError("评论内容不能为空")
        if len(msg) > 1000:
            raise ValueError("评论内容超过 1000 字上限")
        payload = {"oid": aid, "type": 1, "message": msg, "plat": 1}
        if root:
            payload["root"] = root
            payload["parent"] = parent or root
        # x/v2/reply/add 需 wbi 签名，缺失时服务端直接回 404/412
        params = sign.sign_params({"oid": aid, "type": 1}, await self.mixin_key())
        url = f"{API}/x/v2/reply/add?" + urlencode(params)
        v = await self.http.post_json(url, payload, self._video_ref(bvid))
        d = v.get("data") or {}
        return {"aid": aid, "bvid": bvid,
                "rpid": str(d.get("rpid_str") or d.get("rpid") or ""),
                "message": msg}

    # ---------------- 互动 ----------------

    async def like_state(self, aid: int, bvid: str) -> dict:
        v = await self.http.get_json(f"{API}/x/web-interface/archive/has/like",
                                     {"aid": aid}, self._video_ref(bvid))
        return {"liked": bool(v.get("data"))}

    async def coin_state(self, aid: int, bvid: str) -> dict:
        v = await self.http.get_json(f"{API}/x/web-interface/archive/coins",
                                     {"aid": aid}, self._video_ref(bvid))
        return {"coins": int(v.get("data") or 0)}

    async def fav_state(self, aid: int, bvid: str) -> dict:
        v = await self.http.get_json(f"{API}/x/v2/fav/video/favoured",
                                     {"aid": aid}, self._video_ref(bvid))
        d = v.get("data") or {}
        return {"favoured": bool(d.get("favoured"))}

    async def like(self, aid: int, bvid: str, on: bool = True) -> dict:
        await self.http.post_json(f"{API}/x/web-interface/archive/like",
                                  {"aid": aid, "like": 1 if on else 2},
                                  self._video_ref(bvid))
        return {"aid": aid, "liked": on}

    async def coin(self, aid: int, bvid: str, n: int = 1,
                   also_like: bool = False) -> dict:
        n = 2 if int(n) >= 2 else 1
        await self.http.post_json(f"{API}/x/web-interface/coin/add",
                                  {"aid": aid, "multiply": n,
                                   "select_like": 1 if also_like else 0},
                                  self._video_ref(bvid))
        return {"aid": aid, "coins": n, "also_like": also_like}

    async def fav_folders(self, mid: int | None = None) -> list[dict]:
        mid = mid or self.mid
        if not mid:
            raise NeedLogin("未登录：无法读取收藏夹列表")
        v = await self.http.get_json(f"{API}/x/v3/fav/folder/created/list-all",
                                     {"up_mid": mid},
                                     f"https://space.bilibili.com/{mid}/favlist")
        out = []
        for f in (v.get("data") or {}).get("list") or []:
            out.append({"id": f.get("id"), "title": clean(f.get("title") or ""),
                        "count": f.get("media_count") or 0})
        return out

    async def fav_deal(self, aid: int, bvid: str, folder_id: int | str,
                       add: bool = True) -> dict:
        payload = {"rid": aid, "type": 2}
        payload["add_media_ids" if add else "del_media_ids"] = folder_id
        payload["del_media_ids" if add else "add_media_ids"] = ""
        await self.http.post_json(f"{API}/x/v3/fav/resource/deal", payload,
                                  self._video_ref(bvid))
        return {"aid": aid, "folder": folder_id, "favoured": add}
