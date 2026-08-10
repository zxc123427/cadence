"""cadence 的数据层：所有读写都从这里走。

三条纪律（设计文档 5.6）：
  1. 追加不覆盖 —— 记忆会过时，但旧版本要留着，才查得出"它为什么这么以为"
  2. 时间戳一律 UTC + ISO 8601 —— 只在显示给人看的时候才转本地时区
  3. 别过早拆表 —— 宽表 + 一个 JSON 字段放杂项，跑半年再重构

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

-- 客观记录：吃了什么、几点起的、坐了多久。宽表，靠 kind 区分。
CREATE TABLE IF NOT EXISTS logs (
    id         INTEGER PRIMARY KEY,
    kind       TEXT NOT NULL,           -- meal / wake / sit ...
    name       TEXT,                    -- "牛肉面"
    ts         TEXT NOT NULL,           -- 事情发生的时间（UTC）
    created_at TEXT NOT NULL,           -- 写进库的时间（UTC），两者可以不同
    note       TEXT,
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


# ---------- 时间：存 UTC，显示本地 ----------

def now() -> str:
    """当前时间，UTC ISO 8601。所有写库的时间戳都用它。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def days_ago(n: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=n)).strftime("%Y-%m-%dT%H:%M:%SZ")


def to_local(ts: str) -> str:
    """UTC 时间戳 -> 本地时间的可读字符串。只用于显示，不要写回库里。"""
    dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return dt.astimezone().strftime("%m-%d %H:%M")


# ---------- 连接 ----------

def connect() -> sqlite3.Connection:
    """打开数据库，第一次会自动建表。"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row          # 让查询结果能用列名访问：row["name"]
    conn.executescript(SCHEMA)
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


def log_meal(name: str, note: str | None = None, ts: str | None = None) -> int:
    """记一顿饭。ts 不传就是现在。"""
    ts = ts or now()
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO logs (kind, name, ts, created_at, note) VALUES ('meal', ?, ?, ?, ?)",
            (name, ts, now(), note),
        )
    record_event("cli", "log_meal", {"name": name, "note": note})
    return cur.lastrowid


def correct_log(row_id: int, name: str | None = None, note: str | None = None) -> bool:
    """更正一条记录。id 不存在时返回 False。

    logs 用真改、不留旧版本：memories 追加不覆盖是因为"我曾经这么以为"
    本身有价值；而 logs 里一条抽错的记录不是历史，它从来就不是真的。
    留痕交给 events —— 旧值进流水账，事后能查出改过什么。
    """
    with connect() as conn:
        old = conn.execute("SELECT * FROM logs WHERE id = ?", (row_id,)).fetchone()
        if old is None:
            return False
        new_name = name if name is not None else old["name"]
        new_note = note if note is not None else old["note"]
        conn.execute("UPDATE logs SET name = ?, note = ? WHERE id = ?",
                     (new_name, new_note, row_id))

    record_event("cli", "correct_log", {
        "id": row_id,
        "old": {"name": old["name"], "note": old["note"]},
        "new": {"name": new_name, "note": new_note},
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
        "id": row_id, "kind": row["kind"], "name": row["name"],
        "note": row["note"], "ts": row["ts"],
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

def find_logs(kind: str | None = None, name: str | None = None,
              days: int = 30) -> list[sqlite3.Row]:
    """按条件找记录。返回的行带 id —— 更正和删除都要靠 id 定位。

    为什么不直接按名字改删：名字会重复。"牛肉面"你可能吃过三次，
    按名字动手会一次改掉三条。所以 kind / name / 时间只负责「找到」，
    真正动手的依据永远是 id。
    """
    # 注意：这里的 f-string 只拼固定的条件片段，所有「值」仍然走 ? 参数。
    # 千万别把 name 之类的内容直接拼进 SQL 字符串里。
    where = ["ts >= ?"]
    params: list = [days_ago(days)]
    if kind:
        where.append("kind = ?")
        params.append(kind)
    if name:
        where.append("name LIKE ?")
        params.append(f"%{name}%")

    with connect() as conn:
        return conn.execute(
            f"SELECT * FROM logs WHERE {' AND '.join(where)} ORDER BY ts DESC",
            params,
        ).fetchall()


def recent_meals(days: int = 7) -> list[sqlite3.Row]:
    """find_logs 的薄封装，meals.py 在用。"""
    return find_logs(kind="meal", days=days)


def get_log(row_id: int) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute("SELECT * FROM logs WHERE id = ?", (row_id,)).fetchone()


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
