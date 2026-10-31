---
AIGC:
    Label: "1"
    ContentProducer: 001191440300708461136T1XGW3
    ProduceID: 297ab8dbc8f4cde8dd6b234597dbc01a_c1bc4bfacf9a11f19f8752540064ee0f
    ReservedCode1: a20dOCpHY3cqLMmhllJAwgmScFF+Di2K5QZBEBiLYKKPRkepmJivL73ul5wUWLP38G6VVnVLo+nSKU0NexzemuuW9ZFqj/8AAmjMYsYfRTY9g+2RQoCzCPrdc4vuVGGasjGyvv5XlStFPMznVBMXCUTqvbJ9JKr4Y/2mQaJ6cESyDkfvNXfbRn9Ex1k=
    ContentPropagator: 001191440300708461136T1XGW3
    PropagateID: 297ab8dbc8f4cde8dd6b234597dbc01a_c1bc4bfacf9a11f19f8752540064ee0f
    ReservedCode2: a20dOCpHY3cqLMmhllJAwgmScFF+Di2K5QZBEBiLYKKPRkepmJivL73ul5wUWLP38G6VVnVLo+nSKU0NexzemuuW9ZFqj/8AAmjMYsYfRTY9g+2RQoCzCPrdc4vuVGGasjGyvv5XlStFPMznVBMXCUTqvbJ9JKr4Y/2mQaJ6cESyDkfvNXfbRn9Ex1k=
---



# bili-ai 使用技能

bili-ai 把 B 站变成一台 AI 可读可写的机器：**读**走搜索 / 详情 / 字幕 / 评论 / 收件箱，**写**走评论 / 回复 / 互动 / 投稿。本技能告诉你怎么用它的 MCP 工具把事做成，以及哪些地方一做就出事。

## 1. 适用与不适用

**用**：
- 搜 B 站视频、看某个 BV 的详情与数据
- 读视频字幕（CC / AI 字幕）做总结、提炼、改写、写评论素材
- 在视频下发评论、回复某条评论、回复“别人回复我 / @我”的消息
- 点赞 / 投币 / 收藏
- 看某个时间点的弹幕氛围、发一条弹幕（需人确认）
- 把本地视频文件投稿上传（需人确认）
- 巡览：从候选中取片 → 读字幕 → 写评论任务 → 回填发布

**不用**：需要真实播放画面 / 取流下载 / 删评论（项目未实现）；也不需要它来“生成文案”——**项目内不接大模型、不生成任何文案**，文案永远由你（调用方 AI）写。

## 2. 连接方式

客户端配置（stdio）：

```json
{
  "mcpServers": {
    "bili-ai": {
      "command": "python",
      "args": ["D:\\projects\\bili-ai\\main.py", "mcp"]
    }
  }
}
```

- `python main.py mcp`：stdio 工具服务 + 顺带起后台（浏览器可扫码登录）
- `python main.py mcp --no-web`：纯 stdio，不起后台
- 依赖要求：Python 3.10+，`mcp>=1.2,<3`（1.x 类名 FastMCP，2.x 叫 MCPServer，两者都兼容）。装错版本会直接 ImportError，先 `python -m pip install -U "mcp>=1.2,<3"`
- 必须能访问 bilibili.com；跨境网络容易触发 `v_voucher` 风控

## 3. 输出协议（先看懂，再调用）

所有工具**只返回纯文本**，不是 JSON：

```
view=video  bvid=BV1uronYREWR  av=114339210073708  title="..."  up=xxx(mid=123)  dur=1626
stat: play=31.3万 like=1.5万 coin=1.1万 fav=1.6万 danmaku=542 reply=495
...
actions: [play] [pause] [seek +/-sec] [comments --page] [subtitles] [upload --file 路径 ...]
```

三条读取纪律：
1. **首行 `view=` 决定你处在什么视图**（search / video / comments / subtitles / subtitle / inbox / archives / zones / idle）。变更操作前先看首行，别拿旧视图的假设去发下一句。
2. **末行 `actions:` 是当前可做的下一步**。不确定能干什么时读它，不要凭记忆猜命令名。
3. **只取你要的行**。`state` 全量、`status` 一行；`subtitle` 用 `offset/limit` 分页，不要一次拉 683 条只为回答一个问题。

## 4. 状态模型（最容易踩的地方）

- 服务端只有一个 **StateStore**，`open_video` 会把视频设为「当前视频」，此后所有带 `bvid` 参数的工具**留空即作用于当前视频**。
- 状态只活在一个进程里：
  - MCP 常驻进程 → 状态跨调用保留，`open_video` 一次即可反复用。
  - `python main.py cli <cmd>` **每次都是新进程**，`open_video` 和后续 `comments` 分两条命令跑会报 `ERR_NO_TARGET`。要走 CLI 就一条命令带上 `--bvid`，或者干脆写 Python 脚本在一个进程里 `await core.execute(...)` 串起来（`Core.execute` 是 async）。
- `play/pause/resume/seek` 是**逻辑播放器**：只推进秒数、不解码、不取流。`seek` 只改进度，不会带来画面或音频。

## 5. 工具清单

### 浏览与定位
| 工具 | 参数 | 说明 |
|---|---|---|
| `search` | `keyword`, `page=1`, `limit=10` | 关键词搜片，返回带 `#序号` 的列表（bvid / 时长 / 播放量 / UP） |
| `open_video` | `bvid` | 打开详情（标题/UP/时长/互动/desc），并设为当前视频 |
| `state` | — | 全量状态快照（视图 + 列表 + 播放进度 + 可用动作） |
| `status` | — | 只有一行播放状态，最省 token |

### 播放（逻辑）
| 工具 | 参数 |
|---|---|
| `play` | `bvid=""`, `at=0.0`（传 bvid 会先打开） |
| `pause` / `resume` | — |
| `seek` | `to`："120" 绝对秒，"+15" / "-30" 相对秒 |

### 字幕（读内容的唯一可靠入口）
| 工具 | 参数 | 说明 |
|---|---|---|
| `subtitles` | `bvid=""`, `page=1` | 列字幕轨（`lan` / `lan_doc` / `lock`），`page` 是第几 P |
| `subtitle` | `bvid=""`, `lang=""`, `page=1`, `offset=0`, `limit=200`, `export=""` | 读正文，每行 `[mm:ss] 文本`；`lang` 空则自动选中文（ai-zh 优先）；`limit` ≤500；`export=srt/txt/md` 落盘到 `.data/subs/` 并返回路径 |

要点：
- **画面内嵌的硬字幕取不到**，只有 CC / AI 字幕有数据源。`tracks=0` 就不要再试，换片。
- 部分 AI 字幕要求登录态，未登录时轨道存在但读不出正文。
- 长视频一律分页读：先 `limit=200` 拿开头判断内容，需要细节再按 `next:` 的 offset 续读。

### 评论
| 工具 | 参数 | 说明 |
|---|---|---|
| `comments` | `bvid=""`, `page=1`, `limit=20`, `mode=3` | `mode` 3=热门 2=按时间；真分页（`limit` 上限 20），置顶条目带 `top=true`，只在第 1 页出现且已去重 |
| `comment` | `message`, `bvid=""`, `root=0`, `parent=0` | **真实发布、不可撤回**；`root/parent` 非 0 是回复某条评论（都填目标 rpid） |

### 弹幕
| 工具 | 参数 | 说明 |
| `danmaku_list` | `bvid=""`, `page=1`, `offset=0`, `limit=100`, `around=-1`, `window=30` | 读当前 P 的弹幕，按时间升序；`around` 给秒数时只取该点前后 `window` 秒（`window` 默认 30）；`limit` ≤500，`offset` 是游标，`is_end=true` 表示到底 |
| `danmaku` | `message`, `bvid=""`, `at=""`, `mode=1`, `color="#FFFFFF"`, `fontsize=25`, `confirm=False` | **发弹幕**。默认 `confirm=False` 只回执计划（dry-run）；`at` 支持 `"90"` / `"1:30"` / `"1:30.5"`，留空取当前播放进度；`mode` 1 滚动 / 4 底部 / 5 顶部；`message` 必填且 ≤100 字 |

发弹幕的规矩（与 `comment` 同级，别当只读用）：
1. 先用 `danmaku_list` 带 `around` 看那个时间点大家在说什么，文案要贴着内容，不要发通用灌水（「666」「前排」这类重复弹幕容易被风控）。
2. 想好文案、时间点后**先给用户看**，拿到明确同意才带 `confirm=true` 真正发出。
3. 发出后公开可见、**不可撤回**（本端不提供删除）；报错码原样回给用户，不要自己换文案重试。
4. `visible=` 是接口可见条数，不是视频页的弹幕总数；只有该 P 有弹幕，`page` 是第几 P。

### 互动（真实写账号）
| 工具 | 参数 | 说明 |
|---|---|---|
| `like` | `bvid=""`, `on=True` | `on=False` 取消点赞 |
| `coin` | `bvid=""`, `n=1`, `also_like=False` | `n` 只能 1 或 2，硬币有限 |
| `fav` | `bvid=""`, `folder=""` | folder 可填收藏夹 **id 或名称**，空=第一个收藏夹 |
| `unfav` | `bvid=""`, `folder=""` | 空=从所有收藏夹移除 |
| `folders` | — | 列收藏夹（id/名称/条目数），选 folder 前先看 |

### 账号
| 工具 | 说明 |
|---|---|
| `me` | 当前账号（昵称/mid/大会员）。**这是判断“是否登录”的官方方式** |

### 消息（只检测，不自动回）
| 工具 | 参数 | 说明 |
|---|---|---|
| `inbox` | `kind="all"`（all/reply/at）, `limit=20`, `only_unreplied=True` | 别人回复我 / @我的评论；每条给 `msg_id`、谁、来自哪个视频、对方说了什么、我的原评论 |
| `reply` | `msg_id`, `message` | 发布回复。`msg_id` 取自 inbox；`message` 必填且 ≤1000 字 |

### 巡览（自动挑片）
| 工具 | 参数 | 说明 |
|---|---|---|
| `roam_status` | — | 开关 / 频率 / 每日上限 / 来源 / 统计 / 最近任务（含 skipped 原因） |
| `roam_config` | `on=None`, `interval=0`, `max_per_day=-1`, `source=""`, `keyword=""`, `llm_draft=None`, `llm_publish=None` | 改巡览配置；`interval` 下限 120 秒，`llm_draft` / `llm_publish` 控制是否让大模型拟稿 / 直接发 |
| `roam_next` | `task_id=""` | 取一个 waiting 任务：视频信息 + 字幕正文（前 2000 字） |
| `roam_draft` | `task_id=""`, `publish=None`, `force=False` | 让内置大模型写评论文案，**只写草稿不发**；`publish=True` 切换该任务为自动发 |
| `roam_draft_set` | `task_id`, `text`, `by="agent"` | 把你自己写的文案存成草稿（`by` 记来源），只存不发 |
| `roam_send` | `task_id=""` | 发布已有草稿（**真实发布、不可撤回、受每日上限**）；留空取最早一个带草稿的任务 |
| `roam_comment` | `task_id`, `text` | 跳过草稿直接发布文案，**受每日上限约束** |
| `roam_skip` | `task_id`, `reason="manual"` | 跳过任务并记原因 |

### 外部大模型（可选，OpenAI 兼容）
| 工具 | 参数 | 说明 |
|---|---|---|
| `llm_status` | — | 模型 / Key 掩码 / 今日拟稿与发送数 / 最近错误；**Key 永不明文回显** |
| `llm_configure` | `api_key=""`, `base_url=""`, `model=""`, `roam_prompt=None`, `prompt=None`, `clear_key=None`, `mode=""` | 配置上游；`prompt` / `roam_prompt` 不传=不变，传空串 `""`=恢复内置默认（默认值不会写进 `.data/llm.json`）；`mode` 为内置 AI 的能力模式（`weak` 只发评论 / `full` 除发视频外全功能）；Key 只落本地 `.data`，不进仓库 |
| `llm_test` | — | 真打一次上游接口验证 Key / 地址 / 模型，**不写任何 B 站数据** |
| `llm_audit` | `limit=20` | 内置 AI 最近的动作审计：命令 / 当时模式 / 放行或被拒 / 原因，最新在前 |

### 投稿（不可撤回）
| 工具 | 参数 | 说明 |
|---|---|---|
| `upload_zones` | `top=0` | 列分区，拿 `tid` |
| `upload_video` | `file`, `title`, `tid`, `desc=""`, `tags=""`, `cover=""`, `copyright=1`, `source=""`, `tid_v2=0`, `confirm=False` | `confirm=False` 默认**只预检不发一个字节**；`confirm=True` 才真上传+提审 |
| `upload_list` | `pn=1`, `ps=10` | 我的稿件与审核状态 |

校验规则（违反直接返回 ERR_）：
- `title` ≤80 字、`desc` ≤2000 字
- `tags` 逗号分隔，≤10 个、单个 ≤20 字
- `copyright=2`（转载）必须给 `source`
- `tid` 必填，用 `upload_zones` 查；`tid_v2` 一般留 0，缺了会自动反查一次
- 视频格式：mp4/flv/mov/mkv/avi/wmv/webm/m4v，单文件 ≤8G；封面会自动裁成 16:10

## 6. 标准流程（照抄）

### A. 找片 → 读字幕 → 总结
1. `search(keyword="...")` → 挑 `bvid`（结合播放量/UP/时长，不要只看标题）
2. `open_video(bvid="BV...")` → 确认它真是用户要的那个（标题对不上就换）
3. `subtitles(bvid="")` → 看 `tracks`
   - `tracks=0` → 告诉用户“该片只有硬字幕/无字幕，取不到正文”，不要编内容
4. `subtitle(lang="", offset=0, limit=200)` → 读；需要更多按末行 `next:` 继续
5. 输出总结时以字幕原文为准，不要用标题推断内容

### B. 发评论（发布前必须让人确认）
1. 先 `open_video`，再用 `subtitles` + `subtitle` **真的读一遍内容**（读不到字幕 → 不发）
2. 自己写一条：具体、有信息量、不堆 emoji、不 AI 腔；不要复述视频简介
3. 把「视频标题 + 评论原文」交给用户确认（确认卡片），得到明确同意才继续
4. `comment(message="...")` 发布 → 返回 `ok comment  rpid=...`
5. `comments(mode=2)` 翻一页找该 `rpid` 验证已落库

### C. 回复消息
1. `inbox(kind="all", only_unreplied=True)` → 取 `msg_id` 与上下文
2. 按对方说的话写回复（≤1000 字），用确认卡片过一遍
3. `reply(msg_id="...", message="...")` → 项目自动记账，重复的 `msg_id` 不会再出现在未回复列表

### D. 巡览
1. `roam_status()` 确认开关与今日剩余额度
2. `roam_next()` → 拿视频 + 字幕
3. 写评论 → 用户确认 → `roam_comment(task_id, text)`
4. 不想发就跳过（CLI `roam_skip`），不要为了“清任务”硬发

### E. 投稿
1. `upload_zones()` → 选 `tid`（例：230 软件应用、231 计算机技术、21 日常）
2. `upload_video(file=..., title=..., tid=..., tags=..., cover=..., confirm=False)` → **dry-run 预检**，逐字段核对
3. 把全部字段 + 文件大小/时长/标题给用户确认（公开发布、不可撤回、过审才可见）
4. `upload_video(..., confirm=True)` → 真传
5. `upload_list()` 看 `state_desc` 是否过审

## 7. 错误码与处置

| 返回 | 含义 | 怎么办 |
|---|---|---|
| `ERR_NEED_LOGIN: ...` | 未登录 / SESSDATA 失效 | 后台扫码或 `python main.py login`，别重试 |
| `ERR_NO_TARGET: 先 open_video 或传 bvid` | 没有当前视频（CLI 多进程最常见的病） | 传 `bvid`，或同一进程内先 `open_video` |
| `ERR_NEED_TEXT` / `ERR_TOO_LONG` | 文案为空 / 超 1000 字 | 写文案，压到 1000 字内 |
| `ERR_NEED_MSG_ID` / `ERR_NO_MSG` | 缺 msg_id / 消息已过期 | 重新 `inbox` 取新的 |
| `ERR_TITLE` / `ERR_DESC` / `ERR_TAG` / `ERR_TID` / `ERR_SOURCE` | 投稿字段不合法 | 按提示改，不要硬提交 |
| `ERR_BAD_FOLDER` / `ERR_NO_FOLDER` | 收藏夹名/ID 不存在 | 先 `folders` |
| `ERR_BAD_EXPORT` | export 只能是 srt/txt/md | 改参数 |
| `BAD_ARGS: ...` | 工具名存在但参数名写错（例如给 `comments` 传了 `sort`） | 回第 5 节核对参数名 |
| `ApiError -101 / -403` | 未登录 / 无权限 | 重新登录 |
| `ApiError -412 / -352 / -799` | 风控 / 请求过快 / 频繁 | **停手**，等冷却 |
| `21001` 参数错 / `21003` 分区不存在 / `21004` 标题不合法 | 投稿字段问题 | 改正后重提 |
| `21103` 重复投稿 | 同一稿件已投过 | 换内容或不再重试 |
| `21564` 投稿过于频繁 | 频率限制，**每次提交都算一次投稿** | 停止重试，等数十分钟以上 |
| `21540` 需网页端人工过校 | 稿件被要求人工审核 | 交给用户走网页端 |
| `preupload HTTP 406 code=601「您上传视频过快」` | 上传通道风控冷却 | 不重试，等冷却；可换网页端上传 |

## 7.5 AI 能力模式（跟你有关，也跟你无关）

`weak` / `full` 约束的是**用户自己配了 Key 的内置 AI**（收件箱自动回复、巡览拟稿/自动发布），
它们在调 `Core.execute` 时带 `actor="ai"`，越权会直接返回 `ERR_AI_MODE_DENIED`。

通过 MCP 接入的你（外部客户端）走 `actor="user"`，**不受该模式限制**；但发视频永远要人确认，
不要因为"用户开了强模式"就去点赞投币。看到 `ERR_AI_MODE_DENIED` 时的正确反应：告诉用户
「这动作被当前 AI 模式拦了，需要切强模式」，**不要**试图用别的命令绕过去，也不要替用户改 `mode`。

## 8. 写操作纪律（硬规则）

- **不可撤回**：`comment` / `reply` / `roam_comment` / `upload_video(confirm=True)` 都是公开发布。文案与稿件字段**先给用户确认**再发。
- **不要用无效参数试探接口**：拿错 `tid`、假文件名去试 `upload_video(confirm=True)`，每试一次都算一次投稿，会吃 `21564` 甚至把 SESSDATA 试掉线。想验证就 `confirm=False`（零网络副作用）。
- **风控不重试**：命中 `21564 / 601 / -412 / -352 / -799` 立即停手，同参数重试只会加深冷却。
- **文案不由项目生成**：`inbox` / `roam_next` 只产出“待回填任务”，`reply` / `roam_comment` 的正文必须你自己写。
- **点赞投币收藏是真实副作用**：测试守卫用非法 bvid（如 `BV1xxINVALID`）验证，别拿真视频试；误操作立即用 `like(on=False)` / `unfav` 回滚。
- **每日上限**：巡览发布有配额，别把它当群发通道。

## 9. 省 token 与稳定性

- 只读一句话用 `status`，不要 `state`。
- `search` 的 `limit` 保持 5~10；`subtitle` 的 `limit` 保持 200 起，够用就停。
- 同一视频连续操作时，只 `open_video` 一次，后面 `bvid` 全留空。
- Cookie 热重载对请求生效，但 **MCP 客户端缓存的连接要立刻生效仍建议重启**。

## 10. 反例（不要这么做）

- ❌ 只看标题就写评论（必须先读字幕）
- ❌ 取不到字幕硬编内容
- ❌ 一次 `subtitle` 拉全量 683 行只为回答“这视频讲什么”
- ❌ CLI 分两条命令 `open_video` 然后 `comments`
- ❌ `upload_video(confirm=True)` 当探测手段连发
- ❌ 用 `like` / `coin` 测连通性
- ❌ 把 `play` 当成能放画面
