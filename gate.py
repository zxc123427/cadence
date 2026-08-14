"""主动发言的闸门（设计文档 6.3）。

    allowed, reason = gate.check(nudge)

**这个模块绝不 import llm / openai。** 6.1 的原话：现在该说吗、还值得说吗、
今天说得够多了吗、上次说完你什么反应 —— 这四个判断全部不需要模型，
纯代码就能做，而且必须纯代码做。让模型判断"我现在该不该打断你"，
你将永远无法解释它为什么在凌晨三点开口。

四条规则里**每日预算是最反直觉也最重要的一条**：它强制你面对
"如果今天只能说三句话，说哪三句"。没有这个上限，每加一个场景就多一批提醒，
系统会平滑地滑向讨厌，而且没有任何一天你会察觉到它变糟了（6.3）。

⚠️ 所有判断都接受一个 now 参数（默认 db.now()）。不是为了灵活 ——
是因为不给这个参数，check.py 就没法测"凌晨三点会不会被拦"，
总不能为了跑一次自检去改系统时钟。
"""

from datetime import datetime, timedelta

import db

# ---------- 四条规则的取值 ----------

# 静默时段：本地 23:30 – 07:00，不给穿透。
#
# ⚠️ 文档 6.3 建议的是 22:00，这里定 23:30，理由要写清楚：
# **22:00 那个数字是为"它主动弹出来打断你"定的**，而现在唯一的 nudge
# 是 review —— 你自己开对话才会触发它，打断强度差一个量级。
#
# 等计时器做出来、真有东西会主动弹的时候，再考虑把静默时段按打断强度分档。
# 现在只有一种 nudge 就先分档，是过早抽象。
QUIET_START = (23, 30)
QUIET_END = (7, 0)

# 同类冷却：每个 kind 一个时长（分钟）。没登记的走默认值。
DEFAULT_COOLDOWN = 60
COOLDOWN = {
    # review 一天最多一次。20 小时而不是 24，是留出余量：
    # 昨天 23:00 播的，今天 21:30 就该能再播，24 小时会把今天整个卡掉。
    "review": 20 * 60,
}

# 主动发言一天总共不超过几条。用完就闭嘴，不管队列里还有什么。
DAILY_BUDGET = 3

# 被 mute 几次之后直接停用这个 kind（6.4：muted 必须有确定性后果）。
MUTE_DISABLE_AT = 3


def _hm(ts: str) -> tuple[int, int]:
    return db.local_hm(ts)


def in_quiet_hours(ts: str | None = None) -> bool:
    """现在是不是静默时段。跨零点，所以是"或"不是"且"。"""
    hm = _hm(ts or db.now())
    return hm >= QUIET_START or hm < QUIET_END


def mute_count(kind: str) -> int:
    """这个 kind 被你明确嫌弃过几次。

    不另开一张表存"静音状态" —— 它已经在 nudges.outcome 里了。
    你说"别再提醒我这个"就是给某一条 nudge 打上 muted，
    数一下有几条就是这个 kind 被嫌弃了几次。

    好处不只是省一张表：**这个数字天生可追溯**。哪一天、因为哪一条
    被嫌弃的，`python3 nudges.py` 一翻就看得见，而一个布尔开关
    只能告诉你"现在是关的"，说不出为什么。
    """
    with db.connect() as conn:
        return conn.execute(
            "SELECT count(*) FROM nudges WHERE kind = ? AND outcome = 'muted'",
            (kind,)).fetchone()[0]


def cooldown_minutes(kind: str) -> float:
    """这个 kind 的冷却时长，已经算上被 mute 的惩罚。

    每被 mute 一次翻一倍（6.4 的硬规则：muted 必须有确定性后果，
    **绝不能是"模型记住了你不喜欢"** —— 你说了别再提醒，第二天它又提醒了，
    这一次失信的损失比这个功能全部的价值还大）。

    翻到 MUTE_DISABLE_AT 次就直接停用，返回无穷大。
    """
    n = mute_count(kind)
    if n >= MUTE_DISABLE_AT:
        return float("inf")
    return COOLDOWN.get(kind, DEFAULT_COOLDOWN) * (2 ** n)


def check(nudge, ts: str | None = None) -> tuple[bool, str | None]:
    """该不该把这条播出去。返回 (允许吗, 不允许的原因)。

    原因用的是 db.DROP_REASONS 里的固定值，因为一个月后要 GROUP BY 它
    看"到底是哪条规则在拦"。四条规则的顺序是有讲究的：**先判过期**，
    因为一条早就该作废的提案不该占用今天的预算。
    """
    ts = ts or db.now()

    # 1. 过期作废。到点没发出去就丢弃，绝不补播 —— 迟到的提醒比不提醒更烦。
    if nudge["expires_ts"] and nudge["expires_ts"] < ts:
        return False, "expired"

    # 2. 静默时段
    if in_quiet_hours(ts):
        return False, "quiet_hours"

    # 3. 同类冷却
    last = db.last_fired(nudge["kind"])
    if last is not None:
        limit = cooldown_minutes(nudge["kind"])
        if limit == float("inf"):
            return False, "cooldown"        # 被 mute 够次数了，这个 kind 停用
        gap = datetime.strptime(ts, db.TS_FMT) - datetime.strptime(last, db.TS_FMT)
        if gap < timedelta(minutes=limit):
            return False, "cooldown"

    # 4. 每日预算。按**本地日**算 —— 按 UTC 日算的话，东八区每天早上
    #    八点之前预算都是错的，而且不报错（见 db.local_day）。
    if len(db.fired_on(db.local_day(ts))) >= DAILY_BUDGET:
        return False, "daily_budget"

    return True, None


def run(ts: str | None = None) -> list:
    """把待发队列过一遍闸门，返回**允许播出去**的那些行（已标记 fired）。

    被拦的当场落库记原因，不返回。调用方只管把返回的内容播出去。
    """
    ts = ts or db.now()
    passed = []
    for n in db.pending_nudges():
        allowed, reason = check(n, ts)
        if allowed:
            db.fire_nudge(n["id"], ts)
            passed.append(n)
        else:
            db.drop_nudge(n["id"], reason)
    return passed
