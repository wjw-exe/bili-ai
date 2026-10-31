"""复合工具层：把 37 个原子 cmd 收敛成 12 个强模型可用的工具。

为什么不用原子 cmd：
- 37 个 schema 常驻上下文 ~4k token，模型选择困难、容易乱调；
- 原子工具返回整段格式化文本，多步任务很快把上下文撑爆。

这里的三条设计：
1. **复合**：一个工具做一串有依赖的原子调用（inspect_video = open+字幕+评论+弹幕）；
2. **卸载**：结构化大对象进 refstore，只回 summary + ref 句柄，细节用 read_ref 分页取；
3. **自证**：所有工具返回都带 verify 结论（规则引擎跑真实回执），不靠提示词约束模型。

红线：本层只调 core.execute(actor="ai")，不改 core 逻辑与默认值；
写操作必须 confirmed=True（由 agent.py 在用户点确认后注入，模型拿不到）。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any

from . import events, refstore

DATA = Path(__file__).resolve().parents[1] / ".data"
BATCH_DIR = Path(__file__).resolve().parents[2] / "workspace" / "batches"
MAX_BATCH = 10
ERR_PREFIX = ("ERR", "BAD", "HTTP", "NETWORK")

WRITE_TOOLS = {"publish_batch", "roam_ops", "library"}

# ---------------- schema ----------------

_SCHEMA = {
    "account_digest": ("账号态势一次拿全：登录状态、当前视频、AI 模式与开关、今日事件聚合、配置冲突体检。任务开始时先调它。", {
        "days": {"type": "integer", "description": "聚合最近几天，默认 1"}}, []),
    "search_videos": ("按关键词搜视频，返回精简列表（bvid/标题/UP/播放量）+ ref 句柄。", {
        "keyword": {"type": "string"},
        "limit": {"type": "integer", "description": "默认 8，最多 20"},
        "page": {"type": "integer", "description": "默认 1"}}, ["keyword"]),
    "inspect_video": ("把目标视频的元信息/字幕/评论/弹幕一次取齐，落 ref，只回摘要。默认 bvid 取当前打开的视频。", {
        "bvid": {"type": "string"},
        "want": {"type": "array", "items": {"type": "string",
                 "enum": ["meta", "subtitle", "comments", "danmaku"]},
                 "description": "要哪些块，默认 meta+comments"},
        "limit": {"type": "integer", "description": "评论/弹幕条数，默认 20"}}, []),
    "read_ref": ("分页读回 ref 内容（展平后按行），支持 grep 过滤。上下文不够时才调。", {
        "ref": {"type": "string"},
        "offset": {"type": "integer"},
        "limit": {"type": "integer", "description": "默认 120 行，最多 400"},
        "grep": {"type": "string", "description": "只保留包含该词的行"}}, ["ref"]),
    "inbox_brief": ("收件箱摘要：谁回复/@了我、未回复多少，正文落 ref。", {
        "kind": {"type": "string", "description": "all/reply/at，默认 all"},
        "limit": {"type": "integer", "description": "默认 20"},
        "only_unreplied": {"type": "boolean", "description": "默认 true"}}, []),
    "draft_batch": ("把一批待发布内容写成草稿批次，落盘待批。写操作前必做的一步，调用本身不对外发布。", {
        "items": {"type": "array", "items": {"type": "object", "properties": {
            "type": {"type": "string", "enum": ["reply", "comment", "danmaku"]},
            "msg_id": {"type": "string", "description": "reply 必填，来自 inbox_brief"},
            "bvid": {"type": "string", "description": "comment/danmaku 可选，默认当前视频"},
            "text": {"type": "string", "description": "正文，comment<=1000，danmaku<=100"}},
            "required": ["type", "text"]}},
        "note": {"type": "string", "description": "批次说明"}}, ["items"]),
    "publish_batch": ("发布草稿批次。对外写操作，系统会先让用户确认；逐条执行并逐条自证。", {
        "batch_ref": {"type": "string", "description": "draft_batch 返回的 ref"},
        "limit": {"type": "integer", "description": "本次最多发几条，默认 5、上限 10"}},
        ["batch_ref"]),
    "roam_ops": ("巡览一体化操作：看状态/列任务/取下一个/生成草稿/改草稿/发送/跳过/开关。", {
        "action": {"type": "string", "enum": ["status", "list", "next", "draft", "draft_set",
                                             "send", "skip", "check", "on", "off"]},
        "task_id": {"type": "string"},
        "text": {"type": "string", "description": "draft_set 用"},
        "limit": {"type": "integer"}}, ["action"]),
    "verify": ("用真实回执核对最近的操作到底成没成（查事件流，不靠模型自述）。", {
        "minutes": {"type": "integer", "description": "回看多少分钟，默认 30"},
        "action": {"type": "string", "description": "只看某类动作，如 publish/comment"}}, []),
    "library": ("本地资料库：把结论/素材按主题存档，或按关键词检索。", {
        "action": {"type": "string", "enum": ["put", "get", "list", "del"]},
        "key": {"type": "string", "description": "主题键"},
        "value": {"type": "string", "description": "put 时的内容"},
        "keyword": {"type": "string", "description": "list 过滤"}}, ["action"]),
    "memory": ("跨会话记忆：记住用户偏好/长期结论，后续任务自动可用。", {
        "op": {"type": "string", "enum": ["put", "get", "list", "del"]},
        "key": {"type": "string"},
        "value": {"type": "string"}}, ["op"]),
    "plan_update": ("长任务的计划板：更新某一步的状态，供断点续跑与控制台看板。", {
        "plan_id": {"type": "string"},
        "step_id": {"type": "string"},
        "status": {"type": "string", "enum": ["pending", "running", "done", "failed", "skipped"]},
        "note": {"type": "string"}}, ["plan_id", "step_id", "status"]),
}

TOOL_NAMES = list(_SCHEMA)
WRITE_NAMES = {"publish_batch", "roam_ops", "library"}


def schema() -> list[dict]:
    out = []
    for name, (desc, props, req) in _SCHEMA.items():
        out.append({"type": "function", "function": {
            "name": name, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": req}}})
    return out


# ---------------- 通用 ----------------

def _ok_text(t: str) -> bool:
    return not str(t or "").lstrip().startswith(ERR_PREFIX)


def _lines(text: str) -> list[str]:
    return [x.rstrip() for x in str(text or "").splitlines() if x.strip()]


async def _call(core, name: str, args: dict | None = None) -> tuple[bool, str]:
    try:
        out = await core.execute(name, dict(args or {}), actor="ai")
    except Exception as exc:                     # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    t = str(out or "").strip()
    return _ok_text(t), (t or "（无输出）")


def _env(tool: str, *, ok: bool, summary: str, ref: str = "", count: int = 0,
         fail: int = 0, items: list | None = None, extra: dict | None = None,
         checks: list | None = None, ms: int = 0, err: str = "") -> dict:
    ch = checks or []
    verdict = "pass"
    # 硬检查不过 -> fail；只有软检查（提示性、非致命）不过才降级为 warn
    if not ok or any(c.get("ok") is False and not c.get("soft") for c in ch):
        verdict = "fail"
    elif any(c.get("ok") is False and c.get("soft") for c in ch):
        verdict = "warn"
    if err == "NEED_CONFIRM":
        verdict = "warn"          # 等用户确认是正常中间态，不是执行失败
    out = {"ok": ok, "tool": tool, "summary": summary[:1400],
           "ref": ref or None, "count": count, "fail": fail,
           "items": (items or [])[:20], "verify": {"verdict": verdict, "checks": ch},
           "ms": ms}
    if extra:
        out.update(extra)
    if err:
        out["err"] = err
    return out


# ---------------- 各工具实现 ----------------

async def account_digest(core, store, args: dict) -> dict:
    t0 = time.time()
    days = max(1, min(int(args.get("days") or 1), 30))
    ok_me, me = await _call(core, "me")
    ok_st, st = await _call(core, "state")
    ok_llm, llm = await _call(core, "llm")
    try:
        snap = store.snapshot()
    except Exception:                            # noqa: BLE001
        snap = {}
    roam = (snap.get("roam") or {}) if isinstance(snap, dict) else {}
    agg = events.summary(days=days)

    # 配置冲突体检：llm.auto_publish 与 roam.llm_publish 两处开关
    conflicts: list[str] = []
    try:
        from . import modes as _m
        cur_mode = _m.current()
    except Exception:                            # noqa: BLE001
        cur_mode = ""
    auto_pub = bool(llm.get("auto_publish")) if isinstance(llm, dict) else False
    if "auto_publish" in llm and roam:
        if auto_pub and not roam.get("llm_publish") and roam.get("llm_draft"):
            conflicts.append("llm.auto_publish=true 但 roam.llm_publish=false：巡览只生成草稿不发布")
    if roam.get("draft_failed"):
        conflicts.append(f"巡览草稿失败 {roam.get('draft_failed')} 次（err 只留最后一条，需要看 events 归因）")

    data = {"me": me, "state": st, "llm": llm, "roam": roam,
            "events": agg, "mode": cur_mode, "conflicts": conflicts, "at": time.time()}
    ref = refstore.put("digest", data, key=f"digest|{time.strftime('%Y-%m-%d-%H')}")
    summary = (
        f"账号：{(me.splitlines() or [''])[0] if ok_me else '读取失败'}\n"
        f"状态：{(st.splitlines() or [''])[0] if ok_st else '读取失败'}\n"
        f"当前视频：{json.dumps(snap.get('current') or {}, ensure_ascii=False)[:160]}\n"
        f"AI 模式：{cur_mode or '未知'}\n"
        f"近{days}天事件：{agg['total']} 条，失败 {agg['fail']} 条，token {agg['tokens']}\n"
        f"动作分布：{json.dumps(agg['actions'], ensure_ascii=False)[:300]}\n"
        f"巡览：{json.dumps(roam, ensure_ascii=False)[:300]}\n"
        f"冲突体检：{('；'.join(conflicts) or '无')}\n"
        f"明细：read_ref {ref}"
    )
    checks = [{"name": "account_loaded", "ok": bool(ok_me), "detail": me.splitlines()[0] if me else ""},
              {"name": "no_config_conflict", "ok": not conflicts, "soft": True,
               "detail": "；".join(conflicts) or "两处开关一致"}]
    return _env("account_digest", ok=ok_me and ok_st, summary=summary, ref=ref,
                count=agg["total"], fail=agg["fail"], checks=checks,
                extra={"events": agg, "conflicts": conflicts, "mode": cur_mode},
                ms=int((time.time() - t0) * 1000))


async def search_videos(core, store, args: dict) -> dict:
    t0 = time.time()
    kw = str(args.get("keyword") or "").strip()
    if not kw:
        return _env("search_videos", ok=False, summary="", err="keyword 必填")
    limit = max(1, min(int(args.get("limit") or 8), 20))
    page = max(1, int(args.get("page") or 1))
    ok, text = await _call(core, "search", {"keyword": kw, "limit": limit, "page": page})
    rows = [ln for ln in _lines(text) if re.search(r"BV[0-9A-Za-z]{10}", ln)]
    ref = refstore.put("search", {"keyword": kw, "page": page, "text": text},
                       key=f"search|{kw}|{page}|{time.strftime('%Y-%m-%d')}")
    summary = "\n".join(rows[:limit]) or text[:600]
    checks = [{"name": "has_result", "ok": bool(rows), "detail": f"{len(rows)} 条命中"}]
    return _env("search_videos", ok=ok and bool(rows), count=len(rows), summary=summary,
                ref=ref, items=rows[:10], checks=checks,
                err="" if ok else text[:200], ms=int((time.time() - t0) * 1000))


async def inspect_video(core, store, args: dict) -> dict:
    t0 = time.time()
    bvid = str(args.get("bvid") or "").strip()
    want = args.get("want") or ["meta", "comments"]
    want = [w for w in want if w in ("meta", "subtitle", "comments", "danmaku")] or ["meta"]
    limit = max(1, min(int(args.get("limit") or 20), 50))
    blocks: dict[str, Any] = {}
    checks: list[dict] = []

    if bvid:
        ok, out = await _call(core, "open_video", {"bvid": bvid})
        checks.append({"name": "open_video", "ok": ok, "detail": (out.splitlines() or [""])[0][:120]})
        if not ok:
            return _env("inspect_video", ok=False, summary="", err=out[:200], checks=checks)
    else:
        bvid = ""
    blocks["meta"] = await _call(core, "state")

    if "subtitle" in want:
        ok1, tracks = await _call(core, "subtitles")
        blocks["subtitles"] = tracks
        ok2, sub = await _call(core, "subtitle", {})
        blocks["subtitle_text"] = sub
        checks.append({"name": "subtitle", "ok": ok2 and len(sub) > 40, "soft": True,
                       "detail": f"{len(sub or '')} 字"})
    if "comments" in want:
        okc, cm = await _call(core, "comments", {"bvid": bvid, "page": 1, "limit": limit})
        blocks["comments"] = cm
        n = len([x for x in _lines(cm) if len(x) > 8])
        checks.append({"name": "comments", "ok": okc, "soft": True, "detail": f"{n} 行"})
    if "danmaku" in want:
        okd, dm = await _call(core, "danmaku_list", {"bvid": bvid, "page": 1})
        blocks["danmaku"] = dm
        checks.append({"name": "danmaku", "ok": okd, "soft": True,
                       "detail": f"{len(dm or '')} 字"})

    ref = refstore.put("video", {"bvid": bvid, "blocks": blocks},
                       key=f"video|{bvid or time.strftime('%H%M')}|{'-'.join(want)}|{limit}")
    head = (blocks.get("meta") or ("", ""))[1]
    summ = [f"目标：{bvid or '当前视频'}",
            f"状态：{(head.splitlines() or [''])[0][:200]}"]
    if "comments" in want:
        summ.append("评论摘要：\n" + "\n".join(_lines(blocks.get("comments", ""))[:8]))
    if "subtitle" in want:
        summ.append("字幕摘要：" + (blocks.get("subtitle_text") or "")[:400].replace("\n", " "))
    summ.append(f"全文：read_ref {ref}")
    core_ok = all(c.get("ok") for c in checks if not c.get("soft"))
    return _env("inspect_video", ok=core_ok, summary="\n".join(summ), ref=ref,
                count=sum(1 for c in checks if c.get("ok")), checks=checks,
                ms=int((time.time() - t0) * 1000))


async def read_ref(core, store, args: dict) -> dict:
    res = refstore.read(str(args.get("ref") or ""), offset=int(args.get("offset") or 0),
                        limit=int(args.get("limit") or 120), grep=str(args.get("grep") or ""))
    if not res.get("ok"):
        return _env("read_ref", ok=False, summary="", err=res.get("err"),
                    extra={"hint": res.get("hint")})
    body = "\n".join(res["lines"])
    return _env("read_ref", ok=True, count=res["returned"], summary=body or "（空）",
                extra={"ref": res["ref"], "total_lines": res["total_lines"],
                       "next_offset": res["next_offset"]})


async def inbox_brief(core, store, args: dict) -> dict:
    t0 = time.time()
    kind = str(args.get("kind") or "all")
    limit = max(1, min(int(args.get("limit") or 20), 50))
    only = bool(args.get("only_unreplied", True))
    ok, text = await _call(core, "inbox", {"kind": kind, "limit": limit, "only_unreplied": only})
    items = []
    try:
        with store.lock:
            items = list(store.inbox.get("items") or [])
    except Exception:                            # noqa: BLE001
        items = []
    slim = [{"msg_id": str(m.get("msg_id") or ""), "user": str(m.get("uname") or m.get("user") or ""),
             "text": str(m.get("message") or m.get("content") or "")[:120],
             "replied": bool(m.get("replied"))} for m in items[:limit]]
    ref = refstore.put("inbox", {"raw": text, "items": items},
                       key=f"inbox|{kind}|{only}|{time.strftime('%Y-%m-%d-%H')}")
    lines = [f"inbox {len(slim)} 条（kind={kind}, only_unreplied={only}）"]
    for m in slim[:10]:
        lines.append(f"- {m['msg_id']} @{m['user']}: {m['text']}")
    lines.append(f"全文：read_ref {ref}")
    checks = [{"name": "inbox_loaded", "ok": ok, "detail": (text.splitlines() or [""])[0][:120]}]
    if not slim:
        checks.append({"name": "has_unreplied", "ok": False, "soft": True, "detail": "没有未回复消息"})
    return _env("inbox_brief", ok=ok, summary="\n".join(lines), ref=ref, count=len(slim),
                items=slim[:10], checks=checks, ms=int((time.time() - t0) * 1000))


async def draft_batch(core, store, args: dict) -> dict:
    t0 = time.time()
    raw = args.get("items") or []
    if not isinstance(raw, list) or not raw:
        return _env("draft_batch", ok=False, summary="", err="items 不能为空")
    clean, bad = [], []
    seen = set()
    for it in raw[:50]:
        if not isinstance(it, dict):
            continue
        typ = str(it.get("type") or "")
        text = str(it.get("text") or "").strip()
        if typ not in ("comment", "reply", "danmaku"):
            bad.append({"type": typ, "reason": "未知类型"}); continue
        if not text:
            bad.append({"type": typ, "reason": "正文为空"}); continue
        if len(text) > (100 if typ == "danmaku" else 1000):
            bad.append({"type": typ, "reason": "超长"}); continue
        sig = f"{typ}|{it.get('msg_id','')}|{it.get('bvid','')}|{text}"
        if sig in seen:
            bad.append({"type": typ, "reason": "重复"}); continue
        seen.add(sig)
        clean.append({"type": typ, "text": text,
                      "msg_id": str(it.get("msg_id") or ""),
                      "bvid": str(it.get("bvid") or "")})
    if not clean:
        return _env("draft_batch", ok=False, summary="", err="没有合法条目", items=bad)
    bid = f"b_{time.strftime('%Y%m%d_%H%M%S')}"
    rec = {"batch_id": bid, "note": str(args.get("note") or ""),
           "created": round(time.time(), 3), "items": clean}
    ref = refstore.put("batch", rec, key=f"batch|{bid}")
    try:
        BATCH_DIR.mkdir(parents=True, exist_ok=True)
        (BATCH_DIR / f"{bid}.json").write_text(
            json.dumps({**rec, "ref": ref}, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:                            # noqa: BLE001
        pass
    events.emit("draft_batch", target=bid, ok=True, refs=[ref],
                n=len(clean), rejected=len(bad))
    lines = [f"批次 {bid}：{len(clean)} 条待发布，{len(bad)} 条被拦"]
    for c in clean[:MAX_BATCH]:
        lines.append(f"- {c['type']} {c['msg_id'] or c['bvid'] or '(当前视频)'}: {c['text'][:40]}")
    if bad:
        lines.append("被拦：" + json.dumps(bad[:5], ensure_ascii=False))
    lines.append(f"发布时用 publish_batch(batch_ref=\"{ref}\")")
    checks = [{"name": "batch_created", "ok": True, "detail": f"{len(clean)} 条"},
              {"name": "no_reject", "ok": not bad, "soft": True, "detail": f"拦截 {len(bad)} 条"}]
    return _env("draft_batch", ok=True, summary="\n".join(lines), ref=ref,
                count=len(clean), fail=len(bad), items=clean[:MAX_BATCH],
                checks=checks, extra={"batch_id": bid}, ms=int((time.time() - t0) * 1000))


async def publish_batch(core, store, args: dict, *, confirmed: bool = False) -> dict:
    t0 = time.time()
    if not confirmed:
        return _env("publish_batch", ok=False,
                    summary="待用户确认：这一步会真的对外发布，未确认不执行。",
                    err="NEED_CONFIRM")
    ref = str(args.get("batch_ref") or "")
    blob = refstore.load(ref)
    if not blob or blob.get("kind") != "batch":
        return _env("publish_batch", ok=False, summary="", err="BATCH_NOT_FOUND")
    items = list((blob.get("data") or {}).get("items") or [])
    cap = max(1, min(int(args.get("limit") or 5), MAX_BATCH))
    todo, results = items[:cap], []
    for it in todo:
        typ, text = it.get("type"), it.get("text")
        tid = it.get("msg_id") or it.get("bvid") or ""
        if typ == "reply":
            ok, out = await _call(core, "reply", {"msg_id": it.get("msg_id"), "message": text})
        elif typ == "comment":
            ok, out = await _call(core, "comment", {"bvid": it.get("bvid") or "",
                                                    "message": text})
        else:
            ok, out = await _call(core, "danmaku", {"bvid": it.get("bvid") or "",
                                                    "message": text})
        first = (out.splitlines() or [""])[0][:200]
        results.append({"type": typ, "target": tid, "ok": ok, "receipt": first})
        events.emit("publish", target=f"{typ}:{tid}", ok=ok, ms=0,
                    text_len=len(text or ""), receipt=first)
        await asyncio.sleep(1.2)                 # 串行 + 间隔，别撞风控

    okn = sum(1 for r in results if r["ok"])
    bad = [r for r in results if not r["ok"]]
    remain = len(items) - len(todo)
    lines = [f"发布完成：成功 {okn}/{len(todo)}" + (f"，剩余 {remain} 条未发" if remain > 0 else "")]
    for r in results:
        lines.append(f"- {r['type']} {r['target']}: {'OK' if r['ok'] else 'FAIL'} {r['receipt']}")
    checks = [{"name": "all_sent", "ok": not bad, "detail": f"失败 {len(bad)} 条"},
              {"name": "has_receipt", "ok": all(r["receipt"] for r in results if r["ok"]),
               "soft": True, "detail": "每条都有平台回执"}]
    return _env("publish_batch", ok=okn > 0, summary="\n".join(lines), count=okn,
                fail=len(bad), items=results, checks=checks,
                extra={"remaining": remain}, ms=int((time.time() - t0) * 1000))


async def roam_ops(core, store, args: dict, *, confirmed: bool = False) -> dict:
    t0 = time.time()
    act = str(args.get("action") or "status")
    w = {"draft_set": {"task_id": str(args.get("task_id") or ""),
                       "text": str(args.get("text") or "")},
         "send": {"task_id": str(args.get("task_id") or "")},
         "skip": {"task_id": str(args.get("task_id") or "")},
         "next": {"task_id": str(args.get("task_id") or "")},
         "draft": {"task_id": str(args.get("task_id") or "")},
         "check": {"count": int(args.get("limit") or 1)},
         "on": {"on": True}, "off": {"on": False}}
    if act == "status":
        ok1, rl = await _call(core, "roam")
        ok2, lst = await _call(core, "roam_list", {"limit": int(args.get("limit") or 10)})
        try:
            snap_roam = dict((store.snapshot() or {}).get("roam") or {})
        except Exception:                        # noqa: BLE001
            snap_roam = {}
        ref = refstore.put("roam", {"roam": rl, "list": lst, "snap": snap_roam},
                           key=f"roamstatus|{time.strftime('%Y-%m-%d-%H')}")
        done = int(snap_roam.get("drafted") or 0)
        failed = int(snap_roam.get("draft_failed") or 0)
        summ = (f"巡览快照：{json.dumps(snap_roam, ensure_ascii=False)[:400]}\n"
                f"草稿 {done} / 失败 {failed}\n任务列表：\n" +
                "\n".join(_lines(lst)[:10]) + f"\n全文：read_ref {ref}")
        checks = [{"name": "roam_loaded", "ok": ok1 and ok2, "detail": (rl.splitlines() or [""])[0][:120]},
                  {"name": "draft_fail_ratio", "ok": failed <= max(1, done // 3), "soft": True,
                   "detail": f"{failed}/{done + failed} 失败"}]
        return _env("roam_ops", ok=ok1 and ok2, summary=summ, ref=ref,
                    count=done, fail=failed, checks=checks, ms=int((time.time() - t0) * 1000))

    if act == "list":
        ok, out = await _call(core, "roam_list", {"status": str(args.get("text") or ""),
                                                  "limit": int(args.get("limit") or 20)})
        return _env("roam_ops", ok=ok, summary=out[:1200], count=len(_lines(out)),
                    checks=[{"name": "list", "ok": ok, "detail": (out.splitlines() or [""])[0][:100]}],
                    ms=int((time.time() - t0) * 1000))

    if act in ("send", "on", "off") and not confirmed:
        return _env("roam_ops", ok=False, summary=f"待确认：{act} 属于对外动作。",
                    err="NEED_CONFIRM")

    cmd = {"next": "roam_next", "draft": "roam_draft", "draft_set": "roam_draft_set",
           "send": "roam_send", "skip": "roam_skip", "check": "roam_check",
           "on": "roam", "off": "roam"}.get(act)
    if not cmd:
        return _env("roam_ops", ok=False, summary="", err=f"不支持的 action: {act}")
    ok, out = await _call(core, cmd, w.get(act) or {})
    events.emit("roam_ops", target=act, ok=ok, detail={"out": out[:200]})
    checks = [{"name": f"roam_{act}", "ok": ok, "detail": (out.splitlines() or [""])[0][:160]}]
    return _env("roam_ops", ok=ok, summary=out[:1200], checks=checks,
                ms=int((time.time() - t0) * 1000))


async def verify(core, store, args: dict) -> dict:
    mins = max(1, min(int(args.get("minutes") or 30), 1440))
    act = str(args.get("action") or "")
    rows = events.query(limit=200)
    cutoff = time.time() - mins * 60
    rows = [r for r in rows if float(r.get("ts") or 0) >= cutoff]
    if act:
        rows = [r for r in rows if act in str(r.get("action") or "")]
    writes = [r for r in rows if r.get("action") in ("publish", "roam_ops", "comment", "reply", "danmaku")]
    bad = [r for r in writes if r.get("ok") == 0]
    lines = [f"近 {mins} 分钟：事件 {len(rows)} 条，写操作 {len(writes)} 条，失败 {len(bad)} 条"]
    for r in writes[:12]:
        lines.append(f"- [{r.get('action')}] {r.get('target')} -> {'OK' if r.get('ok') else 'FAIL'}"
                     f" {(r.get('detail') or {}).get('receipt', '') if isinstance(r.get('detail'), dict) else ''}")
    checks = [{"name": "no_fail", "ok": not bad, "detail": f"失败 {len(bad)} 条"}]
    return _env("verify", ok=True, summary="\n".join(lines), count=len(writes), fail=len(bad),
                items=[{k: r.get(k) for k in ("action", "target", "ok", "ts")} for r in writes[:10]],
                checks=checks)


# ---- 本地库 / 记忆 / 计划板（纯本地 json，不碰 core）----

_LIB = DATA / "library.json"
_MEM = DATA / "agent_memory.json"


def _read_json(fp: Path, default):
    try:
        return json.loads(fp.read_text(encoding="utf-8")) or default
    except Exception:                            # noqa: BLE001
        return default


def _write_json(fp: Path, obj) -> None:
    fp.parent.mkdir(parents=True, exist_ok=True)
    tmp = fp.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(fp)


async def library(core, store, args: dict, *, confirmed: bool = False) -> dict:
    act = str(args.get("action") or "list")
    lib = _read_json(_LIB, {})
    key = str(args.get("key") or "").strip()
    if act in ("put", "del") and not confirmed:
        return _env("library", ok=False,
                    summary=f"待确认：{act} {key or ''}".strip(), err="NEED_CONFIRM")
    if act == "put":
        val = str(args.get("value") or "")
        if not key or not val:
            return _env("library", ok=False, summary="", err="key / value 必填")
        lib[key] = {"value": val, "at": round(time.time(), 3)}
        _write_json(_LIB, lib)
        return _env("library", ok=True, summary=f"已存 {key}（{len(val)} 字），库内共 {len(lib)} 条")
    if act == "get":
        hit = lib.get(key)
        if not hit:
            ks = [k for k in lib if key and key in k][:5]
            return _env("library", ok=False, summary=f"没有 {key}" + (f"，相近：{ks}" if ks else ""),
                        err="NOT_FOUND")
        return _env("library", ok=True, summary=str(hit.get("value"))[:1200])
    if act == "del":
        n = 1 if lib.pop(key, None) else 0
        _write_json(_LIB, lib)
        return _env("library", ok=True, summary=f"删除 {n} 条，剩余 {len(lib)}")
    kw = str(args.get("keyword") or "").lower()
    rows = [(k, v) for k, v in lib.items() if not kw or kw in k.lower()
            or kw in str(v.get("value", "")).lower()]
    return _env("library", ok=True, count=len(rows),
                summary="\n".join(f"- {k}（{len(str(v.get('value')))} 字）" for k, v in rows[:20]) or "库为空")


async def memory(core, store, args: dict) -> dict:
    op = str(args.get("op") or "list")
    mem = _read_json(_MEM, {})
    key = str(args.get("key") or "").strip()
    if op == "put":
        val = str(args.get("value") or "")
        if not key or not val:
            return _env("memory", ok=False, summary="", err="key / value 必填")
        mem[key] = {"value": val, "at": round(time.time(), 3)}
        _write_json(_MEM, mem)
        return _env("memory", ok=True, summary=f"记住 {key}，共 {len(mem)} 条")
    if op == "get":
        hit = mem.get(key)
        return _env("memory", ok=bool(hit), summary=str((hit or {}).get("value") or "没有这条记忆")[:1200])
    if op == "del":
        n = 1 if mem.pop(key, None) else 0
        _write_json(_MEM, mem)
        return _env("memory", ok=True, summary=f"删除 {n} 条")
    return _env("memory", ok=True, count=len(mem),
                summary="\n".join(f"- {k}: {str(v.get('value'))[:60]}" for k, v in mem.items()) or "记忆为空")


async def plan_update(core, store, args: dict) -> dict:
    from . import orchestrator
    pid = str(args.get("plan_id") or "")
    res = orchestrator.update_step(pid, str(args.get("step_id") or ""),
                                   str(args.get("status") or "done"),
                                   note=str(args.get("note") or ""))
    return _env("plan_update", ok=bool(res.get("ok")), summary=json.dumps(res, ensure_ascii=False)[:800],
                err="" if res.get("ok") else res.get("err", ""))


# ---------------- 统一入口 ----------------

_HANDLERS = {
    "account_digest": account_digest, "search_videos": search_videos,
    "inspect_video": inspect_video, "read_ref": read_ref,
    "inbox_brief": inbox_brief, "draft_batch": draft_batch,
    "publish_batch": publish_batch, "roam_ops": roam_ops,
    "verify": verify, "library": library, "memory": memory,
    "plan_update": plan_update,
}


async def call(core, store, name: str, args: dict, *, confirmed: bool = False) -> dict:
    """调一个工具，返回 envelope。异常不外抛，转成 ok=False 的 envelope。"""
    fn = _HANDLERS.get(str(name))
    if not fn:
        return _env(str(name), ok=False, summary="", err=f"没有 {name} 这个工具")
    t0 = time.time()
    try:
        if name in ("publish_batch", "roam_ops", "library"):
            env = await fn(core, store, dict(args or {}), confirmed=confirmed)
        else:
            env = await fn(core, store, dict(args or {}))
    except Exception as exc:                     # noqa: BLE001
        return _env(str(name), ok=False, summary="", err=f"{type(exc).__name__}: {exc}"[:300],
                    ms=int((time.time() - t0) * 1000))
    if name not in ("read_ref", "verify"):
        # 待确认既非成功也非失败，记 None，别污染 fail 统计
        _ok = None if env.get("err") == "NEED_CONFIRM" else bool(env.get("ok"))
        events.emit(f"tool:{name}", target=str((args or {}).get("bvid")
                                              or (args or {}).get("keyword")
                                              or (args or {}).get("batch_ref") or "")[:100],
                    ok=_ok, refs=[env["ref"]] if env.get("ref") else [],
                    ms=env.get("ms") or int((time.time() - t0) * 1000),
                    verdict=(env.get("verify") or {}).get("verdict"))
    return env


def is_write(name: str) -> bool:
    return str(name) in WRITE_NAMES
