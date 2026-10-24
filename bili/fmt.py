"""展示层：给 AI 看的紧凑文本（一行一条、短键、稳定 id）"""

import re
from html import unescape

_TAG = re.compile(r"<[^>]+>")


def clean(s: str) -> str:
    return unescape(_TAG.sub("", s or "")).strip()


def cnt(n: int) -> str:
    if n >= 100_000_000:
        return f"{n / 100_000_000:.1f}亿"
    if n >= 10_000:
        return f"{n / 10_000:.1f}万"
    return str(n)


def hms(sec: float) -> str:
    sec = max(0, int(sec))
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def search(r: dict) -> str:
    lines = [f'view=search q="{r["query"]}" page={r["page"]} '
             f'total={cnt(r["total"])} shown={len(r["items"])}']
    for i, it in enumerate(r["items"], 1):
        title = it["title"][:48]
        lines.append(f'#{i}  {it["bvid"]}  {it["dur"]:>6}  play={cnt(it["play"]):>7}  '
                     f'up={it["up"]}  {title}')
    if not r["items"]:
        lines.append("(空结果：可能命中风控，稍后重试或登录)")
    else:
        lines.append(f'next: search --keyword "{r["query"]}" --page {r["page"] + 1}')
    return "\n".join(lines)


def comments(r: dict) -> str:
    head = (f'view=comments  bvid={r["bvid"]}  page={r["page"]}  '
            f'total={cnt(r["total"])}  src={r["source"]}')
    if r["source"] == "plain":
        head += "  (未登录：仅返回精彩评论 3 条)"
    lines = [head]
    for i, c in enumerate(r["items"], 1):
        lines.append(f'#{i}  rpid={c["rpid"]}  like={c["like"]}  sub={c["sub"]}  '
                     f'{c["uname"]}: {c["msg"][:80]}')
    if not r["items"]:
        lines.append("(无评论)")
    elif not r["is_end"]:
        lines.append(f'next: comments --page {r["page"] + 1}')
    return "\n".join(lines)


def inter(d: dict) -> str:
    """互动状态一行：1=已操作 0=未操作 ?=未登录/未知"""
    def flag(v):
        return "?" if v is None else ("1" if v else "0")
    return f'inter: like={flag(d.get("liked"))} coin={flag(d.get("coins"))} fav={flag(d.get("favoured"))}'


def detail(d: dict) -> str:
    return (
        f'view=video  bvid={d["bvid"]}  av={d["avid"]}  '
        f'title="{d["title"][:60]}"  up={d["up"]}(mid={d["mid"]})  '
        f'dur={d["dur"]}  pub={d["pubdate"]}\n'
        f'stat: play={cnt(d["view"])} like={cnt(d["like"])} coin={cnt(d["coin"])} '
        f'fav={cnt(d["fav"])} danmaku={cnt(d["danmaku"])} reply={cnt(d["reply"])}\n'
        f'desc: {d["desc"][:120]}'
    )
