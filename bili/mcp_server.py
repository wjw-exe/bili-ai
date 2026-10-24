"""MCP 层：把命令总线暴露成 stdio 工具

设计约束：每个工具只返回紧凑纯文本（一行一条、短键、稳定 bvid），
不返回 JSON，避免模型把 token 花在括号和字段名上。
"""

from mcp.server.mcpserver import MCPServer

from .core import core
from .store import store

mcp = MCPServer(
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


def run() -> None:
    mcp.run("stdio")
