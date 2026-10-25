"""评论分页离线回归测试（不联网）。

覆盖 bili/api.py `Bili.comments()` 的三级数据源降级、置顶去重与空页收口：

1. legacy `/x/v2/reply` 认 pn/ps —— 正常路径；
2. legacy 失败 → `/x/v2/reply/wbi/main` 游标翻页（pagination_str 递进）；
3. 游标接口也失败 → plain `/x/v2/reply/main` 兜底；
4. 置顶条目（top_replies / reply_control.is_up_top）只在首页标 top=True 且不重复；
5. replies 为空但 count 很大时必须收口 is_end=True，否则上层会死循环翻页。

跑法：`python -m pytest tests -q`，或直接 `python tests/test_comments_page.py`。
"""

import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from bili.api import Bili  # noqa: E402
from bili.http import ApiError, RiskControl  # noqa: E402

API = "https://api.bilibili.com"
LEGACY = f"{API}/x/v2/reply"
WBI_MAIN = f"{API}/x/v2/reply/wbi/main"
PLAIN_MAIN = f"{API}/x/v2/reply/main"


# ---------------- 测试替身 ----------------

class FakeApi:
    """替身 HTTP 层：按 URL 分派，记录每次请求的参数。"""

    def __init__(self, routes):
        self.routes = routes          # {url: fn(params) -> dict} 或 {url: dict}
        self.calls = []               # [(url, params)]

    async def get_json(self, url, params=None, referer=None):
        params = dict(params or {})
        self.calls.append((url, params))
        r = self.routes.get(url)
        if isinstance(r, Exception):
            raise r
        if callable(r):
            return r(params)
        if r is None:
            raise AssertionError(f"未预期的请求：{url}")
        return r

    def urls(self):
        return [u for u, _ in self.calls]


def make_bili(routes) -> Bili:
    b = Bili.__new__(Bili)
    b.http = FakeApi(routes)
    b._mixin = "a" * 32           # 缓存 wbi mixin key，避免走 nav 请求
    b._mixin_at = 1e18
    b._subs = {}
    return b


def row(rpid, uname="u", msg="m", like=0, sub=0, ctime=1, up_top=False):
    r = {"rpid": rpid, "rpid_str": str(rpid), "member": {"uname": uname, "mid": 1},
         "content": {"message": msg}, "like": like, "rcount": sub, "ctime": ctime}
    if up_top:
        r["reply_control"] = {"is_up_top": 1}
    return r


def legacy_payload(replies, tops=(), count=57, size=20, num=1):
    return {"code": 0, "data": {"page": {"count": count, "size": size, "num": num},
                                "replies": list(replies),
                                "top_replies": list(tops)}}


# ---------------- 1. legacy 正常路径 + 置顶去重 ----------------

def test_legacy_page_marks_top_and_dedupes():
    # 置顶条目同时出现在 top_replies 与 replies —— 只应输出一次且带 top
    dup = row(100, uname="UP", msg="置顶说明", like=9)
    payload = legacy_payload([dup, row(101), row(102)], tops=[dup])
    b = make_bili({LEGACY: payload})

    r = asyncio.run(b.comments(111, "BV1xx", page=1, limit=20))

    assert r["source"] == "wbi"
    assert [c["rpid"] for c in r["items"]] == ["100", "101", "102"]   # 无重复
    assert r["items"][0]["top"] is True
    assert r["items"][1]["top"] is False
    assert r["total"] == 57
    assert r["is_end"] is False                                       # 57 条 1 页 20 条


def test_legacy_is_end_on_last_page():
    payload = legacy_payload([row(1)], count=21, size=20, num=2)
    b = make_bili({LEGACY: payload})

    r = asyncio.run(b.comments(111, "BV1xx", page=2, limit=20))

    assert r["is_end"] is True


def test_non_first_page_does_not_mark_top():
    # 第 2 页即便服务端回了 is_up_top，也不该标置顶（避免翻页重复出现置顶条目）
    payload = legacy_payload([row(200, up_top=True)], tops=[row(200, up_top=True)],
                             count=100, size=20, num=2)
    b = make_bili({LEGACY: payload})

    r = asyncio.run(b.comments(111, "BV1xx", page=2, limit=20))

    assert r["items"][0]["top"] is False


# ---------------- 2. legacy 失败 → 游标翻页 ----------------

def test_fallback_cursor_walks_offset():
    seen_offsets = []

    def wbi(params):
        seen_offsets.append(params.get("pagination_str"))
        assert "pn" not in params
        if len(seen_offsets) == 1:
            return {"code": 0, "data": {
                "cursor": {"is_end": False, "all_count": 300,
                           "pagination_reply": {"next_offset": "off2"}},
                "replies": [row(301), row(302)]}}
        return {"code": 0, "data": {
            "cursor": {"is_end": True, "all_count": 300},
            "replies": [row(303)]}}

    b = make_bili({LEGACY: ApiError(-400, "request error"), WBI_MAIN: wbi})

    r1 = asyncio.run(b.comments(111, "BV1xx", page=1, limit=20))
    assert r1["source"] == "cursor"
    assert [c["rpid"] for c in r1["items"]] == ["301", "302"]
    assert len(seen_offsets) == 1                              # 首页只请求一次

    seen_offsets.clear()
    r2 = asyncio.run(b.comments(111, "BV1xx", page=2, limit=20))
    assert r2["source"] == "cursor"
    assert r2["total"] == 300
    assert r2["is_end"] is True
    assert len(seen_offsets) == 2                              # 第 2 页顺序翻两跳
    assert "off2" in seen_offsets[1]                           # 上一页游标被透传
    assert [c["rpid"] for c in r2["items"]] == ["303"]


# ---------------- 3. 游标也失败 → plain 兜底 ----------------

def test_fallback_plain_main_when_cursor_blocked():
    def plain(params):
        assert "pagination_str" in params
        assert "pn" not in params and "ps" not in params
        return {"code": 0, "data": {
            "cursor": {"is_end": True, "all_count": 3},
            "replies": [row(401, msg="精彩评论")]}}

    b = make_bili({LEGACY: ApiError(-400, "request error"),
                   WBI_MAIN: RiskControl("v_voucher"),
                   PLAIN_MAIN: plain})

    r = asyncio.run(b.comments(111, "BV1xx", page=1, limit=20))

    assert r["source"] == "plain"
    assert [c["rpid"] for c in r["items"]] == ["401"]
    assert b.http.urls() == [LEGACY, WBI_MAIN, PLAIN_MAIN]


# ---------------- 4. 空页收口 ----------------

def test_empty_page_closes_pagination():
    # 深翻页时服务端返回空 replies 但 count 仍然很大：必须 is_end，否则死循环
    payload = legacy_payload([], count=5000, size=20, num=251)
    b = make_bili({LEGACY: payload})

    r = asyncio.run(b.comments(111, "BV1xx", page=251, limit=20))

    assert r["items"] == []
    assert r["is_end"] is True


# ---------------- 5. limit 越界被夹紧 ----------------

def test_limit_clamped_to_bili_range():
    caps = {}

    def legacy(params):
        caps.clear()
        caps.update(params)                       # 签名层会把参数值转成 str
        return legacy_payload([row(1)], count=10, size=20, num=1)

    b = make_bili({LEGACY: legacy})

    asyncio.run(b.comments(111, "BV1xx", page=1, limit=30))
    assert int(caps["ps"]) == 20          # ps=30 会被 B 站判 -400
    asyncio.run(b.comments(111, "BV1xx", page=1, limit=0))
    assert int(caps["ps"]) == 1


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print(f"FAIL {name}: {exc!r}")
    raise SystemExit(1 if failed else 0)
