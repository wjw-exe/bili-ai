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
        self.account: dict = {"login": False, "uname": "", "mid": 0, "avatar": ""}

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
                "account": dict(self.account),
                "log": list(self.log),
                "uptime": round(time.time() - self.started_at, 1),
            }


store = Store()
