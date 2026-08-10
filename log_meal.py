#!/usr/bin/env python3
"""记一顿饭。

    python3 log_meal.py 牛肉面
    python3 log_meal.py 牛肉面 --note 有点咸
    python3 log_meal.py 麻辣烫 --at 12:30

这是设计文档 8.2 说的那种 CLI 脚本：以后语音、事件循环、别的 AI 工具
调的都是同一个脚本，不用为每个入口重写一遍。
"""

import argparse
from datetime import datetime, timezone

import db


def parse_at(hhmm: str) -> str:
    """把 '12:30' 理解成今天本地时间的 12:30，转成 UTC 存。"""
    h, m = (int(x) for x in hhmm.split(":"))
    local = datetime.now().astimezone().replace(hour=h, minute=m, second=0, microsecond=0)
    return local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
