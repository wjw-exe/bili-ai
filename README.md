---
AIGC:
    Label: "1"
    ContentProducer: 001191440300708461136T1XGW3
    ProduceID: 297ab8dbc8f4cde8dd6b234597dbc01a_cd9a51aecf5211f19f8752540064ee0f
    ReservedCode1: aZjE3Ar2tgK7SkZEPSiRzFIljOPlYq8bRDthWMiLZqN+BI5NUaAdfd/NY11V/9Ep9WjI2ha6PMtCX9xm5hA9sFvf2N+Qel4Mp4kPnCaEiajdOWDyqSzRSyTcGRd40t0h+y2u3PAU1kxPmNJYb/Mee5ihkKv0q+O2zrF+x0StXV7++kbvXc2eHkQ9LlY=
    ContentPropagator: 001191440300708461136T1XGW3
    PropagateID: 297ab8dbc8f4cde8dd6b234597dbc01a_cd9a51aecf5211f19f8752540064ee0f
    ReservedCode2: aZjE3Ar2tgK7SkZEPSiRzFIljOPlYq8bRDthWMiLZqN+BI5NUaAdfd/NY11V/9Ep9WjI2ha6PMtCX9xm5hA9sFvf2N+Qel4Mp4kPnCaEiajdOWDyqSzRSyTcGRd40t0h+y2u3PAU1kxPmNJYb/Mee5ihkKv0q+O2zrF+x0StXV7++kbvXc2eHkQ9LlY=
---

# bili-ai

B 站客户端的 **headless 内核 + 双前端**：AI 走 MCP/命令操控，人看可视化后台。
无窗口、无 Electron、无 Rust，Python 标准库为主。

```
        ┌─────────────── MCP / CLI / HTTP 命令 ───────────────┐
        │                                                     ▼
   AI 客户端 ──► core.execute(name, args) ─────────────► StateStore（唯一真相源）
                                                             │
                       可视化后台（浏览器） ◄── SSE 0.35s ────┘
```

- **单一真相源**：列表、当前视频、播放进度、命令日志全在 `store.py`，任何前端都从它渲染，不靠 DOM 反推。
- **逻辑进度**：`play/pause/seek` 只推进服务端时钟偏移，不解码、不出声。给 AI 一个稳定可读的 `t=秒`。
- **紧凑文本协议**：MCP 工具只返回纯文本，一行一条、短键、稳定 `bvid`，不返回 JSON。

## 目录

| 文件 | 职责 |
|---|---|
| `main.py` | 入口：`serve` / `mcp` / `cli` / `login` / `status` / `logout` / `selftest` |
| `bili/sign.py` | wbi 签名（mixin key 换表 + 参数排序） |
| `bili/http.py` | HTTP 层：cookie 罐（含登录态持久化）、暖机、风控识别与退避 |
| `bili/auth.py` | 登录：分步扫码（申请 / 出图 / 轮询）/ Cookie 导入 / `nav` 校验 |
| `bili/api.py` | B 站 Web 接口封装：warmup / search / view / comments / 点赞 / 投币 / 收藏 |
| `bili/store.py` | StateStore + 逻辑播放器（时钟推进）+ 评论 / 互动状态 |
| `bili/fmt.py` | 文本展现层（表格化紧凑输出） |
| `bili/core.py` | 命令总线，MCP / Web / CLI 共用同一 `execute` |
| `bili/mcp_server.py` | MCP stdio 层，15 个工具 |
| `bili/web.py` | FastAPI 后台 + SSE + 图床代理 + 扫码登录接口 |
| `static/index.html` | 单文件前端，零资源依赖 |

## 安装

项目全在 D 盘，依赖装进 Python 的 site-packages（C 盘，仅库本体，不装软件）：

```powershell
cd D:\projects\bili-ai
pip install -r requirements.txt     # mcp / fastapi / uvicorn / httpx
```

## 运行

```powershell
python main.py serve                # 人看的后台 http://127.0.0.1:8770
python main.py serve --port 8772    # 换端口
python main.py mcp                  # MCP stdio（同时起后台，可扫码登录）
python main.py mcp --no-web         # 纯 MCP
python main.py cli search 猫 --limit 5
python main.py cli open_video BV1GJ411x7h7
python main.py selftest             # 联网全链路自检
```

### 接到 MCP 客户端

```json
{
  "mcpServers": {
    "bili": {
      "command": "python",
      "args": ["D:\\projects\\bili-ai\\main.py", "mcp"],
      "cwd": "D:\\projects\\bili-ai"
    }
  }
}
```

此时后台会自动起在 8770。后台拥有自己的 `Core` 实例（避免跨线程操作主 loop 的连接池），**按钮可点、可扫码登录**；两边共用同一个 `StateStore`，所以 AI 的每一步与人的每次点击都会实时反映到同一份快照上。

## 命令协议

| 命令 | 参数 | 说明 |
|---|---|---|
| `search` | `keyword` `page=1` `limit=10` | 关键词搜索，写入列表状态 |
| `open_video` | `bvid` | 打开详情并设为播放目标 |
| `play` | `bvid=""` `at=0` | 开始逻辑播放，传 bvid 则先打开 |
| `pause` / `resume` | — | 冻结 / 续播 |
| `seek` | `to` | 绝对 `"120"`，相对 `"+15"` / `"-30"` |
| `state` | — | 完整状态：视图 + 数据 + 进度 + 可用动作 |
| `status` | — | 只读一行进度，最省 token |
| `comments` | `bvid=""` `page=1` `limit=20` `mode=3` | 读评论，`mode` 3=热门 2=时间 |
| `comment` | `message` `bvid=""` `root=0` `parent=0` | 发评论（真实写操作），`root/parent` 非 0 为回复；需登录 |
| `like` | `bvid=""` `on=true` | 点赞 / 取消点赞 |
| `coin` | `bvid=""` `n=1` `also_like=false` | 投币，`n` ∈ {1,2} |
| `fav` | `bvid=""` `folder=""` | 收藏，`folder` 可填 id 或名称，留空收进第一个夹 |
| `unfav` | `bvid=""` `folder=""` | 取消收藏，留空=从所有夹移除 |
| `folders` | — | 列收藏夹（id / 名称 / 条目数） |
| `me` | — | 当前登录账号 |

`like` / `coin` / `fav` / `unfav` / `folders` 需要登录态，未登录返回 `ERR_NEED_LOGIN`（提示去后台扫码），不会静默失败。

输出示例：

```
view=search q="猫" page=1 total=1000 shown=2
#1  BV1Uqpk64EaL    2:18  play=   1.3万  up=橘猫黄小胖  打预防针
#2  BV1WxHA6BESP    1:15  play=   2088  up=人生说明书16888  猫贩子第十集
next: search --keyword "猫" --page 2
```

每条输出末行 `actions:` 告诉 AI 下一步能发什么命令，避免它瞎试。

## 登录

登录态统一落在 `.data/cookie.json`（权限 0600，仅存 `bilibili` 域 cookie），进程启动时自动装载，`serve` / `mcp` / `cli` 共用。

### 方式一：后台扫码（推荐，图形界面）

`python main.py serve` 打开 http://127.0.0.1:8770，点右上角**扫码登录**：

1. 后台申请二维码，弹层里直接出图（`/api/auth/qr` + `/api/auth/qr.png`）；
2. 前端每 1.6 秒轮询 `/api/auth/poll`，扫码后提示「已扫码，请在手机上确认」；
3. 确认成功后自动落盘 `cookie.json`，右上角变成头像 + 昵称，退出按钮可清态。

登录成功后**无需重启进程**：`http.py` 的 `sync_from_disk()` 会在每次请求前比对 `cookie.json` 的 mtime，后台写入的新 cookie 会被 MCP/CLI 侧自动热重载。

### 方式二：命令行扫码

```powershell
python main.py login
```

流程：申请二维码 → 渲染成 `.data/login_qr.png` 并自动打开 → 手机 B 站客户端「扫一扫」→ 手机端点确认 → 自动写入 cookie 并回验昵称。默认 180 秒超时，可 `--timeout 60` 调整，`--no-open` 不自动弹图。

### 方式三：粘贴 Cookie

浏览器登录 bilibili.com → F12 → Application → Cookies → `https://www.bilibili.com`，取 `SESSDATA`、`bili_jct`、`DedeUserID` 三项：

```powershell
python main.py login --import-cookie "SESSDATA=xxx; bili_jct=yyy; DedeUserID=123"
```

也支持直接传 Cookie JSON。等价于把这三个键手写进 `.data/cookie.json`。

### 查看与退出

```powershell
python main.py status   # 调 nav 接口真实验证，打印昵称 / mid / 大会员
python main.py logout   # 删除本地 cookie.json
```

> `mcp` / `serve` 是常驻进程，cookie 只在启动时装载一次。后台扫码登录无需重启（每次请求前自动热重载）；命令行 `login` 写入后同样会被热重载，但 MCP 客户端缓存的连接若要立刻生效，最稳妥仍是重启。

## 后台界面

三栏：左「Now Playing」、中「搜索结果 / 评论」双 Tab、右「Command Log」。

- 播放器下四个按钮：**点赞 / 投币 / 收藏 / 评论**，状态与 `store.inter` 同步（`liked` / `coins` / `favoured`；未登录时三者均为 `null`，界面上按钮变灰）。
- 收藏点开后拉 `/api/folders` 选夹；已收藏再点即取消收藏。
- 评论 Tab 每页 20 条，底部显示页码与总条数，可翻页；`comments_meta.is_end` 控制下一页是否可用。
- 未登录点互动按钮 → 顶部 toast 显示 `ERR_NEED_LOGIN`，不会无声失败。

## 风控

未登录调搜索接口会被 B 站拦（返回 `code=0` 但 `data` 里只有 `v_voucher`）。`http.py` 已内置：

1. 先访问 `https://www.bilibili.com` 暖机拿 `b_nut` / `buvid3` 等 Cookie；
2. 搜索参数只保留最小集 `keyword` / `search_type` / `page`，多带即被拦；
3. `Referer` 必须为 `https://search.bilibili.com/all?keyword=<编码>`；
4. 命中 `v_voucher` 则丢弃 `buvid3/buvid4/b_nut`、退避 6 秒、重新暖机后重试一次。

## 已知限制

- **已实现接口**：search / view / comments / comment / like / coin / fav / unfav / folders / nav(me)。**未实现**：高码率 playurl（真实取流）、删评论、发弹幕。
- **写操作的边界**：点赞、投币、收藏是真实写 B 站账号，调用即产生副作用（投币不可撤回）。MCP 工具描述已标注「需要登录态」，未登录一律 `ERR_NEED_LOGIN` 阻断，不会误写。
- **时长秒位**：搜索接口对部分视频返回的 `duration` 本身是 `16:0` 这种缺位值，前端原样显示，不补零——补零等于伪造数据。`open_video` 后拿到的 `dur` 是精确秒数。
- **逻辑播放**：不对接真实播放器，`t` 是客户端时钟推算值，不是解码进度。

## 自检

```powershell
python main.py selftest
```

依次跑 warmup → search → open_video → play/seek/pause → state，任一环节风控或报文异常会打印错误码。
*（内容由AI生成，仅供参考）*
