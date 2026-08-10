"""跟人当面确认的唯一入口。

单独成一个模块，是因为 llm.py 和 meals.py 都要用它，而 meals.py
不该为了问一句话就把 openai 和 config 拖进来。

⚠️ 两条纪律：

1. **确认必须是这样的确定性代码，不能只在 system prompt 里写
   "改动前请先确认"。** 模型可能听可能不听，而且事后你查不出这次
   为什么没听（设计文档 6.1 的同一个道理）。

2. **别在任何别的地方直接写 input() 来问 y/n。** 将来换语音时这个
   函数会整个换实现：登记一条待确认操作、立刻返回 False 让模型开口
   问"要改吗"，用户下一轮说"对"时再执行，并且代码强制待确认项的
   时间必须早于用户最后一次发言（防止模型同一轮里自问自答）。
   到那时调用方一行都不用动 —— 前提是所有确认都只从这里走。
   这就是设计文档 2.6 说的：把易变的交互层挤进尽量小的地方。
"""

import sys


def confirm(question: str, detail: str = "", indent: str = "    ") -> bool:
    """问用户一句，拿到明确的 y 才算同意。其余一切输入都算拒绝。"""
    if not sys.stdin.isatty():
        # 定时器 / 摄像头这类无人会话：没人能回车。直接拒绝，
        # 不要挂在那里等一个不会来的输入。
        return False

    print(f"\n{indent}{question}")
    if detail:
        print(f"{indent}{detail}")
    try:
        return input(f"{indent}[y/N] ").strip().lower() == "y"
    except (EOFError, KeyboardInterrupt):
        print()
        return False
