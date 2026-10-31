"""AI 能力模式：同一条命令总线，按「自己配 API 的 AI」的身份限权。

两种模式（存在 `.data/llm.json` 的 `mode` 字段，默认 weak）：

- weak 弱模式：只能发评论。对外唯一写动作是「发评论 / 回复评论」
  （comment / reply / roam_comment / roam_send），其余读、本地草稿、
  本地任务状态照常可用；点赞、投币、收藏、发弹幕、改巡览配置、改 AI
  参数一律拒绝。
- full 强模式：除「发视频」外全功能 —— 上面前提下放开点赞、投币、
  收藏/取消、发弹幕、巡览配置、拟稿批量发布、AI 参数调整。

三条硬护栏（任何模式都不放开）：
1. `upload`（发视频）对 AI 永久关闭，只能人自己点；
2. AI 不能改自己的 `mode` / `api_key` / `clear_key`（防自提权），
   强模式下调 llm_configure 时这几个字段会被剔除；
3. 未知动作按 fail-closed 处理：弱模式拒绝，强模式放行（新功能不阻塞）。

限权点只有一处：`Core.execute(actor="ai")`。autoreply / roam 这些 AI
自有链路要么经 execute、要么在自身发布前调 `check`，保证同一个判据。
每次 AI 动作落一行 `.data/ai_audit.jsonl`，事后可追因。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / ".data"
CFG_FILE = DATA_DIR / "llm.json"
AUDIT_FILE = DATA_DIR / "ai_audit.jsonl"
AUDIT_MAX_LINES = 500

MODE_WEAK = "weak"
MODE_FULL = "full"
DEFAULT_MODE = MODE_WEAK
MODES = (MODE_WEAK, MODE_FULL)

LABELS = {
    MODE_WEAK: "弱模式（只能发评论）",
    MODE_FULL: "强模式（除发视频外全功能）",
}

SUMMARIES = {
    MODE_WEAK: "只允许发评论 / 回复评论；搜索、看视频、读评论字幕弹幕消息等只读操作可用",
    MODE_FULL: "点赞、投币、收藏、发弹幕、巡览发布、改 AI 参数都可用；唯独不能发视频",
}

# 只读：任何模式都放行（含本地播放器状态、本地查询、上传记录查询）
READONLY = frozenset({
    "search", "open_video", "comments", "subtitles", "subtitle",
    "danmaku_list", "folders", "inbox", "roam_list", "roam_next",
    "roam_check", "llm", "llm_test", "me", "state", "status",
    "play", "pause", "resume", "seek", "upload_zones", "upload_list",
})

# 评论类写动作：弱模式唯一允许的对外写
COMMENT_WRITE = frozenset({"comment", "reply", "roam_comment", "roam_send"})

# 本地草稿 / 本地任务状态：不对外，弱模式也允许
LOCAL_WRITE = frozenset({"roam_draft", "roam_draft_set", "roam_skip"})

# 强模式才放开：点赞、投币、收藏、弹幕、巡览配置、AI 参数
EXTENDED_WRITE = frozenset({
    "danmaku", "like", "coin", "fav", "unfav", "roam", "llm_configure",
})

# 永不允许 AI 执行
NEVER = frozenset({"upload"})

# AI 不可自我修改的配置字段
LOCKED_FIELDS = frozenset({"mode", "api_key", "clear_key"})

_cfg_cache: dict = {"mtime": -1.0, "mode": DEFAULT_MODE}


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def normalize(mode) -> str:
    v = str(mode or "").strip().lower()
    return v if v in MODES else DEFAULT_MODE


def current() -> str:
    """读当前模式：直读 llm.json，带 mtime 缓存，避免每次调用都开文件"""
    mt = _mtime(CFG_FILE)
    if mt == _cfg_cache["mtime"] and _cfg_cache["mtime"] > 0:
        return _cfg_cache["mode"]
    mode = DEFAULT_MODE
    try:
        raw = json.loads(CFG_FILE.read_text(encoding="utf-8") or "{}")
        mode = normalize(raw.get("mode"))
    except (OSError, ValueError):
        pass
    _cfg_cache.update(mtime=mt, mode=mode)
    return mode


def _deny(msg: str) -> str:
    return f"ERR_AI_MODE_DENIED: {msg}"


def check(mode: str, action: str, args: dict | None = None) -> str | None:
    """返回 None 表示放行，返回字符串表示拒绝原因（直接可回给 AI）"""
    m = normalize(mode)
    a = str(action or "").strip()
    if not a:
        return _deny("缺少动作名")
    if a in NEVER:
        return _deny(f"「{a}」对 AI 永久关闭（发视频只能由本人操作）")
    if a in READONLY or a in COMMENT_WRITE or a in LOCAL_WRITE:
        return None
    if a in EXTENDED_WRITE:
        if m == MODE_FULL:
            return None
        return _deny(f"弱模式只能发评论，[{a}] 需要切到强模式")
    if m == MODE_FULL:
        return None
    return _deny(f"弱模式不认识的动作 [{a}]，已按最小权限拒绝")


def filter_args(action: str, args: dict | None) -> tuple[dict, list[str]]:
    """AI 调配置类命令时剔掉能自我提权的字段；返回 (args, 被剔字段)"""
    out = dict(args or {})
    if str(action or "").strip() != "llm_configure":
        return out, []
    dropped = [k for k in list(out) if k in LOCKED_FIELDS]
    for k in dropped:
        out.pop(k, None)
    return out, dropped


def label(mode: str = "") -> str:
    """模式的中文名；不传则取当前模式"""
    return LABELS[normalize(mode or current())]


def capabilities(mode: str) -> dict:
    """给前端 / CLI 展示的能力清单"""
    m = normalize(mode)
    allow = set(READONLY) | set(COMMENT_WRITE) | set(LOCAL_WRITE)
    if m == MODE_FULL:
        allow |= set(EXTENDED_WRITE)
    deny = set(NEVER) | (set() if m == MODE_FULL else set(EXTENDED_WRITE))
    return {
        "mode": m,
        "label": LABELS[m],
        "summary": SUMMARIES[m],
        "allow": sorted(allow),
        "deny": sorted(deny),
        "modes": [{"value": k, "label": LABELS[k], "summary": SUMMARIES[k]}
                  for k in MODES],
    }


def audit(action: str, mode: str, allowed: bool, detail: str = "") -> None:
    """追加一条 AI 动作审计；文件超过上限时保留最后 AUDIT_MAX_LINES 行"""
    rec = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "action": str(action or "")[:64],
        "mode": normalize(mode),
        "allowed": bool(allowed),
        "detail": str(detail or "")[:200],
    }
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with AUDIT_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        _trim()
    except OSError:
        pass


def _trim() -> None:
    try:
        lines = AUDIT_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    if len(lines) <= AUDIT_MAX_LINES * 2:
        return
    keep = lines[-AUDIT_MAX_LINES:]
    tmp = AUDIT_FILE.with_suffix(".jsonl.tmp")
    try:
        tmp.write_text("\n".join(keep) + "\n", encoding="utf-8")
        tmp.replace(AUDIT_FILE)
    except OSError:
        pass


def tail(n: int = 20) -> list[dict]:
    """读最近 n 条 AI 动作记录（新→旧）"""
    n = max(1, min(AUDIT_MAX_LINES, int(n or 20)))
    try:
        lines = AUDIT_FILE.read_text(encoding="utf-8").splitlines()[-n:]
    except OSError:
        return []
    out = []
    for ln in reversed(lines):
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue
    return out


def text(mode: str = "") -> str:
    """CLI / MCP 展示用文本"""
    cap = capabilities(mode or current())
    return "\n".join([
        f'ai_mode: {cap["mode"]}  ({cap["label"]})',
        f'summary: {cap["summary"]}',
        f'allow({len(cap["allow"])}): ' + " ".join(cap["allow"]),
        f'deny({len(cap["deny"])}): ' + " ".join(cap["deny"]),
        'locked: mode / api_key 由人维护，AI 改不了',
    ])
