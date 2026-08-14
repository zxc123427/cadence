#!/usr/bin/env python3
"""自检：把 db.py 里那些"错了不报错"的逻辑挨个断言一遍。

    python3 check.py

不用 pytest（文档 3.4：单机单用户，框架只多花时间）。就是一串 assert，
全绿才算数，红了会直接打出哪一条断言崩的。

**它跑在一个临时库上，绝不碰 cadence.db。**

为什么值得写：迁移、词表校验、status 推导、检索条件拼接，这四样全是
"出错了不会报错，几天后才发现"的类型 —— 库里静静躺着一批 kind='' 的行，
或者一次查询悄悄多返回了六条。肉眼跑一遍对话根本盖不住。
"""

import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import db

# ⚠️ 必须在任何 connect() 之前改掉。connect() 每次都从模块全局读 DB_PATH，
# 所以改这一行就够，不用改 db.py。
db.DB_PATH = Path(tempfile.mkdtemp(prefix="cadence-check-")) / "check.db"

# 改动前的 logs 建表语句。用它造一个"老库"，才能测到真实的升级路径 ——
# 直接跑 connect() 测的是新建路径，那条路永远是对的，测了等于没测。
OLD_LOGS_SCHEMA = """
CREATE TABLE logs (
    id         INTEGER PRIMARY KEY,
    kind       TEXT NOT NULL,
    name       TEXT,
    ts         TEXT NOT NULL,
    created_at TEXT NOT NULL,
    note       TEXT,
    place      TEXT,
    extra      TEXT
);
"""

passed = 0


def ok(label: str, cond: bool, detail: str = "") -> None:
    global passed
    if not cond:
        print(f"\n✗ {label}")
        if detail:
            print(f"  {detail}")
        sys.exit(1)
    passed += 1
    print(f"  ✓ {label}")


def raises(label: str, fn, *args, **kwargs) -> None:
    """断言 fn 抛 ValueError。抛别的或者不抛都算失败。"""
    try:
        fn(*args, **kwargs)
    except ValueError:
        return ok(label, True)
    except Exception as e:                                  # noqa: BLE001
        return ok(label, False, f"抛的是 {type(e).__name__}，不是 ValueError：{e}")
    ok(label, False, "没抛异常")


def columns(path: Path, table: str = "logs") -> dict:
    """PRAGMA table_info 转成 {列名: (类型, 非空, 默认值)}。

    用字典而不是列表：新建库里 category 是第 2 列，升级库里它排在最后 ——
    顺序本来就不一样，比顺序会误报。要比的是每一列长得对不对。
    """
    conn = sqlite3.connect(path)
    try:
        return {r[1]: (r[2], r[3], r[4])
                for r in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


def fresh_db() -> None:
    """删掉临时库，让下一次 connect() 走新建路径。"""
    db.DB_PATH.unlink(missing_ok=True)


def utc(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) + delta).strftime(db.TS_FMT)


# ---------- 1. 迁移 ----------

print("\n迁移")

# 造一个改动前的老库，灌 3 行
fresh_db()
conn = sqlite3.connect(db.DB_PATH)
conn.executescript(OLD_LOGS_SCHEMA)
OLD_ROWS = [
    ("meal", "牛肉面", "2026-08-10T15:29:20Z", "2026-08-10T15:29:20Z", "有点咸", None),
    ("meal", "梅干菜烧肉", "2026-08-10T16:53:24Z", "2026-08-10T16:53:24Z", "复热烧糊了", None),
    ("meal", "烤羊腿", "2026-08-11T09:06:50Z", "2026-08-11T09:06:50Z", None, "随园餐厅"),
]
conn.executemany(
    "INSERT INTO logs (kind, name, ts, created_at, note, place) VALUES (?,?,?,?,?,?)",
    OLD_ROWS)
conn.commit()
before = conn.execute("SELECT id,kind,name,ts,created_at,note,place FROM logs"
                      " ORDER BY id").fetchall()
conn.close()

db.connect().close()                                        # ← 真正的升级发生在这里

after, _ = db.find_logs(days=3650)
ok("老库升级后行数不变", len(after) == 3, f"变成了 {len(after)} 行")

by_id = {r["id"]: r for r in after}
same = all(
    by_id[old[0]]["kind"] == old[1] and by_id[old[0]]["name"] == old[2]
    and by_id[old[0]]["ts"] == old[3] and by_id[old[0]]["created_at"] == old[4]
    and by_id[old[0]]["note"] == old[5] and by_id[old[0]]["place"] == old[6]
    for old in before)
ok("老数据逐字段没被改动", same)

ok("老行的 status 回填成 done", all(r["status"] == "done" for r in after))
ok("老行的 category 回填成 personal", all(r["category"] == "personal" for r in after))
# 回填成 exact 是对的：ts_precision 这一列出现之前记的，本来就都是具体时间
ok("老行的 ts_precision 回填成 exact", all(r["ts_precision"] == "exact" for r in after))

cols_migrated = columns(db.DB_PATH)
db.connect().close()                                        # 再跑一次
ok("_migrate 幂等（连跑两次列不变）", columns(db.DB_PATH) == cols_migrated)
ok("连跑两次数据不变", db.find_logs(days=3650)[1] == 3)

# 新建库 vs 升级库：两条路必须造出一模一样的表。
# SCHEMA 和 NEW_COLUMNS 是两处写法，漂了就会出现"新装的机器和老机器不一样"，
# 而这种差异要等到你换电脑那天才暴露。
migrated_path = db.DB_PATH
nudges_migrated = columns(db.DB_PATH, "nudges")
db.DB_PATH = migrated_path.parent / "brandnew.db"
db.connect().close()
ok("新建库和升级库的表结构完全一致", columns(db.DB_PATH) == cols_migrated,
   f"新建 {columns(db.DB_PATH)}\n  升级 {cols_migrated}")
# nudges 是这一轮新加的表。老库里它由 CREATE TABLE IF NOT EXISTS 建出来，
# 走的跟新库同一条路 —— 但还是要比一次，否则哪天有人给它加列走了 NEW_COLUMNS，
# 两条路就会静静地漂开。
ok("nudges 表在新建库和升级库里也一致",
   columns(db.DB_PATH, "nudges") == nudges_migrated)
# index_list 每行是 (seq, name, unique, origin, partial)，第 3 个才是唯一性
ok("nudges.dedupe_key 建了唯一约束", any(
    r[2] == 1 for r in sqlite3.connect(db.DB_PATH).execute("PRAGMA index_list(nudges)")))
db.DB_PATH = migrated_path


# ---------- 2. 受控词表 ----------

print("\n受控词表")

ok("两级取值不重叠", not set(db.KINDS) & set(db.CATEGORIES))

# 用户点名最担心的那件事：两级填反了
raises("两级填反当场报错（category=meal, kind=appointment）",
       db.log_event, "meal", "appointment", "填反了")

raises("造新值报错（kind=exam）", db.log_event, "personal", "exam", "期末考试")
raises("空 kind 报错", db.log_event, "personal", "", "x")
raises("None kind 报错", db.log_event, "personal", None, "x")
raises("空 category 报错", db.log_event, "", "meal", "x")

n_before = db.find_logs(days=3650)[1]
try:
    db.log_event("personal", "exam", "不该落库")
except ValueError:
    pass
ok("非法值不留半条脏数据", db.find_logs(days=3650)[1] == n_before)

rid = db.log_event("PERSONAL", " Meal ", "大小写和空格")
row = db.get_log(rid)
ok("归一化：'PERSONAL'/' Meal ' → personal/meal",
   (row["category"], row["kind"]) == ("personal", "meal"),
   f"实际是 {row['category']}/{row['kind']}")

raises("correct_log 也校验 status", db.correct_log, rid, status="做完了")
db.correct_log(rid, status="DONE")
ok("correct_log 的 status 也归一化", db.get_log(rid)["status"] == "done")


# ---------- 3. status 推导 ----------

print("\nstatus")

ok("ISO 8601 按字典序排就是按时间排（跨年也成立）",
   "2025-12-31T23:00:00Z" < "2026-01-01T01:00:00Z")

future = db.log_event("personal", "study", "期末考试", ts=utc(timedelta(hours=1)))
past = db.log_event("personal", "study", "上次考试", ts=utc(timedelta(hours=-1)))
noarg = db.log_event("personal", "meal", "刚吃的")
ok("ts 在未来 → planned", db.get_log(future)["status"] == "planned")
ok("ts 在过去 → done", db.get_log(past)["status"] == "done")
ok("不传 ts → done", db.get_log(noarg)["status"] == "done")

edge = db.log_event("personal", "other", "边界", ts=db.now())
ok("ts 恰好等于现在 → done（边界用 > 不是 >=）",
   db.get_log(edge)["status"] == "done")

tonight = db.log_event("personal", "other", "今晚",
                       ts=db.from_local(
                           (datetime.now().astimezone() + timedelta(hours=6))
                           .strftime(db.LOCAL_FMT)))
ok("本地时间 → UTC 换算后仍判成 planned（六小时后）",
   db.get_log(tonight)["status"] == "planned")

db.correct_log(future, ts=utc(timedelta(hours=-2)))
ok("改 ts 不自动重算 status（仍是 planned）",
   db.get_log(future)["status"] == "planned")


# ---------- 4. 检索 ----------

print("\n检索")

far = db.log_event("appointment", "travel", "去宣城找朋友",
                   ts=utc(timedelta(days=90)), place="宣城",
                   note="租车带我去泾县转一转")

got, _ = db.find_logs(days=1)
ok("days 只是下界：三个月后的记录也在结果里", any(r["id"] == far for r in got))

got, _ = db.find_logs(until=utc(timedelta(days=-1)))
ok("until 能排除未来的记录", not any(r["id"] == far for r in got))

got, _ = db.find_logs(category="appointment", kind="travel",
                   status="planned", place="宣城", days=3650)
ok("四个条件同时给，结果正确",
   [r["id"] for r in got] == [far], f"拿到 {[r['id'] for r in got]}")

got, _ = db.find_logs(place="随园", days=3650)
ok("place 为空的记录不参与匹配（没有 place != '' 就会全中）",
   all(r["place"] for r in got) and len(got) == 1,
   f"拿到 {[(r['id'], r['place']) for r in got]}")

ok("双向包含：库存「随园餐厅」，查高德的长名字能命中",
   db.find_logs(place="随园餐厅(仙鹤门店)", days=3650)[1] == 1)
ok("双向包含：「随园别院」不该命中",
   db.find_logs(place="随园别院", days=3650)[1] == 0)

ok("kind=meal 不会混进 study / travel",
   all(r["kind"] == "meal" for r in db.find_logs(kind="meal", days=3650)[0]))

# keyword 必须三列一起搜。第三批实测：库里五条跟"老王"有关的记录，
# name 列写的是"健身房""打羽毛球""徒步"，只有一条含"老王" ——
# 当初这个参数只搜 name，模型查一次拿到一条，就回答"你只跟他出去过一次"。
db.log_event("appointment", "exercise", "健身房", note="跟老王一起去的", ts=utc(timedelta(days=-3)))
db.log_event("appointment", "travel", "徒步", note="跟老王去的", ts=utc(timedelta(days=3)))
db.log_event("personal", "meal", "牛肉面", note="有点咸", place="老王面馆",
             ts=utc(timedelta(days=-1)))
ok("keyword 搜 name 列", db.find_logs(keyword="徒步", days=3650)[1] == 1)
ok("keyword 搜 note 列（人名几乎只存在于这里）",
   db.find_logs(keyword="老王", days=3650)[1] == 3,
   f"只拿到 {db.find_logs(keyword='老王', days=3650)[1]} 条")
ok("keyword 搜 place 列", any(r["place"] == "老王面馆"
                             for r in db.find_logs(keyword="面馆", days=3650)[0]))
ok("keyword 是子串不是语义：搜「面条」找不到「牛肉面」",
   db.find_logs(keyword="面条", days=3650)[1] == 0)
ok("keyword 空串不当条件用（否则一查全中）",
   db.find_logs(keyword="  ", days=3650)[1] == db.find_logs(days=3650)[1])

# 时间窗的默认值取决于有没有关键词。关键词本身就是筛子，所以搜全库不心疼；
# 会失控的是没有关键词的全量查询，那种才需要时间窗兜着。
# 不这么做的后果实测过：模型调 find_logs(keyword=小明) 不带 days，
# 一年后会漏掉三十天以前的全部，还照样给出一个自信的结论。
old = db.log_event("appointment", "meal", "两年前跟老王吃的饭",
                   note="跟老王", ts=utc(timedelta(days=-730)))
ok("给了 keyword 就不加时间下界（两年前的记录搜得到）",
   any(r["id"] == old for r in db.find_logs(keyword="老王")[0]),
   "默认值规则没生效，keyword 查询被 30 天窗口截住了")
ok("给了 place 同样不加时间下界",
   db.find_logs(place="随园餐厅")[1] == 1)
ok("没给关键词时仍然只看最近 30 天",
   not any(r["id"] == old for r in db.find_logs()[0]))
ok("模型显式传了 days 就以它为准，默认值不许顶掉",
   not any(r["id"] == old for r in db.find_logs(keyword="老王", days=7)[0]))

# 截断必须让调用方知道。只截不说，模型会拿一个残缺集合下结论 ——
# 那正是实测过的"查到一条就回答只有这一次"的翻版。
for i in range(70):
    db.log_event("personal", "other", f"批量{i}", ts=utc(timedelta(days=-i - 1)))
rows, total = db.find_logs(keyword="批量")
ok("超过上限时按 limit 截断", len(rows) == db.FIND_LIMIT, f"回了 {len(rows)} 行")
ok("返回的总数是真实总数，不是截断后的长度", total == 70, f"总数报了 {total}")
ok("limit 可以显式放宽", len(db.find_logs(keyword="批量", limit=100)[0]) == 70)

ok("place_history 委托给 find_logs，结果一致",
   [r["id"] for r in db.place_history("随园餐厅")]
   == [r["id"] for r in db.find_logs(place="随园餐厅", days=3650)[0]])
ok("place_history 空串返回空", db.place_history("  ") == [])


# ---------- 4.5 工具参数的契约 ----------
#
# 交界处必须有契约（文档 12.2）。这里改之前是一句裸的 json.loads —— 模型
# 吐一个畸形 JSON，异常直接冒泡，整轮工具调用全废。而它吐畸形不是偶发：
# 实测同一句话连试三次，三次都把日期写成了没引号的字面量。

print("\n工具参数的契约")

import llm as _llm  # noqa: E402

a, e = _llm._parse_args('{"kind": "meal", "name": "x"}')
ok("正常 JSON 照常解析", a == {"kind": "meal", "name": "x"} and not e)

a, e = _llm._parse_args('{"name": "看话剧", "ts": 2026-08-07 19:00}')
ok("日期少引号能补上（模型的稳定畸形）",
   a.get("ts") == "2026-08-07 19:00" and not e, f"拿到 {a}, {e}")

a, e = _llm._parse_args('{"note": "他说"来不了""}')
ok("补不了的如实退回，不抛异常", a == {} and "不是合法 JSON" in e)
ok("退回的话要指名道姓说错在哪", "ts" in e and "引号" in e)

a, e = _llm._parse_args(None)
ok("参数为空当成空字典", a == {} and not e)


# ---------- 4.6 回合内去重 ----------
#
# 实测两次同一个模式：模型 find_logs 看见了 id=38「看车展」，下一圈还是
# log_event 又写了一条。它越常被问起的事越容易记重，而重复记录会污染
# 后面所有的统计（"我跟老王出去过几次"会多算）。

print("\n回合内去重")

fresh_db()
existing = db.log_event("appointment", "travel", "看车展",
                        note="小明说带我去", ts=utc(timedelta(days=2)))

# 模拟一个回合：先 find_logs（会往 seen 里登记），再 log_event（该被拦）
seen: dict[str, int] = {}
_llm._run_tool("find_logs", {"keyword": "看车展"}, seen)
ok("find_logs 会把查到的记录登记进 seen", seen.get("看车展") == existing, str(seen))

n0 = db.find_logs(days=3650)[1]
out = _llm._run_tool("log_event", {"category": "appointment", "kind": "travel",
                                   "name": "看车展"}, seen)
ok("同一回合里再记同名 → 拦下，一行都不写", db.find_logs(days=3650)[1] == n0)
ok("拦下时要告诉模型是哪一条、该怎么办",
   str(existing) in out and "correct_log" in out, out)

# 换一个回合 = 换一个 seen。同一天吃两次牛肉面是合法的，不能靠"同名同日"硬拦
n1 = db.find_logs(days=3650)[1]
_llm._run_tool("log_event", {"category": "appointment", "kind": "travel",
                             "name": "看车展"}, {})
ok("跨回合的同名记录照常写得进（别误伤「同一天吃两次牛肉面」）",
   db.find_logs(days=3650)[1] == n1 + 1)


# ---------- 5. 渲染 ----------
#
# 这一节盯的是两级分类改动**引入**的那个 bug：place_history 按 ts 倒序，
# 未来的计划排在最前，原来的 rows[0] 就把"下周约了要去"当成了"最近去过一次"。

print("\n渲染")

import llm      # noqa: E402  放在这里是因为它会拖进 openai，前面几节用不着
import places   # noqa: E402

fresh_db()
db.log_event("personal", "meal", "烤羊腿", ts=utc(timedelta(days=-15)),
             place="翠苑餐厅", note="排队久但值")
db.log_event("personal", "meal", "牛肉面", ts=utc(timedelta(days=-3)), place="翠苑餐厅")
db.log_event("appointment", "meal", "和张三吃饭", ts=utc(timedelta(days=3)),
             place="翠苑餐厅", note="张三请客")
db.log_event("appointment", "meal", "和李四吃饭", ts=utc(timedelta(days=2)), place="新店")
overdue = db.log_event("appointment", "exercise", "约了健身",
                       ts=utc(timedelta(days=1)), place="老地方")
with db.connect() as c:      # 造一条"说好了，日子过了，没人确认"的
    c.execute("UPDATE logs SET ts = ? WHERE id = ?", (utc(timedelta(days=-2)), overdue))

both = llm._been_there("翠苑餐厅")
ok("done + planned 都有：两段都在", "去过2次" in both and "还约了要去" in both, both)
ok("done + planned 都有：最近一次取 done 里最新的，不是未来那条",
   "牛肉面" in both, both)

only_planned = llm._been_there("新店")
ok("只有 planned：**输出里绝不能出现「去过」**", "去过" not in only_planned,
   f"这正是改动引入的那个 bug：{only_planned}")
ok("只有 planned：说清是约了要去", "约了要去" in only_planned, only_planned)

ok("planned 且已过期：点出来问一句", "去没去" in llm._been_there("老地方"))
ok("一条记录都没有：返回空串，不留光秃秃的箭头", llm._been_there("没去过的店") == "")
ok("CLI 版 history_note 同样不会把 planned 说成去过",
   "去过" not in places.history_note("新店"))

rows = {r["name"]: r for r in db.find_logs(days=3650)[0]}
ok("_fmt_log 给未来的事标 [计划中]", "[计划中]" in llm._fmt_log(rows["和张三吃饭"]))
ok("_fmt_log 给过期未确认的标 [⏰ 早就过了]",
   "早就过了" in llm._fmt_log(rows["约了健身"]))
ok("_fmt_log 不给 done 加状态后缀",
   "[" not in llm._fmt_log(rows["牛肉面"]).split("id=")[1])
ok("_fmt_log 带上了两级分类",
   "appointment/exercise" in llm._fmt_log(rows["约了健身"]))


# ---------- 6. 留痕 ----------

print("\n留痕")

import json                                                 # noqa: E402

# 自己造一次更正，不依赖前面几节 —— 上一节 fresh_db() 过，库是新的
db.correct_log(rows["约了健身"]["id"], status="done", note="去了，人很多")
with db.connect() as c:
    ev = c.execute("SELECT payload FROM events WHERE type='correct_log'"
                   " ORDER BY id DESC LIMIT 1").fetchone()
p = json.loads(ev["payload"])
ok("correct_log 的 events 留痕带上了 status 新旧值",
   "status" in p["old"] and "status" in p["new"])

# append_note vs correct_log 的分工：一个补一个换。
# 搞混了不报错，只是几个月后你发现当初记的细节被自己一句评价洗掉了。
nid = db.log_event("appointment", "exercise", "打羽毛球", note="老王叫的")
db.append_note(nid, "人挺多的")
ok("append_note 接在原备注后面，不覆盖",
   db.get_log(nid)["note"] == "老王叫的；人挺多的", db.get_log(nid)["note"])
with db.connect() as c:
    p2 = json.loads(c.execute("SELECT payload FROM events WHERE type='append_note'"
                              " ORDER BY id DESC LIMIT 1").fetchone()["payload"])
ok("append_note 也留痕（旧值查得回来）", p2["old"] == "老王叫的")

db.append_note(db.log_event("personal", "meal", "面"), "还行")
ok("原来没备注时不留下多余的分隔符",
   db.find_logs(keyword="面")[0][0]["note"] == "还行")
ok("append_note 对不存在的 id 返回 False", db.append_note(99999, "x") is False)

db.correct_log(nid, note="改成这句")
ok("correct_log 仍然是覆盖语义", db.get_log(nid)["note"] == "改成这句")

with db.connect() as c:
    bad = c.execute("SELECT count(*) FROM logs WHERE category NOT IN (?,?)"
                    " OR kind NOT IN (?,?,?,?,?,?,?)"
                    " OR status NOT IN (?,?)",
                    (*db.CATEGORIES, *db.KINDS, *db.STATUSES)).fetchone()[0]
ok("库里没有任何越界的分类值", bad == 0)


# ---------- 7. 主动性：闸门与 review ----------
#
# 这一节的断言全部走 ts 参数，不碰系统时钟。所有时刻都用 db.from_local
# 从本地时间换算过去 —— 这样断言在任何时区下都成立，而且**在非 UTC 时区下
# 才真正有威慑力**：把 local_day 写成 ts[:10] 的话，下面第一条当场就红。

print("\n主动性：闸门")

import gate                                                  # noqa: E402
import review                                                # noqa: E402

fresh_db()

D = "2026-08-14"


def at(hm: str) -> str:
    """本地时间 "2026-08-14 22:00" -> UTC ISO。"""
    return db.from_local(f"{D} {hm}")


def planned(category: str, kind: str, name: str, ts: str,
            ts_precision: str = "exact") -> int:
    """记一条**计划中**的事。

    不能只靠 log_event —— 它的 status 是拿 ts 跟**真实的现在**比出来的
    （db._status_for），而这一节用的是 2026-08-14 这个固定日期，
    跑自检的那天一过它就全变成 done 了，断言会莫名其妙地红。
    所以显式改回 planned，让这一节的结果跟今天几号无关。
    """
    row_id = db.log_event(category, kind, name, ts=ts, ts_precision=ts_precision)
    db.correct_log(row_id, status="planned")
    return row_id


# --- 本地日 vs UTC 日 ---
# 这两条是整节的地基。东八区本地 07:30 时 UTC 还停在前一天，
# 西半球本地 22:00 时 UTC 已经是第二天 —— 两边各错一头，
# 而 ts[:10] 这种写法在两边都会静静地给出错误答案。
ok("本地清晨算当天（UTC 可能还在昨天）", db.local_day(at("07:30")) == D)
ok("本地深夜算当天（UTC 可能已到明天）", db.local_day(at("22:00")) == D)

# --- 静默时段 ---
ok("23:35 在静默时段内", gate.in_quiet_hours(at("23:35")))
ok("06:00 在静默时段内", gate.in_quiet_hours(at("06:00")))
ok("中午不在静默时段", not gate.in_quiet_hours(at("12:00")))
ok("23:29 还没进静默时段", not gate.in_quiet_hours(at("23:29")))

# --- dedupe_key 幂等 ---
first = db.propose_nudge("review", "第一次", dedupe_key="k1")
again = db.propose_nudge("review", "第二次", dedupe_key="k1")
ok("同 dedupe_key 重复提案返回 None", again is None)
ok("同 dedupe_key 不产生第二行", len(db.pending_nudges()) == 1)
ok("重复提案没有覆盖已有内容", db.get_nudge(first)["payload"] == "第一次")

# --- 闸门四条 ---
n = db.get_nudge(first)
ok("静默时段拦住", gate.check(n, at("23:40")) == (False, "quiet_hours"))
ok("窗口内放行", gate.check(n, at("22:00")) == (True, None))

expiring = db.get_nudge(db.propose_nudge(
    "other", "过期的", dedupe_key="k2", expires_ts=at("20:00")))
ok("过期的先于静默时段被判掉",
   gate.check(expiring, at("23:40")) == (False, "expired"))

# 同类冷却：review 的冷却是 20 小时
db.fire_nudge(first, at("22:00"))
n2 = db.get_nudge(db.propose_nudge("review", "同一类的下一条", dedupe_key="k3"))
ok("同 kind 冷却期内被拦",
   gate.check(n2, at("22:30")) == (False, "cooldown"))
ok("不同 kind 不受这条冷却影响",
   gate.check(db.get_nudge(db.propose_nudge("other", "别的类", dedupe_key="k4")),
              at("22:30")) == (True, None))

# --- 每日预算按本地日 ---
fresh_db()
for i, hm in enumerate(("08:00", "12:00", "18:00")):
    db.fire_nudge(db.propose_nudge(f"k{i}", "x", dedupe_key=f"d{i}"), at(hm))
ok("同一本地日的三条都算进今天", len(db.fired_on(D)) == 3)
ok("预算用完后第四条被拦",
   gate.check(db.get_nudge(db.propose_nudge("k9", "x", dedupe_key="d9")),
              at("21:00")) == (False, "daily_budget"))
# 前一天发的不该占今天的额度
ok("昨天发的不算今天",
   len(db.fired_on(db.local_day(db.from_local(f"2026-08-13 12:00")))) == 0)

# --- muted 的确定性后果 ---
fresh_db()
base = gate.cooldown_minutes("review")
mid = db.propose_nudge("review", "x", dedupe_key="m1")
db.fire_nudge(mid, at("22:00"))
db.set_outcome(mid, "muted")
ok("被 mute 一次冷却翻倍", gate.cooldown_minutes("review") == base * 2)
for i in range(2):
    other = db.propose_nudge("review", "x", dedupe_key=f"m{i + 2}")
    db.fire_nudge(other, at("22:00"))
    db.set_outcome(other, "muted")
ok("被 mute 到上限直接停用", gate.cooldown_minutes("review") == float("inf"))
ok("停用后一律拦下，原因记 cooldown",
   gate.check(db.get_nudge(db.propose_nudge("review", "x", dedupe_key="m9")),
              at("22:00")) == (False, "cooldown"))

raises("dropped_reason 不认自造的值", db.drop_nudge, mid, "我不想发")
raises("outcome 不认自造的值", db.set_outcome, mid, "还行吧")

# --- gate.run 把被拦的原因落库 ---
fresh_db()
blocked = db.propose_nudge("review", "深夜这条", dedupe_key="r1")
ok("静默时段 run 一条都不放行", gate.run(at("23:45")) == [])
ok("被拦的原因写进了库", db.get_nudge(blocked)["dropped_reason"] == "quiet_hours")
ok("被拦的不再出现在待发队列", db.pending_nudges() == [])


print("\n主动性：review")

fresh_db()

# --- 播报窗口 ---
ok("21:00 还没到播报窗口", not review.in_window(at("21:00")))
ok("22:00 在播报窗口内", review.in_window(at("22:00")))
ok("23:40 已经出了播报窗口", not review.in_window(at("23:40")))

# --- 数据源：ts_precision 的分工 ---
# 定死了时间的过期计划才该被追问"做了没"；
# "这周末跟家人聚餐"那种 ts 是代码填的区间起点，拿它追问是纯误报。
exact_id = planned("appointment", "study", "期末考试", at("09:00"), "exact")
vague_id = planned("appointment", "meal", "跟家人聚餐", at("10:00"), "week")
overdue = db.overdue_logs(at("22:00"))
ok("过期未确认只收 exact 的", [r["id"] for r in overdue] == [exact_id])
ok("没定死时间的不会被追问做了没",
   vague_id not in [r["id"] for r in overdue])

# 反过来，"明天有什么"要把没定死的也说出来 —— 说错了只是一句废话
ahead = db.upcoming_logs(days=1, ts=at("22:00"))
future = planned("personal", "travel", "大概去趟宣城",
                 db.from_local("2026-08-15 10:00"), "day")
ok("明天预告收没定死时间的",
   future in [r["id"] for r in db.upcoming_logs(days=1, ts=at("22:00"))])
ok("已经过去的不算明天的事", exact_id not in [r["id"] for r in ahead])

# --- db.when 按精度换措辞 ---
ok("exact 显示到分钟", ":" in db.when(db.get_log(exact_id)))
ok("week 不显示钟点", "那周" in db.when(db.get_log(vague_id))
   and ":" not in db.when(db.get_log(vague_id)))

# --- 排序 ---
fresh_db()
ids = {
    "约人吃饭": planned("appointment", "meal", "约人吃饭", at("08:00")),
    "自己吃饭": planned("personal", "meal", "自己吃饭", at("09:00")),
    "自己考试": planned("personal", "study", "自己考试", at("10:00")),
}
for _ in range(40):                       # 把 meal 灌成高频，同时越过冷启动门槛
    db.log_event("personal", "meal", "又吃了一顿")
rows = db.overdue_logs(at("22:00"))
ranked = review.rank(rows, db.kind_counts(), db.total_logs())
names = [r["name"] for r in ranked if r["name"] in ids]
ok("有别人的排在最前", names[0] == "约人吃饭", f"实际顺序 {names}")
ok("低频的排在高频前面", names.index("自己考试") < names.index("自己吃饭"),
   f"实际顺序 {names}")

# 冷启动：数据太少时什么都是"低频"，那个信号是噪音，不该参与排序
few = review.rank(rows, {"meal": 100, "study": 1}, total=5)
ok("库存不够时频次不参与排序",
   [r["name"] for r in few if r["name"] in ids][:1] == ["约人吃饭"])

# --- 渲染 ---
ok("没话说就不播", review.render([], []) is None)

for i in range(5):                         # 凑到超过 REVIEW_LIMIT 才测得到熔断
    planned("personal", "other", f"待办{i}", at("07:00"))
many = db.overdue_logs(at("22:00"))
ok("测熔断的数据确实超了上限", len(many) > review.REVIEW_LIMIT)
text = review.render(many, [])
shown = sum(1 for ln in text.splitlines() if ln.strip().startswith("["))
ok("一次最多摆 REVIEW_LIMIT 条", shown == review.REVIEW_LIMIT,
   f"摆了 {shown} 条")
# 截断不说等于"把有的说成没有"，跟 find_logs 上一轮踩的是同一个坑
ok("截断了必须说还有多少条", f"还有 {len(many) - review.REVIEW_LIMIT} 条" in text)
ok("每一行都带 id（不带的话你没法说「第几条做了」）",
   all("[" in ln for ln in text.splitlines() if ln.strip().startswith("[")))

# --- propose 幂等 ---
fresh_db()
planned("appointment", "study", "期末考试", at("09:00"))
one = review.propose(at("22:00"))
two = review.propose(at("22:30"))
ok("同一晚开两次对话只生成一条提案", one is not None and two is None)
ok("窗口外不生成提案", review.propose(at("20:00")) is None)

# 今晚的提案在窗口结束时作废，绝不留到明早补播
ok("提案的失效时间是今晚窗口结束",
   db.get_nudge(one)["expires_ts"] == at("23:30"))

fresh_db()
ok("库里没有可说的事时不硬凑一条", review.propose(at("22:00")) is None)


print(f"\n全过了（{passed} 条断言）。临时库：{db.DB_PATH.parent}")
