---


---
为 ai 而生的哔哩哔哩
---

# bili-ai

B 站客户端的 **headless 内核 + 双前端**：AI 走 MCP / CLI / HTTP 操控，人看可视化后台。
无窗口、无 Electron、无 Rust、无 Node，主体是 Python 标准库。

**不接大模型、不需要任何 API Key**——项目只负责"把 B 站的数据与动作变成 AI 可读可写的接口"，
读什么、写什么由调用它的 AI 决定。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Python](https://img.shields.io/badge/Python-3.10%2B-blue)
![MCP](https://img.shields.io/badge/MCP-26%20tools-00aeec)
![No API Key](https://img.shields.io/badge/API%20Key-not%20required-success)

```bash
git clone https://github.com/wjw-exe/bili-ai.git
cd bili-ai                       # 必须在项目根目录运行，bili 是包
python -m venv .venv
.venv\Scripts\activate           # PowerShell: .\.venv\Scripts\Activate.ps1
python -m pip install -U pip
python -m pip install -r requirements.txt
python main.py serve             # → http://127.0.0.1:8770
```

装完先跑一次 `python main.py selftest` 验证链路，再开后台。装不上/起不来的对号入座见
[安装](#安装) 与 [启动不起来？](#启动不起来)。

```
        ┌──────────── MCP（26 工具） / CLI / HTTP ────────────┐
        │                                                     ▼
   AI 客户端 ──► core.execute(name, args) ───────────► StateStore（唯一真相源）
                                                             │
                       可视化后台（浏览器） ◄── SSE 0.35s ────┘
                                ▲
                                │ 同一份 store / 同一套 execute
              inbox.py（回复我 / @我）· roam.py（巡览取片 → 读字幕 → 待评论任务）
```

## 它解决什么问题

B 站的开放能力是按"人点按钮"设计的：接口分散、风控隐晦、状态在页面里。
对 AI 来说，难点不是"能不能调接口"，而是**能不能拿到稳定可读的状态、并且知道自己下一步能做什么**。

bili-ai 的三条设计原则：

- **单一真相源**：搜索结果、当前视频、播放进度、字幕、评论、互动状态、命令日志全在 `store.py`，
  MCP / CLI / Web 三个前端都从它渲染，不靠 DOM 反推，也不各自维护副本。
- **紧凑文本协议**：MCP 工具只返回纯文本，一行一条、短键、稳定 `bvid`，不返回 JSON——
  不让模型把 token 花在括号和字段名上；每条输出末行 `actions:` 告诉 AI 下一步能发什么命令，避免瞎试。
- **写操作显式、可审计**：点赞 / 投币 / 收藏 / 发评论 / 回消息都是真实写账号的行为，
  未登录统一返回 `ERR_NEED_LOGIN` 阻断，绝不静默失败；自动回复与巡览都**先生成任务、再等文案回填**，
  项目内部不生成任何文案、不自动群发。

## 功能一览

| 分类 | 能力 | 状态 |
|---|---|---|
| 浏览 | 关键词搜索（分页 / 总量 / 时长 / 播放量 / UP） | 已实现 |
| 浏览 | 视频详情、多 P 分 P 信息 | 已实现 |
| 播放 | 逻辑播放器：`play` / `pause` / `resume` / `seek`（绝对或相对） | 已实现 |
| 评论 | 读评论（热门 / 时间）、分页 | 已实现 |
| 评论 | 发评论、回复某条评论（`root` / `parent`） | 已实现 |
| 互动 | 点赞 / 投币（1~2）/ 收藏 / 取消收藏 / 列收藏夹 | 已实现 |
| 字幕 | 列 CC / AI 字幕轨、分页读正文、导出 srt / txt / md | 已实现 |
| 消息 | 收件箱：别人**回复我** / **@我** 的评论，去重、去已回复 | 已实现 |
| 消息 | 用外部 AI 给的文案回复某条消息，并记账防重复 | 已实现 |
| 巡览 | 定时自动挑视频 → 读字幕 → 生成待评论任务（无字幕不发） | 已实现 |
| 巡览 | 取任务（含字幕正文）→ 回填文案发布 / 跳过，受每日上限约束 | 已实现 |
| 投稿 | 查分区 tid、我的稿件与审核状态 | 已实现 |
| 投稿 | 上传本地视频并提交审核（`upload`，默认 dry-run，`confirm=true` 才真发） | 已实现 |
| 登录 | 后台扫码 / 命令行扫码 / 粘贴 Cookie，登录态热重载 | 已实现 |
| 浏览 | 真实取流 playurl、发弹幕、删评论 | 未实现 |

## 目录

| 文件 | 职责 |
|---|---|
| `main.py` | 入口：`serve` / `mcp` / `cli` / `login` / `status` / `logout` / `selftest` |
| `bili/sign.py` | wbi 签名（mixin key 换表 + 参数排序） |
| `bili/http.py` | HTTP 层：cookie 罐（登录态持久化 + mtime 热重载）、暖机、风控识别与退避 |
| `bili/auth.py` | 登录：分步扫码（申请 / 出图 / 轮询）/ Cookie 导入 / `nav` 校验 |
| `bili/api.py` | B 站 Web 接口封装：warmup / search / hot_videos / view / subtitles / comments / comment_add / msgfeed / 互动 |
| `bili/store.py` | StateStore + 逻辑播放器（时钟推进）+ 评论 / 字幕 / 互动 / 收件箱 / 巡览状态 |
| `bili/fmt.py` | 文本展现层（表格化紧凑输出） |
| `bili/core.py` | 命令总线，MCP / Web / CLI 共用同一 `execute` |
| `bili/upload.py` | 投稿链路：preupload → upos 分片上传 / 合并 → 封面上传 → web add 提交审核 |
| `bili/mcp_server.py` | MCP stdio 层，**26 个工具** |
| `bili/inbox.py` | 消息收件箱：拉 `msgfeed/reply` + `msgfeed/at`、归一化、baseline、回复记账 |
| `bili/roam.py` | AI 巡览：取片 → 过滤已见 → 读字幕 → 落 waiting 任务 → 发布 / 跳过 |
| `bili/web.py` | FastAPI 后台 + SSE + 图床代理 + 扫码登录 + 收件箱 / 巡览接口 |
| `static/index.html` | 单文件前端，零资源依赖 |

## 环境要求

| 项 | 要求 | 说明 |
|---|---|---|
| Python | **3.10+**（实测 3.11） | 代码用了 `X \| None` 与内置泛型注解，3.9 及以下会报 TypeError |
| mcp | **1.2 ≤ 版本 < 3** | 1.x 服务端类叫 `FastMCP`，2.x 改名 `MCPServer`，本项目两者都兼容；区间外直接 ImportError |
| fastapi / uvicorn | 已列入 requirements | 只有 `serve`、以及 `mcp` 附带的浏览器后台需要；`mcp --no-web` 纯 stdio 也仍会 import |
| Node / Rust / Electron | 不需要 | 前端是单文件 `static/index.html`，零构建 |
| 网络 | 能访问 bilibili.com | 首次运行会暖机拿 Cookie，跨境网络可能触发风控 `v_voucher` |

## 安装

```powershell
cd D:\projects\bili-ai          # 你的项目目录，必须是 main.py 所在层
python -m venv .venv
.\.venv\Scripts\Activate.ps1    # cmd 用 .venv\Scripts\activate.bat；Git Bash 用 source .venv/Scripts/activate
python -m pip install -U pip
python -m pip install -r requirements.txt
```

四条硬性注意：

1. **必须在项目根目录执行**。`bili/` 是包，`main.py` 用相对导入；在别的目录 `python D:\projects\bili-ai\main.py` 会 `ModuleNotFoundError: No module named 'bili'`。
2. **用 `python -m pip`，不要裸 `pip`**。机器上常有多套 Python，裸 `pip` 可能装到另一个解释器，启动时照样报缺包。
3. **mcp 版本按区间装**。`requirements.txt` 已写 `mcp>=1.2,<3`；若你曾手动 `pip install mcp` 装到旧版或未来的大版本，先 `python -m pip install -U "mcp>=1.2,<3"`。
4. **升级依赖别整套 overwrite**。已经有环境时只 `python -m pip install -r requirements.txt`，不要 `pip freeze > requirements.txt` 反向覆盖。

安装完成的判定（三条都应通过）：

```powershell
python -V                                            # 3.10 以上
python -c "import fastapi, uvicorn, httpx, segno"     # 无输出即通过
python -c "import bili.mcp_server as m; print(m.SERVER_API)"   # 打印 fastmcp (mcp 1.x) 或 mcpserver (mcp 2.x)
```

最后一条能打出 `SERVER_API`，说明 MCP 层和当前 mcp 版本对上了——这是"按 README 装完却起不来"最常见的一环。

## 运行

所有命令都在项目根目录执行。

```powershell
python main.py serve                # 人看的后台 http://127.0.0.1:8770
python main.py serve --port 8772    # 换端口
python main.py mcp                  # MCP stdio（同时起后台，可扫码登录）
python main.py mcp --no-web         # 纯 MCP
python main.py cli search 猫 --limit 5
python main.py cli open_video BV1GJ411x7h7
python main.py cli inbox --kind reply
python main.py selftest             # 联网全链路自检
```

### 启动不起来？

按现象对号入座，前三条覆盖了绝大多数情况：

| 现象 | 原因 | 处理 |
|---|---|---|
| `ModuleNotFoundError: No module named 'bili'` | 当前目录不是项目根目录，或 IDE 里直接运行了 `bili/` 下的文件 | `cd` 到 `main.py` 所在目录再执行；IDE 的运行配置把 `Working directory` 指向项目根 |
| `ModuleNotFoundError: No module named 'fastapi'`（或 `uvicorn` / `httpx` / `segno`） | 依赖装进了另一套 Python，或 venv 没激活 | 激活 venv 后 `python -m pip install -r requirements.txt`；用 `python -V` 和 `python -m pip -V` 确认两者路径一致 |
| `ImportError: 未找到 MCP 服务端类：需要 mcp>=1.2` | mcp 版本低于 1.2 或 ≥3，1.x / 2.x 类名都对不上 | `python -m pip install -U "mcp>=1.2,<3"` |
| `AttributeError` / `TypeError: run() got an unexpected keyword argument 'transport'` | mcp 版本与启动签名不匹配 | 同上拉开到 1.2~2.x；本项目 `run()` 已按 `transport=` 与位置参数两种写法自适应 |
| `OSError: [Errno 10048] error while attempting to bind 127.0.0.1:8770` | 8770 被占（上一次进程没退干净） | `python main.py serve --port 8772`；或先 `Get-NetTCPConnection -LocalPort 8770` 找到进程结束 |
| `UnicodeEncodeError: 'gbk' codec can't encode` | Windows 控制台默认 GBK | `chcp 65001`，或 `$env:PYTHONUTF8=1` 后重开 |
| `python : 无法将“python”项识别为` | Python 未装或没进 PATH | 装 3.10+ 并勾选 Add to PATH；或用 `py -3 main.py serve` |
| 后台能打开但一片空白 / 接口 500 | 前端在 `static/index.html`，缺文件或 `cwd` 不对 | 确认从项目根启动；`python main.py selftest` 看具体断点 |

启动成功的样子：

```
INFO:     Uvicorn running on http://127.0.0.1:8770 (Press CTRL+C to quit)
```

浏览器打开 `http://127.0.0.1:8770` 能看到三栏后台（Now Playing / 搜索·评论·字幕 / Command Log）即正常。
MCP 侧另测一条：`python main.py mcp --no-web` 后由客户端 `initialize` + `tools/list` 应返回 26 个工具。

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

此时后台会自动起在 8770。后台拥有自己的 `Core` 实例（避免跨线程操作主 loop 的连接池），
**按钮可点、可扫码登录**；两边共用同一个 `StateStore`，所以 AI 的每一步与人的每次点击
都会实时反映到同一份快照上。`mcp --no-web` 时不起后台，纯 stdio 工具服务。

> 用 venv 装的依赖，`command` 要写 venv 里的解释器绝对路径，否则客户端会用系统 Python 启动而报缺包：
> `"command": "D:\\projects\\bili-ai\\.venv\\Scripts\\python.exe"`。`cwd` 必须保留，`bili` 是包。

### 三个 AI 闭环

**A. AI 替我写评论**（先读懂，再写）

```
search "编译器原理" → open_video BV1xx → subtitle --lang ai-zh --limit 200
   → （AI 阅读字幕并撰写）→ comment --message "……"
```

**B. 有人回复我，AI 代写回复**

```
inbox --kind reply            # 只检测：列出谁回了什么、我在原评论说了什么
   → （AI 依据上下文生成文案）→ reply --msg-id 1184712873918478 --message "……"
```

`inbox` 只做检测、不发任何东西；`reply` 必填文案，缺了直接 `ERR_NEED_TEXT`。
已回复的消息写入 `.data/inbox.json` 记账，`only_unreplied=true` 时不再出现，天然防重复。

**C. AI 自己巡览 B 站**（不预设文案，先读后写）

```
roam --on --interval 1800 --max-per-day 5 --source hot
   ↓ 后台每 30 分钟：热门榜挑片 → 查字幕轨 → 拉正文
   ↓ 无字幕 / 空正文 / 轨锁定 → 记 skipped，绝不发评论
roam_next                     # 取一个 waiting 任务：视频信息 + 前 2000 字字幕正文
   → （AI 阅读后写评论）→ roam_comment --task-id T20261024xxxx --message "……"
roam_status                   # 开关 / 频率 / 今日已发 / 最近任务及跳过原因
```

- 每个 bvid 只处理一次（`seen_bvid`，上限 3000 条滚动淘汰）；
- 频率硬下限 120 秒（小于一律收敛），默认 30 分钟；每日发布上限默认 5，`0` 表示不限；
- 来源 `hot`（综合热门）或 `search`（关键词轮询，默认 `科技, 数码, 编程`，可自定义）；
- 文案一律由调用方传入，服务端只负责"发"与"记账"。

## 命令协议

MCP 工具、`main.py cli <cmd>`、`POST /api/cmd` 三者参数完全一致。

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
| `comment` | `message` `bvid=""` `root=0` `parent=0` | 发评论（真实写），`root/parent` 非 0 为回复；需登录 |
| `like` | `bvid=""` `on=true` | 点赞 / 取消点赞 |
| `coin` | `bvid=""` `n=1` `also_like=false` | 投币，`n` ∈ {1,2} |
| `fav` | `bvid=""` `folder=""` | 收藏，`folder` 可填 id 或名称，留空收进第一个夹 |
| `unfav` | `bvid=""` `folder=""` | 取消收藏，留空 = 从所有夹移除 |
| `folders` | — | 列收藏夹（id / 名称 / 条目数） |
| `subtitles` | `bvid=""` `page=1` | 列出字幕轨（lan / 语言 / 是否 AI / 是否需解锁） |
| `subtitle` | `bvid=""` `lang=""` `page=1` `offset=0` `limit=200` `export=""` | 读字幕正文（按句分页，limit 上限 500），`export=txt/srt/md` 落盘 |
| `me` | — | 当前登录账号，并刷新后台账号显示 |
| `inbox` | `kind=all` `limit=20` `only_unreplied=true` | 收件箱：`all` / `reply` / `at`，**只检测不发送** |
| `reply` | `msg_id` `message` | 按 msg_id 回复该条消息，文案必填，上限 1000 字；需登录 |
| `roam` | `on/off` `interval` `max_per_day` `source` `keyword` | 巡览配置；`interval` 秒（下限 120） |
| `roam_list` | `status=""` `limit=20` | 列巡览任务（waiting / sent / skipped / failed） |
| `roam_check` | `count=1` | 立刻跑一轮取片，只落任务、不发评论 |
| `roam_next` | `task_id=""` | 取 waiting 任务：视频信息 + 前 2000 字字幕正文 |
| `roam_comment` | `task_id` `message` | 发布巡览评论（真实写，不可撤回，受每日上限） |
| `roam_skip` | `task_id` `reason=manual` | 跳过某任务并记原因 |
| `upload_zones` | `top=0` | 列投稿分区（父分区 → tid 子分区），选 tid 用 |
| `upload` | `file` `title` `tid` `desc=""` `tag=""` `cover=""` `copyright=1` `source=""` `confirm=false` | 投稿视频：**默认 dry-run 只预检**，`confirm=true` 才真上传 + 提交审核 |
| `upload_list` | `pn=1` `ps=10` | 我的稿件与审核状态（`state_desc`） |

`like` / `coin` / `fav` / `unfav` / `folders` / `comment` / `reply` / `roam_comment` / `upload` / `upload_list` / `upload_zones`
需要登录态，未登录返回 `ERR_NEED_LOGIN`（提示去后台扫码），不会静默失败。

输出示例：

```
view=search q="猫" page=1 total=1000 shown=2
#1  BV1Uqpk64EaL    2:18  play=   1.3万  up=橘猫黄小胖  打预防针
#2  BV1WxHA6BESP    1:15  play=   2088  up=人生说明书16888  猫贩子第十集
next: search --keyword "猫" --page 2
```

收件箱输出示例：

```
inbox: 2 条  kind=reply  only_unreplied=1  fetched=46  replied=7
- msg_id=1184712873918478  [reply]  某UP(123456)  status=new  视频 BV1xx411c7mD  《标题》
  他说: 你上次说的那个方案能详细讲讲吗
  我: 第三段的思路挺关键的
actions: [reply --msg-id <id> --message "<AI 生成的文案>"]
```

## 投稿（发视频）

`upload` 是**全链路**投稿：本地预检 → preupload 申请线路 → upos 分片上传（默认 3 线程、分片重试）→
合并分片 → 可选封面上传（PIL 在则裁 16:10）→ `x/vu/web/add` 提交审核。

```powershell
python main.py cli upload_zones                                   # 先挑分区 tid
python main.py cli upload --file D:\a.mp4 --title "标题" --tid 21 --tag 编程,自制   # 只预检（dry-run）
python main.py cli upload --file D:\a.mp4 --title "标题" --tid 21 --tag 编程,自制 --confirm
python main.py cli upload_list                                    # 看审核状态
```

MCP 侧同名工具：`upload_zones` / `upload_video` / `upload_list`；`upload_video` 的 `confirm`
默认 `false`，只回投稿计划、**不上传任何字节**。

注意几条：

- **投稿是真实发布**，过审后公开可见、不可撤回，所以 `confirm` 必须显式给；先 dry-run 看清标题/分区/标签。
- 标题 ≤ 80 字；单文件 ≤ 8G，支持 `mp4/flv/mov/mkv/avi/wmv/webm/m4v`；`copyright=1` 自制、`2` 转载（转载必须给 `source` 原地址）。
- 分片大小以 preupload 返回的 `chunk_size` 为准（实测 10MB），不是本地写死。
- 提交返回码会翻成人话：`21001` 参数/分区错、`21564` 投稿频率限制、`21540` 需网页端人工过校。
- 新版分区体系要 `tid_v2`：默认可不填，出现 `21001` 时程序会用投稿预接口反查一次并自动重提；也可手动传 `tid_v2`。
- **别拿无效参数试探提交接口**：每发一次 `/x/vu/web/add` 都算一次投稿，连续几次就会吃 `21564`（24 小时级频率限制），
  严重时 `preupload` 直接 403、SESSDATA 被失效，需要重新扫码登录。预检一律走 `upload`（省掉 `--confirm`），它不发任何请求。
- 稿件 ID 从 `upload_list` 或 `state`（`upload.last`）拿；刚提交时 `state=0` 之前是审核中，属正常。

## 字幕（外部 CC，不含画面内嵌字幕）

B 站的 AI 字幕 / 人工字幕都属于**外挂轨**，可通过 `player/wbi/v2` 拿到 `subtitle_url` 再拉正文；
画面里烧死的硬字幕没有对应数据，取不到，本项目也不做 OCR。

```
subtitles                                        # 列轨：lan=ai-zh 中文 ai=1 lock=0
subtitle --lang ai-zh --offset 300 --limit 200   # 分页读正文，末行 next: 给下一页
subtitle --lang ai-zh --export srt               # 导出到 .data/subs/BVxxx_ai-zh.srt
```

输出是便于 AI 阅读的逐句文本，时间码 `[mm:ss]` 便于定位：

```
view=subtitle  bvid=BV1zNoVB1EWb  lan=ai-zh  中文  ai=1  cues=4709  chars=49592  offset=300 limit=4
[11:39] 啊就那时候口气很大啊
[11:41] 这是哪一年时候
next: subtitle --offset 304 --limit 4
```

- 字幕正文进程内缓存（同一 bvid+lang 只拉一次），翻页不重复请求。
- `subtitle_url` 带签名参数，拉取时**不能**再加普通参数，否则 403——`http.get_raw_json` 专为此实现。
- 后台第三个 Tab「字幕」可视化同一份数据：自动选 `ai-zh`、每页 300 句、可「加载更多」、可导出 txt。
- 该视频没有字幕轨时返回 `tracks=0` 明确提示，而不是空正文——巡览器据此判定"跳过、不发评论"。

## 登录

登录态统一落在 `.data/cookie.json`（权限 0600，仅存 `bilibili` 域 cookie），进程启动时自动装载，
`serve` / `mcp` / `cli` 共用。

### 方式一：后台扫码（推荐，图形界面）

`python main.py serve` 打开 http://127.0.0.1:8770，点右上角**扫码登录**：

1. 后台申请二维码，弹层里直接出图（`/api/auth/qr` + `/api/auth/qr.png`）；
2. 前端每 1.6 秒轮询 `/api/auth/poll`，扫码后提示「已扫码，请在手机上确认」；
3. 确认成功后自动落盘 `cookie.json`，右上角变成头像 + 昵称，退出按钮可清态。

登录成功后**无需重启进程**：`http.py` 的 `sync_from_disk()` 会在每次请求前比对 `cookie.json` 的 mtime，
后台写入的新 cookie 会被 MCP / CLI 侧自动热重载。

### 方式二：命令行扫码

```powershell
python main.py login
```

流程：申请二维码 → 渲染成 `.data/login_qr.png` 并自动打开 → 手机 B 站客户端「扫一扫」→
手机端点确认 → 自动写入 cookie 并回验昵称。默认 180 秒超时，可 `--timeout 60` 调整，`--no-open` 不自动弹图。

### 方式三：粘贴 Cookie

浏览器登录 bilibili.com → F12 → Application → Cookies → `https://www.bilibili.com`，
取 `SESSDATA`、`bili_jct`、`DedeUserID` 三项：

```powershell
python main.py login --import-cookie "SESSDATA=xxx; bili_jct=yyy; DedeUserID=123"
```

也支持直接传 Cookie JSON。等价于把这三个键手写进 `.data/cookie.json`。

### 查看与退出

```powershell
python main.py status   # 调 nav 接口真实验证，打印昵称 / mid / 大会员
python main.py logout   # 删除本地 cookie.json
```

> `mcp` / `serve` 是常驻进程，cookie 只在启动时装载一次。后台扫码登录无需重启（每次请求前自动热重载）；
> 命令行 `login` 写入后同样会被热重载，但 MCP 客户端缓存的连接若要立刻生效，最稳妥仍是重启。

## 后台界面

三栏：左「Now Playing」、中「搜索结果 / 评论 / 字幕」三 Tab、右「Command Log」。

- 播放器下四个按钮：**点赞 / 投币 / 收藏 / 评论**，状态与 `store.inter` 同步
  （`liked` / `coins` / `favoured`；未登录时三者均为 `null`，按钮变灰）。
- 收藏点开后拉 `/api/folders` 选夹；已收藏再点即取消收藏。
- 评论 Tab 顶部可切来源：**热门 / 最新 / 回复我的 / @我的**。后两者即收件箱视图，
  每条显示对方说了什么、我的原评论、来源视频，可直接在输入框写回复发出（发出后标记已回复）。
- 评论 Tab 上方是 **AI 巡览条**：开关、频率（分钟）、每日上限、来源（热门 / 关键词）、关键词、
  「立即巡览」按钮；展开后每个任务卡带视频信息、字幕摘要、评论输入框与「跳过」。
  这里的人工输入与 MCP 侧 `roam_comment` 走同一个发布函数，同样受每日上限约束。
- 未登录点互动按钮 → 顶部 toast 显示 `ERR_NEED_LOGIN`，不会无声失败。

## HTTP API

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/state` | 当前完整快照 |
| GET | `/api/events` | SSE 推送（约 0.35s 一次，仅变化时发） |
| POST | `/api/cmd` | 执行白名单内命令：`{"name": "...", "args": {...}}`（26 个名字，含 inbox / reply / roam 系列） |
| GET | `/api/inbox` | 收件箱：`kind` / `only_unreplied` / `limit` |
| POST | `/api/reply` | 回复某条消息：`msg_id` + `message` |
| GET | `/api/folders` | 收藏夹列表 |
| GET | `/api/roam` | 巡览状态 + 任务列表 |
| POST | `/api/roam/set` | 改配置：开关 / 频率 / 上限 / 来源 / 关键词 |
| POST | `/api/roam/check` | 立即跑一轮取片 |
| POST | `/api/roam/next` | 取一个 waiting 任务 |
| POST | `/api/roam/comment` | 回填并发布评论 |
| POST | `/api/roam/skip` | 跳过任务 |
| GET | `/api/thumb` | 封面图代理（绕过 B 站 referer 限制） |
| POST | `/api/auth/qr` `/api/auth/poll` `/api/auth/logout` | 扫码登录三件套 |

## 数据文件

全部在 `.data/`，已被 `.gitignore` 排除（含登录态，**绝不入库**）：

| 文件 | 内容 |
|---|---|
| `cookie.json` | 登录态（仅 bilibili 域，权限 0600） |
| `inbox.json` | 收件箱 baseline + 已回复记账（防重复回同一条） |
| `roam.json` | 巡览配置、seen_bvid、任务列表、统计（picked / sent / skipped / failed / today_sent） |
| `subs/` | `subtitle --export` 导出的字幕文件 |

`inbox.json` / `roam.json` 同时被 `store` 镜像，因此 MCP 侧发的回复、网页侧点的按钮，
两边看到的是同一份进度，不会互相覆盖。

## 风控

未登录调搜索接口会被 B 站拦（返回 `code=0` 但 `data` 里只有 `v_voucher`）。`http.py` 已内置：

1. 先访问 `https://www.bilibili.com` 暖机拿 `b_nut` / `buvid3` 等 Cookie；
2. 搜索参数只保留最小集 `keyword` / `search_type` / `page`，多带即被拦；
3. `Referer` 必须为 `https://search.bilibili.com/all?keyword=<编码>`；
4. 命中 `v_voucher` 则丢弃 `buvid3` / `buvid4` / `b_nut`、退避 6 秒、重新暖机后重试一次。

巡览器另有自己的节流：频率硬下限 120 秒、一轮最多试 8 个候选、每片只处理一次，
避免"自动化"把账号跑进风控。

## 安全与边界

- **写操作是真实的**：点赞、投币、收藏、发评论、回消息都会改写你的 B 站账号，
  调用即产生副作用（投币不可撤回）。工具描述里逐条标注「需要登录态」，未登录一律阻断。
- **不生成文案**：`inbox` / `roam_next` 只提供素材（谁说了什么、字幕正文），
  文案由调用方 AI 或人在输入框里给，项目内没有任何"自动回话"的隐藏逻辑。
- **不会群发**：收件箱有 baseline + 已回复记账，巡览有 seen_bvid + 每日上限，
  重复调用不会把历史消息重发一遍。
- **不碰验证码**：扫码登录依赖你手机上的确认，项目不做任何绕过。

## 已知限制

- **已实现接口**：search / hot_videos / view / pages / comments / comment / like / coin / fav / unfav /
  folders / subtitles / subtitle / nav(me) / msgfeed(reply, at)。**未实现**：高码率 playurl（真实取流）、删评论、发弹幕。
- **时长秒位**：搜索接口对部分视频返回的 `duration` 本身是 `16:0` 这种缺位值，前端原样显示，不补零
  ——补零等于伪造数据。`open_video` 后拿到的 `dur` 是精确秒数。
- **逻辑播放**：不对接真实播放器，`t` 是客户端时钟推算值，不是解码进度。
- **巡览只处理有字幕的视频**：没字幕就看不懂内容，宁可不发也不硬凑；这类视频只记 skipped 并进 seen。
- **单进程单账号**：一份 `.data/` 对应一个登录态，多账号请复制项目目录或改 `DATA_DIR`。

## 自检

```powershell
python main.py selftest
```

依次跑 warmup → search → open_video → play/seek/pause → comments + 写操作守卫 → subtitles →
state，任一环节风控或报文异常会打印错误码。写操作守卫用非法 bvid 测试，不会真的点赞你的账号。

## 版本

| 版本 | 内容 |
|---|---|
| v0.1.x | 搜索 / 详情 / 逻辑播放 / 评论 / 点赞投币收藏 |
| v0.2.0 | CC / AI 字幕读取与导出 |
| v0.3.0 | 消息收件箱（回复我 / @我）、AI 代写回复、AI 巡览；后台改浅色中性主题 |
| v0.4.0 | 投稿（发视频）：`upload` / `upload_zones` / `upload_list`；upos 分片上传（多线程、分片重试、合并重试）+ 封面上传（16:10 裁剪）+ web add 提交；默认 dry-run 安全阀 |
| v0.3.1 | 修正安装说明：新增环境要求 / 安装判定 / 启动排错；`requirements.txt` 锁定 `mcp>=1.2,<3`；MCP 层兼容 mcp 1.x `FastMCP` 与 2.x `MCPServer` 两种类名及 `run()` 签名 |

## License

[MIT](LICENSE)
*（内容由AI生成，仅供参考）*
