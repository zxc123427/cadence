#!/usr/bin/env python3
"""看看最近吃了什么。

    python3 meals.py
    python3 meals.py --days 30
"""

import argparse
from collections import Counter

import db
import ui


def do_delete(row_id: int) -> None:
    """删除入口只在这里 —— 模型没有这个工具，只有你自己能删。

    删除不可逆而更正可逆，所以这个权限留在人手里（设计文档 5.7）。
    """
    row = db.get_log(row_id)
    if row is None:
        print(f"没有 id={row_id} 这条记录。")
        return
    detail = (f"{db.to_local(row['ts'])}  {row['name']}"
              + (f" @{row['place']}" if row["place"] else "")
              + (f"（{row['note']}）" if row["note"] else ""))
    if not ui.confirm("确定删除这条吗？", detail, indent=""):
        print("没删。")
        return
    db.delete_log(row_id)
    print(f"已删除 id={row_id}（内容已记进 events，真删错了还能查回来）")


def main() -> None:
    p = argparse.ArgumentParser(description="查看最近的饮食记录")
    p.add_argument("--days", type=int, default=7, help="往前看几天，默认 7")
    p.add_argument("--delete", type=int, metavar="ID", help="删除指定 id 的记录")
    args = p.parse_args()

    if args.delete is not None:
        return do_delete(args.delete)

    rows = db.recent_meals(args.days)
    if not rows:
        print(f"最近 {args.days} 天没有记录。用 log_meal.py 记一条试试。")
        return

    print(f"最近 {args.days} 天，{len(rows)} 条记录：\n")
    for r in rows:
        # 带上 id：删除和更正都要靠它定位
        line = f"  [{r['id']:>3}]  {db.to_local(r['ts'])}  {r['name']}"
        if r["place"]:
            line += f"  @{r['place']}"
        if r["note"]:
            line += f"  （{r['note']}）"
        print(line)

    repeats = [(n, c) for n, c in Counter(r["name"] for r in rows).most_common() if c > 1]
    if repeats:
        print("\n吃过不止一次的：" + "、".join(f"{n} ×{c}" for n, c in repeats))

    # 去过不止一次的店 —— 这是推荐时最有用的一行（设计文档 7.5）
    visits = Counter(r["place"] for r in rows if r["place"])
    if again := [(p, c) for p, c in visits.most_common() if c > 1]:
        print("常去的店：" + "、".join(f"{p} ×{c}" for p, c in again))


if __name__ == "__main__":
    main()
