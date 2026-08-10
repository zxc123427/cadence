#!/usr/bin/env python3
"""手动记一条偏好，以及查看当前所有记忆。

    python3 remember.py --list
    python3 remember.py preference food.dislike.cilantro "不吃香菜"
    python3 remember.py constraint diet.goal "这段时间在减脂" --expires 90
    python3 remember.py --history food.dislike.cilantro

设计文档 13 节的 v0 就是这一步：手动填十条偏好，每次对话注入 system prompt。
跑一周就能感觉到"它记得我" —— 这一步不需要任何自动抽取逻辑。

关于 key：别直接用自然语言（见 5.4）。存了"香菜"，那"芫荽"算不算命中？
用 food.dislike.cilantro 这种归一化的 key，人话放 value 里。
"""

import argparse
from datetime import datetime, timedelta, timezone

import db

CATEGORIES = ("preference", "constraint", "fact")


def show_list() -> None:
    rows = db.active_memories()
    if not rows:
        print("还没有任何记忆。试试：")
        print('  python3 remember.py preference food.dislike.cilantro "不吃香菜"')
        return
    print(f"当前有效记忆 {len(rows)} 条：\n")
    current = None
    for r in rows:
        if r["category"] != current:
            current = r["category"]
            print(f"[{current}]")
        expiry = f"  ⏳ {db.to_local(r['expires_at'])} 过期" if r["expires_at"] else ""
        print(f"  {r['key']:<32} {r['value']}{expiry}")


def show_history(key: str) -> None:
    rows = db.memory_history(key)
    if not rows:
        print(f"没有 key 为 {key} 的记忆。")
        return
    print(f"{key} 的全部版本（新 → 旧）：\n")
    for i, r in enumerate(rows):
        mark = "→ 当前" if i == 0 else "  历史"
        print(f"  {mark}  {db.to_local(r['created_at'])}  {r['value']}  [来源 {r['source']}]")


def main() -> None:
    p = argparse.ArgumentParser(description="记录 / 查看偏好")
    p.add_argument("category", nargs="?", choices=CATEGORIES, help="记忆类别")
    p.add_argument("key", nargs="?", help="归一化的 key，如 food.dislike.cilantro")
    p.add_argument("value", nargs="?", help="人话，如 不吃香菜")
    p.add_argument("--expires", type=int, metavar="天", help="多少天后失效，不填就是永久")
    p.add_argument("--list", action="store_true", help="列出当前有效的记忆")
    p.add_argument("--history", metavar="KEY", help="查看某个 key 的全部历史版本")
    args = p.parse_args()

    if args.list:
        return show_list()
    if args.history:
        return show_history(args.history)
    if not (args.category and args.key and args.value):
        p.print_help()
        return

    expires_at = None
    if args.expires:
        expires_at = (datetime.now(timezone.utc) + timedelta(days=args.expires)
                      ).strftime("%Y-%m-%dT%H:%M:%SZ")

    old = db.memory_history(args.key)
    db.remember(args.category, args.key, args.value, expires_at=expires_at)

    print(f"记住了：{args.key} = {args.value}")
    if old:
        print(f"（这是第 {len(old) + 1} 个版本，上一版是「{old[0]['value']}」，旧版本保留着）")


if __name__ == "__main__":
    main()
