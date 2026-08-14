#!/usr/bin/env python3
"""看它主动说了什么、什么被拦了、为什么被拦。

    python3 nudges.py                    # 最近 7 天
    python3 nudges.py --days 30
    python3 nudges.py --outcome 3 acted  # 回写你的反应
    python3 nudges.py --mute review      # 明确说"别再提醒我这个"

**"它为什么没提醒我"才是难查的那个方向。** 没发生的事不留痕，你就永远
不知道闸门是不是拦错了 —— 所以每一条被拦的都带着原因躺在库里，这个 CLI
就是把它们摆出来（设计文档 6.2）。

跑一个月之后，最下面那张统计表会直接告诉你哪些场景该砍。
比任何主观感受都准（6.4）。
"""

import argparse
from collections import Counter

import db
import gate
import ui

# 只影响这个 CLI 的显示，不落库。
_MARK = {None: "·", "ignored": "·", "acked": "✓", "acted": "★", "muted": "✗"}


def fmt(r) -> str:
    when = db.to_local(r["fired_at"] or r["created_at"])
    if r["fired_at"]:
        head = f"  [{r['id']:>3}]  {when}  {_MARK[r['outcome']]} 发了"
        if r["outcome"]:
            head += f"（{r['outcome']}）"
    elif r["dropped_reason"]:
        head = f"  [{r['id']:>3}]  {when}  ✗ 拦了 —— {r['dropped_reason']}"
    else:
        head = f"  [{r['id']:>3}]  {when}  ⧗ 待发"
    # 播报内容可能好几行，缩进对齐着摆，别跟下一条混在一起
    body = "\n".join(f"        {ln}" for ln in (r["payload"] or "").splitlines())
    return f"{head}\n{body}" if body else head


def do_outcome(nudge_id: int, outcome: str) -> None:
    if outcome not in db.OUTCOMES:
        print(f"outcome 只能是 {' / '.join(db.OUTCOMES)} 之一。")
        return
    if not db.set_outcome(nudge_id, outcome):
        print(f"没有 id={nudge_id} 这条 nudge。")
        return
    print(f"已记下 id={nudge_id} 的反应：{outcome}")
    if outcome == "muted":
        _report_mute(db.get_nudge(nudge_id)["kind"])


def do_mute(kind: str) -> None:
    """把这个 kind 最近发过的一条标成 muted。

    为什么要挂在一条具体的 nudge 上，而不是记一个"这个 kind 关掉了"的开关：
    这样才留得下**是哪一天、因为哪一句话**被嫌弃的。开关只能告诉你
    "现在是关的"，说不出为什么关 —— 半年后你自己都想不起来。
    """
    rows = [r for r in db.recent_nudges(days=3650)
            if r["kind"] == kind and r["fired_at"] and r["outcome"] != "muted"]
    if not rows:
        print(f"没有发出去过的 {kind}，没什么可 mute 的。")
        return
    latest = rows[0]
    if not ui.confirm(f"把这条标成「别再提醒我」？", fmt(latest).strip(), indent=""):
        print("没改。")
        return
    db.set_outcome(latest["id"], "muted")
    _report_mute(kind)


def _report_mute(kind: str) -> None:
    """确定性后果必须当场说出来。

    6.4 的硬规则：muted 必须有确定性后果，**绝不能是"模型记住了你不喜欢"**。
    所以这里报的是代码算出来的新冷却时长，不是一句"好的我记住了"。
    """
    minutes = gate.cooldown_minutes(kind)
    if minutes == float("inf"):
        print(f"{kind} 已停用 —— 被 mute {gate.mute_count(kind)} 次，到上限了。")
    else:
        print(f"{kind} 的冷却已经变成 {minutes / 60:.0f} 小时"
              f"（被 mute {gate.mute_count(kind)} 次，每次翻倍）。")


def main() -> None:
    p = argparse.ArgumentParser(description="看主动发言的提案与结果")
    p.add_argument("--days", type=int, default=7, help="往前看几天，默认 7")
    p.add_argument("--outcome", nargs=2, metavar=("ID", "OUTCOME"),
                   help=f"回写反应：{' / '.join(db.OUTCOMES)}")
    p.add_argument("--mute", metavar="KIND", help="明确说别再提醒这一类")
    args = p.parse_args()

    if args.outcome:
        return do_outcome(int(args.outcome[0]), args.outcome[1])
    if args.mute:
        return do_mute(args.mute)

    rows = db.recent_nudges(days=args.days)
    if not rows:
        print(f"最近 {args.days} 天它一句主动的话都没说过。")
        print("review 只在晚上 21:30–23:30 之间开对话时才会触发。")
        return

    print(f"最近 {args.days} 天，{len(rows)} 条：\n")
    for r in rows:
        print(fmt(r))
        print()

    # 这张表是 6.4 说的"唯一能让主动性变好的信号"。
    # 一个月之后它会直接告诉你哪些场景该砍。
    stats = Counter((r["kind"], r["outcome"] or r["dropped_reason"] or "待发")
                    for r in rows)
    print("统计（kind × 结果）：")
    for (kind, result), n in sorted(stats.items()):
        print(f"  {kind:<10} {result:<14} {n}")


if __name__ == "__main__":
    main()
