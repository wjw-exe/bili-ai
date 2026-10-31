"""AI 能力模式（弱/强）离线回归测试，不联网。

覆盖四层：
1. `bili/modes.py` 的白/黑名单判定（弱模式只放评论，强模式除发视频全放）；
2. `Core.execute(actor="ai")` 真的会拦下越权动作、人（actor="user"）不受影响；
3. AI 调 llm_configure 时 `mode` / `api_key` / `clear_key` 被剔除（防自提权）；
4. `llm.py` 的 mode 读写与非法值拒绝，以及审计日志落盘。

跑法：`python -m pytest tests -q`，或直接 `python tests/test_ai_modes.py`。
"""

import asyncio
import json
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from bili import llm as llm_mod  # noqa: E402
from bili import modes  # noqa: E402
from bili.core import Core  # noqa: E402


class FakeHttp:
    """替身 HTTP 层：只提供 sync_from_disk，避免碰真实 cookie 文件"""

    def sync_from_disk(self) -> None:
        pass


def _tmpdir() -> pathlib.Path:
    return pathlib.Path(tempfile.mkdtemp(prefix="bili_modes_"))


def _patch_paths(tmp: pathlib.Path):
    """把 llm / modes 的配置文件与审计文件都指到临时目录，绝不碰真实 .data"""
    cfg = tmp / "llm.json"
    cfg.write_text(json.dumps({"mode": "weak"}), encoding="utf-8")
    old = (modes.CFG_FILE, modes.AUDIT_FILE, modes.DATA_DIR,
           llm_mod.CFG_FILE, llm_mod.STATE_FILE)
    modes.CFG_FILE = cfg
    modes.AUDIT_FILE = tmp / "ai_audit.jsonl"
    modes.DATA_DIR = tmp
    modes._cfg_cache.update(mtime=-1.0, mode=modes.DEFAULT_MODE)
    llm_mod.CFG_FILE = cfg
    llm_mod.STATE_FILE = tmp / "llm_state.json"
    return cfg, old


def _restore_paths(old) -> None:
    (modes.CFG_FILE, modes.AUDIT_FILE, modes.DATA_DIR,
     llm_mod.CFG_FILE, llm_mod.STATE_FILE) = old
    modes._cfg_cache.update(mtime=-1.0, mode=modes.DEFAULT_MODE)


def make_core():
    """真 Core 实例 + 假 HTTP + 被替换的 cmd_*（记录调用，不发请求）"""
    c = Core()
    c.bili.http = FakeHttp()
    calls = []

    def stub(name):
        async def _f(**kw):
            calls.append((name, kw))
            return f"ok {name}"
        return _f

    for n in ("like", "comment", "reply", "danmaku", "upload",
              "llm_configure", "roam_send", "search"):
        setattr(c, f"cmd_{n}", stub(n))
    return c, calls


# ---------------- 1. 名单判定 ----------------

def test_weak_allows_only_comment_writes():
    weak = modes.MODE_WEAK
    for a in ("comment", "reply", "roam_comment", "roam_send"):
        assert modes.check(weak, a) is None, f"{a} 属评论，弱模式应放行"
    for a in ("search", "open_video", "comments", "inbox", "state", "llm"):
        assert modes.check(weak, a) is None, f"{a} 只读，应放行"
    for a in ("roam_draft", "roam_draft_set", "roam_skip"):
        assert modes.check(weak, a) is None, f"{a} 只动本地草稿，应放行"
    for a in ("like", "coin", "fav", "unfav", "danmaku", "roam"):
        out = modes.check(weak, a)
        assert out and out.startswith("ERR_AI_MODE_DENIED"), f"{a} 弱模式应拒绝"
        assert "强模式" in out
    assert modes.check(weak, "llm_configure").startswith("ERR_AI_MODE_DENIED")


def test_full_allows_extended_but_never_upload():
    full = modes.MODE_FULL
    for a in ("like", "coin", "fav", "unfav", "danmaku", "roam",
              "llm_configure", "comment", "reply", "search"):
        assert modes.check(full, a) is None, f"{a} 强模式应放行"
    out = modes.check(full, "upload")
    assert out.startswith("ERR_AI_MODE_DENIED") and "永久关闭" in out


def test_unknown_action_fail_closed_in_weak():
    assert modes.check(modes.MODE_WEAK, "brand_new_thing").startswith("ERR_AI_MODE_DENIED")
    assert modes.check(modes.MODE_FULL, "brand_new_thing") is None
    assert modes.check(modes.MODE_WEAK, "") == ("ERR_AI_MODE_DENIED: 缺少动作名")


def test_capabilities_shape():
    cap = modes.capabilities("weak")
    assert cap["mode"] == "weak"
    assert "comment" in cap["allow"] and "like" in cap["deny"]
    assert "like" in cap["allow"] is False or "like" not in cap["allow"]
    cap2 = modes.capabilities("full")
    assert "like" in cap2["allow"] and "upload" in cap2["deny"]
    assert [m["value"] for m in cap["modes"]] == ["weak", "full"]


# ---------------- 2. execute 守门 ----------------

def test_execute_blocks_ai_but_not_human():
    tmp = _tmpdir()
    cfg, old = _patch_paths(tmp)
    try:
        c, calls = make_core()
        out = asyncio.run(c.execute("like", {"bvid": "BV1xx411c7mD"}, actor="ai"))
        assert out.startswith("ERR_AI_MODE_DENIED")
        assert calls == []                       # 关键：被拦的命令一行代码都没执行

        out = asyncio.run(c.execute("comment", {"message": "好"}, actor="ai"))
        assert out == "ok comment"
        assert calls == [("comment", {"message": "好"})]

        # 人自己点不受模式限制
        out = asyncio.run(c.execute("like", {"bvid": "BV1xx411c7mD"}))
        assert out == "ok like"
        assert calls[-1][0] == "like"
    finally:
        _restore_paths(old)


def test_execute_full_mode_unlocks_and_upload_stays_closed():
    tmp = _tmpdir()
    cfg, old = _patch_paths(tmp)
    try:
        cfg.write_text(json.dumps({"mode": "full"}), encoding="utf-8")
        modes._cfg_cache.update(mtime=-1.0, mode=modes.DEFAULT_MODE)
        assert modes.current() == "full"
        c, calls = make_core()
        assert asyncio.run(c.execute("like", {"bvid": "BV1"})) == "ok like"
        assert asyncio.run(c.execute("like", {"bvid": "BV1"}, actor="ai")) == "ok like"
        out = asyncio.run(c.execute("upload", {"title": "x"}, actor="ai"))
        assert out.startswith("ERR_AI_MODE_DENIED")
        assert [n for n, _ in calls] == ["like", "like"]
    finally:
        _restore_paths(old)


def test_ai_cannot_self_escalate_on_llm_configure():
    tmp = _tmpdir()
    cfg, old = _patch_paths(tmp)
    try:
        cfg.write_text(json.dumps({"mode": "full"}), encoding="utf-8")
        modes._cfg_cache.update(mtime=-1.0, mode=modes.DEFAULT_MODE)
        c, calls = make_core()
        out = asyncio.run(c.execute("llm_configure", {
            "mode": "full", "api_key": "sk-偷来的", "clear_key": True,
            "prompt": "改个提示词"}, actor="ai"))
        assert out == "ok llm_configure"
        kw = calls[-1][1]
        assert kw == {"prompt": "改个提示词"}    # 提权字段全被剔除
    finally:
        _restore_paths(old)


# ---------------- 3. 配置读写 ----------------

def test_llm_mode_roundtrip_and_bad_value():
    tmp = _tmpdir()
    cfg, old = _patch_paths(tmp)
    try:
        inst = llm_mod.LLM()
        assert inst.public()["mode"] == "weak"            # 默认弱模式
        assert inst.configure(mode="full") == "ok saved"
        assert inst.raw()["mode"] == "full"
        assert inst.public()["mode_label"].startswith("强模式")
        assert json.loads(cfg.read_text(encoding="utf-8"))["mode"] == "full"
        assert inst.configure(mode="god").startswith("ERR_BAD_MODE")
        assert inst.raw()["mode"] == "full"              # 非法值不改配置
    finally:
        _restore_paths(old)


def test_mode_default_when_file_missing():
    tmp = _tmpdir()
    cfg, old = _patch_paths(tmp)
    try:
        cfg.unlink()
        modes._cfg_cache.update(mtime=-1.0, mode=modes.DEFAULT_MODE)
        assert modes.current() == "weak"
        inst = llm_mod.LLM()
        assert inst.public()["mode"] == "weak"
    finally:
        _restore_paths(old)


# ---------------- 4. 审计 ----------------

def test_audit_written_and_tail_readable():
    tmp = _tmpdir()
    cfg, old = _patch_paths(tmp)
    try:
        modes.audit("like", "weak", False, "弱模式只能发评论")
        modes.audit("comment", "weak", True, "ok comment")
        lines = modes.AUDIT_FILE.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        rec = json.loads(lines[0])
        assert rec["action"] == "like" and rec["allowed"] is False
        tail = modes.tail(5)
        assert tail[0]["action"] == "comment"            # 新→旧
        assert modes.tail(1)[0]["allowed"] is True
    finally:
        _restore_paths(old)


def test_text_view_mentions_mode():
    txt = modes.text("full")
    assert "ai_mode: full" in txt
    assert "upload" in txt


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print(f"FAIL {name}: {exc!r}")
    raise SystemExit(1 if failed else 0)
