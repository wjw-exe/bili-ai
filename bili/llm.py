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

import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / ".data"
CFG_FILE = DATA_DIR / "llm.json"
STATE_FILE = DATA_DIR / "llm_state.json"

DEFAULT_BASE = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"
DEFAULT_PROMPT = (
    "你在替 B 站账号主回复评论。对方回复了账号主或 @ 了账号主。"
    "请写一条不超过 40 字的回复：口语、自然、有回应感，不客套、不刷表情、"
    "不出现「AI/机器人」等自称，不加引号。只输出回复正文。"
)
TIMEOUT = 45


class LLMError(RuntimeError):
    """配置/网络/上游返回错误，message 直接可展示给用户"""


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

    # ---------------- 配置 ----------------

    def _load(self) -> dict:
        if self._cfg is not None:
            return self._cfg
        raw = _read_json(CFG_FILE, {})
        cfg = {
            "base_url": str(raw.get("base_url") or DEFAULT_BASE).strip(),
            "model": str(raw.get("model") or DEFAULT_MODEL).strip(),
            "api_key": str(raw.get("api_key") or ""),
            "prompt": str(raw.get("prompt") or DEFAULT_PROMPT),
            "auto_draft": bool(raw.get("auto_draft")),
            "auto_publish": bool(raw.get("auto_publish")),
            "interval": int(raw.get("interval") or 600),
            "max_per_day": int(raw.get("max_per_day") or 20),
            "temperature": float(raw.get("temperature") or 0.8),
        }
        if cfg["interval"] < 60:
            cfg["interval"] = 60
        self._cfg = cfg
        return cfg

    def _save(self) -> None:
        _write_json(CFG_FILE, self._load())

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
                "auto_draft": cfg["auto_draft"],
                "auto_publish": cfg["auto_publish"],
                "interval": cfg["interval"],
                "max_per_day": cfg["max_per_day"],
                "temperature": cfg["temperature"],
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
            if cfg["auto_publish"] and not cfg["api_key"]:
                cfg["auto_publish"] = False
                self._save()
                return "ERR_NEED_KEY: 没有 API Key，自动发送保持关闭"
            self._save()
            return "ok saved"

    # ---------------- 用量统计 ----------------

    def _state_load(self) -> dict:
        if self._state is not None and self._state.get("day") == _today():
            return self._state
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
        except OSError:
            pass

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
