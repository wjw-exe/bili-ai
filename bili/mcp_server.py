"""MCP 层：把命令总线暴露成 stdio 工具

设计约束：每个工具只返回紧凑纯文本（一行一条、短键、稳定 bvid），
不返回 JSON，避免模型把 token 花在括号和字段名上。

版本兼容：官方 Python SDK 换过一次服务端类名——
  mcp 1.x：``from mcp.server.fastmcp import FastMCP``
  mcp 2.x：``from mcp.server.mcpserver import MCPServer``（FastMCP 改名 MCPServer）
两者工具注册语法一致（``@server.tool()`` + 类型注解 + docstring），故按可用性择优导入，
不把用户钉死在某一个大版本上。
"""

try:                                        # mcp 1.2 ~ 1.x
    from mcp.server.fastmcp import FastMCP as _Server
    SERVER_API = "fastmcp (mcp 1.x)"
except Exception:                           # mcp 2.x：FastMCP 更名为 MCPServer
    try:
        from mcp.server.mcpserver import MCPServer as _Server
        SERVER_API = "mcpserver (mcp 2.x)"
    except Exception:
        try:
            from mcp.server import MCPServer as _Server
            SERVER_API = "mcpserver (mcp 2.x, top-level)"
        except Exception as e:              # pragma: no cover
            raise ImportError(
                "未找到 MCP 服务端类：需要 mcp>=1.2。"
                "安装：pip install -U \"mcp>=1.2,<3\"（1.x 走 FastMCP，2.x 走 MCPServer）"
            ) from e

from .core import core
from .store import store

mcp = _Server(
    name="bili",
    instructions=(
        "B 站客户端（headless，无窗口）。所有状态由同一个 StateStore 维护，"
        "调用 search/open_video 写入状态，state 读取当前状态。\n"
        "典型流程：search -> open_video(bvid) -> play -> state。\n"
        "返回文本中的 view= 表示当前视图，actions: 列出下一步可用命令。"
        "player 的 t 为逻辑播放进度（秒），由客户端按时钟推进，非真实解码播放。"
    ),
)


@mcp.tool()
async def search(keyword: str, page: int = 1, limit: int = 10) -> str:
    """按关键词搜索视频。返回 total 与带 #序号 的条目列表（含 bvid、时长、播放量、UP 主）。"""
    await core.bili.warmup()
    return await core.execute("search", {"keyword": keyword, "page": page, "limit": limit})


@mcp.tool()
async def open_video(bvid: str) -> str:
    """打开视频详情页（标题/UP/时长/互动数据），并把它设为当前播放目标。bvid 形如 BV1xx411c7mD。"""
    await core.bili.warmup()
    return await core.execute("open_video", {"bvid": bvid})


@mcp.tool()
async def play(bvid: str = "", at: float = 0.0) -> str:
    """开始逻辑播放；传 bvid 时先打开该视频。at 为起播秒数。"""
    await core.bili.warmup()
    return await core.execute("play", {"bvid": bvid, "at": at})


@mcp.tool()
async def pause() -> str:
    """暂停，冻结当前位置。"""
    return await core.execute("pause", {})


@mcp.tool()
async def resume() -> str:
    """从暂停位置继续。"""
    return await core.execute("resume", {})


@mcp.tool()
async def seek(to: str) -> str:
    """跳转播放位置。to 支持绝对秒数 "120" 或相对量 "+15" / "-30"。"""
    return await core.execute("seek", {"to": to})


@mcp.tool()
async def state() -> str:
    """读取当前完整状态：视图、列表/详情、播放进度、可用动作。"""
    return await core.execute("state", {})


@mcp.tool()
async def status() -> str:
    """只读播放状态一行，token 最省。"""
    return await core.execute("status", {})


@mcp.tool()
async def comments(bvid: str = "", page: int = 1, limit: int = 20, mode: int = 3) -> str:
    """读评论列表（一页 20 条）。bvid 留空用当前视频。mode: 3=热门 2=时间。"""
    await core.bili.warmup()
    return await core.execute("comments",
                              {"bvid": bvid, "page": page, "limit": limit, "mode": mode})


@mcp.tool()
async def comment(message: str, bvid: str = "", root: int = 0, parent: int = 0) -> str:
    """在视频下发一条评论（会公开发布，不可撤回）。bvid 留空用当前视频；root/parent 非 0 表示回复某条评论。需要登录态。"""
    await core.bili.warmup()
    return await core.execute("comment",
                              {"bvid": bvid, "message": message,
                               "root": root, "parent": parent})


@mcp.tool()
async def like(bvid: str = "", on: bool = True) -> str:
    """点赞/取消点赞当前视频。需要登录态，未登录会返回 ERR_NEED_LOGIN。"""
    await core.bili.warmup()
    return await core.execute("like", {"bvid": bvid, "on": on})


@mcp.tool()
async def coin(bvid: str = "", n: int = 1, also_like: bool = False) -> str:
    """给视频投币，n 只能 1 或 2；also_like=True 时同时点赞。需要登录态。"""
    await core.bili.warmup()
    return await core.execute("coin", {"bvid": bvid, "n": n, "also_like": also_like})


@mcp.tool()
async def fav(bvid: str = "", folder: str = "") -> str:
    """收藏到指定收藏夹。folder 可填收藏夹 id 或名称，留空收进第一个收藏夹。需要登录态。"""
    await core.bili.warmup()
    return await core.execute("fav", {"bvid": bvid, "folder": folder})


@mcp.tool()
async def unfav(bvid: str = "", folder: str = "") -> str:
    """取消收藏；folder 留空表示从所有收藏夹移除。需要登录态。"""
    await core.bili.warmup()
    return await core.execute("unfav", {"bvid": bvid, "folder": folder})


@mcp.tool()
async def folders() -> str:
    """列出账号下的收藏夹（id/名称/条目数），用于给 fav 选夹。需要登录态。"""
    return await core.execute("folders", {})


@mcp.tool()
async def me() -> str:
    """查看当前登录账号（昵称/mid/大会员），同时刷新后台的账号显示。"""
    await core.bili.warmup()
    return await core.execute("me", {})


@mcp.tool()
async def subtitles(bvid: str = "", page: int = 1) -> str:
    """列出视频的字幕轨（B 站 CC 字幕 / AI 字幕，含语言与是否需解锁）。bvid 留空用当前视频，
    page 为第几 P。画面内嵌的硬字幕没有数据源，取不到。先用它挑语言，再用 subtitle 读正文。"""
    await core.bili.warmup()
    return await core.execute("subtitles", {"bvid": bvid, "page": page})


@mcp.tool()
async def subtitle(bvid: str = "", lang: str = "", page: int = 1, offset: int = 0,
                   limit: int = 200, export: str = "") -> str:
    """读字幕正文，用于总结/整理视频内容。lang 留空自动选中文轨（ai-zh 优先）；
    offset/limit 按句分页（limit 上限 500），返回每行 [mm:ss] 文本，末尾给下一页 offset；
    export 填 srt/txt/md 时把全量字幕落盘到 .data/subs/ 并返回路径。"""
    await core.bili.warmup()
    return await core.execute("subtitle",
                              {"bvid": bvid, "lang": lang, "page": page,
                               "offset": offset, "limit": limit, "export": export})


@mcp.tool()
async def inbox(kind: str = "all", limit: int = 20, only_unreplied: bool = True) -> str:
    """列出别人回复我 / @我的评论消息，供 AI 生成回复文案。

    kind: all（回复+@）/ reply（只看别人回复我）/ at（只看 @我）；
    only_unreplied=True 时只返回还没回复过的消息。
    每条给出 msg_id、是谁、来自哪个视频/文章、对方说了什么、我的原评论、状态。
    本工具只做检测，不会发送任何内容。"""
    await core.bili.warmup()
    return await core.execute("inbox", {"kind": kind, "limit": limit,
                                       "only_unreplied": only_unreplied})


@mcp.tool()
async def reply(msg_id: str, message: str) -> str:
    """把 AI 生成的文案作为评论回复发出（回复对方那条消息所在评论区）。需要登录态。

    msg_id 取自 inbox 的结果；message 为要发布的回复正文，必填。"""
    await core.bili.warmup()
    return await core.execute("reply", {"msg_id": msg_id, "message": message})


@mcp.tool()
async def roam_next(task_id: str = "") -> str:
    """取一个 AI 巡览待评论任务：视频信息 + 字幕正文（前 2000 字），供阅读后写评论。

    task_id 留空取最早的 waiting 任务。任务由后台巡览自动产生（先读字幕，
    读不到字幕的视频不会生成任务）。读完据此写一条评论，再用 roam_comment 回填。"""
    await core.bili.warmup()
    return await core.execute("roam_next", {"task_id": task_id})


@mcp.tool()
async def roam_comment(task_id: str, text: str) -> str:
    """把 AI 写好的评论正文发布到该巡览任务的视频下（会公开发布，不可撤回）。

    task_id 取自 roam_next；text 为评论正文，必填。受每日上限约束。需要登录态。"""
    await core.bili.warmup()
    return await core.execute("roam_comment", {"task_id": task_id, "message": text})


@mcp.tool()
async def roam_status() -> str:
    """查看 AI 巡览状态：开关/频率/每日上限/来源/统计，以及最近任务（含 skipped 原因）。"""
    return core.roam.status_text() + "\n" + core.roam.list_text("", 10)


@mcp.tool()
async def upload_zones(top: int = 0) -> str:
    """列出投稿分区（父分区 -> tid 子分区），用于给 upload_video 选 tid。需要登录态。"""
    await core.bili.warmup()
    return await core.execute("upload_zones", {"top": top})


@mcp.tool()
async def upload_video(file: str, title: str, tid: int, desc: str = "",
                       tags: str = "", cover: str = "", copyright: int = 1,
                       source: str = "", tid_v2: int = 0,
                       confirm: bool = False) -> str:
    """把一个本地视频文件投稿到 B 站（上传 + 提交审核，会公开发布、不可撤回）。

    file 为本地视频绝对路径；title ≤80 字；tid 用 upload_zones 查；
    tags 逗号分隔；cover 为封面图片路径（可空）；copyright 1=自制 2=转载（2 必须给 source）。
    confirm=False（默认）只做本地预检并返回投稿计划，不消耗任何上传流量；
    confirm=True 才会真实上传并提交审核。需要登录态。
    tid_v2 一般留 0：新版分区会用到，缺了会自动反查一次。"""
    await core.bili.warmup()
    return await core.execute("upload",
                              {"file": file, "title": title, "tid": tid, "desc": desc,
                               "tag": tags, "cover": cover, "copyright": copyright,
                               "source": source, "tid_v2": tid_v2,
                               "confirm": confirm})


@mcp.tool()
async def upload_list(pn: int = 1, ps: int = 10) -> str:
    """列出我的稿件与审核状态（state_desc），用于确认 upload_video 是否过审。需要登录态。"""
    await core.bili.warmup()
    return await core.execute("upload_list", {"pn": pn, "ps": ps})


def run() -> None:
    """stdio 传输启动。1.x / 2.x 的 run 签名不同，按可用形式调用。"""
    try:
        mcp.run(transport="stdio")
    except TypeError:
        mcp.run("stdio")
