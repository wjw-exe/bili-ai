"""B 站接口封装：wbi 密钥缓存 + 搜索 + 详情"""

import asyncio
import json
import re
import time
from urllib.parse import quote, urlencode

from . import sign
from .fmt import clean
from .http import WEB, Api, ApiError, NeedLogin, RiskControl

API = "https://api.bilibili.com"
MSG = f"{API}/x/msgfeed"
MSGREF = "https://message.bilibili.com/"

# business_id -> 评论对象 type（1 视频 / 12 文章 / 17 动态）
# 其它值（直播、课程等）oid 语义不同，发评论接口对不上，直接跳过
BIZ2TYPE = {1: 1, 12: 12, 17: 17}

# 「回复 @某某 :」前缀，归一化时剥掉，只留对方原话
_AT_PREFIX = re.compile(r"^\s*回复\s*@[^\s:：]+\s*[:：]\s*")


def _abs(u: str) -> str:
    """B 站图床返回协议相对地址（//i0.hdslb.com/...），统一补全 scheme"""
    return "https:" + u if u.startswith("//") else u


def _strip_at(s: str) -> str:
    """去掉「回复 @nickname :」前缀并压缩空白"""
    t = " ".join(str(s or "").split())
    m = _AT_PREFIX.match(t)
    return t[m.end():].strip() if m else t


class Bili:
    def __init__(self) -> None:
        self.http = Api()
        self._mixin: str | None = None
        self._mixin_at = 0.0
        self._subs: dict[tuple, dict] = {}

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

    async def hot_videos(self, limit: int = 20) -> list[dict]:
        """综合热门列表（巡览取片用）。GET /x/web-interface/popular，pn=1&ps=limit。

        该接口不区分 BV/AV，返回的 duration 为秒数；desc 只留前 120 字，
        避免把整段简介灌给调用方。
        """
        n = max(1, min(50, int(limit or 20)))
        params = {"pn": 1, "ps": n}
        try:
            v = await self.http.get_json(f"{API}/x/web-interface/popular", params, WEB)
        except RiskControl:
            self.http.drop_buvid()
            await asyncio.sleep(6)
            await self.warmup()
            v = await self.http.get_json(f"{API}/x/web-interface/popular", params, WEB)

        data = v.get("data") or {}
        out = []
        for r in (data.get("list") or [])[:n]:
            owner = r.get("owner") or {}
            stat = r.get("stat") or {}
            out.append({
                "bvid": r.get("bvid") or "",
                "title": clean(r.get("title") or ""),
                "up": owner.get("name") or "",
                "mid": owner.get("mid") or 0,
                "dur": int(r.get("duration") or 0),
                "play": stat.get("view") or 0,
                "desc": " ".join((r.get("desc") or "").split())[:120],
                "pic": _abs(r.get("pic") or ""),
            })
        return out

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

    # ---------------- 字幕 ----------------

    async def pages(self, bvid: str) -> list[dict]:
        """分 P 列表。字幕按 cid 走，多 P 视频必须先定位到具体一 P"""
        ref = self._video_ref(bvid)
        v = await self.http.get_json(f"{API}/x/player/pagelist", {"bvid": bvid}, ref)
        out = []
        for p in v.get("data") or []:
            out.append({"page": p.get("page") or 0, "cid": p.get("cid") or 0,
                        "part": clean(p.get("part") or ""),
                        "dur": int(p.get("duration") or 0)})
        return out

    async def subtitle_tracks(self, bvid: str, page: int = 1) -> dict:
        """列出平台字幕轨（CC 字幕 / AI 字幕）。

        只覆盖 B 站后台的字幕文件；烧进画面的硬字幕没有数据源，无法获取。
        """
        ref = self._video_ref(bvid)
        ps = await self.pages(bvid)
        if not ps:
            raise ApiError(-404, "视频不存在或已下架")
        idx = min(max(1, int(page)), len(ps)) - 1
        p = ps[idx]
        params = sign.sign_params({"bvid": bvid, "cid": p["cid"]}, await self.mixin_key())
        v = await self.http.get_json(f"{API}/x/player/wbi/v2", params, ref)
        st = (v.get("data") or {}).get("subtitle") or {}
        tracks = []
        for s in st.get("subtitles") or []:
            lan = s.get("lan") or ""
            tracks.append({
                "lan": lan,
                "lan_doc": s.get("lan_doc") or "",
                "url": _abs(s.get("subtitle_url") or ""),
                "ai": bool((s.get("ai_type") or 0) > 0 or lan.startswith("ai-")),
                "lock": bool(s.get("is_lock")),
            })
        return {"bvid": bvid, "cid": p["cid"], "page": p["page"], "parts": len(ps),
                "allow_submit": bool(st.get("allow_submit")), "tracks": tracks}

    def _pick_track(self, tracks: list[dict], lan: str) -> dict:
        if lan:
            key = str(lan).strip().lower()
            hit = (next((t for t in tracks if t["lan"].lower() == key), None)
                   or next((t for t in tracks
                            if key in (t["lan"] + " " + t["lan_doc"]).lower()), None))
            if not hit:
                names = ", ".join(t["lan"] for t in tracks)
                raise ApiError(-400, f"没有该语言字幕：{lan}  可选 [{names}]")
            return hit
        for want in ("ai-zh", "zh-CN", "zh-Hans", "zh"):
            hit = next((t for t in tracks if t["lan"] == want), None)
            if hit:
                return hit
        return tracks[0]

    async def subtitle_cues(self, bvid: str, lan: str = "", page: int = 1) -> dict:
        meta = await self.subtitle_tracks(bvid, page)
        tracks = meta["tracks"]
        if not tracks:
            raise ApiError(-404, "该视频没有字幕轨（若为画面内嵌硬字幕则无法获取）")
        pick = self._pick_track(tracks, lan)
        if pick["lock"]:
            raise ApiError(-403, f'字幕轨 {pick["lan"]} 需购买/充电后解锁')
        key = (bvid, meta["cid"], pick["lan"])
        if key in self._subs:
            return self._subs[key]
        j = await self.http.get_raw_json(pick["url"], self._video_ref(bvid))
        cues = []
        for b in j.get("body") or []:
            text = " ".join(clean(b.get("content") or "").split())
            if not text:
                continue
            cues.append({"from": round(float(b.get("from") or 0), 2),
                         "to": round(float(b.get("to") or 0), 2), "text": text})
        out = {"bvid": bvid, "cid": meta["cid"], "page": meta["page"],
               "lan": pick["lan"], "lan_doc": pick["lan_doc"], "ai": pick["ai"],
               "tracks": [{"lan": t["lan"], "lan_doc": t["lan_doc"],
                           "lock": t["lock"]} for t in tracks],
               "cues": cues}
        self._subs[key] = out
        return out

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

    @staticmethod
    def _top_ids(tops: list[dict], rows: list[dict]) -> set:
        """置顶评论 rpid 集合：top_replies 全收，replies 里 reply_control.is_up_top 兜底"""
        ids = set()
        for r in tops:
            key = str(r.get("rpid_str") or r.get("rpid") or "")
            if key:
                ids.add(key)
        for r in rows:
            if ((r.get("reply_control") or {}).get("is_up_top")):
                key = str(r.get("rpid_str") or r.get("rpid") or "")
                if key:
                    ids.add(key)
        return ids

    @staticmethod
    def _norm_replies(rows: list[dict], top_ids: set | None = None) -> list[dict]:
        """主楼评论归一化（top_replies / replies 共用）。

        top_ids 命中者带 top=True，供前端渲染「置顶」标；非首页传空集合，
        避免翻页时把置顶条目重复标出来。
        """
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
                "top": bool(top_ids) and str(rpid or "") in top_ids,
            })
        return items

    async def _comments_cursor(self, aid: int, bvid: str, page: int,
                               mode: int, base: dict, plain: bool = False) -> dict:
        """wbi/main（或 plain main）的游标翻页：该接口不认 pn，只认 pagination_str。

        实测（2026-10）：带 pn=1/2/3 返回的永远是同一页；只有把上一页
        cursor.pagination_reply.next_offset 塞进 pagination_str 才能拿到下一页。
        这里顺序翻到第 page 页（page<=1 只发一次请求）。
        """
        referer = self._video_ref(bvid)
        offset, v = "", {}
        for _ in range(max(1, int(page))):
            if plain:
                params = dict(base)
                payload = json.dumps({"offset": offset}, separators=(",", ":"))
                params.pop("pn", None)
                params.pop("ps", None)
                params["pagination_str"] = payload
                v = await self.http.get_json(f"{API}/x/v2/reply/main", params, referer)
            else:
                payload = json.dumps({"offset": offset}, separators=(",", ":"))
                params = sign.sign_params(
                    {"oid": aid, "type": 1, "mode": mode,
                     "pagination_str": payload}, await self.mixin_key())
                v = await self.http.get_json(f"{API}/x/v2/reply/wbi/main", params, referer)
            cur = (v.get("data") or {}).get("cursor") or {}
            offset = ((cur.get("pagination_reply") or {}).get("next_offset")) or ""
            if cur.get("is_end") or not offset:
                break
        return v

    async def comments(self, aid: int, bvid: str, page: int = 1,
                       limit: int = 20, mode: int = 3) -> dict:
        """评论列表（真分页）。

        分页源优先级（实测 2026-10）：
        1. `/x/v2/reply`（legacy，wbi 签名）：唯一认 pn/ps 的接口，page 直接生效；
        2. `/x/v2/reply/wbi/main`：只认 pagination_str 游标，按 page 顺序翻；
        3. `/x/v2/reply/main`（plain）：未登录/风控时的兜底，通常只有几条精彩评论。
        """
        ps = max(1, min(20, int(limit)))
        pn = max(1, int(page or 1))
        base = {"oid": aid, "type": 1, "mode": mode, "pn": pn, "ps": ps}
        referer = self._video_ref(bvid)

        # 1) legacy：支持 pn/ps
        src, v, data = "wbi", None, {}
        try:
            params = sign.sign_params(base, await self.mixin_key())
            v = await self.http.get_json(f"{API}/x/v2/reply", params, referer)
            data = v.get("data") or {}
        except (ApiError, RiskControl, NeedLogin):
            v = None

        if v is not None:
            pg = data.get("page") or {}
            replies = list(data.get("replies") or [])
            # 置顶（reply_control.is_up_top）每页都会回传，仅在首页保留，避免翻页重复
            tops = list(data.get("top_replies") or []) if pn == 1 else []
            top_ids = self._top_ids(tops, replies) if pn == 1 else set()
            items = self._norm_replies(tops + replies, top_ids)[:ps + len(tops)]
            total = int(pg.get("count") or 0)
            size = int(pg.get("size") or ps)
            num = int(pg.get("num") or pn)
            if not replies:
                # 服务端深翻页会返回空列表（count 仍很大），此处必须收口，否则上层死循环
                is_end = True
            elif total:
                is_end = num * size >= total
            else:
                is_end = len(replies) < ps
            return {"aid": aid, "bvid": bvid, "page": pn, "source": src,
                    "total": total, "is_end": bool(is_end), "items": items}

        # 2) wbi/main 游标翻页
        try:
            src = "cursor"
            v = await self._comments_cursor(aid, bvid, pn, mode, base)
        except (ApiError, RiskControl, NeedLogin):
            # 3) plain 兜底（未登录态）
            src = "plain"
            v = await self._comments_cursor(aid, bvid, pn, mode, base, plain=True)

        data = v.get("data") or {}
        cur = data.get("cursor") or {}
        tops = list(data.get("top_replies") or []) if pn == 1 else []
        replies = list(data.get("replies") or [])
        top_ids = self._top_ids(tops, replies) if pn == 1 else set()
        items = self._norm_replies(tops + replies, top_ids)[:ps + len(tops)]
        return {"aid": aid, "bvid": bvid, "page": pn, "source": src,
                "total": int(cur.get("all_count") or 0),
                "is_end": bool(cur.get("is_end")), "items": items}

    async def comments_replies(self, aid: int, bvid: str, root: int | str,
                               page: int = 1, limit: int = 20) -> dict:
        """读某条主楼评论下的子回复（含楼中楼，展平为一层）。

        实测（2026-10）：登录态下无需 wbi 签名；data.replies[] 为直接子回复，
        每条可能再带 sub_replies[]，这里一并展平并保留 parent 便于精确回复。
        """
        ps = max(1, min(20, int(limit)))
        params = {"oid": int(aid), "type": 1, "root": int(root),
                  "pn": max(1, int(page)), "ps": ps}
        v = await self.http.get_json(f"{API}/x/v2/reply/reply", params,
                                     self._video_ref(bvid))
        data = v.get("data") or {}
        pg = data.get("page") or {}
        rows = list(data.get("replies") or [])
        items, seen = [], set()
        for r in rows:
            for x in [r] + list(r.get("sub_replies") or []):
                rpid = str(x.get("rpid_str") or x.get("rpid") or "")
                if not rpid or rpid in seen:
                    continue
                seen.add(rpid)
                mem = x.get("member") or {}
                items.append({
                    "rpid": rpid,
                    "root": str(x.get("root_str") or x.get("root") or root),
                    "parent": str(x.get("parent_str") or x.get("parent") or ""),
                    "mid": mem.get("mid") or 0,
                    "uname": mem.get("uname") or "",
                    "msg": " ".join(clean((x.get("content") or {}).get("message") or "").split()),
                    "ctime": x.get("ctime") or 0,
                })
        total = int(pg.get("count") or 0)
        num = int(pg.get("num") or page)
        size = int(pg.get("size") or ps)
        is_end = (not rows) or len(rows) < ps or bool(total and num * size >= total)
        return {"root": str(root), "page": page, "is_end": bool(is_end),
                "total": total, "items": items}

    async def find_my_comments(self, aid: int, bvid: str, pages: int = 3,
                               limit: int = 20) -> list[dict]:
        """扫主楼评论找我自己发的评论。

        /x/v2/reply/member 已 404，只能翻 /x/v2/reply/wbi/main 主楼按
        member.mid == self.mid 匹配；命中后停止翻页。
        """
        me = int(self.mid or 0)
        if not me:
            raise NeedLogin("未登录：无法定位自己的评论")
        out, seen = [], set()
        pages = max(1, min(20, int(pages)))
        for p in range(1, pages + 1):
            r = await self.comments(aid, bvid, p, limit, 3)
            for it in r["items"]:
                if int(it.get("mid") or 0) != me:
                    continue
                if it["rpid"] in seen:
                    continue
                seen.add(it["rpid"])
                out.append({"rpid": it["rpid"], "msg": it["msg"],
                            "sub": it["sub"], "ctime": it["ctime"]})
            if r["is_end"]:
                break
        return out

    def _comment_ref(self, ctype: int, oid: int, bvid: str = "") -> str:
        """按评论对象类型给 Referer：视频用 bvid，文章用 cv，动态用 t.bilibili"""
        if ctype == 12:
            return f"https://www.bilibili.com/read/cv{oid}"
        if ctype == 17:
            return f"https://t.bilibili.com/{oid}"
        if bvid:
            return self._video_ref(bvid)
        return WEB

    async def comment_add(self, aid: int, bvid: str, message: str,
                          root: int | str = 0, parent: int | str = 0,
                          type: int = 1) -> dict:
        """发表评论。type 为评论对象类型（1 视频 / 12 文章 / 17 动态），默认视频。
        root/parent 非 0 时是对某条评论的回复。

        写接口：需登录态 + csrf(bili_jct)，未登录 http 层直接抛 NeedLogin。
        """
        msg = (message or "").strip()
        if not msg:
            raise ValueError("评论内容不能为空")
        if len(msg) > 1000:
            raise ValueError("评论内容超过 1000 字上限")
        ctype = int(type or 1)
        payload = {"oid": aid, "type": ctype, "message": msg, "plat": 1}
        if root:
            payload["root"] = root
            payload["parent"] = parent or root
        # x/v2/reply/add 需 wbi 签名，缺失时服务端直接回 404/412
        params = sign.sign_params({"oid": aid, "type": ctype}, await self.mixin_key())
        url = f"{API}/x/v2/reply/add?" + urlencode(params)
        v = await self.http.post_json(url, payload, self._comment_ref(ctype, aid, bvid))
        d = v.get("data") or {}
        return {"aid": aid, "bvid": bvid, "oid": aid, "type": ctype,
                "rpid": str(d.get("rpid_str") or d.get("rpid") or ""),
                "message": msg}

    # ---------------- 消息（回复我的 / @我的） ----------------

    async def _msgfeed(self, kind: str) -> list[dict]:
        """拉 msgfeed 原始条目。kind: reply | at。需登录态。"""
        path = "at" if str(kind).lower() == "at" else "reply"
        v = await self.http.get_json(
            f"{MSG}/{path}",
            {"platform": "web", "build": 0, "mobi_app": "web"}, MSGREF)
        return list((v.get("data") or {}).get("items") or [])

    @staticmethod
    def _norm_msg(row: dict, kind: str) -> dict | None:
        """归一化一条消息；business_id 不在映射表里（系统通知等）返回 None"""
        it = row.get("item") or {}
        u = row.get("user") or {}
        try:
            ctype = BIZ2TYPE.get(int(it.get("business_id") or 0))
        except (TypeError, ValueError):
            ctype = None
        oid = int(it.get("subject_id") or 0)
        if not ctype or not oid:
            return None
        root = int(it.get("root_id") or 0) or int(it.get("source_id") or 0)
        parent = int(it.get("source_id") or 0) or root
        title = (clean(it.get("title") or "") or clean(it.get("detail_title") or "")
                 or str(it.get("desc") or ""))
        return {
            "msg_id": str(row.get("id") or ""),
            "kind": kind,
            "mid": int(u.get("mid") or 0),
            "uname": u.get("nickname") or "",
            "avatar": _abs(u.get("avatar") or ""),
            "oid": oid,
            "type": ctype,
            "root": root,
            "parent": parent,
            "uri": it.get("uri") or "",
            "title": " ".join(str(title).split())[:120],
            "his_msg": _strip_at(it.get("source_content") or "")[:500],
            "my_msg": _strip_at(it.get("target_reply_content") or "")[:500],
            "ts": int(row.get("reply_time") or row.get("at_time") or 0),
        }

    async def msg_replies(self) -> list[dict]:
        """别人回复我（含回复我的评论）的消息列表，已归一化"""
        return [m for m in (self._norm_msg(r, "reply")
                            for r in await self._msgfeed("reply")) if m]

    async def msg_ats(self) -> list[dict]:
        """别人 @我 的消息列表，已归一化"""
        return [m for m in (self._norm_msg(r, "at")
                            for r in await self._msgfeed("at")) if m]

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
