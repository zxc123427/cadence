"""模型层：拼 system prompt、定义工具、跑工具调用循环。

两个设计要点，都来自设计文档：

  5.3 always-on 注入 —— 有效的偏好一共几十条几百 token，全塞进 system prompt
      比任何检索都准，而且可解释。所以这里不做检索。

  5.7 收窄接口 —— 给模型的是 log_event(category, kind, name) 这种参数化函数，
      不是 execute_sql()。它写不出你意料之外的查询，你也就不用猜数据
      是怎么变成那样的。

这一层是可换的：以后换成语音、换成别家模型，改的只是 client 和 model，
工具定义和记忆注入原样搬走（设计文档 2.6）。
"""

import json
import re
from datetime import datetime

from openai import OpenAI

import amap
import config
import db
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


def _status_tag(r) -> str:
    """给模型看的状态结论，不是两个字段让它自己拼。

    "这条过期了没"是**渲染时现算**的，不落库 —— 所以不存在"上次判定和
    下次判定之间的空窗"，那个问题只属于定时任务写库的方案。这里没有
    "判定时刻"，只有"显示时刻"，而显示时刻永远是现在。

    为什么不让模型自己比：那是拿今天的日期去减记录的日期，它会算错，
    而且错了不报错（跟 5.6 的时区推论同一个道理）。
    """
    if r["status"] != "planned":
        return ""
    if r["ts"] < db.now():
        return "  [⏰ 早就过了，还没确认做没做]"
    return "  [计划中]"


def _fmt_log(r) -> str:
    # db.when 而不是 db.to_local：没定死时间的事，ts 只是个区间起点，
    # 照原样打出来会显示成一个看起来很确定的钟点（见 db.when）
    return (f"id={r['id']}  {db.when(r)}  {r['category']}/{r['kind']}"
            f"  {r['name']}"
            + (f" @{r['place']}" if r["place"] else "")
            + (f"（{r['note']}）" if r["note"] else "")
            + _status_tag(r))


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
    地名解析走 amap.resolve()，跟 places.py 是同一个函数。
    """
    global _last_center
    if near:
        hit = amap.resolve(near)
        _last_center = (hit["location"], hit["label"])
    elif _last_center is None:
        if not config.HOME:
            raise amap.GeocodeError(
                "没有默认地点。要么在问题里说个地方，"
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
            "name": "log_event",
            "description": (
                "记录用户做了什么、正在做什么、或者将要做什么。"
                "他提到吃了东西、提到已经发生过的事、提到接下来要做的事时都调用。"
                "**没做成的事也要记**：约了人没去成、计划了但取消了、"
                "本来要做结果加班了——这些照样是发生过的事，而且正是以后回顾时最有用的。"
                "一句话里提到几件事，就每件调用一次，不要合并成一条。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "enum": list(db.CATEGORIES),
                        "description": (
                            "大分类，看有没有别人：和别人一起或约了别人就 appointment，"
                            "只关自己就 personal。"
                        ),
                    },
                    "kind": {
                        "type": "string",
                        "enum": list(db.KINDS),
                        "description": (
                            "小分类，看是什么事。吃东西一律 meal（约人吃饭也是 meal，"
                            "「和别人」那一维由 category 记）；考试上课复习 study；"
                            "跑步健身打球 exercise；出差旅行 travel；看病吃药体检 health；"
                            "买东西下单 purchase。"
                            "判不准就填 other —— 绝不要造这个清单以外的新值。"
                        ),
                    },
                    # 这里刻意不给具体食物做示例：示例词会在上下文里被反复
                    # 强化，模型后面容易把它当默认值填进来。
                    "name": {
                        "type": "string",
                        "description": (
                            "这件事本身，短短几个字。只能取用户最新这一句话里明确提到的内容，"
                            "绝不要沿用上一轮的名字。"
                            "只有当这一句纯粹是闲聊或提问、没提到任何具体的事时，才不要调用本工具——"
                            "事情没做成不算「没有事」，那也是一件事，要记。"
                            "kind=meal 时这里填食物名称，一句话提到多样食物就每样调用一次。"
                        ),
                    },
                    "note": {
                        "type": "string",
                        "description": (
                            "约了谁、具体要干什么、评价、补充，全写这里。"
                            "这是事后唯一能想起细节的地方，用户说了就别丢。没有就不填。"
                        ),
                    },
                    "place": {
                        "type": "string",
                        "description": (
                            "这件事发生在哪儿。店名、医院、健身房、城市都行 —— "
                            "用户说到什么粒度就填什么粒度，不要追问更细的，也不要自己补。"
                            "只填地点本身，不要加「那家」「附近的」这类修饰词。没提就不填。"
                        ),
                    },
                    "ts": {
                        "type": "string",
                        "description": (
                            "这件事发生（或将要发生）的本地时间，形如 2026-08-10 15:00。"
                            "**将来的事必须填**，不填会被当成已经发生了。"
                            "用户提到了时间（昨天下午、明天早上、周三中午）就必须填；"
                            "完全没提时间才不填，默认记成现在。"
                            "只说了时段没说几点，就用这套锚点："
                            "上午 09:00 / 中午 12:00 / 下午 15:00 / 晚上 19:00。"
                        ),
                    },
                    "ts_precision": {
                        "type": "string",
                        "enum": list(db.TS_PRECISIONS),
                        "description": (
                            "ts 这个时间有多准，不填就是 exact。"
                            "说定了几点几分是 exact；只说了哪一天（明天、周三）是 day；"
                            "只说了大概哪一周（这周末、下周）是 week；"
                            "只说了个月份或者「过段时间」是 month。"
                            "**不是 exact 的时候，ts 填那个范围的开头**："
                            "「这周末」填周六 00:00，「下个月」填一号 00:00。"
                            "填对这一项很重要：只有 exact 的事以后才会被追问「做了没」。"
                        ),
                    },
                },
                # ts 刻意不设 required：设了就等于逼模型给每顿饭编一个时间。
                "required": ["category", "kind", "name"],
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
                    "kind": {
                        "type": "string",
                        "enum": list(db.KINDS),
                        "description": "小分类。查饮食填 meal —— 推荐吃的地方之前必须带上它，不带会捞进考试和开会。不填就查全部",
                    },
                    "category": {
                        "type": "string",
                        "enum": list(db.CATEGORIES),
                        "description": "大分类。「我这周有什么约」用 appointment。不填就查全部",
                    },
                    "status": {
                        "type": "string",
                        "enum": list(db.STATUSES),
                        "description": "planned=还没做的，done=已经发生的。「我还有什么没干」用 planned。不填就查全部",
                    },
                    "place": {
                        "type": "string",
                        "description": "按地点找。「我去过某某餐厅吗」用这个，店名写全称或简称都行",
                    },
                    "keyword": {
                        "type": "string",
                        "description": (
                            "关键词，同时在名称、备注、地点三处里找。"
                            "**问到某个人（老王、张姐）时一定用它** —— 人名通常写在备注里。"
                            "是子串匹配不是语义匹配：搜「面条」找不到「牛肉面」，搜「面」可以。"
                            "查出来条数少得可疑就别下结论，去掉 keyword 用 kind 或 category 重查一次。"
                        ),
                    },
                    "days": {
                        "type": "integer",
                        "description": (
                            "往前看几天。**通常不用填** —— 给了 keyword 或 place 时"
                            "系统自动搜全部历史，没给时自动看最近 30 天。"
                            "只有用户明确说了「最近三天」这种范围才填。"
                            "注意它只是下界，将来的事一直都在结果里。"
                        ),
                    },
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
                "更正一条已有记录。**必须先用 find_logs 查到 id**，不要凭印象猜 id。"
                "用户说某件计划中的事做完了、取消了，也用它把 status 改掉。"
                "执行前系统会向用户当面确认，用户拒绝就作罢。"
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
                        "description": (
                            "改成的新时间，本地格式如 2026-08-10 19:00。不改就不填。"
                            "注意改时间不会自动改状态 —— 如果那件事其实已经做了，"
                            "要把 status 一起填成 done。"
                        ),
                    },
                    "place": {"type": "string", "description": "改成的新地点。不改就不填"},
                    "category": {
                        "type": "string",
                        "enum": list(db.CATEGORIES),
                        "description": "改成的新大分类。不改就不填",
                    },
                    "status": {
                        "type": "string",
                        "enum": list(db.STATUSES),
                        "description": "改成的新状态。用户说「那个我做了」就填 done。不改就不填",
                    },
                    "ts_precision": {
                        "type": "string",
                        "enum": list(db.TS_PRECISIONS),
                        "description": (
                            "改成的新时间精度。用户说「那个还没定死时间」就往回改成"
                            " day/week/month；说「定在周五三点了」就改成 exact"
                            "（这时 ts 也要一起填）。不改就不填"
                        ),
                    },
                },
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_note",
            "description": (
                "给一条已有记录**补**一句备注，接在原来那句后面，不会覆盖。"
                "用户回顾一件事、给评价、补细节时用它（「那场球人挺多」「那家店太吵了」）。"
                "⚠️ 跟 correct_log 分工很清楚："
                "用户说记的**不对**（记错时间、记错名字）才用 correct_log；"
                "用户只是在**说这件事怎么样**，一律用 add_note —— "
                "原来那句备注没有错，不该被评价洗掉。"
                "如果同时还要改状态（「那个做了」），就再调一次 correct_log 改 status。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer", "description": "记录 id，来自 find_logs 或开头那段总结"},
                    "text": {"type": "string", "description": "要补上的这句话，用用户自己的说法"},
                },
                "required": ["id", "text"],
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


def _run_tool(name: str, args: dict, seen: dict[str, int] | None = None) -> str:
    """执行工具。seen 是**回合作用域**的去重表，见 chat() 里的注释。"""
    seen = seen if seen is not None else {}

    if name == "log_event":
        if err := _to_utc(args, "ts"):
            return err
        # 回合内去重：这一轮刚查到过同名的记录，就别再记一条。
        # 实测两次同一个模式 —— 模型 find_logs 看见了 id=38「看车展」，
        # 下一圈还是 log_event 又写了一条。它越常被问起的事，越容易记重，
        # 而重复记录会污染后面所有的统计（"我跟老王出去过几次"会多算）。
        if (dup := seen.get((args.get("name") or "").strip())) is not None:
            return (f"你这一轮刚查到过 id={dup}「{args['name']}」，别再记一条重复的。"
                    f"要改它就用 correct_log；确实是另一件同名的事，"
                    f"就把名字写具体一点再记。")
        try:
            row_id = db.log_event(args.get("category"), args.get("kind"), args["name"],
                                  note=args.get("note"), ts=args.get("ts"),
                                  place=args.get("place"),
                                  ts_precision=args.get("ts_precision") or "exact")
        except ValueError as e:
            return _bad_vocab(e, args)
        # 回显必须带时间和状态。模型能自己填 ts 了，它就会把"昨天下午"记成
        # 今天 —— 不当场打出来，这种错你要等到下次查记录才发现（文档 5.2）。
        return f"已记录：{_fmt_log(db.get_log(row_id))}"

    if name == "find_logs":
        if err := _to_utc(args, "since", "until"):
            return err
        # days 不传就交给 db.find_logs 去定：有关键词搜全库，没关键词最近 30 天。
        # 这里绝不能写 args.get("days", 30) —— 那个 30 会把默认值规则顶掉。
        rows, total = db.find_logs(
            kind=args.get("kind"), keyword=args.get("keyword"),
            days=args.get("days"),
            since=args.get("since"), until=args.get("until"),
            category=args.get("category"), place=args.get("place"),
            status=args.get("status"))
        if not rows:
            return "没找到符合条件的记录。"
        # 登记进回合作用域的去重表，供本轮后面的 log_event 用
        for r in rows:
            if r["name"]:
                seen.setdefault(r["name"].strip(), r["id"])
        out = "\n".join(_fmt_log(r) for r in rows)
        if total > len(rows):
            # 截断了就必须说。不说的话模型会拿这批当全部去下结论 ——
            # 那是"把有的说成没有"的另一种长相。
            out += (f"\n（符合条件的共 {total} 条，上面是最近 {len(rows)} 条。"
                    f"要看更早的就把关键词收窄，或者用 since/until 指定时段。）")
        return out

    if name == "add_note":
        if not db.append_note(args["id"], args["text"]):
            return f"没有 id={args['id']} 这条记录，先用 find_logs 确认 id。"
        return f"已补上：{_fmt_log(db.get_log(args['id']))}"

    if name == "correct_log":
        row_id = args["id"]
        old = db.get_log(row_id)
        if old is None:
            return f"没有 id={row_id} 这条记录，先用 find_logs 确认 id。"
        if err := _to_utc(args, "ts"):
            return err

        # ⚠️ 一律用 is not None，别用真假值。db.correct_log 那边判的就是
        # is not None，两边看法不一致时空字符串会绕过确认框：模型传
        # {"name": "", "note": "x"}，框里只说"备注改成 x"，但 name 也被
        # 写成了空串 —— 一个没经过确认的字段被改掉了（README 已知问题）。
        # 空串本身也挡掉："清空"该是显式操作，不该是省略参数的副作用。
        edits = {k: args[k] for k in ("name", "note", "ts", "place",
                                      "category", "status", "ts_precision")
                 if args.get(k) is not None}
        if any(v == "" for v in edits.values()):
            return "不接受空字符串。要清空某个字段就明说，别传空串。"

        labels = {"name": "名称", "note": "备注", "ts": "时间", "place": "地点",
                  "category": "大分类", "status": "状态", "ts_precision": "时间精度"}
        changes = [
            # 确认框里说的是本地时间 —— 给人看的东西不该出现 UTC
            f"{labels[k]}改成「{db.to_local(v) if k == 'ts' else v}」"
            for k, v in edits.items()
        ]
        if not changes:
            return f"没说要改什么，{' / '.join(labels)} 至少给一个。"

        if not ui.confirm(f"要把这条{'，'.join(changes)}吗？", _fmt_log(old)):
            # 把拒绝如实告诉模型，让它知道这次没生效，别接着往下假设。
            return "用户拒绝了这次改动，记录没有变。"

        try:
            db.correct_log(row_id, **edits)
        except ValueError as e:
            return _bad_vocab(e, args)
        return f"已更正：{_fmt_log(db.get_log(row_id))}"

    if name == "remember":
        db.remember(args["category"], args["key"], args["value"], source="voice")
        return f"已记住：{args['value']}"

    if name in ("find_places", "list_place_types"):
        return _run_place_tool(name, args)

    return f"没有这个工具：{name}"


# 模型爱把日期当字面量写：{"ts": 2026-08-07 19:00}，少了引号就不是合法 JSON。
# 实测同一句话连试三次全栽在这里，不是偶发抖动，是一个稳定的模型行为。
_NAKED_TS = re.compile(r':\s*(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?)\s*(?=[,}])')


def _parse_args(raw: str | None) -> tuple[dict, str]:
    """解析模型给的工具参数。返回 (参数, 出错时给模型看的话)。

    这里是**交界处**（设计文档 12.2：故障出现在模块交界处，所以契约要写在
    交界处）。改之前这里是一句裸的 json.loads —— 模型吐一个畸形 JSON，
    异常直接冒泡，整轮工具调用全废。chat.py 外面那层 try 只能兜住不退出，
    兜不住"这一轮什么都没干成"。

    两层处理，顺序不能反：

    1. **窄修补**：只补日期缺的那对引号。意图完全无歧义（`2026-08-07 19:00`
       不可能是别的东西），而且这个畸形是稳定复现的。修补要**打印 + 落库**，
       不许静默 —— 悄悄改模型的输出是条滑坡，能修的前提是看得见修了什么。
    2. **修不好就如实退回**，并且**说清楚错在哪一个字段**。只说"JSON 不合法"
       它多半会原样再来一遍；指名道姓说"ts 少了引号"，它才改得掉。
    """
    raw = raw or "{}"
    try:
        return json.loads(raw), ""
    except json.JSONDecodeError:
        pass

    fixed = _NAKED_TS.sub(r': "\1"', raw)
    if fixed != raw:
        try:
            args = json.loads(fixed)
        except json.JSONDecodeError:
            pass
        else:
            print("  ⚠ 模型的日期没加引号，已自动补上（原样已记进 events）")
            db.record_event("cli", "json_repair", {"raw": raw, "fixed": fixed})
            return args, ""

    db.record_event("cli", "bad_json", {"raw": raw})
    return {}, ("你给的参数不是合法 JSON，这次调用没执行。"
                "常见原因：日期这类值必须用双引号包成字符串，"
                '写成 "ts": "2026-08-07 19:00"，不能写成 "ts": 2026-08-07 19:00。'
                "检查一遍引号再调一次。")


def _bad_vocab(err: ValueError, args: dict) -> str:
    """分类填错了：告诉模型重填，同时把这次失败落账。

    跟 _to_utc 一个路子 —— 参数填错不是程序坏了，抛异常会打断整场对话，
    给它一句人话让它重填就行。

    落账是为了以后能回答"到底是模型笨还是词表定错了"。这一条天天出现，
    说明该改的是词表（文档 12.1：没有账本就没有优化）。
    """
    db.record_event("cli", "bad_vocab", {
        "error": str(err),
        "category": args.get("category"), "kind": args.get("kind"),
        "status": args.get("status"),
    })
    return (f"{err} 从清单里挑一个重填，不要造新值；"
            f"判不准就用 other。别把这个错误告诉用户，直接重试。")


def _been_there(store: str) -> str:
    """这个地方用户去过没有 / 约了要去没有。空字符串表示一条记录都没有。

    这一行就是整个推荐功能的支点：高德能说出附近有什么，但"这家你去过两次、
    上次说排队久但值"只有 logs 有。设计文档 7.5 说的、赢得过美团的唯一原因
    就是这一条 —— 所以候选集里每一家都要过一遍这个函数。

    它是**那个 join**：高德返回一批店，status 在 logs 里，总得有段代码把
    两边接起来。不能让模型自己一家一家去 find_logs —— 那是二十次工具调用。

    "去过"和"约了要去"直接按 status 分组，不做任何时间判断：status 存的
    就是这件事发生了没有，再拿时间戳猜一遍纯属多此一举，还会猜错 ——
    改之前它拿 rows[0] 当"最近一次"，而未来的计划排序在最前，
    于是"下周约了要去"会被报成"你去过1次"。
    """
    rows = db.place_history(store)
    if not rows:
        return ""

    done = [r for r in rows if r["status"] == "done"]
    planned = [r for r in rows if r["status"] == "planned"]

    parts = []
    if done:
        last = done[0]
        bit = f"你去过{len(done)}次，最近 {db.to_local(last['ts'])}"
        if last["name"]:
            # kind 不是 meal 时别说成"吃的"——在咖啡馆学习也会记 place
            bit += f"{'吃的' if last['kind'] == 'meal' else '：'}{last['name']}"
        if last["note"]:
            bit += f"，你说「{last['note']}」"
        parts.append(bit)
    for r in planned:
        when = db.to_local(r["ts"])
        if r["ts"] < db.now():
            parts.append(f"你 {when} 约了要去，那天已经过了 —— 去没去？")
        else:
            parts.append(f"{when} 还约了要去")
    return "；".join(parts)


def _run_place_tool(name: str, args: dict) -> str:
    """find_places / list_place_types。两个都要先定中心点，所以放在一起。"""
    try:
        center, label = _resolve_center(args.get("near"))
    except amap.GeocodeError as e:
        # 定位不了不是程序坏了，是用户说的地方太含糊。
        # 把原话转给模型让它去问，比抛异常打断整场对话强。
        return f"{e} 让用户说得具体一点，别自己猜一个地方查。"
    except (amap.AmapError, OSError) as e:
        # 地址整个解析不出来时高德会直接报错（实测编一个不存在的路名会
        # 返回 30001）。这一步也必须接住 —— 工具抛异常会打断整场对话，
        # 而这只是"这个地名查不了"，说一声就行。
        return (f"定位「{args.get('near')}」失败：{e}。"
                f"告诉用户这个地名查不到，让他换个说法，不要编一个地方。")

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
        "用户提到吃了什么、发生过什么事、接下来要做什么，就调 log_event 记下来，"
        "时间、地点、跟谁、细节都填进去，不用问他要不要记。"
        "**没做成的事同样要记**（约了没去成、计划了又取消），那是新的一条记录，不是更正。",
        "看见记录标着「早就过了，还没确认做没做」，可以顺口问一句做了没；"
        "他说做了就用 correct_log 把 status 改成 done。",
        "用户提到某个人（名字、「我朋友」「同事」），先用 find_logs 的 keyword 查一遍这个人 ——"
        "人名多半写在备注里，keyword 三列一起搜才找得到。"
        "查到过往有值得提的（放过他鸽子、上次很尽兴、老是迟到），**主动说出来**，别等他问。"
        "这是他自己的记录，是这个助手唯一比搜索引擎强的地方。",
        "只有当用户明确指向一条已有记录时（「那条记错了」「上次你记的不对」），"
        "才先用 find_logs 查出来复述、再 correct_log。"
        "他只是在讲一件事，哪怕这件事没办成，也一律是 log_event 新记一条。",
        "推荐吃的地方之前，先用 find_logs(kind=\"meal\") 看他最近吃了什么，别推他刚吃过的。",
        "find_places 的结果里标了「你去过N次」的，一定要把去过几次、上次说了什么讲出来 ——"
        "这是他自己的记录，比评分有用得多。没标的就是没去过，别编成去过。",
        "用户没说想吃什么就先调 list_place_types，拿着这一带真实有的菜系给他两三个具体方向，"
        "别空口问「你想吃什么」。",
        "查不到的信息就说没查到，绝不编。编一家不存在的餐厅比不回答糟糕得多。",
        "每条用户消息开头方括号里的是系统加的当前时间，不是用户说的内容，别复述它。"
        "所有工具的时间参数都填本地时间，格式 2026-08-10 15:00。",
        "用户说的时间没定死（「这周末」「过段时间」「下个月」），"
        "log_event 的 ts 就填那个范围的开头，同时把 ts_precision 填成 week / month —— "
        "**别自己编一个具体钟点**。编了的话以后它会被当成一个真约好的时间来追问你「做了没」。",
        "开头那段总结是系统按库里的记录直接列出来的，不是你说的。"
        "用户回一句「第几条做了」，就用 correct_log 把那条 status 改成 done；"
        "他顺口给的评价用 add_note 补上去，**别拿评价去覆盖原来的备注**。",
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
    """跑一轮对话（含工具调用），返回模型最终说的话。messages 会被就地更新。

    ⚠️ seen 是**回合作用域**的去重表：{记录名: id}。

    一个「回合」= 用户说一句话 → 模型可能连着绕好几圈工具 → 最后回一句话，
    也就是下面这个 for 循环的整个生命周期。seen 是这个函数的局部变量，
    循环一结束就跟着销毁 —— 所以下一回合用户再提同一件事，照常记得下来。

    为什么作用域必须是回合，不能是"同名同日就拦"：同一天吃两次牛肉面是
    合法的，按名字加日期硬拦会误伤。而"你这一轮刚查到过它，还要再记一遍"
    这个信号是精确的，不会误判。
    """
    seen: dict[str, int] = {}
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
            args, err = _parse_args(tc.function.arguments)
            if err:
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": err})
                continue
            # 先打印再执行，两个原因：确认框在 _run_tool 里弹，你得先看见
            # 它要干什么再被问；而且 _to_utc 会就地把本地时间换成 UTC，
            # 打印晚了就看不到模型原本填的是什么了 —— 这行的全部意义
            # 就是"模型到底填了什么"。
            if verbose:
                print(f"  · {tc.function.name}({', '.join(f'{k}={v}' for k, v in args.items())})")
            result = _run_tool(tc.function.name, args, seen)
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})

    return "（工具调用绕了太多轮，我先停下了。）"
