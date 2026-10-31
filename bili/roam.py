"""AI 巡览（roam）：按频率自动挑视频 -> 先读字幕 -> 读到字幕才生成待评论任务

设计分工（和 inbox 一致）：

1. 取片：source=hot 走综合热门榜，source=search 用 keywords 轮询搜索结果；
2. 过滤：seen_bvid 记录处理过的视频，每个视频只处理一次；
3. 字幕优先：view 拿 avid -> subtitle_tracks 选轨（ai-zh 优先）-> subtitle_cues 取正文；
   - 无字幕轨 / 正文为空 / 字幕轨锁定 -> 该视频记 skipped 并写入 seen，绝不发评论；
   - 有字幕 -> 落一个 waiting 任务（含 excerpt 摘要与前 2000 字正文）；
4. 拟稿（可选，接 llm.py）：llm_draft=on 且配了 API Key 时，autodraft 用外部大模型
   按字幕写评论文案 -> 写入 task["draft"]；llm_publish=on 时顺手发布。
   两个开关都关着也照样能跑：文案一律由调用方（MCP 客户端 AI / 网页输入框 / CLI）传入。
5. 发布：publish(task_id, text) 才真正调 comment_add（先审后发）；
   send_draft(task_id) 是把已有草稿原样发出去（人工一键）。

状态文件 .data/roam.json::

    {"enabled": false, "interval": 1800, "max_per_day": 5,
     "source": "hot", "keywords": [...], "kw_i": 0,
     "llm_draft": true, "llm_publish": false,
     "seen_bvid": ["BV..."],
     "tasks": [{"task_id": "T...", "bvid": "...", "status": "waiting",
                "draft": "...", "draft_at": "...", "draft_model": "...",
                "text_head": "...", ...}],
     "stats": {"picked": 0, "sent": 0, "skipped": 0, "failed": 0,
               "drafted": 0, "draft_failed": 0,
               "today_sent": 0, "day": "2026-10-24",
               "last_run": "", "last_run_ts": 0, "last_bvid": "", "err": ""}}
"""

import asyncio
import json
import os
import time
from pathlib import Path

from .http import ApiError
from .store import store

DATA_DIR = Path(__file__).resolve().parent.parent / ".data"
ROAM_FILE = DATA_DIR / "roam.json"
PUB_LOCK_FILE = DATA_DIR / "roam.pub.lock"

MIN_INTERVAL = 120          # 频率硬下限（秒），小于此值一律收敛
DEFAULT_INTERVAL = 1800     # 默认 30 分钟
DEFAULT_MAX_PER_DAY = 5
MAX_TASKS = 200             # 任务列表上限（超出优先淘汰已结束的旧任务）
MAX_SEEN = 3000             # 已见 bvid 上限
HEAD_CHARS = 2000           # 存给 AI 阅读的字幕正文长度
EXCERPT_CHARS = 300         # 卡片/概览用摘要长度
CAND_PER_ROUND = 8          # 一轮最多试几个视频（无字幕也算试过）
DEFAULT_KEYWORDS = ["科技", "数码", "编程"]

LLM_DRAFT_BATCH = 2         # 一轮最多自动拟几条（控制单次 API 消耗）
LLM_RETRY_GAP = 120         # 同一任务拟稿失败后的冷却（秒），避免空转烧额度
LLM_LOOP_GAP = 60           # 后台补拟稿的最小间隔（秒）
SEND_STALE = 180            # sending 占位超过这么久视为进程崩溃，回退成 waiting（秒）

ZH_LANS = ("ai-zh", "zh-CN", "zh-Hans", "zh")


class _FileLock:
    """跨进程互斥锁（web_core / MCP core / CLI 同时操作 roam.json 时用）

    发布评论是不可撤销的写操作，两个进程（如后台 loop 与 MCP 工具）若同时
    通过 waiting 检查，会对同一条视频发出两条评论。这里用 O_CREAT|O_EXCL
    建锁文件做互斥，超时或陈旧锁自动清理，不引入第三方依赖。
    """

    def __init__(self, path: Path, timeout: float = 6.0) -> None:
        self.path = path
        self.timeout = timeout
        self.fd: int | None = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        t0 = time.time()
        while True:
            try:
                self.fd = os.open(str(self.path),
                                  os.O_CREAT | os.O_EXCL | os.O_RDWR)
                os.write(self.fd, str(os.getpid()).encode())
                return self
            except FileExistsError:
                try:                                  # 陈旧锁（持锁进程已死）
                    if time.time() - self.path.stat().st_mtime > 30:
                        self.path.unlink()
                        continue
                except OSError:
                    pass
                if time.time() - t0 > self.timeout:
                    raise TimeoutError("LOCK_TIMEOUT")
                time.sleep(0.05)

    def __exit__(self, *exc) -> bool:
        try:
            if self.fd is not None:
                os.close(self.fd)
        finally:
            self.fd = None
            try:
                self.path.unlink()
            except OSError:
                pass
        return False


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
        self._last_draft_ts = 0.0

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
            "llm_draft": bool(raw.get("llm_draft", True)),
            "llm_publish": bool(raw.get("llm_publish", False)),
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
                "draft": str(t.get("draft") or ""),
                "draft_by": str(t.get("draft_by") or ""),
                "draft_at": str(t.get("draft_at") or ""),
                "draft_model": str(t.get("draft_model") or ""),
                "draft_err": str(t.get("draft_err") or ""),
                "draft_try_ts": float(t.get("draft_try_ts") or 0),
                "created_at": str(t.get("created_at") or ""),
                "created_ts": int(t.get("created_ts") or 0),
                "sent_at": str(t.get("sent_at") or ""),
                "send_try_ts": float(t.get("send_try_ts") or 0),
                "rpid": str(t.get("rpid") or ""),
                "err": str(t.get("err") or ""),
            })
        s = raw.get("stats") if isinstance(raw.get("stats"), dict) else {}
        st["stats"] = {
            "picked": int(s.get("picked") or 0),
            "sent": int(s.get("sent") or 0),
            "skipped": int(s.get("skipped") or 0),
            "failed": int(s.get("failed") or 0),
            "drafted": int(s.get("drafted") or 0),
            "draft_failed": int(s.get("draft_failed") or 0),
            "today_sent": int(s.get("today_sent") or 0),
            "day": str(s.get("day") or ""),
            "last_run": str(s.get("last_run") or ""),
            "last_run_ts": float(s.get("last_run_ts") or 0),
            "last_bvid": str(s.get("last_bvid") or ""),
            "err": str(s.get("err") or ""),
        }
        self._recover_sending(st)
        self._roll_day(st)
        self._state, self._mtime = st, mtime
        return st

    @staticmethod
    def _recover_sending(st: dict) -> None:
        """发布占位（sending）超时说明持锁进程已崩，退回 waiting 让任务可继续处理"""
        now = time.time()
        for t in st["tasks"]:
            if t.get("status") != "sending":
                continue
            if now - float(t.get("send_try_ts") or 0) < SEND_STALE:
                continue
            t["status"] = "waiting"
            t["err"] = "RECOVERED: 上次发布中断（进程退出或超时），已退回待处理"

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
            "llm_draft": st["llm_draft"],
            "llm_publish": st["llm_publish"],
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
        counts = {"waiting": 0, "sending": 0, "sent": 0, "skipped": 0, "failed": 0}
        for t in st["tasks"]:
            k = t.get("status") or "waiting"
            if k in counts:
                counts[k] += 1
        stats = dict(st["stats"])
        stats.update(counts)
        with store.lock:
            store.roam = {
                "enabled": st["enabled"],
                "llm_draft": st["llm_draft"],
                "llm_publish": st["llm_publish"],
                "llm_ready": self.llm_ready(),
                "llm_model": self.llm_model(),
                "interval": st["interval"],
                "max_per_day": st["max_per_day"],
                "source": st["source"],
                "keywords": list(st["keywords"]),
                "stats": stats,
                "tasks": tasks[:30],
                "loop": bool(store.roam.get("loop")),
            }

    # ---------------- 配置 ----------------

    def _llm(self):
        """复用进程内 llm 单例（配置 / 用量都在 .data/llm.json）"""
        from .llm import get_llm

        return get_llm(self.core)

    def llm_ready(self) -> bool:
        """是否已配好 API Key（没 Key 时拟稿开关自动失效，不发请求）"""
        try:
            return bool(self._llm().raw().get("api_key"))
        except Exception:                            # noqa: BLE001
            return False

    def llm_model(self) -> str:
        try:
            return str(self._llm().raw().get("model") or "")
        except Exception:                            # noqa: BLE001
            return ""

    def configure(self, on=None, interval=0, max_per_day=-1,
                  source="", keyword="", keywords=None,
                  llm_draft=None, llm_publish=None) -> str:
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
        if llm_draft is not None and str(llm_draft).strip() != "":
            v = _truthy(llm_draft)
            if v != st["llm_draft"]:
                st["llm_draft"] = v
                notes.append("AI 拟稿已开启" if v else "AI 拟稿已关闭")
            if v and not st["llm_publish"]:
                notes.append("（只拟稿不发送，草稿进评论面板）")
        if llm_publish is not None and str(llm_publish).strip() != "":
            v = _truthy(llm_publish)
            if v and not self.llm_ready():
                self._save()
                return ("ERR_NEED_KEY: 还没配 API Key（网页顶部「AI 自动回复」里填，"
                        "或 cli llm_configure --api-key xxx），自动发布未开启\n"
                        + self.status_text())
            if v != st["llm_publish"]:
                st["llm_publish"] = v
                notes.append("AI 自动发布已开启" if v else "AI 自动发布已关闭")
            if v and not st["llm_draft"]:
                st["llm_draft"] = True
                notes.append("（自动发布需要拟稿，已同时开启 AI 拟稿）")
        self._save()
        return "\n".join([f"roam {'已更新' if notes else '无变化'}: " +
                          ("; ".join(notes) if notes else "参数未变"),
                          self.status_text()])

    # ---------------- 状态文本 ----------------

    def status_text(self) -> str:
        st = self._load()
        self._roll_day(st)
        self._sync()
        counts = {"waiting": 0, "sending": 0, "sent": 0, "skipped": 0, "failed": 0}
        for t in st["tasks"]:
            k = t.get("status") or "waiting"
            if k in counts:
                counts[k] += 1
        s = st["stats"]
        cap = st["max_per_day"]
        login = bool(getattr(self.core.bili, "logged_in", False))
        ready = self.llm_ready()
        lines = [
            (f'roam: {"on" if st["enabled"] else "off"}  '
             f'interval={st["interval"]}s ({round(st["interval"] / 60, 1)}min)  '
             f'max_per_day={cap if cap else "不限"}  '
             f'source={st["source"]}  login={int(login)}'),
            ("keywords: " + (", ".join(st["keywords"]) if st["keywords"] else "(空)")),
            (f'llm: key={int(ready)}  model={self.llm_model() or "-"}  '
             f'draft={"on" if st["llm_draft"] else "off"}  '
             f'publish={"on" if st["llm_publish"] else "off"}  '
             f'drafted={s.get("drafted", 0)}  draft_failed={s.get("draft_failed", 0)}'
             + ("" if ready else "  （没填 Key，拟稿不会触发）")),
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
            if t.get("draft"):
                lines.append(f'next_draft[{t.get("draft_by") or "?"}]: {t["draft"][:80]}')
        lines.append("actions: [roam --on/--off] [roam --interval 1800] "
                     "[roam --max-per-day 5] [roam --source hot|search --keyword 科技] "
                     "[roam --llm-draft on|off] [roam --llm-publish on|off] "
                     "[roam_check] [roam_list] [roam_next] [roam_draft] "
                     "[roam_draft_set --task-id T --text \"文案\"] "
                     f'[roam_comment --task-id T --message "文案"] [roam_send --task-id T] '
                     "[roam_skip --task-id T]")
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
            if t.get("draft"):
                extra += "  draft✎"
            elif t.get("draft_err"):
                extra += "  draft×"
            lines.append(f'{flag} {t["task_id"]}  {t["bvid"]}  '
                         f'cues={t["n_cues"]} chars={t["chars"]}  '
                         f'{t["status"]}{extra}  {t["title"][:44]}')
        return "\n".join(lines)

    # ---------------- AI 拟稿（接 llm.py） ----------------

    def _undrafted(self, st: dict) -> list[dict]:
        """待拟稿任务：waiting + 还没有草稿，按创建时间排序"""
        return [t for t in sorted(st["tasks"], key=lambda x: x.get("created_ts") or 0)
                if t["status"] == "waiting" and not t.get("draft")]

    async def autodraft(self, limit: int = LLM_DRAFT_BATCH,
                        publish=None, only_id: str = "",
                        force: bool = False) -> dict:
        """给 waiting 任务批量拟稿；publish=None 时按 llm_publish 开关决定是否顺手发

        only_id 指定时只拟这一个任务；force=True 才允许覆盖它已有的草稿
        （人工「重拟」用，避免冲掉对话侧 Agent / 人工写好的稿）。
        返回 {ok, drafted, published, failed, skipped, msgs, tasks}
        - 没有 Key / 拟稿开关关着 -> skipped 里说明原因，不发任何请求
        - 同一任务失败后 LLM_RETRY_GAP 秒内不重试，避免烧额度
        """
        out = {"ok": True, "drafted": 0, "published": 0, "failed": 0,
               "skipped": "", "msgs": [], "tasks": []}
        st = self._load()
        if not only_id and not st["llm_draft"] and publish is None:
            out["skipped"] = "DRAFT_OFF: AI 拟稿未开启（roam --llm-draft on 打开）"
            return out
        llm = self._llm()
        if not self.llm_ready():
            out["skipped"] = "NO_KEY: 还没配 API Key，无法拟稿"
            return out
        do_pub = st["llm_publish"] if publish is None else bool(publish)
        want = max(1, min(10, int(limit or 1)))
        model = self.llm_model()

        if only_id:
            one = self._find(st, only_id)
            if not one:
                out["ok"] = False
                out["skipped"] = f"ERR_NO_TASK: 找不到任务 {only_id}"
                return out
            if one["status"] != "waiting":
                out["ok"] = False
                out["skipped"] = (f'ERR_TASK_{one["status"].upper()}: '
                                  f'{only_id} 已处理过')
                return out
            targets = [one]
        else:
            targets = self._undrafted(st)[:want]

        for t in targets:
            last = float(t.get("draft_try_ts") or 0)
            if t.get("draft_err") and time.time() - last < LLM_RETRY_GAP:
                continue
            tid = t["task_id"]
            base = t.get("draft") or ""              # 开工时的草稿基线
            try:
                text = await asyncio.to_thread(llm.draft_for_roam, dict(t))
            except Exception as e:                   # noqa: BLE001
                err = f"{type(e).__name__}: {e}"[:300]
                if self._commit_draft(tid, {"draft_err": err,
                                            "draft_try_ts": time.time()},
                                      steal=True, stat="draft_failed"):
                    llm.note(False, err)
                    out["failed"] += 1
                    out["msgs"].append(f"DRAFT_FAIL {tid}: {err}")
                continue
            # 拟稿期间 Agent 可能已手工存稿：锁内合并，抢到的人优先，不覆盖
            wrote = self._commit_draft(tid, {"draft": text, "draft_by": "llm",
                                             "draft_at": _now(), "draft_model": model,
                                             "draft_err": "", "draft_try_ts": time.time()},
                                       steal=bool(force), stat="drafted",
                                       expect=base if force else None)
            if not wrote:
                out["msgs"].append(f"SKIP {tid}: 已有草稿或任务已结束，保留原稿")
                continue
            out["drafted"] += 1
            out["tasks"].append(tid)
            llm.bump("draft")
            llm.note(True)
            out["msgs"].append(f"DRAFT {tid}: {text}")
            if do_pub:
                if not llm.quota_ok():
                    out["msgs"].append("  -> 未发送（llm 每日发送上限已到，草稿留着）")
                    continue
                r = await self.publish(tid, text, by="llm", actor="ai")
                if r.get("ok"):
                    out["published"] += 1
                    llm.bump("sent")
                    out["msgs"].append(f'  -> SENT rpid={r.get("task", {}).get("rpid") or "-"}')
                else:
                    out["msgs"].append(f'  -> NOT_SENT {r.get("msg")}')
        self._sync()
        return out

    async def draft_one(self, task_id: str = "", publish=None,
                        force: bool = False) -> dict:
        """手动给指定任务（或最早一个未拟稿任务）拟稿

        task_id 已有草稿时：force=True 才重新生成（人工要我改写时用）；
        否则保留原草稿——避免把对话侧 Agent / 人工写好的稿冲掉。
        """
        st = self._load()
        if task_id:
            t = self._find(st, task_id)
            if not t:
                return {"ok": False, "skipped": "",
                        "msgs": [f"ERR_NO_TASK: 找不到任务 {task_id}"]}
            if t["status"] != "waiting":
                return {"ok": False, "skipped": "",
                        "msgs": [f'ERR_TASK_{t["status"].upper()}: {task_id} 已处理过']}
            if t.get("draft"):
                by = t.get("draft_by") or "unknown"
                if not force:
                    return {"ok": False, "skipped": "KEEP_DRAFT",
                            "msgs": [f'ERR_KEEP_DRAFT: {task_id} 已有草稿'
                                     f'（来源={by}），未覆盖；要重拟请传 force=True']}
            return await self.autodraft(limit=1, publish=publish,
                                        only_id=task_id, force=bool(force))
        rows = self._undrafted(st)
        if not rows:
            return {"ok": False, "skipped": "",
                    "msgs": ["ERR_NO_TASK: 没有待拟稿的 waiting 任务（都已拟过或没有任务）"]}
        return await self.autodraft(limit=1, publish=publish)

    def _commit_draft(self, task_id: str, fields: dict,
                      steal: bool = False, stat: str = "",
                      expect: str | None = None) -> bool:
        """在跨进程锁下把草稿字段合并进最新状态，避免覆盖别的进程刚写入的草稿

        steal=False（默认）时，目标任务此刻已有草稿就放弃写入、保留原稿；
        expect 给出时，要求当前草稿仍等于这个旧值才写——用于 force 重拟：
        只覆盖「我开工时看到的那份」，拟稿途中别人新写的稿一律不动。
        stat 指定时顺手把该统计项 +1（在锁内做，避免计数被并发覆盖）。
        返回 True=已写入，False=被别处抢先或任务已结束，本次不写。
        """
        try:
            with _FileLock(PUB_LOCK_FILE):
                self._state = None                   # 强制重读，拿别进程的最新状态
                st = self._load()
                t = self._find(st, task_id)
                if not t or t["status"] != "waiting":
                    return False
                if expect is not None and (t.get("draft") or "") != expect:
                    return False
                if t.get("draft") and not steal:
                    return False
                t.update(fields)
                if stat:
                    st["stats"][stat] = int(st["stats"].get(stat) or 0) + 1
                self._save()
                return True
        except TimeoutError:
            return False

    def set_draft(self, task_id: str, text: str, by: str = "agent") -> dict:
        """写入/覆盖草稿但不发布——供对话侧 Agent、前端「存草稿」使用

        与 llm 自动拟稿共用 task["draft"] 通道，用 draft_by 标记来源
        （llm=外部模型、agent=对话侧助手、human=人工）。显式写入即覆盖，
        写完后 autodraft 不会再重拟该任务（_undrafted 只挑没有草稿的）。
        """
        msg = str(text or "").strip()
        if not msg:
            return {"ok": False, "msg": "ERR_NEED_TEXT: 草稿不能为空"}
        if len(msg) > 1000:
            return {"ok": False, "msg": "ERR_TEXT_TOO_LONG: 超过 1000 字上限"}
        by = str(by or "agent").strip().lower()
        if by not in ("agent", "human", "llm"):
            by = "agent"
        ok = self._commit_draft(task_id,
                                {"draft": msg, "draft_by": by,
                                 "draft_at": _now(), "draft_err": ""},
                                steal=True)
        if not ok:
            st = self._load()
            t = self._find(st, task_id)
            if not t:
                return {"ok": False, "msg": f"ERR_NO_TASK: 找不到任务 {task_id}"}
            return {"ok": False,
                    "msg": f'ERR_TASK_{t["status"].upper()}: {task_id} 已处理过'}
        return {"ok": True,
                "msg": f'ok roam_draft_set task={task_id} by={by} len={len(msg)}',
                "task": self._find(self._load(), task_id)}

    async def send_draft(self, task_id: str = "") -> dict:
        """把已有草稿原样发布（人工确认后一键发）；task_id 留空取最早带草稿的任务"""
        st = self._load()
        t = self._find(st, task_id) if task_id else next(
            (x for x in sorted(st["tasks"], key=lambda y: y.get("created_ts") or 0)
             if x["status"] == "waiting" and x.get("draft")), None)
        if not t:
            return {"ok": False, "msg": f"ERR_NO_TASK: 找不到任务 {task_id or '(带草稿的 waiting)'}"}
        if not t.get("draft"):
            return {"ok": False,
                    "msg": f'ERR_NO_DRAFT: {t["task_id"]} 还没有 AI 草稿，先 roam_draft'}
        return await self.publish(t["task_id"], t["draft"],
                                  by=t.get("draft_by") or "")

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
                   "tasks": [], "note": "", "draft": None}
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
            # 新任务出来就顺手让大模型拟稿（开关关着 / 没 Key 时静默跳过）
            if summary["picked"] and st["llm_draft"] and self.llm_ready():
                dr = await self.autodraft(
                    limit=min(summary["picked"], LLM_DRAFT_BATCH))
                summary["draft"] = {"drafted": dr["drafted"],
                                    "published": dr["published"],
                                    "failed": dr["failed"],
                                    "msgs": dr["msgs"]}
                if dr["failed"] and dr["drafted"] == 0:
                    summary["err"] = summary["err"] or f'DRAFT_FAIL: {dr["msgs"][0][:160]}'
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
        lines = [
            (f'roam_next: task={t["task_id"]}  bvid={t["bvid"]}  avid={t["avid"]}  '
             f'《{t["title"]}》'),
            (f'up={t["up"]}  dur={t["dur"]}s  play={t["play"]}  lan={t["lan"]}  '
             f'cues={t["n_cues"]}  chars={t["chars"]}'),
            "--- 字幕正文（前 2000 字，供阅读后写评论）---",
            body,
            "--- 字幕结束 ---",
        ]
        if t.get("draft"):
            by = t.get("draft_by") or "unknown"
            who = {"llm": "外部大模型", "agent": "对话助手", "human": "人工"}.get(by, by)
            lines += [
                (f'--- 已有草稿（来源={by}/{who}  model={t.get("draft_model") or "-"}  '
                 f'at={t.get("draft_at") or "-"}）---'),
                t["draft"],
                f'[roam_send --task-id {t["task_id"]}] 原样发出这条草稿；'
                f'想改就用 [roam_comment --task-id {t["task_id"]} --message "新文案"]；'
                f'要换外部模型重拟就 [roam_draft --task-id {t["task_id"]} --force]',
            ]
        else:
            hint = (f'hint: 读完据此写一条评论，用 '
                    f'[roam_comment --task-id {t["task_id"]} --message "文案"] 回填并发布；'
                    f'不想发就 [roam_skip --task-id {t["task_id"]}]')
            if t.get("draft_err"):
                hint += f'\nnote: 上次 AI 拟稿失败：{t["draft_err"]}'
            lines.append(hint)
        return "\n".join(lines)

    async def publish(self, task_id: str, text: str, by: str = "",
                      actor: str = "user") -> dict:
        """回填文案并发布（默认先审后发）。返回 {ok, msg, task}

        by: 文案来源（llm / agent / human），只做记录，不影响发布逻辑。
        actor: "ai" 表示这条是内置 AI 自己发的，先过 AI 能力模式守门
        （autodraft 自动发布走这条路；人点「发送」是 actor="user" 不受限）。
        并发保护：除了本实例的 _busy，再用跨进程文件锁 + sending 占位，
        防止 web loop 与 MCP/CLI 同时通过 waiting 检查、对同一条视频发两条评论。
        """
        msg = str(text or "").strip()
        if not msg:
            return {"ok": False, "msg": "ERR_NEED_TEXT: 评论内容不能为空"}
        if len(msg) > 1000:
            return {"ok": False, "msg": "ERR_TEXT_TOO_LONG: 超过 1000 字上限"}
        if str(actor or "").strip().lower() in ("ai", "llm", "agent"):
            from . import modes

            mode = modes.current()
            deny = modes.check(mode, "roam_send")
            if deny:
                modes.audit("roam_send", mode, False, deny)
                return {"ok": False, "msg": deny}
        try:
            with _FileLock(PUB_LOCK_FILE):
                self._state = None                   # 强制重读：别的进程可能刚发过
                st = self._load()
                self._roll_day(st)
                t = self._find(st, task_id)
                if not t:
                    return {"ok": False, "msg": f"ERR_NO_TASK: 找不到任务 {task_id}"}
                if t["status"] != "waiting":
                    return {"ok": False,
                            "msg": f'ERR_TASK_{t["status"].upper()}: {task_id} 已处理过'}
                cap = st["max_per_day"]
                if cap and st["stats"]["today_sent"] >= cap:
                    return {"ok": False,
                            "msg": f'ERR_DAILY_LIMIT: 今日已发 {st["stats"]["today_sent"]}/{cap}，'
                                   f'超限只采集不发布'}
                if not t["avid"]:
                    return {"ok": False, "msg": f'ERR_NO_AID: {t["bvid"]} 缺少 avid'}
                # 占位：把任务从 waiting 摘走并落盘，另一个进程再来会直接看到 sending
                t["status"] = "sending"
                t["send_try_ts"] = time.time()
                t["err"] = ""
                self._save()
        except TimeoutError:
            return {"ok": False, "msg": "ERR_BUSY: 另一个进程正在发布评论，请稍后重试"}
        src = str(by or "").strip().lower()
        if src in ("llm", "agent", "human"):
            t["draft"] = msg
            t["draft_by"] = src
            t["draft_at"] = t.get("draft_at") or _now()
        try:
            r = await self.core.bili.comment_add(t["avid"], t["bvid"], msg)
        except Exception as e:                           # noqa: BLE001
            self._state = None
            st = self._load()
            t = self._find(st, task_id) or t
            t["status"], t["err"] = "failed", f"{type(e).__name__}: {e}"
            st["stats"]["failed"] += 1
            st["stats"]["err"] = t["err"]
            self._save()
            return {"ok": False, "msg": f'{t["err"]}', "task": t}
        # 发送期间别的进程可能已改动文件：重读后再落状态，避免把别人的变更覆盖掉
        self._state = None
        st = self._load()
        t = self._find(st, task_id) or t
        t["status"] = "sent"
        t["sent_at"] = _now()
        t["rpid"] = str(r.get("rpid") or "")
        t["err"] = ""
        st["stats"]["sent"] += 1
        st["stats"]["today_sent"] += 1
        st["stats"]["err"] = ""
        self._save()
        self._sync()
        return {"ok": True,
                "msg": (f'ok roam_comment  task={t["task_id"]}  {t["bvid"]}  '
                        f'rpid={t["rpid"] or "-"}  today={st["stats"]["today_sent"]}'
                        f'{"/" + str(cap) if cap else ""}'
                        f'{"" if not src else "  by=" + src}'),
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
        self._sync()
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
                    # 补拟稿：开关开着且有积压的 waiting 未拟稿，独立于取片频率慢慢消化
                    if (st["llm_draft"] and self.llm_ready()
                            and time.time() - self._last_draft_ts >= LLM_LOOP_GAP
                            and self._undrafted(st)):
                        self._last_draft_ts = time.time()
                        await self.autodraft(limit=1)
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
