"""弹幕功能离线回归测试（不联网）。

覆盖三层：
1. `bili/api.py` 的 dm_color / parse_danmaku_xml / danmaku_send / danmaku_list；
2. 写接口的登录态与参数校验（NeedLogin 阻断、100 字上限、模式白名单）；
3. `bili/core.py` 的 cmd_danmaku：默认 dry-run 绝不发 POST，confirm=True 才发一次。

跑法：`python -m pytest tests -q`，或直接 `python tests/test_danmaku.py`。
"""

import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from bili.api import DM_MODES, Bili, parse_danmaku_xml  # noqa: E402
from bili.core import Core  # noqa: E402
from bili.http import NeedLogin  # noqa: E402
from bili.store import store  # noqa: E402

API = "https://api.bilibili.com"

XML = """<?xml version="1.0" encoding="UTF-8"?><i>
<chatserver>chat.bilibili.com</chatserver>
<d p="12.34,1,25,16777215,1700000000,0,abc,1">早啊&amp;你好</d>
<d p="95.00,5,25,16711680,1700000001,0,def,2">顶端提示</d>
<d p="5.50,4,18,255,1700000002,0,ghi,3">底部字幕</d>
<d p="300.00,1,25,16777215,1700000003,0,jkl,4">太远了</d>
</i>"""


# ---------------- 测试替身 ----------------

class FakeHttp:
    """替身 HTTP 层：记录 POST，按需返回 XML 或抛 NeedLogin。"""

    def __init__(self, login=True, code=0, xml=XML):
        self.login = login
        self.code = code
        self.xml = xml
        self.posts = []          # [(url, payload, referer)]
        self.gets = []           # [(url, referer)]

    async def post_json(self, url, payload=None, referer=None):
        if not self.login:
            raise NeedLogin("未登录")
        self.posts.append((url, dict(payload or {}), referer))
        if self.code:
            return {"code": self.code, "message": "风控拦截"}
        return {"code": 0, "data": {"dmid": 987654321, "dmid_str": "987654321"}}

    async def get_text(self, url, referer=None):
        self.gets.append((url, referer))
        return self.xml


def make_bili(login=True, code=0, xml=XML, cid=555) -> Bili:
    b = Bili.__new__(Bili)
    b.http = FakeHttp(login=login, code=code, xml=xml)

    async def pages(bvid):
        return [{"cid": cid, "page": 1, "part": "P1", "dur": 600},
                {"cid": cid + 1, "page": 2, "part": "P2", "dur": 600}]

    b.pages = pages            # 实例属性，避免真实网络请求
    b._subs = {}
    return b


class FakeBili:
    """Core 层替身：只实现弹幕相关两个方法。"""

    def __init__(self):
        self.sent = []
        self.http = type("H", (), {"sync_from_disk": lambda self: None})()

    def dm_color(self, c):
        return Bili.dm_color(c)

    async def danmaku_send(self, aid, bvid, message, **kw):
        self.sent.append({"aid": aid, "bvid": bvid, "message": message, **kw})
        return {"aid": aid, "bvid": bvid, "message": message,
                "dmid": "987654321", "mode": kw.get("mode", 1),
                "mode_name": DM_MODES[kw.get("mode", 1)],
                "progress": kw.get("progress_ms", 0), "color": kw.get("color")}

    async def danmaku_list(self, bvid, **kw):
        return {"bvid": bvid, "cid": 555, "page": kw.get("page", 1), "parts": 2,
                "total": 4, "offset": kw.get("offset", 0),
                "limit": kw.get("limit", 100), "around": -1.0, "window": 30.0,
                "items": [{"t": 5.5, "mode": 1, "mode_name": "scroll",
                           "size": 25, "color": 16777215, "text": "hello"}],
                "is_end": True}


def make_core(cur=None):
    c = Core.__new__(Core)          # 跳过 __init__，避免建真实 Bili
    c.bili = FakeBili()
    c.readonly = False
    target = cur if cur is not None else {"bvid": "BV1xx411c7mD", "avid": 111,
                                          "title": "测试视频标题", "dur": 600}

    async def _target(bvid=""):
        return target

    c._target = _target
    return c


# ---------------- 1. 颜色归一 ----------------

def test_dm_color_formats():
    assert Bili.dm_color("#00AEEC") == 44780
    assert Bili.dm_color("00AEEC") == 44780
    assert Bili.dm_color("#FFFFFF") == 16777215
    assert Bili.dm_color(16711680) == 16711680
    assert Bili.dm_color("16711680") == 16711680
    # 非法值一律回白色，绝不让脏值进请求体
    assert Bili.dm_color("bad") == 16777215
    assert Bili.dm_color("#12345") == 16777215
    assert Bili.dm_color(None) == 16777215
    assert Bili.dm_color("") == 16777215


# ---------------- 2. XML 解析 ----------------

def test_parse_xml_decodes_entities_and_fields():
    items = parse_danmaku_xml(XML)
    assert len(items) == 4
    first = items[0]
    assert first["t"] == 12.34
    assert first["mode"] == 1 and first["mode_name"] == "scroll"
    assert first["size"] == 25 and first["color"] == 16777215
    assert first["text"] == "早啊&你好"          # html.unescape 生效
    assert items[1]["mode_name"] == "top"
    assert items[2]["mode_name"] == "bottom"
    assert parse_danmaku_xml("<i></i>") == []


# ---------------- 3. 发送：请求体与登录态 ----------------

def test_send_payload_matches_bili_api():
    b = make_bili()
    r = asyncio.run(b.danmaku_send(111, "BV1xx411c7mD", "  前方高能  ",
                                   progress_ms=90300, mode=5,
                                   fontsize=25, color="#00AEEC"))
    url, payload, referer = b.http.posts[0]
    assert url == f"{API}/x/v2/dm/post"
    assert payload["type"] == 1
    assert payload["oid"] == 111
    assert payload["msg"] == "前方高能"            # 已 strip
    assert payload["bvid"] == "BV1xx411c7mD"
    assert payload["progress"] == 90300
    assert payload["mode"] == 5
    assert payload["color"] == 44780
    assert payload["pool"] == 0 and payload["plat"] == 1
    assert "bilibili.com/video/BV1xx411c7mD" in (referer or "")
    assert r["dmid"] == "987654321"


def test_send_requires_login():
    b = make_bili(login=False)
    try:
        asyncio.run(b.danmaku_send(111, "BV1xx411c7mD", "hi"))
    except NeedLogin:
        pass
    else:
        raise AssertionError("未登录必须抛 NeedLogin")
    assert b.http.posts == []                     # 一个请求都不该发出去


def test_send_rejects_bad_args():
    b = make_bili()
    for kwargs, why in [
        ({"message": "   "}, "空内容"),
        ({"message": "字" * 101}, "超 100 字"),
        ({"message": "ok", "mode": 7}, "非法模式"),
    ]:
        try:
            asyncio.run(b.danmaku_send(111, "BV1xx411c7mD", **kwargs))
        except ValueError:
            continue
        raise AssertionError(f"{why} 应抛 ValueError")
    assert b.http.posts == []


# ---------------- 4. 列表：全量 / around 窗口 / 分页 ----------------

def test_list_all_sorted():
    b = make_bili()
    r = asyncio.run(b.danmaku_list("BV1xx411c7mD", limit=100))
    assert r["total"] == 4
    assert [d["t"] for d in r["items"]] == [5.5, 12.34, 95.0, 300.0]   # 按时间排序
    assert r["is_end"] is True
    assert r["cid"] == 555 and r["parts"] == 2
    assert "oid=555" in b.http.gets[0][0]


def test_list_around_window():
    b = make_bili()
    r = asyncio.run(b.danmaku_list("BV1xx411c7mD", around=90, window=30))
    assert [round(d["t"], 2) for d in r["items"]] == [95.0]            # 60~120s
    assert r["total"] == 4                                             # total 仍是全量


def test_list_paging_cursor():
    b = make_bili()
    r1 = asyncio.run(b.danmaku_list("BV1xx411c7mD", offset=0, limit=2))
    assert [d["text"] for d in r1["items"]] == ["底部字幕", "早啊&你好"]
    assert r1["is_end"] is False
    r2 = asyncio.run(b.danmaku_list("BV1xx411c7mD", offset=2, limit=2))
    assert [d["t"] for d in r2["items"]] == [95.0, 300.0]
    assert r2["is_end"] is True
    # limit 越界被夹到 1..500
    assert asyncio.run(b.danmaku_list("BV1xx411c7mD", limit=999))["limit"] == 500
    assert asyncio.run(b.danmaku_list("BV1xx411c7mD", limit=0))["limit"] == 1


# ---------------- 5. Core：dry-run 不发、confirm 才发 ----------------

def test_core_dry_run_never_posts():
    c = make_core()
    out = asyncio.run(c.cmd_danmaku(message="前方高能", at="1:30"))
    assert "dry-run" in out
    assert "1:30" in out and "前方高能" in out
    assert c.bili.sent == []                       # 关键：一条都没发


def test_core_confirm_sends_once_with_ms():
    c = make_core()
    out = asyncio.run(c.cmd_danmaku(message="前方高能", at="01:30.5",
                                    mode=5, color="#00AEEC", confirm=True))
    assert "ok danmaku" in out
    assert len(c.bili.sent) == 1
    s = c.bili.sent[0]
    assert s["progress_ms"] == 90500               # 1:30.5 -> 90500ms
    assert s["mode"] == 5 and s["color"] == 44780
    assert s["aid"] == 111


def test_core_rejects_bad_input():
    c = make_core()
    assert asyncio.run(c.cmd_danmaku(message="", at="10")).startswith("ERR_NEED_TEXT")
    assert asyncio.run(c.cmd_danmaku(message="字" * 101, at="10")).startswith("ERR_TEXT")
    assert asyncio.run(c.cmd_danmaku(message="ok", at="10", mode=7)).startswith("ERR_MODE")
    assert asyncio.run(c.cmd_danmaku(message="ok", at="abc")).startswith("ERR_BAD_AT")
    assert asyncio.run(c.cmd_danmaku(message="ok", at="9999")).startswith("ERR_BAD_AT")
    assert c.bili.sent == []


def test_core_at_defaults_to_player_progress():
    c = make_core()
    store.player.bvid = "BV1xx411c7mD"
    store.player.base, store.player.playing = 42.0, False
    try:
        out = asyncio.run(c.cmd_danmaku(message="打卡", at=""))
        assert "00:42" in out
    finally:
        store.player.bvid = ""
        store.player.base, store.player.playing = 0.0, False


def test_sec_parser():
    f = Core._sec_of
    assert f("90") == 90.0
    assert f("1:30") == 90.0
    assert f("01:02:03") == 3723.0
    assert f("1:30.5") == 90.5
    assert f(12) == 12.0
    assert f("", 7) == 7.0
    for bad in ("abc", "1:xx", ""):
        try:
            f(bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad!r} 应抛 ValueError")


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
