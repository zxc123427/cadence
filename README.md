# cadence · v0.1

设计文档见 `个人AI助手项目设计文档.md`。这里是它第 13 节路线图的阶段 1：
**记饮食 → 查高德拿候选 → 用自己的记录筛 → 推荐**。
没有语音、没有摄像头、没有 agent 框架。

## 开始用

1. 打开 `.env`，把你那家模型的三行取消注释，填上 API KEY
2. 想用地点推荐再加两行（不加也能跑，只是没有 `places.py`）：
   ```
   CADENCE_AMAP_KEY=在 lbs.amap.com 申请，平台类型必须选「Web 服务」
   CADENCE_HOME=经度,纬度        # 用 python3 places.py --where 你家地址 查
   ```
3. `python3 chat.py`

## 命令

```bash
python3 chat.py                                    # 对话（需要 API KEY）

python3 log_meal.py 牛肉面 --note 有点咸            # 记一顿饭
python3 log_meal.py 麻辣烫 --at 12:30
python3 log_meal.py 烤羊腿 --place 随园餐厅          # 记在哪家店吃的
python3 meals.py --days 7                          # 看最近吃了什么（带 id）
python3 meals.py --delete 3                        # 删一条（只有你能删，模型不能）

python3 places.py --types                          # 这一带实际有哪些菜系
python3 places.py 四川菜                            # 查这类店，带上"你去过没有"
python3 places.py 四川菜,湘菜,火锅 --max-cost 60     # 多个关键词、限人均
python3 places.py 火锅 --at 新街口                   # 换个中心点，地址自动转坐标
python3 places.py --where 新街口                    # 只查坐标

python3 remember.py --list                         # 当前有效的记忆
python3 remember.py preference food.dislike.cilantro "不吃香菜"
python3 remember.py constraint diet.goal "在减脂" --expires 90
python3 remember.py --history food.dislike.cilantro   # 一个 key 的全部版本
```

不用模型 API KEY 也能跑除 `chat.py` 外的全部命令（`places.py` 要高德 key）。

## 文件

| 文件 | 作用 |
|---|---|
| `db.py` | **所有读写的唯一入口**。没有 `execute_sql()`，只有具体函数（文档 5.7） |
| `llm.py` | system prompt、工具定义、工具调用循环 |
| `amap.py` | 高德接口。**只回答"附近有什么"**，不含任何个人偏好逻辑 |
| `ui.py` | **跟人确认的唯一入口**。换语音时只改这一个文件 |
| `config.py` / `.env` | 模型和高德的配置。换厂商只改 `.env` |
| `chat.py` | 对话入口 |
| `log_meal.py` / `meals.py` / `remember.py` / `places.py` | 独立 CLI 脚本（文档 8.2） |
| `cadence.db` | SQLite 单文件。删掉就是重来 |

**高德的数据一条都不落库。** 库里只有你自己的东西：吃了什么、在哪吃的、
说过什么。候选集随时能重新拿到，个人记录拿不回来 —— 只有后者值得存。

## 已经定下来的纪律

来自设计文档，现在遵守能省很多事：

- **时间戳一律存 UTC + ISO 8601**，只在显示时转本地时区（5.6）
- **模型只说本地时间，UTC 不出 `db.py`。** 工具参数一律填 `2026-08-10 15:00` 这种本地格式，`db.from_local()` 负责换算。让模型自己算时区，它会算错，而且错了不报错——一个格式不对的字符串塞进 TEXT 列，SQL 比较只会安静地给出错误结果
- **当前时间缀在 user 消息上，不写进 system prompt。** prompt 缓存按前缀匹配，system 在 `messages` 最前面，它每轮一变，整段对话历史全部失配。变化的东西必须待在列表末尾
- **记忆追加不覆盖**，所以 `--history` 查得到"它为什么这么以为"（5.4）
- **`memories.key` 用归一化英文**（`food.dislike.cilantro`），人话放 `value`（5.4）
- **模型只能调窄接口**，不能自己写 SQL（5.7）
- **模型能改不能删。** 删除不可逆、更正可逆，风险差一档，所以删除权留在 CLI 里（5.7）
- **确认只能走 `ui.confirm()`，别在别处写 `input()`。** 写在 system prompt 里的"请先确认"不算数——模型可能不听，而且你查不出为什么（6.1）
- **查不到就说查不到，不编**（7.4）
- **候选集和判断依据必须分开。** 高德只回答"附近有什么"，"哪家合适"只能由
  `logs` 和 `memories` 回答。让高德评分参与主排序，就是放弃这个项目唯一的优势（7.5）
- **地点是会话状态，用代码兜住，而且必须回显。** 说过"新街口"之后模型可能忘了填
  `near` 就悄悄退回家，而你看不出来——所以 `llm.py` 记住上一个中心点，
  并且每次返回都写明用的是哪儿（6.1 的同一个道理）
- **高德不认得的词要当场丢掉。** 搜「辣的」它不返回 0 条，而是给你八家肯德基。
  乱猜比查不到更危险：查不到你会改词，乱猜你会真把肯德基当辣菜推出去（7.4）
- **地名一律限定城市，并且把解析出的完整地址显示出来。** 不限城市查「新街口」，
  高德给的是**四川乐山**的那个，还不报错。重名是常态不是意外
- 每次模型调用的 token 数写进 `events` 表 —— 没有账本就没有优化（12.1）
- **加表加列走 `db.py` 的 `NEW_COLUMNS`。** `CREATE TABLE IF NOT EXISTS` 对已存在的表
  不生效，光改 `SCHEMA` 老库不会长出新列。改结构前先 `cp cadence.db` 备份

## 已知问题

**空字符串能绕过确认框（已知，暂不修）**

`llm.py` 判断"模型要不要改这个字段"用的是真假值（`args.get("name")`），
`db.correct_log` 用的是 `is not None`。两边对空字符串的看法不一样：
模型传 `{"id": 2, "name": "", "note": "x"}`，确认框只会显示"备注改成 x"，
但 `correct_log` 会把 `name` 一并写成空串——**改了一个没经过确认的字段**。

暂不修是因为还没见模型真传过空串。修的话两边统一成 `is not None`，
并且把空串当非法输入挡掉（"清空"应该是显式操作，不是省略的副作用）。

**输入偶尔丢字符（不复现，决定不修）**

2026-08-10 出现过一次：终端里输入"我还吃了梅干菜烧肉，复热的时候…"，
到达 Python 的只有"复热的时候…"，前半句丢了，模型只好从上文推断，
把菜名记成了牛肉面。第二天原样再输一遍，正常。

不加防线，因为三种候选都比不加更糟：

- **回显输入** —— 没用。终端本来就在回显，字符在输入法那层丢了就没上过屏
- **检测"句子不完整"** —— 会误伤"对""删了""嗯"这类正常的短输入，天天弹确认
- **禁用 readline** —— 方向键和历史记录会一起没掉

真正的防线是**可查、可见、可改**，这三样已经有了：`events` 记原话、
工具调用当场回显、`correct_log` 能改回来。防不住的故障，保证它可追可修就够了。

再遇到时，这样确认 Python 实际收到了什么（跟屏幕上显示的对不上就是又发生了）：

```bash
sqlite3 cadence.db "SELECT payload FROM events WHERE type='utterance' ORDER BY id DESC LIMIT 5;"
```

## 下一步

**阶段 1 齐了，现在开始的两周是判据期。** 不加新功能，就用。

判据是事先写好的，达不到就砍不修：
**我主动用了 ≥10 次，且 ≥3 次真按它说的吃了。**

想让它变准，唯一要做的事是**吃完记一笔，带上店名和一句真话**：

```bash
python3 log_meal.py 烤羊腿 --place 随园餐厅 --note 排队久但值
```

去过的店下次会带着这句话出现在推荐里。记录攒不起来，推荐就只是个搜索框。

两周后再看这几件事值不值得做（现在都不做）：

- 高德那条慢路（"今天吃什么"要拉全量，四到六秒）——只缓存那一行分类目录就能解决
- 吃完主动问"这家怎么样"——属于文档 6 的主动性策略层，要走闸门
- `logs.place` 存菜系，才能回答"我这个月吃了几次川菜"

## 备份

```bash
cp cadence.db ~/cadence-backup-$(date +%F).db
```

文档 3.1：定时 `cp` 到另一块盘即备份。不需要比这更复杂的东西。

## 看数据库

装 [DB Browser for SQLite](https://sqlitebrowser.org/)，双击打开 `cadence.db`。
"能亲眼看见表里的数据"比任何教程都有用（文档 5.6）。
