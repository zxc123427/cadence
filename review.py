"""每天晚上的总结 —— cadence 的第一个主动场景（设计文档 6.2 的"场景规则"那一格）。

    text = review.tonight()      # 到点了且闸门放行，返回要说的话；否则 None

它做的事：晚上你一开对话，它先开口问"今天快要结束了，要总结一下吗"，
然后把**标了计划但时间已经过了、还没确认做没做**的事一件件摆出来，
顺带说一句明天有什么。

**这一层的产出是一段纯代码渲染好的文本，不过模型。** 跟 6.1"闸门必须纯代码"
是同一个道理：你看到的那段字一个字都不会被模型改写、漏报或者编。
模型只负责接下来的对话 —— 你说"第 12 条做了"，它去 correct_log。

⚠️ **没做完就没做完，不补、不追。** review 真正的作用是收集你的评价、
改 status，也就是它必须有反向输入。没人回答的时候扫库是没有意义的，
所以这里既没有定时兜底扫描，也没有"上次没答完，这次接着问"。
这个系统不是侦探，它不用把每件事都弄清楚 —— 你不想反馈的时候，
干什么非逼着你反馈。（写进文档 15）
"""

import db
import gate

# 播报窗口：本地 21:30 – 23:30。窗口外一条提案都不生成。
#
# 上界 23:30 跟 gate.QUIET_START 是同一个时刻，这是刻意对齐的：
# 窗口关的那一秒静默时段正好开始，中间不留缝。留了缝就会出现
# "提案生成了，但闸门拦了"的空转，日志里多一堆没意义的 quiet_hours。
WINDOW_START = (21, 30)
WINDOW_END = (23, 30)

# 一次最多摆几条。超了必须**连真实总数一起说** —— 见 render 的注释。
REVIEW_LIMIT = 5

# 库里少于这么多条时，频次不参与排序。
# 数据太少的时候什么都是"低频"，那个信号是噪音不是信息。
COLD_START_MIN = 30


def in_window(ts: str | None = None) -> bool:
    return WINDOW_START <= db.local_hm(ts or db.now()) < WINDOW_END


def rank(rows: list, counts: dict[str, int], total: int) -> list:
    """最该问的排前面。

    两遍排序而不是写一个复杂的 key —— Python 的 sort 是稳定的，
    先排的那一遍会在后一遍里作为并列时的次序保留下来。
    这比把三个字段塞进一个 tuple 好读，也好改。
    """
    rows = sorted(rows, key=lambda r: r["ts"], reverse=True)   # 近的在前
    return sorted(rows, key=lambda r: (
        # 有别人在的事排前面：鸽了人的代价比自己少跑一趟健身房高得多
        0 if r["category"] == "appointment" else 1,
        # 频次低的排前面。考试、独自出游半年一次，值得问；
        # 吃饭健身天天有，问一遍就烦了
        counts.get(r["kind"], 0) if total >= COLD_START_MIN else 0,
    ))


def render(overdue: list, tomorrow: list) -> str | None:
    """把两批数据拼成要说的话。两批都空就返回 None —— 没话找话是最讨嫌的主动性。

    ⚠️ **截断必须连真实总数一起说。** 只摆五条不吭声，你会以为就这五条 ——
    那是 P3「把有的说成没有」，跟 find_logs 上一轮踩的是同一个坑。
    """
    if not overdue and not tomorrow:
        return None

    lines = ["今天快要结束了，要总结一下吗？"]

    if overdue:
        shown = overdue[:REVIEW_LIMIT]
        lines.append("\n这些你标了计划，时间已经过了 —— 做了没？")
        lines += [f"  {_line(r)}" for r in shown]
        if len(overdue) > len(shown):
            lines.append(f"  （还有 {len(overdue) - len(shown)} 条没列，"
                         f"python3 logs.py --status planned 全看）")

    if tomorrow:
        lines.append("\n明天：")
        lines += [f"  {_line(r)}" for r in tomorrow[:REVIEW_LIMIT]]
        if len(tomorrow) > REVIEW_LIMIT:
            lines.append(f"  （还有 {len(tomorrow) - REVIEW_LIMIT} 条）")

    return "\n".join(lines)


def _line(r) -> str:
    """带 id 的一行。id 必须在 —— 你说"第 12 条做了"，模型才有东西可以 correct_log。"""
    s = f"[{r['id']:>3}]  {db.when(r)}  {r['category']}/{r['kind']}  {r['name']}"
    if r["place"]:
        s += f"  @{r['place']}"
    if r["note"]:
        s += f"（{r['note']}）"
    return s


def propose(ts: str | None = None) -> int | None:
    """在窗口内就生成今晚的提案入库，返回 nudge id。窗口外或没话说返回 None。

    同一晚开三次 chat.py 只会有一条提案 —— dedupe_key 顶着（见 db.propose_nudge）。
    """
    ts = ts or db.now()
    if not in_window(ts):
        return None

    day = db.local_day(ts)
    overdue = rank(db.overdue_logs(ts), db.kind_counts(), db.total_logs())
    tomorrow = db.upcoming_logs(days=1, ts=ts)
    text = render(overdue, tomorrow)
    if text is None:
        return None

    return db.propose_nudge(
        "review", text, dedupe_key=f"review:daily:{day}",
        # 今晚窗口一关就作废。**绝不补播** —— 明早七点跟你说
        # "昨天快要结束了" 比不说更烦（6.3）。
        expires_ts=db.from_local(f"{day} {WINDOW_END[0]:02d}:{WINDOW_END[1]:02d}"),
    )


def tonight(ts: str | None = None) -> str | None:
    """chat.py 启动时调这一个函数：生成提案 → 过闸门 → 返回该说的话。

    提案和闸门分开两步、中间落一次库，是 6.2 的核心纪律：
    **任何主动发言都不许直接播出去，必须先变成一条提案入库。**
    这样"它为什么没提醒我"才查得出来 —— 被拦的那条带着原因躺在 nudges 里。
    """
    ts = ts or db.now()
    propose(ts)
    # 返回闸门放行的**全部**内容，不只是 review 的。
    # 将来多了别的 kind，漏掉谁就等于"标记成发过了但其实没说出口"——
    # 那是 P3「把有的说成没有」在主动性层的形态，而且极难发现：
    # 库里明明白白写着 fired_at，你却从没看见过那句话。
    fired = gate.run(ts)
    return "\n\n".join(n["payload"] for n in fired) if fired else None
