# cadence · v0

设计文档见 `个人AI助手项目设计文档.md`。这里是它第 13 节说的 v0：
**敲字 → 查库 → 调模型 → 写库**。没有语音、没有摄像头、没有 agent 框架。

## 开始用

1. 打开 `.env`，把你那家模型的三行取消注释，填上 API KEY
2. `python3 chat.py`

## 命令

```bash
python3 chat.py                                    # 对话（需要 API KEY）

python3 log_meal.py 牛肉面 --note 有点咸            # 记一顿饭
python3 log_meal.py 麻辣烫 --at 12:30
python3 meals.py --days 7                          # 看最近吃了什么

python3 remember.py --list                         # 当前有效的记忆
python3 remember.py preference food.dislike.cilantro "不吃香菜"
python3 remember.py constraint diet.goal "在减脂" --expires 90
python3 remember.py --history food.dislike.cilantro   # 一个 key 的全部版本
```

不用 API KEY 也能跑除 `chat.py` 外的全部命令。

## 文件

| 文件 | 作用 |
|---|---|
| `db.py` | **所有读写的唯一入口**。没有 `execute_sql()`，只有具体函数（文档 5.7） |
| `llm.py` | system prompt、工具定义、工具调用循环 |
| `config.py` / `.env` | 模型配置。换厂商只改 `.env` |
| `chat.py` | 对话入口 |
| `log_meal.py` / `meals.py` / `remember.py` | 独立 CLI 脚本（文档 8.2） |
| `cadence.db` | SQLite 单文件。删掉就是重来 |

## 已经定下来的纪律

来自设计文档，现在遵守能省很多事：

- **时间戳一律存 UTC + ISO 8601**，只在显示时转本地时区（5.6）
- **记忆追加不覆盖**，所以 `--history` 查得到"它为什么这么以为"（5.4）
- **`memories.key` 用归一化英文**（`food.dislike.cilantro`），人话放 `value`（5.4）
- **模型只能调窄接口**，不能自己写 SQL（5.7）
- **查不到就说查不到，不编**（7.4）
- 每次模型调用的 token 数写进 `events` 表 —— 没有账本就没有优化（12.1）

## 下一步

按文档 13 节的路线图，阶段 1 还差**高德 POI 推荐**：
用饮食日志筛选候选餐厅，验证文档 7.5 的判断 ——
赢过美团的不是商家数据，是"你上周吃了三次辣、这家你说过难吃"。

两周后的通过判据（事先写好，达不到就砍不修）：
**我主动用了 ≥10 次，且 ≥3 次真按它说的吃了。**

## 备份

```bash
cp cadence.db ~/cadence-backup-$(date +%F).db
```

文档 3.1：定时 `cp` 到另一块盘即备份。不需要比这更复杂的东西。

## 看数据库

装 [DB Browser for SQLite](https://sqlitebrowser.org/)，双击打开 `cadence.db`。
"能亲眼看见表里的数据"比任何教程都有用（文档 5.6）。
