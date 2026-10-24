"""AI 巡览（roam）：按频率自动挑视频 -> 先读字幕 -> 读到字幕才生成待评论任务

设计分工（和 inbox 一致，本模块不接大模型、不需要任何 API Key）：

1. 取片：source=hot 走综合热门榜，source=search 用 keywords 轮询搜索结果；
2. 过滤：seen_bvid 记录处理过的视频，每个视频只处理一次；
3. 字幕优先：view 拿 avid -> subtitle_tracks 选轨（ai-zh 优先）-> subtitle_cues 取正文；
   - 无字幕轨 / 正文为空 / 字幕轨锁定 -> 该视频记 skipped 并写入 seen，绝不发评论；
   - 有字幕 -> 落一个 waiting 任务（含 excerpt 摘要与前 2000 字正文），等外部 AI 回填文案；
4. 发布：publish(task_id, text) 才真正调 comment_add（先审后发），
   文案一律由调用方（MCP 客户端 AI / 网页输入框 / CLI 参数）传入。

状态文件 .data/roam.json::

    {"enabled": false, "interval": 1800, "max_per_day": 5,
     "source": "hot", "keywords": [...], "kw_i": 0,
     "seen_bvid": ["BV..."],
     "tasks": [{"task_id": "T...", "bvid": "...", "status": "waiting",
                "text_head": "...", ...}],
     "stats": {"picked": 0, "sent": 0, "skipped": 0, "failed": 0,
               "today_sent": 0, "day": "2026-10-24",
               "last_run": "", "last_run_ts": 0, "last_bvid": "", "err": ""}}
"""

import asyncio
import json
import time
from pathlib import Path

from .http import ApiError
from .store import store

DATA_DIR = Path(__file__).resolve().parent.parent / ".data"
ROAM_FILE = DATA_DIR / "roam.json"

MIN_INTERVAL = 120          # 频率硬下限（秒），小于此值一律收敛
DEFAULT_INTERVAL = 1800     # 默认 30 分钟
DEFAULT_MAX_PER_DAY = 5
MAX_TASKS = 200             # 任务列表上限（超出优先淘汰已结束的旧任务）
MAX_SEEN = 3000             # 已见 bvid 上限
HEAD_CHARS = 2000           # 存给 AI 阅读的字幕正文长度
EXCERPT_CHARS = 300         # 卡片/概览用摘要长度
CAND_PER_ROUND = 8          # 一轮最多试几个视频（无字幕也算试过）
DEFAULT_KEYWORDS = ["科技", "数码", "编程"]

ZH_LANS = ("ai-zh", "zh-CN", "zh-Hans", "zh")


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _clamp_interval(v) -> int:
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL
    return max(MIN_INTERVAL, min(86400, n))


def _clamp_max_per_day(v) -> int:
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        return DEFAULT_MAX_PER_DAY
    if n < 0:
        return DEFAULT_MAX_PER_DAY
    return min(100, n) if n else 0        # 0 表示不限


def _norm_source(v) -> str:
    k = str(v or "").strip().lower()
    return k if k in ("hot", "search") else "hot"


def _norm_keywords(v) -> list[str]:
    if isinstance(v, (list, tuple)):
        raw = list(v)
    else:
        raw = str(v or "").replace("，", ",").replace(";", ",").replace("；", ",").split(",")
    out, seen = [], set()
    for k in raw:
        s = " ".join(str(k).split())
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out[:10]


def _truthy(v) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    return str(v).strip().lower() not in ("", "0", "false", "off", "no", "n", "否")


class Roam:
    def __init__(self, core) -> None:
        self.core = core
        self._state: dict | None = None
        self._mtime = 0.0
        self._busy = False

    # ---------------- 持久化 ----------------

    def _file_mtime(self) -> float:
        try:
            return ROAM_FILE.stat().st_mtime
        except OSError:
            return 0.0

    def _load(self) -> dict:
        """读状态；文件被别的实例（web_core / MCP core）改过则重新加载"""
        mtime = self._file_mtime()
        if self._state is not None and mtime == self._mtime:
            return self._state
        raw: dict = {}
        if mtime:
            try:
                raw = json.loads(ROAM_FILE.read_text(encoding="utf-8")) or {}
            except (OSError, ValueError):
                raw = {}
        if not isinstance(raw, dict):
            raw = {}
        st = {
            "enabled": bool(raw.get("enabled")),
            "interval": _clamp_interval(raw.get("interval", DEFAULT_INTERVAL)),
            "max_per_day": _clamp_max_per_day(raw.get("max_per_day", DEFAULT_MAX_PER_DAY)),
            "source": _norm_source(raw.get("source")),
            "keywords": _norm_keywords(raw.get("keywords")),
            "kw_i": int(raw.get("kw_i") or 0),
            "seen_bvid": [str(x) for x in (raw.get("seen_bvid") or []) if str(x)],
            "tasks": [],
            "stats": {},
        }
        for t in (raw.get("tasks") or []):
            if not isinstance(t, dict):
                continue
            tid = str(t.get("task_id") or "").strip()
            if not tid:                                  # 跳过类任务可能没有 id，补一个便于回看
                tid = f'S{int(t.get("created_ts") or 0)}'
            st["tasks"].append({
                "task_id": tid,
                "bvid": str(t.get("bvid") or ""),
                "avid": int(t.get("avid") or 0),
                "title": str(t.get("title") or ""),
                "up": str(t.get("up") or ""),
                "dur": int(t.get("dur") or 0),
                "play": int(t.get("play") or 0),
                "lan": str(t.get("lan") or ""),
                "lan_doc": str(t.get("lan_doc") or ""),
                "n_cues": int(t.get("n_cues") or 0),
                "chars": int(t.get("chars") or 0),
                "excerpt": str(t.get("excerpt") or ""),
                "text_head": str(t.get("text_head") or ""),
                "status": str(t.get("status") or "waiting"),
                "reason": str(t.get("reason") or ""),
                "created_at": str(t.get("created_at") or ""),
                "created_ts": int(t.get("created_ts") or 0),
                "sent_at": str(t.get("sent_at") or ""),
                "rpid": str(t.get("rpid") or ""),
                "err": str(t.get("err") or ""),
            })
        s = raw.get("stats") if isinstance(raw.get("stats"), dict) else {}
        st["stats"] = {
            "picked": int(s.get("picked") or 0),
            "sent": int(s.get("sent") or 0),
            "skipped": int(s.get("skipped") or 0),
            "failed": int(s.get("failed") or 0),
            "today_sent": int(s.get("today_sent") or 0),
            "day": str(s.get("day") or ""),
            "last_run": str(s.get("last_run") or ""),
            "last_run_ts": float(s.get("last_run_ts") or 0),
            "last_bvid": str(s.get("last_bvid") or ""),
            "err": str(s.get("err") or ""),
        }
        self._roll_day(st)
        self._state, self._mtime = st, mtime
        return st

    @staticmethod
    def _roll_day(st: dict) -> None:
        """跨天后把每日发布计数归零"""
        if st["stats"].get("day") != _today():
            st["stats"]["day"] = _today()
            st["stats"]["today_sent"] = 0

    def _save(self) -> None:
        # 以内存态为准：_load() 在文件被别的实例改过时会重新读盘，
        # 若直接写它会把本轮刚追加的任务丢掉，故这里只做「增量合并」。
        st = self._state if self._state is not None else self._load()
        disk: dict = {}
        if self._file_mtime() and self._file_mtime() != self._mtime:
            try:
                disk = json.loads(ROAM_FILE.read_text(encoding="utf-8")) or {}
            except (OSError, ValueError):
                disk = {}
        if isinstance(disk, dict):
            known = {t.get("task_id") for t in st["tasks"]}
            for t in disk.get("tasks") or []:
                tid = (t or {}).get("task_id")
                if tid and tid not in known:
                    st["tasks"].append(t)
                    known.add(tid)
            merged = list(st["seen_bvid"])
            for b in disk.get("seen_bvid") or []:
                b = str(b)
                if b and b not in merged:
                    merged.append(b)
            st["seen_bvid"] = merged[-MAX_SEEN:]
            st["tasks"].sort(key=lambda t: t.get("created_ts") or 0)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        data = {
            "enabled": st["enabled"],
            "interval": st["interval"],
            "max_per_day": st["max_per_day"],
            "source": st["source"],
            "keywords": list(st["keywords"]),
            "kw_i": st["kw_i"],
            "seen_bvid": st["seen_bvid"][-MAX_SEEN:],
            "tasks": st["tasks"],
            "stats": dict(st["stats"]),
        }
        try:
            ROAM_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                                 encoding="utf-8")
            self._mtime = self._file_mtime()
        except OSError:
            pass
        self._sync()

    def _sync(self) -> None:
        """把配置 / 统计 / 任务概览同步进 store，供三端实时观察"""
        st = self._load()
        tasks = sorted(st["tasks"], key=lambda t: t.get("created_ts") or 0,
                       reverse=True)
        counts = {"waiting": 0, "sent": 0, "skipped": 0, "failed": 0}
        for t in st["tasks"]:
            k = t.get("status") or "waiting"
            if k in counts:
                counts[k] += 1
        stats = dict(st["stats"])
        stats.update(counts)
        with store.lock:
            store.roam = {
                "enabled": st["enabled"],
                "interval": st["interval"],
                "max_per_day": st["max_per_day"],
                "source": st["source"],
                "keywords": list(st["keywords"]),
                "stats": stats,
                "tasks": tasks[:30],
                "loop": bool(store.roam.get("loop")),
            }

    # ---------------- 配置 ----------------

    def configure(self, on=None, interval=0, max_per_day=-1,
                  source="", keyword="", keywords=None) -> str:
        st = self._load()
        notes: list[str] = []
        if on is not None and str(on).strip() != "":
            v = _truthy(on)
            if v != st["enabled"]:
                st["enabled"] = v
                notes.append("已开启自动巡览" if v else "已关闭自动巡览")
        if interval not in (0, None, ""):
            try:
                raw = int(float(interval))
            except (TypeError, ValueError):
                return f"ERR_BAD_INTERVAL: {interval}"
            new = _clamp_interval(raw)
            st["interval"] = new
            notes.append(f"interval={new}s" + (f"（{raw}s 小于下限 {MIN_INTERVAL}s，已收敛）"
                                               if raw < MIN_INTERVAL else ""))
        if int(max_per_day) >= 0:
            st["max_per_day"] = _clamp_max_per_day(max_per_day)
            notes.append(f'max_per_day={st["max_per_day"]}' if st["max_per_day"]
                         else "max_per_day=0（不限）")
        if source:
            k = str(source).strip().lower()
            if k not in ("hot", "search"):
                return f"ERR_BAD_SOURCE: {source}  可选 hot/search"
            st["source"] = k
            notes.append(f"source={k}")
        kw_raw = keywords if keywords is not None else keyword
        if kw_raw:
            ks = _norm_keywords(kw_raw)
            if ks:
                st["keywords"] = ks
                if not source:                       # 只给关键词时自动切到搜索来源
                    st["source"] = "search"
                    notes.append("source=search（按关键词巡览）")
                notes.append("keywords=" + ",".join(ks))
        self._save()
        return "\n".join([f"roam {'已更新' if notes else '无变化'}: " +
                          ("; ".join(notes) if notes else "参数未变"),
                          self.status_text()])

    # ---------------- 状态文本 ----------------

    def status_text(self) -> str:
        st = self._load()
        self._roll_day(st)
        self._sync()
        counts = {"waiting": 0, "sent": 0, "skipped": 0, "failed": 0}
        for t in st["tasks"]:
            k = t.get("status") or "waiting"
            if k in counts:
                counts[k] += 1
        s = st["stats"]
        cap = st["max_per_day"]
        login = bool(getattr(self.core.bili, "logged_in", False))
        lines = [
            (f'roam: {"on" if st["enabled"] else "off"}  '
             f'interval={st["interval"]}s ({round(st["interval"] / 60, 1)}min)  '
             f'max_per_day={cap if cap else "不限"}  '
             f'source={st["source"]}  login={int(login)}'),
            ("keywords: " + (", ".join(st["keywords"]) if st["keywords"] else "(空)")),
            (f'stats: picked={s["picked"]}  waiting={counts["waiting"]}  '
             f'sent={s["sent"]}  skipped={counts["skipped"]}  '
             f'failed={counts["failed"]}  today_sent={s["today_sent"]}'
             f'{" / " + str(cap) if cap else ""}'),
            (f'loop: {"running" if store.roam.get("loop") else "stopped"}  '
             f'last_run={s["last_run"] or "-"}  last_bvid={s["last_bvid"] or "-"}'
             + (f'  err={s["err"]}' if s["err"] else "")),
        ]
        if counts["waiting"]:
            t = next(x for x in reversed(st["tasks"]) if x["status"] == "waiting")
            lines.append(f'next: task={t["task_id"]}  {t["bvid"]}  '
                         f'cues={t["n_cues"]} chars={t["chars"]}  {t["title"][:40]}')
        lines.append("actions: [roam --on/--off] [roam --interval 1800] "
                     "[roam --max-per-day 5] [roam --source hot|search --keyword 科技] "
                     "[roam_check] [roam_list] [roam_next] "
                     '[roam_comment --task-id T --message "文案"] [roam_skip --task-id T]')
        return "\n".join(lines)

    def list_text(self, status: str = "", limit: int = 20) -> str:
        st = self._load()
        self._sync()
        want = str(status or "").strip().lower()
        rows = [t for t in sorted(st["tasks"], key=lambda x: x.get("created_ts") or 0,
                                  reverse=True)
                if not want or t["status"] == want]
        if not rows:
            return f"roam_list: 0 条{'（status=' + want + '）' if want else ''}"
        limit = max(1, min(100, int(limit or 20)))
        lines = [f"roam_list: {len(rows)} 条  显示前 {min(limit, len(rows))}"]
        for t in rows[:limit]:
            flag = {"waiting": "·", "sent": "✓", "skipped": "~", "failed": "×"}.get(
                t["status"], "?")
            extra = f'  {t["reason"]}' if t["reason"] else ""
            lines.append(f'{flag} {t["task_id"]}  {t["bvid"]}  '
                         f'cues={t["n_cues"]} chars={t["chars"]}  '
                         f'{t["status"]}{extra}  {t["title"][:44]}')
        return "\n".join(lines)

    # ---------------- 取片 ----------------

    async def _candidates(self, st: dict) -> list[dict]:
        """按 source 取候选视频（已排除 seen_bvid）"""
        seen = set(st["seen_bvid"])
        out: list[dict] = []
        if st["source"] == "search":
            kws = st["keywords"] or DEFAULT_KEYWORDS
            kw = kws[st["kw_i"] % len(kws)]
            st["kw_i"] = (st["kw_i"] + 1) % max(1, len(kws))
            r = await self.core.bili.search(kw, 1, 10)
            for it in r.get("items") or []:
                if it.get("bvid") and it["bvid"] not in seen:
                    out.append({"bvid": it["bvid"], "title": it.get("title") or "",
                                "up": it.get("up") or "", "dur": 0,
                                "play": it.get("play") or 0})
            return out
        for it in await self.core.bili.hot_videos(20):
            if it.get("bvid") and it["bvid"] not in seen:
                out.append({"bvid": it["bvid"], "title": it.get("title") or "",
                            "up": it.get("up") or "", "dur": it.get("dur") or 0,
                            "play": it.get("play") or 0})
        return out

    def _add_task(self, st: dict, task: dict) -> None:
        st["tasks"].append(task)
        if len(st["tasks"]) > MAX_TASKS:
            # 先淘汰已结束的旧任务，再按时间截断
            done = [t for t in st["tasks"] if t["status"] != "waiting"]
            for t in done[:max(0, len(st["tasks"]) - MAX_TASKS)]:
                st["tasks"].remove(t)
            st["tasks"] = st["tasks"][-MAX_TASKS:]

    def _mark_seen(self, st: dict, bvid: str) -> None:
        if bvid and bvid not in st["seen_bvid"]:
            st["seen_bvid"].append(bvid)
            st["seen_bvid"] = st["seen_bvid"][-MAX_SEEN:]

    async def check(self, count: int = 1,
                    force: bool = False) -> dict:
        """跑一轮取片：挑视频 -> 读字幕 -> 生成 waiting 任务（不发任何评论）

        count: 本轮最多产出几个 waiting 任务（默认 1）
        force: 忽略 enabled 开关（手动 roam_check 用）
        """
        st = self._load()
        self._roll_day(st)
        summary = {"tried": 0, "picked": 0, "skipped": 0, "err": "",
                   "tasks": [], "note": ""}
        if self._busy:
            summary["err"] = "BUSY: 上一轮仍在执行"
            return summary
        if not force and not st["enabled"]:
            summary["err"] = "DISABLED: 巡览未开启"
            return summary
        if not getattr(self.core.bili, "logged_in", False):
            summary["err"] = "NEED_LOGIN: 未登录，跳过巡览"
            st["stats"]["err"] = summary["err"]
            self._save()
            return summary

        self._busy = True
        try:
            try:
                cands = await self._candidates(st)
            except Exception as e:                       # noqa: BLE001
                summary["err"] = f"FETCH_FAIL: {type(e).__name__}: {e}"
                st["stats"]["err"] = summary["err"]
                st["stats"]["last_run"] = _now()
                st["stats"]["last_run_ts"] = time.time()
                self._save()
                return summary
            if not cands:
                summary["note"] = "本轮没有新视频（候选都已看过或列表为空）"
                st["stats"]["last_run"] = _now()
                st["stats"]["last_run_ts"] = time.time()
                st["stats"]["err"] = ""
                self._save()
                return summary

            want = max(1, min(5, int(count or 1)))
            for cand in cands:
                if summary["picked"] >= want or summary["tried"] >= CAND_PER_ROUND:
                    break
                bvid = cand["bvid"]
                if bvid in st["seen_bvid"]:
                    continue
                summary["tried"] += 1
                try:
                    d = await self.core.bili.view(bvid)
                except Exception as e:                   # noqa: BLE001
                    summary["err"] = f"VIEW_FAIL {bvid}: {type(e).__name__}: {e}"
                    continue                          # 不写 seen，下一轮再试

                task = {
                    "task_id": "", "bvid": bvid, "avid": int(d.get("avid") or 0),
                    "title": d.get("title") or cand.get("title") or "",
                    "up": d.get("up") or cand.get("up") or "",
                    "dur": int(d.get("dur") or cand.get("dur") or 0),
                    "play": int(d.get("view") or cand.get("play") or 0),
                    "lan": "", "lan_doc": "", "n_cues": 0, "chars": 0,
                    "excerpt": "", "text_head": "",
                    "status": "waiting", "reason": "",
                    "created_at": _now(), "created_ts": int(time.time()),
                    "sent_at": "", "rpid": "", "err": "",
                }

                picked = await self._read_subtitle(d, task)
                if not picked:
                    task["status"] = "skipped"          # 无字幕 -> 只看不发
                    task["task_id"] = f'S{int(time.time())}{len(st["tasks"]) % 1000:03d}'
                    self._mark_seen(st, bvid)
                    self._add_task(st, task)
                    summary["skipped"] += 1
                    continue

                task["task_id"] = f'T{int(time.time())}{len(st["tasks"]) % 1000:03d}'
                self._mark_seen(st, bvid)
                self._add_task(st, task)
                st["stats"]["picked"] += 1
                st["stats"]["last_bvid"] = bvid
                summary["picked"] += 1
                summary["tasks"].append(task["task_id"])

            st["stats"]["last_run"] = _now()
            st["stats"]["last_run_ts"] = time.time()
            st["stats"]["err"] = summary["err"]
            self._save()
            return summary
        finally:
            self._busy = False

    async def _read_subtitle(self, d: dict, task: dict) -> bool:
        """读字幕并写入 task；读到正文返回 True，否则写 reason 返回 False"""
        bvid = task["bvid"]
        try:
            meta = await self.core.bili.subtitle_tracks(bvid)
        except ApiError as e:
            task["reason"] = "no_subtitle" if e.code == -404 else f"api_{e.code}"
            return False
        except Exception:                                # noqa: BLE001
            task["reason"] = "subtitle_error"
            return False
        tracks = meta.get("tracks") or []
        if not tracks:
            task["reason"] = "no_subtitle"
            return False
        pick = next((t for t in tracks if t["lan"] in ZH_LANS), None)
        if pick is None:                                 # 没有中文轨时退用首轨
            pick = tracks[0]
        if pick.get("lock"):
            task["reason"] = f'locked:{pick["lan"]}'
            return False
        try:
            r = await self.core.bili.subtitle_cues(bvid, pick["lan"])
        except ApiError as e:
            task["reason"] = "no_subtitle" if e.code == -404 else f"api_{e.code}"
            return False
        except Exception:                                # noqa: BLE001
            task["reason"] = "subtitle_error"
            return False
        cues = r.get("cues") or []
        text = "".join(c["text"] for c in cues).strip()
        if not text:
            task["reason"] = "no_subtitle"           # 轨道在但正文为空，同样跳过
            return False
        task.update({
            "lan": r.get("lan") or pick["lan"],
            "lan_doc": r.get("lan_doc") or pick.get("lan_doc") or "",
            "n_cues": len(cues),
            "chars": len(text),
            "excerpt": text[:EXCERPT_CHARS],
            "text_head": text[:HEAD_CHARS],
        })
        return True

    # ---------------- 任务分发 / 发布 ----------------

    def _find(self, st: dict, task_id: str) -> dict | None:
        key = str(task_id or "").strip()
        if not key:
            return None
        return next((t for t in st["tasks"] if t["task_id"] == key), None)

    def take_next(self, task_id: str = "") -> dict | None:
        """取一个任务（默认最早 waiting 的一个），含字幕正文供 AI 阅读"""
        st = self._load()
        if task_id:
            t = self._find(st, task_id)
            if not t:
                return None
            return t
        rows = [t for t in sorted(st["tasks"], key=lambda x: x.get("created_ts") or 0)
                if t["status"] == "waiting"]
        return rows[0] if rows else None

    def next_text(self, task_id: str = "") -> str:
        st = self._load()
        t = self.take_next(task_id)
        if not t:
            if not st["tasks"]:
                return "ERR_NO_TASK: 还没有任务，先 roam_check 取一轮"
            if st["tasks"] and all(x["status"] != "waiting" for x in st["tasks"]):
                return "ERR_NO_TASK: 没有 waiting 任务（已全部处理）"
            return f"ERR_NO_TASK: 找不到任务 {task_id}"
        if t["status"] != "waiting":
            return (f'ERR_TASK_{t["status"].upper()}: {t["task_id"]} 状态为 '
                    f'{t["status"]}，不能再取')
        body = t["text_head"] or t["excerpt"] or "(字幕正文为空)"
        return "\n".join([
            (f'roam_next: task={t["task_id"]}  bvid={t["bvid"]}  avid={t["avid"]}  '
             f'《{t["title"]}》'),
            (f'up={t["up"]}  dur={t["dur"]}s  play={t["play"]}  lan={t["lan"]}  '
             f'cues={t["n_cues"]}  chars={t["chars"]}'),
            "--- 字幕正文（前 2000 字，供阅读后写评论）---",
            body,
            "--- 字幕结束 ---",
            (f'hint: 读完据此写一条评论，用 '
             f'[roam_comment --task-id {t["task_id"]} --message "文案"] 回填并发布；'
             f'不想发就 [roam_skip --task-id {t["task_id"]}]'),
        ])

    async def publish(self, task_id: str, text: str) -> dict:
        """回填文案并发布（默认先审后发）。返回 {ok, msg, task}"""
        st = self._load()
        self._roll_day(st)
        t = self._find(st, task_id)
        if not t:
            return {"ok": False, "msg": f"ERR_NO_TASK: 找不到任务 {task_id}"}
        if t["status"] != "waiting":
            return {"ok": False,
                    "msg": f'ERR_TASK_{t["status"].upper()}: {task_id} 已处理过'}
        msg = str(text or "").strip()
        if not msg:
            return {"ok": False, "msg": "ERR_NEED_TEXT: 评论内容不能为空"}
        if len(msg) > 1000:
            return {"ok": False, "msg": "ERR_TEXT_TOO_LONG: 超过 1000 字上限"}
        cap = st["max_per_day"]
        if cap and st["stats"]["today_sent"] >= cap:
            return {"ok": False,
                    "msg": f'ERR_DAILY_LIMIT: 今日已发 {st["stats"]["today_sent"]}/{cap}，'
                           f'超限只采集不发布'}
        if not t["avid"]:
            return {"ok": False, "msg": f'ERR_NO_AID: {t["bvid"]} 缺少 avid'}
        try:
            r = await self.core.bili.comment_add(t["avid"], t["bvid"], msg)
        except Exception as e:                           # noqa: BLE001
            t["status"], t["err"] = "failed", f"{type(e).__name__}: {e}"
            st["stats"]["failed"] += 1
            st["stats"]["err"] = t["err"]
            self._save()
            return {"ok": False, "msg": f'{t["err"]}', "task": t}
        t["status"] = "sent"
        t["sent_at"] = _now()
        t["rpid"] = str(r.get("rpid") or "")
        t["err"] = ""
        st["stats"]["sent"] += 1
        st["stats"]["today_sent"] += 1
        st["stats"]["err"] = ""
        self._save()
        return {"ok": True,
                "msg": (f'ok roam_comment  task={t["task_id"]}  {t["bvid"]}  '
                        f'rpid={t["rpid"] or "-"}  today={st["stats"]["today_sent"]}'
                        f'{"/" + str(cap) if cap else ""}'),
                "task": t}

    def skip(self, task_id: str, reason: str = "manual") -> str:
        st = self._load()
        t = self._find(st, task_id)
        if not t:
            return f"ERR_NO_TASK: 找不到任务 {task_id}"
        if t["status"] != "waiting":
            return f'ERR_TASK_{t["status"].upper()}: {task_id} 已处理过'
        t["status"] = "skipped"
        t["reason"] = str(reason or "manual")
        self._save()
        return f'ok roam_skip  task={task_id}  {t["bvid"]}  reason={t["reason"]}'

    # ---------------- 后台轮询 ----------------

    async def loop(self) -> None:
        """后台轮询：每 20 秒检查一次是否到点。未开启/未登录自动跳过。"""
        with store.lock:
            store.roam["loop"] = True
        try:
            while True:
                try:
                    st = self._load()
                    if st["enabled"] and getattr(self.core.bili, "logged_in", False):
                        due = st["stats"]["last_run_ts"] + st["interval"]
                        if time.time() >= due:
                            await self.check()
                except asyncio.CancelledError:
                    raise
                except Exception as e:                   # noqa: BLE001
                    st = self._load()
                    st["stats"]["err"] = f"LOOP_FAIL: {type(e).__name__}: {e}"
                    try:
                        self._save()
                    except Exception:                    # noqa: BLE001
                        pass
                await asyncio.sleep(20)
        finally:
            with store.lock:
                store.roam["loop"] = False


_ROAMS: dict[int, Roam] = {}
_STARTED: dict[int, bool] = {}


def get_roam(core) -> Roam:
    """按 Core 取实例（状态共享 store.roam / .data/roam.json）"""
    r = _ROAMS.get(id(core))
    if r is None:
        r = Roam(core)
        _ROAMS[id(core)] = r
    return r


def start_loop(roam: Roam) -> bool:
    """幂等启动后台轮询：同一 Core 只起一个 loop"""
    key = id(roam.core)
    if _STARTED.get(key):
        return False
    _STARTED[key] = True
    try:
        asyncio.get_running_loop().create_task(roam.loop())
    except RuntimeError:                                 # 不在事件循环里
        _STARTED[key] = False
        return False
    return True
