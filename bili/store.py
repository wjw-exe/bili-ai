"""单一真相源：所有前端（MCP / Web / CLI）只读写这里

播放进度为「逻辑进度」：不接解码器，play 之后按单调时钟自行推进，
pause 时冻结，seek 时重置基准。
"""

import threading
import time
from collections import deque


class Player:
    def __init__(self) -> None:
        self.bvid = ""
        self.title = ""
        self.dur = 0
        self.playing = False
        self.base = 0.0        # 暂停/定位时刻的秒数
        self.started = 0.0     # 起播时的 monotonic

    def pos(self) -> float:
        if not self.playing:
            return round(self.base, 1)
        return round(min(float(self.dur or 86400), self.base + (time.monotonic() - self.started)), 1)

    def play(self, at: float = 0.0) -> None:
        self.base = at
        self.started = time.monotonic()
        self.playing = True

    def pause(self) -> None:
        self.base = self.pos()
        self.playing = False

    def seek(self, to: float) -> None:
        to = max(0.0, min(float(self.dur or 86400), to))
        if self.playing:
            self.base, self.started = to, time.monotonic()
        else:
            self.base = to

    def snapshot(self) -> dict:
        return {"bvid": self.bvid, "title": self.title, "dur": self.dur,
                "playing": self.playing, "pos": self.pos()}


class Store:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.view = "idle"            # idle | search | video
        self.query = ""
        self.page = 1
        self.total = 0
        self.items: list[dict] = []
        self.current: dict | None = None
        self.player = Player()
        self.log: deque[dict] = deque(maxlen=120)
        self.started_at = time.time()
        # 评论面板
        self.comments: list[dict] = []
        self.comments_meta: dict = {}
        # 当前视频的互动状态；None 表示未登录/未知
        self.inter: dict = {"liked": None, "coins": None, "favoured": None}
        # 字幕：当前视频的轨道列表与已取回的字幕概况
        self.subs: dict = {"bvid": "", "lan": "", "lan_doc": "", "tracks": [],
                           "n": 0, "chars": 0, "saved": ""}
        self.account: dict = {"login": False, "uname": "", "mid": 0, "avatar": ""}
        # AI 巡览：自动挑视频 -> 读字幕 -> 生成待评论任务（文案由外部 AI 回填，本端不接大模型）
        self.roam: dict = {
            "enabled": False,
            "interval": 1800,
            "max_per_day": 5,
            "source": "hot",
            "keywords": [],
            "stats": {"picked": 0, "waiting": 0, "sent": 0, "skipped": 0,
                      "failed": 0, "today_sent": 0, "day": "",
                      "last_run": "", "last_bvid": "", "err": ""},
            "tasks": [],           # 最近任务概览（不含长文本字幕）
            "loop": False,         # 后台轮询是否在跑
        }
        # 消息收件箱（别人回复我 / @我）：items 为最近一次拉取结果，
        # replied 为 {msg_id: {text, rpid, ts}}，drafts 为大模型拟的待发草稿
        # {msg_id: {text, ts, model}}，stats 只做展示用
        self.inbox: dict = {"items": [], "replied": {}, "drafts": {},
                            "stats": {"fetched": 0, "replied": 0,
                                      "last_fetch": "", "err": ""}}
        # 投稿（发视频）：只记录最近一次成功提交，供后台展示
        self.upload: dict = {"last": None}

    def note(self, cmd: str, ok: bool, summary: str) -> None:
        with self.lock:
            self.log.appendleft({"t": time.strftime("%H:%M:%S"), "ts": round(time.time() - self.started_at, 1),
                                 "cmd": cmd, "ok": ok, "summary": summary[:160]})

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "view": self.view,
                "query": self.query,
                "page": self.page,
                "total": self.total,
                "items": list(self.items),
                "current": dict(self.current) if self.current else None,
                "player": self.player.snapshot(),
                "comments": list(self.comments),
                "comments_meta": dict(self.comments_meta),
                "inter": dict(self.inter),
                "subs": dict(self.subs),
                "account": dict(self.account),
                "inbox": self._inbox_snapshot(),
                "roam": self._roam_snapshot(),
                "upload": {"last": dict(self.upload.get("last") or {}) or None},
                "log": list(self.log),
                "uptime": round(time.time() - self.started_at, 1),
            }

    def _roam_snapshot(self) -> dict:
        r = self.roam
        stats = dict(r.get("stats") or {})
        tasks = []
        for t in (r.get("tasks") or []):
            tasks.append({k: t.get(k) for k in
                          ("task_id", "bvid", "avid", "title", "up", "dur", "play",
                           "lan", "n_cues", "chars", "excerpt", "status",
                           "reason", "created_at", "sent_at", "rpid")})
        return {
            "enabled": bool(r.get("enabled")),
            "interval": int(r.get("interval") or 1800),
            "max_per_day": int(r.get("max_per_day") or 0),
            "source": r.get("source") or "hot",
            "keywords": list(r.get("keywords") or []),
            "stats": stats,
            "tasks": tasks,
            "loop": bool(r.get("loop")),
        }

    def _inbox_snapshot(self) -> dict:
        b = self.inbox
        return {
            "items": [dict(m) for m in (b.get("items") or [])],
            "replied": {str(k): dict(v) for k, v in (b.get("replied") or {}).items()},
            "drafts": {str(k): dict(v) for k, v in (b.get("drafts") or {}).items()},
            "stats": dict(b.get("stats") or {}),
        }


store = Store()
