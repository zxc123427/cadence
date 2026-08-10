#!/usr/bin/env python3
"""看看最近吃了什么。

    python3 meals.py
    python3 meals.py --days 30
"""

import argparse
from collections import Counter

import db


def main() -> None:
    p = argparse.ArgumentParser(description="查看最近的饮食记录")
    p.add_argument("--days", type=int, default=7, help="往前看几天，默认 7")
    args = p.parse_args()

    rows = db.recent_meals(args.days)
    if not rows:
        print(f"最近 {args.days} 天没有记录。用 log_meal.py 记一条试试。")
        return

    print(f"最近 {args.days} 天，{len(rows)} 条记录：\n")
    for r in rows:
        line = f"  {db.to_local(r['ts'])}  {r['name']}"
        if r["note"]:
            line += f"  （{r['note']}）"
        print(line)

    repeats = [(n, c) for n, c in Counter(r["name"] for r in rows).most_common() if c > 1]
    if repeats:
        print("\n吃过不止一次的：" + "、".join(f"{n} ×{c}" for n, c in repeats))


if __name__ == "__main__":
    main()
