"""模型层：拼 system prompt、定义工具、跑工具调用循环。

两个设计要点，都来自设计文档：

  5.3 always-on 注入 —— 有效的偏好一共几十条几百 token，全塞进 system prompt
      比任何检索都准，而且可解释。所以这里不做检索。

  5.7 收窄接口 —— 给模型的是 log_meal(name, note) 这种参数化函数，
      不是 execute_sql()。它写不出你意料之外的查询，你也就不用猜数据
      是怎么变成那样的。

这一层是可换的：以后换成语音、换成别家模型，改的只是 client 和 model，
工具定义和记忆注入原样搬走（设计文档 2.6）。
"""

import json
from datetime import datetime

from openai import OpenAI

import amap
import config
import db
import places
import ui

# 一次对话里最多跑几轮工具调用。防的不是逻辑错误，是"模型和工具互相刷屏
# 停不下来"——设计文档 12.1：熔断的意义在于让"忘了关"不再有灾难后果。
MAX_ROUNDS = 5

_client_cache: OpenAI | None = None


def _client() -> OpenAI:
    """用到时才建客户端。

    如果在 import 时就建，配置没填好会直接抛一个 SDK 的原始报错，
    config.check() 那句"你还缺哪几项"根本轮不到显示。
    """
    global _client_cache
    if _client_cache is None:
        config.check()
        _client_cache = OpenAI(api_key=config.API_KEY, base_url=config.BASE_URL)
    return _client_cache


def _fmt_log(r) -> str:
    return (f"id={r['id']}  {db.to_local(r['ts'])}  {r['kind']}  {r['name']}"
            + (f" @{r['place']}" if r["place"] else "")
            + (f"（{r['note']}）" if r["note"] else ""))


# ---------- 地点：中心点要跟着对话走 ----------

# 本次会话最后用过的中心点，(坐标, 用户说的那个名字)。
#
# 为什么要记：用户说完"新街口"，下一轮问"那边有火锅吗"，模型很可能不填
# near，于是悄悄退回家附近 —— 而用户发现不了，因为返回的店名他本来就
# 不认识。靠 system prompt 写一句"请沿用最近提到的地点"是不够的，模型
# 可能听可能不听，事后还查不出为什么没听（设计文档 6.1）。
#
# 所以这里用确定性代码兜住，并且**每次都把用的是哪儿回显出去**。
_last_center: tuple[str, str] | None = None


def _resolve_center(near: str | None) -> tuple[str, str]:
    """定这次查询的中心点，返回 (坐标, 给人看的名字)。

    优先级：这次说了 > 上次用过的 > .env 里的家。
    """
    global _last_center
    if near:
        _last_center = places.resolve(near, quiet=True)
    elif _last_center is None:
        if not config.HOME:
            raise ValueError("没有默认地点。要么在问题里说个地方，"
                             "要么在 .env 里加一行 CADENCE_HOME=经度,纬度")
        _last_center = (config.HOME, "家")
    return _last_center


def reset_center() -> None:
    """新会话开始时清掉。chat.py 每次启动调一次。"""
    global _last_center
    _last_center = None


# ---------- 时间：模型没有时钟，得每轮告诉它 ----------

def _time_hint() -> str:
    """当前本地时间 + 星期，如 2026-08-11 16:20 周二。

    带星期是因为用户会说"上周三"，模型光有日期推不出来。
    刻意不给 UTC —— 模型侧一律说本地时间，换算交给 db.from_local()。
    """
    dt = datetime.now().astimezone()
    return f"{dt.strftime(db.LOCAL_FMT)} 周{'一二三四五六日'[dt.weekday()]}"


def user_message(text: str) -> dict:
    """把用户的话包成消息，前面缀上当前时间。

    模型没有时钟。不给它基准，"昨天下午"就填不出来，find_logs 返回的
    08-11 是不是今天它也判断不了。

    为什么缀在 user 消息上而不是写进 system prompt：prompt 缓存按前缀
    匹配 —— 从头连续相同的那段才能复用，一个 token 对不上，从那里往后
    全部作废。system 在列表最前面，时间戳每轮变，整段对话历史就全部
    失配，越聊越贵。变化的东西必须待在列表末尾。

    代价是 messages 里这条不再是原话，但 chat.py 记 events 用的是原始
    字符串、不走 messages，留痕不受影响。
    """
    return {"role": "user", "content": f"[{_time_hint()}] {text}"}


def _to_utc(args: dict, *keys: str) -> str | None:
    """就地把 args 里的本地时间字段换成 UTC。格式不对就返回一句给模型看的话。

    转换失败不抛异常 —— 这是模型填错了参数，不是程序坏了。给它一句
    人话让它重填，比让异常冒泡打断整场对话强。
    """
    for k in keys:
        if args.get(k):
            try:
                args[k] = db.from_local(args[k])
            except ValueError:
                return (f"{k}=「{args[k]}」格式不对。要写成 2026-08-10 15:00 这样的"
                        f"本地时间，不能用「昨天下午」这种词——你得自己算出具体日期。"
                        f"重新填一次。")
    return None


# ---------- 给模型的工具（全是 db.py 里的窄接口） ----------

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "log_meal",
            "description": (
                "记录用户吃了什么。用户提到自己吃了/在吃某样东西时调用。"
                "一句话里提到多样食物，就每样调用一次，不要合并成一条。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    # 这里刻意不给具体食物做示例：示例词会在上下文里被反复
                    # 强化，模型后面容易把它当默认值填进来。
                    "name": {
                        "type": "string",
                        "description": (
                            "食物名称。只能取用户最新这一句话里明确提到的食物，"
                            "绝不要沿用上一轮的名字。这一句里没有出现食物名称就不要调用本工具。"
                        ),
                    },
                    "note": {"type": "string", "description": "用户对它的评价或补充。没有就不填"},
                    "place": {
                        "type": "string",
                        "description": (
                            "在哪家店吃的。用户提到了店名就填，在家做饭/没提就不填。"
                            "只填店名本身，不要加「那家」「附近的」这类修饰词。"
                        ),
                    },
                    "ts": {
                        "type": "string",
                        "description": (
                            "这顿饭发生的本地时间，形如 2026-08-10 15:00。"
                            "用户提到了时间（昨天下午、今天早上、周三中午）就必须填；"
                            "完全没提时间就不填，默认记成现在。"
                            "只说了时段没说几点，就用这套锚点："
                            "上午 09:00 / 中午 12:00 / 下午 15:00 / 晚上 19:00。"
                        ),
                    },
                },
                # ts 刻意不设 required：设了就等于逼模型给每顿饭编一个时间。
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_logs",
            "description": (
                "查记录。回答'我最近吃了啥''推荐吃什么'时先调这个。"
                "要更正某条记录之前也必须先调它拿到 id。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "description": "记录类型，饮食填 meal。不填就查全部"},
                    "name": {"type": "string", "description": "按名称模糊匹配。不填就不按名称筛"},
                    "days": {"type": "integer", "description": "往前看几天，默认 30。问'最近'用这个"},
                    "since": {
                        "type": "string",
                        "description": (
                            "区间起点，本地时间，形如 2026-08-10 12:00。"
                            "用户说了具体时段（今天中午、昨天下午、周三晚上）时用 since+until，"
                            "别再给 days。时段展开成这些区间："
                            "上午 06:00-12:00 / 中午 11:00-14:00 / 下午 12:00-18:00 / 晚上 17:00-23:00。"
                        ),
                    },
                    "until": {"type": "string", "description": "区间终点，本地时间，格式同 since"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "correct_log",
            "description": (
                "更正一条已有记录的名称或备注。**必须先用 find_logs 查到 id**，"
                "不要凭印象猜 id。执行前系统会向用户当面确认，用户拒绝就作罢。"
                "没有删除记录的工具，用户要删就如实告诉他你做不到。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer", "description": "要改的记录 id，来自 find_logs 的结果"},
                    "name": {"type": "string", "description": "改成的新名称。不改就不填"},
                    "note": {"type": "string", "description": "改成的新备注。不改就不填"},
                    "ts": {
                        "type": "string",
                        "description": "改成的新时间，本地格式如 2026-08-10 19:00。不改就不填",
                    },
                    "place": {"type": "string", "description": "改成的新店名。不改就不填"},
                },
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_place_types",
            "description": (
                "看某个地方实际有哪些餐饮分类。用户没说想吃什么（「今天吃什么」「随便推荐个」）"
                "时先调这个，拿到方向再去 find_places。"
                "find_places 说关键词不对时也调它。"
                "⚠️ 它要拉全量，慢（几秒），别在已经知道要查什么的时候调。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "near": {
                        "type": "string",
                        "description": (
                            "地点名，如 新街口。用户这轮提到了新地方才填；"
                            "没提就不填，系统会自动沿用上一次用过的地点。"
                        ),
                    },
                    "radius": {"type": "integer", "description": "半径米数，默认 3000"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_places",
            "description": (
                "查附近有哪些店，结果里会标出用户去过哪几家、当时说了什么。"
                "推荐吃饭的地方就用它。\n"
                "⚠️ keyword 必须是**真实存在的分类名或店名**。高德那边是文本匹配，"
                "「辣的」「清淡」「好吃的」这种描述词查出来的全是蒙的。"
                "用户说「想吃辣的」，你要自己翻译成具体菜系（四川菜,湖南菜,云贵菜,火锅）再查。"
                "不确定这一带有哪些菜系就先调 list_place_types。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": (
                            "分类名或店名，如 四川菜、日本料理、火锅、随园餐厅。"
                            "逗号分隔可以一次给多个，如 四川菜,湖南菜,火锅。"
                        ),
                    },
                    "near": {
                        "type": "string",
                        "description": (
                            "地点名，如 新街口。用户这轮提到了新地方才填；"
                            "没提就不填，系统会自动沿用上一次用过的地点。"
                        ),
                    },
                    "radius": {"type": "integer", "description": "半径米数，默认 3000"},
                    "max_cost": {"type": "number", "description": "人均不超过多少元。用户提了预算才填"},
                    "min_rating": {"type": "number", "description": "评分不低于多少。用户明确要求才填"},
                },
                "required": ["keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remember",
            "description": (
                "记住一条长期偏好或约束。只在用户明确表达了稳定的偏好、忌口、"
                "长期目标时调用。临时话题、一次性的评价、疑问句都不要记。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "enum": ["preference", "constraint", "fact"]},
                    "key": {
                        "type": "string",
                        "description": "归一化的英文 key，如 food.dislike.cilantro、diet.goal。不要用中文",
                    },
                    "value": {"type": "string", "description": "人话，如 不吃香菜"},
                },
                "required": ["category", "key", "value"],
            },
        },
    },
]


def _run_tool(name: str, args: dict) -> str:
    """执行工具，返回给模型看的文本结果。"""
    if name == "log_meal":
        if err := _to_utc(args, "ts"):
            return err
        row_id = db.log_meal(args["name"], note=args.get("note"), ts=args.get("ts"),
                             place=args.get("place"))
        # 回显必须带时间。模型能自己填 ts 了，它就会把"昨天下午"记成今天 ——
        # 不当场打出来，这种错你要等到下次查记录才发现（设计文档 5.2）。
        return f"已记录：{_fmt_log(db.get_log(row_id))}"

    if name == "find_logs":
        if err := _to_utc(args, "since", "until"):
            return err
        rows = db.find_logs(kind=args.get("kind"), name=args.get("name"),
                            days=args.get("days", 30),
                            since=args.get("since"), until=args.get("until"))
        if not rows:
            return "没找到符合条件的记录。"
        return "\n".join(_fmt_log(r) for r in rows)

    if name == "correct_log":
        row_id = args["id"]
        old = db.get_log(row_id)
        if old is None:
            return f"没有 id={row_id} 这条记录，先用 find_logs 确认 id。"
        if err := _to_utc(args, "ts"):
            return err

        changes = []
        if args.get("name"):
            changes.append(f"名称改成「{args['name']}」")
        if args.get("note"):
            changes.append(f"备注改成「{args['note']}」")
        if args.get("ts"):
            # 确认框里说的是本地时间 —— 给人看的东西不该出现 UTC
            changes.append(f"时间改成「{db.to_local(args['ts'])}」")
        if args.get("place"):
            changes.append(f"地点改成「{args['place']}」")
        if not changes:
            return "没说要改什么，name / note / ts / place 至少给一个。"

        if not ui.confirm(f"要把这条{'，'.join(changes)}吗？", _fmt_log(old)):
            # 把拒绝如实告诉模型，让它知道这次没生效，别接着往下假设。
            return "用户拒绝了这次改动，记录没有变。"

        db.correct_log(row_id, name=args.get("name"), note=args.get("note"),
                       ts=args.get("ts"), place=args.get("place"))
        return f"已更正：{_fmt_log(db.get_log(row_id))}"

    if name == "remember":
        db.remember(args["category"], args["key"], args["value"], source="voice")
        return f"已记住：{args['value']}"

    if name in ("find_places", "list_place_types"):
        return _run_place_tool(name, args)

    return f"没有这个工具：{name}"


def _been_there(store: str) -> str:
    """这家店用户去过没有。空字符串表示没记录。

    这一行就是整个推荐功能的支点：高德能说出附近有什么，但"这家你去过两次、
    上次说排队久但值"只有 logs 有。设计文档 7.5 说的、赢得过美团的唯一原因
    就是这一条 —— 所以候选集里每一家都要过一遍这个函数。
    """
    rows = db.place_history(store)
    if not rows:
        return ""
    last = rows[0]
    out = f"你去过{len(rows)}次，最近 {db.to_local(last['ts'])}"
    if last["name"]:
        out += f"吃的{last['name']}"
    if last["note"]:
        out += f"，你说「{last['note']}」"
    return out


def _run_place_tool(name: str, args: dict) -> str:
    """find_places / list_place_types。两个都要先定中心点，所以放在一起。"""
    try:
        center, label = _resolve_center(args.get("near"))
    except (ValueError, SystemExit) as e:
        return f"定位不了这个地方：{e}。让用户说得具体一点。"

    radius = args.get("radius") or 3000

    try:
        if name == "list_place_types":
            got = amap.list_types(center, radius)
            listed = "｜".join(f"{t} {n}" for t, n in got["types"][:25])
            return (f"以「{label}」为中心 {radius} 米内共 {got['total']} 家餐饮，"
                    f"抽样 {got['sampled']} 家，分类分布：\n{listed}\n"
                    f"（家数是抽样的，只能看出有哪些菜系，别拿数字做比较。"
                    f"查具体的店请用上面这些分类名调 find_places。）")

        keyword = args["keyword"]
        got = amap.search_keywords(center, keyword, radius)
    except amap.AmapError as e:
        # 如实报错，不要退回一个空列表让模型以为"这一带没有"（文档 7.4）
        return f"高德这次没返回数据：{e}。告诉用户查询失败了，不要编。"
    except OSError as e:
        return f"网络请求失败：{e}。告诉用户查询失败了，不要编。"

    pois = got["pois"]
    if (mc := args.get("max_cost")) is not None:
        # 缺数据的一律保留：把没评分的店悄悄筛掉，等于让一家店因为高德
        # 没收录而消失，那是在替用户做他没要求的决定。
        pois = [p for p in pois if p["cost"] is None or p["cost"] <= mc]
    if (mr := args.get("min_rating")) is not None:
        pois = [p for p in pois if p["rating"] is None or p["rating"] >= mr]

    if not pois:
        return (f"以「{label}」为中心 {radius} 米内没有符合条件的「{keyword}」。"
                f"可以调 list_place_types 看看这一带实际有什么，再换个方向问用户。")

    if got["loose"]:
        return (f"「{keyword}」不是高德认得的分类名或店名 —— 它返回的 "
                f"{len(got['pois'])} 家里没有一家的分类或店名真含这个词，全是蒙的，"
                f"已经丢弃。请调 list_place_types 拿到这一带真实存在的分类名，"
                f"再用那些名字重查。不要拿这次的结果推荐给用户。")

    lines = [f"以「{label}」为中心 {radius} 米内的「{keyword}」，{len(pois)} 家："]
    for p in pois[:40]:      # 兜底上限，不是筛选（文档 12.1）
        bits = [f"{p['name']}", p["short_type"], f"{p['distance']}m"]
        if p["rating"]:
            bits.append(f"★{p['rating']}")
        if p["cost"]:
            bits.append(f"¥{p['cost']:.0f}")
        line = "  ".join(bits)
        if been := _been_there(p["name"]):
            line += f"  ← {been}"
        lines.append(line)
    return "\n".join(lines)


# ---------- system prompt ----------

def system_prompt() -> str:
    lines = [
        "你是 cadence，一个只服务于一个人的私人助手。说话简短、直接，不用客服腔，不要每句都确认。",
        "用户提到吃了什么就调 log_meal 记下来，不用问他要不要记。",
        "用户说之前哪条记错了，先用 find_logs 查出来复述给他，确认是哪一条之后再用 correct_log。",
        "推荐吃的地方之前，先用 find_logs 看他最近吃了什么，别推他刚吃过的。",
        "find_places 的结果里标了「你去过N次」的，一定要把去过几次、上次说了什么讲出来 ——"
        "这是他自己的记录，比评分有用得多。没标的就是没去过，别编成去过。",
        "用户没说想吃什么就先调 list_place_types，拿着这一带真实有的菜系给他两三个具体方向，"
        "别空口问「你想吃什么」。",
        "查不到的信息就说没查到，绝不编。编一家不存在的餐厅比不回答糟糕得多。",
        "每条用户消息开头方括号里的是系统加的当前时间，不是用户说的内容，别复述它。"
        "所有工具的时间参数都填本地时间，格式 2026-08-10 15:00。",
    ]
    # 注意：这里刻意不拼当前时间。system prompt 在 messages 最前面，
    # 它每轮一变，整段对话历史的 prompt 缓存就全部失配。时间走
    # user_message() 缀在末尾（见那个函数的注释）。

    memories = db.active_memories()
    if memories:
        lines.append("\n关于这个人，你已经知道的事：")
        lines += [f"- [{m['category']}] {m['value']}" for m in memories]
    return "\n".join(lines)


# ---------- 主循环 ----------

def chat(messages: list[dict], verbose: bool = True) -> str:
    """跑一轮对话（含工具调用），返回模型最终说的话。messages 会被就地更新。"""
    for _ in range(MAX_ROUNDS):
        resp = _client().chat.completions.create(
            model=config.MODEL, messages=messages, tools=TOOLS,
        )

        # 记账：每次调用的 token 数落库。设计文档 12.1 —— 没有账本就没有优化。
        if resp.usage:
            db.record_event("cli", "llm_call", {
                "model": config.MODEL,
                "prompt_tokens": resp.usage.prompt_tokens,
                "completion_tokens": resp.usage.completion_tokens,
                # 缓存命中数。时间戳缀在 user 消息末尾而不是写进 system prompt，
                # 图的就是这个数字 —— 不记下来，这个决定有没有兑现就查不出来。
                # getattr 兜底：不是每家兼容接口都返回这两个字段。
                "cache_hit": getattr(resp.usage, "prompt_cache_hit_tokens", None),
                "cache_miss": getattr(resp.usage, "prompt_cache_miss_tokens", None),
            })

        msg = resp.choices[0].message

        if not msg.tool_calls:
            return msg.content or ""

        # 手工构造这条 assistant 消息，而不是直接塞 SDK 对象：
        # 各家兼容接口对多余字段的容忍度不一样，显式写出来最稳。
        messages.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in msg.tool_calls
            ],
        })

        for tc in msg.tool_calls:
            args = json.loads(tc.function.arguments or "{}")
            # 先打印再执行，两个原因：确认框在 _run_tool 里弹，你得先看见
            # 它要干什么再被问；而且 _to_utc 会就地把本地时间换成 UTC，
            # 打印晚了就看不到模型原本填的是什么了 —— 这行的全部意义
            # 就是"模型到底填了什么"。
            if verbose:
                print(f"  · {tc.function.name}({', '.join(f'{k}={v}' for k, v in args.items())})")
            result = _run_tool(tc.function.name, args)
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})

    return "（工具调用绕了太多轮，我先停下了。）"
