"""bili-ai 统一入口

python main.py serve            # 只开可视化后台 http://127.0.0.1:8770
python main.py mcp              # MCP stdio 服务 + 后台线程（后台只读）
python main.py mcp --no-web     # 纯 MCP，不起后台
python main.py cli search 关键词 # 命令行
python main.py login            # 扫码登录（二维码图片自动弹出，手机扫）
python main.py login --import-cookie "SESSDATA=xx; bili_jct=yy"  # 手动导入
python main.py status           # 查看登录态
python main.py logout           # 清除本地登录态
python main.py selftest         # 联网自检
"""

import argparse
import asyncio
import sys
import threading


def _serve(port: int, quiet: bool = False) -> None:
    import uvicorn

    from bili.web import app

    uvicorn.run(app, host="127.0.0.1", port=port,
                log_level="critical" if quiet else "warning",
                access_log=False, log_config=None)


def _parse_cli(cmd: str, rest: list[str]) -> dict:
    """--key value 形式解析；多余的位置参数按命令映射到主参数

    例：cli search 猫 --limit 5  ->  {"keyword": "猫", "limit": 5}
        cli open_video BV1xx     ->  {"bvid": "BV1xx"}
    """
    args, pos, i = {}, [], 0
    while i < len(rest):
        k = rest[i]
        if k.startswith("--"):
            key = k[2:].replace("-", "_")
            if i + 1 < len(rest) and not rest[i + 1].startswith("--"):
                v = rest[i + 1]
                if key == "at":
                    args[key] = float(v)
                elif key in ("page", "limit", "offset"):
                    args[key] = int(v)
                else:
                    args[key] = v
                i += 2
                continue
            args[key] = True
        else:
            pos.append(k)
        i += 1
    if pos:
        main_key = {"search": "keyword", "open_video": "bvid", "play": "bvid",
                    "comment": "message", "subtitles": "bvid",
                    "subtitle": "bvid"}.get(cmd)
        if main_key and main_key not in args:
            args[main_key] = pos[0]
    return args


def _auth_cmd(a) -> int:
    """login / status / logout 三合一"""
    from bili.auth import import_cookie, nav, qr_login
    from bili.http import COOKIE_FILE, Api

    if a.mode == "logout":
        Api().clear_cookies()
        print("已清除本地登录态")
        return 0

    if a.mode == "status":
        async def _run():
            api = Api()
            await api.warmup()
            try:
                return await nav(api)
            finally:
                await api.aclose()

        info = asyncio.run(_run())
        if info.get("error"):
            print(f"校验失败：{info['error']}")
        if info.get("is_login"):
            print(f"登录态：已登录\n账号：{info['uname']}（mid={info['mid']}）"
                  f"{'  大会员' if info.get('vip') else ''}")
        else:
            print("登录态：未登录（仅搜索 / 视频详情可用）")
        print(f"Cookie 文件：{COOKIE_FILE}"
              f"{'' if COOKIE_FILE.exists() else '（不存在）'}")
        return 0 if info.get("is_login") else 1

    if a.cookie:
        r = import_cookie(a.cookie)
        if not r.get("ok"):
            print(f"导入失败：{r['msg']}")
            return 1
        print(f"Cookie 已写入：{r['cookie_file']}（{', '.join(r['keys'])}）")

    async def _run():
        api = Api()
        await api.warmup()
        try:
            if a.cookie:
                return await nav(api)
            return await qr_login(api, timeout=a.timeout, open_qr=not a.no_open)
        finally:
            await api.aclose()

    r = asyncio.run(_run())
    if a.cookie:
        if r.get("is_login"):
            print(f"校验通过：{r['uname']}（mid={r['mid']}）")
            return 0
        print(f"校验未通过：{r.get('message') or r.get('error')}")
        return 1
    return 0 if r.get("ok") else 1


async def _selftest() -> int:
    from bili.core import core

    print("[1/5] warmup + search")
    out = await core.execute("search", {"keyword": "python", "page": 1, "limit": 3})
    print(out)
    lines = [ln for ln in out.splitlines() if ln.startswith("#")]
    if not lines:
        print("FAIL: 搜索无结果")
        return 1

    bvid = lines[0].split()[1]
    print(f"\n[2/5] open_video {bvid}")
    out = await core.execute("open_video", {"bvid": bvid})
    print(out)

    print("\n[3/5] play / seek / pause")
    print(await core.execute("play", {"bvid": "", "at": 0.0}))
    await asyncio.sleep(1.2)
    print(await core.execute("seek", {"to": "+15"}))
    print(await core.execute("pause", {}))

    print("\n[4/5] comments + 写操作守卫")
    out = await core.execute("comments", {"page": 1, "limit": 3})
    print(out)
    if not any(ln.startswith("#") for ln in out.splitlines()):
        print("FAIL: 评论为空")
        return 1
    # 守卫测试用非法 bvid，避免在已登录账号上真的点赞某个视频
    out = await core.execute("like", {"bvid": "BV1xxINVALID", "on": True})
    print(out)
    if not (out.startswith("ERR_") or out.startswith("ok like") or "ApiError" in out):
        print("FAIL: 写操作守卫未如期返回")
        return 1

    print("\n[5/5] subtitles（无字幕轨则跳过，不算失败）")
    out = await core.execute("subtitles", {"bvid": bvid, "page": 1})
    print(out)
    if "lan=" in out:
        lang = next(ln for ln in out.splitlines() if "lan=" in ln)
        lang = lang.split("lan=")[1].split()[0]
        out = await core.execute("subtitle", {"bvid": bvid, "lang": lang,
                                              "offset": 0, "limit": 3})
        print(out)
        if not out.startswith("view=subtitle"):
            print("FAIL: 字幕正文读取失败")
            return 1
    else:
        print("SKIP: 该视频没有字幕轨")

    print("\n" + await core.execute("state", {}))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="bili-ai")
    sub = ap.add_subparsers(dest="mode", required=True)

    s = sub.add_parser("serve")
    s.add_argument("--port", type=int, default=8770)

    m = sub.add_parser("mcp")
    m.add_argument("--port", type=int, default=8770)
    m.add_argument("--no-web", action="store_true")

    c = sub.add_parser("cli")
    c.add_argument("cmd")
    c.add_argument("rest", nargs=argparse.REMAINDER)

    lg = sub.add_parser("login")
    lg.add_argument("--import-cookie", dest="cookie", default=None,
                    help="手动导入 Cookie 串或 JSON，跳过扫码")
    lg.add_argument("--timeout", type=float, default=180.0)
    lg.add_argument("--no-open", action="store_true", help="不自动打开二维码图片")

    sub.add_parser("status")
    sub.add_parser("logout")
    sub.add_parser("selftest")

    a = ap.parse_args()

    if a.mode in ("login", "status", "logout"):
        return _auth_cmd(a)

    if a.mode == "serve":
        _serve(a.port)
        return 0

    if a.mode == "mcp":
        from bili.core import core
        from bili.mcp_server import run

        core.readonly = True          # MCP 主 loop 标记；后台走自己的 web_core，不受影响
        if not a.no_web:
            threading.Thread(target=_serve, args=(a.port, True), daemon=True).start()
        run()                          # stdio 阻塞直到客户端断开
        return 0

    if a.mode == "cli":
        from bili.core import core

        print(asyncio.run(core.execute(a.cmd.replace("-", "_"), _parse_cli(a.cmd.replace("-", "_"), a.rest))))
        return 0

    if a.mode == "selftest":
        return asyncio.run(_selftest())

    return 2


if __name__ == "__main__":
    sys.exit(main())
