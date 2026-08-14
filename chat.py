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
import review


def main() -> None:
    config.check()

    # 地点是会话状态：上一场对话查过新街口，这一场不该还停在新街口。
    llm.reset_center()

    n = len(db.active_memories())
    print(f"cadence（{config.MODEL}）· 已知 {n} 条关于你的事 · Ctrl-C 退出\n")

    messages = [{"role": "system", "content": llm.system_prompt()}]

    # 它先开口 —— cadence 的第一个主动场景（设计文档 6）。
    # 晚上 21:30–23:30 之间开对话才会有，其余时候这里什么都不发生。
    #
    # ⚠️ 这段话是 review.py 纯代码渲染的，**不过模型**。
    # 用 assistant 角色塞回历史，是因为那确实是助手说的话 —— 对话历史
    # 得是真的，模型才知道"我刚说过这些"，你回一句"第 12 条做了"
    # 它才接得上去 correct_log。
    if (opening := review.tonight()) is not None:
        print(f"cadence > {opening}\n")
        messages.append({"role": "assistant", "content": opening})

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
