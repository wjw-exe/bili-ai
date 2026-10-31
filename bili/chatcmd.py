"""打字控制：把自然语言翻成 bili-ai 命令

三条硬边界（改代码时别放宽）：
1. **写操作默认要确认**：评论 / 回复 / 弹幕 / 点赞 / 投币 / 收藏 / 巡览开关，
   解析结果先回前端「确认」，点了才真执行。会话里 15 条/天的限速照旧生效。
2. **规则优先、模型兜底**：常见句式用正则直接命中，零延迟也不会误解；只有正则
   认不出的长尾说法才交给本地小模型（qwen2.5-0.5b）翻成 JSON。
3. **模型不许自由发挥**：它只能从命令表里挑一个，参数还要过白名单与类型校验；
   输出解析不出来就退回闲聊，绝不执行猜出来的写操作。

对外只有两个入口：
    parse(text, ctx)          同步、纯正则
    aparse(llm, text, ctx)    先正则，未命中再问模型（llm 可为 None）
"""

from __future__ import annotations

import json
import re

# ---------------- 命令表 ----------------

# name -> 允许的参数名（其余一律丢弃，避免模型瞎编参数）
ARGS: dict[str, set[str]] = {
    "search": {"keyword", "page", "limit"},
    "open_video": {"bvid"},
    "comments": {"limit"},
    "comment": {"message", "bvid"},
    "reply": {"index", "msg_id", "message"},
    "inbox": {"limit"},
    "like": {"on"},
    "coin": {"n"},
    "fav": set(),
    "danmaku": {"message"},
    "play": set(),
    "pause": set(),
    "resume": set(),
    "seek": {"to"},
    "state": set(),
    "roam": {"on"},
}

# 真·写操作：默认必须用户点头
WRITE = {"comment", "reply", "danmaku", "like", "coin", "fav", "roam"}

LABEL = {
    "search": "搜索", "open_video": "打开视频", "comments": "看评论",
    "comment": "发评论", "reply": "回复消息", "inbox": "看消息",
    "like": "点赞", "coin": "投币", "fav": "收藏", "danmaku": "发弹幕",
    "play": "播放", "pause": "暂停", "resume": "继续播放", "seek": "跳转",
    "state": "看状态", "roam": "自动巡览",
}

INT_ARGS = {"index", "n", "limit", "page", "ts"}


# ---------------- 文本预处理 ----------------

_PUNCT = "：:，,。！!？?、~～ "
_LEAD = re.compile(r"^(?:请你|请|帮我|帮忙|麻烦你|麻烦|给我|替我|你|能|可以|去|我想要|我要)+")
_TAIL = re.compile(r"[。！!~～\s]+$")


def _clean(text: str) -> str:
    t = (text or "").strip()
    t = t.replace("：", ":").replace("，", ",")
    t = _TAIL.sub("", t)
    return t


def _strip_lead(t: str) -> str:
    prev = None
    while prev != t:
        prev = t
        t = _LEAD.sub("", t).strip()
    return t


def _kw(raw: str) -> str:
    """从「找一下关于机械键盘的视频」里抠出「机械键盘」"""
    k = raw.strip(" \"'“”‘’《》「」")
    k = re.sub(r"^关于", "", k)
    k = re.sub(r"(的)?(视频|影片|内容|片子)$", "", k)
    k = re.sub(r"^(一下|一些|几个|一个)", "", k)
    k = re.sub(r"^(B站|b站|哔哩哔哩)(上|里)?的?", "", k)
    for _ in range(2):                     # 「看门狗相关的作品」→「看门狗」
        k = re.sub(r"(相关|有关)(的)?$", "", k).strip()
        k = re.sub(r"(的)?(视频|影片|内容|片子|作品)$", "", k).strip()
    return k.strip(" \"'“”‘’《》「」")


_BV = re.compile(r"BV[0-9A-Za-z]{10}")

CAPS = ("我能替你操作 B 站：搜视频、打开某个视频、看评论、发评论、发弹幕、回复消息、"
        "点赞投币收藏、开关自动巡览、看当前状态。直接说人话就行，"
        "比如「搜 看门狗」「评论一句 讲得好」「回复第二条 收到」。")

OFFTOPIC = ("我只管 B 站这边的事：搜视频、看评论、发评论、回复消息、点赞收藏。"
            "这个我帮不上，换个 AI 助手更靠谱。")


_CN_NUM = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


# ---------------- 规则解析 ----------------

def _rule(text: str, ctx: dict) -> dict | None:
    t = _strip_lead(_clean(text))
    if not t:
        return None
    low = t.lower()
    bvid = (_BV.search(t).group(0) if _BV.search(t) else "")

    # 0) 能力询问 / 与 B 站无关的话题：给固定回话，别让 0.5B 现编命令
    if re.search(r"(能|会|可以)(干|做|帮)(点|些|什么|啥)|你能干啥|你是谁|你会什么"
                 r"|怎么用你|有什么功能|干什么用|你能做什么", t):
        return {"name": "_chat", "args": {"say": CAPS}}
    if re.search(r"(天气|气温|股票|汇率|金价|几点|几号|星期几|讲个笑话|翻[译译])", t):
        return {"name": "_chat", "args": {"say": OFFTOPIC}}

    # 1) 回复消息：必须有「回复 + 第N条 / 消息」双要素
    m = re.search(r"回复\s*第?\s*([0-9]+|[一二两三四五六七八九十])\s*[条个]?\s*"
                  r"(?:那条|这条)?\s*(?:消息|私信)?\s*[:\s,]*(.*)$", t)
    if m and "回复" in t and (m.group(1) or "消息" in t or "私信" in t):
        raw_idx = m.group(1) or "1"
        idx = int(raw_idx) if raw_idx.isdigit() else _CN_NUM.get(raw_idx, 1)
        msg = m.group(2).strip(" \"'“”‘’")
        return {"name": "reply", "args": {"index": idx, "message": msg}}

    # 2) 看评论（先于「发评论」判定，避免「看看评论」被当成发评论）
    if re.search(r"(看|看看|看下|看一下|拉|拉一下|刷新|打开|显示)(一下)?(评论|评论区)", t) \
            or t in ("评论", "评论列表", "评论区") \
            or re.search(r"^(评论)(区|里|中)", t) \
            or re.search(r"(评论区|评论里|评论中).{0,4}(说|聊|讲|写)", t) \
            or re.search(r"(别人|大家|网友|他们)(都|在)?.{0,3}(说|聊|讲)(啥|什么)", t):
        return {"name": "comments", "args": {}}

    # 3) 发评论：带内容（「评论一句 X」里的量词要吃掉，不能当成正文）
    m = re.search(r"(?:发|写|留|评|发个|发一条|写一条)?(?:一?[条个])?"
                  r"(?:评论|评一句|说一句)\s*(?:一句|一下|一个)?\s*[:\s,]*(.+)$", t)
    if m:
        msg = m.group(1).strip(" \"'“”‘’")
        if msg and msg not in ("一下", "吧", "看看"):
            return {"name": "comment", "args": {"message": msg, "bvid": bvid}}

    # 4) 弹幕
    m = re.search(r"发?(?:一?[条个])?弹幕\s*[:\s,]*(.+)$", t)
    if m and not re.search(r"(看|拉|列表|关)", t):
        msg = m.group(1).strip(" \"'“”‘’")
        if msg:
            return {"name": "danmaku", "args": {"message": msg}}

    # 5) 三连
    if re.search(r"三连|一键三连", t):
        return {"name": "triple", "args": {}}

    # 6) 点赞 / 取消
    if re.search(r"(取消|撤)(一下)?(赞|点赞)", t):
        return {"name": "like", "args": {"on": False}}
    if re.search(r"(点个?赞|赞一下|点赞|给.{0,8}点赞|投个赞)", t) and not re.search(r"投币", t):
        return {"name": "like", "args": {"on": True}}

    # 7) 投币
    m = re.search(r"投\s*([0-9一二两]?)\s*(个|枚)?\s*币", t)
    if m:
        n = {"一": 1, "二": 2, "两": 2, "": 1}.get(m.group(1) or "", None)
        return {"name": "coin", "args": {"n": int(n or 1) if not str(m.group(1)).isdigit() else int(m.group(1))}}

    # 8) 收藏
    if re.search(r"收藏", t) and "收藏夹" not in t:
        return {"name": "fav", "args": {}}

    # 9) 消息收件箱
    if re.search(r"(看|看看|看下|查|有没有|还有)(一下)?(未回复的?|没回的?)?(消息|私信|回复|@)", t) \
            or re.search(r"收件箱|谁(回复|回|@|艾特)我|有人(回复|回|@|艾特)我", t):
        return {"name": "inbox", "args": {}}

    # 10) 播放控制
    if re.search(r"^(暂停|停一下|停|别放了)", t):
        return {"name": "pause", "args": {}}
    if re.search(r"^(继续|接着放|继续播|恢复播放|接着播)", t):
        return {"name": "resume", "args": {}}
    if re.search(r"^(播放|开播|放到)", t) and (bvid or "视频" in t):
        return {"name": "play", "args": {}}

    # 11) 状态
    if re.search(r"^(状态|当前状态|什么状态|现在在干嘛|在干什么|进度|当前视频|现在看的是)", t) \
            or re.search(r"(什么|啥|当前|现在).{0,3}(状态|情况)", t) \
            or re.search(r"在(干嘛|干什么)|进度如何|看到哪", t):
        return {"name": "state", "args": {}}

    # 12) 巡览开关
    m = re.search(r"(开启|打开|开始|启动|关掉|关闭|停止|停了|别)(一下)?(自动)?(巡览|巡视|自动逛)", t)
    if m:
        on = m.group(1) in ("开启", "打开", "开始", "启动")
        return {"name": "roam", "args": {"on": on}}
    if re.search(r"^(自动巡览|巡览)", t):
        return {"name": "roam", "args": {"on": True}}

    # 13) 打开视频
    if bvid:
        return {"name": "open_video", "args": {"bvid": bvid}}
    if re.search(r"打开(这个|当前|现在)?(视频|它)", t):
        return {"name": "open_video", "args": {"bvid": ctx.get("bvid") or ""}}

    # 14) 搜索
    m = re.search(r"^(搜索|搜一下|搜搜|搜|找一下|找找|找|查一下|查|看看有没有|有没有)(.*)$", t)
    if m:
        kw = _kw(m.group(2))
        if kw:
            return {"name": "search", "args": {"keyword": kw}}
    # 「看看 X 的视频 / 我想看 X 的作品」也是搜（前面已排掉看评论、看消息）
    m = re.search(r"^(?:我想看看|我想看|想看看|看看|想看|想找|来点|看点)(.+)$", t)
    if m and re.search(r"(视频|影片|片子|内容|作品)", t):
        kw = _kw(m.group(1))
        if kw:
            return {"name": "search", "args": {"keyword": kw}}
    return None


# ---------------- 模型解析 ----------------

SYSTEM = (
    "你把用户的话翻成一条 B 站客户端命令，只输出一行 JSON，不要解释、不要代码块。\n"
    "命令表：\n"
    '搜索视频: {"name":"search","args":{"keyword":"关键词"}}\n'
    '打开视频: {"name":"open_video","args":{"bvid":"BV1xxxxxxxxx"}}\n'
    "注意：open_video 的 bvid 必须照抄用户给的、BV 开头的 12 位真实号码；用户没给具体号码时，改用 search 按关键词搜。\n"
    '看当前视频评论: {"name":"comments","args":{}}\n'
    '给当前视频发评论: {"name":"comment","args":{"message":"评论内容"}}\n'
    '回复收件箱第N条消息: {"name":"reply","args":{"index":2,"message":"回复内容"}}\n'
    '看未回复消息: {"name":"inbox","args":{}}\n'
    '点赞: {"name":"like","args":{"on":true}}\n'
    '投币: {"name":"coin","args":{"n":1}}\n'
    '收藏: {"name":"fav","args":{}}\n'
    '发弹幕: {"name":"danmaku","args":{"message":"内容"}}\n'
    '看当前状态: {"name":"state","args":{}}\n'
    '开关自动巡览: {"name":"roam","args":{"on":true}}\n'
    '只是聊天、不是操作: {"name":"chat","args":{"say":"一句话回答"}}\n'
    "例子：\n"
    "用户：随便搜点嵌入式开发的视频看\n"
    '{"name":"search","args":{"keyword":"嵌入式开发"}}\n'
    "用户：这条讲得不错，帮我评一句\n"
    '{"name":"comment","args":{"message":"讲得不错"}}\n'
    "用户：今天天气怎么样\n"
    '{"name":"chat","args":{"say":"我只管B站这边的事，天气你问别人更靠谱。"}}\n'
)


def _extract_json(text: str) -> dict | None:
    """从模型输出里抠出第一个 JSON 对象；容忍前后废话和代码块围栏"""
    s = (text or "").strip()
    s = re.sub(r"^```(?:json)?|```$", "", s, flags=re.M).strip()
    start = s.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(s)):
        if s[i] == "{":
            depth += 1
        elif s[i] == "}":
            depth -= 1
            if depth == 0:
                blob = s[start:i + 1]
                for cand in (blob, blob.replace("'", '"'), re.sub(r",\s*([}\]])", r"\1", blob)):
                    try:
                        d = json.loads(cand)
                        return d if isinstance(d, dict) else None
                    except ValueError:
                        continue
                return None
    return None


def _from_model(raw: str) -> dict | None:
    d = _extract_json(raw)
    if not d:
        return None
    name = str(d.get("name") or d.get("cmd") or "").strip()
    args = d.get("args") if isinstance(d.get("args"), dict) else {}
    if name == "chat":
        say = str(args.get("say") or d.get("say") or "").strip()
        return {"kind": "chat", "say": say or (raw or "").strip()[:400], "source": "llm"}
    if name not in ARGS:
        return None
    cmd = {"name": name, "args": args}
    if name == "triple":
        pass
    return {"kind": "cmd", "cmds": [cmd], "source": "llm"}


# ---------------- 参数归一 ----------------

_KW_HEAD = ("有没有", "有没", "帮我", "帮忙", "给我", "我想", "我要", "想看",
            "麻烦", "请", "关于", "讲讲", "聊聊", "搜一下", "搜索", "找一下", "看看")
_KW_TAIL = ("相关视频", "相关的", "的东西", "的视频", "的内容", "之类", "什么的",
            "这些", "那些", "吧", "呢", "啊", "嘛", "哈")


def _clean_kw(s: str) -> str:
    """小模型常把整句当关键词，这里削掉口语壳子，只留检索词"""
    s = (s or "").strip().strip("\"'“”‘’ 　")
    s = re.split(r"[，,。.！!？?；;\n]", s, 1)[0].strip()
    for _ in range(4):
        before = s
        for h in _KW_HEAD:
            if s.startswith(h) and len(s) > len(h):
                s = s[len(h):].strip()
                break
        # 单字动词只在后面跟空格 / 英文时削，避免误伤「看门狗」这类词
        if len(s) > 1 and s[0] in "搜找讲看聊" and (s[1] in " 　" or s[1].isascii()):
            s = s[1:].strip()
        for t in _KW_TAIL:
            if s.endswith(t) and len(s) > len(t) + 1:
                s = s[:-len(t)].strip()
                break
        s = s.strip("\"'“”‘’ 　、:：")
        if s == before:
            break
    return s


def _norm(name: str, args: dict, ctx: dict) -> tuple[dict | None, str]:
    """白名单过滤 + 类型纠正 + 目标补全。返回 (cmd, 错误说明)"""
    if name == "triple":
        base = {"bvid": ctx.get("bvid") or ""}
        return {"name": "triple", "args": base}, ""

    if name not in ARGS:
        return None, f"不认识的命令：{name}"

    out: dict = {}
    for k, v in (args or {}).items():
        if k not in ARGS[name]:
            continue
        if k in INT_ARGS:
            try:
                out[k] = int(v)
            except (TypeError, ValueError):
                continue
        elif k == "on":
            out[k] = bool(v) if isinstance(v, bool) else str(v).lower() not in ("false", "0", "no", "off")
        else:
            out[k] = str(v).strip()

    # 目标视频：没给就用当前打开的；给了就必须是合法 BV 号
    # （小模型常把「BV号」这种占位词直接塞进来，不挡住会一路崩到 httpx 头部编码）
    if name in ("comment", "danmaku", "like", "coin", "fav", "open_video"):
        if not out.get("bvid"):
            out["bvid"] = ctx.get("bvid") or ""
        b = out["bvid"]
        if b and (not _BV.fullmatch(b) or re.search(r"[xX]{3,}|0{4,}|1{5,}", b)):
            return None, f"「{b}」不是有效的 BV 号。给我一个 BV1 开头的 12 位号，或者说「搜 xxx」让我找。"

    # 发评论 / 弹幕 / 回复：内容不能空
    if name in ("comment", "danmaku", "reply"):
        msg = (out.get("message") or "").strip()
        if not msg:
            return None, ("评论内容" if name == "comment" else
                          "弹幕内容" if name == "danmaku" else "回复内容") + "还没给，说说写什么？"
        out["message"] = msg[:1000]

    # 回复目标：index -> msg_id
    if name == "reply":
        if not out.get("msg_id"):
            items = ctx.get("inbox") or []
            idx = int(out.pop("index", 1) or 1)
            if not items:
                return None, "收件箱还没拉到消息，先在后台「消息」里刷新一次。"
            if idx < 1 or idx > len(items):
                return None, f"收件箱只有 {len(items)} 条，第 {idx} 条不存在。"
            out["msg_id"] = str(items[idx - 1].get("msg_id") or "")
        if not out.get("msg_id"):
            return None, "没定位到要回复的消息。"
    else:
        out.pop("msg_id", None)

    if name == "coin":
        out["n"] = max(1, min(2, int(out.get("n") or 1)))
    if name == "search":
        out["keyword"] = _clean_kw(out.get("keyword") or "")
        if not out["keyword"]:
            return None, "搜什么？把关键词告诉我。"
    if name in ("open_video", "play") and not out.get("bvid") and name == "open_video":
        return None, "要打开哪个视频？给我 BV 号。"

    return {"name": name, "args": out}, ""


# ---------------- 预览文案 ----------------

def _preview(cmd: dict, ctx: dict) -> str:
    name, a = cmd["name"], cmd["args"]
    title = ctx.get("title") or ""
    who = f"《{title}》" if title else "当前视频"
    if name == "search":
        return f"搜索：{a.get('keyword')}"
    if name == "open_video":
        return f"打开视频：{a.get('bvid')}"
    if name == "comments":
        return f"拉取{who}的评论"
    if name == "comment":
        return f"在{who}下发评论：{a.get('message')}"
    if name == "reply":
        idx = 0
        for i, m in enumerate(ctx.get("inbox") or [], 1):
            if str(m.get("msg_id")) == str(a.get("msg_id")):
                idx = i
                break
        from_ = ""
        if idx:
            m = ctx["inbox"][idx - 1]
            from_ = f"，来自 {m.get('user') or '对方'}"
        head = f"第 {idx} 条消息{from_}" if idx else "指定消息"
        return f"回复{head}：{a.get('message')}"
    if name == "inbox":
        return "拉取未回复消息"
    if name == "like":
        return ("取消" if a.get("on") is False else "") + f"{who}的赞"
    if name == "coin":
        return f"给{who}投 {a.get('n', 1)} 个币"
    if name == "fav":
        return f"收藏{who}"
    if name == "danmaku":
        return f"在{who}发弹幕：{a.get('message')}"
    if name == "roam":
        return ("开启" if a.get("on") else "关闭") + "自动巡览"
    if name == "state":
        return "查看当前状态"
    return LABEL.get(name, name)


# ---------------- 对外入口 ----------------

def parse(text: str, ctx: dict) -> dict:
    """只走规则。命中返回命令，未命中返回 {"kind": "none"}"""
    ctx = ctx or {}
    hit = _rule(text, ctx)
    if not hit:
        return {"kind": "none"}

    if hit["name"] == "_chat":
        return {"kind": "chat", "say": hit["args"]["say"], "source": "rule"}

    if hit["name"] == "triple":
        cmds = [{"name": "like", "args": {"bvid": ctx.get("bvid") or "", "on": True}},
                {"name": "coin", "args": {"bvid": ctx.get("bvid") or "", "n": 2}},
                {"name": "fav", "args": {"bvid": ctx.get("bvid") or ""}}]
        return {"kind": "cmd", "cmds": cmds, "write": True, "source": "rule",
                "preview": "一键三连：点赞 + 投 2 币 + 收藏",
                "labels": [LABEL[c["name"]] for c in cmds]}

    cmd, err = _norm(hit["name"], hit.get("args") or {}, ctx)
    if err:
        return {"kind": "ask", "say": err}
    if not cmd:
        return {"kind": "none"}
    return {"kind": "cmd", "cmds": [cmd], "write": cmd["name"] in WRITE, "source": "rule",
            "preview": _preview(cmd, ctx), "labels": [LABEL.get(cmd["name"], cmd["name"])]}


def finalize(result: dict, ctx: dict) -> dict:
    """把解析结果补全成前端可直接用的形状（含 write / preview / labels）"""
    if result.get("kind") != "cmd":
        return result
    cmds, previews = [], []
    for c in result.get("cmds") or []:
        cmd, err = _norm(c["name"], c.get("args") or {}, ctx)
        if err:
            return {"kind": "ask", "say": err}
        cmds.append(cmd)
        previews.append(_preview(cmd, ctx))
    result["cmds"] = cmds
    result["write"] = any(c["name"] in WRITE for c in cmds)
    result["preview"] = "；".join(previews)
    result["labels"] = [LABEL.get(c["name"], c["name"]) for c in cmds]
    return result


def _tidy_say(s: str) -> str:
    """小模型爱把命令 JSON 塞进闲聊回复，直接展示会很怪，兜个自然话术"""
    s = (s or "").strip()
    if not s:
        return "这句我没听明白。要搜视频就说「搜 xxx」，要评论就说「评论一句 xxx」。"
    if s.startswith("{") and '"name"' in s:
        return "这句我没对应上具体操作。要搜视频就说「搜 xxx」，要评论就说「评论一句 xxx」。"
    return s[:400]


async def aparse(llm, text: str, ctx: dict) -> dict:
    """规则优先；规则没命中才问模型。llm 为 None / 调用失败时退回闲聊提示"""
    r = parse(text, ctx)
    if r.get("kind") != "none":
        return r

    if llm is None:
        return {"kind": "chat", "say": "这句我没听明白。要搜视频就说「搜 xxx」，要评论就说「评论 xxx」。",
                "source": "none"}

    import asyncio

    try:
        raw = await asyncio.to_thread(
            llm.chat,
            [{"role": "system", "content": SYSTEM},
             {"role": "user", "content": _clean(text)}],
            160, 0.1)
    except Exception as e:                       # noqa: BLE001  模型挂了不能挡住聊天
        return {"kind": "chat", "source": "none",
                "say": f"本地模型没回话（{str(e)[:80]}），这句我也没匹配到命令。"}

    from_model = _from_model(raw)
    if not from_model:
        return {"kind": "chat", "say": _tidy_say(raw), "source": "llm"}

    if from_model["kind"] == "chat":
        from_model["say"] = _tidy_say(from_model.get("say"))
        return from_model

    # 模型给的命令：走同一条校验链路，不合法就当闲聊
    got = finalize(from_model, ctx)
    if got.get("kind") != "cmd":
        return {"kind": "chat", "source": "llm",
                "say": _tidy_say(got.get("say"))}
    return got


def summarize(name: str, out: str, limit: int = 1200) -> str:
    """命令回显 -> 适合聊天气泡的短文"""
    text = (out or "").strip()
    if not text:
        return "执行完了，没有输出。"
    if text.startswith(("ERR", "BAD")):
        first = text.split("\n", 1)[0]
        if "ERR_NO_TARGET" in first:
            return "还没打开视频。先说「搜 xxx」挑一个，或者给我 BV 号打开，我再操作。"
        if "ERR_ApiError" in first or "code=-404" in first:
            return "B 站那边没找到这个视频，BV 号可能抄错了。"
        if "ERR_UnicodeEncodeError" in first:
            return "这条指令里的目标不对（大概率 BV 号是编的），换个说法试试。"
        return "执行失败：" + first
    if len(text) > limit:
        text = text[:limit] + "\n……（完整输出见后台）"
    return text
