"""回复文案生成：本地 0.5B 多点采样 + 规则打分挑选（无模板池）

为什么这么写（都是实测踩出来的）：
Qwen2.5-0.5B 单次贪心解码会直接复述对方原话——「你是机器人吗」→「你是机器人吗」，
把要求写长、加 few-shot 都压不住，这是模型容量问题，不是 prompt 不够狠。

实测有效的手段有三个，缺一不可：
1. 助手回合预置种子「回复：」：把任务从「生成回答」变成「续写一句话」，
   模型不再抄用户那句，逐字复述率基本归零；
2. 同一输入用不同温度并发采样 3 条：候选之间差异明显，留出挑选空间；
3. 规则打分挑选：复述、套话、格式残留、过长、反问语气一律否决，
   在剩下的候选里挑最口语、最贴对方话题的一条。

模板池已移除（用户明确不要）。全部候选不合格时返回空串，
由上层跳过、下一轮换个随机种子再试——宁可不回，也不发复读机文案。
"""

import re
import threading

# 助手回合种子：任务变成「续写」，这是压住逐字复述的关键
SEED = "回复："
# 多点采样的温度分布：低温柔顺、高温发散，避免几条一模一样
TEMPS = (0.75, 0.95, 1.15, 1.35)
# 第一轮都不够好时再摇一轮，换一组温度（小模型方差大，多抽几次命中率明显提升）
TEMPS2 = (0.6, 0.85, 1.05, 1.25)
# 招呼类输入专用第三轮
TEMPS3 = (0.5, 0.7, 0.9, 1.1)
# 及格线：低于这个分数的候选一律不发，宁可这轮不回
MIN_SCORE = 55.0
# 招呼类输入的门槛，抬高是因为这类最容易答非所问
SHORT_BAR = 88.0
# 够好就不用再摇第二轮，省时间
GOOD_ENOUGH = 92.0
# 每条候选的最大 token（20 字中文大约 30-40 token，留点余量）
MAX_TOKENS = 48

# llm.json 没配 prompt 时的兜底 system
SYSTEM_FALLBACK = (
    "你是B站UP主本人，正在回粉丝评论。回一句口语，20字内，不复述对方的话，"
    "不提AI、不提脚本、不说抱歉、不留联系方式、不编造平台名。"
    "对方只是打招呼就自然应一声。"
)

# few-shot 放助手回合里，短、少，0.5B 上下文吃不下长篇示范
SHOTS = [
    {"role": "user", "content": "他说：「你是真人吗」\n回复："},
    {"role": "assistant", "content": "是真人，工具只是帮我盯消息\n"},
    {"role": "user", "content": "他说：「这个怎么弄的，我卡在登录」\n回复："},
    {"role": "assistant", "content": "多半是登录态过期，重新登一次\n"},
    {"role": "user", "content": "他说：「前排」\n回复："},
    {"role": "assistant", "content": "来得早，自己倒水\n"},
]

# 明显不该发的套话 / 自称，模型爱吐这些；后两条是这个号早期刷屏过的固定句
_BAD = ("AI自动回复", "作为一个", "我无法", "抱歉", "以下是", "希望对你", "有什么可以帮",
        "请问您", "请问", "语言模型", "我是AI", "机器人助手", "自动回复测试", "感谢您的",
        "没动一下鼠标", "全靠命令", "靠命令", "命令", "脚本", "人工智能", "我是机器人",
        "助手",
        # 隐私外泄：实测模型会自己编一个微信号发出去，这条必须掐死
        "微信", "加我", "私信", "二维码", "http", "www", "电话", "号码", "联系我", "关注我",
        # 把 prompt 里的字数要求当正文吐出来，也见过
        "20字", "字数", "回答你的问题", "你的问题", "不限字数", "回答我", "20秒")
# 这几个词大小写敏感：小写 ai 会误伤拼音和英文单词，只掐大写的
_BAD_CS = ("AI", "GPT", "LLM", "QQ")
# 编造平台/厂商名（问"用的什么模型"时模型会瞎报百度阿里），事实风险高，一律否决
_BAD_VENDOR = ("百度", "阿里", "通义", "文心", "DeepSeek", "微软", "谷歌", "豆包", "Kimi",
               "Claude", "Meta", "LLaMA", "ChatGPT", "OpenAI")
# 时间戳、URL 残片、连续标点、算式符号、长数字串（手机号/QQ），一律否决
_JUNK = re.compile(r"(\d{4}年|\d{1,2}:\d{2}|\d{4}-\d{2}|\d{5,}|[+|=/\\<>]|。{2,}|\.{3,}|！{2,}|\?{2,})")



# 书面腔，命中扣分不否决（有时确实会用到）
_FORMAL = ("因此", "综上", "此外", "首先", "其次", "建议您", "值得一提", "总体而言", "您好")

# 口语标记，命中加分
_SPOKEN = ("吧", "啊", "哈", "嘛", "就", "挺", "其实", "反正", "确实", "呢", "呗", "了", "哎")

# 输出里出现这些，说明模型把 prompt 或模板复读出来了，在它之前截断
_CUT = ("对方评论", "他说：", "回复：", "视频：", "我之前的评论", "（", "(", "【", "抱歉")

_EMOJI = re.compile(r"\[[^\]]{1,10}]")
# 回复里出现 4 个以上连续英文基本是乱入（B站/UP 这种单字母双字母不算）
_ENGLISH = re.compile(r"[a-zA-Z]{4,}")
# 招呼类输入（"出来""前排""在吗"）的合格开头：必须是应答，不能自顾自说事
_ACK_HEAD = ("在", "来", "嗯", "哈", "嗨", "好", "早", "哦", "噢", "收到", "哟", "诶", "欸",
             "到", "是", "有", "行", "可以")

# 本轮（进程内）已用过的句子，防同一批回复刷同一句
_USED: list = []


def _clean(text: str) -> str:
    """清洗模型输出：去种子前缀、截断格式残留、去表情/引号"""
    t = str(text or "").strip().split("\n")[0]
    t = re.sub(r"^(回|回复|答|我回)[：:]\s*", "", t)
    for c in _CUT:
        i = t.find(c)
        if i > 0:
            t = t[:i]
    t = _EMOJI.sub("", t)
    t = t.strip().strip('"“”\'‘’《》[] ')
    return " ".join(t.split())


def _lcs(a: str, b: str) -> int:
    """最长公共子串长度（中文按字符），判「是不是在复述对方」"""
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    best = 0
    for ch in a:
        cur = [0] * (len(b) + 1)
        for j, ch2 in enumerate(b, 1):
            if ch == ch2:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


def _bigrams(s: str) -> set:
    return {s[i:i + 2] for i in range(len(s) - 1)}


def score(text: str, his: str, recent=None) -> tuple[float, str]:
    """给候选打分；负分表示否决，正分越高越可用"""
    t = _clean(text)
    if not t:
        return -99, "空"
    if len(t) < 2:
        return -99, "太短"
    if len(t) > 45:
        return -99, "太长"
    low = t.lower()
    for w in _BAD:
        if w.lower() in low:
            return -99, f"套话「{w}」"
    for w in _BAD_CS:
        if w in t:
            return -99, f"套话「{w}」"
    for w in _BAD_VENDOR:
        if w in t:
            return -99, f"编造平台名「{w}」"
    if _JUNK.search(t):
        return -99, "夹了时间戳/符号/号码"
    if _ENGLISH.search(t):
        return -99, "夹英文"
    # 对方就一句招呼，回的却自顾自说事（"出来"→"你出去吧，别来了"），这种最伤
    if len(his) <= 4 and not (t.startswith(_ACK_HEAD) and len(t) <= 10):
        return -99, "招呼类没应声"
    # 冒号/逗号收尾说明句子没写完（"教程在这里："）
    if t.endswith(("：", ":", "，", ",", "、", "；", ";")):
        return -99, "句子没写完"
    # 引号书名号括号必须成对，实测模型会吐 "《月光》，视频标题叫《xxx" 这种断头句
    for a, b in (("《", "》"), ("“", "”"), ("（", "）"), ("(", ")"), ("「", "」"), ("【", "】")):
        if t.count(a) != t.count(b):
            return -99, "标点没配对"
    # 被问"用的什么模型/技术"时它一定会现编一个（"马尔科夫马龙模型"），
    # 事实风险太高，宁可不回也不瞎报
    if any(w in his for w in ("什么模型", "什么框架", "什么技术", "用的什么", "用的啥")) and \
            any(w in t for w in ("模型", "框架", "技术", "算法")):
        return -99, "会瞎编技术名"
    # 问具体名字/曲子/链接这类必须准确答的，它只会瞎给（"是“老马”这个名字"）
    if any(w in his.lower() for w in ("叫什么", "什么名字", "是哪首", "bgm", "链接", "下载地址")) and \
            any(w in t for w in ("叫", "是《", "是“", "是\"")):
        return -99, "会瞎编具体信息"
    # 被问"你是机器人吗"还答"是的"，等于自曝，必须掐
    if ("机器人" in his or "AI" in his or "ai" in his) and \
            t.startswith(("是的", "对的", "没错", "对，", "嗯，是")) and "不" not in t[:5]:
        return -99, "顺着承认是机器人"
    # 同上，换成"我是机器大当家"这种绕法：提了机器又没否认，一样算自曝
    if ("机器人" in his or "机器" in his) and "机器" in t and not any(
            w in t for w in ("不", "非", "没有", "真人")):
        return -99, "承认是机器"
    # 复述判定看比例：短句共享几个字很正常，整句照搬才算复述
    same = _lcs(t, his)
    if same >= 6 and (len(t) <= same + 2 or same / max(1, len(t)) >= 0.5) or same >= 9:
        return -99, "复述对方原话"
    # 换个说法照抄也算复述（"这个项目开源吗，想学习一下"→"这是项目开源吗？想学习一下"），
    # 直接查候选里是否嵌了对方连续 5 个字
    if len(his) >= 8 and any(his[i:i + 5] in t for i in range(len(his) - 4)):
        return -99, "复述对方原话"
    for r in (recent or []):
        if r and (r == t or _lcs(t, r) >= 7):
            return -99, "和最近发过的重复"

    sc = 100.0 - abs(len(t) - 14) * 1.5
    sc += sum(2.0 for w in _SPOKEN if w in t)
    if _ENGLISH.search(t):
        sc -= 20
    sc -= sum(12.0 for w in _FORMAL if w in t)
    # 对方是问句，回的却是问句 → 像反问，扣分
    if his.rstrip().endswith(("?", "？", "吗", "呢")) and t.endswith(("?", "？")):
        sc -= 15
    # 对方只是一句招呼，回得太长基本是跑题
    if len(his) <= 4 and len(t) > 12:
        sc -= 30
    # 和对方共享 1-2 个双字词，说明还贴着话题，给点分
    if len(his) > 4:
        overlap = len(_bigrams(t) & _bigrams(his))
        sc += 3 * overlap
        # 一个词都不沾，基本是自言自语（"哈死我了"→"太有希望了！"）
        if overlap == 0:
            sc -= 25
    # 开头跟已经发过的某句撞了，说明在复读同一个口头禅，压一压
    head = t[:3]
    if head and any(r[:3] == head for r in (recent or []) if r):
        sc -= 12
    return sc, "ok"


def _messages(llm, item: dict) -> list[dict]:
    """拼会话：system 取 llm.json 里配置的 prompt，没有就用兜底"""
    prompt = SYSTEM_FALLBACK
    try:
        prompt = str(llm.raw().get("prompt") or "").strip() or SYSTEM_FALLBACK
    except Exception:                          # noqa: BLE001
        pass
    his = str(item.get("his_msg") or "").strip()[:60]
    my = str(item.get("my_msg") or "").strip()[:40]
    title = str(item.get("title") or "").strip()[:24]
    u = ""
    if title:
        u += f"视频：{title}\n"
    u += f"他说：「{his}」"
    if my:
        u += f"\n我之前的评论：「{my}」"
    u += "\n" + SEED
    return ([{"role": "system", "content": prompt}] + SHOTS
            + [{"role": "user", "content": u}, {"role": "assistant", "content": SEED}])


def _sample(llm, messages: list[dict], temps=TEMPS) -> list[str]:
    """不同温度并发采样，返回清洗后的候选（并发是为了别让一条拖到十几秒）"""
    out: dict = {}

    def work(tp: float) -> None:
        try:
            txt = llm.chat(messages, max_tokens=MAX_TOKENS, temperature=tp,
                           extra={"frequency_penalty": 0.6})
            out[tp] = _clean(txt)
        except Exception:                      # noqa: BLE001  单条失败不影响整体
            out[tp] = ""

    ths = [threading.Thread(target=work, args=(float(tp),), daemon=True)
           for tp in temps]
    for t in ths:
        t.start()
    for t in ths:
        t.join(timeout=60)
    return [out[float(tp)] for tp in temps if out.get(float(tp))]


def _remember(text: str) -> None:
    _USED.append(text)
    del _USED[:-20]


def make(item: dict, llm=None, recent=None) -> tuple[str, str]:
    """生成一条回复。返回 (文案, 来源)；来源 = model / fail

    0.5B 才是主力，这里只做两件事：多点采样 + 挑最好的一条。
    全挑不出来就返回空串，交给 autoreply 跳过，不发垃圾。
    """
    his = str(item.get("his_msg") or "").strip()
    if not his or llm is None:
        return "", "fail"

    recent = list(recent or []) + _USED
    messages = _messages(llm, item)
    # 一句招呼（"出来""前排""在吗"）信息量太少，0.5B 特别容易跑偏，
    # 这类要求更高的分数、也允许摇更多轮，摇不出来就不回
    short = len(his) <= 4
    bar = SHORT_BAR if short else MIN_SCORE
    rounds = (TEMPS, TEMPS2, TEMPS3) if short else (TEMPS, TEMPS2)

    pool: list = []
    best, best_sc = "", bar
    for temps in rounds:
        pool += _sample(llm, messages, temps=temps)
        for c in pool:
            sc, _why = score(c, his, recent)
            if sc > best_sc:
                best, best_sc = c, sc
        if best_sc >= GOOD_ENOUGH or (best and best_sc >= bar + 20):
            break
    if not best:
        return "", "fail"
    _remember(best)
    return best, "model"


def debug(item: dict, llm, recent=None) -> dict:
    """调 prompt 时用：把候选和打分全摊开，线上不走这条路"""
    his = str(item.get("his_msg") or "").strip()
    recent = list(recent or []) + _USED
    cands = _sample(llm, _messages(llm, item))
    rows = [{"text": c, "score": score(c, his, recent)[0],
             "why": score(c, his, recent)[1]} for c in cands]
    rows.sort(key=lambda r: -r["score"])
    return {"cands": rows, "pick": rows[0] if rows and rows[0]["score"] > 0 else None}
