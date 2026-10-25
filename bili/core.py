"""命令总线：MCP / Web / CLI 三端共用同一个 execute"""

import asyncio
import time

from . import fmt
from .api import Bili
from .http import NeedLogin
from .store import store


class Core:
    def __init__(self) -> None:
        self.bili = Bili()
        # MCP 模式下后台跑在独立线程，跨 loop 调 API 会崩连接池，故仅只读观察
        self.readonly = False

    # ---------------- 命令 ----------------

    async def execute(self, name: str, args: dict) -> str:
        try:
            # 后台扫码登录会改写 .data/cookie.json，跑命令前热重载一次
            self.bili.http.sync_from_disk()
            out = await getattr(self, f"cmd_{name}")(**args)
            store.note(name, True, out.splitlines()[0] if out else "")
            return out
        except TypeError as e:
            msg = f"BAD_ARGS: {e}"
            store.note(name, False, msg)
            return msg
        except NeedLogin as e:
            msg = f"ERR_NEED_LOGIN: {e.msg}（后台扫码登录，或 python main.py login）"
            store.note(name, False, msg)
            return msg
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            store.note(name, False, msg)
            return msg

    async def cmd_search(self, keyword: str, page: int = 1, limit: int = 10) -> str:
        r = await self.bili.search(keyword, page, limit)
        with store.lock:
            store.view, store.query, store.page = "search", keyword, page
            store.total, store.items = r["total"], r["items"]
        return fmt.search(r)

    async def cmd_open_video(self, bvid: str) -> str:
        d = await self.bili.view(bvid)
        with store.lock:
            store.view, store.current = "video", d
            store.player.bvid = d["bvid"]
            store.player.title = d["title"]
            store.player.dur = d["dur"]
            store.player.base = 0.0
            store.player.playing = False
            store.comments, store.comments_meta = [], {}
            store.subs = {"bvid": "", "lan": "", "lan_doc": "", "tracks": [],
                          "n": 0, "chars": 0, "saved": ""}
            store.inter = {"liked": None, "coins": None, "favoured": None}
        await self._refresh_inter()
        return "\n".join([fmt.detail(d), fmt.inter(store.inter), self._actions()])

    async def cmd_comments(self, bvid: str = "", page: int = 1,
                           limit: int = 20, mode: int = 3) -> str:
        cur = await self._target(bvid)
        if not cur:
            return "ERR_NO_TARGET: 先 open_video 或传 bvid"
        r = await self.bili.comments(cur["avid"], cur["bvid"], page, limit, mode)
        with store.lock:
            store.comments = r["items"]
            store.comments_meta = {"bvid": r["bvid"], "page": page,
                                   "total": r["total"], "source": r["source"],
                                   "is_end": r["is_end"]}
        return fmt.comments(r)

    # ---------------- 字幕 ----------------

    async def cmd_subtitles(self, bvid: str = "", page: int = 1) -> str:
        cur = await self._target(bvid)
        if not cur:
            return "ERR_NO_TARGET: 先 open_video 或传 bvid"
        r = await self.bili.subtitle_tracks(cur["bvid"], page)
        with store.lock:
            store.subs = {"bvid": r["bvid"], "lan": "", "lan_doc": "",
                          "tracks": [{"lan": t["lan"], "lan_doc": t["lan_doc"],
                                      "lock": t["lock"]} for t in r["tracks"]],
                          "n": 0, "chars": 0, "saved": ""}
        return fmt.subtitle_tracks(r)

    async def cmd_subtitle(self, bvid: str = "", lang: str = "", page: int = 1,
                           offset: int = 0, limit: int = 200,
                           export: str = "") -> str:
        """读字幕正文。offset/limit 按「句」分页（给 AI 逐段读），export 落盘 srt/txt/md。"""
        cur = await self._target(bvid)
        if not cur:
            return "ERR_NO_TARGET: 先 open_video 或传 bvid"
        r = await self.bili.subtitle_cues(cur["bvid"], lang, page)
        n = len(r["cues"])
        chars = sum(len(c["text"]) for c in r["cues"])
        with store.lock:
            store.subs = {"bvid": r["bvid"], "lan": r["lan"], "lan_doc": r["lan_doc"],
                          "tracks": r["tracks"], "n": n, "chars": chars,
                          "saved": store.subs.get("saved") or ""}
        head = fmt.subtitle_head(r)
        if export:
            kind = str(export).strip().lower()
            renders = {"srt": fmt.subtitle_srt, "txt": fmt.subtitle_txt,
                       "md": fmt.subtitle_md}
            if kind not in renders:
                return f"ERR_BAD_EXPORT: {export}  可选 srt/txt/md"
            body = renders[kind](r)
            path = self._sub_dir() / f'{r["bvid"]}_{r["lan"]}.{kind}'
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
            with store.lock:
                store.subs["saved"] = str(path)
            return (f"{head}\nok subtitle_saved  export={kind}  path={path}\n"
                    f'hint: 需要正文时用 [subtitle --offset N --limit 200] 分段读取')
        limit = max(1, min(500, int(limit)))
        return fmt.subtitle_page(r, max(0, int(offset)), limit)

    @staticmethod
    def _sub_dir():
        from pathlib import Path
        return Path(__file__).resolve().parent.parent / ".data" / "subs"

    async def cmd_comment(self, bvid: str = "", message: str = "",
                          root: int | str = 0, parent: int | str = 0,
                          type: int = 1) -> str:
        cur = await self._target(bvid)
        if not cur:
            return "ERR_NO_TARGET"
        r = await self.bili.comment_add(cur["avid"], cur["bvid"], message, root, parent, type)
        line = f'ok comment  rpid={r["rpid"] or "-"}  {cur["bvid"]}'
        try:                                     # 顺带刷新评论区首屏，便于确认已上墙
            await self.cmd_comments(cur["bvid"], 1, 20, 2)
        except Exception:
            pass
        return f'{line}\nmsg: {r["message"]}'

    async def cmd_like(self, bvid: str = "", on: bool = True) -> str:
        cur = await self._target(bvid)
        if not cur:
            return "ERR_NO_TARGET"
        await self.bili.like(cur["avid"], cur["bvid"], on)
        await self._refresh_inter()
        return f'ok like={"on" if on else "off"}  {cur["bvid"]}\n{fmt.inter(store.inter)}'

    async def cmd_coin(self, bvid: str = "", n: int = 1,
                       also_like: bool = False) -> str:
        cur = await self._target(bvid)
        if not cur:
            return "ERR_NO_TARGET"
        r = await self.bili.coin(cur["avid"], cur["bvid"], n, also_like)
        await self._refresh_inter()
        return (f'ok coin={r["coins"]} also_like={int(bool(r["also_like"]))}  '
                f'{cur["bvid"]}\n{fmt.inter(store.inter)}')

    async def cmd_fav(self, bvid: str = "", folder: str = "") -> str:
        cur = await self._target(bvid)
        if not cur:
            return "ERR_NO_TARGET"
        fs = await self.bili.fav_folders()
        if not fs:
            return "ERR_NO_FOLDER: 账号下没有收藏夹"
        target = None
        if folder:
            key = str(folder).strip()
            target = next((f for f in fs if str(f["id"]) == key or f["title"] == key), None)
            if not target:
                names = ", ".join(f'{f["id"]}:{f["title"]}' for f in fs)
                return f"ERR_BAD_FOLDER: {folder}  可选 [{names}]"
        else:
            target = fs[0]
        await self.bili.fav_deal(cur["avid"], cur["bvid"], target["id"], True)
        await self._refresh_inter()
        return (f'ok fav={target["title"]}(id={target["id"]})  {cur["bvid"]}\n'
                f'{fmt.inter(store.inter)}')

    async def cmd_unfav(self, bvid: str = "", folder: str = "") -> str:
        cur = await self._target(bvid)
        if not cur:
            return "ERR_NO_TARGET"
        fs = await self.bili.fav_folders()
        key = str(folder).strip()
        targets = [f for f in fs if (not key or str(f["id"]) == key or f["title"] == key)]
        if not targets:
            return f"ERR_NO_FOLDER: {folder}"
        for f in targets:
            try:
                await self.bili.fav_deal(cur["avid"], cur["bvid"], f["id"], False)
            except Exception:                    # 不在该夹里会报错，忽略继续
                continue
        await self._refresh_inter()
        return f'ok unfav  {cur["bvid"]}\n{fmt.inter(store.inter)}'

    async def cmd_folders(self) -> str:
        fs = await self.bili.fav_folders()
        if not fs:
            return "folders: (空)"
        lines = [f"folders: {len(fs)}"] + [
            f'- id={f["id"]}  {f["title"]}  count={f["count"]}' for f in fs]
        return "\n".join(lines)

    # ---------------- 消息收件箱（回复我的 / @我的） ----------------

    @property
    def inbox(self):
        """按 Core 取收件箱视图；数据落 .data/inbox.json，跨端共享 store.inbox"""
        from .inbox import get_inbox

        return get_inbox(self)

    async def cmd_inbox(self, kind: str = "all", limit: int = 20,
                        only_unreplied: bool = True) -> str:
        """只做检测：列出别人回复我 / @我的评论消息，不回任何东西。

        回复文案由调用方（MCP 客户端 AI 或网页输入框）生成后交给 reply。
        """
        items = await self.inbox.list(kind, bool(only_unreplied), limit)
        with store.lock:
            stats = dict(store.inbox.get("stats") or {})
        head = (f'inbox: {len(items)} 条  kind={kind}  '
                f'only_unreplied={int(bool(only_unreplied))}  '
                f'fetched={stats.get("fetched") or 0}  '
                f'replied={stats.get("replied") or 0}')
        if stats.get("err"):
            head += f'  err={stats["err"][:120]}'
        if not items:
            return head + "\n(没有匹配的消息；文案由调用方生成，本工具不自动回复)"
        lines = [head]
        for m in items:
            src = ("视频 " + _bvid_of(m.get("uri") or "")) if m.get("type") == 1 else \
                  ("文章 " + str(m.get("uri") or "")) if m.get("type") == 12 else "动态"
            title = (m.get("title") or "")[:40]
            lines.append(f'- msg_id={m["msg_id"]}  [{m["kind"]}]  '
                         f'{m.get("uname") or "?"}({m.get("mid") or 0})  '
                         f'status={m.get("status") or "new"}  src={src}  {title}')
            lines.append(f'  他说: {(m.get("his_msg") or "(空)")[:160]}')
            if m.get("my_msg"):
                lines.append(f'  我: {m["my_msg"][:120]}')
            if m.get("status") == "replied":
                lines.append(f'  已回: {(m.get("replied_text") or "")[:120]}  '
                             f'rpid={m.get("replied_rpid") or "-"}')
        lines.append('actions: [reply --msg-id <id> --message "<AI 生成的文案>"]')
        return "\n".join(lines)

    async def cmd_reply(self, msg_id: str, message: str = "") -> str:
        """把调用方传入的文案作为评论回复发出；文案必填，项目内不生成任何文案。"""
        text = (message or "").strip()
        if not text:
            return "ERR_NEED_TEXT: message 必填（文案由调用方 AI 生成后传入）"
        if len(text) > 1000:
            return "ERR_TOO_LONG: 回复内容超过 1000 字上限"
        key = str(msg_id or "").strip()
        if not key:
            return "ERR_NEED_MSG_ID: 缺少 msg_id，先执行 inbox 查看"
        msg = None
        for m in await self.inbox.list("all", False, 100):
            if str(m.get("msg_id")) == key:
                msg = m
                break
        if not msg:                                   # 不在最近一页里，直连接口再找一次
            for m in await self.inbox.fetch("all"):
                if str(m.get("msg_id")) == key:
                    msg = m
                    break
        if not msg:
            return f"ERR_NO_MSG: msg_id={key} 不在当前消息列表（可能已过期）"
        r = await self.bili.comment_add(int(msg["oid"]), _bvid_of(msg.get("uri") or ""),
                                        text, msg.get("root") or 0,
                                        msg.get("parent") or 0, int(msg.get("type") or 1))
        self.inbox.mark_replied(key, text, r["rpid"])
        return (f'ok reply  rpid={r["rpid"] or "-"}  msg_id={key}  '
                f'type={msg.get("type")}  oid={msg.get("oid")}\n'
                f'to: {msg.get("uname") or "?"}  ({msg.get("kind")})\n'
                f'text: {text[:120]}')

    # ---------------- AI 巡览（自己看 B 站） ----------------

    @property
    def roam(self):
        """按 Core 取巡览器；状态落 .data/roam.json，跨端共享 store.roam"""
        from .roam import get_roam

        return get_roam(self)

    @property
    def llm(self):
        """外部大模型（OpenAI 兼容）；配置与用量落 .data/llm.json"""
        from .llm import get_llm

        return get_llm(self)

    async def cmd_roam(self, on=None, off=None, interval=0, max_per_day=-1,
                       source: str = "", keyword: str = "",
                       llm_draft=None, llm_publish=None) -> str:
        """配置巡览：开关 / 频率(秒,下限120) / 每日发布上限(0=不限) / 来源 / 关键词

        llm_draft / llm_publish：是否让内置外部大模型（llm.py）拟稿 / 拟完直接发
        """
        if off is not None and on is None:      # CLI 的 --off 无值解析为 True
            v = off
            flag = (v is True) or str(v).strip().lower() not in ("0", "false", "off", "no", "n", "否")
            on = not flag
        return self.roam.configure(on=on, interval=interval,
                                   max_per_day=max_per_day,
                                   source=source, keyword=keyword,
                                   llm_draft=llm_draft,
                                   llm_publish=llm_publish)

    async def cmd_roam_list(self, status: str = "", limit: int = 20) -> str:
        return self.roam.list_text(status, limit)

    async def cmd_roam_next(self, task_id: str = "") -> str:
        """取一个 waiting 任务：视频信息 + 字幕正文，供调用方 AI 阅读后写评论"""
        return self.roam.next_text(task_id)

    async def cmd_roam_comment(self, task_id: str = "", message: str = "") -> str:
        """回填文案并发布（文案由调用方传入，或先用 roam_draft 让大模型拟）"""
        r = await self.roam.publish(task_id, message, by="agent")
        if not r.get("ok"):
            return r["msg"]
        self.roam._sync()
        return f'{r["msg"]}\ntext: {str(message).strip()[:120]}'

    async def cmd_roam_draft(self, task_id: str = "", publish=None,
                             force=False) -> str:
        """让内置大模型（llm.py）按字幕写评论文案；publish=True 时拟完直接发

        force=True 才会覆盖已有草稿（含对话侧 Agent 写的），默认保留。
        """
        if publish is not None and not isinstance(publish, bool):
            s = str(publish).strip().lower()
            publish = s not in ("", "0", "false", "off", "no", "n", "否")
        if not isinstance(force, bool):
            force = str(force).strip().lower() not in ("", "0", "false", "off", "no", "n", "否")
        r = await self.roam.draft_one(task_id, publish=publish, force=force)
        head = (f'roam_draft: drafted={r.get("drafted", 0)} '
                f'published={r.get("published", 0)} failed={r.get("failed", 0)}')
        if r.get("skipped"):
            return f'{head}\nskipped: {r["skipped"]}'
        tail = ("\n草稿已存，可 roam_list 查看、roam_next 读全文、roam_send --task-id T 发出，"
                "或 roam_comment --task-id T --message \"改后文案\" 覆盖")
        return head + "\n" + "\n".join(r.get("msgs") or ["(无可拟稿任务)"]) + tail

    async def cmd_roam_draft_set(self, task_id: str = "", text: str = "",
                                 by: str = "agent") -> str:
        """把调用方（对话侧 Agent / 人工）写好的文案存成草稿，不发布

        与 llm 自动拟稿共用 draft 通道，靠 draft_by 标记来源，互不覆盖。
        """
        r = self.roam.set_draft(task_id, text, by=by)
        if not r.get("ok"):
            return r["msg"]
        return f'{r["msg"]}\n草稿：{str(text).strip()[:120]}\n下一步：roam_send --task-id {task_id} 发出'

    async def cmd_roam_send(self, task_id: str = "") -> str:
        """把已有 AI 草稿原样发出去（人工确认后的最后一跳）"""
        r = await self.roam.send_draft(task_id)
        if not r.get("ok"):
            return r["msg"]
        self.roam._sync()
        t = r.get("task") or {}
        return (f'{r["msg"]}\ntext: {str(t.get("text") or t.get("draft") or "")[:120]}')

    async def cmd_llm(self) -> str:
        """看外部大模型配置（不含 Key 明文）+ 巡览里 llm 的开关状态"""
        pub = self.llm.public()
        st = self.roam._load()
        stats = pub.get("stats") or {}
        from .llm import DEFAULT_ROAM_PROMPT

        custom = bool(pub.get("roam_prompt")) and \
            pub.get("roam_prompt") != DEFAULT_ROAM_PROMPT
        lines = [
            (f'llm: configured={int(pub.get("configured", False))}  '
             f'model={pub.get("model") or "-"}  base_url={pub.get("base_url") or "-"}'),
            (f'key: {pub.get("key_masked") or "(未填)"}  '
             f'auto_draft={int(bool(pub.get("auto_draft")))}  '
             f'auto_publish={int(bool(pub.get("auto_publish")))}  '
             f'max_per_day={pub.get("max_per_day")}'),
            (f'roam: llm_draft={int(st["llm_draft"])}  '
             f'llm_publish={int(st["llm_publish"])}  '
             f'roam_prompt={"自定义" if custom else "默认"}'),
            (f'usage: drafted_today={stats.get("drafted_today", 0)}  '
             f'sent_today={stats.get("sent_today", 0)}  '
             f'last_error={stats.get("last_error") or "-"}'),
            ("actions: [llm_configure --api-key sk-xxx] [llm_configure --model gpt-4o-mini] "
             "[llm_configure --roam-prompt \"...\"] [llm_test]"),
        ]
        return "\n".join(lines)

    async def cmd_llm_configure(self, api_key: str = "", base_url: str = "",
                                model: str = "", roam_prompt: str = "",
                                prompt: str = "", clear_key=None) -> str:
        """配外部大模型（Key 只落本地 .data/llm.json，不进 git）"""
        kw = {}
        if api_key:
            kw["api_key"] = api_key
        if base_url:
            kw["base_url"] = base_url
        if model:
            kw["model"] = model
        if roam_prompt:
            kw["roam_prompt"] = roam_prompt
        if prompt:
            kw["prompt"] = prompt
        if clear_key is not None:
            kw["clear_key"] = bool(clear_key)
        if not kw:
            return self.llm.public().__str__()
        msg = self.llm.configure(**kw)
        self.roam._sync()
        return msg

    async def cmd_llm_test(self) -> str:
        """真打一次上游接口，验证 Key / 地址 / 模型"""
        r = await asyncio.to_thread(self.llm.test)
        ok = r.get("ok")
        return (f'llm_test: ok={int(bool(ok))}  ms={r.get("ms")}  '
                f'model={r.get("model")}\n{(r.get("reply") or r.get("error") or "")[:200]}')

    async def cmd_roam_skip(self, task_id: str = "", reason: str = "manual") -> str:
        out = self.roam.skip(task_id, reason)
        self.roam._sync()
        return out

    async def cmd_roam_check(self, count: int = 1) -> str:
        """立刻跑一轮取片（只落任务，不发任何评论）"""
        self.roam._sync()
        s = await self.roam.check(count, force=True)
        head = (f'roam_check: tried={s["tried"]}  picked={s["picked"]}  '
                f'skipped={s["skipped"]}  err={s["err"] or "-"}')
        if s["note"]:
            head += f'\nnote: {s["note"]}'
        lines = [head]
        for tid in s["tasks"]:
            t = self.roam.take_next(tid)
            if t:
                lines.append(f'+ task={t["task_id"]}  {t["bvid"]}  '
                             f'lan={t["lan"]}  cues={t["n_cues"]}  chars={t["chars"]}  '
                             f'《{t["title"][:40]}》')
        lines.append(self.roam.list_text("", 5))
        lines.append("actions: [roam_next] "
                     '[roam_comment --task-id T --message "文案"] [roam_skip --task-id T]')
        return "\n".join(lines)

    async def cmd_me(self) -> str:
        from .auth import nav

        info = await nav(self.bili.http)
        with store.lock:
            store.account = {"login": bool(info.get("is_login")),
                             "uname": info.get("uname") or "",
                             "mid": info.get("mid") or 0,
                             "avatar": info.get("avatar") or ""}
        if not info.get("is_login"):
            return f'account: 未登录  {info.get("message") or ""}'.strip()
        return (f'account: 已登录  uname={info["uname"]}  mid={info["mid"]}  '
                f'vip={int(bool(info.get("vip")))}')

    async def cmd_state(self) -> str:
        with store.lock:
            snap = store.snapshot()
        lines: list[str] = []
        if snap["view"] == "search":
            lines.append(f'view=search  q="{snap["query"]}"  page={snap["page"]}  '
                         f'total={fmt.cnt(snap["total"])}')
            for i, it in enumerate(snap["items"], 1):
                lines.append(f'#{i}  {it["bvid"]}  {it["dur"]:>6}  play={fmt.cnt(it["play"]):>7}  '
                             f'up={it["up"]}  {it["title"][:48]}')
        elif snap["view"] == "video" and snap["current"]:
            lines.append(fmt.detail(snap["current"]))
        else:
            lines.append("view=idle  (无数据，先 search 或 open_video)")
        if snap["player"]["bvid"]:
            p = snap["player"]
            lines.append(f'player: {"playing" if p["playing"] else "paused"} '
                         f't={p["pos"]}/{p["dur"]} ({fmt.hms(p["pos"])})')
        subs = snap.get("subs") or {}
        if subs.get("bvid"):
            if subs.get("n"):
                lines.append(f'subs: lan={subs["lan"]} cues={subs["n"]} '
                             f'chars={subs["chars"]}')
            else:
                lines.append(f'subs: tracks={len(subs.get("tracks") or [])} (未取正文)')
        lines.append(self._actions(snap))
        return "\n".join(lines)

    async def cmd_play(self, bvid: str = "", at: float = 0.0) -> str:
        if bvid and (not store.player.bvid or store.player.bvid != bvid):
            await self.cmd_open_video(bvid)
        if not store.player.bvid:
            return "ERR_NO_TARGET: 先 open_video 或 play <bvid>"
        store.player.play(at)
        return self._player_line()

    async def cmd_pause(self) -> str:
        if not store.player.bvid:
            return "ERR_NO_TARGET"
        store.player.pause()
        return self._player_line()

    async def cmd_resume(self) -> str:
        if not store.player.bvid:
            return "ERR_NO_TARGET"
        store.player.play(store.player.pos())
        return self._player_line()

    async def cmd_seek(self, to: str) -> str:
        if not store.player.bvid:
            return "ERR_NO_TARGET"
        cur = store.player.pos()
        t = str(to).strip()
        target = cur + float(t[1:]) if t[:1] in "+-" else float(t)
        store.player.seek(target)
        return self._player_line()

    async def cmd_status(self) -> str:
        return self._player_line()

    # ---------------- 辅助 ----------------

    async def _target(self, bvid: str = "") -> dict | None:
        """取当前视频；传了别的 bvid 就先打开它"""
        cur = store.current
        if bvid and (not cur or cur["bvid"] != bvid):
            await self.cmd_open_video(bvid)
            cur = store.current
        return cur

    async def _refresh_inter(self) -> None:
        """刷新点赞/投币/收藏状态；未登录或出错则置 None（未知）"""
        cur = store.current
        if not cur:
            return
        aid, bvid = cur["avid"], cur["bvid"]
        try:
            like = await self.bili.like_state(aid, bvid)
            coin = await self.bili.coin_state(aid, bvid)
            fav = await self.bili.fav_state(aid, bvid)
        except NeedLogin:
            with store.lock:
                store.inter = {"liked": None, "coins": None, "favoured": None}
            return
        except Exception:                        # 网络抖动不影响主流程
            return
        with store.lock:
            store.inter = {"liked": like["liked"], "coins": coin["coins"],
                           "favoured": fav["favoured"]}

    # ---------------- 投稿（发视频） ----------------

    @staticmethod
    def _int(v, default: int = 0) -> int:
        """CLI 传进来的数字都是字符串，统一收口，非法值回 default"""
        if isinstance(v, bool):
            return int(v)
        try:
            return int(str(v).strip() or default)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _tag_list(tag) -> list[str]:
        if isinstance(tag, (list, tuple)):
            raw = [str(t) for t in tag]
        else:
            raw = str(tag or "").replace("，", ",").split(",")
        out = []
        for t in raw:
            t = t.strip()
            if t and t not in out:
                out.append(t)
        return out

    async def cmd_upload(self, file: str = "", title: str = "", tid=0,
                         desc: str = "", tag="", cover: str = "",
                         copyright=1, source: str = "", dynamic: str = "",
                         no_reprint=1, threads=3, dtime=0, tid_v2=0,
                         confirm: bool = False) -> str:
        """投稿视频。默认 dry-run 只做本地预检；confirm=True 才真实上传+提交审核。"""
        from . import upload as up

        meta = up.scan(file)
        tid_i = self._int(tid)
        tags = self._tag_list(tag)
        if not str(title or "").strip():
            return "ERR_TITLE: 缺少 title（稿件标题，≤80 字）"
        if len(str(title).strip()) > 80:
            return f"ERR_TITLE: 标题 {len(str(title).strip())} 字，超过 80 字上限"
        if len(str(desc or "")) > 2000:
            return f"ERR_DESC: 简介 {len(str(desc))} 字，超过 2000 字上限"
        if len(tags) > 10:
            return f"ERR_TAG: 标签 {len(tags)} 个，超过 10 个上限"
        if any(len(t) > 20 for t in tags):
            return "ERR_TAG: 单个标签超过 20 字"
        if not tid_i:
            return "ERR_TID: 缺少分区 tid，用 upload_zones 查"
        copyright_i = 2 if self._int(copyright, 1) == 2 else 1
        if copyright_i == 2 and not str(source or "").strip():
            return "ERR_SOURCE: 转载（copyright=2）必须给 source 原视频地址"

        plan = [
            f"plan upload{' (dry-run，未上传任何字节)' if not confirm else ''}",
            f"file: {meta['name']}  {meta['mb']}",
            f"title: {title}  ({len(title)}字)",
            f"tid: {tid_i}   copyright: {copyright_i}"
            f"{'（自制）' if copyright_i == 1 else '（转载 ' + str(source) + '）'}",
            f"tag: {','.join(tags) or '-'}",
            f"cover: {cover or '-'}   desc: {(desc or '-')[:60]}",
        ]
        if self._int(tid_v2):
            plan[3] = plan[3] + f"   tid_v2={self._int(tid_v2)}"
        if not confirm:
            plan.append("提示: 未提交。加 --confirm（MCP: confirm=true）才会真实上传并投稿，"
                        "投稿公开可见、不可撤回")
            return "\n".join(plan)

        r = await up.publish(self.bili.http, path=meta["path"], title=title,
                             tid=tid_i, desc=desc, tags=tags,
                             cover=str(cover or ""), copyright=copyright_i,
                             tid_v2=self._int(tid_v2) or None,
                             source=str(source or ""), dynamic=str(dynamic or ""),
                             no_reprint=self._int(no_reprint, 1),
                             threads=self._int(threads, 3) or 3,
                             dtime=self._int(dtime) or None)
        with store.lock:
            store.upload["last"] = {"bvid": r.get("bvid") or "", "aid": r.get("aid"),
                                    "title": title, "tid": tid_i,
                                    "file": r["path"], "size": r["size"],
                                    "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
        out = [f"ok upload {r.get('bvid') or '(无bvid)'}  aid={r.get('aid')}"
               f"  状态=已提交待审核",
               f"file: {meta['name']}  {meta['mb']}  chunks={r['chunks']}"
               f"  上传={r['cost']}s  {r['speed_mb']}MB/s  总耗时={r['total_cost']}s",
               f"title: {title}   tid={tid_i}   cover={'有' if r.get('cover') else '-'}",
               "hint: 用 upload_list 查看审核状态；过审后才会公开可见"]
        return "\n".join(out)

    async def cmd_upload_zones(self, top: int = 0) -> str:
        """列出投稿分区（父分区 -> tid 子分区）"""
        from . import upload as up

        z = await up.zones(self.bili.http)
        lines = ["view=zones  投稿分区（挑 tid 填进 upload）"]
        rows = z["tree"] if not self._int(top) else z["tree"][: self._int(top)]
        for t in rows:
            subs = " / ".join(f"{s['tid']}={s['name']}" for s in t["subs"])
            lines.append(f"- {t['name']}: {subs}")
        return "\n".join(lines)

    async def cmd_upload_list(self, pn=1, ps=10) -> str:
        """我的稿件列表 + 审核状态"""
        from . import upload as up

        rows = await up.archives(self.bili.http, pn=self._int(pn, 1) or 1,
                                 ps=self._int(ps, 10) or 10)
        if not rows:
            return "view=archives  没有稿件"
        lines = [f"view=archives  {len(rows)} 条"]
        for i, a in enumerate(rows, 1):
            lines.append(f"#{i} {a['bvid'] or a['aid']}  state={a['state']}"
                         f"({a['state_desc']})  {a['title']}")
        return "\n".join(lines)

    def _player_line(self) -> str:
        p = store.player
        return (f'player: {"playing" if p.playing else "paused"}  {p.bvid}  '
                f't={p.pos()}/{p.dur} ({fmt.hms(p.pos())})  q=-')

    def _actions(self, snap: dict | None = None) -> str:
        v = (snap or store.snapshot())["view"]
        if v == "search":
            return "actions: [open_video bvid] [search keyword --page]"
        if v == "video":
            return ("actions: [play] [pause] [seek +/-sec] [comments --page] "
                    "[subtitles] [subtitle --lang ai-zh --offset 0 --limit 200] "
                    "[like --on/--off] [coin --n 2 --also-like] [fav --folder 名称] "
                    "[unfav] [folders] [me] [state] [inbox --kind all] "
                    "[reply --msg-id <id> --message \"文案\"] "
                    "[roam --on] [roam_check] [roam_next] "
                    "[upload --file 路径 --title 标题 --tid 21 --confirm] [upload_list]")
        return "actions: [search keyword] [open_video bvid]"


def _bvid_of(uri: str) -> str:
    """从消息 uri 里抠出 BV 号（视频类消息才有）"""
    import re

    m = re.search(r"(BV[0-9A-Za-z]{10})", str(uri or ""))
    return m.group(1) if m else ""


core = Core()
