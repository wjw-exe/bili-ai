"""可选的外部大模型接入：为「自动回复」生成文案草稿

三条硬边界（改代码时别放宽）：
1. **默认关闭**：没填 API Key 之前，本模块不产生任何网络请求。
2. **默认只产草稿**：草稿进评论面板的输入框，点「回复」才真正发；自动发送
   （auto_publish）必须显式打开，且受「每日上限 + 轮次内限速」双重约束。
3. **Key 只落本地**：存在项目 `.data/llm.json`（已在 .gitignore 里），
   接口只回掩码 `sk-****abcd`，明文永不出后端、不写日志。

不引入第三方依赖：用标准库 urllib 打 OpenAI 兼容的 `/chat/completions`，
因此任何兼容协议的服务（DeepSeek / 通义 / Moonshot / 本地 vLLM …）都能接。
"""

import functools
import json
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import modes

DATA_DIR = Path(__file__).resolve().parent.parent / ".data"
CFG_FILE = DATA_DIR / "llm.json"
STATE_FILE = DATA_DIR / "llm_state.json"
STATE_LOCK_FILE = DATA_DIR / "llm.state.lock"

DEFAULT_BASE = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"
DEFAULT_PROMPT = (
    "你在替 B 站账号主回复评论。对方回复了账号主或 @ 了账号主。"
    "请写一条不超过 40 字的回复：口语、自然、有回应感，不客套、不刷表情、"
    "不出现「AI/机器人」等自称，不加引号。只输出回复正文。"
)
DEFAULT_ROAM_PROMPT = (
    "你在替 B 站账号主看视频并留评论。你会收到视频标题、UP 主和字幕正文。"
    "请写一条不超过 60 字的评论：只针对字幕里真正讲到的具体内容发表看法（可提一句"
    "观点、疑问或补充），口语、自然、有信息量；不概括全文、不客套、不刷表情、"
    "不出现「AI/机器人」等自称，不加引号。只输出评论正文。"
)
TIMEOUT = 45


class LLMError(RuntimeError):
    """配置/网络/上游返回错误，message 直接可展示给用户"""


class _FileLock:
    """跨进程互斥（web_core / MCP core / CLI 各持一份用量状态时用）

    用量是「读文件-改-写」，两个进程同时 bump 会丢计数，进而让每日上限
    判断失真（自动发送超量）。用 O_CREAT|O_EXCL 建锁文件串行化，超时抛
    TimeoutError，陈旧锁（持锁进程已死）自动清理。
    """

    def __init__(self, path: Path, timeout: float = 3.0) -> None:
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
                try:
                    if time.time() - self.path.stat().st_mtime > 30:
                        self.path.unlink()
                        continue
                except OSError:
                    pass
                if time.time() - t0 > self.timeout:
                    raise TimeoutError("LOCK_TIMEOUT")
                time.sleep(0.02)

    def __exit__(self, *exc) -> None:
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None
        try:
            self.path.unlink()
        except OSError:
            pass


def _fresh_state(fn):
    """把「读-改-写用量」包进进程锁，并在锁内强制重读，避免跨进程互相覆盖"""
    @functools.wraps(fn)
    def wrapper(self, *a, **kw):
        try:
            with _FileLock(STATE_LOCK_FILE):
                self._st_mtime = -1.0                # 强制重读别进程写入的计数
                self._state = None
                return fn(self, *a, **kw)
        except TimeoutError:
            return None                              # 拿不到锁就放弃这次计数，不影响主流程
    return wrapper


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _read_json(p: Path, default: dict) -> dict:
    if not p.exists():
        return default
    try:
        raw = json.loads(p.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return default
    return raw if isinstance(raw, dict) else default


def _write_json(p: Path, data: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)
    try:                                     # 有 Key，尽量收紧权限（Windows 上是 no-op）
        p.chmod(0o600)
    except OSError:
        pass


def mask_key(key: str) -> str:
    k = str(key or "")
    if not k:
        return ""
    if len(k) <= 10:
        return "*" * len(k)
    return f"{k[:4]}{'*' * 6}{k[-4:]}"


class LLM:
    """配置 + 调用 + 用量统计；线程安全，进程内单例"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._cfg: dict | None = None
        self._state: dict | None = None
        self._cfg_mtime = -1.0
        self._st_mtime = -1.0

    # ---------------- 配置 ----------------

    @staticmethod
    def _mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0

    def _load(self) -> dict:
        # 别的进程（web 后台 / MCP 进程 / CLI）改过配置就重新读，避免各进程各自缓存
        mt = self._mtime(CFG_FILE)
        if self._cfg is not None and mt == self._cfg_mtime:
            return self._cfg
        self._cfg_mtime = mt
        raw = _read_json(CFG_FILE, {})
        cfg = {
            "base_url": str(raw.get("base_url") or DEFAULT_BASE).strip(),
            "model": str(raw.get("model") or DEFAULT_MODEL).strip(),
            "api_key": str(raw.get("api_key") or ""),
            "prompt": str(raw.get("prompt") or DEFAULT_PROMPT),
            "roam_prompt": str(raw.get("roam_prompt") or DEFAULT_ROAM_PROMPT),
            "auto_draft": bool(raw.get("auto_draft")),
            "auto_publish": bool(raw.get("auto_publish")),
            "interval": int(raw.get("interval") or 600),
            "max_per_day": int(raw.get("max_per_day") or 20),
            "temperature": float(raw.get("temperature") or 0.8),
            # 能力模式：weak=只能发评论，full=除发视频外全功能
            "mode": modes.normalize(raw.get("mode")),
        }
        if cfg["interval"] < 60:
            cfg["interval"] = 60
        self._cfg = cfg
        return cfg

    def _save(self) -> None:
        _write_json(CFG_FILE, self._load())
        self._cfg_mtime = self._mtime(CFG_FILE)

    def raw(self) -> dict:
        with self._lock:
            return dict(self._load())

    def public(self) -> dict:
        """给前端的视图：不含 Key 明文"""
        with self._lock:
            cfg = self._load()
            st = self._state_load()
            return {
                "configured": bool(cfg["api_key"]),
                "base_url": cfg["base_url"],
                "model": cfg["model"],
                "key_masked": mask_key(cfg["api_key"]),
                "prompt": cfg["prompt"],
                "roam_prompt": cfg["roam_prompt"],
                "auto_draft": cfg["auto_draft"],
                "auto_publish": cfg["auto_publish"],
                "interval": cfg["interval"],
                "max_per_day": cfg["max_per_day"],
                "temperature": cfg["temperature"],
                "mode": cfg["mode"],
                "mode_label": modes.LABELS[cfg["mode"]],
                "mode_summary": modes.SUMMARIES[cfg["mode"]],
                "capabilities": modes.capabilities(cfg["mode"]),
                "stats": st,
            }

    def configure(self, **kw) -> str:
        """改配置。api_key 为 None/缺省 = 保持不变；clear_key=True 清空；"""
        with self._lock:
            cfg = self._load()
            if kw.get("clear_key"):
                cfg["api_key"] = ""
                cfg["auto_draft"] = False
                cfg["auto_publish"] = False
            key = kw.get("api_key")
            if isinstance(key, str) and key.strip():
                cfg["api_key"] = key.strip()
            if "base_url" in kw and str(kw["base_url"] or "").strip():
                cfg["base_url"] = str(kw["base_url"]).strip().rstrip("/")
            if "model" in kw and str(kw["model"] or "").strip():
                cfg["model"] = str(kw["model"]).strip()
            if "prompt" in kw and str(kw.get("prompt") or "").strip():
                cfg["prompt"] = str(kw["prompt"]).strip()
            if "roam_prompt" in kw and str(kw.get("roam_prompt") or "").strip():
                cfg["roam_prompt"] = str(kw["roam_prompt"]).strip()
            for k in ("auto_draft", "auto_publish"):
                if k in kw and kw[k] is not None:
                    cfg[k] = bool(kw[k])
            if kw.get("interval") is not None:
                try:
                    cfg["interval"] = max(60, int(float(kw["interval"])))
                except (TypeError, ValueError):
                    pass
            if kw.get("max_per_day") is not None:
                try:
                    cfg["max_per_day"] = max(0, int(float(kw["max_per_day"])))
                except (TypeError, ValueError):
                    pass
            if kw.get("temperature") is not None:
                try:
                    cfg["temperature"] = min(2.0, max(0.0, float(kw["temperature"])))
                except (TypeError, ValueError):
                    pass
            if kw.get("mode") is not None and str(kw["mode"]).strip():
                raw_mode = str(kw["mode"]).strip().lower()
                if raw_mode not in modes.MODES:
                    return (f"ERR_BAD_MODE: {raw_mode}  可选 "
                            + "/".join(modes.MODES))
                cfg["mode"] = raw_mode
            if cfg["auto_publish"] and not cfg["api_key"]:
                cfg["auto_publish"] = False
                self._save()
                return "ERR_NEED_KEY: 没有 API Key，自动发送保持关闭"
            self._save()
            return "ok saved"

    # ---------------- 用量统计 ----------------

    def _state_load(self) -> dict:
        mt = self._mtime(STATE_FILE)
        if (self._state is not None and self._state.get("day") == _today()
                and mt == self._st_mtime):
            return self._state
        self._st_mtime = mt
        raw = _read_json(STATE_FILE, {})
        st = {
            "day": raw.get("day") or _today(),
            "sent_today": int(raw.get("sent_today") or 0),
            "drafted_today": int(raw.get("drafted_today") or 0),
            "total_sent": int(raw.get("total_sent") or 0),
            "total_drafted": int(raw.get("total_drafted") or 0),
            "last_ok": str(raw.get("last_ok") or ""),
            "last_error": str(raw.get("last_error") or ""),
            "last_run": str(raw.get("last_run") or ""),
            "last_model": str(raw.get("last_model") or ""),
        }
        if st["day"] != _today():
            st.update(day=_today(), sent_today=0, drafted_today=0)
        self._state = st
        return st

    def _state_save(self) -> None:
        try:
            _write_json(STATE_FILE, self._state_load())
            self._st_mtime = self._mtime(STATE_FILE)
        except OSError:
            pass

    @_fresh_state
    def bump(self, kind: str, n: int = 1) -> None:
        with self._lock:
            st = self._state_load()
            if st["day"] != _today():
                st.update(day=_today(), sent_today=0, drafted_today=0)
            if kind == "sent":
                st["sent_today"] += n
                st["total_sent"] += n
            else:
                st["drafted_today"] += n
                st["total_drafted"] += n
            self._state_save()

    @_fresh_state
    def note(self, ok: bool, msg: str = "") -> None:
        with self._lock:
            st = self._state_load()
            st["last_run"] = _now()
            st["last_model"] = self._load()["model"]
            if ok:
                st["last_ok"] = _now()
                st["last_error"] = ""
            else:
                st["last_error"] = str(msg or "")[:300]
            self._state_save()

    # ---------------- 调用 ----------------

    def quota_ok(self) -> bool:
        cfg = self._load()
        st = self._state_load()
        cap = cfg["max_per_day"]
        return not cap or st["sent_today"] < cap

    def _endpoint(self, base: str) -> str:
        b = (base or DEFAULT_BASE).rstrip("/")
        if b.endswith("/chat/completions"):
            return b
        return b + "/chat/completions"

    def chat(self, messages: list[dict], max_tokens: int = 256) -> str:
        """同步调用（调用方用 asyncio.to_thread 包一层），返回正文字符串"""
        cfg = self._load()
        if not cfg["api_key"]:
            raise LLMError("还没有填 API Key")
        body = json.dumps({
            "model": cfg["model"],
            "messages": messages,
            "temperature": cfg["temperature"],
            "max_tokens": max_tokens,
            "stream": False,
        }, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self._endpoint(cfg["base_url"]), data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {cfg['api_key']}",
                     "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                raw = r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:300]
            except Exception:                # noqa: BLE001
                pass
            raise LLMError(f"HTTP {e.code}：{detail or e.reason}") from None
        except urllib.error.URLError as e:
            raise LLMError(f"连不上接口：{e.reason}") from None
        except TimeoutError:
            raise LLMError("请求超时")
        try:
            data = json.loads(raw)
        except ValueError:
            raise LLMError(f"返回不是 JSON：{raw[:200]}") from None
        if isinstance(data, dict) and data.get("error"):
            err = data["error"]
            raise LLMError(str(err.get("message") if isinstance(err, dict) else err)[:300])
        try:
            msg = (data.get("choices") or [])[0].get("message") or {}
        except (IndexError, AttributeError):
            raise LLMError(f"返回格式不认识：{raw[:200]}") from None
        text = str(msg.get("content") or "").strip()
        # 部分兼容服务把思考内容放 reasoning_content，正文为空时兜底用它
        if not text:
            text = str(msg.get("reasoning_content") or "").strip()
        if not text:
            raise LLMError("模型返回了空内容")
        return text

    def draft(self, item: dict) -> str:
        """按一条收件箱消息生成回复文案（同步，最长 200 字）"""
        cfg = self._load()
        his = str(item.get("his_msg") or "").strip()
        my = str(item.get("my_msg") or "").strip()
        kind = "对方 @ 了你" if item.get("kind") == "at" else "对方回复了你的评论"
        title = str(item.get("title") or "").strip()
        user = [f"{kind}。" + (f"视频标题：{title}" if title else ""),
                f"对方内容：{his or '(空)'}"]
        if my:
            user.append(f"你原来的评论：{my}")
        user.append("请写一条回复。")
        text = self.chat([{"role": "system", "content": cfg["prompt"]},
                          {"role": "user", "content": "\n".join(x for x in user if x)}])
        text = text.strip().strip('"“”\'').replace("\n", " ").strip()
        return text[:200]

    def draft_for_roam(self, task: dict) -> str:
        """按一条巡览任务（视频 + 字幕）生成评论文案（同步，最长 200 字）

        巡览和「回复评论」语境不同：这里模型没见过原评论，只能靠字幕判断视频讲了什么，
        所以单独用 roam_prompt，输入里带上标题 / UP / 时长 / 播放量做上下文。
        """
        cfg = self._load()
        title = str(task.get("title") or "").strip()
        up = str(task.get("up") or "").strip()
        dur = int(task.get("dur") or 0)
        play = int(task.get("play") or 0)
        body = " ".join(str(task.get("text_head") or task.get("excerpt") or "").split())
        if not body:
            raise LLMError("该任务没有字幕正文，无法拟稿")
        head = [f"视频标题：{title or '(未知)'}"]
        if up:
            head.append(f"UP 主：{up}")
        if dur:
            head.append(f"时长：{dur // 60} 分 {dur % 60} 秒")
        if play:
            head.append(f"播放量：{play}")
        user = "\n".join(head) + "\n--- 字幕正文（节选）---\n" + body[:1800] + \
            "\n--- 字幕结束 ---\n请写一条评论。"
        text = self.chat([{"role": "system", "content": cfg["roam_prompt"]},
                          {"role": "user", "content": user}], max_tokens=240)
        text = text.strip().strip('"“”\'')
        for pre in ("评论：", "评论:", "回复："):
            if text.startswith(pre):
                text = text[len(pre):].strip()
        text = " ".join(text.split())
        return text[:200]

    def test(self) -> dict:
        """真打一次接口，验证 Key / 地址 / 模型是否可用"""
        t0 = time.time()
        try:
            reply = self.chat([{"role": "user", "content": "只回两个字：在的"}],
                              max_tokens=16)
        except LLMError as e:
            self.note(False, str(e))
            return {"ok": False, "msg": str(e), "ms": int((time.time() - t0) * 1000)}
        self.note(True)
        return {"ok": True, "msg": "连接正常", "sample": reply[:60],
                "model": self._load()["model"],
                "ms": int((time.time() - t0) * 1000)}


_LLM: LLM | None = None


def get_llm(owner=None) -> LLM:
    """全局单例：一个进程一份配置、一份用量统计（owner 仅为签名兼容）"""
    global _LLM
    if _LLM is None:
        _LLM = LLM()
    return _LLM
