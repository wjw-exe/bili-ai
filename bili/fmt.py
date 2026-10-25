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
        flag = "[置顶] " if c.get("top") else ""
        lines.append(f'#{i}  rpid={c["rpid"]}  like={c["like"]}  sub={c["sub"]}  '
                     f'{flag}{c["uname"]}: {c["msg"][:80]}')
    if not r["items"]:
        lines.append("(无评论)")
    elif not r["is_end"]:
        lines.append(f'next: comments --page {r["page"] + 1}')
    return "\n".join(lines)


def danmaku_list(r: dict) -> str:
    """弹幕列表：[mode 标记] 时间 正文。is_end 时给 next 游标"""
    scope = (f'  窗口={hms(r["around"] - r["window"])}~{hms(r["around"] + r["window"])}'
             if r.get("around", -1) >= 0 else "")
    shown = (f'{r["offset"] + 1}-{r["offset"] + len(r["items"])}'
             if r["items"] else "0")
    lines = [f'view=danmaku  bvid={r["bvid"]}  cid={r["cid"]}  '
             f'page={r["page"]}/{r["parts"]}  visible={cnt(r["total"])}  '
             f'shown={shown}{scope}']
    for i, d in enumerate(r["items"], 1):
        flag = {"bottom": "[底]", "top": "[顶]"}.get(d["mode_name"], "")
        lines.append(f'#{r["offset"] + i}  {hms(d["t"]):>7}  {flag}{d["text"][:70]}')
    if not r["items"]:
        lines.append("(该区间没有弹幕)")
    elif not r["is_end"]:
        lines.append(f'next: danmaku_list --offset {r["offset"] + len(r["items"])} '
                     f'--limit {r["limit"]}')
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


# ---------------- 字幕 ----------------

def _hms_ms(sec: float) -> str:
    """SRT 时间戳：00:00:01,240"""
    ms = int(round(float(sec) * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def subtitle_tracks(r: dict) -> str:
    head = (f'view=subtitles  bvid={r["bvid"]}  cid={r["cid"]}  '
            f'page={r["page"]}/{r["parts"]}  tracks={len(r["tracks"])}')
    lines = [head]
    if not r["tracks"]:
        lines.append("(无字幕轨：该视频没有平台字幕；画面内嵌硬字幕无法获取)")
        return "\n".join(lines)
    for i, t in enumerate(r["tracks"], 1):
        lines.append(f'#{i}  lan={t["lan"]}  {t["lan_doc"]}  '
                     f'ai={int(t["ai"])}  lock={int(t["lock"])}')
    first = next((t for t in r["tracks"] if not t["lock"]), r["tracks"][0])
    lines.append(f'next: subtitle --lang {first["lan"]} --offset 0 --limit 200')
    return "\n".join(lines)


def subtitle_head(r: dict) -> str:
    return (f'view=subtitle  bvid={r["bvid"]}  lan={r["lan"]}  {r["lan_doc"]}  '
            f'ai={int(r["ai"])}  cues={len(r["cues"])}  '
            f'chars={sum(len(c["text"]) for c in r["cues"])}')


def subtitle_page(r: dict, offset: int, limit: int) -> str:
    cues = r["cues"]
    lo, hi = max(0, offset), min(len(cues), max(0, offset) + max(1, limit))
    lines = [f"{subtitle_head(r)}  offset={lo} limit={hi - lo}"]
    for c in cues[lo:hi]:
        lines.append(f'[{hms(c["from"])}] {c["text"]}')
    if not cues:
        lines.append("(字幕内容为空)")
    elif hi < len(cues):
        lines.append(f'next: subtitle --offset {hi} --limit {limit}')
    else:
        lines.append("(已到末尾)")
    return "\n".join(lines)


def subtitle_srt(r: dict) -> str:
    out = []
    for i, c in enumerate(r["cues"], 1):
        out.append(f'{i}\n{_hms_ms(c["from"])} --> {_hms_ms(c["to"])}\n{c["text"]}\n')
    return "\n".join(out)


def subtitle_txt(r: dict) -> str:
    """纯文本稿：一行一句，不带时间码，适合直接喂给模型做摘要"""
    return "\n".join(c["text"] for c in r["cues"])


def subtitle_md(r: dict) -> str:
    """带时间码的 Markdown 稿，适合做章节化摘要"""
    lines = [f'# {r["bvid"]} 字幕 · {r["lan_doc"]} · {len(r["cues"])} 句', ""]
    for c in r["cues"]:
        lines.append(f'- **[{hms(c["from"])}]** {c["text"]}')
    return "\n".join(lines) + "\n"
