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

from openai import OpenAI

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


def _fmt_log(r) -> str:
    return (f"id={r['id']}  {db.to_local(r['ts'])}  {r['kind']}  {r['name']}"
            + (f"（{r['note']}）" if r["note"] else ""))


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
                },
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
                    "days": {"type": "integer", "description": "往前看几天，默认 30"},
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
                },
                "required": ["id"],
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
        db.log_meal(args["name"], note=args.get("note"))
        return f"已记录：{args['name']}"

    if name == "find_logs":
        rows = db.find_logs(kind=args.get("kind"), name=args.get("name"),
                            days=args.get("days", 30))
        if not rows:
            return "没找到符合条件的记录。"
        return "\n".join(_fmt_log(r) for r in rows)

    if name == "correct_log":
        row_id = args["id"]
        old = db.get_log(row_id)
        if old is None:
            return f"没有 id={row_id} 这条记录，先用 find_logs 确认 id。"

        changes = []
        if args.get("name"):
            changes.append(f"名称改成「{args['name']}」")
        if args.get("note"):
            changes.append(f"备注改成「{args['note']}」")
        if not changes:
            return "没说要改什么，name 和 note 至少给一个。"

        if not ui.confirm(f"要把这条{'，'.join(changes)}吗？", _fmt_log(old)):
            # 把拒绝如实告诉模型，让它知道这次没生效，别接着往下假设。
            return "用户拒绝了这次改动，记录没有变。"

        db.correct_log(row_id, name=args.get("name"), note=args.get("note"))
        return f"已更正 id={row_id}"

    if name == "remember":
        db.remember(args["category"], args["key"], args["value"], source="voice")
        return f"已记住：{args['value']}"

    return f"没有这个工具：{name}"


# ---------- system prompt ----------

def system_prompt() -> str:
    lines = [
        "你是 cadence，一个只服务于一个人的私人助手。说话简短、直接，不用客服腔，不要每句都确认。",
        "用户提到吃了什么就调 log_meal 记下来，不用问他要不要记。",
        "用户说之前哪条记错了，先用 find_logs 查出来复述给他，确认是哪一条之后再用 correct_log。",
        "查不到的信息就说没查到，绝不编。编一家不存在的餐厅比不回答糟糕得多。",
    ]

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
            result = _run_tool(tc.function.name, args)
            if verbose:
                print(f"  · {tc.function.name}({', '.join(f'{k}={v}' for k, v in args.items())})")
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})

    return "（工具调用绕了太多轮，我先停下了。）"
