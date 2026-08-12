#!/usr/bin/env python3
"""看记录，删记录。

    python3 logs.py                          # 最近 7 天的全部记录
    python3 logs.py --days 30
    python3 logs.py --kind meal              # 只看吃的
    python3 logs.py --category appointment   # 只看和别人有约的
    python3 logs.py --status planned         # 还没做的
    python3 logs.py --place 随园餐厅          # 这个地方的全部记录
    python3 logs.py --delete 3               # 删一条

这是 db.find_logs 的 CLI 外壳，跟 places.py 是 amap 的外壳一个路子。
它替代了原来的 meals.py：模型现在能记的不只是吃饭，
只能看见饮食的话，它写错一条考试记录你都找不到 id 去删。

**删除只有这里有。** 模型没有这个工具 —— 删除不可逆、更正可逆，
风险差一档，所以这个权限留在人手里（设计文档 5.7）。
"""

import argparse
from collections import Counter

import db
import ui


def fmt(r) -> str:
    """一行一条。跟 llm.py 的 _fmt_log 保持一致，只是这边带 id 更靠前。"""
    line = (f"  [{r['id']:>3}]  {db.to_local(r['ts'])}  "
            f"{r['category']}/{r['kind']}  {r['name']}")
    if r["place"]:
        line += f"  @{r['place']}"
    if r["note"]:
        line += f"  （{r['note']}）"
    if r["status"] == "planned":
        # 过期与否是现算的，不落库 —— 见 db._status_for 的注释
        line += "  [⏰ 早就过了]" if r["ts"] < db.now() else "  [计划中]"
    return line


def do_delete(row_id: int) -> None:
    row = db.get_log(row_id)
    if row is None:
        print(f"没有 id={row_id} 这条记录。")
        return
    if not ui.confirm("确定删除这条吗？", fmt(row).strip(), indent=""):
        print("没删。")
        return
    db.delete_log(row_id)
    print(f"已删除 id={row_id}（内容已记进 events，真删错了还能查回来）")


def main() -> None:
    p = argparse.ArgumentParser(description="查看和删除记录")
    p.add_argument("--days", type=int, default=7, help="往前看几天，默认 7")
    p.add_argument("--kind", choices=db.KINDS, help="小分类，不填就看全部")
    p.add_argument("--category", choices=db.CATEGORIES, help="大分类，不填就看全部")
    p.add_argument("--status", choices=db.STATUSES, help="做了没有，不填就看全部")
    p.add_argument("--place", help="按地点找，店名写全称或简称都行")
    p.add_argument("--delete", type=int, metavar="ID", help="删除指定 id 的记录")
    args = p.parse_args()

    if args.delete is not None:
        return do_delete(args.delete)

    rows = db.find_logs(days=args.days, kind=args.kind, category=args.category,
                        status=args.status, place=args.place)
    if not rows:
        print(f"最近 {args.days} 天没有符合条件的记录。用 log_meal.py 记一条试试。")
        return

    print(f"最近 {args.days} 天，{len(rows)} 条记录：\n")
    for r in rows:
        # 带上 id：删除和更正都要靠它定位
        print(fmt(r))

    repeats = [(n, c) for n, c in Counter(r["name"] for r in rows).most_common() if c > 1]
    if repeats:
        print("\n重复出现的：" + "、".join(f"{n} ×{c}" for n, c in repeats))

    # 去过不止一次的地方 —— 这是推荐时最有用的一行（设计文档 7.5）
    visits = Counter(r["place"] for r in rows if r["place"])
    if again := [(p, c) for p, c in visits.most_common() if c > 1]:
        print("常去的地方：" + "、".join(f"{p} ×{c}" for p, c in again))


if __name__ == "__main__":
    main()
