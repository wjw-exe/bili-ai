"""引用句柄仓库（Ref 卸载层）。

大对象（整页评论、字幕全文、巡览任务、批量草稿）不进模型上下文，
落 workspace/refs/<ref>.json，只把 ref 句柄 + 摘要回给模型；
模型需要细节时调 read_ref(ref, offset, limit, grep) 分页取。

好处：上下文恒定可控，且 ref 可复现（同一素材同一天得到同一个 ref）。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
REF_DIR = ROOT / "workspace" / "refs"
TTL_DAYS = 7


def _hash(kind: str, seed: str) -> str:
    h = hashlib.sha1(f"{kind}|{seed}".encode("utf-8")).hexdigest()[:10]
    return f"r_{h}"


def path_of(ref: str) -> Path:
    return REF_DIR / f"{str(ref).lstrip('/').replace('.json', '')}.json"


def put(kind: str, data: Any, *, key: str = "", ttl_days: int = TTL_DAYS) -> str:
    """存对象，返回 ref 句柄。key 相同则复用（幂等）。"""
    seed = key or hashlib.sha1(
        json.dumps(data, ensure_ascii=False, sort_keys=True, default=str).encode()
    ).hexdigest()
    ref = _hash(kind, seed)
    fp = path_of(ref)
    try:
        REF_DIR.mkdir(parents=True, exist_ok=True)
        blob = {"ref": ref, "kind": kind, "created": round(time.time(), 3),
                "ttl_days": ttl_days, "data": data}
        tmp = fp.with_suffix(".tmp")
        tmp.write_text(json.dumps(blob, ensure_ascii=False, default=str), encoding="utf-8")
        os.replace(tmp, fp)
    except Exception:                            # noqa: BLE001
        pass
    return ref


def load(ref: str) -> dict | None:
    try:
        return json.loads(path_of(ref).read_text(encoding="utf-8"))
    except Exception:                            # noqa: BLE001
        return None


def _flatten(data: Any, prefix: str = "") -> list[str]:
    """把任意结构展平成文本行，便于 grep / 分页。"""
    lines: list[str] = []
    if isinstance(data, dict):
        for k, v in data.items():
            lines.extend(_flatten(v, f"{prefix}{k}."))
    elif isinstance(data, list):
        for i, v in enumerate(data):
            lines.extend(_flatten(v, f"{prefix}[{i}]."))
    else:
        s = "" if data is None else str(data).replace("\n", " ")
        lines.append(f"{prefix.rstrip('.')} = {s}")
    return lines


def read(ref: str, *, offset: int = 0, limit: int = 120, grep: str = "") -> dict:
    """分页读 ref 内容（展平后按行滑窗），支持 grep 过滤。"""
    blob = load(ref)
    if not blob:
        return {"ok": False, "err": "REF_NOT_FOUND", "ref": ref,
                "hint": "句柄可能已过期（默认 7 天），请重新拉取原始数据"}
    lines = _flatten(blob.get("data"))
    if grep:
        g = str(grep).lower()
        lines = [x for x in lines if g in x.lower()]
    total = len(lines)
    off = max(0, int(offset or 0))
    lim = max(1, min(int(limit or 120), 400))
    chunk = lines[off:off + lim]
    return {"ok": True, "ref": ref, "kind": blob.get("kind"), "total_lines": total,
            "offset": off, "returned": len(chunk),
            "next_offset": off + len(chunk) if off + len(chunk) < total else None,
            "lines": chunk}


def usage() -> dict:
    try:
        files = list(REF_DIR.glob("r_*.json"))
        size = sum(p.stat().st_size for p in files)
        return {"count": len(files), "bytes": size, "dir": str(REF_DIR)}
    except Exception:                            # noqa: BLE001
        return {"count": 0, "bytes": 0, "dir": str(REF_DIR)}


def gc(ttl_days: int = TTL_DAYS) -> int:
    """删过期 ref，返回删除数。"""
    now = time.time()
    n = 0
    try:
        for p in REF_DIR.glob("r_*.json"):
            try:
                blob = json.loads(p.read_text(encoding="utf-8"))
                ttl = float(blob.get("ttl_days") or ttl_days)
                if now - float(blob.get("created") or 0) > ttl * 86400:
                    p.unlink(missing_ok=True)
                    n += 1
            except Exception:                    # noqa: BLE001
                continue
    except Exception:                            # noqa: BLE001
        pass
    return n
