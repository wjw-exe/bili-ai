"""智能控制台：强模型通过 12 个复合工具真正操作 bili-ai（v0.3 顶级模型版）

和 v0.2 的区别：
- 工具从 14 个原子操作换成 tools.py 的 12 个复合工具（内部收敛 37 个 cmd）；
- 大对象走 refstore，模型只看 summary + ref，上下文不再被评论/字幕撑爆；
- 历史做裁剪（system + 最近 24 条 + 结果压缩），不再全量 hist.append；
- 同一轮多个只读工具并行（≤5），写操作串行且必须用户确认；
- 结果自证：每个工具返回带 verify 结论，提示词不再堆"不要幻觉"这类软约束。

四条硬边界（改代码时别放宽）：
1. **只在强模式下开门**：mode 不是 full 时 ready() 直接不可用；
2. **写操作必确认**：publish_batch / roam send / library 写，一律先回确认卡；
3. **执行统一走 Core.execute(actor="ai")**：守门、审计、每日上限沿用原有那一处；
4. **Key 只落本地**：`.data/agent.json`，接口只回掩码；免 Key 的免费接口除外。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from . import events, modes, tools as T

DATA_DIR = Path(__file__).resolve().parent.parent / ".data"
CFG_FILE = DATA_DIR / "agent.json"

DEFAULT_BASE = "https://text.pollinations.ai/openai"
DEFAULT_MODEL = "openai"
TIMEOUT = 120
CONFIRM_TTL = 300
HIST_MAX = 24
RESULT_MAX = 3000

# 允许不带 Key 的免费 OpenAI 兼容接口（测试用）
KEYLESS_HOSTS = ("text.pollinations.ai", "pollinations.ai")


class AgentError(RuntimeError):
    """配置 / 网络 / 上游错误，message 直接可展示"""


_PENDING: dict[str, dict] = {}
_DEFAULTS = {"base_url": DEFAULT_BASE, "model": DEFAULT_MODEL, "api_key": "",
             "temperature": 0.3, "max_steps": 12}

SYSTEM = """你是 bili-ai 的操作员，用工具真正操作这个 B 站客户端。

现场：{scene}

工作方式：
- 复合工具优先：先 account_digest 看态势，再按需 search_videos / inspect_video；
  长正文只在必要时用 read_ref 分页取，别一次读几千行。
- 要发东西先 draft_batch 写草稿批次，再 publish_batch 发布；系统会让用户确认，
  你不用反复追问，也**不要自己编 token**。
- 每个工具返回都带 verify.verdict：pass 才算真做成，fail 就按 err 说真话、别粉饰。
- 工具返回 ok=false 时先看 err 再决定换工具还是停；同一个工具失败两次就停手说明原因。
- 最后用中文两三句话交代：做了什么、真实结果、还有什么没做。不要输出 JSON。"""


# ---------------- 配置 ----------------

def load_cfg() -> dict:
    raw: dict = {}
    try:
        if CFG_FILE.exists():
            raw = json.loads(CFG_FILE.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        raw = {}
    cfg = dict(_DEFAULTS)
    for k in _DEFAULTS:
        if k in raw and raw[k] is not None:
            cfg[k] = raw[k]
    cfg["api_key"] = str(cfg.get("api_key") or "")
    try:
        cfg["max_steps"] = max(1, min(16, int(cfg.get("max_steps") or 12)))
    except (TypeError, ValueError):
        cfg["max_steps"] = 12
    try:
        cfg["temperature"] = float(cfg.get("temperature") or 0.3)
    except (TypeError, ValueError):
        cfg["temperature"] = 0.3
    return cfg


def save_cfg(patch: dict) -> dict:
    cfg = load_cfg()
    for k in ("base_url", "model", "api_key"):
        if k in patch and patch[k] is not None:
            cfg[k] = str(patch[k]).strip()
    if "temperature" in patch and patch["temperature"] not in (None, ""):
        try:
            cfg["temperature"] = max(0.0, min(2.0, float(patch["temperature"])))
        except (TypeError, ValueError):
            pass
    if "max_steps" in patch and patch["max_steps"] not in (None, ""):
        try:
            cfg["max_steps"] = max(1, min(16, int(patch["max_steps"])))
        except (TypeError, ValueError):
            pass
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CFG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return public_cfg(cfg)


def public_cfg(cfg: dict | None = None) -> dict:
    c = dict(cfg or load_cfg())
    key = str(c.get("api_key") or "")
    c["key_masked"] = (key[:3] + "****" + key[-4:]) if len(key) > 8 else ("已设置" if key else "")
    c["has_key"] = bool(key)
    c["keyless"] = _keyless(c.get("base_url") or "")
    c["tools"] = T.TOOL_NAMES
    c.pop("api_key", None)
    return c


def _keyless(base: str) -> bool:
    b = str(base or "").lower()
    return any(h in b for h in KEYLESS_HOSTS)


def ready() -> tuple[bool, str]:
    mode = modes.current()
    if mode != modes.MODE_FULL:
        return False, "当前是弱模式（本地小模型），智能控制台已关闭。切到强模式才启用。"
    cfg = load_cfg()
    base = str(cfg.get("base_url") or "")
    if not cfg["api_key"] and not _keyless(base):
        return False, "还没填强模型 API Key，填完才能用智能控制台。"
    if ("127.0.0.1" in base or "localhost" in base) and not cfg["api_key"]:
        return False, "接口地址指向本机模型，智能控制台需要远端强模型。"
    return True, ""


def scene(store) -> str:
    try:
        with store.lock:
            cur = dict(store.current or {})
            acct = dict(store.account or {})
        title = cur.get("title") or ""
        bvid = cur.get("bvid") or ""
        who = acct.get("uname") or ""
    except Exception:                            # noqa: BLE001
        title, bvid, who = "", "", ""
    parts = [f"账号「{who}」已登录" if who else "账号未登录（只能只读，写操作会失败）",
             f"当前视频：{title}（{bvid}）" if bvid else "当前没有打开的视频",
             f"AI 模式：{modes.LABELS.get(modes.current(), modes.current())}"]
    return "；".join(parts)


# ---------------- 模型调用 ----------------

def _retry_wait(detail: str) -> int:
    """从限流响应里读出建议等待秒数，兜底 45s，上限 95s。"""
    m = re.search(r"retry after\s*(\d+)", detail or "", re.I)
    if m:
        try:
            return max(2, min(95, int(m.group(1)) + 3))
        except Exception:                        # noqa: BLE001
            pass
    return 45


def _post(cfg: dict, messages: list[dict], tools: list | None) -> tuple[dict, dict]:
    """返回 (message, usage)。"""
    base = (cfg.get("base_url") or DEFAULT_BASE).rstrip("/")
    url = base if base.endswith("/chat/completions") else base + "/chat/completions"
    body: dict = {"model": cfg.get("model") or DEFAULT_MODEL, "messages": messages,
                  "temperature": cfg.get("temperature", 0.3), "stream": False}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if cfg.get("api_key"):
        headers["Authorization"] = f"Bearer {cfg['api_key']}"
    req = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                 method="POST", headers=headers)
    raw = ""
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                raw = r.read().decode("utf-8", "replace")
            break
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:400]
            except Exception:                    # noqa: BLE001
                pass
            # 免费接口限流 / 过载：等一会儿再试，别直接把 402 甩给用户
            if e.code in (402, 429, 503) and attempt < 2:
                time.sleep(_retry_wait(detail))
                continue
            if e.code in (400, 404, 422) and ("tool" in detail.lower() or "function" in detail.lower()):
                raise AgentError("这个模型不支持工具调用，换 deepseek-chat / gpt-oss / gpt-4o 之类") from None
            raise AgentError(f"模型接口 HTTP {e.code}：{detail or e.reason}") from None
        except urllib.error.URLError as e:
            raise AgentError(f"连不上模型接口：{e.reason}") from None
        except TimeoutError:
            raise AgentError(f"模型接口超时（{TIMEOUT}s）")
    try:
        obj = json.loads(raw)
    except ValueError:
        raise AgentError(f"模型返回不是 JSON：{raw[:200]}") from None
    if isinstance(obj, dict) and obj.get("error"):
        err = obj["error"]
        msg = err.get("message") if isinstance(err, dict) else str(err)
        raise AgentError(str(msg)[:300])
    try:
        msg = (obj.get("choices") or [])[0].get("message") or {}
    except (IndexError, AttributeError):
        raise AgentError(f"模型返回格式不认识：{raw[:200]}") from None
    return msg, (obj.get("usage") or {})


def _args(raw: str) -> dict:
    try:
        v = json.loads(raw or "{}")
        return v if isinstance(v, dict) else {}
    except ValueError:
        return {}


def _clip(messages: list[dict]) -> list[dict]:
    """历史裁剪：保留 system + 最近 HIST_MAX 条，工具消息压到 RESULT_MAX。"""
    if len(messages) <= HIST_MAX + 1:
        return [messages[0]] + [dict(m, content=str(m.get("content") or "")[:RESULT_MAX])
                                for m in messages[1:]] if messages else []
    head, tail = messages[0], messages[1:][-HIST_MAX:]
    out = [head]
    for m in tail:
        c = str(m.get("content") or "")[:RESULT_MAX]
        out.append({**m, "content": c})
    return out


def _preview(name: str, args: dict) -> str:
    if name == "publish_batch":
        return f"发布批次 {args.get('batch_ref', '')}（最多 {args.get('limit', 5)} 条，对外可见）"
    if name == "roam_ops":
        a = args.get("action")
        return {"send": f"发送巡览任务 {args.get('task_id', '')} 的评论（对外可见）",
                "on": "开启自动巡览（会持续自动评论）",
                "off": "关闭自动巡览"}.get(a, f"巡览操作 {a}")
    if name == "library":
        return f"资料库 {args.get('action')} {args.get('key', '')}"
    return name


def _need_confirm(name: str, args: dict) -> bool:
    if name == "publish_batch":
        return True
    if name == "roam_ops":
        return str(args.get("action") or "") in ("send", "on", "off")
    if name == "library":
        return str(args.get("action") or "") in ("put", "del")
    return False


async def _one(core, store, name: str, args: dict, *, confirmed: bool) -> dict:
    t0 = time.time()
    if name not in T.TOOL_NAMES:
        return {"ok": False, "tool": name, "summary": "", "err": f"没有 {name} 这个工具"}
    env = await T.call(core, store, name, args, confirmed=confirmed)
    env["_ms"] = int((time.time() - t0) * 1000)
    return env


def _payload(env: dict) -> str:
    """喂回模型的工具结果：只留决策必需字段。"""
    slim = {k: env.get(k) for k in ("ok", "summary", "ref", "count", "fail",
                                    "verify", "err", "hint", "next_offset",
                                    "total_lines", "remaining") if env.get(k) not in (None, "", [])}
    if env.get("items") and env.get("tool") in ("inbox_brief", "publish_batch", "verify"):
        slim["items"] = env["items"][:8]
    txt = json.dumps(slim, ensure_ascii=False)
    return txt[:RESULT_MAX]


async def run(core, store, messages: list[dict], auto_confirm: bool = False) -> dict:
    ok, why = ready()
    if not ok:
        raise AgentError(why)
    cfg = load_cfg()
    hist = [{"role": "system", "content": SYSTEM.format(scene=scene(store))}]
    for m in (messages or [])[-12:]:
        role = "assistant" if m.get("role") == "assistant" else "user"
        text = str(m.get("content") or "")[:2000]
        if text:
            hist.append({"role": role, "content": text})

    steps: list[dict] = []
    pending: list[dict] = []
    say = ""
    tok_in = tok_out = 0

    for _ in range(cfg["max_steps"]):
        msg, usage = await asyncio.to_thread(_post, cfg, _clip(hist), T.schema())
        tok_in += int(usage.get("prompt_tokens") or 0)
        tok_out += int(usage.get("completion_tokens") or 0)
        calls = msg.get("tool_calls") or []
        say = str(msg.get("content") or "").strip() or say
        if not calls:
            break
        hist.append({"role": "assistant", "content": msg.get("content") or "",
                     "tool_calls": calls})

        # 同一轮：只读并行（≤5），写操作排队等确认
        runnable: list[tuple[str, str, str, dict]] = []
        for tc in calls:
            fn = tc.get("function") or {}
            name = str(fn.get("name") or "")
            args = _args(fn.get("arguments") or "{}")
            tid = str(tc.get("id") or uuid.uuid4().hex[:12])
            if name not in T.TOOL_NAMES:
                hist.append({"role": "tool", "tool_call_id": tid,
                             "content": f'{{"ok":false,"err":"没有 {name} 这个工具"}}'})
                continue
            if _need_confirm(name, args) and not auto_confirm:
                pending.append({"name": name, "args": args, "preview": _preview(name, args)})
                hist.append({"role": "tool", "tool_call_id": tid,
                             "content": '{"ok":false,"err":"NEED_CONFIRM",'
                                        '"summary":"已排队，等用户点确认后才会真正执行"}'})
                continue
            if len(runnable) < 5:
                runnable.append((name, tid, name, args))
            else:
                env = await _one(core, store, name, args, confirmed=auto_confirm)
                hist.append({"role": "tool", "tool_call_id": tid, "content": _payload(env)})

        if runnable:
            reads = [r for r in runnable if not T.is_write(r[0]) or
                     (r[0] == "roam_ops" and str(r[3].get("action")) in
                      ("status", "list", "next", "draft", "draft_set", "check"))]
            writes = [r for r in runnable if r not in reads]
            if reads:
                envs = await asyncio.gather(*[_one(core, store, n, a, confirmed=False)
                                              for n, _t, _n, a in reads])
                for (n, tid, _n, a), env in zip(reads, envs):
                    steps.append({"name": n, "args": a, "ok": bool(env.get("ok")),
                                  "verdict": (env.get("verify") or {}).get("verdict"),
                                  "text": (env.get("summary") or env.get("err") or "")[:400],
                                  "ref": env.get("ref") or ""})
                    hist.append({"role": "tool", "tool_call_id": tid, "content": _payload(env)})
            for n, tid, _n, a in writes:
                env = await _one(core, store, n, a, confirmed=auto_confirm)
                steps.append({"name": n, "args": a, "ok": bool(env.get("ok")),
                              "verdict": (env.get("verify") or {}).get("verdict"),
                              "text": (env.get("summary") or env.get("err") or "")[:400],
                              "ref": env.get("ref") or ""})
                hist.append({"role": "tool", "tool_call_id": tid, "content": _payload(env)})
                await asyncio.sleep(1.0)

    events.emit("agent_run", ok=None, model=cfg.get("model", ""),
                tokens_in=tok_in, tokens_out=tok_out,
                steps=len(steps), pending=len(pending))
    if pending:
        now = time.time()
        for k in [k for k, v in _PENDING.items() if v["exp"] < now]:
            _PENDING.pop(k, None)
        token = uuid.uuid4().hex[:16]
        _PENDING[token] = {"pending": pending, "exp": now + CONFIRM_TTL, "store": store}
        return {"ok": True, "kind": "confirm", "token": token, "pending": pending,
                "preview": "；".join(p["preview"] for p in pending),
                "say": say or "这几步要对外发出去，确认一下：",
                "steps": steps}

    return {"ok": True, "kind": "say", "say": say or "做完了。", "steps": steps,
            "usage": {"in": tok_in, "out": tok_out}}


async def confirm(core, token: str) -> dict:
    item = _PENDING.pop(str(token or ""), None)
    if not item or float(item.get("exp") or 0) < time.time():
        return {"ok": False, "say": "这条确认已经过期，重新说一遍。"}
    outs = []
    st = item.get("store")
    for p in item["pending"]:
        env = await _one(core, st, p["name"], p["args"], confirmed=True)
        outs.append({"name": p["name"], "preview": p.get("preview"),
                     "ok": bool(env.get("ok")),
                     "verdict": (env.get("verify") or {}).get("verdict"),
                     "text": (env.get("summary") or env.get("err") or "")[:400]})
        await asyncio.sleep(1.0)
    bad = [o for o in outs if not o["ok"]]
    say = ("；".join(f"{o['name']} 失败：{(o['text'].splitlines() or [''])[0]}" for o in bad)
           if bad else "已执行：" + "；".join(o["preview"] or o["name"] for o in outs))
    events.emit("agent_confirm", ok=not bad, n=len(outs), fail=len(bad))
    return {"ok": True, "kind": "done", "say": say, "items": outs}
