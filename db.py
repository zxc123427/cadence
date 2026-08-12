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

# 起步三张表。conversations（对话摘要）和 nudges（主动发言）等到真需要时再加，
# 见设计文档 5.5 和 6.2 —— 但它们都会放在这同一个库里。
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
              ts: str | None = None, place: str | None = None) -> int:
    """记一件事。ts 不传就是现在（于是 status=done）。

    两级分类都走受控词表，非法值直接抛 ValueError —— 校验必须在 INSERT
    之前，否则会留下半条脏数据。
    """
    category = _one_of(category, CATEGORIES, "category")
    kind = _one_of(kind, KINDS, "kind")
    ts = ts or now()
    status = _status_for(ts)
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO logs (category, kind, name, ts, created_at, note, place, status)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (category, kind, name, ts, now(), note, place, status),
        )
    record_event("cli", "log_event", {
        "category": category, "kind": kind, "name": name,
        "note": note, "place": place, "ts": ts, "status": status,
    })
    return cur.lastrowid


def log_meal(name: str, note: str | None = None, ts: str | None = None,
             place: str | None = None) -> int:
    """记一顿饭 —— log_event 的快捷方式，log_meal.py 在用。"""
    return log_event("personal", "meal", name, note=note, ts=ts, place=place)


def correct_log(row_id: int, name: str | None = None, note: str | None = None,
                ts: str | None = None, place: str | None = None,
                category: str | None = None, status: str | None = None) -> bool:
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

    fields = ("name", "note", "ts", "place", "category", "status")
    given = {"name": name, "note": note, "ts": ts, "place": place,
             "category": category, "status": status}

    with connect() as conn:
        old = conn.execute("SELECT * FROM logs WHERE id = ?", (row_id,)).fetchone()
        if old is None:
            return False
        new = {f: (given[f] if given[f] is not None else old[f]) for f in fields}
        conn.execute(
            "UPDATE logs SET name = ?, note = ?, ts = ?, place = ?,"
            " category = ?, status = ? WHERE id = ?",
            (*(new[f] for f in fields), row_id))

    record_event("cli", "correct_log", {
        "id": row_id,
        "old": {f: old[f] for f in fields},
        "new": new,
    })
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

def find_logs(kind: str | None = None, keyword: str | None = None, days: int = 30,
              since: str | None = None, until: str | None = None,
              category: str | None = None, place: str | None = None,
              status: str | None = None) -> list[sqlite3.Row]:
    """按条件找记录。返回的行带 id —— 更正和删除都要靠 id 定位。

    为什么不直接按名字改删：名字会重复。"牛肉面"你可能吃过三次，
    按名字动手会一次改掉三条。所以这些条件只负责「找到」，
    真正动手的依据永远是 id。

    时间有两种问法，共用同一个查询：
      days              —— "最近几天"，单边下界，粗筛用
      since / until     —— 一个区间，"今天中午"这种具体时段用（UTC ISO）
    给了 since 或 until 就按区间走，days 作废。两个都不给才回到 days。

    ⚠️ **days 只是下界**（ts >= days_ago(n)），没有上界，所以将来的事
    永远包含在结果里。这是刻意的：计划中的事就该出现在"最近"里，
    否则问"最近有什么"会漏掉明天的考试。别当 bug 改掉。
    要排除未来的，用 until，或者 status="done"。

    keyword 同时搜 name / note / place 三列 —— 找"跟老王""排队""泾县"
    这种散落在自由文本里的东西全靠它。place 参数是另一回事，
    它用双向包含专门给高德的店名对齐用，见下面的注释。
    """
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

    with connect() as conn:
        return conn.execute(
            f"SELECT * FROM logs WHERE {' AND '.join(where)} ORDER BY ts DESC",
            params,
        ).fetchall()


def get_log(row_id: int) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute("SELECT * FROM logs WHERE id = ?", (row_id,)).fetchone()


def place_history(place: str) -> list[sqlite3.Row]:
    """这个地方的全部记录，新的在前。没有就是空列表。

    这是整个推荐功能的支点。高德能告诉你附近有什么，但"这家你去过两次、
    上次说排队久但值"只有这张表有 —— 设计文档 7.5 说的、赢得过美团的
    唯一原因就是这一条。

    只是 find_logs 的一个壳子 —— 双向包含的匹配规则**只该有一份**，
    散成两处早晚会不一致。days 给一个大数，因为"我去过这家没有"
    要看一辈子，不是看最近三十天。

    匹配不上就返回空，调用方必须如实说"没有记录"，不许猜（文档 7.4）。
    """
    if not (place or "").strip():
        return []
    return find_logs(place=place, days=3650)


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
