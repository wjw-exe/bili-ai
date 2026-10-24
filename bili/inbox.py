"""消息收件箱：检测「别人回复我 / @我」，并记录手动的回复结果

只做两件事：
1. 检测：拉 msgfeed/reply + msgfeed/at，归一化后交给上层展示；
2. 记账：把「某条消息已经回复过」写进 .data/inbox.json，避免重复回同一条。

明确不做的事：不生成任何回复文案、不接大模型 / 不需要任何 API Key、
不自动定时发送。回复内容一律由调用方（MCP 客户端 AI 或网页输入框）传入。

状态文件结构::

    {"baseline_ids": ["1184712873918478", ...],
     "replied": {"1184712873918478": {"text": "...", "rpid": "...", "ts": 1761280000}}}

首次运行（文件不存在）时，把当前拉到的全部消息记为 baseline 并全部按「未回复」
可读展示 —— 不会发出任何东西。
"""

import json
import time
from pathlib import Path

from .store import store

DATA_DIR = Path(__file__).resolve().parent.parent / ".data"
INBOX_FILE = DATA_DIR / "inbox.json"
MAX_BASELINE = 2000      # baseline 只留最近这么多条
MAX_REPLIED = 500        # 已回复记录上限，超出按时间淘汰


def _norm_kind(kind: str) -> str:
    k = str(kind or "all").strip().lower()
    return k if k in ("all", "reply", "at") else "all"


class InboxView:
    def __init__(self, core) -> None:
        self.core = core
        self._state: dict | None = None

    # ---------------- 持久化 ----------------

    def _load(self) -> dict:
        if self._state is not None:
            return self._state
        raw: dict = {}
        if INBOX_FILE.exists():
            try:
                raw = json.loads(INBOX_FILE.read_text(encoding="utf-8")) or {}
            except (OSError, ValueError):
                raw = {}
        if not isinstance(raw, dict):
            raw = {}
        replied: dict[str, dict] = {}
        rep_raw = raw.get("replied")
        if isinstance(rep_raw, dict):
            for k, v in rep_raw.items():
                if isinstance(v, dict):
                    replied[str(k)] = {"text": str(v.get("text") or ""),
                                       "rpid": str(v.get("rpid") or ""),
                                       "ts": int(v.get("ts") or 0)}
        base = [str(x) for x in (raw.get("baseline_ids") or []) if str(x)]
        self._state = {"baseline_ids": base[-MAX_BASELINE:], "replied": replied}
        return self._state

    def _save(self) -> None:
        state = self._load()
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        data = {"baseline_ids": state["baseline_ids"][-MAX_BASELINE:],
                "replied": state["replied"]}
        try:
            INBOX_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                                  encoding="utf-8")
        except OSError:
            pass

    def first_run(self) -> bool:
        """还没有状态文件：首次调用，需要把当前消息记成 baseline"""
        return not INBOX_FILE.exists()

    # ---------------- 拉取 ----------------

    async def fetch(self, kind: str = "all") -> list[dict]:
        """拉取并归一化消息（reply + at 合并、按时间倒序、按 msg_id 去重）"""
        k = _norm_kind(kind)
        raw: list[dict] = []
        if k in ("all", "reply"):
            raw += await self.core.bili.msg_replies()
        if k in ("all", "at"):
            raw += await self.core.bili.msg_ats()
        seen, out = set(), []
        for m in sorted(raw, key=lambda x: x.get("ts") or 0, reverse=True):
            key = (m["kind"], m["msg_id"])
            if key in seen:
                continue
            seen.add(key)
            out.append(m)
        return out

    async def list(self, kind: str = "all", only_unreplied: bool = True,
                   limit: int = 20) -> list[dict]:
        """列出消息。

        kind: all | reply | at
        only_unreplied: 只要还没回复过的（status == new）
        返回条目在归一化字段基础上追加 status / fresh / replied_text / replied_rpid。
        """
        limit = max(1, min(100, int(limit)))
        state = self._load()
        err = ""
        try:
            raw = await self.fetch(kind)
        except Exception as e:                    # noqa: BLE001
            err = f"{type(e).__name__}: {e}"
            raw = []

        first = self.first_run()
        if err:
            # 拉取失败：沿用上次缓存，避免把界面刷成空白
            with store.lock:
                cached = list(store.inbox.get("items") or [])
            store.inbox["stats"].update({"err": err, "last_fetch": _now()})
            items = cached
        else:
            if first and raw:
                # 首次运行：当前全部消息记为 baseline，全部按未回复可读展示
                state["baseline_ids"] = [m["msg_id"] for m in raw][-MAX_BASELINE:]
                self._save()
            base = set(state["baseline_ids"])
            replied = state["replied"]
            items = []
            for m in raw:
                rep = replied.get(m["msg_id"])
                items.append(dict(
                    m,
                    status="replied" if rep else "new",
                    fresh=bool(state["baseline_ids"]) and m["msg_id"] not in base,
                    replied_text=(rep or {}).get("text") or "",
                    replied_rpid=(rep or {}).get("rpid") or "",
                ))
            with store.lock:
                store.inbox = {
                    "items": items,
                    "replied": dict(replied),
                    "stats": {"fetched": len(items), "replied": len(replied),
                              "last_fetch": _now(), "err": ""},
                }

        if only_unreplied:
            items = [m for m in items if m.get("status") != "replied"]
        return items[:limit]

    # ---------------- 记账 ----------------

    def mark_replied(self, msg_id: str, text: str, rpid: str = "") -> None:
        """记一条「已回复」，写盘并同步 store；不发起任何网络请求"""
        state = self._load()
        state["replied"][str(msg_id)] = {"text": str(text or "")[:1000],
                                         "rpid": str(rpid or ""),
                                         "ts": int(time.time())}
        if len(state["replied"]) > MAX_REPLIED:
            drop = sorted(state["replied"],
                          key=lambda k: state["replied"][k].get("ts") or 0)
            for k in drop[:len(state["replied"]) - MAX_REPLIED]:
                state["replied"].pop(k, None)
        self._save()
        with store.lock:
            store.inbox["replied"] = dict(state["replied"])
            stats = store.inbox.setdefault(
                "stats", {"fetched": 0, "replied": 0, "last_fetch": "", "err": ""})
            stats["replied"] = len(state["replied"])


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


_INBOXES: dict[int, InboxView] = {}


def get_inbox(core) -> InboxView:
    """按 Core 取实例（状态共享 store.inbox / .data/inbox.json）"""
    v = _INBOXES.get(id(core))
    if v is None:
        v = InboxView(core)
        _INBOXES[id(core)] = v
    return v
