#!/usr/bin/env python3
"""cadence 的第一个入口：敲字对话。

    python3 chat.py

这就是设计文档 13 节说的 v0 闭环 —— 敲字 → 查库 → 调模型 → 写库。
没有语音、没有摄像头、没有 agent 框架。

以后加语音、加摄像头，加的是新的入口，走的还是同一个 db.py 和 llm.py
（设计文档 5.5：所有路径读写同一个库，否则你会得到四个互不认识的机器人）。
"""

import config
import db
import llm


def main() -> None:
    config.check()

    n = len(db.active_memories())
    print(f"cadence（{config.MODEL}）· 已知 {n} 条关于你的事 · Ctrl-C 退出\n")

    messages = [{"role": "system", "content": llm.system_prompt()}]

    while True:
        try:
            user = input("你 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见。")
            return
        if not user:
            continue

        # events 记的是原话；messages 里那条会被 llm 缀上当前时间。
        # 留痕看 events，别看 messages。
        db.record_event("cli", "utterance", {"text": user})
        messages.append(llm.user_message(user))

        try:
            reply = llm.chat(messages)
        except Exception as e:
            # 降级而不是崩溃（设计文档 7.4 的精神）：说清楚发生了什么，对话继续。
            print(f"\n  ✗ 调用失败：{type(e).__name__}: {e}\n")
            messages.pop()
            continue

        messages.append({"role": "assistant", "content": reply})
        print(f"\ncadence > {reply}\n")


if __name__ == "__main__":
    main()
