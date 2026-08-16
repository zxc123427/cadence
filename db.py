"""cadence 的数据层：所有读写都从这里走。

四条纪律（设计文档 5.6）：
  1. 追加不覆盖 —— 记忆会过时，但旧版本要留着，才查得出"它为什么这么以为"
  2. 时间戳一律 UTC + ISO 8601 —— 只在显示给人看的时候才转本地时区
  3. 别过早拆表 —— 宽表 + 一个 JSON 字段放杂项，跑半年再重构
  4. 分类走受控词表，代码强制 —— 让模型自由填，它会造出 exam/test/考试
     三种写法，然后按分类什么都查不全，而且一声不吭

对外只暴露窄接口（设计文档 5.7）：
  没有 execute_sql()。想干什么就加一个具体函数。
  收窄接口就是收窄 debug 范围 —— 数据变成什么样，一定能追到是哪个函数写的。
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / "cadence.db"

# 四张表。conversations（对话摘要）等到真需要时再加，见设计文档 5.5 ——
# 但它也会放在这同一个库里。
SCHEMA = """
-- 所有事件的流水账。出问题时唯一能查的东西。
-- 字段对应设计文档 2.5 的统一内部消息格式。
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY,        -- 每一行的编号，自动递增，不用自己填
    source  TEXT NOT NULL,              -- 文本类型，NOT NULL = 不能为空
    type    TEXT NOT NULL,              -- state_change / utterance / schedule
    payload TEXT,                       -- 没写 NOT NULL，所以可以为空。JSON 杂项塞这里
    ts      TEXT NOT NULL               -- UTC ISO 8601
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);

-- 客观记录：做过的和要做的。宽表，靠两级分类区分（见下面的 CATEGORIES / KINDS）。
CREATE TABLE IF NOT EXISTS logs (
    id         INTEGER PRIMARY KEY,
    category   TEXT NOT NULL DEFAULT 'personal',  -- 大分类：有没有别人
    kind       TEXT NOT NULL,           -- 小分类：是什么事（meal / study / travel ...）
    name       TEXT,                    -- "牛肉面" / "期末考试"
    ts         TEXT NOT NULL,           -- 事情发生（或将要发生）的时间（UTC）
    created_at TEXT NOT NULL,           -- 写进库的时间（UTC），两者可以不同
    note       TEXT,
    place      TEXT,                    -- 在哪儿。店名、医院、城市都行，没有就为空
    status     TEXT NOT NULL DEFAULT 'done',      -- planned / done
    ts_precision TEXT NOT NULL DEFAULT 'exact',   -- ts 这个时间有多准，见 TS_PRECISIONS
    extra      TEXT                     -- JSON 杂项
);
CREATE INDEX IF NOT EXISTS idx_logs_kind_ts ON logs(kind, ts);

-- 偏好与约束。同一个 key 写多次 = 追加多个版本，读取只取最新未过期的那条。
CREATE TABLE IF NOT EXISTS memories (
    id         INTEGER PRIMARY KEY,
    category   TEXT NOT NULL,           -- preference / constraint / fact
    key        TEXT NOT NULL,           -- 归一化的 key，如 food.dislike.cilantro
    value      TEXT NOT NULL,           -- 人话放这里
    source     TEXT NOT NULL,           -- manual / voice / extraction
    created_at TEXT NOT NULL,
    expires_at TEXT                     -- 可空。短期状态用，如"这个月在减脂"
);
CREATE INDEX IF NOT EXISTS idx_memories_key ON memories(key, created_at);

-- 主动发言的提案与结果（设计文档 6.2）。
-- **任何主动发言都不许直接播出去，必须先变成这里的一行。**
-- 这样"它为什么提醒我"和"它为什么没提醒我"两个方向都查得出来 ——
-- 后者才是难的：没发生的事不留痕，你就永远不知道闸门是不是拦错了。
CREATE TABLE IF NOT EXISTS nudges (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL,          -- review / upcoming / ...
    urgency     INTEGER NOT NULL DEFAULT 1,   -- 0 可有可无 / 1 普通 / 2 重要
    payload     TEXT,                   -- 要说的话。**纯代码渲染好的**，不过模型
    dedupe_key  TEXT NOT NULL UNIQUE,   -- 幂等靠它，见 propose_nudge 的注释
    created_at  TEXT NOT NULL,
    expires_ts  TEXT,                   -- 晚于此时间作废，不补播
    fired_at    TEXT,                   -- 空 = 还没发
    dropped_reason TEXT,                -- quiet_hours / cooldown / daily_budget / expired
    outcome     TEXT                    -- ignored / acked / acted / muted
);
CREATE INDEX IF NOT EXISTS idx_nudges_kind_fired ON nudges(kind, fired_at);
"""


# ---------- 受控词表：logs 的两级分类 ----------
#
# 分两级，是因为"是什么事"和"有没有别人"是**正交的两个维度**。
# 塞进一列一定会碎：按活动分，"约了人一起去健身"是健身；按有没有别人分，
# 它是约。一列装不下两个答案，硬装就会在这种例子上二选一。
#
# 教训写在设计文档 5.6：动手前先确认分类的维度是不是正交的。

# 大分类：有没有别人。穷尽（一件事要么涉及别人要么不涉及），所以没有兜底值。
CATEGORIES = ("appointment", "personal")

# 小分类：是什么事。不穷尽（剪头发、修电脑…），所以留 other 兜底。
# ⚠️ 判不准就填 other，绝不造新值 —— 造了以后按 kind 就查不全，而且不报错。
KINDS = ("meal", "study", "exercise", "travel", "health", "purchase", "other")

# 事情做了没有。写入时由 _status_for() 从 ts 推出来，之后只有用户开口才改。
STATUSES = ("planned", "done")

# ts 这个时间有多准。
#
# 为什么要单开一列：**ts 一列同时装了两个答案** —— "什么时候发生"和
# "这个时间有多准"。ts 是 NOT NULL，于是"周末跟家人聚餐"逼着模型瞎猜一个
# 时间填进去，写成 planned，周一那天就变成"过期未确认"，助手会问你
# "周末跟家人聚餐做了没" —— 可那件事的时间从来就没定过。
#
# 这跟当初 category / kind 拆两级是同一类问题（5.6）：动手前先问一句
# "会不会有一件事同时属于两类"。会，就得拆。
#
# ⚠️ 拆法是 **ts 存区间的起点 + 精度另开一列**，不是"再开一列存模糊时间的原话"。
# 存原话有两个致命问题：
#   1. 不可比较。"周末"是个字符串，SQL 查不了"这周有哪些还没定死的事"
#   2. 会失效。三周后那条记录还写着"下周"，但它指的是三周前的那个下周
# 存起点两个问题都没有 —— 所有现成的区间查询和排序一行都不用改。
TS_PRECISIONS = ("exact", "day", "week", "month")

# 主动发言被闸门拦下来的原因（设计文档 6.3 的四条规则）。
# 落库的是这几个固定值而不是自由文本 —— 一个月后要 GROUP BY 它来看
# "到底是哪条规则在拦"，自由文本会拼出三种写法然后什么都统计不出来。
DROP_REASONS = ("quiet_hours", "cooldown", "daily_budget", "expired")

# 播报之后你的反应。这是全系统唯一一个"它做得对不对"的真实标签（6.4）。
OUTCOMES = ("ignored", "acked", "acted", "muted")

# ⚠️ 两级取值不许有交集。这是"category 和 kind 别填反"唯一可靠的防线 ——
# 靠注释提醒不管用，靠值域不重叠，填反了校验当场就报错。
assert not set(KINDS) & set(CATEGORIES), "两级分类的取值重叠了，填反了就查不出来"


def _one_of(value: str | None, allowed: tuple[str, ...], field: str) -> str:
    """校验受控词表的取值。宽进严出：先归一化，再严格比对。

    归一化是必要的：模型返回 "Meal" 或 " meal " 是常事，为这个报错纯属
    自找麻烦。落库的永远是小写。

    空串必须显式挡掉 —— **SQLite 的 TEXT 列里空串不违反 NOT NULL**。
    不挡，库里就会长出一批 kind='' 的行，而且一声不吭。这跟 5.6 说的
    "格式不对的字符串塞进 TEXT 列，SQL 比较只会安静地给出错误结果"
    是同一类坑。
    """
    v = (value or "").strip().lower()
    if v not in allowed:
        raise ValueError(f"{field} 只能是 {' / '.join(allowed)} 之一，收到的是 {value!r}")
    return v


# ---------- 时间：存 UTC，显示本地 ----------

# 库里所有时间戳的格式。只此一份，别再散着写字面量。
TS_FMT = "%Y-%m-%dT%H:%M:%SZ"

# 人说的时间格式。模型和 CLI 只用这个，UTC 不出 db.py 这一层。
LOCAL_FMT = "%Y-%m-%d %H:%M"


def now() -> str:
    """当前时间，UTC ISO 8601。所有写库的时间戳都用它。"""
    return datetime.now(timezone.utc).strftime(TS_FMT)


def days_ago(n: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=n)).strftime(TS_FMT)


def to_local(ts: str) -> str:
    """UTC 时间戳 -> 本地时间的可读字符串。只用于显示，不要写回库里。"""
    dt = datetime.strptime(ts, TS_FMT).replace(tzinfo=timezone.utc)
    return dt.astimezone().strftime("%m-%d %H:%M")


def local_day(ts: str | None = None) -> str:
    """这个 UTC 时间戳落在本地的哪一天，如 "2026-08-14"。

    ⚠️ **"今天"必须按本地日算，不能按 UTC 日。** 东八区的 08:00 之前
    UTC 还停在昨天 —— 拿 ts[:10] 当日期用，每天早上八点前的判断全是错的，
    而且不报错。每日预算和 dedupe_key 都踩在这个坑边上，所以只此一份。
    """
    dt = datetime.strptime(ts or now(), TS_FMT).replace(tzinfo=timezone.utc)
    return dt.astimezone().strftime("%Y-%m-%d")


def local_hm(ts: str | None = None) -> tuple[int, int]:
    """这个 UTC 时间戳的本地时和分。闸门判静默时段用，理由同 local_day。"""
    dt = datetime.strptime(ts or now(), TS_FMT).replace(tzinfo=timezone.utc)
    local = dt.astimezone()
    return local.hour, local.minute


def when(row) -> str:
    """一条记录的时间，按 ts_precision 换措辞。**显示专用。**

    存在的理由：ts 对没定死的事只是个区间起点。把"这周末跟家人聚餐"
    显示成 "08-16 00:00" 是在撒谎 —— 那个 00:00 是代码填的，不是你说的，
    可它看起来跟一个真约好的时间一模一样。

    宁可显示得粗，也不能显示得比实际知道的更精确。
    """
    precision = row["ts_precision"] if "ts_precision" in row.keys() else "exact"
    if precision == "exact":
        return to_local(row["ts"])
    dt = datetime.strptime(row["ts"], TS_FMT).replace(tzinfo=timezone.utc).astimezone()
    if precision == "day":
        return dt.strftime("%m-%d") + " 某时"
    if precision == "week":
        return dt.strftime("%m-%d") + " 那周"
    return dt.strftime("%m 月")


def from_local(s: str) -> str:
    """to_local 的逆：本地时间 "2026-08-10 15:00" -> UTC ISO。

    存在的理由：模型没有可靠的时区算术能力。让它填 UTC，它得把
    "昨天下午三点"减一天再减八小时，算错了还不报错，静默写进库。
    所以模型只说本地时间，换算放在这里做一次。

    格式不对就抛 ValueError，绝不猜。ts 是 TEXT 列，一个格式不对的
    字符串塞进去，SQL 比较不会报错，只会安静地给出错误结果。
    """
    dt = datetime.strptime(s.strip(), LOCAL_FMT)      # 格式不对在这里就炸
    # strptime 出来的是 naive datetime；astimezone 会按系统本地时区解释它
    return dt.astimezone(timezone.utc).strftime(TS_FMT)


# ---------- 连接与迁移 ----------

# 后来才加的列。表已经存在时，改上面的 SCHEMA 是没用的 ——
# CREATE TABLE IF NOT EXISTS 看见表在就直接跳过，不会去补列。
# 所以每加一列都要在这里登记一笔，由 _migrate() 补上。
#
# 新库走 SCHEMA 一次建全，老库走这里补齐，两条路结果一样。
# ⚠️ 带非空默认值的 ADD COLUMN 会**自动回填老行** —— 加 status 时现有的
# 记录全部变成 'done'，正好是对的（它们都是已经发生过的事）。
# SQLite 只允许"NOT NULL + 有默认值"这一种组合，光写 NOT NULL 会被拒绝。
NEW_COLUMNS = [
    ("logs", "place", "TEXT"),
    ("logs", "status", "TEXT NOT NULL DEFAULT 'done'"),
    ("logs", "category", "TEXT NOT NULL DEFAULT 'personal'"),
    # 老行全部回填成 exact，这是对的：以前记的本来就都是具体时间。
    # 个别不对的（当初被迫瞎猜的那种）用 correct_log 改。
    ("logs", "ts_precision", "TEXT NOT NULL DEFAULT 'exact'"),
]


def _migrate(conn: sqlite3.Connection) -> None:
    """把已存在的表补成 SCHEMA 描述的样子。

    幂等：跑多少次结果都一样 —— 先问表里现在有哪些列，缺了才加。
    所以它可以放在每次 connect() 里，不需要记"迁移到第几版了"。

    只加列，不改列、不删列。SQLite 对后两者支持很差，真要做的时候
    正规做法是"建新表 → 搬数据 → 换名字"，那一步该单独写脚本、
    单独备份，不该藏在 connect() 里悄悄发生。
    """
    for table, column, coltype in NEW_COLUMNS:
        # PRAGMA table_info 返回这张表的所有列，row[1] 是列名
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")


def connect() -> sqlite3.Connection:
    """打开数据库，第一次会自动建表，老库自动补新列。"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row          # 让查询结果能用列名访问：row["name"]

    # WAL：写的时候不动主文件，把新内容追加到 cadence.db-wal，读的人照常读主文件。
    # 默认的 delete 模式是直接改主文件，于是**一个正在读的连接会挡住写**——
    # 实测 delete 模式下，读者开着事务时写者提交直接 database is locked。
    #
    # 现在只有一个进程，用不上。加在这里是因为待办 F（计时器）和 A（后台归档）
    # 都会引入第二个进程 —— 那时 propose_nudge 靠 UNIQUE 防竞态的思路才真正被考验。
    #
    # 这一句是**持久化**的：它写进 cadence.db 的文件头，设一次就永远是 WAL，
    # 每次 connect 都跑只是图省事（幂等）。代价是库变成三个文件（.db / -wal / -shm），
    # 备份要一起拷，单独拷 .db 会丢掉还没 checkpoint 的最近写入。
    #
    # ⚠️ 必须在 executescript 之前：PRAGMA journal_mode 在事务里执行会静默失败。
    #
    # 配套的 busy_timeout（撞锁后重试多久）不用设 —— Python 的 sqlite3.connect
    # 有个 timeout=5.0 参数，默认就把它设成了 5000ms。实测 PRAGMA busy_timeout
    # 返回 5000。写一句 PRAGMA busy_timeout=5000 是把 5000 设成 5000。
    conn.execute("PRAGMA journal_mode=WAL")

    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


# ---------- 窄接口：写 ----------

def record_event(source: str, type: str, payload: dict | None = None) -> int:
    """往流水账里记一笔。以后摄像头、语音、定时器都往这里写。"""
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO events (source, type, payload, ts) VALUES (?, ?, ?, ?)",
            (source, type, json.dumps(payload or {}, ensure_ascii=False), now()),
        )
        return cur.lastrowid


def _status_for(ts: str) -> str:
    """事情在未来就是 planned，在过去就是 done。

    为什么不让模型填 status：这是算术。让它拿今天的日期去比记录的日期，
    它会算错，而且错了不报错（5.6 的时区推论，同一个道理）。

    直接比字符串就够，因为时间戳是 ISO 8601 UTC —— **按字典序排就是按
    时间排**，跨年也成立（"2025-12-31T23:00:00Z" < "2026-01-01T01:00:00Z"）。
    当初选这个格式图的就是这个。
    """
    return "planned" if ts > now() else "done"


def log_event(category: str, kind: str, name: str, note: str | None = None,
              ts: str | None = None, place: str | None = None,
              ts_precision: str = "exact") -> int:
    """记一件事。ts 不传就是现在（于是 status=done）。

    两级分类都走受控词表，非法值直接抛 ValueError —— 校验必须在 INSERT
    之前，否则会留下半条脏数据。

    ts_precision 说的是"ts 这个时间有多准"：说好了周五九点是 exact，
    只说"周末"是 week，这时 ts 填那个区间的**起点**（周六 00:00）。
    见 TS_PRECISIONS 的注释。
    """
    category = _one_of(category, CATEGORIES, "category")
    kind = _one_of(kind, KINDS, "kind")
    ts_precision = _one_of(ts_precision, TS_PRECISIONS, "ts_precision")
    ts = ts or now()
    status = _status_for(ts)
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO logs (category, kind, name, ts, created_at, note, place,"
            " status, ts_precision) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (category, kind, name, ts, now(), note, place, status, ts_precision),
        )
    record_event("cli", "log_event", {
        "category": category, "kind": kind, "name": name,
        "note": note, "place": place, "ts": ts, "status": status,
        "ts_precision": ts_precision,
    })
    return cur.lastrowid


def log_meal(name: str, note: str | None = None, ts: str | None = None,
             place: str | None = None) -> int:
    """记一顿饭 —— log_event 的快捷方式，log_meal.py 在用。"""
    return log_event("personal", "meal", name, note=note, ts=ts, place=place)


def correct_log(row_id: int, name: str | None = None, note: str | None = None,
                ts: str | None = None, place: str | None = None,
                category: str | None = None, status: str | None = None,
                ts_precision: str | None = None) -> bool:
    """更正一条记录。id 不存在时返回 False。ts 传 UTC ISO。

    ⚠️ **改 ts 不会自动重算 status。** 用户说"那件事其实是上周做的"，
    ts 改到过去之后 status 仍然是 planned，于是显示成"早就过了，还没确认"，
    模型会问一句"那做了没"。看着绕，但自动重算更糟 —— 它会静默覆盖
    用户刚刚显式设过的状态。状态只有人开口才变，这是唯一解释得清的规则。

    logs 用真改、不留旧版本：memories 追加不覆盖是因为"我曾经这么以为"
    本身有价值；而 logs 里一条抽错的记录不是历史，它从来就不是真的。
    留痕交给 events —— 旧值进流水账，事后能查出改过什么。

    ts 也能改：模型现在可以自己填时间了（llm.py 的 log_meal 有 ts 参数），
    那它就会把时间填错。凡是模型写得进去的字段都必须改得回来，
    否则等于开了一个只写不可救的坑（设计文档 5.2）。
    """
    # 校验放在读记录之前：非法值不该走到 UPDATE 那一步
    if category is not None:
        category = _one_of(category, CATEGORIES, "category")
    if status is not None:
        status = _one_of(status, STATUSES, "status")
    if ts_precision is not None:
        ts_precision = _one_of(ts_precision, TS_PRECISIONS, "ts_precision")

    fields = ("name", "note", "ts", "place", "category", "status", "ts_precision")
    given = {"name": name, "note": note, "ts": ts, "place": place,
             "category": category, "status": status, "ts_precision": ts_precision}

    with connect() as conn:
        old = conn.execute("SELECT * FROM logs WHERE id = ?", (row_id,)).fetchone()
        if old is None:
            return False
        new = {f: (given[f] if given[f] is not None else old[f]) for f in fields}
        conn.execute(
            "UPDATE logs SET name = ?, note = ?, ts = ?, place = ?,"
            " category = ?, status = ?, ts_precision = ? WHERE id = ?",
            (*(new[f] for f in fields), row_id))

    record_event("cli", "correct_log", {
        "id": row_id,
        "old": {f: old[f] for f in fields},
        "new": new,
    })
    return True


def append_note(row_id: int, text: str) -> bool:
    """在已有备注后面接一段，**不覆盖**。id 不存在返回 False。

    为什么不能用 correct_log 干这件事：那是**更正**，替换语义 ——
    "我记错了，其实是周三" 就该把周二冲掉。而这里是**补充**：
    review 时你说"那场球人挺多"，原来那句"老王叫的"没有错，不该消失。

    一个工具装两种语义，模型只能靠语气猜该保留还是该覆盖，
    而且猜错了不报错 —— 你要等到几个月后翻记录，才发现当初记的细节
    被自己的一句评价洗掉了。所以分成两个函数，各自的语义写死在名字里。

    ⚠️ 这个操作不进 ui.confirm：追加不毁数据，而确认框的意义是拦住
    不可逆的动作（5.7）。每加一句评价都弹一次框，你很快就不肯用 review 了。
    """
    with connect() as conn:
        row = conn.execute("SELECT note FROM logs WHERE id = ?", (row_id,)).fetchone()
        if row is None:
            return False
        old = (row["note"] or "").strip()
        new = f"{old}；{text.strip()}" if old else text.strip()
        conn.execute("UPDATE logs SET note = ? WHERE id = ?", (new, row_id))

    record_event("cli", "append_note", {"id": row_id, "old": old, "new": new})
    return True


def delete_log(row_id: int) -> sqlite3.Row | None:
    """删掉一条记录，返回被删的内容；id 不存在返回 None。

    ⚠️ 这个函数不给模型用，只给 CLI（见 llm.py 的 TOOLS）。
    删除不可逆、更正可逆，风险差一档，所以这个权限留在人手里。

    被删内容会进 events，所以真删错了还能从流水账里读回来重插。
    """
    with connect() as conn:
        row = conn.execute("SELECT * FROM logs WHERE id = ?", (row_id,)).fetchone()
        if row is None:
            return None
        conn.execute("DELETE FROM logs WHERE id = ?", (row_id,))

    record_event("cli", "delete_log", {
        "id": row_id, "category": row["category"], "kind": row["kind"],
        "name": row["name"], "note": row["note"], "ts": row["ts"],
        "place": row["place"], "status": row["status"],
        # 这一条不能漏：events 是"删错了还能重插"的唯一依据，
        # 少记一个字段，重插出来的就是另一条记录了
        "ts_precision": row["ts_precision"],
    })
    return row


def remember(category: str, key: str, value: str,
             source: str = "manual", expires_at: str | None = None) -> int:
    """记一条偏好/约束/事实。

    同一个 key 再写一次不会覆盖旧的，而是追加新版本 —— 读取时自然取到最新的。
    这样"它为什么以为我爱吃日料"永远查得出来。

    ⚠️ 所以记忆不需要 undo / delete，别再加。记错了就再 remember 一次：
    新版本自动生效，错的那条留着当历史。追加不覆盖的设计已经把"改"
    这件事解决了（设计文档 5.4）。
    """
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO memories (category, key, value, source, created_at, expires_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (category, key, value, source, now(), expires_at),
        )
        return cur.lastrowid


# ---------- 窄接口：读 ----------

# 一次检索最多回多少条。这是熔断，不是筛选（文档 12.1）——
# _fmt_log 实测 29 token/条，60 条约 1.7K，塞进 64K 的上下文里安全。
# 比 find_places 的 40 放宽，是因为"有没有谁老放我鸽子"这类聚合题
# 需要看到全部的约（实测那次是 26 条）。
FIND_LIMIT = 60


def find_logs(kind: str | None = None, keyword: str | None = None,
              days: int | None = None,
              since: str | None = None, until: str | None = None,
              category: str | None = None, place: str | None = None,
              status: str | None = None,
              limit: int = FIND_LIMIT) -> tuple[list[sqlite3.Row], int]:
    """按条件找记录，返回 (最多 limit 行, 符合条件的真实总数)。

    行带 id —— 更正和删除都要靠 id 定位。为什么不直接按名字改删：
    名字会重复。"牛肉面"你可能吃过三次，按名字动手会一次改掉三条。
    所以这些条件只负责「找到」，真正动手的依据永远是 id。

    ⚠️ **总数必须跟着行一起返回。** 只截断不告诉调用方，模型就会拿着
    一个残缺的集合下结论 —— 那正是实测过的"把有的说成没有"
    （查到一条就回答"你只跟他出去过一次"，其实有五条）。

    时间有三种问法：
      days              —— "最近几天"，单边下界，粗筛用
      since / until     —— 一个区间，"今天中午"这种具体时段用（UTC ISO）
      都不给            —— 见下面 days 的默认值规则

    给了 since 或 until 就按区间走，days 作废。

    **days 不传时的默认值取决于有没有关键词**，这是刻意的：

      给了 keyword 或 place  →  不限时间，搜全库
      两个都没给             →  最近 30 天

    因为**关键词本身就是筛子**。"小明这人靠谱吗"要看的是这辈子，
    不是最近三十天 —— 实测模型调的就是 find_logs(keyword=小明) 不带 days，
    现在数据都是新的所以全中，一年后会漏掉三十天以前的全部，
    而且照样给出一个自信的结论。会失控的是**没有关键词**的全量查询，
    那种才需要时间窗兜着。

    搜全库不心疼：实测 5 万行三列 LIKE 只要 6–26 毫秒，全表扫描
    十年内都不是瓶颈。贵的从来不是查，是回填进 prompt 的条数，
    而那个由 limit 兜住了。

    ⚠️ 这条规则必须写在代码里当默认值，不能写成提示词让模型"记得传
    days=3650" —— 提示词不是闸门（文档 6.1）。

    ⚠️ **days 只是下界**（ts >= days_ago(n)），没有上界，所以将来的事
    永远包含在结果里。这是刻意的：计划中的事就该出现在"最近"里，
    否则问"最近有什么"会漏掉明天的考试。别当 bug 改掉。
    要排除未来的，用 until，或者 status="done"。

    keyword 同时搜 name / note / place 三列 —— 找"跟老王""排队""泾县"
    这种散落在自由文本里的东西全靠它。place 参数是另一回事，
    它用双向包含专门给高德的店名对齐用，见下面的注释。
    """
    if days is None and since is None and until is None:
        narrowed = bool((keyword or "").strip()) or bool((place or "").strip())
        days = None if narrowed else 30
    # 区间的值来自模型，必须校验。ts 是 TEXT 列，格式不对的字符串
    # 比较不会报错，只会安静地给出错误结果 —— 那种 bug 最难查。
    for label, v in (("since", since), ("until", until)):
        if v is not None:
            try:
                datetime.strptime(v, TS_FMT)
            except ValueError:
                raise ValueError(f"{label} 要 UTC ISO 格式（{TS_FMT}），收到的是：{v!r}")

    # 注意：这里的 f-string 只拼固定的条件片段，所有「值」仍然走 ? 参数。
    # 千万别把 name 之类的内容直接拼进 SQL 字符串里。
    where: list[str] = []
    params: list = []
    if since is None and until is None:
        if days is not None:            # days=None 就是"不限时间，搜全库"
            where.append("ts >= ?")
            params.append(days_ago(days))
    else:
        if since is not None:
            where.append("ts >= ?")
            params.append(since)
        if until is not None:
            where.append("ts <= ?")
            params.append(until)
    if kind:
        where.append("kind = ?")
        params.append(kind)
    if category:
        where.append("category = ?")
        params.append(category)
    if status:
        where.append("status = ?")
        params.append(status)
    if keyword and keyword.strip():
        # 三列一起搜，因为**人名几乎只存在于 note 里**。
        # 实测：库里四条跟老王有关的记录，name 列写的是"健身房""打羽毛球"
        # "徒步"，只有一条含"老王"。当初这个参数只搜 name，于是
        # find_logs(name=老王) 返回一条，模型就回答"你只跟他出去过一次"——
        # 把有的说成没有，比编造更难发现，因为它听起来很守规矩。
        #
        # 参数从 name 改名成 keyword 是必须的：它现在搜三列，
        # 还叫 name 就是在撒谎，而撒谎的参数名会让下一个人（和模型）用错。
        kw = f"%{keyword.strip()}%"
        where.append("(name LIKE ? OR note LIKE ? OR place LIKE ?)")
        params += [kw, kw, kw]
    if place and place.strip():
        # ⚠️ 双向包含，因为两边的名字粒度对不上：库里存的是你随口说的
        # 「随园餐厅」，高德给的是「随园餐厅(仙鹤门店)」。
        # 拿长名字来查要匹配库里的短名字，拿短名字来查要匹配库里的长名字。
        #
        # 前面那个 place != '' 不能省：库里大量记录 place 是空的
        # （在家吃饭没有店名），少了它 'X' LIKE '%%' 恒真，一查就全中。
        where.append("place IS NOT NULL AND place != ''"
                     " AND (? LIKE '%' || place || '%' OR place LIKE ?)")
        params += [place.strip(), f"%{place.strip()}%"]

    # 一个条件都没有时 where 是空的，' AND '.join([]) 会拼出
    # "WHERE " 这种语法错误。用恒真兜住，别让它变成一个偶发的崩溃。
    cond = " AND ".join(where) if where else "1=1"

    with connect() as conn:
        # 总数单独查一次。SELECT * 之后在 Python 里数长度是不行的 ——
        # 那样数到的是截断后的条数，等于自己骗自己。
        total = conn.execute(
            f"SELECT count(*) FROM logs WHERE {cond}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM logs WHERE {cond} ORDER BY ts DESC LIMIT ?",
            [*params, limit],
        ).fetchall()
    return rows, total


def get_log(row_id: int) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute("SELECT * FROM logs WHERE id = ?", (row_id,)).fetchone()


def place_history(place: str) -> list[sqlite3.Row]:
    """这个地方的全部记录，新的在前。没有就是空列表。

    这是整个推荐功能的支点。高德能告诉你附近有什么，但"这家你去过两次、
    上次说排队久但值"只有这张表有 —— 设计文档 7.5 说的、赢得过美团的
    唯一原因就是这一条。

    只是 find_logs 的一个壳子 —— 双向包含的匹配规则**只该有一份**，
    散成两处早晚会不一致。不传 days：给了 place 就自动搜全库，
    因为"我去过这家没有"要看一辈子，不是看最近三十天。

    这里丢掉总数是刻意的：调用方（_been_there / history_note）拿它拼
    "你去过N次"，而一家店去过六十次以上还要精确计数，那个需求不存在。

    匹配不上就返回空，调用方必须如实说"没有记录"，不许猜（文档 7.4）。
    """
    if not (place or "").strip():
        return []
    rows, _ = find_logs(place=place)
    return rows


def active_memories() -> list[sqlite3.Row]:
    """当前有效的记忆：每个 key 只取最新的一条，且跳过已过期的。

    设计文档 5.3：这批东西不多（几十条），直接全塞进 system prompt
    比任何检索都准，而且可解释。
    """
    with connect() as conn:
        return conn.execute(
            """
            SELECT * FROM memories m
            WHERE m.id = (
                -- 用 id 兜底，不能只按 created_at：同一秒写入的两个版本会并列，
                -- 结果就是同一个 key 冒出两条互相矛盾的记忆。
                SELECT id FROM memories WHERE key = m.key
                ORDER BY created_at DESC, id DESC LIMIT 1
            )
            AND (expires_at IS NULL OR expires_at > ?)
            ORDER BY category, key
            """,
            (now(),),
        ).fetchall()


def memory_history(key: str) -> list[sqlite3.Row]:
    """一个 key 的全部历史版本 —— 当它说出莫名其妙的话时用这个查。"""
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM memories WHERE key = ? ORDER BY created_at DESC", (key,)
        ).fetchall()


# ---------- 窄接口：review 要的两批数据 ----------

def overdue_logs(ts: str | None = None) -> list[sqlite3.Row]:
    """标了计划、时间已经过了、还没确认做没做的事。

    这是 review 的主料。库里现在就躺着八条 —— 除非你自己想起来问，
    否则它们永远不会被处理，这正是"没有主动性"的具体代价。

    ⚠️ **只要 ts_precision='exact'。** "周末跟家人聚餐"那种 ts 是瞎填的
    区间起点，周一就会显示成"过期未确认"，可那件事的时间从来没定过 ——
    拿它去问"做了没"是纯误报。没定死时间的事归 review 的另一种形态管
    （weekly / monthly，见文档 6.5），不归这里。
    """
    ts = ts or now()
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM logs WHERE status = 'planned' AND ts < ?"
            " AND ts_precision = 'exact' ORDER BY ts DESC",
            (ts,),
        ).fetchall()


def upcoming_logs(days: int = 1, ts: str | None = None) -> list[sqlite3.Row]:
    """从现在到 days 天后，还没做的事。review 顺带报一句"明天有什么"。

    这里**不限 ts_precision** —— 跟 overdue_logs 相反，是刻意的：
    "这周末大概要跟家人聚餐"提前说一句是有用的，说错了代价也只是一句废话；
    而拿它去问"做了没"是在逼你回答一个没有答案的问题。
    同一列在两个方向上的容错度本来就不一样。
    """
    ts = ts or now()
    end = (datetime.strptime(ts, TS_FMT) + timedelta(days=days)).strftime(TS_FMT)
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM logs WHERE status = 'planned' AND ts >= ? AND ts <= ?"
            " ORDER BY ts",
            (ts, end),
        ).fetchall()


def kind_counts() -> dict[str, int]:
    """每个 kind 在库里有多少条。review 拿它当排序权重。

    这是"这件事值不值得问一句"的全部实现 —— 天天干的事（吃饭、健身）
    条数多，半年一次的事（考试、独自出游）条数少，越少越该问。

    为什么是排序权重而不是二元判断：**排错序只是顺序不理想，判错了是漏报。**
    而且它自适应 —— 你的习惯变了，频次自动跟着变，不像写死在行里的评分。
    """
    with connect() as conn:
        return {r["kind"]: r["n"] for r in conn.execute(
            "SELECT kind, count(*) AS n FROM logs GROUP BY kind")}


def total_logs() -> int:
    """库里一共多少条。review 用它判断频次信号够不够可信（冷启动门槛）。"""
    with connect() as conn:
        return conn.execute("SELECT count(*) FROM logs").fetchone()[0]


# ---------- 窄接口：nudges ----------

def propose_nudge(kind: str, payload: str, dedupe_key: str,
                  urgency: int = 1, expires_ts: str | None = None) -> int | None:
    """登记一条主动发言的提案。**已经有同 dedupe_key 的就什么都不做，返回 None。**

    ⚠️ 幂等交给数据库的 UNIQUE 约束，不在 Python 里先查后插 ——
    那中间有竞态，而且以后加了定时器就是两个进程在写同一张表。
    `INSERT OR IGNORE` 是一句话的事，先查后插是一个将来才会炸的 bug。

    dedupe_key 的形状：`review:daily:2026-08-14`（**本地**日期，见 local_day）。
    设计文档 6.2 的 schema 里没有这一列，是实现时发现的缺口 ——
    场景规则会被反复调用（你一晚上开三次 chat.py），没有它同一件事会入库三条。
    """
    with connect() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO nudges (kind, urgency, payload, dedupe_key,"
            " created_at, expires_ts) VALUES (?, ?, ?, ?, ?, ?)",
            (kind, urgency, payload, dedupe_key, now(), expires_ts),
        )
        if cur.rowcount == 0:        # 撞了 dedupe_key，这次不算新提案
            return None
    record_event("cli", "nudge_proposed",
                 {"kind": kind, "dedupe_key": dedupe_key, "urgency": urgency})
    return cur.lastrowid


def pending_nudges() -> list[sqlite3.Row]:
    """还没发也没被丢弃的提案，急的在前。"""
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM nudges WHERE fired_at IS NULL AND dropped_reason IS NULL"
            " ORDER BY urgency DESC, created_at",
        ).fetchall()


def fire_nudge(nudge_id: int, ts: str | None = None) -> None:
    """标记这条已经播出去了。outcome 先留空，等你的反应。

    ts 可传，理由跟 gate 那边一样：不给这个参数，自检就没法构造
    "跨了 UTC 日但没跨本地日"这种场景，而那正是最容易错的地方。
    """
    with connect() as conn:
        conn.execute("UPDATE nudges SET fired_at = ? WHERE id = ?",
                     (ts or now(), nudge_id))
    record_event("cli", "nudge_fired", {"id": nudge_id})


def drop_nudge(nudge_id: int, reason: str) -> None:
    """闸门拦下来了。**被拦的原因必须落库** —— 见 nudges 表的注释：
    "它为什么没提醒我"才是难查的那个方向。"""
    reason = _one_of(reason, DROP_REASONS, "dropped_reason")
    with connect() as conn:
        conn.execute("UPDATE nudges SET dropped_reason = ? WHERE id = ?",
                     (reason, nudge_id))
    record_event("cli", "nudge_dropped", {"id": nudge_id, "reason": reason})


def set_outcome(nudge_id: int, outcome: str) -> bool:
    """回写你的反应。id 不存在返回 False。

    这是全系统唯一一个"它做得对不对"的真实标签（6.4）。跑一个月后
    SELECT kind, outcome, count(*) FROM nudges GROUP BY 1,2 会直接
    告诉你哪些场景该砍，比任何主观感受都准。
    """
    outcome = _one_of(outcome, OUTCOMES, "outcome")
    with connect() as conn:
        cur = conn.execute("UPDATE nudges SET outcome = ? WHERE id = ?",
                           (outcome, nudge_id))
        if cur.rowcount == 0:
            return False
    record_event("cli", "nudge_outcome", {"id": nudge_id, "outcome": outcome})
    return True


def fired_on(day: str, ts: str | None = None) -> list[sqlite3.Row]:
    """某个**本地日**发出去的全部 nudge。每日预算靠它数数。

    ⚠️ 不能写成 `WHERE fired_at LIKE '2026-08-14%'` —— fired_at 是 UTC，
    东八区早上八点之前 UTC 还停在昨天，那样数出来的"今天"每天早上都是错的，
    而且不报错。所以取出来在 Python 里用 local_day 逐条判。
    数量级是每天几条，这点开销无所谓。
    """
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM nudges WHERE fired_at IS NOT NULL ORDER BY fired_at"
        ).fetchall()
    return [r for r in rows if local_day(r["fired_at"]) == day]


def last_fired(kind: str) -> str | None:
    """这个 kind 上一次发出去是什么时候（UTC）。同类冷却靠它。"""
    with connect() as conn:
        row = conn.execute(
            "SELECT fired_at FROM nudges WHERE kind = ? AND fired_at IS NOT NULL"
            " ORDER BY fired_at DESC LIMIT 1", (kind,)).fetchone()
    return row["fired_at"] if row else None


def recent_nudges(days: int = 7) -> list[sqlite3.Row]:
    """最近的提案，新的在前。nudges.py 拿它给你看"它本来会说什么"。"""
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM nudges WHERE created_at >= ? ORDER BY created_at DESC",
            (days_ago(days),)).fetchall()


def get_nudge(nudge_id: int) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute("SELECT * FROM nudges WHERE id = ?",
                            (nudge_id,)).fetchone()
