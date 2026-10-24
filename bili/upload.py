"""投稿（发视频）：preupload -> upos 分片上传 -> web add 提交审核

接口来自 B 站 Web 投稿台（member.bilibili.com），与 biliup 的实现对齐：

1. ``GET  /preupload?r=upos&profile=ugcupos/bup&name=&size=``
   -> ``endpoint / upos_uri / auth / biz_id / chunk_size``
2. ``POST https:{endpoint}/{upos_uri}?uploads&output=json``  -> ``upload_id``
3. ``PUT  同上``  带 ``X-Upos-Auth`` 逐块上传（params: uploadId/chunk/size/partNumber/start/end）
4. ``POST 同上``  ``{"parts": [{partNumber, eTag}]}`` 合并分片
5. ``POST /x/vu/web/add?csrf=bili_jct`` 提交稿件进审核

安全边界：第 5 步是**真实投稿**（公开可见、不可撤回），所以外层 ``Core.cmd_upload``
默认 dry-run，只有显式 ``confirm=True`` 才会走完整流程。
"""

import asyncio
import base64
import math
import os
import time
from pathlib import Path
from typing import Any

from .http import NeedLogin, RiskControl

PRE = "https://member.bilibili.com/preupload"
ADD = "https://member.bilibili.com/x/vu/web/add"
COVER = "https://member.bilibili.com/x/vu/web/cover/up"
ZONES = "https://member.bilibili.com/x/vupre/web/archive/pre"
ARCHIVES = "https://member.bilibili.com/x/web/archives"
GEETEST = "https://member.bilibili.com/x/geetest/pre/add"
REF = "https://member.bilibili.com/platform/upload/video/frame"

VIDEO_EXT = {".mp4", ".flv", ".mov", ".mkv", ".avi", ".wmv", ".webm", ".m4v"}
MAX_SIZE = 8 * 1024 ** 3              # Web 投稿单文件上限 8G
DEFAULT_CHUNK = 8 * 1024 * 1024


class UploadError(Exception):
    """投稿链路上的可读错误（参数/本地文件/接口返回）"""


# 投稿接口常见返回码的人话补充（未见过的码只回原始 message）
SUBMIT_CODES = {
    21001: "参数错误：多为 tid 不存在、标题/标签超限或字段缺失",
    21003: "分区不存在：用 upload_zones 重新取 tid",
    21004: "标题不合法",
    21103: "投稿被判定为重复内容",
    21564: "投稿触发频率限制（短时间内多次提交），需等待一段时间再试",
    21540: "账号未完成实名认证/风险校验，需在网页端完成一次人工投稿",
}


def _mb(n: int | float) -> str:
    return f"{n / 1024 / 1024:.1f}MB"


def scan(path: str) -> dict:
    """本地预检：存在性、扩展名、大小。不做任何网络请求。"""
    p = Path(str(path or "")).expanduser()
    if not str(path or "").strip():
        raise UploadError("缺少视频文件路径")
    if not p.is_file():
        raise UploadError(f"文件不存在: {p}")
    size = p.stat().st_size
    if size <= 0:
        raise UploadError(f"空文件: {p}")
    if p.suffix.lower() not in VIDEO_EXT:
        raise UploadError(f"不支持的后缀 {p.suffix}，可选 "
                          f"{'/'.join(sorted(e.lstrip('.') for e in VIDEO_EXT))}")
    if size > MAX_SIZE:
        raise UploadError(f"文件超过 8G 上限（{_mb(size)}）")
    return {"path": str(p), "name": p.name, "size": size, "mb": _mb(size)}


async def preupload(http, name: str, size: int, cdn: str = "bda2") -> dict:
    """申请上传会话，拿到 endpoint / upos_uri / auth / chunk_size。"""
    params = {"r": "upos", "profile": "ugcupos/bup", "ssl": "0",
              "version": "2.8.12", "build": "2081200",
              "name": name, "size": size,
              "upcdn": cdn, "probe_version": "20221109"}
    try:
        r = await http.client.get(PRE, params=params, headers={"Referer": REF},
                                  timeout=20.0)
    except Exception as e:                        # noqa: BLE001 网络层统一转可读错误
        raise UploadError(f"preupload 网络失败: {e!r}") from e
    if r.status_code in (403, 412):
        raise RiskControl(f"preupload 被风控（HTTP {r.status_code}）："
                          f"多为短时间多次申请/异地，稍后重试或先在网页端手动投稿一次")
    if r.status_code != 200:
        raise UploadError(f"preupload HTTP {r.status_code}: {r.text[:120]}")
    v = r.json()
    if v.get("OK") != 1 or not v.get("upos_uri"):
        raise UploadError(f"preupload 被拒: {str(v)[:200]}")
    v.setdefault("chunk_size", DEFAULT_CHUNK)
    return v


def _read_at(path: str, start: int, size: int) -> bytes:
    """按偏移读一段；每个分片独立句柄，避免多线程共用 seek 位置互相踩。"""
    with open(path, "rb") as f:
        f.seek(start)
        return f.read(size)


async def upos_upload(http, session: dict, path: str, threads: int = 3) -> dict:
    """分片上传并在服务端合并，返回 B 站内部文件名（投稿时 videos[].filename）。"""
    endpoint = session["endpoint"]
    uri = str(session["upos_uri"]).replace("upos://", "")
    url = f"https:{endpoint}/{uri}"
    headers = {"X-Upos-Auth": session["auth"]}
    F = session.get("chunk_size") or DEFAULT_CHUNK
    F = max(1 * 1024 * 1024, int(F))

    try:
        r = await http.client.post(f"{url}?uploads&output=json", headers=headers,
                                   timeout=20.0)
        upload_id = r.json().get("upload_id") or ""
    except Exception as e:                        # noqa: BLE001
        raise UploadError(f"申请分片上传失败: {e!r}") from e
    if not upload_id:
        raise UploadError(f"申请分片上传失败: {r.text[:160]}")

    total = os.path.getsize(path)
    chunks = math.ceil(total / F)
    parts: list[dict] = []
    done = {"bytes": 0}
    t0 = time.perf_counter()
    lock = asyncio.Lock()
    sem = asyncio.Semaphore(max(1, min(6, int(threads or 3))))

    async def one(idx: int) -> None:
        start = idx * F
        data = await asyncio.to_thread(_read_at, path, start, F)
        if not data:
            return
        params = {"uploadId": upload_id, "chunk": idx, "size": len(data),
                  "partNumber": idx + 1, "start": start, "end": start + len(data)}
        async with sem:
            for attempt in range(6):
                try:
                    rr = await http.client.put(url, params=params, content=data,
                                               headers=headers, timeout=180.0)
                    if rr.status_code == 200:
                        break
                    raise UploadError(f"chunk {idx} HTTP {rr.status_code}")
                except Exception as e:            # noqa: BLE001 分片重试
                    if attempt == 5:
                        raise UploadError(f"分片 {idx} 上传失败: {e!r}") from e
                    await asyncio.sleep(1.0 * (attempt + 1))
        async with lock:
            parts.append({"partNumber": idx + 1, "eTag": "etag"})
            done["bytes"] += len(data)

    await asyncio.gather(*[one(i) for i in range(chunks)])

    cost = max(0.001, time.perf_counter() - t0)
    parts.sort(key=lambda x: x["partNumber"])
    merge_params = {"name": os.path.basename(path), "uploadId": upload_id,
                    "biz_id": session["biz_id"], "output": "json",
                    "profile": "ugcupos/bup"}
    last = ""
    for attempt in range(6):                      # 合并失败重试，避免白传
        try:
            rr = await http.client.post(url, params=merge_params,
                                        json={"parts": parts}, headers=headers,
                                        timeout=60.0)
            v = rr.json()
            if v.get("OK") == 1:
                break
            last = str(v)[:160]
        except Exception as e:                    # noqa: BLE001
            last = repr(e)
            v = {}
        if attempt == 5:
            raise UploadError(f"合并分片失败: {last}")
        await asyncio.sleep(2.0 * (attempt + 1))

    return {"filename": Path(uri).stem, "chunks": chunks, "chunk_size": F,
            "cost": round(cost, 1), "speed_mb": round(total / 1024 / 1024 / cost, 2)}


async def cover_up(http, image_path: str) -> str:
    """上传封面，返回可直接放进 cover 字段的 url（//i0.hdslb.com/...）。"""
    p = Path(str(image_path or "")).expanduser()
    if not p.is_file():
        raise UploadError(f"封面不存在: {p}")
    raw = p.read_bytes()
    try:                                          # 有 PIL 就裁成 16:10
        from io import BytesIO

        from PIL import Image

        with Image.open(BytesIO(raw)) as im:
            x, y = im.size
            if x / y > 1.6:
                d = x - y * 1.6
                box = (int(d / 2), 0, int(x - d / 2), y)
            else:
                d = y - x * 10 / 16
                box = (0, int(d / 2), x, int(y - d / 2))
            buf = BytesIO()
            im.crop(box).convert("RGB").save(buf, format="JPEG", quality=92)
            raw = buf.getvalue()
    except ImportError:
        pass                                      # 没有 PIL 就原图直传
    token = http.client.cookies.get("bili_jct")
    if not token:
        raise NeedLogin("未登录：缺少 bili_jct，无法上传封面/投稿")
    b64 = b"data:image/jpeg;base64," + base64.b64encode(raw)
    r = await http.client.post(COVER, data={"cover": b64, "csrf": token},
                               headers={"Referer": REF,
                                        "Origin": "https://member.bilibili.com"},
                               timeout=60.0)
    v = r.json()
    if v.get("code") != 0 or not (v.get("data") or {}).get("url"):
        raise UploadError(f"封面上传失败: {str(v)[:160]}")
    return str(v["data"]["url"])


async def tid_v2_for(http, tid: int) -> int | None:
    """新分区体系下投稿要带 tid_v2。用投稿预接口反查，拿不到就返回 None。"""
    try:
        r = await http.client.get(ZONES, params={"tid": int(tid), "lang": "cn"},
                                  headers={"Referer": REF}, timeout=15.0)
        d = (r.json() or {}).get("data") or {}
    except Exception:                             # noqa: BLE001 拿不到不阻塞主流程
        return None
    for k in ("tid_v2", "tidV2", "new_tid", "sub_tid"):
        try:
            v = int(d.get(k) or 0)
        except (TypeError, ValueError):
            v = 0
        if v:
            return v
    return None


async def submit(http, *, filename: str, title: str, tid: int,
                 tid_v2: int | None = None, desc: str = "", tags: str = "",
                 cover: str = "", copyright: int = 1, source: str = "",
                 dynamic: str = "", no_reprint: int = 1,
                 open_elec: int = 0, dtime: int | None = None) -> dict:
    """提交稿件进审核（真实发布，不可撤回）。"""
    token = http.client.cookies.get("bili_jct")
    if not token:
        raise NeedLogin("未登录：缺少 bili_jct，无法投稿")
    try:                                          # 投稿前的验证码预检，失败不影响主流程
        await http.client.get(GEETEST, headers={"Referer": REF}, timeout=10.0)
    except Exception:                             # noqa: BLE001
        pass

    payload: dict[str, Any] = {
        "copyright": int(copyright or 1),
        "source": str(source or ""),
        "tid": int(tid),
        "cover": str(cover or ""),
        "title": title,
        "desc_format_id": 0,
        "desc": desc,
        "desc_v2": [{"raw_text": desc, "biz_id": "", "type": 1}],
        "dynamic": dynamic,
        "subtitle": {"open": 0, "lan": ""},
        "tag": tags,
        "videos": [{"title": title[:80], "filename": filename, "desc": desc}],
        "no_reprint": int(no_reprint or 0),
        "open_elec": int(open_elec or 0),
    }
    if tid_v2:
        payload["tid_v2"] = int(tid_v2)
    if dtime:
        payload["dtime"] = int(dtime)

    try:
        r = await http.client.post(f"{ADD}?csrf={token}", json=payload,
                                   headers={"Referer": REF,
                                            "Origin": "https://member.bilibili.com"},
                                   timeout=30.0)
    except Exception as e:                        # noqa: BLE001
        raise UploadError(f"提交网络失败: {e!r}") from e
    try:
        v = r.json()
    except ValueError:
        raise UploadError(f"提交返回非 JSON: {r.text[:160]}")
    code = v.get("code", -1)
    if code == 0:
        return v.get("data") or {}
    if code in (-101, -403):
        raise NeedLogin(v.get("message") or "账号未登录")
    if code in (-412, -352, -799):
        raise RiskControl(f"code={code} {v.get('message') or ''}".strip())
    hint = SUBMIT_CODES.get(int(code), "")
    msg = (v.get("message") or "").strip()
    raise UploadError(f"提交被拒 code={code} {msg}"
                      f"{'（' + hint + '）' if hint else ''}")


async def publish(http, *, path: str, title: str, tid: int,
                  tid_v2: int | None = None, desc: str = "", tags: list[str] | None = None,
                  cover: str = "", copyright: int = 1, source: str = "",
                  dynamic: str = "", no_reprint: int = 1,
                  threads: int = 3, dtime: int | None = None) -> dict:
    """完整投稿链路：预检 -> preupload -> 分片上传 -> 封面 -> 提交。"""
    meta = scan(path)
    if not title.strip():
        raise UploadError("标题不能为空")
    if len(title) > 80:
        raise UploadError(f"标题最多 80 字（当前 {len(title)}）")
    if not int(tid or 0):
        raise UploadError("缺少分区 tid（用 upload_zones 查）")
    tag_str = ",".join(t.strip() for t in (tags or []) if str(t).strip())
    if len(tag_str) > 2000:
        raise UploadError("标签总长超限")

    t0 = time.perf_counter()
    session = await preupload(http, meta["name"], meta["size"])
    up = await upos_upload(http, session, meta["path"], threads=threads)
    cover_url = await cover_up(http, cover) if str(cover or "").strip() else ""
    args = dict(filename=up["filename"], title=title, tid=int(tid), tid_v2=tid_v2,
                desc=desc, tags=tag_str, cover=cover_url, copyright=copyright,
                source=source, dynamic=dynamic, no_reprint=no_reprint, dtime=dtime)
    try:
        d = await submit(http, **args)
    except UploadError as e:
        # 21001 常见诱因就是缺 tid_v2：反查一次再重提，仍失败就把原错抛出去
        if "21001" not in str(e) or tid_v2:
            raise
        guess = await tid_v2_for(http, int(tid))
        if not guess:
            raise
        args["tid_v2"] = guess
        d = await submit(http, **args)
    return {**meta, **up, "cover": cover_url,
            "aid": d.get("aid"), "bvid": d.get("bvid") or "",
            "total_cost": round(time.perf_counter() - t0, 1)}


async def zones(http) -> dict:
    """拉投稿分区表（tid）。返回 {"flat": {tid: 名称}, "tree": [...]}"""
    r = await http.client.get(ZONES, headers={"Referer": REF}, timeout=20.0)
    if r.status_code != 200:
        raise UploadError(f"分区接口 HTTP {r.status_code}")
    v = r.json()
    if v.get("code") not in (0, None):
        if v.get("code") in (-101, -403):
            raise NeedLogin(v.get("message") or "账号未登录")
        raise UploadError(f"分区接口 code={v.get('code')} {v.get('message') or ''}")
    data = v.get("data") or {}
    rows = data.get("typelist") or data.get("list") or data.get("tids") or []
    flat: dict[int, str] = {}
    tree: list[dict] = []
    for top in rows:
        if not isinstance(top, dict):
            continue
        name = str(top.get("name") or "")
        children = top.get("children") or top.get("sub") or []
        subs = []
        for c in children:
            if not isinstance(c, dict):
                continue
            cid, cname = c.get("id"), str(c.get("name") or "")
            if cid and cname:
                flat[int(cid)] = cname
                subs.append({"tid": int(cid), "name": cname})
        if subs:
            tree.append({"name": name, "subs": subs})
    if not flat:
        raise UploadError(f"分区表解析失败，原始键: {list(data)[:8]}")
    return {"flat": flat, "tree": tree}


async def archives(http, pn: int = 1, ps: int = 10, status: str = "") -> list[dict]:
    """我的稿件列表（含审核状态），用于确认投稿是否过审。"""
    params = {"pn": pn, "ps": ps, "status": status, "order": "ptime",
              "sort": "desc", "coiner": "", "draft": ""}
    r = await http.client.get(ARCHIVES, params=params, headers={"Referer": REF},
                              timeout=20.0)
    v = r.json()
    code = v.get("code", -1)
    if code in (-101, -403):
        raise NeedLogin(v.get("message") or "账号未登录")
    if code != 0:
        raise UploadError(f"稿件列表 code={code} {v.get('message') or ''}".strip())
    out = []
    for row in ((v.get("data") or {}).get("arc_audits") or []):
        a = (row or {}).get("Archive") or {}
        if not a:
            continue
        out.append({
            "aid": a.get("aid"), "bvid": a.get("bvid") or "",
            "title": a.get("title") or "",
            "state": a.get("state"), "state_desc": a.get("state_desc") or "",
            "ptime": a.get("ptime"), "dur": a.get("duration"),
        })
    return out
