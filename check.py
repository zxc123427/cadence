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


def columns(path: Path) -> dict:
    """PRAGMA table_info 转成 {列名: (类型, 非空, 默认值)}。

    用字典而不是列表：新建库里 category 是第 2 列，升级库里它排在最后 ——
    顺序本来就不一样，比顺序会误报。要比的是每一列长得对不对。
    """
    conn = sqlite3.connect(path)
    try:
        return {r[1]: (r[2], r[3], r[4]) for r in conn.execute("PRAGMA table_info(logs)")}
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

cols_migrated = columns(db.DB_PATH)
db.connect().close()                                        # 再跑一次
ok("_migrate 幂等（连跑两次列不变）", columns(db.DB_PATH) == cols_migrated)
ok("连跑两次数据不变", db.find_logs(days=3650)[1] == 3)

# 新建库 vs 升级库：两条路必须造出一模一样的表。
# SCHEMA 和 NEW_COLUMNS 是两处写法，漂了就会出现"新装的机器和老机器不一样"，
# 而这种差异要等到你换电脑那天才暴露。
migrated_path = db.DB_PATH
db.DB_PATH = migrated_path.parent / "brandnew.db"
db.connect().close()
ok("新建库和升级库的表结构完全一致", columns(db.DB_PATH) == cols_migrated,
   f"新建 {columns(db.DB_PATH)}\n  升级 {cols_migrated}")
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

with db.connect() as c:
    bad = c.execute("SELECT count(*) FROM logs WHERE category NOT IN (?,?)"
                    " OR kind NOT IN (?,?,?,?,?,?,?)"
                    " OR status NOT IN (?,?)",
                    (*db.CATEGORIES, *db.KINDS, *db.STATUSES)).fetchone()[0]
ok("库里没有任何越界的分类值", bad == 0)


print(f"\n全过了（{passed} 条断言）。临时库：{db.DB_PATH.parent}")
