"""读 .env 里的配置。

不用装 python-dotenv，十行就够了 —— 设计文档 3.4 的精神：
每加一个依赖都要问问它到底解决了什么问题。
"""

import sys
from pathlib import Path

ENV_PATH = Path(__file__).parent / ".env"


def _load() -> dict[str, str]:
    values: dict[str, str] = {}
    if not ENV_PATH.exists():
        return values
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        values[k.strip()] = v.strip().strip('"').strip("'")
    return values


_env = _load()

BASE_URL = _env.get("CADENCE_BASE_URL", "")
MODEL = _env.get("CADENCE_MODEL", "")
API_KEY = _env.get("CADENCE_API_KEY", "")


def check() -> None:
    """配置不全就说清楚缺什么再退出，别让它在调用时报一个看不懂的错。"""
    missing = [n for n, v in
               (("CADENCE_BASE_URL", BASE_URL),
                ("CADENCE_MODEL", MODEL),
                ("CADENCE_API_KEY", API_KEY)) if not v]
    if missing:
        print(f"配置不全，.env 里还缺：{'、'.join(missing)}")
        print(f"文件在：{ENV_PATH}")
        print("打开它，把你那家的三行取消注释并填上 KEY。")
        sys.exit(1)
