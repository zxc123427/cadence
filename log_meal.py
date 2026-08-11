#!/usr/bin/env python3
"""记一顿饭。

    python3 log_meal.py 牛肉面
    python3 log_meal.py 牛肉面 --note 有点咸
    python3 log_meal.py 麻辣烫 --at 12:30

这是设计文档 8.2 说的那种 CLI 脚本：以后语音、事件循环、别的 AI 工具
调的都是同一个脚本，不用为每个入口重写一遍。
"""

import argparse
from datetime import datetime

import db


def parse_at(hhmm: str) -> str:
    """把 '12:30' 理解成今天本地时间的 12:30，转成 UTC 存。

    补上今天的日期就交给 db.from_local —— 时区换算和格式字面量
    只该有一处实现，别在每个入口各写一遍。
    """
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    return db.from_local(f"{today} {hhmm}")


def main() -> None:
    p = argparse.ArgumentParser(description="记录一顿饭")
    p.add_argument("name", help="吃了什么，例如 牛肉面")
    p.add_argument("--note", help="备注，例如 有点咸 / 太贵了")
    p.add_argument("--at", help="几点吃的，格式 HH:MM，默认现在")
    args = p.parse_args()

    ts = parse_at(args.at) if args.at else None
    db.log_meal(args.name, note=args.note, ts=ts)

    when = db.to_local(ts) if ts else "刚刚"
    print(f"记下了：{args.name}（{when}）" + (f" —— {args.note}" if args.note else ""))


if __name__ == "__main__":
    main()
