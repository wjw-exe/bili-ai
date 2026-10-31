"""结构化事件流（P0 观测层）。

一句话：所有 AI 动作、巡览结果、发送成败、token 用量都写一条 JSONL，
再同步进 sqlite 索引，供 account_digest / 控制台看板按天聚合。

设计约束：
- 纯标准库；任何写失败都不抛异常打断主业务（观测层不能成为单点故障）；
- 单一事实源仍是 .data/*.json，事件流只做"追加日志 + 索引"，不改任何业务状态；
- 按天分文件：.data/events/YYYY-MM-DD.jsonl
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

DATA = Path(__file__).resolve().parents[1] / ".data"
EV_DIR = DATA / "events"
DB = EV_DIR / "index.sqlite"

_LOCK = threading.Lock()
_SCHEMA = """
CREATE TABLE IF NOT EXISTS ev (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL, day TEXT, action TEXT, target TEXT,
  ok INTEGER, model TEXT, tokens_in INTEGER, tokens_out INTEGER,
  ms INTEGER, refs TEXT, detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_day_action ON ev(day, action);
CREATE INDEX IF NOT EXISTS idx_target ON ev(target);
"""


def _day(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts if ts is not None else time.time()))


def _conn() -> sqlite3.Connection:
    EV_DIR.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB, timeout=5.0)
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(_SCHEMA)
    return c


def emit(action: str, *, target: str = "", ok: Any = None, model: str = "",
         tokens_in: int = 0, tokens_out: int = 0, ms: int = 0,
         refs: list[str] | None = None, **detail: Any) -> dict:
    """追加一条事件。永不抛异常。"""
    ts = time.time()
    day = _day(ts)
    ev = {
        "ts": round(ts, 3), "day": day, "action": str(action)[:64],
        "target": str(target)[:120],
        "ok": None if ok is None else bool(ok),
        "model": str(model)[:60],
        "tokens_in": int(tokens_in or 0), "tokens_out": int(tokens_out or 0),
        "ms": int(ms or 0),
        "refs": list(refs or []),
        "detail": detail or {},
    }
    try:
        EV_DIR.mkdir(parents=True, exist_ok=True)
        with _LOCK:
            with (EV_DIR / f"{day}.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    except Exception:                            # noqa: BLE001
        pass
    try:
        d = json.dumps(ev["detail"], ensure_ascii=False)[:2000]
        with _LOCK:
            with _conn() as c:
                c.execute(
                    "INSERT INTO ev(ts,day,action,target,ok,model,tokens_in,tokens_out,ms,refs,detail)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (ev["ts"], day, ev["action"], ev["target"],
                     -1 if ev["ok"] is None else int(ev["ok"]),
                     ev["model"], ev["tokens_in"], ev["tokens_out"], ev["ms"],
                     ",".join(ev["refs"]), d))
    except Exception:                            # noqa: BLE001
        pass
    return ev


def summary(day: str = "", *, days: int = 1) -> dict:
    """当天（或最近 N 天）聚合：总数/失败数/工具分布/token/耗时。"""
    out: dict[str, Any] = {"scope": day or f"近{days}天", "total": 0, "fail": 0,
                           "tokens": 0, "actions": {}, "targets": {}}
    try:
        with _conn() as c:
            if day:
                rows = c.execute(
                    "SELECT action,target,ok,tokens_in,tokens_out,ms FROM ev WHERE day=?",
                    (day,)).fetchall()
            else:
                since = _day(time.time() - 86400 * max(1, days))
                rows = c.execute(
                    "SELECT action,target,ok,tokens_in,tokens_out,ms FROM ev WHERE day>=?",
                    (since,)).fetchall()
    except Exception:                            # noqa: BLE001
        return out
    for action, target, ok, ti, to, ms in rows:
        out["total"] += 1
        if ok == 0:
            out["fail"] += 1
        out["tokens"] += int(ti or 0) + int(to or 0)
        a = out["actions"].setdefault(action, {"n": 0, "fail": 0, "ms": 0})
        a["n"] += 1
        a["ms"] += int(ms or 0)
        if ok == 0:
            a["fail"] += 1
        if target:
            t = out["targets"].setdefault(target, {"n": 0, "fail": 0})
            t["n"] += 1
            if ok == 0:
                t["fail"] += 1
    out["targets"] = dict(sorted(out["targets"].items(),
                                 key=lambda kv: kv[1]["n"], reverse=True)[:8])
    return out


def query(*, day: str = "", action: str = "", target: str = "",
          only_fail: bool = False, limit: int = 100) -> list[dict]:
    """按天/动作/目标查事件明细。"""
    sql = "SELECT ts,day,action,target,ok,model,tokens_in,tokens_out,ms,refs,detail FROM ev WHERE 1=1"
    args: list[Any] = []
    if day:
        sql += " AND day=?"; args.append(day)
    if action:
        sql += " AND action=?"; args.append(action)
    if target:
        sql += " AND target LIKE ?"; args.append(f"%{target}%")
    if only_fail:
        sql += " AND ok=0"
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, min(int(limit or 100), 500)))
    try:
        with _conn() as c:
            cur = c.execute(sql, args)
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception:                            # noqa: BLE001
        return []
    for r in rows:
        try:
            r["detail"] = json.loads(r.get("detail") or "{}")
        except Exception:                        # noqa: BLE001
            r["detail"] = {}
    return rows


def tail(n: int = 30) -> list[dict]:
    """最近 n 条（跨天）。"""
    try:
        f = sorted(EV_DIR.glob("*.jsonl"))[-1]
    except Exception:                            # noqa: BLE001
        return []
    try:
        lines = f.read_text(encoding="utf-8", errors="replace").splitlines()[-max(1, n):]
        return [json.loads(x) for x in lines if x.strip()]
    except Exception:                            # noqa: BLE001
        return []


def stats() -> dict:
    """库文件规模，供控制台展示。"""
    try:
        files = list(EV_DIR.glob("*.jsonl"))
        size = sum(p.stat().st_size for p in files)
        return {"days": len(files), "bytes": size,
                "db_bytes": DB.stat().st_size if DB.exists() else 0,
                "dir": str(EV_DIR)}
    except Exception:                            # noqa: BLE001
        return {"days": 0, "bytes": 0, "db_bytes": 0, "dir": str(EV_DIR)}
