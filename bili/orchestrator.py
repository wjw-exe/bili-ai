"""长任务编排（Orchestrator）。

解决三个真实问题：
1. 多步任务中途断了（模型抽风 / 网络断 / 用户关窗口）要能从断点继续；
2. 只读步骤可以并行提速，写步骤必须串行（风控），两者不能混；
3. 同一个步骤重复执行会造成重复评论/弹幕，必须有幂等键。

计划落 .data/plans/<plan_id>.json，幂等台账落同名 .idem.json。
不新起权限：执行仍走 tools.call → core.execute(actor="ai")。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path
from typing import Any

from . import events

DATA = Path(__file__).resolve().parents[1] / ".data"
PLAN_DIR = DATA / "plans"

READ_PARALLEL = 5          # 只读并行上限
BREAKER_FAILS = 3          # 同一工具连续失败熔断阈值


def _fm(pid: str) -> Path:
    return PLAN_DIR / f"{str(pid).replace('/', '_')}.json"


def _im(pid: str) -> Path:
    return PLAN_DIR / f"{str(pid).replace('/', '_')}.idem.json"


def _sig(tool: str, args: dict) -> str:
    return hashlib.sha1(
        f"{tool}|{json.dumps(args or {}, ensure_ascii=False, sort_keys=True)}".encode()
    ).hexdigest()[:16]


def load(plan_id: str) -> dict | None:
    try:
        return json.loads(_fm(plan_id).read_text(encoding="utf-8"))
    except Exception:                            # noqa: BLE001
        return None


def save(plan: dict) -> dict:
    PLAN_DIR.mkdir(parents=True, exist_ok=True)
    plan["updated"] = round(time.time(), 3)
    tmp = _fm(plan["plan_id"]).with_suffix(".tmp")
    tmp.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(_fm(plan["plan_id"]))
    return plan


def _load_idem(plan_id: str) -> dict:
    try:
        return json.loads(_im(plan_id).read_text(encoding="utf-8")) or {}
    except Exception:                            # noqa: BLE001
        return {}


def _save_idem(plan_id: str, d: dict) -> None:
    PLAN_DIR.mkdir(parents=True, exist_ok=True)
    try:
        _im(plan_id).write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:                            # noqa: BLE001
        pass


def create(goal: str, steps: list[dict], *, plan_id: str = "") -> dict:
    """steps: [{title, tool, args, mode:"read"|"write", needs:[step_id]}]"""
    pid = plan_id or f"p_{time.strftime('%Y%m%d_%H%M%S')}"
    norm = []
    for i, s in enumerate(steps or []):
        if not isinstance(s, dict) or not s.get("tool"):
            continue
        norm.append({
            "step_id": str(s.get("step_id") or f"s{i + 1}"),
            "title": str(s.get("title") or s.get("tool"))[:80],
            "tool": str(s["tool"]),
            "args": s.get("args") or {},
            "mode": "write" if str(s.get("mode") or "") == "write" else "read",
            "needs": list(s.get("needs") or []),
            "status": "pending", "tries": 0, "note": "", "ref": "",
        })
    plan = {"plan_id": pid, "goal": str(goal)[:400], "created": round(time.time(), 3),
            "status": "running", "steps": norm, "fail_streak": {}}
    return save(plan)


def update_step(plan_id: str, step_id: str, status: str, *, note: str = "",
                ref: str = "") -> dict:
    plan = load(plan_id)
    if not plan:
        return {"ok": False, "err": "PLAN_NOT_FOUND", "plan_id": plan_id}
    hit = None
    for s in plan.get("steps") or []:
        if s["step_id"] == step_id:
            hit = s
            break
    if not hit:
        return {"ok": False, "err": "STEP_NOT_FOUND", "step_id": step_id}
    hit["status"] = status
    if note:
        hit["note"] = str(note)[:300]
    if ref:
        hit["ref"] = ref
    if all(s["status"] in ("done", "skipped") for s in plan["steps"]):
        plan["status"] = "done"
    save(plan)
    return {"ok": True, "plan_id": plan_id, "step_id": step_id, "status": status,
            "plan_status": plan["status"]}


def _ready(plan: dict) -> list[dict]:
    done = {s["step_id"] for s in plan["steps"] if s["status"] in ("done", "skipped")}
    return [s for s in plan["steps"]
            if s["status"] in ("pending", "failed")
            and all(n in done for n in s.get("needs") or [])]


def board(plan_id: str = "") -> list[dict]:
    """控制台看板：计划一览。"""
    out = []
    try:
        files = sorted(PLAN_DIR.glob("*.json"))
    except Exception:                            # noqa: BLE001
        return []
    for fp in files:
        if fp.name.endswith(".idem.json"):
            continue
        if plan_id and plan_id not in fp.name:
            continue
        try:
            p = json.loads(fp.read_text(encoding="utf-8"))
        except Exception:                        # noqa: BLE001
            continue
        st = p.get("steps") or []
        out.append({"plan_id": p.get("plan_id"), "goal": p.get("goal"),
                    "status": p.get("status"), "updated": p.get("updated"),
                    "done": sum(1 for s in st if s["status"] == "done"),
                    "total": len(st)})
    return sorted(out, key=lambda x: x.get("updated") or 0, reverse=True)[:20]


async def _exec_one(core, store, step: dict, *, confirmed: bool) -> dict:
    from . import tools as T
    t0 = time.time()
    step["tries"] = int(step.get("tries") or 0) + 1
    env = await T.call(core, store, step["tool"], step.get("args") or {},
                       confirmed=confirmed)
    ms = int((time.time() - t0) * 1000)
    ok = bool(env.get("ok"))
    step["status"] = "done" if ok else ("failed" if step["tries"] < 2 else "failed")
    step["note"] = (env.get("summary") or env.get("err") or "")[:200]
    step["ref"] = env.get("ref") or ""
    return {"step_id": step["step_id"], "tool": step["tool"], "ok": ok, "ms": ms,
            "verdict": (env.get("verify") or {}).get("verdict"),
            "note": step["note"][:160], "ref": step.get("ref") or ""}


async def run(core, store, plan_id: str, *, confirmed_writes: bool = False,
              max_rounds: int = 8) -> dict:
    """把能跑的步骤跑完：只读并行 ≤5，写串行且需 confirmed，出问题熔断。"""
    plan = load(plan_id)
    if not plan:
        return {"ok": False, "err": "PLAN_NOT_FOUND", "plan_id": plan_id}
    if plan.get("status") == "done":
        return {"ok": True, "plan_id": plan_id, "status": "done", "executed": [],
                "waiting": [], "msg": "计划已完成"}

    idem = _load_idem(plan_id)
    executed: list[dict] = []
    waiting: list[dict] = []
    streak: dict[str, int] = dict(plan.get("fail_streak") or {})

    for _ in range(max(1, max_rounds)):
        ready = _ready(plan)
        if not ready:
            break
        reads = [s for s in ready if s["mode"] == "read"][:READ_PARALLEL]
        writes = [s for s in ready if s["mode"] == "write"]

        if reads:
            for s in reads:
                sig = _sig(s["tool"], s.get("args") or {})
                if sig in idem:
                    s["status"] = "skipped"
                    s["note"] = "幂等命中，跳过"
            reads = [s for s in reads if s["status"] != "skipped"]
            if reads:
                res = await asyncio.gather(*[_exec_one(core, store, s, confirmed=False)
                                             for s in reads], return_exceptions=True)
                for s, r in zip(reads, res):
                    if isinstance(r, Exception):
                        r = {"step_id": s["step_id"], "tool": s["tool"], "ok": False,
                             "note": f"{type(r).__name__}: {r}"[:160]}
                        s["status"] = "failed"
                    executed.append(r)
                    if r["ok"]:
                        idem[_sig(s["tool"], s.get("args") or {})] = round(time.time(), 3)
                        streak[s["tool"]] = 0
                    else:
                        streak[s["tool"]] = streak.get(s["tool"], 0) + 1
            save(plan)
            _save_idem(plan_id, idem)
            if any(v >= BREAKER_FAILS for v in streak.values()):
                plan["status"] = "blocked"
                plan["fail_streak"] = streak
                save(plan)
                return {"ok": False, "plan_id": plan_id, "status": "blocked",
                        "executed": executed, "waiting": [],
                        "msg": f"熔断：{max(streak, key=streak.get)} 连续失败 {BREAKER_FAILS} 次"}
            continue

        w = writes[0]
        sig = _sig(w["tool"], w.get("args") or {})
        if sig in idem:
            w["status"] = "skipped"
            w["note"] = "幂等命中，跳过"
            save(plan)
            continue
        if not confirmed_writes:
            waiting = [{"step_id": w["step_id"], "tool": w["tool"],
                        "title": w["title"], "args": w.get("args") or {}}]
            plan["fail_streak"] = streak
            save(plan)
            return {"ok": True, "plan_id": plan_id, "status": "need_confirm",
                    "executed": executed, "waiting": waiting,
                    "msg": "有写步骤待用户确认"}
        r = await _exec_one(core, store, w, confirmed=True)
        executed.append(r)
        if r["ok"]:
            idem[sig] = round(time.time(), 3)
            streak[w["tool"]] = 0
        else:
            streak[w["tool"]] = streak.get(w["tool"], 0) + 1
            if streak[w["tool"]] >= BREAKER_FAILS:
                plan["status"] = "blocked"
                plan["fail_streak"] = streak
                save(plan)
                _save_idem(plan_id, idem)
                return {"ok": False, "plan_id": plan_id, "status": "blocked",
                        "executed": executed, "waiting": [],
                        "msg": f"熔断：{w['tool']} 连续失败 {BREAKER_FAILS} 次"}
        save(plan)
        _save_idem(plan_id, idem)

    plan = load(plan_id) or plan
    if all(s["status"] in ("done", "skipped") for s in plan["steps"]):
        plan["status"] = "done"
        save(plan)
    plan["fail_streak"] = streak
    save(plan)
    return {"ok": True, "plan_id": plan_id, "status": plan.get("status"),
            "executed": executed, "waiting": waiting,
            "progress": sum(1 for s in plan["steps"] if s["status"] in ("done", "skipped")),
            "total": len(plan["steps"])}
