"""自动回复：拉「回复我 / @我」的未回复消息 → 调外部模型拟文案 →（可选）自动发

安全默认（改动前先看这三条）:
1. `auto_draft` / `auto_publish` 默认都是关的；关着的时候后台轮询什么都不做。
2. 草稿只写到 `.data/reply_drafts.json` 并同步给评论面板，**不会自动发**；
   只有 `auto_publish=True` 且没超每日上限时才真发。
3. 一轮内每次调模型至少间隔 `_GAP` 秒，避免把对方接口打挂；单轮最多处理
   `limit` 条新消息。

发出去的动作复用 `core.execute("reply")`，与手动点「回复」走同一条链路
（同样会记账、同样不可撤回）。
"""

import asyncio
import time
from pathlib import Path

from .llm import get_llm
from .store import store

DATA_DIR = Path(__file__).resolve().parent.parent / ".data"
DRAFTS_FILE = DATA_DIR / "reply_drafts.json"
MAX_DRAFTS = 200
_GAP = 1.5                      # 两次调模型之间的最小间隔（秒）


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _load_drafts() -> dict:
    import json
    if not DRAFTS_FILE.exists():
        return {}
    try:
        raw = json.loads(DRAFTS_FILE.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out = {}
    for k, v in raw.items():
        if isinstance(v, dict) and str(v.get("text") or "").strip():
            out[str(k)] = {"text": str(v["text"])[:1000],
                           "ts": int(v.get("ts") or 0),
                           "model": str(v.get("model") or "")}
    return out


def _save_drafts(drafts: dict) -> None:
    import json
    if len(drafts) > MAX_DRAFTS:
        for k in sorted(drafts, key=lambda x: drafts[x].get("ts") or 0)[:len(drafts) - MAX_DRAFTS]:
            drafts.pop(k, None)
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = DRAFTS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(drafts, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(DRAFTS_FILE)
    except OSError:
        pass
    with store.lock:
        store.inbox["drafts"] = {k: dict(v) for k, v in drafts.items()}


class AutoReply:
    def __init__(self, core) -> None:
        self.core = core
        self.llm = get_llm()
        self._busy = False
        self.last: dict = {}
        with store.lock:
            store.inbox["drafts"] = _load_drafts()

    # ---------------- 单条：生成草稿 / 发送 ----------------

    def _item(self, msg_id: str) -> dict | None:
        with store.lock:
            for m in (store.inbox.get("items") or []):
                if str(m.get("msg_id")) == str(msg_id):
                    return dict(m)
        return None

    async def draft_one(self, msg_id: str) -> dict:
        """给一条消息生草稿（不发送）。返回 {ok, text, msg}"""
        item = self._item(msg_id)
        if not item:
            return {"ok": False, "msg": "没找到这条消息，先点「刷新」拉一次"}
        if item.get("status") == "replied":
            return {"ok": False, "msg": "这条已经回复过了"}
        if not self.llm.public()["configured"]:
            return {"ok": False, "msg": "先填 API Key"}
        try:
            text = await asyncio.to_thread(self.llm.draft, item)
        except Exception as e:                   # noqa: BLE001
            self.llm.note(False, str(e))
            self.last = {"err": str(e), "at": _now()}
            return {"ok": False, "msg": str(e)}
        self.llm.note(True)
        self.llm.bump("draft")
        drafts = _load_drafts()
        drafts[str(msg_id)] = {"text": text, "ts": int(time.time()),
                               "model": self.llm.raw()["model"]}
        _save_drafts(drafts)
        return {"ok": True, "text": text, "msg": "ok"}

    async def send_one(self, msg_id: str, text: str) -> dict:
        """真发一条回复（不可撤回，调用方必须先拿到用户确认）"""
        text = str(text or "").strip()
        if not text:
            return {"ok": False, "msg": "回复内容不能为空"}
        if not self.llm.quota_ok():
            return {"ok": False, "msg": "已到今日自动发送上限"}
        out = await self.core.execute("reply", {"msg_id": str(msg_id), "message": text})
        ok = out.startswith("ok")
        if ok:
            self.llm.bump("sent")
            drafts = _load_drafts()
            drafts.pop(str(msg_id), None)
            _save_drafts(drafts)
        return {"ok": ok, "msg": out}

    # ---------------- 一轮批量 ----------------

    async def run_once(self, limit: int = 5) -> dict:
        if self._busy:
            return {"ok": False, "err": "上一轮还没跑完"}
        cfg = self.llm.raw()
        if not cfg["api_key"]:
            return {"ok": False, "err": "未配置 API Key"}
        if not (cfg["auto_draft"] or cfg["auto_publish"]):
            return {"ok": True, "skipped": "自动回复未开启", "drafted": 0, "sent": 0}
        self._busy = True
        limit = max(1, min(20, int(limit)))
        res = {"ok": True, "drafted": 0, "sent": 0, "err": "", "items": []}
        try:
            items = await self.core.inbox.list("all", True, 20)
            drafts = _load_drafts()
            for m in items:
                mid = str(m.get("msg_id") or "")
                if not mid or m.get("status") == "replied":
                    continue
                d = drafts.get(mid)
                if not d:
                    if res["drafted"] >= limit:
                        break
                    try:
                        text = await asyncio.to_thread(self.llm.draft, m)
                    except Exception as e:       # noqa: BLE001
                        self.llm.note(False, str(e))
                        res["err"] = str(e)
                        break
                    self.llm.note(True)
                    self.llm.bump("draft")
                    drafts[mid] = {"text": text, "ts": int(time.time()),
                                   "model": cfg["model"]}
                    _save_drafts(drafts)
                    res["drafted"] += 1
                    res["items"].append({"msg_id": mid, "step": "draft", "text": text})
                    await asyncio.sleep(_GAP)
                    d = drafts[mid]
                if cfg["auto_publish"] and self.llm.quota_ok():
                    out = await self.core.execute(
                        "reply", {"msg_id": mid, "message": d["text"]})
                    if out.startswith("ok"):
                        self.llm.bump("sent")
                        res["sent"] += 1
                        res["items"].append({"msg_id": mid, "step": "sent"})
                        drafts.pop(mid, None)
                        _save_drafts(drafts)
                        await asyncio.sleep(_GAP)
                    else:
                        res["err"] = out[:200]
                        self.llm.note(False, out[:200])
                        break
            self.llm.note(not res["err"], res["err"])
        except Exception as e:                   # noqa: BLE001
            res["ok"] = False
            res["err"] = f"{type(e).__name__}: {e}"
            self.llm.note(False, res["err"])
        finally:
            self._busy = False
            self.last = dict(res, at=_now())
        return res

    def status(self) -> dict:
        pub = self.llm.public()
        pub["last"] = dict(self.last)
        pub["busy"] = bool(self._busy)
        with store.lock:
            pub["drafts"] = len(store.inbox.get("drafts") or {})
        return pub


_AR: dict[int, AutoReply] = {}
_TASKS: dict[int, asyncio.Task] = {}


def get_autoreply(core) -> AutoReply:
    v = _AR.get(id(core))
    if v is None:
        v = AutoReply(core)
        _AR[id(core)] = v
    return v


def start_loop(ar: AutoReply) -> bool:
    """起后台轮询（幂等）。间隔取配置里的 interval；关着的时候空转不做请求"""
    key = id(ar)
    t = _TASKS.get(key)
    if t and not t.done():
        return False

    async def loop() -> None:
        while True:
            try:
                cfg = ar.llm.raw()
                if cfg["api_key"] and (cfg["auto_draft"] or cfg["auto_publish"]):
                    await ar.run_once(5)
            except asyncio.CancelledError:
                raise
            except Exception:                    # noqa: BLE001  轮询异常不拖垮后台
                pass
            try:
                await asyncio.sleep(max(60, ar.llm.raw()["interval"]))
            except asyncio.CancelledError:
                raise

    _TASKS[key] = asyncio.get_event_loop().create_task(loop())
    return True
